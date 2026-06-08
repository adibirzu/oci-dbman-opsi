"""Command line interface for dbman-opsi."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from dataclasses import replace
from pathlib import Path

from dbman_opsi.agent_scripts import generate_agent_scripts
from dbman_opsi.bastion_exec import BastionSqlRunner
from dbman_opsi.config import EnablementConfig, load_config, save_config
from dbman_opsi.credentials import CredentialService
from dbman_opsi.datasafe import DataSafeDecision, DataSafeService
from dbman_opsi.db_check import parse_validation_output
from dbman_opsi.db_exec import DbExecService
from dbman_opsi.db_scripts import generate_db_scripts
from dbman_opsi.discovery import DiscoveryService
from dbman_opsi.doctor import check_environment, check_session, summarize_checks
from dbman_opsi.enablement import EnablementService
from dbman_opsi.oci_cli import OciCli
from dbman_opsi.opsi_payloads import generate_opsi_payloads
from dbman_opsi.orchestrator import ConfigureReport, ConfigureService
from dbman_opsi.preflight import PreflightService
from dbman_opsi.prerequisites import PrerequisiteService
from dbman_opsi.redact import redact_data
from dbman_opsi.reporting import print_configure_report, print_inventory, print_preflight_report
from dbman_opsi.runner import CommandRunner
from dbman_opsi.terraform import run_terraform, write_tfvars
from dbman_opsi.tf_outputs import merge_outputs_into_config, read_terraform_outputs
from dbman_opsi.validation import ValidationService
from dbman_opsi.wizard import run_wizard


def _add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="dbman-opsi.yaml", help="Path to YAML/JSON config")
    parser.add_argument("--dry-run", action="store_true", help="Print commands instead of executing")
    parser.add_argument("--apply", action="store_true", help="Execute changes even when config dry_run is true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dbman-opsi")
    subcommands = parser.add_subparsers(dest="command", required=True)

    plan = subcommands.add_parser("plan", help="Run interactive discovery/planning wizard")
    plan.add_argument("--profile", required=True)
    plan.add_argument("--region", required=True)
    plan.add_argument("--output", default="dbman-opsi.yaml")

    discover = subcommands.add_parser(
        "discover",
        help="Read-only inventory of reusable resources (subnets, vaults, databases, endpoints, agents, bastions)",
    )
    discover.add_argument("--profile", required=True)
    discover.add_argument("--region", required=True)
    discover.add_argument("--compartment", help="Root compartment OCID (defaults to tenancy)")
    discover.add_argument("--tenancy", help="Tenancy OCID (defaults to compartment)")
    discover.add_argument("--subtree", action="store_true", help="Scan the compartment subtree")
    discover.add_argument("--json", action="store_true", help="Emit the inventory as JSON")

    provision = subcommands.add_parser("provision", help="Render tfvars and run Terraform")
    _add_config_args(provision)
    provision.add_argument("--render-only", action="store_true", help="Only write terraform.tfvars.json")

    import_outputs = subcommands.add_parser(
        "import-tf-outputs",
        help="Read terraform outputs and merge created OCIDs (subnet, PE, provisioned DBs) back into the config",
    )
    import_outputs.add_argument("--config", default="dbman-opsi.yaml")
    import_outputs.add_argument("--terraform-dir", help="Override config.terraform_dir")
    import_outputs.add_argument("--dry-run", action="store_true", help="Print changes without writing the config")

    enable = subcommands.add_parser("enable", help="Enable Database Management and Ops Insights")
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

    prereqs = subcommands.add_parser("prepare-prereqs", help="Create OCI-side prerequisites such as private endpoints and optional Vault secrets")
    _add_config_args(prereqs)
    prereqs.add_argument("--password-env", help="Environment variable containing the monitoring password for Vault secret creation")

    validate = subcommands.add_parser("validate", help="Validate registrations and collection readiness")
    _add_config_args(validate)

    preflight = subcommands.add_parser("preflight", help="Read-only check of all enablement prerequisites")
    preflight.add_argument("--config", default="dbman-opsi.yaml")
    preflight.add_argument("--json", action="store_true", help="Emit the report as JSON")
    preflight.add_argument(
        "--db-check-file",
        help="Spooled output of 04-validate-monitoring-user.sql to verify the DB monitoring user",
    )

    configure = subcommands.add_parser(
        "configure",
        help="Orchestrated flow: detect, branch by location, gate on prerequisites, then enable or hand off",
    )
    configure.add_argument("--config", default="dbman-opsi.yaml")
    configure.add_argument("--apply", action="store_true", help="Enable services when all prerequisites pass")
    configure.add_argument("--db-side-only", action="store_true", help="Generate DB-side handoff packets and stop")
    configure.add_argument("--force", action="store_true", help="Ignore blocking prerequisite failures")
    configure.add_argument("--output", default="generated/handoff", help="Handoff packet output directory")
    configure.add_argument("--json", action="store_true", help="Emit the report as JSON")
    configure.add_argument(
        "--with-data-safe",
        action="store_true",
        help="Also register Data Safe targets (datasafe pillar) during --apply",
    )
    configure.add_argument("--data-safe-user", help="Data Safe service account (default: target monitoring_user or DBSNMP)")
    configure.add_argument("--data-safe-password-env", help="Env var holding the Data Safe account password (non-interactive)")

    agent = subcommands.add_parser("generate-agent-scripts", help="Generate Management Agent install scripts")
    agent.add_argument("--config", default="dbman-opsi.yaml")
    agent.add_argument("--output", default="generated/agents")

    db_scripts = subcommands.add_parser("generate-db-scripts", help="Generate database-side SQL scripts")
    db_scripts.add_argument("--config", default="dbman-opsi.yaml")
    db_scripts.add_argument("--output", default="generated/db-scripts")

    opsi_payloads = subcommands.add_parser("generate-opsi-payloads", help="Generate Operations Insights JSON payload files")
    opsi_payloads.add_argument("--config", default="dbman-opsi.yaml")
    opsi_payloads.add_argument("--output", default="generated/opsi-payloads")

    set_creds = subcommands.add_parser(
        "set-credentials",
        help="Set DBM advanced-diagnostics preferred credentials via a Vault named credential",
    )
    set_creds.add_argument("--config", default="dbman-opsi.yaml")

    doctor = subcommands.add_parser("doctor", help="Check local or Cloud Shell prerequisites")
    doctor.add_argument("--profile", help="Also verify the OCI session is authenticated for this profile")
    doctor.add_argument("--region", help="Region to use for the session check")

    db_exec = subcommands.add_parser(
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

    data_safe = subcommands.add_parser(
        "data-safe",
        help="Register databases as Data Safe targets (security pillar) for targets that opt into 'datasafe'",
    )
    data_safe.add_argument("--config", default="dbman-opsi.yaml")
    data_safe.add_argument("--apply", action="store_true", help="Perform live registration (otherwise dry-run)")
    data_safe.add_argument("--user", help="Data Safe service account (default: target monitoring_user or DBSNMP)")
    data_safe.add_argument("--password-env", help="Env var holding the Data Safe account password (non-interactive)")

    return parser


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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plan":
        discovery = OciCli(args.profile, args.region, CommandRunner(dry_run=False))
        config = run_wizard(args.profile, args.region, discovery)
        save_config(args.output, config)
        print(f"Wrote sanitized config to {args.output}")
        return 0

    if args.command == "doctor":
        checks = check_environment()
        if args.profile:
            checks = checks + (check_session(args.profile, args.region),)
        for check in checks:
            status = "ok" if check.ok else "missing"
            print(f"{check.name}: {status} ({check.detail})")
        print(summarize_checks(checks))
        return 0 if all(check.ok for check in checks) else 1

    if args.command == "provision":
        config = load_config(args.config)
        tfvars = write_tfvars(config)
        print(f"Wrote {tfvars}")
        if not args.render_only:
            run_terraform(config, CommandRunner(dry_run=not args.apply and (args.dry_run or config.dry_run)))
        return 0

    if args.command == "enable":
        config = load_config(args.config)
        runner = CommandRunner(dry_run=not args.apply and (args.dry_run or config.dry_run))
        EnablementService(OciCli(config.profile, config.region, runner)).enable_all(
            config, force_reconcile=args.force_reconcile
        )
        if args.apply and not args.skip_credentials:
            # Complete the workflow: set the DBM advanced-diagnostics preferred
            # credentials (live + idempotent). Best-effort — blocked targets are
            # reported with remediation rather than failing the enable.
            live = OciCli(config.profile, config.region, CommandRunner(dry_run=False))
            for decision in CredentialService(live).set_all(config):
                print(f"- credentials {decision.target}: {decision.status} ({decision.detail})")
        return 0

    if args.command == "prepare-prereqs":
        config = load_config(args.config)
        runner = CommandRunner(dry_run=not args.apply and (args.dry_run or config.dry_run))
        PrerequisiteService(OciCli(config.profile, config.region, runner)).prepare(config, args.password_env)
        return 0

    if args.command == "validate":
        config = load_config(args.config)
        # validate is read-only: reads must always execute. Building the runner
        # from args.dry_run would stub every OCI read to {} under
        # `validate --dry-run`, yielding bogus NOT_FOUND/empty results.
        runner = CommandRunner(dry_run=False)
        findings = ValidationService(OciCli(config.profile, config.region, runner)).validate(config)
        for finding in findings:
            print(f"- {finding}")
        return 0

    if args.command == "set-credentials":
        config = load_config(args.config)
        # Live reads + idempotent writes (named credential reuse, preferred
        # credential SET is idempotent), so re-runs are safe.
        oci = OciCli(config.profile, config.region, CommandRunner(dry_run=False))
        decisions = CredentialService(oci).set_all(config)
        for decision in decisions:
            print(f"- {decision.target}: {decision.status} ({decision.detail})")
        blocked = [decision for decision in decisions if decision.status == "blocked"]
        return 1 if blocked else 0

    if args.command == "discover":
        oci = OciCli(args.profile, args.region, CommandRunner(dry_run=False))
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

    if args.command == "import-tf-outputs":
        config = load_config(args.config)
        terraform_dir = args.terraform_dir or config.terraform_dir
        outputs = read_terraform_outputs(terraform_dir, CommandRunner(dry_run=False))
        merged, changes = merge_outputs_into_config(config, outputs)
        if not changes:
            print("No new values to import from terraform outputs.")
            return 0
        for change in changes:
            print(f"Updated {change}")
        if args.dry_run:
            print("Dry run: config not written.")
            return 0
        save_config(args.config, merged)
        print(f"Wrote merged config to {args.config}")
        return 0

    if args.command == "preflight":
        config = load_config(args.config)
        db_check = None
        if args.db_check_file:
            db_check = parse_validation_output(Path(args.db_check_file).read_text(encoding="utf-8"))
        service = PreflightService(OciCli(config.profile, config.region, CommandRunner(dry_run=False)))
        report = service.run(config, db_check=db_check)
        if args.json:
            print(json.dumps(redact_data(report.to_dict()), indent=2, sort_keys=True))
        else:
            print_preflight_report(report)
        return 0 if report.ok else 1

    if args.command == "configure":
        config = load_config(args.config)
        mode = "db-side-only" if args.db_side_only else ("apply" if args.apply else "plan")
        # Reads are always live (read-only); only the enable write respects the mode.
        read_oci = OciCli(config.profile, config.region, CommandRunner(dry_run=False))
        write_oci = OciCli(config.profile, config.region, CommandRunner(dry_run=mode != "apply"))
        datasafe = None
        if args.with_data_safe and any(target.wants("datasafe") for target in config.targets):
            datasafe = DataSafeService(
                write_oci,
                credential_provider=_make_data_safe_provider(
                    mode == "apply", args.data_safe_user, args.data_safe_password_env
                ),
            )
        service = ConfigureService(read_oci, EnablementService(write_oci), datasafe=datasafe)
        report: ConfigureReport = service.configure(
            config, mode=mode, handoff_dir=args.output, force=args.force
        )
        if args.json:
            print(json.dumps(redact_data(report.to_dict()), indent=2, sort_keys=True))
        else:
            print_configure_report(report)
        return 0 if report.ok else 1

    if args.command == "generate-agent-scripts":
        config = load_config(args.config)
        paths = generate_agent_scripts(config, Path(args.output))
        for path in paths:
            print(path)
        return 0

    if args.command == "generate-db-scripts":
        config = load_config(args.config)
        paths = generate_db_scripts(config, Path(args.output))
        for path in paths:
            print(path)
        return 0

    if args.command == "generate-opsi-payloads":
        config = load_config(args.config)
        paths = generate_opsi_payloads(config, Path(args.output))
        for path in paths:
            print(path)
        return 0

    if args.command == "db-exec":
        config = load_config(args.config)
        # Regenerate scripts so the plan reflects the current config, then show the
        # per-target run plan. Actual auto-execution against the DB runs through the
        # Bastion procedure / handoff packet (see generated <target>/HANDOFF.md).
        generate_db_scripts(config, Path(args.scripts_dir))
        if args.apply:
            if not (args.bastion_id and args.target_ip and args.ssh_key):
                raise SystemExit("db-exec --apply requires --bastion-id, --target-ip, and --ssh-key")
            answers = Path(args.answers_file).read_text(encoding="utf-8") if args.answers_file else None
            runner = BastionSqlRunner(
                bastion_id=args.bastion_id, target_private_ip=args.target_ip, ssh_key=args.ssh_key,
                profile=config.profile, region=config.region, answers=answers,
            )
            decisions = DbExecService(runner).execute(config, args.scripts_dir, force=args.force)
        else:
            decisions = DbExecService().plan(config, force=args.force)
        for decision in decisions:
            print(f"- db-exec {decision.target}: {decision.action} ({decision.detail})")
        return 1 if any(d.action == "failed" for d in decisions) else 0

    if args.command == "data-safe":
        config = load_config(args.config)
        # Reads (list targets/PEs for idempotency) must be live; writes respect
        # --apply via the runner so a dry-run prints commands without registering.
        runner = CommandRunner(dry_run=not args.apply)
        oci = OciCli(config.profile, config.region, runner)
        service = DataSafeService(
            oci, credential_provider=_make_data_safe_provider(args.apply, args.user, args.password_env)
        )
        decisions = service.enable_all(config)
        for decision in decisions:
            print(f"- data-safe {decision.target}: {decision.status} ({decision.detail})")
        if args.apply:
            updated = _persist_data_safe_targets(config, decisions)
            if updated is not config:
                save_config(args.config, updated)
                print(f"Updated Data Safe target OCIDs in {args.config}")
        blocked = [decision for decision in decisions if decision.status == "blocked"]
        return 1 if blocked else 0

    raise ValueError(f"Unhandled command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
