from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dbman_opsi.oci_cli import OciCli
from dbman_opsi.runner import CommandResult


class _Runner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, args, cwd=None, check=True, retry_on_transient=False):
        self.commands.append(args)
        return CommandResult(tuple(args), "{}", "", 0)


def _oci(runner: _Runner) -> OciCli:
    return OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]


def test_offboarding_facade_uses_explicit_reverse_lifecycle_verbs() -> None:
    runner = _Runner()
    oci = _oci(runner)

    oci.disable_opsi_database_insight("opsi")
    oci.disable_dbm_pdb("pdb", "DIAGNOSTICS_AND_MANAGEMENT")
    oci.disable_dbm_cdb("cdb", "DIAGNOSTICS_AND_MANAGEMENT", can_disable_all_pdbs=True)
    oci.disable_database_management("database")
    oci.disable_pluggable_database_management("pluggable")
    oci.disable_autonomous_database_management("autonomous")
    oci.delete_data_safe_target("datasafe-target")
    oci.delete_named_credential("credential")
    oci.delete_run_owned_dbcs_test_database("dbcs")
    oci.delete_run_owned_autonomous_test_database("adb")
    oci.delete_db_management_private_endpoint("dbm-endpoint")
    oci.delete_opsi_private_endpoint("opsi-endpoint")
    oci.schedule_run_owned_secret_deletion(
        "secret", not_before=datetime(2026, 7, 28, tzinfo=UTC)
    )

    commands = [command[5:] for command in runner.commands]
    assert commands == [
        ["opsi", "database-insights", "disable", "--database-insight-id", "opsi"],
        [
            "database-management", "managed-database", "disable-pluggable-database-management-feature",
            "--pluggable-database-id", "pdb", "--feature", "DIAGNOSTICS_AND_MANAGEMENT",
        ],
        [
            "database-management", "managed-database", "disable-database-management-feature",
            "--database-id", "cdb", "--feature", "DIAGNOSTICS_AND_MANAGEMENT",
            "--can-disable-all-pdbs", "true",
        ],
        ["db", "database", "disable-database-management", "--database-id", "database"],
        ["db", "pluggable-database", "disable-pluggable-database-management", "--pluggable-database-id", "pluggable"],
        ["db", "autonomous-database", "disable-autonomous-database-management", "--autonomous-database-id", "autonomous"],
        ["data-safe", "target-database", "delete", "--target-database-id", "datasafe-target", "--force"],
        ["database-management", "named-credential", "delete", "--named-credential-id", "credential", "--force"],
        ["db", "database", "delete", "--database-id", "dbcs", "--force"],
        ["db", "autonomous-database", "delete", "--autonomous-database-id", "adb", "--force"],
        ["database-management", "private-endpoint", "delete", "--private-endpoint-id", "dbm-endpoint", "--force"],
        ["opsi", "opsi-private-endpoint", "delete", "--opsi-private-endpoint-id", "opsi-endpoint", "--force"],
        [
            "vault", "secret", "schedule-secret-deletion", "--secret-id", "secret",
            "--time-of-deletion", "2026-07-28T00:00:00+00:00",
        ],
    ]


def test_secret_schedule_uses_oci_default_earliest_time_not_evidence_retention_delay() -> None:
    runner = _Runner()

    _oci(runner).schedule_run_owned_secret_deletion("secret")

    assert runner.commands[0][5:] == [
        "vault", "secret", "schedule-secret-deletion", "--secret-id", "secret"
    ]


@pytest.mark.parametrize("feature", ("", "UNREVIEWED_FEATURE"))
def test_dbm_cleanup_contract_rejects_missing_or_unsupported_feature(feature: str) -> None:
    runner = _Runner()

    with pytest.raises(ValueError, match="DBM feature"):
        _oci(runner).disable_dbm_pdb("pdb", feature)

    assert runner.commands == []


def test_dbm_cleanup_contract_rejects_pdb_cascade_for_non_diagnostics_feature() -> None:
    runner = _Runner()

    with pytest.raises(ValueError, match="only valid"):
        _oci(runner).disable_dbm_cdb("cdb", "SQLWATCH", can_disable_all_pdbs=True)

    assert runner.commands == []


def test_endpoint_and_network_cleanup_facades_use_installed_delete_contracts() -> None:
    runner = _Runner()
    oci = _oci(runner)

    oci.delete_data_safe_private_endpoint("data-safe")
    oci.delete_run_owned_route_table("route-table")
    oci.delete_run_owned_security_list("security-list")

    assert [command[5:] for command in runner.commands] == [
        ["data-safe", "private-endpoint", "delete", "--private-endpoint-id", "data-safe", "--force"],
        ["network", "route-table", "delete", "--rt-id", "route-table", "--force"],
        ["network", "security-list", "delete", "--security-list-id", "security-list", "--force"],
    ]


def test_log_analytics_dissociation_uses_recorded_items_file_only() -> None:
    runner = _Runner()
    _oci(runner).delete_log_analytics_associations(
        "logan", "compartment", [{"entityId": "entity", "sourceName": "source"}]
    )

    command = runner.commands[0][5:]
    assert command[:7] == [
        "log-analytics", "assoc", "delete-assocs", "--namespace-name", "logan", "--compartment-id", "compartment"
    ]
    assert command[7] == "--items" and command[8].startswith("file://")
