"""Configuration model and YAML/JSON serialization."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from dbman_opsi.redact import redact_data

TargetKind = Literal["dbcs", "autonomous", "exadata", "external-db", "external-exadata"]


@dataclass(frozen=True)
class NetworkSelection:
    vcn_id: str | None = None
    subnet_id: str | None = None
    create_test_network: bool = False
    cidr_block: str = "10.44.0.0/16"
    subnet_cidr_block: str = "10.44.10.0/24"


@dataclass(frozen=True)
class VaultSelection:
    vault_id: str | None = None
    key_id: str | None = None
    create_vault: bool = False


@dataclass(frozen=True)
class Target:
    kind: TargetKind
    name: str
    compartment_id: str | None = None
    resource_id: str | None = None
    service_name: str | None = None
    monitoring_user: str | None = None
    password_secret_id: str | None = None
    wallet_secret_id: str | None = None
    private_endpoint_id: str | None = None
    opsi_private_endpoint_id: str | None = None
    opsi_database_insight_id: str | None = None
    opsi_connection_details_file: str | None = None
    opsi_credential_details_file: str | None = None
    management_agent_id: str | None = None
    deployment_type: Literal["VIRTUAL_MACHINE", "BARE_METAL", "EXACC", "EXACS"] = "VIRTUAL_MACHINE"
    database_resource_type: str = "database"
    database_role: Literal["CDB", "PDB", "NON_CDB"] = "CDB"
    parent_cdb_id: str | None = None
    management_type: Literal["BASIC", "ADVANCED"] = "ADVANCED"
    provision: bool = False
    external_host: str | None = None
    external_os: Literal["linux", "windows", "solaris", "aix"] | None = None


@dataclass(frozen=True)
class EnablementConfig:
    profile: str
    region: str
    tenancy_id: str | None = None
    compartment_id: str | None = None
    network: NetworkSelection = field(default_factory=NetworkSelection)
    vault: VaultSelection = field(default_factory=VaultSelection)
    targets: tuple[Target, ...] = ()
    policy_group_name: str = "dbman-opsi-admins"
    dry_run: bool = True
    terraform_dir: str = "terraform/examples/zero-start-poc"

    def sanitized(self) -> dict[str, Any]:
        return redact_data(to_dict(self))


def _network_from_dict(value: dict[str, Any] | None) -> NetworkSelection:
    return NetworkSelection(**(value or {}))


def _vault_from_dict(value: dict[str, Any] | None) -> VaultSelection:
    return VaultSelection(**(value or {}))


def _target_from_dict(value: dict[str, Any]) -> Target:
    return Target(**value)


def from_dict(value: dict[str, Any]) -> EnablementConfig:
    """Create an immutable config object from raw dict data."""

    return EnablementConfig(
        profile=value["profile"],
        region=value["region"],
        tenancy_id=value.get("tenancy_id"),
        compartment_id=value.get("compartment_id"),
        network=_network_from_dict(value.get("network")),
        vault=_vault_from_dict(value.get("vault")),
        targets=tuple(_target_from_dict(item) for item in value.get("targets", [])),
        policy_group_name=value.get("policy_group_name", "dbman-opsi-admins"),
        dry_run=bool(value.get("dry_run", True)),
        terraform_dir=value.get("terraform_dir", "terraform/examples/zero-start-poc"),
    )


def to_dict(config: EnablementConfig) -> dict[str, Any]:
    return {
        "profile": config.profile,
        "region": config.region,
        "tenancy_id": config.tenancy_id,
        "compartment_id": config.compartment_id,
        "network": config.network.__dict__,
        "vault": config.vault.__dict__,
        "targets": [target.__dict__ for target in config.targets],
        "policy_group_name": config.policy_group_name,
        "dry_run": config.dry_run,
        "terraform_dir": config.terraform_dir,
    }


def load_config(path: str | Path) -> EnablementConfig:
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw) if str(path).endswith(".json") else yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping")
    return from_dict(data)


def save_config(path: str | Path, config: EnablementConfig) -> None:
    destination = Path(path)
    data = to_dict(config)
    if destination.suffix == ".json":
        destination.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        return
    destination.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
