"""Interactive planning wizard."""

from __future__ import annotations

import re
from collections.abc import Callable

from dbman_opsi.config import (
    DEFAULT_SERVICES,
    EnablementConfig,
    NetworkSelection,
    Service,
    Target,
    VaultSelection,
)
from dbman_opsi.conn import service_name_from_record
from dbman_opsi.oci_cli import OciCli

_VALID_SERVICES = ("dbm", "opsi", "datasafe")
_DEFAULT_POLICY_GROUP = "dbman-opsi-admins"
_REQUIRED_POLICY_NEEDLES = ("service dpd", "service operations-insights")


def _ask_services() -> tuple[Service, ...]:
    """Prompt for which observability/security/management pillars to enable.

    'dbm' = Database Management, 'opsi' = Operations Insights, 'datasafe' = Data
    Safe. Defaults to DBM + OPSI; Data Safe (security) is opt-in.
    """

    raw = _ask(
        "Enable which pillars? comma-separated from dbm,opsi,datasafe",
        ",".join(DEFAULT_SERVICES),
    )
    chosen = tuple(s.strip().lower() for s in raw.split(",") if s.strip() in _VALID_SERVICES)
    return chosen or DEFAULT_SERVICES  # type: ignore[return-value]


def _safe_discover(
    description: str, callback: Callable[[], list[dict[str, object]]]
) -> list[dict[str, object]]:
    try:
        return callback()
    except Exception as exc:
        print(f"Could not discover {description}: {exc}")
        return []


def _active(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """Keep resources a user can reasonably select in an interactive wizard."""

    terminal = {"DELETED", "TERMINATED", "TERMINATING"}
    return [item for item in items if str(item.get("lifecycle-state", "")).upper() not in terminal]


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or (default or "")


def _ask_bool(prompt: str, default: bool = False) -> bool:
    value = _ask(prompt, "yes" if default else "no").lower()
    return value in {"y", "yes", "true", "1"}


def _label(item: dict[str, object]) -> str:
    name = item.get("display-name") or item.get("name") or item.get("secret-name") or item.get("db-name") or "unnamed"
    lifecycle = item.get("lifecycle-state") or item.get("status") or ""
    identifier = item.get("id") or ""
    short_id = f"...{str(identifier)[-12:]}" if identifier else ""
    extra = item.get("_extra") or ""
    return f"{name} {lifecycle} {extra} {short_id}".strip()


def _append_extra(item: dict[str, object], extra: str) -> dict[str, object]:
    current = str(item.get("_extra") or "")
    return {**item, "_extra": f"{current} {extra}".strip()}


def _search_scope(
    selected: dict[str, object] | None,
    selected_id: str,
    compartments: list[dict[str, object]],
) -> list[dict[str, object]]:
    primary = selected or {"id": selected_id, "name": "selected"}
    primary_id = str(primary.get("id"))
    rest = [c for c in _active(compartments) if str(c.get("id")) != primary_id]
    return [primary, *rest]


def _discover_across_compartments(
    description: str,
    compartments: list[dict[str, object]],
    callback: Callable[[str], list[dict[str, object]]],
) -> list[dict[str, object]]:
    discovered: list[dict[str, object]] = []
    for compartment in compartments:
        compartment_id = str(compartment.get("id") or "")
        compartment_name = str(compartment.get("name") or compartment.get("display-name") or compartment_id)
        for item in _safe_discover(f"{description} in {compartment_name}", lambda cid=compartment_id: callback(cid)):
            discovered.append(
                _append_extra(
                    {**item, "_compartment_id": compartment_id, "_compartment_name": compartment_name},
                    f"(compartment: {compartment_name})",
                )
            )
    return discovered


def _dedupe_scope(compartments: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[str] = set()
    result: list[dict[str, object]] = []
    for compartment in compartments:
        compartment_id = str(compartment.get("id") or "")
        if not compartment_id or compartment_id in seen:
            continue
        seen.add(compartment_id)
        result.append(compartment)
    return result


def _discover_policy_group(
    oci: OciCli | None,
    tenancy_id: str,
    search_compartments: list[dict[str, object]],
) -> str:
    if not oci:
        return _DEFAULT_POLICY_GROUP
    scope = _dedupe_scope([{"id": tenancy_id, "name": "tenancy"}, *search_compartments])
    policies = _discover_across_compartments("IAM policies", scope, lambda cid: oci.list_policies(cid))
    statements = "\n".join(
        str(statement)
        for policy in policies
        for statement in policy.get("statements", [])
    )
    lowered = statements.lower()
    missing = [needle for needle in _REQUIRED_POLICY_NEEDLES if needle not in lowered]
    if not policies:
        print("IAM policies: none discovered in tenancy/accessible compartments")
    elif not missing:
        print("IAM policies: required DBM/OPSI service-principal statements discovered")
    else:
        print(f"IAM policies: missing service-principal statements for {', '.join(missing)}")
    groups = _policy_groups_from_statements(oci, statements)
    if groups:
        print(f"IAM policy groups discovered: {', '.join(_policy_group_display(group) for group in groups)}")
        return groups[0]
    return _DEFAULT_POLICY_GROUP


def _policy_group_display(group: str) -> str:
    if group.startswith("id ocid1.group"):
        return f"id ...{group[-12:]}"
    return group


def _policy_groups_from_statements(oci: OciCli, statements: str) -> list[str]:
    named_groups = {
        group.strip().strip("'\"")
        for group in re.findall(r"allow\s+group\s+(?!id\s)(.+?)\s+to\s+", statements, flags=re.IGNORECASE)
        if "/" not in group
    }
    resolved_ids: set[str] = set()
    for group_id in re.findall(r"allow\s+group\s+id\s+(ocid1\.group[^\s]+)\s+to\s+", statements, flags=re.IGNORECASE):
        try:
            group = oci.get_group(group_id)
        except Exception:  # noqa: BLE001 - keep valid policy syntax as fallback
            resolved_ids.add(f"id {group_id}")
            continue
        name = str(group.get("name") or group.get("display-name") or "").strip()
        resolved_ids.add(name or f"id {group_id}")
    return sorted(named_groups | resolved_ids)


def _select(prompt: str, items: list[dict[str, object]]) -> dict[str, object] | None:
    choices = _active(items)
    if not choices:
        return None
    print(prompt)
    for index, item in enumerate(choices, start=1):
        print(f"  {index}. {_label(item)}")
    while True:
        value = _ask("Select number or leave blank for manual entry")
        if not value:
            return None
        normalized = value.lstrip("\\")
        if not normalized.isdigit():
            print("Invalid selection. Enter a number from the list, or leave blank for manual entry.")
            continue
        selected_index = int(normalized) - 1
        if 0 <= selected_index < len(choices):
            return choices[selected_index]
        print("Selection out of range. Enter a number from the list, or leave blank for manual entry.")


def _select_id(prompt: str, items: list[dict[str, object]], manual_prompt: str) -> str:
    selected = _select(prompt, items)
    return str(selected.get("id")) if selected and selected.get("id") else _ask(manual_prompt)


def _service_name(record: dict[str, object], fallback: str = "ORCLPDB1") -> str:
    # Canonical connection-string parse lives in conn.service_name_from_record;
    # the wizard adds a name-field fallback so it always yields a usable default.
    return service_name_from_record(record) or str(
        record.get("pdb-name") or record.get("db-name") or record.get("display-name") or fallback
    )


def _target_name(record: dict[str, object], fallback: str) -> str:
    return str(record.get("pdb-name") or record.get("display-name") or record.get("name") or record.get("db-name") or fallback)


def _pdb_parent_id(record: dict[str, object]) -> str | None:
    for key in ("container-database-id", "database-id", "cdb-id", "parent-database-id"):
        value = record.get(key)
        if value:
            return str(value)
    return None


def _discover_cloud_databases(compartments: list[dict[str, object]], oci: OciCli) -> list[dict[str, object]]:
    discovered: list[dict[str, object]] = []
    for compartment in compartments:
        compartment_id = str(compartment.get("id") or "")
        compartment_name = str(compartment.get("name") or compartment.get("display-name") or compartment_id)
        for system in _safe_discover("DB systems", lambda cid=compartment_id: oci.list_db_systems(cid)):
            system_id = str(system.get("id") or "")
            system_name = str(system.get("display-name") or system.get("name") or "")
            for database in _safe_discover(
                f"databases in {system_name or system_id}",
                lambda cid=compartment_id, sid=system_id: oci.list_databases(cid, sid),
            ):
                discovered.append(
                    _append_extra(
                        {
                            **database,
                            "_compartment_id": compartment_id,
                            "_compartment_name": compartment_name,
                            "_db_system_id": system_id,
                            "_database_role": "CDB",
                            "_default_service_name": _service_name(database),
                        },
                        f"(compartment: {compartment_name}; DB system: {system_name})"
                        if system_name
                        else f"(compartment: {compartment_name})",
                    )
                )
    return discovered


def _discover_target_choices(kind: str, compartments: list[dict[str, object]], oci: OciCli) -> list[dict[str, object]]:
    if kind in {"dbcs", "exadata"}:
        return _discover_cloud_databases(compartments, oci)
    if kind == "autonomous":
        return _discover_across_compartments(
            "Autonomous Databases", compartments, lambda cid: oci.list_autonomous_databases(cid)
        )
    return []


def _list_optional(
    oci: OciCli | None, description: str, callback: Callable[[], list[dict[str, object]]]
) -> list[dict[str, object]]:
    return _safe_discover(description, callback) if oci else []


def _discover_pdb_targets(cdb: Target, compartment_id: str, oci: OciCli) -> list[Target]:
    """Offer to add the CDB's pluggable databases as PDB targets.

    PDB targets inherit the CDB's private endpoint, Vault secret, and monitoring
    user, and link back to the parent via parent_cdb_id so enablement can order
    the container database first.
    """

    if not _ask_bool("Discover pluggable databases (PDBs) for this CDB?", False):
        return []
    discovered = _safe_discover("pluggable databases", lambda: oci.list_pluggable_databases(compartment_id))
    linked = [pdb for pdb in discovered if _pdb_parent_id(pdb) == cdb.resource_id]
    # Some OCI list payloads omit parent metadata; in that case preserve manual
    # compatibility and show the full compartment list.
    pdbs = linked if linked else discovered
    targets: list[Target] = []
    for pdb in pdbs:
        pdb_name = str(pdb.get("pdb-name") or pdb.get("display-name") or "pdb")
        if not _ask_bool(f"Add PDB '{pdb_name}' as a target?", True):
            continue
        targets.append(
            Target(
                kind=cdb.kind,
                name=f"{cdb.name}-{pdb_name}",
                compartment_id=compartment_id,
                resource_id=str(pdb.get("id")) if pdb.get("id") else None,
                service_name=pdb_name,
                monitoring_user=cdb.monitoring_user,
                password_secret_id=cdb.password_secret_id,
                private_endpoint_id=cdb.private_endpoint_id,
                opsi_private_endpoint_id=cdb.opsi_private_endpoint_id,
                # Inherit the parent's pillar selection, DB system, and Data Safe PE
                # so PDB targets register the same services as their CDB.
                db_system_id=cdb.db_system_id,
                data_safe_private_endpoint_id=cdb.data_safe_private_endpoint_id,
                services=cdb.services,
                database_role="PDB",
                parent_cdb_id=cdb.resource_id,
            )
        )
    return targets


def run_wizard(profile: str, region: str, oci: OciCli | None = None) -> EnablementConfig:
    discovered_tenancy = None
    if oci and hasattr(oci, "profile_tenancy"):
        try:
            discovered_tenancy = oci.profile_tenancy()
        except Exception as exc:  # noqa: BLE001 - keep manual tenancy entry as fallback
            print(f"Could not discover profile tenancy: {exc}")
    if discovered_tenancy:
        tenancy_id = str(discovered_tenancy)
        print("Tenancy OCID: using OCI profile tenancy")
    else:
        tenancy_id = _ask("Tenancy OCID")
    compartments = _list_optional(oci, "compartments", lambda: oci.list_compartments(tenancy_id))
    selected_compartment = _select("Accessible compartments:", compartments)
    compartment_id = str(selected_compartment.get("id")) if selected_compartment else _ask("Target compartment OCID")
    search_compartments = _search_scope(selected_compartment, compartment_id, compartments)
    policy_group_name = _discover_policy_group(oci, tenancy_id, search_compartments)
    existing_vcns = _discover_across_compartments("VCNs", search_compartments, lambda cid: oci.list_vcns(cid)) if oci else []
    create_network = _ask_bool("Create a PoC VCN/subnet?", not bool(_active(existing_vcns)))
    vcn_id = None
    subnet_id = None
    if not create_network:
        selected_vcn = _select("Available VCNs:", existing_vcns)
        vcn_id = str(selected_vcn.get("id")) if selected_vcn and selected_vcn.get("id") else _ask("Existing VCN OCID")
        vcn_compartment_id = str((selected_vcn or {}).get("_compartment_id") or compartment_id)
        subnets = _list_optional(oci, "subnets", lambda: oci.list_subnets(vcn_compartment_id, vcn_id))
        subnet_id = _select_id("Available subnets:", subnets, "Existing private subnet OCID")
    network = NetworkSelection(
        create_test_network=create_network,
        vcn_id=vcn_id,
        subnet_id=subnet_id,
    )
    create_vault = _ask_bool("Create a PoC Vault/key?", False)
    vault_id = None
    key_id = None
    if not create_vault:
        vaults = _discover_across_compartments("Vaults", search_compartments, lambda cid: oci.list_vaults(cid)) if oci else []
        selected_vault = _select("Available Vaults:", vaults)
        vault_id = str(selected_vault.get("id")) if selected_vault else _ask("Existing Vault OCID")
        vault_compartment_id = str((selected_vault or {}).get("_compartment_id") or compartment_id)
        endpoint = str(selected_vault.get("management-endpoint") or "") if selected_vault else ""
        keys = _list_optional(oci, "Vault keys", lambda: oci.list_keys(vault_compartment_id, endpoint)) if endpoint else []
        key_id = _select_id("Available Vault keys:", keys, "Existing Key OCID")
    vault = VaultSelection(
        create_vault=create_vault,
        vault_id=vault_id,
        key_id=key_id,
    )
    targets: list[Target] = []
    while _ask_bool("Add a target?", len(targets) == 0):
        kind = _ask("Target kind (dbcs/autonomous/exadata/external-db/external-exadata)", "dbcs")
        provision = _ask_bool("Provision this target from zero?", False)
        discovered = _discover_target_choices(kind, search_compartments, oci) if not provision and oci else []
        selected_target = _select("Discovered matching targets:", discovered)
        default_name = _target_name(selected_target or {}, kind)
        name = _ask("Target display name", default_name)
        resource_id = None if provision else str(selected_target.get("id")) if selected_target else _ask("Existing database/resource OCID")
        target_compartment_id = str((selected_target or {}).get("_compartment_id") or compartment_id)
        service_name = (
            None
            if kind == "autonomous"
            else _ask("Database service name", str((selected_target or {}).get("_default_service_name") or "ORCLPDB1"))
        )
        monitoring_user = _ask("Monitoring username", "DBSNMP")
        password_secret_id = ""
        private_endpoint_id = ""
        opsi_private_endpoint_id = ""
        if kind in {"dbcs", "exadata"}:
            secrets = _discover_across_compartments("Vault secrets", search_compartments, lambda cid: oci.list_secrets(cid)) if oci else []
            password_secret_id = _select_id(
                "Available password secrets:",
                secrets,
                "Password secret OCID (leave blank if provision step will create it)",
            )
            dbm_private_endpoints = (
                _discover_across_compartments(
                    "DB Management private endpoints",
                    search_compartments,
                    lambda cid: oci.list_db_management_private_endpoints(cid),
                )
                if oci
                else []
            )
            private_endpoint_id = _select_id(
                "Available DB Management private endpoints:",
                dbm_private_endpoints,
                "DB Management private endpoint OCID (leave blank if provision step will create it)",
            )
            opsi_private_endpoints = (
                _discover_across_compartments(
                    "Ops Insights private endpoints",
                    search_compartments,
                    lambda cid: oci.list_opsi_private_endpoints(cid),
                )
                if oci
                else []
            )
            opsi_private_endpoint_id = _select_id(
                "Available Ops Insights private endpoints:",
                opsi_private_endpoints,
                "Ops Insights private endpoint OCID (leave blank if provision step will create it)",
            )
        services = _ask_services()
        db_system_id = None
        database_role = "CDB"
        if kind in {"dbcs", "exadata"} and selected_target:
            db_system_id = str(selected_target.get("_db_system_id") or "")
            database_role = str(selected_target.get("_database_role") or "CDB")
        data_safe_private_endpoints = (
            _discover_across_compartments(
                "Data Safe private endpoints",
                search_compartments,
                lambda cid: oci.list_data_safe_private_endpoints(cid),
            )
            if oci
            else []
        )
        data_safe_private_endpoint_id = (
            _select_id(
                "Available Data Safe private endpoints:",
                data_safe_private_endpoints,
                "Data Safe private endpoint OCID (leave blank to create during enable)",
            )
            if "datasafe" in services
            else ""
        )
        external_os = None
        external_host = None
        if kind.startswith("external"):
            external_host = _ask("External database host")
            external_os = _ask("External host OS (linux/windows/solaris/aix)", "linux")
        target = Target(
            kind=kind,  # type: ignore[arg-type]
            name=name,
            compartment_id=target_compartment_id,
            resource_id=resource_id or None,
            service_name=service_name or None,
            monitoring_user=monitoring_user or None,
            password_secret_id=password_secret_id or None,
            private_endpoint_id=private_endpoint_id or None,
            opsi_private_endpoint_id=opsi_private_endpoint_id or None,
            db_system_id=db_system_id,
            data_safe_private_endpoint_id=data_safe_private_endpoint_id or None,
            services=services,
            database_role=database_role,  # type: ignore[arg-type]
            provision=provision,
            external_host=external_host or None,
            external_os=external_os or None,  # type: ignore[arg-type]
        )
        targets.append(target)
        if kind in {"dbcs", "exadata"} and not provision and oci:
            targets.extend(_discover_pdb_targets(target, target_compartment_id, oci))
    return EnablementConfig(
        profile=profile,
        region=region,
        tenancy_id=tenancy_id,
        compartment_id=compartment_id,
        network=network,
        vault=vault,
        targets=tuple(targets),
        policy_group_name=policy_group_name,
        dry_run=True,
    )
