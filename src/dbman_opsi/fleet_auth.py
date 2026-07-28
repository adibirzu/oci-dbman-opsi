"""Explicit OCI authentication selection for lifecycle commands.

The object deliberately holds references and modes only; it never reads or
prints tokens, private keys, or other credential material.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AuthMode(str, Enum):
    API_KEY = "api-key"
    SECURITY_TOKEN = "security-token"
    INSTANCE_PRINCIPAL = "instance-principal"
    RESOURCE_PRINCIPAL = "resource-principal"


@dataclass(frozen=True)
class OciAuth:
    mode: AuthMode = AuthMode.API_KEY
    profile: str = "DEFAULT"

    def __post_init__(self) -> None:
        if self.mode in (AuthMode.API_KEY, AuthMode.SECURITY_TOKEN) and not self.profile:
            raise ValueError("profile is required for API-key and security-token authentication")

    def cli_args(self, *, region: str) -> list[str]:
        if self.mode is AuthMode.API_KEY:
            return ["oci", "--profile", self.profile, "--region", region]
        if self.mode is AuthMode.SECURITY_TOKEN:
            return ["oci", "--profile", self.profile, "--region", region, "--auth", "security_token"]
        return ["oci", "--region", region, "--auth", self.mode.value.replace("-", "_")]
