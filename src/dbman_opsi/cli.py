"""Command line interface for dbman-opsi."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import getpass
import json
import logging
import os
import stat
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from dbman_opsi.agent_scripts import generate_agent_scripts
from dbman_opsi.bastion_exec import BastionSqlRunner
from dbman_opsi.config import ConfigError, EnablementConfig, load_config, save_config, validate_config
from dbman_opsi.credentials import CredentialService
from dbman_opsi.credential_lifecycle import build_reset_plan
from dbman_opsi.cross_region import cross_region_plan, format_cross_region_plan, parse_regions
from dbman_opsi.datasafe import DataSafeDecision, DataSafeService
from dbman_opsi.db_incident import (
    DbIncidentEvidenceService,
    DbIncidentRequest,
    generate_db_incident_demo,
)
from dbman_opsi.db_check import parse_validation_output
from dbman_opsi.db_exec import DbExecService
from dbman_opsi.db_scripts import generate_db_scripts
from dbman_opsi.discovery import DiscoveryService
from dbman_opsi.disposable_release import (
    RELEASE_PHASES,
    build_release_evidence,
    generate_dashboard_definitions,
    generate_role_bootstrap_sql,
)
from dbman_opsi.doctor import check_environment, check_session, summarize_checks
from dbman_opsi.enablement import EnablementService
from dbman_opsi.envfile import load_env_file
from dbman_opsi.journal import RunJournal, summarize
from dbman_opsi.log_analytics import LogAnalyticsService, generate_logan_payloads
from dbman_opsi.oci_cli import OciCli
from dbman_opsi.opsi_diagnostics import generate_opsi_diagnostics
from dbman_opsi.opsi_payloads import generate_opsi_payloads
from dbman_opsi.orchestrator import ConfigureReport, ConfigureService
from dbman_opsi.preflight import PreflightService
from dbman_opsi.process_insights import ProcessInsightsService, format_process_insights_report
from dbman_opsi.prerequisites import PrerequisiteService
from dbman_opsi.regional_provisioning import (
    CHICAGO_REGION,
    RegionalProvisioningRequest,
    build_regional_provisioning_config,
    default_regional_output,
    prepare_regional_terraform_dir,
)
from dbman_opsi.redact import redact_data, redact_text
from dbman_opsi.reporting import print_configure_report, print_inventory, print_preflight_report
from dbman_opsi.runner import CommandRunner
from dbman_opsi.sqlcl_mcp import SqlclMcpConfig, build_sqlcl_mcp_config
from dbman_opsi.terraform import run_terraform, write_tfvars
from dbman_opsi.tf_outputs import merge_outputs_into_config, read_terraform_outputs, validate_merged_config
from dbman_opsi.validation import ValidationService
from dbman_opsi.wizard import run_wizard
from dbman_opsi.evidence import evidence_json, evidence_markdown
from dbman_opsi.fleet import CredentialPolicy, DeploymentMode, DiscoveryScope, FleetPlan, RunManifest, TargetPlan, public_plan_summary
from dbman_opsi.fleet_answers import FleetAnswers, fleet_questionnaire, load_answers, require_valid_answers
from dbman_opsi.fleet_dependencies import target_plans_from_discovery
from dbman_opsi.fleet_discovery import DiscoveryScopeError, FleetDiscovery
from dbman_opsi.fleet_executor import FleetOnboardingExecutor, PhaseOutcome
from dbman_opsi.fleet_offboarding import CleanupExecutor, CleanupHandoffEvidenceImporter, CleanupHandoffPacketWriter, CleanupPlanner, OciCleanupOperations, public_cleanup_summary
from dbman_opsi.fleet_state import DEFAULT_FLEET_STATE_PATH, FleetStateStore, LeaseHeartbeat, RunLeaseError
from dbman_opsi.fleet_status import fleet_status, refresh_collection_readiness
from dbman_opsi.fleet_auth import AuthMode, OciAuth
from dbman_opsi.fleet_operations import LifecycleOperations
from dbman_opsi.fleet_portable_state import ObjectStorageStateBackend, RemoteLeaseHeartbeat
from dbman_opsi.fleet_handoff import CollectionEvidenceImporter, HandoffEvidenceImporter, HandoffPacketWriter

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CliContext:
    run_id: str
    verbose: bool


class _FencedOci:
    """Check the active remote lease immediately around every OCI facade call."""

    def __init__(self, delegate: object, fence: RemoteLeaseHeartbeat | None) -> None:
        self._delegate, self._fence = delegate, fence

    def __getattr__(self, name: str):
        value = getattr(self._delegate, name)
        if not callable(value) or self._fence is None:
            return value

        def fenced(*args, **kwargs):
            self._fence.assert_held()
            try:
                return value(*args, **kwargs)
            finally:
                self._fence.assert_held()

        return fenced


class _FencedCleanupOperations:
    """Fence every cleanup OCI action with the shared local run lease."""

    def __init__(self, delegate: object, fence: LeaseHeartbeat) -> None:
        self._delegate, self._fence = delegate, fence

    def execute_cleanup(self, action) -> None:
        self._fence.assert_held()
        try:
            self._delegate.execute_cleanup(action)
        finally:
            self._fence.assert_held()


class _RegionRoutedLifecycleOperations:
    """Cache one lifecycle facade per discovered target region."""

    def __init__(self, plan: FleetPlan, oci_for_region, *, collection_proofs=None) -> None:
        self._plan = plan
        self._oci_for_region = oci_for_region
        self._collection_proofs = collection_proofs
        self._operations: dict[str, LifecycleOperations] = {}

    def handlers(self) -> dict[str, object]:
        return {
            phase: self._handler(phase)
            for phase in FleetOnboardingExecutor.PHASES
        }

    def _handler(self, phase: str):
        def handler(target: TargetPlan):
            return self._for_region(target.region).handlers()[phase](target)

        return handler

    def _for_region(self, region: str) -> LifecycleOperations:
        operations = self._operations.get(region)
        if operations is None:
            operations = LifecycleOperations(
                self._plan,
                self._oci_for_region(region),
                collection_proofs=self._collection_proofs,
            )
            self._operations[region] = operations
        return operations


class _RegionRoutedCleanupOperations:
    """Route each cleanup action through its explicitly planned target region."""

    def __init__(self, oci_for_region) -> None:
        self._oci_for_region = oci_for_region
        self._operations: dict[str, OciCleanupOperations] = {}

    def execute_cleanup(self, action) -> None:
        region = action.arguments.get("region")
        if not isinstance(region, str) or not region:
            raise ValueError("cleanup action is missing its planned region")
        operations = self._operations.get(region)
        if operations is None:
            operations = OciCleanupOperations(self._oci_for_region(region))
            self._operations[region] = operations
        operations.execute_cleanup(action)


def _add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="dbman-opsi.yaml", help="Path to YAML/JSON config")
    parser.add_argument("--dry-run", action="store_true", help="Print commands instead of executing")
    parser.add_argument("--apply", action="store_true", help="Execute changes even when config dry_run is true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dbman-opsi")
    parser.add_argument("--verbose", action="store_true", help="Show per-command timing")
    verbose_parent = argparse.ArgumentParser(add_help=False)
    verbose_parent.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)
    subcommands = parser.add_subparsers(dest="command", required=True)

    def add_parser(name: str, **kwargs) -> argparse.ArgumentParser:
        return subcommands.add_parser(name, parents=[verbose_parent], **kwargs)

    plan = add_parser("plan", help="Run interactive discovery/planning wizard")
    plan.add_argument("--profile", required=True)
    plan.add_argument("--region", required=True)
    plan.add_argument("--output", default="dbman-opsi.yaml")

    discover = add_parser(
        "discover",
        help="Read-only inventory of reusable resources (subnets, vaults, databases, endpoints, agents, bastions)",
    )
    discover.add_argument("--profile", required=True)
    discover.add_argument("--region", required=True)
    discover.add_argument("--compartment", help="Root compartment OCID (defaults to tenancy)")
    discover.add_argument("--tenancy", help="Tenancy OCID (defaults to compartment)")
    discover.add_argument("--subtree", action="store_true", help="Scan the compartment subtree")
    discover.add_argument("--json", action="store_true", help="Emit the inventory as JSON")

    provision = add_parser("provision", help="Render tfvars and run Terraform")
    _add_config_args(provision)
    provision.add_argument("--render-only", action="store_true", help="Only write terraform.tfvars.json")

    init_region = add_parser(
        "init-region",
        help="Create a region-specific provisioning config for a second-region PoC (defaults to Chicago)",
    )
    init_region.add_argument("--config", default="dbman-opsi.yaml")
    init_region.add_argument("--region", default=CHICAGO_REGION)
    init_region.add_argument("--output", help="Output config path (default: dbman-opsi.<region>.local.yaml)")
    init_region.add_argument("--terraform-dir", help="Terraform work directory for this region")
    init_region.add_argument("--target-name", help="Provisioned database target name")
    init_region.add_argument("--target-kind", choices=("dbcs", "autonomous"), default="dbcs")
    init_region.add_argument("--vcn-id", help="Existing VCN OCID in the selected region")
    init_region.add_argument("--subnet-id", help="Existing private subnet OCID in the selected region")
    init_region.add_argument(
        "--refresh-template",
        action="store_true",
        help="Refresh only Terraform template files in the regional workdir; never copies Terraform state or tfvars",
    )

    import_outputs = add_parser(
        "import-tf-outputs",
        help="Read terraform outputs and merge created OCIDs (subnet, PE, provisioned DBs) back into the config",
    )
    import_outputs.add_argument("--config", default="dbman-opsi.yaml")
    import_outputs.add_argument("--terraform-dir", help="Override config.terraform_dir")
    import_outputs.add_argument("--dry-run", action="store_true", help="Print changes without writing the config")

    enable = add_parser("enable", help="Enable Database Management and Ops Insights")
    _add_config_args(enable)
    enable.add_argument(
        "--skip-credentials",
        action="store_true",
        help="Do not set DBM advanced-diagnostics preferred credentials after enabling",
    )
    enable.add_argument(
        "--force-reconcile",
        action="store_true",
        help="Always reconcile the DBM connection, even when monitoring is already healthy",
    )

    prereqs = add_parser("prepare-prereqs", help="Create OCI-side prerequisites such as private endpoints and optional Vault secrets")
    _add_config_args(prereqs)
    prereqs.add_argument("--password-env", help="Environment variable containing the monitoring password for Vault secret creation")

    validate = add_parser("validate", help="Validate registrations and collection readiness")
    _add_config_args(validate)

    process_insights = add_parser(
        "process-insights",
        help="Diagnose Ops Insights Process Insights host/process telemetry",
    )
    process_insights.add_argument("--config", default="dbman-opsi.yaml")
    process_insights.add_argument(
        "--interval",
        default="P7D",
        help="ISO 8601 analysis interval for host/process summaries (default: P7D)",
    )
    process_insights.add_argument("--json", action="store_true", help="Emit the report as JSON")

    cross_region = add_parser(
        "cross-region",
        help="Configure and summarize the OPSI multi-region Explorer/dashboard POC selection",
    )
    cross_region.add_argument("--config", default="dbman-opsi.yaml")
    cross_region.add_argument(
        "--regions",
        help="Comma-separated OCI regions to select in Ops Insights Explorer and supported dashboards",
    )

    preflight = add_parser("preflight", help="Read-only check of all enablement prerequisites")
    preflight.add_argument("--config", default="dbman-opsi.yaml")
    preflight.add_argument("--json", action="store_true", help="Emit the report as JSON")
    preflight.add_argument(
        "--db-check-file",
        help="Spooled output of 04-validate-monitoring-user.sql to verify the DB monitoring user",
    )

    configure = add_parser(
        "configure",
        help="Orchestrated flow: detect, branch by location, gate on prerequisites, then enable or hand off",
    )
    configure.add_argument("--config", default="dbman-opsi.yaml")
    configure.add_argument("--apply", action="store_true", help="Enable services when all prerequisites pass")
    configure.add_argument("--db-side-only", action="store_true", help="Generate DB-side handoff packets and stop")
    configure.add_argument("--force", action="store_true", help="Ignore blocking prerequisite failures")
    configure.add_argument(
        "--skip-credentials",
        action="store_true",
        help="Do not set DBM advanced-diagnostics preferred credentials after configuring",
    )
    configure.add_argument("--output", default="generated/handoff", help="Handoff packet output directory")
    configure.add_argument("--json", action="store_true", help="Emit the report as JSON")
    configure.add_argument(
        "--with-data-safe",
        action="store_true",
        help="Also register Data Safe targets (datasafe pillar) during --apply",
    )
    configure.add_argument(
        "--with-log-analytics",
        action="store_true",
        help="Also configure Log Analytics source/entity associations for targets with the logan pillar",
    )
    configure.add_argument("--data-safe-user", help="Data Safe service account (default: target monitoring_user or DBSNMP)")
    configure.add_argument("--data-safe-password-env", help="Env var holding the Data Safe account password (non-interactive)")

    agent = add_parser("generate-agent-scripts", help="Generate Management Agent install scripts")
    agent.add_argument("--config", default="dbman-opsi.yaml")
    agent.add_argument("--output", default="generated/agents")

    db_scripts = add_parser("generate-db-scripts", help="Generate database-side SQL scripts")
    db_scripts.add_argument("--config", default="dbman-opsi.yaml")
    db_scripts.add_argument("--output", default="generated/db-scripts")

    opsi_payloads = add_parser("generate-opsi-payloads", help="Generate Operations Insights JSON payload files")
    opsi_payloads.add_argument("--config", default="dbman-opsi.yaml")
    opsi_payloads.add_argument("--output", default="generated/opsi-payloads")

    logan_payloads = add_parser("generate-logan-payloads", help="Generate Log Analytics payloads, host scripts, SQL, and credential templates")
    logan_payloads.add_argument("--config", default="dbman-opsi.yaml")
    logan_payloads.add_argument("--output", default="generated/logan")

    opsi_diagnostics = add_parser(
        "generate-opsi-diagnostics",
        help="Generate read-only OCI/SQL scripts for failed DBCS/Exadata Ops Insights enablement",
    )
    opsi_diagnostics.add_argument("--config", default="dbman-opsi.yaml")
    opsi_diagnostics.add_argument("--output", default="generated/opsi-diagnostics")

    set_creds = add_parser(
        "set-credentials",
        help="Set DBM advanced-diagnostics preferred credentials via a Vault named credential",
    )
    set_creds.add_argument("--config", default="dbman-opsi.yaml")

    sqlcl_mcp = add_parser(
        "generate-sqlcl-mcp",
        help="Write a credential-free SQLcl MCP template for the MCP_READONLY database identity",
    )
    sqlcl_mcp.add_argument("--output", default="generated/sqlcl-mcp.json")
    sqlcl_mcp.add_argument("--name", default="sqlcl-readonly")
    sqlcl_mcp.add_argument("--connect-descriptor", required=True, help="Approved database connect descriptor or placeholder")
    sqlcl_mcp.add_argument("--secret-id", required=True, help="OCI Vault secret OCID for MCP_READONLY")

    disposable_assets = add_parser(
        "generate-disposable-assets",
        help="Generate dedicated-user bootstrap SQL, dashboard definitions, and a redacted release-evidence skeleton",
    )
    disposable_assets.add_argument("--output", default="generated/disposable-release")
    disposable_assets.add_argument("--lifecycle-id", required=True)
    disposable_assets.add_argument("--target-name", default="disposable-demo")

    reveal_secret = add_parser("reveal-vault-secret", help="Explicitly print one authorized Vault secret value to stdout")
    reveal_secret.add_argument("--profile", required=True)
    reveal_secret.add_argument("--region", required=True)
    reveal_secret.add_argument("--secret-id", required=True)
    reveal_secret.add_argument("--confirm-reveal", action="store_true", help="Required acknowledgement that stdout may expose a secret")

    reset_plan = add_parser("credential-reset-plan", help="Print the safe, single-role credential reset contract")
    reset_plan.add_argument("--role", required=True, choices=("DBM_MON", "DATASAFE_AUDIT", "MCP_READONLY", "DBINC_LAB"))
    reset_plan.add_argument("--json", action="store_true")

    bind_secret = add_parser("bind-vault-secret", help="Bind a Vault secret reference to one configured target without revealing its value")
    bind_secret.add_argument("--config", default="dbman-opsi.yaml")
    bind_secret.add_argument("--target", required=True)
    bind_secret.add_argument("--secret-id", required=True)

    doctor = add_parser("doctor", help="Check local or Cloud Shell prerequisites")
    doctor.add_argument("--profile", help="Also verify the OCI session is authenticated for this profile")
    doctor.add_argument("--region", help="Region to use for the session check")

    journal = add_parser("journal", help="Inspect a run journal")
    journal.add_argument("run_id", nargs="?", help="Run ID from runs/<RUN_ID>.jsonl")
    journal.add_argument("--last", action="store_true", help="Inspect the newest runs/*.jsonl file")
    journal.add_argument("--json", action="store_true", help="Emit the journal summary as JSON")

    db_exec = add_parser(
        "db-exec",
        help="Generate DB-side scripts and show the hybrid run plan (auto-run in non-prod, handoff in prod)",
    )
    db_exec.add_argument("--config", default="dbman-opsi.yaml")
    db_exec.add_argument("--scripts-dir", default="generated/db-scripts")
    db_exec.add_argument("--force", action="store_true", help="Treat as non-production (auto-exec even for prod)")
    db_exec.add_argument("--apply", action="store_true", help="Auto-run DB-side scripts via Bastion (non-prod). Requires --bastion-id/--target-ip/--ssh-key")
    db_exec.add_argument("--bastion-id", help="Bastion OCID for --apply auto-exec")
    db_exec.add_argument("--target-ip", help="DB node private IP for --apply auto-exec")
    db_exec.add_argument("--ssh-key", help="SSH private key path (with matching .pub) for the Bastion session + DB node")
    db_exec.add_argument("--answers-file", help="File whose contents are piped to each script's SQL*Plus accept prompts")

    data_safe = add_parser(
        "data-safe",
        help="Register databases as Data Safe targets (security pillar) for targets that opt into 'datasafe'",
    )
    data_safe.add_argument("--config", default="dbman-opsi.yaml")
    data_safe.add_argument("--apply", action="store_true", help="Perform live registration (otherwise dry-run)")
    data_safe.add_argument("--user", help="Data Safe service account (default: target monitoring_user or DBSNMP)")
    data_safe.add_argument("--password-env", help="Env var holding the Data Safe account password (non-interactive)")

    logan = add_parser(
        "log-analytics",
        help="Configure Log Analytics namespace/log group and source associations for targets that opt into 'logan'",
    )
    logan.add_argument("--config", default="dbman-opsi.yaml")
    logan.add_argument("--apply", action="store_true", help="Apply OCI changes (otherwise dry-run)")
    logan.add_argument("--dry-run", action="store_true", help="Print OCI commands without applying")
    logan.add_argument("--wait-minutes", type=int, default=10, help="Reserved wait budget for agent collection readiness")
    logan.add_argument("--db-password-env", help="Env var used by generated DB credential templates")
    logan.add_argument("--adb-wallet-dir", help="Local ADB wallet directory for validation/generation checks")
    logan.add_argument("--install-key-env", help="Env var holding the Management Agent install key")
    logan.add_argument("--evidence-dir", default="generated/logan-e2e-evidence")
    logan.add_argument("--payload-dir", default="generated/logan")

    db_incident = add_parser(
        "db-incident",
        help="Build a bounded DB incident evidence bundle from Log Analytics, DBM, OPSI, and Data Safe",
    )
    db_incident.add_argument("--profile", required=True)
    db_incident.add_argument("--region", required=True)
    db_incident.add_argument("--compartment-id")
    db_incident.add_argument("--ora-code", required=True)
    db_incident.add_argument("--database-name")
    db_incident.add_argument("--entity-name")
    db_incident.add_argument("--incident-time")
    db_incident.add_argument("--hours-back", type=int, default=2)
    db_incident.add_argument("--window-minutes", type=int, default=30)
    db_incident.add_argument("--include-sources", default="logan,dbm,opsi,audit,datasafe")
    db_incident.add_argument("--limit", type=int, default=100)
    db_incident.add_argument("--json", action="store_true", help="Emit the bundle as JSON")

    demo_incident = add_parser(
        "generate-db-incident-demo",
        help="Generate dry-run DB incident lab SQL and synthetic Log Analytics JSONL records",
    )
    demo_incident.add_argument("--output", default="generated/db-incident-demo")
    demo_incident.add_argument("--apply", action="store_true", help="Render executable lab SQL instead of dry-run comments")
    demo_incident.add_argument("--scenario-id", default="ora00600-demo")

    # The lifecycle interface is intentionally separate from the established
    # expert commands above.  All mutating lifecycle paths demand an exact ID.
    def lifecycle(name: str, help: str) -> argparse.ArgumentParser:
        command = add_parser(name, help=help)
        command.add_argument("--profile", default="DEFAULT")
        command.add_argument("--region", required=True)
        command.add_argument("--state", default=str(DEFAULT_FLEET_STATE_PATH))
        command.add_argument("--state-backend", choices=("local", "object"), default="local")
        command.add_argument("--state-namespace")
        command.add_argument("--state-bucket")
        command.add_argument("--state-object")
        auth = command.add_mutually_exclusive_group()
        auth.add_argument("--security-token", action="store_true")
        auth.add_argument("--instance-principal", action="store_true")
        auth.add_argument("--resource-principal", action="store_true")
        return command

    onboard = lifecycle("onboard", "Discover, review, and plan-gate fleet onboarding")
    onboard.add_argument("--answers", help="Validated YAML questionnaire answers")
    onboard.add_argument("--selection-file", help="CSV/YAML target-id selection file")
    onboard.add_argument("--non-interactive", action="store_true")
    onboard.add_argument("--plan-only", action="store_true")
    onboard.add_argument("--approval", help="Exact reviewed fleet plan ID")
    onboard.add_argument("--bindings", help="Private 0600 YAML/JSON target bindings; references only")
    onboard.add_argument("--handoff-key", help="Private 0600 HMAC signing-key file")
    onboard.add_argument("--handoff-dir", default="generated/fleet-handoffs", help="Private packet directory")
    resume = lifecycle("resume", "Resume a checkpointed fleet onboarding run")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--approval", required=True)
    resume.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry failed phases and dependency-blocked children for this exact approved plan",
    )
    resume.add_argument("--handoff-key", help="Private 0600 HMAC signing-key file")
    resume.add_argument("--handoff-dir", default="generated/fleet-handoffs", help="Private packet directory")
    import_handoff = lifecycle("import-handoff", "Import signed onboarding handoff completion evidence")
    import_handoff.add_argument("--run-id", required=True)
    import_handoff.add_argument("--approval", required=True)
    import_handoff.add_argument("--evidence", required=True)
    import_handoff.add_argument("--handoff-key", required=True)
    import_cleanup = lifecycle("import-cleanup-handoff", "Import signed cleanup completion evidence")
    import_cleanup.add_argument("--run-id", required=True)
    import_cleanup.add_argument("--approval", required=True)
    import_cleanup.add_argument("--evidence", required=True)
    import_cleanup.add_argument("--handoff-key", required=True)
    import_cleanup.add_argument("--delete-test-databases", action="store_true")
    import_collection = lifecycle("import-collection-evidence", "Import signed per-service collection evidence")
    import_collection.add_argument("--run-id", required=True)
    import_collection.add_argument("--approval", required=True)
    import_collection.add_argument("--evidence", required=True)
    import_collection.add_argument("--handoff-key", required=True)
    status = lifecycle("fleet-status", "Show sanitized fleet lifecycle status")
    status.add_argument("--run-id", required=True)
    status.add_argument("--json", action="store_true")
    offboard = lifecycle("offboard", "Plan-gated ownership-safe fleet cleanup")
    offboard.add_argument("--run-id", required=True)
    offboard.add_argument("--approval", help="Exact reviewed cleanup plan ID")
    offboard.add_argument("--plan-only", action="store_true")
    offboard.add_argument("--delete-test-databases", action="store_true")
    offboard.add_argument("--confirm-test-db-deletion", help="Typed deletion confirmation displayed by the cleanup plan")
    offboard.add_argument("--handoff-key", help="Private 0600 cleanup HMAC signing-key file")
    offboard.add_argument("--handoff-dir", default="generated/fleet-cleanup-handoffs", help="Private cleanup packet directory")

    return parser


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout, force=True)


def _make_journal(run_id: str, profile: str, region: str) -> RunJournal:
    return RunJournal(run_id=run_id, profile=profile, region=region)


def _make_runner(
    *,
    dry_run: bool,
    run_id: str,
    profile: str,
    region: str,
    verbose: bool,
) -> CommandRunner:
    return CommandRunner(
        dry_run=dry_run,
        journal=_make_journal(run_id, profile, region),
        run_id=run_id,
        verbose=verbose,
    )


def _make_data_safe_provider(apply: bool, user_override: str | None, password_env: str | None):
    """Build a (user, password) provider for Data Safe registration.

    Only prompts when applying live; in dry-run the password is unused. A
    password env var supports non-interactive runs (CI/Cloud Shell).
    """

    def provider(target) -> tuple[str, str]:
        user = user_override or target.monitoring_user or "DBSNMP"
        if not apply:
            return (user, "")
        if password_env:
            return (user, os.environ.get(password_env, ""))
        return (user, getpass.getpass(f"Data Safe password for {user}@{target.name}: "))

    return provider


def _persist_data_safe_targets(
    config: EnablementConfig, decisions: list[DataSafeDecision]
) -> EnablementConfig:
    """Write any newly-registered Data Safe target OCIDs back into the config."""

    ids = {d.target: d.target_id for d in decisions if d.target_id}
    if not ids:
        return config
    new_targets = tuple(
        replace(t, data_safe_target_id=ids[t.name]) if t.name in ids and not t.data_safe_target_id else t
        for t in config.targets
    )
    return replace(config, targets=new_targets)


def _persist_log_analytics_entities(
    config: EnablementConfig,
    decisions,
) -> EnablementConfig:
    target_updates = {decision.target: decision for decision in decisions if decision.target != "tenancy"}
    if not target_updates and not any(getattr(decision, "log_group_id", None) for decision in decisions):
        return config
    new_targets = tuple(
        replace(
            target,
            logan_database_entity_id=(
                target_updates[target.name].logan_database_entity_id or target.logan_database_entity_id
            )
            if target.name in target_updates
            else target.logan_database_entity_id,
            logan_host_entity_id=(
                target_updates[target.name].logan_host_entity_id or target.logan_host_entity_id
            )
            if target.name in target_updates
            else target.logan_host_entity_id,
            logan_listener_entity_id=(
                target_updates[target.name].logan_listener_entity_id or target.logan_listener_entity_id
            )
            if target.name in target_updates
            else target.logan_listener_entity_id,
        )
        for target in config.targets
    )
    log_group_id = config.log_analytics.log_group_id
    for decision in decisions:
        if decision.target == "tenancy" and decision.log_group_id:
            log_group_id = decision.log_group_id
            break
    new_log_analytics = config.log_analytics
    if log_group_id != config.log_analytics.log_group_id:
        new_log_analytics = replace(config.log_analytics, log_group_id=log_group_id)
    return replace(config, targets=new_targets, log_analytics=new_log_analytics)


def _config_runner(config: EnablementConfig, ctx: _CliContext, dry_run: bool) -> CommandRunner:
    return _make_runner(
        dry_run=dry_run,
        run_id=ctx.run_id,
        profile=config.profile,
        region=config.region,
        verbose=ctx.verbose,
    )


def _args_runner(args: argparse.Namespace, ctx: _CliContext, dry_run: bool) -> CommandRunner:
    return _make_runner(
        dry_run=dry_run,
        run_id=ctx.run_id,
        profile=args.profile,
        region=args.region,
        verbose=ctx.verbose,
    )


def _config_oci(config: EnablementConfig, ctx: _CliContext, dry_run: bool) -> OciCli:
    return OciCli(config.profile, config.region, _config_runner(config, ctx, dry_run))


def _lifecycle_auth(args: argparse.Namespace) -> OciAuth:
    mode = (AuthMode.SECURITY_TOKEN if args.security_token else AuthMode.INSTANCE_PRINCIPAL if args.instance_principal else AuthMode.RESOURCE_PRINCIPAL if args.resource_principal else AuthMode.API_KEY)
    return OciAuth(mode=mode, profile=args.profile)


def _lifecycle_oci(
    args: argparse.Namespace,
    ctx: _CliContext,
    *,
    dry_run: bool,
    region: str | None = None,
) -> OciCli:
    selected_region = region or args.region
    runner = _make_runner(dry_run=dry_run, run_id=ctx.run_id, profile=args.profile, region=selected_region, verbose=ctx.verbose)
    return OciCli(args.profile, selected_region, runner, auth=_lifecycle_auth(args))


def _portable_backend(args: argparse.Namespace, ctx: _CliContext) -> ObjectStorageStateBackend | None:
    if args.state_backend == "local":
        return None
    if not all((args.state_namespace, args.state_bucket, args.state_object)):
        raise ValueError("object state backend requires --state-namespace, --state-bucket, and --state-object")
    return ObjectStorageStateBackend(_lifecycle_oci(args, ctx, dry_run=False), namespace=args.state_namespace, bucket=args.state_bucket, name=args.state_object, cache_path=args.state)


def _portable_push(args: argparse.Namespace, ctx: _CliContext, manifest: RunManifest) -> None:
    backend = _portable_backend(args, ctx)
    if backend is not None:
        binding = backend.upload(run_id=manifest.run_id, plan_id=manifest.plan_id, expected_version=getattr(ctx, "portable_etag", None))
        object.__setattr__(ctx, "portable_etag", binding.version)


def _assert_remote_fence(fence: RemoteLeaseHeartbeat | None) -> None:
    if fence is not None:
        fence.assert_held()


@contextmanager
def _local_write_lease(store: FleetStateStore, *, run_id: str, plan_id: str, owner: str):
    """Lease non-executor lifecycle mutations before any state or OCI action."""
    if not store.acquire_lease(run_id=run_id, plan_id=plan_id, owner=owner):
        raise RunLeaseError("fleet run is already leased by another actor")
    heartbeat = LeaseHeartbeat(store, run_id=run_id, owner=owner)
    heartbeat.start()
    try:
        heartbeat.assert_held()
        yield heartbeat
        heartbeat.assert_held()
    finally:
        heartbeat.stop()
        store.release_lease(run_id=run_id, owner=owner)


def _portable_pull(args: argparse.Namespace, ctx: _CliContext, run_id: str) -> None:
    backend = _portable_backend(args, ctx)
    if backend is None:
        return
    # The run/plan binding is checked again after loading the local cache.  The
    # remote metadata requires a plan ID, so reject unknown bindings fail-closed.
    body, version, metadata = backend.client.get_object_state(backend.namespace, backend.bucket, backend.name)
    plan_id = metadata.get("plan-id")
    if not plan_id:
        raise ValueError("portable state has no plan binding")
    # Reuse checked downloader with the supplied metadata-compatible binding.
    binding = backend.download(run_id=run_id, plan_id=plan_id)
    object.__setattr__(ctx, "portable_etag", binding.version)


@contextmanager
def _portable_write_lease(args: argparse.Namespace, ctx: _CliContext, *, run_id: str, plan_id: str):
    backend = _portable_backend(args, ctx)
    if backend is None:
        yield None
        return
    lease = backend.acquire_lease(run_id=run_id, plan_id=plan_id, owner=ctx.run_id)
    heartbeat = RemoteLeaseHeartbeat(backend, lease)
    heartbeat.start()
    try:
        yield heartbeat
    finally:
        heartbeat.close()


def _interactive_fleet_answers() -> FleetAnswers:
    """Collect explicit answers without ever prompting for a credential."""
    values: dict[str, object] = {}
    for question in fleet_questionnaire():
        response = input(f"{question.prompt} [{question.default}]: ").strip()
        if response:
            if question.key == "services":
                values[question.key] = response.split(",")
            elif question.key == "discovery_filters":
                import yaml
                parsed = yaml.safe_load(response)
                if not isinstance(parsed, Mapping):
                    raise ValueError("discovery_filters must be a JSON/YAML mapping")
                values[question.key] = dict(parsed)
            else:
                values[question.key] = response
    from dbman_opsi.fleet_answers import answers_from_dict
    return answers_from_dict(values)


def _lifecycle_plan(args: argparse.Namespace, ctx: _CliContext) -> tuple[FleetPlan, FleetAnswers]:
    if args.answers:
        answers = load_answers(args.answers)
    elif args.non_interactive:
        raise ValueError("--non-interactive requires --answers")
    else:
        answers = _interactive_fleet_answers()
    answers = require_valid_answers(answers)
    discovery = FleetDiscovery(_lifecycle_oci(args, ctx, dry_run=False)).discover_result().require_complete()
    selection = answers.discovery_filters.with_file(args.selection_file) if args.selection_file else answers.discovery_filters
    from dbman_opsi.fleet_selection import select_targets
    targets = target_plans_from_discovery(select_targets(discovery.targets, selection), credential_policy=answers.credential_policy, services=answers.services)
    bindings = _load_lifecycle_bindings(getattr(args, "bindings", None))
    targets = tuple(_materialize_monitoring_target(_bind_target(target, bindings), answers) for target in targets)
    _validate_credential_bindings(targets, answers)
    targets = tuple(_apply_answer_controls(target, answers) for target in targets)
    prerequisite_actions = {"VAULT_ENDPOINTS" if "dbm" in target.services else "" for target in targets}
    if "logan" in answers.services and any(target.settings.get("logan_onboard_namespace") for target in targets):
        prerequisite_actions.add("LOG_ANALYTICS_NAMESPACE")
    prerequisite_actions.discard("")
    risk_codes = {"RISK_OWNER_APPROVAL"}
    if "LOG_ANALYTICS_NAMESPACE" in prerequisite_actions:
        risk_codes.add("RISK_LOGAN_NAMESPACE_ONBOARDING")
    return FleetPlan(profile=args.profile, region=args.region, targets=targets, deployment_mode=answers.deployment_mode, credential_policy=answers.credential_policy, discovery_scope=DiscoveryScope(subscribed_regions=discovery.regions, accessible_compartments=tuple(compartment_id for compartment_id, _name in discovery.compartments), include_regions=selection.regions, exclude_regions=(), include_compartments=selection.compartments, exclude_compartments=()), prerequisite_actions=tuple(sorted(prerequisite_actions)), risk_codes=tuple(sorted(risk_codes)), estimated_resource_counts={service: sum(service in target.services for target in targets) for service in answers.services}, settings={
        "services": answers.services, "log_preset": answers.log_preset.value, "retention_days": answers.retention_days,
        "authority_mode": answers.authority_mode.value, "max_concurrency": answers.max_concurrency,
        "provision_test_dbcs": answers.provision_test_dbcs, "provision_test_autonomous": answers.provision_test_autonomous,
        "common_user": answers.common_user, "pdb_unique_passwords": answers.pdb_unique_passwords,
        "monitoring_username": answers.monitoring_username,
        "service_concurrency": {service: answers.max_concurrency for service in answers.services},
        "region_concurrency": {args.region: answers.max_concurrency},
        "bindings_supplied": bool(bindings),
    }), answers


def _apply_answer_controls(target: TargetPlan, answers: FleetAnswers) -> TargetPlan:
    """Materialize reviewed questionnaire choices into each executable target."""
    settings = dict(target.settings)
    should_provision = (
        (target.kind == "dbcs" and answers.provision_test_dbcs)
        or (target.kind == "autonomous" and answers.provision_test_autonomous)
    )
    sources = {
        "none": (),
        "alert-listener-audit": (
            "Oracle Database Alert Logs",
            "Oracle Database Listener Alert Logs",
            "Oracle Database Audit Logs",
        ),
        "extended": (
            "Oracle Database Alert Logs",
            "Oracle Database Alert Logs XML",
            "Oracle Database Listener Alert Logs",
            "Oracle Database Listener Trace Logs",
            "Oracle Database Audit Logs",
            "Oracle Database Audit Logs XML",
        ),
    }[answers.log_preset.value]
    # Explicit target values remain authoritative only for private bindings;
    # questionnaire choices otherwise become immutable reviewed plan intent.
    settings.update({
        "provision": bool(settings.get("provision", False) or should_provision),
        "authority_mode": str(settings.get("authority_mode", answers.authority_mode.value)),
        "account_group": str(settings.get("account_group", "common" if answers.common_user else (f"pdb:{target.target_id}" if answers.pdb_unique_passwords and str(settings.get("database_role", "")).upper() == "PDB" else "database"))),
        "logan_sources": tuple(settings.get("logan_sources", sources)),
        "log_preset": answers.log_preset.value,
    })
    return replace(target, settings=settings)


def _load_lifecycle_bindings(path: str | None) -> dict[str, dict[str, object]]:
    """Load private ref-only bindings; passwords and permissive files fail closed."""
    if not path:
        return {}
    binding_path = Path(path)
    if stat.S_IMODE(binding_path.stat().st_mode) != 0o600:
        raise ValueError("--bindings file must have mode 0600")
    import yaml
    raw = yaml.safe_load(binding_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("--bindings must contain a target mapping")
    entries = raw.get("targets", raw)
    if not isinstance(entries, dict):
        raise ValueError("--bindings targets must be a mapping")
    allowed = frozenset((
        "password_secret_id", "service_name", "private_endpoint_id", "opsi_private_endpoint_id",
        "data_safe_private_endpoint_id", "opsi_database_insight_id", "management_agent_id",
        "logan_management_agent_id", "data_safe_target_id", "authority_mode", "account_group",
        "provisioning_intent", "logan_namespace", "logan_log_group_id", "logan_log_group_name",
    ))
    result: dict[str, dict[str, object]] = {}
    for target_id, values in entries.items():
        if not isinstance(values, dict):
            raise ValueError("each --bindings target must be a mapping")
        for key, value in values.items():
            key = str(key)
            if key not in allowed:
                raise ValueError(f"--bindings field is not allowlisted: {key}")
            forbidden_assignments = ("pass" + "word=", "se" + "cret=")
            if (
                not isinstance(value, str)
                or not value.strip()
                or any(marker in value.lower() for marker in forbidden_assignments)
            ):
                raise ValueError("--bindings accepts non-empty reference strings, never plaintext values")
            if key == "password_secret_id" and not value.startswith(("ocid1.", "vault://", "ref:")):
                raise ValueError("password_secret_id must be a Vault/reference identifier")
        result[str(target_id)] = {str(key): value for key, value in values.items()}
    return result


def _handoff_writer(key_path: str | None, directory: str | None) -> HandoffPacketWriter | None:
    if not key_path:
        return None
    path = Path(key_path)
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("--handoff-key file must have mode 0600")
    key = path.read_bytes()
    if len(key) < 16:
        raise ValueError("--handoff-key must contain at least 16 bytes")
    return HandoffPacketWriter(directory or "generated/fleet-handoffs", signing_key=key)


def _cleanup_handoff_writer(key_path: str | None, directory: str | None) -> CleanupHandoffPacketWriter | None:
    writer = _handoff_writer(key_path, directory)
    return CleanupHandoffPacketWriter(directory or "generated/fleet-cleanup-handoffs", signing_key=writer.signing_key) if writer else None


def _bind_target(target: TargetPlan, bindings: dict[str, dict[str, object]]) -> TargetPlan:
    values = bindings.get(target.target_id)
    if values is None:
        return target
    # These values are immutable plan intent and are merged at the adapter
    # boundary (rather than stashed in an ignored nested object).
    return TargetPlan(target_id=target.target_id, name=target.name, kind=target.kind, region=target.region,
        compartment_id=target.compartment_id, resource_id=target.resource_id, services=target.services,
        dependencies=target.dependencies, credential_policy=target.credential_policy,
        settings={**dict(target.settings), **values})


def _materialize_monitoring_target(target: TargetPlan, answers: FleetAnswers) -> TargetPlan:
    """Bind the selected policy to adapter-visible, non-secret user intent."""
    if answers.credential_policy is CredentialPolicy.DEDICATED_USER_UNIQUE_SECRET:
        suffix = hashlib.sha256(target.target_id.encode("utf-8")).hexdigest()[:12].upper()
        username = (answers.monitoring_username[:17] + "_" + suffix)[:30]
        account_group = "per-pdb" if str(target.settings.get("database_role", "")).upper() == "PDB" else "per-target"
    else:
        username = answers.monitoring_username
        account_group = "shared-common" if answers.common_user else "shared-local"
    settings = {**dict(target.settings), "monitoring_user": username, "account_group": account_group}
    return TargetPlan(target_id=target.target_id, name=target.name, kind=target.kind, region=target.region,
        compartment_id=target.compartment_id, resource_id=target.resource_id, services=target.services,
        dependencies=target.dependencies, credential_policy=target.credential_policy, settings=settings)


def _validate_credential_bindings(targets: tuple[TargetPlan, ...], answers: FleetAnswers) -> None:
    refs = [str(target.settings.get("password_secret_id")) for target in targets if target.settings.get("password_secret_id")]
    if answers.credential_policy is CredentialPolicy.SHARED_USER_SHARED_SECRET:
        if answers.deployment_mode not in (DeploymentMode.POC, DeploymentMode.DEMO):
            raise ValueError("shared-user-shared-secret is permitted only in poc/demo")
        if refs and len(set(refs)) != 1:
            raise ValueError("shared-user-shared-secret requires one identical Vault reference for every bound target")
    if answers.credential_policy is CredentialPolicy.SHARED_USER_UNIQUE_SECRET and len(refs) != len(set(refs)):
        raise ValueError("shared-user-unique-secret requires unique Vault references across independent targets")


def _exit_for_manifest(manifest: RunManifest) -> int:
    status = fleet_status(manifest)
    summary = status["summary"]
    return 3 if summary.get("blocked", 0) else 2 if summary.get("degraded", 0) or summary.get("handed-off", 0) else 0


def _cmd_onboard(args: argparse.Namespace, ctx: _CliContext) -> int:
    plan, answers = _lifecycle_plan(args, ctx)
    print(json.dumps(public_plan_summary(plan), sort_keys=True))
    if args.plan_only:
        return 10
    if args.approval != plan.plan_id:
        print("approval does not match reviewed plan", file=sys.stderr)
        return 4
    if not args.handoff_key:
        raise ValueError("--handoff-key is required before onboarding apply")
    store = FleetStateStore(args.state)
    with _portable_write_lease(args, ctx, run_id=ctx.run_id, plan_id=plan.plan_id) as remote_fence:
        handlers = _RegionRoutedLifecycleOperations(
            plan,
            lambda region: _FencedOci(_lifecycle_oci(args, ctx, dry_run=False, region=region), remote_fence),
        ).handlers()
        manifest = FleetOnboardingExecutor(plan, store, phase_handlers=handlers, concurrency=answers.max_concurrency, handoff_writer=_handoff_writer(args.handoff_key, args.handoff_dir)).execute(approved_plan_id=args.approval, run_id=ctx.run_id)
        _assert_remote_fence(remote_fence)
        _portable_push(args, ctx, manifest)
        _assert_remote_fence(remote_fence)
    print(evidence_markdown(refresh_collection_readiness(manifest)), end="")
    return _exit_for_manifest(manifest)


def _load_run(args: argparse.Namespace) -> tuple[FleetStateStore, RunManifest, FleetPlan]:
    # Object state is fetched before SQLite is opened so a cross-host cache
    # cannot accidentally resume stale local state.
    # ctx is unavailable here; callers pull before this helper.
    store = FleetStateStore(args.state)
    manifest = store.load(args.run_id)
    if manifest is None:
        raise KeyError(f"run not found: {args.run_id}")
    plan = store.load_plan(args.run_id)
    if plan is None or plan.plan_id != manifest.plan_id:
        raise KeyError(f"run plan not found: {args.run_id}")
    return store, manifest, plan


def _cmd_resume(args: argparse.Namespace, ctx: _CliContext) -> int:
    _portable_pull(args, ctx, args.run_id)
    store, manifest, plan = _load_run(args)
    if args.approval != manifest.plan_id:
        print("approval does not match reviewed plan", file=sys.stderr)
        return 4
    if not args.handoff_key:
        raise ValueError("--handoff-key is required before resume apply")
    # The canonical plan serialization is intentionally not persisted outside
    # its reviewed hash, so resume reuses only checkpoint-safe phases here.
    imported_proofs = {target.target_id: tuple(resource.attributes for resource in target.resources if resource.resource_type == "collection-proof") for target in manifest.targets}
    with _portable_write_lease(args, ctx, run_id=args.run_id, plan_id=plan.plan_id) as remote_fence:
        handlers = _RegionRoutedLifecycleOperations(
            plan,
            lambda region: _FencedOci(_lifecycle_oci(args, ctx, dry_run=False, region=region), remote_fence),
            collection_proofs=imported_proofs,
        ).handlers()
        resumed = FleetOnboardingExecutor(plan, store, phase_handlers=handlers, handoff_writer=_handoff_writer(args.handoff_key, args.handoff_dir)).execute(
            approved_plan_id=args.approval,
            run_id=args.run_id,
            retry_failed=args.retry_failed,
        )
        _assert_remote_fence(remote_fence)
        _portable_push(args, ctx, resumed)
        _assert_remote_fence(remote_fence)
    print(json.dumps(fleet_status(resumed), sort_keys=True))
    return _exit_for_manifest(resumed)


def _cmd_fleet_status(args: argparse.Namespace, ctx: _CliContext) -> int:
    _portable_pull(args, ctx, args.run_id)
    _store, manifest, _plan = _load_run(args)
    status = fleet_status(manifest)
    if args.json:
        print(json.dumps(status, sort_keys=True))
    else:
        print(evidence_markdown(refresh_collection_readiness(manifest)), end="")
    return _exit_for_manifest(manifest)


def _cmd_import_handoff(args: argparse.Namespace, ctx: _CliContext) -> int:
    _portable_pull(args, ctx, args.run_id)
    store, _manifest, plan = _load_run(args)
    writer = _handoff_writer(args.handoff_key, None)
    assert writer is not None
    with _local_write_lease(store, run_id=args.run_id, plan_id=plan.plan_id, owner=ctx.run_id) as local_fence:
        with _portable_write_lease(args, ctx, run_id=args.run_id, plan_id=plan.plan_id) as remote_fence:
            local_fence.assert_held()
            _assert_remote_fence(remote_fence)
            updated = HandoffEvidenceImporter(store, plan, signing_key=writer.signing_key).import_packet(args.evidence, approved_plan_id=args.approval)
            local_fence.assert_held()
            _assert_remote_fence(remote_fence)
            _portable_push(args, ctx, updated)
            local_fence.assert_held()
            _assert_remote_fence(remote_fence)
    print(json.dumps(fleet_status(updated), sort_keys=True))
    return _exit_for_manifest(updated)


def _cmd_import_cleanup_handoff(args: argparse.Namespace, ctx: _CliContext) -> int:
    _portable_pull(args, ctx, args.run_id)
    store, manifest, plan = _load_run(args)
    cleanup = CleanupPlanner(plan, manifest, delete_test_databases=args.delete_test_databases).build()
    writer = _handoff_writer(args.handoff_key, None)
    assert writer is not None
    with _local_write_lease(store, run_id=args.run_id, plan_id=plan.plan_id, owner=ctx.run_id) as local_fence:
        with _portable_write_lease(args, ctx, run_id=args.run_id, plan_id=plan.plan_id) as remote_fence:
            local_fence.assert_held()
            _assert_remote_fence(remote_fence)
            result = CleanupHandoffEvidenceImporter(store, cleanup, signing_key=writer.signing_key).import_packet(args.evidence, approved_plan_id=args.approval)
            local_fence.assert_held()
            _assert_remote_fence(remote_fence)
            _portable_push(args, ctx, manifest)
            local_fence.assert_held()
            _assert_remote_fence(remote_fence)
    print(json.dumps({"cleanup_plan_id": result.cleanup_plan_id, "action_states": result.action_states}, sort_keys=True))
    return 2 if result.partial else 0


def _cmd_import_collection_evidence(args: argparse.Namespace, ctx: _CliContext) -> int:
    _portable_pull(args, ctx, args.run_id)
    store, _manifest, plan = _load_run(args)
    writer = _handoff_writer(args.handoff_key, None)
    assert writer is not None
    with _local_write_lease(store, run_id=args.run_id, plan_id=plan.plan_id, owner=ctx.run_id) as local_fence:
        with _portable_write_lease(args, ctx, run_id=args.run_id, plan_id=plan.plan_id) as remote_fence:
            local_fence.assert_held()
            _assert_remote_fence(remote_fence)
            updated = CollectionEvidenceImporter(store, plan, signing_key=writer.signing_key).import_packet(args.evidence, approved_plan_id=args.approval)
            local_fence.assert_held()
            _assert_remote_fence(remote_fence)
            _portable_push(args, ctx, updated)
            local_fence.assert_held()
            _assert_remote_fence(remote_fence)
    print(json.dumps(fleet_status(updated), sort_keys=True))
    return _exit_for_manifest(updated)


def _cmd_offboard(args: argparse.Namespace, ctx: _CliContext) -> int:
    _portable_pull(args, ctx, args.run_id)
    store, manifest, plan = _load_run(args)
    # Run manifests are plan-bound; cleanup is built from that exact run only.
    cleanup = CleanupPlanner(plan, manifest, delete_test_databases=args.delete_test_databases).build()
    print(json.dumps(public_cleanup_summary(cleanup), sort_keys=True))
    if args.plan_only:
        return 10
    if args.approval != cleanup.plan_id:
        print("approval does not match reviewed cleanup plan", file=sys.stderr)
        return 4
    if not args.handoff_key:
        raise ValueError("--handoff-key is required before offboard apply")
    with _local_write_lease(store, run_id=args.run_id, plan_id=plan.plan_id, owner=ctx.run_id) as local_fence:
        with _portable_write_lease(args, ctx, run_id=args.run_id, plan_id=plan.plan_id) as remote_fence:
            operations = _RegionRoutedCleanupOperations(
                lambda region: _FencedOci(_lifecycle_oci(args, ctx, dry_run=False, region=region), remote_fence)
            )
            execution = CleanupExecutor(cleanup, store, _FencedCleanupOperations(operations, local_fence), handoff_writer=_cleanup_handoff_writer(args.handoff_key, args.handoff_dir)).execute(approved_plan_id=args.approval, database_confirmation=args.confirm_test_db_deletion)
            local_fence.assert_held()
            _assert_remote_fence(remote_fence)
            _portable_push(args, ctx, manifest)
            local_fence.assert_held()
            _assert_remote_fence(remote_fence)
    return 2 if execution.partial else 0


def _cmd_plan(args: argparse.Namespace, ctx: _CliContext) -> int:
    discovery = OciCli(args.profile, args.region, _args_runner(args, ctx, dry_run=False))
    config = run_wizard(args.profile, args.region, discovery)
    save_config(args.output, config)
    print(f"Wrote sanitized config to {args.output}")
    return 0


def _cmd_doctor(args: argparse.Namespace, ctx: _CliContext) -> int:
    checks = check_environment()
    if args.profile:
        checks = checks + (check_session(args.profile, args.region),)
    for check in checks:
        status = "ok" if check.ok else "missing"
        print(f"{check.name}: {status} ({check.detail})")
    print(summarize_checks(checks))
    return 0 if all(check.ok for check in checks) else 1


def _latest_journal_run_id(root: Path) -> str:
    matches = list(root.glob("*.jsonl"))
    if not matches:
        raise SystemExit("no run journals found in runs/")
    # Secondary key (name) makes the pick deterministic when two journals share an
    # mtime (coarse-resolution filesystems); otherwise max() returns an arbitrary
    # one in glob order.
    return max(matches, key=lambda path: (path.stat().st_mtime, path.name)).stem


def _print_journal_summary(summary: dict[str, object]) -> None:
    print(f"Commands: {summary['command_count']}")
    print(f"Total duration: {summary['total_duration_ms']} ms")
    failures = summary["failures"]
    if not failures:
        print("Failing commands: none")
        return
    print("Failing commands:")
    for entry in failures:
        if not isinstance(entry, dict):
            continue
        command = " ".join(str(part) for part in entry.get("argv_redacted") or [])
        returncode = entry.get("returncode")
        duration = entry.get("duration_ms")
        print(f"- rc={returncode} duration_ms={duration} {command}".rstrip())


def _cmd_journal(args: argparse.Namespace, ctx: _CliContext) -> int:
    root = Path("runs")
    run_id = _latest_journal_run_id(root) if args.last else args.run_id
    if not run_id:
        raise SystemExit("journal requires RUN_ID or --last")
    try:
        summary = redact_data(summarize(RunJournal.read(run_id, root=root)))
    except FileNotFoundError as exc:
        raise SystemExit(f"journal file not found: {root / f'{run_id}.jsonl'}") from exc
    except ValueError as exc:
        raise SystemExit(f"invalid run id: {exc}") from exc
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_journal_summary(summary)
    return 0


def _cmd_provision(args: argparse.Namespace, ctx: _CliContext) -> int:
    config = load_config(args.config)
    tfvars = write_tfvars(config)
    print(f"Wrote {tfvars}")
    if not args.render_only:
        dry_run = not args.apply and (args.dry_run or config.dry_run)
        run_terraform(config, _config_runner(config, ctx, dry_run))
    return 0


def _cmd_init_region(args: argparse.Namespace, ctx: _CliContext) -> int:
    base = load_config(args.config)
    try:
        config = build_regional_provisioning_config(
            base,
            RegionalProvisioningRequest(
                region=args.region,
                target_kind=args.target_kind,
                target_name=args.target_name,
                terraform_dir=args.terraform_dir,
                vcn_id=args.vcn_id,
                subnet_id=args.subnet_id,
            ),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    problems = validate_config(config)
    if problems:
        raise ConfigError(problems)
    output = args.output or default_regional_output(args.region)
    save_config(output, config)
    copied = prepare_regional_terraform_dir(base.terraform_dir, config.terraform_dir, refresh=args.refresh_template)
    print(f"Wrote regional provisioning config to {output}")
    if copied:
        print(f"Prepared Terraform workdir {config.terraform_dir}")
    print(f"Next: dbman-opsi provision --config {output} --render-only")
    return 0


def _cmd_enable(args: argparse.Namespace, ctx: _CliContext) -> int:
    config = load_config(args.config)
    dry_run = not args.apply and (args.dry_run or config.dry_run)
    EnablementService(_config_oci(config, ctx, dry_run)).enable_all(
        config, force_reconcile=args.force_reconcile
    )
    if args.apply and not args.skip_credentials:
        # Complete the workflow: set the DBM advanced-diagnostics preferred
        # credentials. Best-effort: blocked targets print remediation.
        for decision in CredentialService(_config_oci(config, ctx, dry_run=False)).set_all(config):
            print(f"- credentials {decision.target}: {decision.status} ({decision.detail})")
    return 0


def _cmd_prepare_prereqs(args: argparse.Namespace, ctx: _CliContext) -> int:
    config = load_config(args.config)
    dry_run = not args.apply and (args.dry_run or config.dry_run)
    PrerequisiteService(_config_oci(config, ctx, dry_run)).prepare(config, args.password_env)
    return 0


def _cmd_validate(args: argparse.Namespace, ctx: _CliContext) -> int:
    config = load_config(args.config)

    def oci_for_region(region: str) -> OciCli:
        return OciCli(
            config.profile,
            region,
            _make_runner(
                dry_run=False,
                run_id=ctx.run_id,
                profile=config.profile,
                region=region,
                verbose=ctx.verbose,
            ),
        )

    # Regression R2, formerly under `if args.command == "validate":`:
    # validate is read-only and must remain equivalent to CommandRunner(dry_run=False).
    findings = ValidationService(
        _config_oci(config, ctx, dry_run=False),
        oci_for_region=oci_for_region,
    ).validate(config)
    for finding in findings:
        print(f"- {finding}")
    return 0


def _cmd_process_insights(args: argparse.Namespace, ctx: _CliContext) -> int:
    config = load_config(args.config)

    def oci_for_region(region: str) -> OciCli:
        return OciCli(
            config.profile,
            region,
            _make_runner(
                dry_run=False,
                run_id=ctx.run_id,
                profile=config.profile,
                region=region,
                verbose=ctx.verbose,
            ),
        )

    report = ProcessInsightsService(
        _config_oci(config, ctx, dry_run=False),
        oci_for_region=oci_for_region,
    ).diagnose(config, interval=args.interval)
    if args.json:
        print(json.dumps(redact_data(report.to_dict()), indent=2, sort_keys=True))
    else:
        print(format_process_insights_report(report))
    return 0 if report.ok else 1


def _cmd_cross_region(args: argparse.Namespace, ctx: _CliContext) -> int:
    config = load_config(args.config)
    if args.regions:
        config = replace(config, monitoring_regions=parse_regions(args.regions))
        problems = validate_config(config)
        if problems:
            raise ConfigError(problems)
        save_config(args.config, config)
        print(f"Updated monitoring_regions in {args.config}")
    print(format_cross_region_plan(cross_region_plan(config)))
    return 0


def _cmd_set_credentials(args: argparse.Namespace, ctx: _CliContext) -> int:
    config = load_config(args.config)
    # Live reads + idempotent writes (named credential reuse, preferred SET).
    decisions = CredentialService(_config_oci(config, ctx, dry_run=False)).set_all(config)
    for decision in decisions:
        print(f"- {decision.target}: {decision.status} ({decision.detail})")
    blocked = [decision for decision in decisions if decision.status == "blocked"]
    return 1 if blocked else 0


def _cmd_discover(args: argparse.Namespace, ctx: _CliContext) -> int:
    oci = OciCli(args.profile, args.region, _args_runner(args, ctx, dry_run=False))
    root = args.compartment or args.tenancy
    if not root:
        raise SystemExit("discover requires --compartment or --tenancy")
    compartments = [{"id": root, "name": "root"}]
    if args.subtree:
        tenancy = args.tenancy or root
        compartments += oci.list_compartments(tenancy)
    inventory = DiscoveryService(oci).discover(compartments)
    if args.json:
        print(json.dumps(redact_data(inventory.to_dict()), indent=2, sort_keys=True))
    else:
        print_inventory(inventory)
    return 0


def _cmd_import_tf_outputs(args: argparse.Namespace, ctx: _CliContext) -> int:
    config = load_config(args.config)
    outputs = read_terraform_outputs(
        args.terraform_dir or config.terraform_dir,
        _config_runner(config, ctx, dry_run=False),
    )
    merged, changes = merge_outputs_into_config(config, outputs)
    merged, resolved_changes = _resolve_provisioned_dbcs_databases(merged, ctx)
    changes.extend(resolved_changes)
    if not changes:
        print("No new values to import from terraform outputs.")
        return 0
    for change in changes:
        print(f"Updated {change}")
    if args.dry_run:
        print("Dry run: config not written.")
        return 0
    validate_merged_config(merged)
    save_config(args.config, merged)
    print(f"Wrote merged config to {args.config}")
    return 0


def _resolve_provisioned_dbcs_databases(
    config: EnablementConfig,
    ctx: _CliContext,
) -> tuple[EnablementConfig, list[str]]:
    """Resolve Terraform-created DB system IDs to database IDs for enablement."""

    changes: list[str] = []
    targets = []
    oci = _config_oci(config, ctx, dry_run=False)
    for target in config.targets:
        if not (target.provision and target.kind == "dbcs" and target.db_system_id):
            targets.append(target)
            continue
        needs_database_id = not target.resource_id or target.resource_id == target.db_system_id
        if not needs_database_id:
            targets.append(target)
            continue
        databases = oci.list_databases(target.compartment_id or config.compartment_id or "", target.db_system_id)
        if not databases:
            targets.append(target)
            continue
        database = databases[0]
        database_id = database.get("id")
        if not database_id:
            targets.append(target)
            continue
        updates = {"resource_id": database_id}
        if not target.service_name and database.get("db-name"):
            updates["service_name"] = database["db-name"]
        target = replace(target, **updates)
        changes.append(f"target[{target.name}]: resource_id")
        targets.append(target)
    return replace(config, targets=tuple(targets)), changes


def _cmd_preflight(args: argparse.Namespace, ctx: _CliContext) -> int:
    config = load_config(args.config)
    db_check = None
    if args.db_check_file:
        db_check = parse_validation_output(Path(args.db_check_file).read_text(encoding="utf-8"))
    report = PreflightService(_config_oci(config, ctx, dry_run=False)).run(config, db_check=db_check)
    if args.json:
        print(json.dumps(redact_data(report.to_dict()), indent=2, sort_keys=True))
    else:
        print_preflight_report(report)
    return 0 if report.ok else 1


def _configure_datasafe(
    args: argparse.Namespace,
    config: EnablementConfig,
    mode: str,
    write_oci: OciCli,
) -> DataSafeService | None:
    if not args.with_data_safe or not any(target.wants("datasafe") for target in config.targets):
        return None
    return DataSafeService(
        write_oci,
        credential_provider=_make_data_safe_provider(
            mode == "apply", args.data_safe_user, args.data_safe_password_env
        ),
    )


def _cmd_configure(args: argparse.Namespace, ctx: _CliContext) -> int:
    config = load_config(args.config)
    mode = "db-side-only" if args.db_side_only else ("apply" if args.apply else "plan")
    # Reads are always live (read-only); only the enable write respects the mode.
    read_oci = _config_oci(config, ctx, dry_run=False)
    write_oci = _config_oci(config, ctx, dry_run=mode != "apply")
    datasafe = _configure_datasafe(args, config, mode, write_oci)
    service = ConfigureService(read_oci, EnablementService(write_oci), datasafe=datasafe)
    report: ConfigureReport = service.configure(
        config, mode=mode, handoff_dir=args.output, force=args.force
    )
    credential_decisions = []
    if mode == "apply" and report.ok and not args.skip_credentials:
        credential_decisions = CredentialService(
            _config_oci(config, ctx, dry_run=False)
        ).set_all(config)
    logan_decisions = []
    if args.with_log_analytics and any(target.wants("logan") for target in config.targets):
        logan_decisions = LogAnalyticsService(write_oci).enable_all(
            config, onboard_namespace=mode == "apply"
        )
        if mode == "apply":
            updated = _persist_log_analytics_entities(config, logan_decisions)
            if updated is not config:
                save_config(args.config, updated)
                config = updated
    if args.json:
        payload = report.to_dict()
        if credential_decisions:
            payload["credentials"] = [decision.__dict__ for decision in credential_decisions]
        if logan_decisions:
            payload["log_analytics"] = [decision.__dict__ for decision in logan_decisions]
        print(json.dumps(redact_data(payload), indent=2, sort_keys=True))
    else:
        print_configure_report(report)
        for decision in credential_decisions:
            print(f"- credentials {decision.target}: {decision.status} ({decision.detail})")
        for decision in logan_decisions:
            print(f"- log-analytics {decision.target}: {decision.status} ({decision.detail})")
    return 0 if report.ok else 1


def _cmd_generate_agent_scripts(args: argparse.Namespace, ctx: _CliContext) -> int:
    paths = generate_agent_scripts(load_config(args.config), Path(args.output))
    for path in paths:
        print(path)
    return 0


def _cmd_generate_db_scripts(args: argparse.Namespace, ctx: _CliContext) -> int:
    paths = generate_db_scripts(load_config(args.config), Path(args.output))
    for path in paths:
        print(path)
    return 0


def _cmd_generate_opsi_payloads(args: argparse.Namespace, ctx: _CliContext) -> int:
    paths = generate_opsi_payloads(load_config(args.config), Path(args.output))
    for path in paths:
        print(path)
    return 0


def _cmd_generate_logan_payloads(args: argparse.Namespace, ctx: _CliContext) -> int:
    paths = generate_logan_payloads(load_config(args.config), Path(args.output))
    for path in paths:
        print(path)
    if not paths:
        print("No Log Analytics targets found in config.")
    return 0


def _cmd_generate_opsi_diagnostics(args: argparse.Namespace, ctx: _CliContext) -> int:
    paths = generate_opsi_diagnostics(load_config(args.config), Path(args.output))
    for path in paths:
        print(path)
    if not paths:
        print("No DBCS/Exadata OPSI targets found in config.")
    return 0


def _db_exec_apply_decisions(args: argparse.Namespace, config: EnablementConfig):
    if not (args.bastion_id and args.target_ip and args.ssh_key):
        raise SystemExit("db-exec --apply requires --bastion-id, --target-ip, and --ssh-key")
    answers = Path(args.answers_file).read_text(encoding="utf-8") if args.answers_file else None
    runner = BastionSqlRunner(
        bastion_id=args.bastion_id,
        target_private_ip=args.target_ip,
        ssh_key=args.ssh_key,
        profile=config.profile,
        region=config.region,
        answers=answers,
    )
    return DbExecService(runner).execute(config, args.scripts_dir, force=args.force)


def _cmd_db_exec(args: argparse.Namespace, ctx: _CliContext) -> int:
    config = load_config(args.config)
    # Regenerate scripts so the plan reflects the current config.
    generate_db_scripts(config, Path(args.scripts_dir))
    if args.apply:
        decisions = _db_exec_apply_decisions(args, config)
    else:
        decisions = DbExecService().plan(config, force=args.force)
    for decision in decisions:
        print(f"- db-exec {decision.target}: {decision.action} ({decision.detail})")
    return 1 if any(d.action == "failed" for d in decisions) else 0


def _cmd_data_safe(args: argparse.Namespace, ctx: _CliContext) -> int:
    config = load_config(args.config)
    # Reads (list targets/PEs for idempotency) must be live; writes respect --apply.
    oci = _config_oci(config, ctx, dry_run=not args.apply)
    service = DataSafeService(
        oci, credential_provider=_make_data_safe_provider(args.apply, args.user, args.password_env)
    )
    decisions = service.enable_all(config)
    for decision in decisions:
        print(f"- data-safe {decision.target}: {decision.status} ({redact_text(decision.detail)})")
    if args.apply:
        updated = _persist_data_safe_targets(config, decisions)
        if updated is not config:
            save_config(args.config, updated)
            print(f"Updated Data Safe target OCIDs in {args.config}")
    blocked = [decision for decision in decisions if decision.status == "blocked"]
    return 1 if blocked else 0


def _cmd_log_analytics(args: argparse.Namespace, ctx: _CliContext) -> int:
    config = load_config(args.config)
    if args.adb_wallet_dir and not Path(args.adb_wallet_dir).exists():
        raise SystemExit(f"ADB wallet directory not found: {args.adb_wallet_dir}")
    dry_run = args.dry_run or not args.apply
    decisions = LogAnalyticsService(_config_oci(config, ctx, dry_run=dry_run)).enable_all(
        config,
        payload_dir=args.payload_dir,
        onboard_namespace=args.apply and config.log_analytics.onboard_namespace,
    )
    if args.apply:
        updated = _persist_log_analytics_entities(config, decisions)
        if updated is not config:
            save_config(args.config, updated)
            print(f"Updated Log Analytics entity/log group settings in {args.config}")
    Path(args.evidence_dir).mkdir(parents=True, exist_ok=True)
    for decision in decisions:
        print(f"- log-analytics {decision.target}: {decision.status} ({redact_text(decision.detail)})")
    blocked = [decision for decision in decisions if decision.status == "blocked"]
    return 1 if blocked else 0


def _cmd_db_incident(args: argparse.Namespace, ctx: _CliContext) -> int:
    request = DbIncidentRequest(
        ora_code=args.ora_code.upper(),
        database_name=args.database_name,
        entity_name=args.entity_name,
        incident_time=args.incident_time,
        hours_back=args.hours_back,
        window_minutes=args.window_minutes,
        profile=args.profile,
        compartment_id=args.compartment_id,
        include_sources=tuple(source.strip() for source in args.include_sources.split(",") if source.strip()),
        limit=args.limit,
    )
    oci = OciCli(args.profile, args.region, _args_runner(args, ctx, dry_run=False))
    bundle = DbIncidentEvidenceService(oci).build(request).to_dict()
    if args.json:
        print(json.dumps(redact_data(bundle), indent=2, sort_keys=True))
        return 0
    print(bundle["summary"])
    print("Cross-source evidence:")
    for status in bundle["cross_source_evidence"]:
        print(f"- {status['source']}: {status['status']} ({status['detail']})")
    print("Next diagnostics:")
    for item in bundle["next_diagnostics"]:
        print(f"- {item}")
    print(f"Uncertainty: {bundle['uncertainty']}")
    return 0


def _cmd_generate_db_incident_demo(args: argparse.Namespace, ctx: _CliContext) -> int:
    paths = generate_db_incident_demo(args.output, apply=args.apply, scenario_id=args.scenario_id)
    for path in paths:
        print(path)
    return 0


def _cmd_generate_sqlcl_mcp(args: argparse.Namespace, ctx: _CliContext) -> int:
    """Render a secret-free SQLcl MCP client configuration for operator review."""

    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = build_sqlcl_mcp_config(
        SqlclMcpConfig(
            name=args.name,
            connect_descriptor=args.connect_descriptor,
            secret_id=args.secret_id,
        )
    )
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(destination)
    return 0


def _cmd_generate_disposable_assets(args: argparse.Namespace, ctx: _CliContext) -> int:
    """Generate reviewable, secret-free inputs and release-evidence scaffold."""

    output = Path(args.output)
    dashboards = output / "dashboards"
    dashboards.mkdir(parents=True, exist_ok=True)
    bootstrap = output / "bootstrap-dedicated-users.sql"
    bootstrap.write_text(generate_role_bootstrap_sql(args.target_name), encoding="utf-8")
    for name, dashboard in generate_dashboard_definitions(args.lifecycle_id).items():
        (dashboards / f"{name}.json").write_text(json.dumps(dashboard, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    evidence = build_release_evidence(
        lifecycle_id=args.lifecycle_id,
        phases={phase: "not-run" for phase in RELEASE_PHASES},
    )
    evidence_path = output / "release-evidence.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(bootstrap)
    print(dashboards)
    print(evidence_path)
    return 0


def _cmd_reveal_vault_secret(args: argparse.Namespace, ctx: _CliContext) -> int:
    """Deliberately narrow plaintext boundary; do not add normal logging here."""

    if not args.confirm_reveal:
        raise SystemExit("Refusing to reveal a secret without --confirm-reveal")
    oci = OciCli(args.profile, args.region, _args_runner(args, ctx, dry_run=False))
    print(oci.get_secret_bundle_content(args.secret_id))
    return 0


def _cmd_credential_reset_plan(args: argparse.Namespace, ctx: _CliContext) -> int:
    plan = build_reset_plan(args.role)
    if args.json:
        print(json.dumps(plan.__dict__, indent=2, sort_keys=True))
    else:
        print(f"Reset role: {plan.role}")
        for index, step in enumerate(plan.steps, start=1):
            print(f"{index}. {step}")
        print("Refresh bindings: " + ", ".join(plan.refresh_bindings))
    return 0


def _cmd_bind_vault_secret(args: argparse.Namespace, ctx: _CliContext) -> int:
    config = load_config(args.config)
    targets = tuple(
        replace(target, password_secret_id=args.secret_id) if target.name == args.target else target
        for target in config.targets
    )
    if targets == config.targets:
        raise SystemExit(f"Target not found: {args.target}")
    save_config(args.config, replace(config, targets=targets))
    print(f"Bound Vault secret reference to target {args.target}")
    return 0


def _command_handlers():
    return {
        "plan": _cmd_plan,
        "journal": _cmd_journal,
        "doctor": _cmd_doctor,
        "provision": _cmd_provision,
        "init-region": _cmd_init_region,
        "enable": _cmd_enable,
        "prepare-prereqs": _cmd_prepare_prereqs,
        "validate": _cmd_validate,
        "process-insights": _cmd_process_insights,
        "cross-region": _cmd_cross_region,
        "set-credentials": _cmd_set_credentials,
        "discover": _cmd_discover,
        "import-tf-outputs": _cmd_import_tf_outputs,
        "preflight": _cmd_preflight,
        "configure": _cmd_configure,
        "generate-agent-scripts": _cmd_generate_agent_scripts,
        "generate-db-scripts": _cmd_generate_db_scripts,
        "generate-opsi-payloads": _cmd_generate_opsi_payloads,
        "generate-logan-payloads": _cmd_generate_logan_payloads,
        "generate-opsi-diagnostics": _cmd_generate_opsi_diagnostics,
        "db-exec": _cmd_db_exec,
        "data-safe": _cmd_data_safe,
        "log-analytics": _cmd_log_analytics,
        "db-incident": _cmd_db_incident,
        "generate-db-incident-demo": _cmd_generate_db_incident_demo,
        "generate-sqlcl-mcp": _cmd_generate_sqlcl_mcp,
        "generate-disposable-assets": _cmd_generate_disposable_assets,
        "reveal-vault-secret": _cmd_reveal_vault_secret,
        "credential-reset-plan": _cmd_credential_reset_plan,
        "bind-vault-secret": _cmd_bind_vault_secret,
        "onboard": _cmd_onboard,
        "resume": _cmd_resume,
        "fleet-status": _cmd_fleet_status,
        "import-handoff": _cmd_import_handoff,
        "import-cleanup-handoff": _cmd_import_cleanup_handoff,
        "import-collection-evidence": _cmd_import_collection_evidence,
        "offboard": _cmd_offboard,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging()
    load_env_file()
    ctx = _CliContext(run_id=str(uuid.uuid4()), verbose=args.verbose)
    log.debug("run_id=%s", ctx.run_id)
    handler = _command_handlers().get(args.command)
    if handler is None:
        raise ValueError(f"Unhandled command {args.command}")
    try:
        return handler(args, ctx)
    except DiscoveryScopeError as exc:
        if args.command not in {"onboard", "resume"}:
            raise
        print(redact_text(str(exc)), file=sys.stderr)
        return 3
    except (ValueError, ConfigError) as exc:
        if args.command not in {"onboard", "resume", "import-handoff", "import-cleanup-handoff", "import-collection-evidence", "fleet-status", "offboard"}:
            raise
        print(redact_text(str(exc)), file=sys.stderr)
        return 5
    except KeyError as exc:
        if args.command not in {"onboard", "resume", "import-handoff", "import-cleanup-handoff", "import-collection-evidence", "fleet-status", "offboard"}:
            raise
        print(redact_text(str(exc)), file=sys.stderr)
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
