"""SQLcl MCP configuration and local read-only policy helpers.

Read-only database grants are the enforcement boundary.  The lexical guard here
is defence in depth for callers that choose to preflight user-provided SQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class SqlclMcpError(ValueError):
    """Raised for invalid or unsafe SQLcl MCP configuration."""


@dataclass(frozen=True)
class SqlclMcpConfig:
    name: str
    connect_descriptor: str
    secret_id: str
    username: str = "MCP_READONLY"


_READ_ONLY = re.compile(r"^(?:select|with|explain\s+plan\s+for)\b", re.IGNORECASE | re.DOTALL)
_COMMENT = re.compile(r"^(?:--[^\n]*(?:\n|$)|/\*.*?\*/\s*)*", re.DOTALL)


def is_read_only_sql(statement: str) -> bool:
    """Accept one SELECT/CTE/EXPLAIN statement and reject compound SQL."""

    normalized = _COMMENT.sub("", statement).strip()
    if not normalized or ";" in normalized:
        return False
    return _READ_ONLY.match(normalized) is not None


def build_sqlcl_mcp_config(config: SqlclMcpConfig) -> dict[str, Any]:
    """Build a credential-free MCP client configuration template.

    ``DBMAN_OPSI_SECRET_ID`` is a reference.  The launcher retrieves its value
    only for the child SQLcl process after the operator has authenticated with
    OCI; it is never written into this configuration.
    """

    if config.username != "MCP_READONLY":
        raise SqlclMcpError("SQLcl MCP must use the dedicated MCP_READONLY account")
    if not config.secret_id or not config.connect_descriptor:
        raise SqlclMcpError("connect descriptor and Vault secret reference are required")
    return {
        "mcpServers": {
            config.name: {
                "command": "sql",
                "args": ["-mcp"],
                "env": {
                    "DBMAN_OPSI_CONNECT_DESCRIPTOR": config.connect_descriptor,
                    "DBMAN_OPSI_DB_USERNAME": config.username,
                    "DBMAN_OPSI_SECRET_ID": config.secret_id,
                    "DBMAN_OPSI_READ_ONLY": "true",
                },
            }
        }
    }
