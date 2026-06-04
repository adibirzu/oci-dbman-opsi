"""Command line interface for dbman-opsi."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dbman_opsi.agent_scripts import generate_agent_scripts
from dbman_opsi.config import load_config, save_config
from dbman_opsi.credentials import CredentialService
from dbman_opsi.db_check import parse_validation_output
from dbman_opsi.db_scripts import generate_db_scripts
from dbman_opsi.discovery import DiscoveryService
from dbman_opsi.doctor import check_environment, check_session, summarize_checks
from dbman_opsi.enablement import EnablementService
from dbman_opsi.oci_cli import OciCli
from dbman_opsi.opsi_payloads import generate_opsi_payloads
from dbman_opsi.orchestrator import ConfigureReport, ConfigureService
from dbman_opsi.preflight import PreflightService
from dbman_opsi.prerequisites import PrerequisiteService
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

    return parser


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
        EnablementService(OciCli(config.profile, config.region, runner)).enable_all(config)
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
        runner = CommandRunner(dry_run=args.dry_run)
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
            print(json.dumps(inventory.to_dict(), indent=2, sort_keys=True))
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
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        else:
            print_preflight_report(report)
        return 0 if report.ok else 1

    if args.command == "configure":
        config = load_config(args.config)
        mode = "db-side-only" if args.db_side_only else ("apply" if args.apply else "plan")
        # Reads are always live (read-only); only the enable write respects the mode.
        read_oci = OciCli(config.profile, config.region, CommandRunner(dry_run=False))
        write_oci = OciCli(config.profile, config.region, CommandRunner(dry_run=mode != "apply"))
        service = ConfigureService(read_oci, EnablementService(write_oci))
        report: ConfigureReport = service.configure(
            config, mode=mode, handoff_dir=args.output, force=args.force
        )
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
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

    raise ValueError(f"Unhandled command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
