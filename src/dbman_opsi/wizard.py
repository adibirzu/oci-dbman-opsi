"""Interactive planning wizard."""

from __future__ import annotations

from dbman_opsi.config import EnablementConfig, NetworkSelection, Target, VaultSelection
from dbman_opsi.oci_cli import OciCli


def _safe_discover(description: str, callback) -> list[dict[str, object]]:
    try:
        return callback()
    except Exception as exc:
        print(f"Could not discover {description}: {exc}")
        return []


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or (default or "")


def _ask_bool(prompt: str, default: bool = False) -> bool:
    value = _ask(prompt, "yes" if default else "no").lower()
    return value in {"y", "yes", "true", "1"}


def _label(item: dict[str, object]) -> str:
    name = item.get("display-name") or item.get("name") or item.get("db-name") or "unnamed"
    lifecycle = item.get("lifecycle-state") or item.get("status") or ""
    identifier = item.get("id") or ""
    return f"{name} {lifecycle} {identifier}".strip()


def _select(prompt: str, items: list[dict[str, object]]) -> dict[str, object] | None:
    if not items:
        return None
    print(prompt)
    for index, item in enumerate(items, start=1):
        print(f"  {index}. {_label(item)}")
    value = _ask("Select number or leave blank for manual entry")
    if not value:
        return None
    selected_index = int(value) - 1
    if selected_index < 0 or selected_index >= len(items):
        raise ValueError("Selection out of range")
    return items[selected_index]


def _discover_pdb_targets(cdb: Target, compartment_id: str, oci: OciCli) -> list[Target]:
    """Offer to add the CDB's pluggable databases as PDB targets.

    PDB targets inherit the CDB's private endpoint, Vault secret, and monitoring
    user, and link back to the parent via parent_cdb_id so enablement can order
    the container database first.
    """

    if not _ask_bool("Discover pluggable databases (PDBs) for this CDB?", False):
        return []
    pdbs = _safe_discover("pluggable databases", lambda: oci.list_pluggable_databases(compartment_id))
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
                database_role="PDB",
                parent_cdb_id=cdb.resource_id,
            )
        )
    return targets


def run_wizard(profile: str, region: str, oci: OciCli | None = None) -> EnablementConfig:
    tenancy_id = _ask("Tenancy OCID")
    compartments = _safe_discover("compartments", lambda: oci.list_compartments(tenancy_id)) if oci else []
    selected_compartment = _select("Accessible compartments:", compartments)
    compartment_id = str(selected_compartment.get("id")) if selected_compartment else _ask("Target compartment OCID")
    create_network = _ask_bool("Create a PoC VCN/subnet?", True)
    vcn_id = None
    subnet_id = None
    if not create_network:
        vcns = _safe_discover("VCNs", lambda: oci.list_vcns(compartment_id)) if oci else []
        selected_vcn = _select("Available VCNs:", vcns)
        vcn_id = str(selected_vcn.get("id")) if selected_vcn else _ask("Existing VCN OCID")
        subnets = _safe_discover("subnets", lambda: oci.list_subnets(compartment_id, vcn_id)) if oci else []
        selected_subnet = _select("Available subnets:", subnets)
        subnet_id = str(selected_subnet.get("id")) if selected_subnet else _ask("Existing private subnet OCID")
    network = NetworkSelection(
        create_test_network=create_network,
        vcn_id=vcn_id,
        subnet_id=subnet_id,
    )
    create_vault = _ask_bool("Create a PoC Vault/key?", False)
    vault_id = None
    key_id = None
    if not create_vault:
        vaults = _safe_discover("Vaults", lambda: oci.list_vaults(compartment_id)) if oci else []
        selected_vault = _select("Available Vaults:", vaults)
        vault_id = str(selected_vault.get("id")) if selected_vault else _ask("Existing Vault OCID")
        key_id = _ask("Existing Key OCID")
    vault = VaultSelection(
        create_vault=create_vault,
        vault_id=vault_id,
        key_id=key_id,
    )
    targets: list[Target] = []
    while _ask_bool("Add a target?", len(targets) == 0):
        kind = _ask("Target kind (dbcs/autonomous/exadata/external-db/external-exadata)", "dbcs")
        name = _ask("Target display name")
        provision = _ask_bool("Provision this target from zero?", False)
        discovered: list[dict[str, object]] = []
        if not provision and oci:
            if kind == "dbcs":
                discovered = _safe_discover("DB systems", lambda: oci.list_db_systems(compartment_id))
            elif kind == "autonomous":
                discovered = _safe_discover("Autonomous Databases", lambda: oci.list_autonomous_databases(compartment_id))
            elif kind == "exadata":
                discovered = _safe_discover("Exadata infrastructure", lambda: oci.list_exadata_infrastructure(compartment_id))
        selected_target = _select("Discovered matching targets:", discovered)
        resource_id = None if provision else str(selected_target.get("id")) if selected_target else _ask("Existing database/resource OCID")
        service_name = None if kind == "autonomous" else _ask("Database service name", "ORCLPDB1")
        monitoring_user = _ask("Monitoring username", "DBSNMP")
        password_secret_id = _ask("Password secret OCID (leave blank if provision step will create it)")
        private_endpoint_id = _ask("DB Management private endpoint OCID (leave blank if provision step will create it)")
        external_os = None
        external_host = None
        if kind.startswith("external"):
            external_host = _ask("External database host")
            external_os = _ask("External host OS (linux/windows/solaris/aix)", "linux")
        target = Target(
            kind=kind,  # type: ignore[arg-type]
            name=name,
            compartment_id=compartment_id,
            resource_id=resource_id or None,
            service_name=service_name or None,
            monitoring_user=monitoring_user or None,
            password_secret_id=password_secret_id or None,
            private_endpoint_id=private_endpoint_id or None,
            provision=provision,
            external_host=external_host or None,
            external_os=external_os or None,  # type: ignore[arg-type]
        )
        targets.append(target)
        if kind in {"dbcs", "exadata"} and not provision and oci:
            targets.extend(_discover_pdb_targets(target, compartment_id, oci))
    return EnablementConfig(
        profile=profile,
        region=region,
        tenancy_id=tenancy_id,
        compartment_id=compartment_id,
        network=network,
        vault=vault,
        targets=tuple(targets),
        dry_run=True,
    )
