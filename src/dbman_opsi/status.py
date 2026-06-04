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
