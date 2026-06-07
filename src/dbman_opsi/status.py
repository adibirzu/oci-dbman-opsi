"""Accessors for Database Management / Ops Insights status across resource shapes.

OCI exposes enablement status differently per resource:

- Autonomous Database: top-level ``database-management-status`` /
  ``operations-insights-status``.
- Container/non-CDB database (``db database get``): nested under
  ``database-management-config.management-status``.
- Pluggable database (``db pluggable-database get``): nested under
  ``pluggable-database-management-config.management-status``.
"""

from __future__ import annotations

from typing import Any

_ENABLED_STATES = {"ENABLED", "ENABLING"}


def dbm_status(details: dict[str, Any], kind: str, role: str = "CDB") -> str | None:
    """Return the Database Management status for any supported resource shape."""

    if kind == "autonomous":
        return details.get("database-management-status")
    if role == "PDB":
        config = details.get("pluggable-database-management-config") or {}
    else:
        config = details.get("database-management-config") or {}
    return config.get("management-status") or config.get("database-management-status")


def opsi_status(details: dict[str, Any], kind: str) -> str | None:
    """Ops Insights status (only carried on the Autonomous Database resource)."""

    if kind == "autonomous":
        return details.get("operations-insights-status")
    return None


def is_enabled(status: str | None) -> bool:
    return str(status or "").upper() in _ENABLED_STATES


# Lifecycle states that count as an OPSI insight or Data Safe target being "on".
_RESOURCE_ENABLED_STATES = {"ACTIVE", "CREATING", "UPDATING"}


def _candidate_ids(record: dict[str, Any]) -> set[str]:
    """Collect every OCID a separate resource might use to reference a DB."""

    details = record.get("database-details") or {}
    ids = {
        record.get("database-id"),
        record.get("id"),
        details.get("database-id"),
        details.get("db-system-id"),
        details.get("autonomous-database-id"),
        details.get("vm-cluster-id"),
        details.get("infrastructure-id"),
    }
    return {str(value) for value in ids if value}


def opsi_insight_status(insights: list[dict[str, Any]], db_id: str) -> str:
    """ENABLED if an OPSI insight references ``db_id`` and is in an active state."""

    for insight in insights:
        if db_id in _candidate_ids(insight):
            if str(insight.get("lifecycle-state", "")).upper() in _RESOURCE_ENABLED_STATES:
                return "ENABLED"
            return str(insight.get("lifecycle-state") or "NOT_ENABLED")
    return "NOT_ENABLED"


def data_safe_status(targets: list[dict[str, Any]], db_id: str, db_system_id: str | None = None) -> str:
    """ENABLED if a Data Safe target-database references this DB (or its DB system).

    Data Safe registration is a standalone ``target-database`` resource, so a DB
    is "Data Safe enabled" when a registered target's database details point back
    at this database OCID (autonomous / cloud) or its parent DB-system OCID
    (Base Database / Exadata cloud service).
    """

    wanted = {db_id}
    if db_system_id:
        wanted.add(str(db_system_id))
    for target in targets:
        if wanted & _candidate_ids(target):
            if str(target.get("lifecycle-state", "")).upper() in _RESOURCE_ENABLED_STATES:
                return "ENABLED"
            return str(target.get("lifecycle-state") or "NOT_ENABLED")
    return "NOT_ENABLED"
