"""Safe, reference-only models for disposable demo database credentials.

This module deliberately does not retrieve or persist secret values.  OCI Vault
is the value boundary; CLI and report surfaces use these immutable references.
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from typing import Iterable


DEMO_DATABASE_ROLES: tuple[str, ...] = ("DBM_MON", "DATASAFE_AUDIT", "MCP_READONLY", "DBINC_LAB")
_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "_#%+"


@dataclass(frozen=True)
class CredentialReference:
    """A Vault secret reference safe to show in normal command output."""

    role: str
    secret_id: str
    version: int | None = None


@dataclass(frozen=True)
class CredentialResetPlan:
    """Ordered reset contract; executor implementations must be transactional."""

    role: str
    refresh_bindings: tuple[str, ...]
    steps: tuple[str, ...]


def generate_compliant_password(length: int = 24) -> str:
    """Create an Oracle-compatible password without putting it in application state.

    Callers must pass the return value directly to their authorized Vault/DB
    execution boundary and must never log, serialize, or retain it.
    """

    if length < 16:
        raise ValueError("password length must be at least 16")
    required = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice("_#%+"),
    ]
    remaining = [secrets.choice(_PASSWORD_ALPHABET) for _ in range(length - len(required))]
    characters = required + remaining
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


def public_credential_status(references: Iterable[CredentialReference]) -> list[dict[str, object]]:
    """Return references and versions only; secret content is intentionally absent."""

    return [
        {"role": reference.role, "secret_id": reference.secret_id, "version": reference.version}
        for reference in references
    ]


def build_reset_plan(role: str) -> CredentialResetPlan:
    """Describe the all-or-report-remediation reset sequence for one role."""

    if role not in DEMO_DATABASE_ROLES:
        raise ValueError(f"unsupported demo credential role: {role}")
    return CredentialResetPlan(
        role=role,
        refresh_bindings=("dbm", "opsi", "datasafe"),
        steps=(
            "generate password in process memory",
            "alter exactly one database account",
            "create a new Vault secret version",
            "refresh affected service credential bindings",
            "verify health or report remediation state",
        ),
    )
