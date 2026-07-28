"""Generated, redacted artifacts for the disposable database demo release."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from dbman_opsi.redact import redact_data


RELEASE_PHASES: tuple[str, ...] = (
    "provision",
    "vault_bootstrap",
    "user_bootstrap",
    "mcp_check",
    "incident_scenario",
    "observability",
    "teardown",
)


def generate_role_bootstrap_sql(target_name: str) -> str:
    """Return idempotent SQL for four dedicated demo identities.

    Values are prompted in SQLcl/SQL*Plus with ``hide`` and are deliberately
    absent from generated files. The invoking bootstrap runner retrieves them
    from Vault immediately before execution.
    """

    header = f"""-- Disposable DB demo role bootstrap for {target_name}
-- Run as a dedicated administrator in the intended PDB/container.
-- Passwords are prompted and must be supplied by an authorized Vault retrieval flow.
set echo on
set verify off
set serveroutput on
whenever sqlerror exit sql.sqlcode
"""
    roles = {
        "DBM_MON": ("create session", "select_catalog_role", "select any dictionary"),
        "DATASAFE_AUDIT": ("create session", "select_catalog_role", "audit_viewer"),
        "MCP_READONLY": ("create session",),
        "DBINC_LAB": ("create session", "create table", "create procedure", "create sequence"),
    }
    parts = [header]
    for role, grants in roles.items():
        variable = role.lower()
        parts.append(f"""
accept {variable}_password char hide prompt '{role} password (Vault retrieval): '
declare
  l_exists number;
begin
  select count(*) into l_exists from dba_users where username = '{role}';
  if l_exists = 0 then
    execute immediate 'create user {role} identified by "' || replace('&{variable}_password', '"', '""') || '" account unlock';
  else
    execute immediate 'alter user {role} identified by "' || replace('&{variable}_password', '"', '""') || '" account unlock';
  end if;
end;
/
""")
        parts.extend(f"grant {grant} to {role};\n" for grant in grants)
    parts.append("""
-- After the incident workload creates DBINC_LAB.incident_event_log, grant only
-- its approved evidence objects to MCP_READONLY. Do not grant broad dictionary
-- or write privileges to the MCP identity.
""")
    return "".join(parts)


def generate_dashboard_definitions(lifecycle_id: str) -> dict[str, dict[str, Any]]:
    """Create portable dashboard definitions keyed by release lifecycle tag."""

    panels = {
        "db-health": "Database Management health and availability evidence",
        "opsi-capacity": "Operations Insights capacity and forecast evidence",
        "datasafe-audit": "Data Safe target, profile, trail, and audit-event evidence",
        "incident-timeline": "Cross-pillar incident timeline filtered by scenario ID",
        "credential-lifecycle": "Secret version, reset verdict, and service-binding evidence",
        "teardown-evidence": "Tagged-resource teardown and seven-day retention evidence",
    }
    return {
        name: {
            "schemaVersion": 1,
            "displayName": f"dbman-opsi {name}",
            "description": description,
            "filters": {"dbman-opsi.lifecycle": lifecycle_id},
            "widgets": [{"title": description, "query": f"* | where lifecycle_id = '{lifecycle_id}'"}],
        }
        for name, description in panels.items()
    }


def build_release_evidence(
    *, lifecycle_id: str,
    phases: Mapping[str, str],
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a portable evidence envelope without credentials or topology."""

    missing = [phase for phase in RELEASE_PHASES if phase not in phases]
    normalized = {phase: phases.get(phase, "not-run") for phase in RELEASE_PHASES}
    verdict = "passed" if not missing and all(value == "passed" for value in normalized.values()) else "failed"
    return redact_data(
        {
            "release": "disposable-db-e2e",
            "lifecycle_id": lifecycle_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "retention_days": 7,
            "verdict": verdict,
            "phases": normalized,
            "missing_phases": missing,
            "details": dict(details or {}),
        }
    )
