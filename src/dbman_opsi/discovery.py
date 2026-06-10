"""Read-only inventory of resources reusable for enablement.

Answers "what already exists that I can reuse?" before provisioning anything:
subnets (and whether they can reach OCI services), vaults and keys, databases
(CDB and PDB), autonomous databases, existing private endpoints, Management
Agents, and bastions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from dbman_opsi.conn import service_name_from_record
from dbman_opsi.oci_cli import OciCli
from dbman_opsi.status import data_safe_status, dbm_status, opsi_insight_status


def _safe(call: Callable[[], Any], default: Any, attempts: int = 2) -> Any:
    """Run a read-only lookup, retrying once to ride out transient OCI 404/5xx blips."""

    for attempt in range(attempts):
        try:
            return call()
        except Exception:  # noqa: BLE001 - discovery is best-effort and read-only
            if attempt == attempts - 1:
                return default
    return default


@dataclass(frozen=True)
class SubnetInfo:
    id: str
    name: str
    vcn_id: str
    private: bool
    has_service_gateway: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "vcn_id": self.vcn_id,
            "private": self.private,
            "has_service_gateway": self.has_service_gateway,
        }


@dataclass(frozen=True)
class VaultInfo:
    id: str
    name: str
    management_endpoint: str
    keys: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "management_endpoint": self.management_endpoint,
            "keys": [{"id": key_id, "name": key_name} for key_id, key_name in self.keys],
        }


@dataclass(frozen=True)
class DatabaseInfo:
    id: str
    name: str
    role: str
    state: str
    dbm_status: str
    opsi_status: str = "NOT_ENABLED"
    data_safe_status: str = "NOT_ENABLED"

    @property
    def enabled_services(self) -> tuple[str, ...]:
        """Pillars already on for this DB ('dbm', 'opsi', 'datasafe')."""

        from dbman_opsi.status import is_enabled

        result: list[str] = []
        if is_enabled(self.dbm_status):
            result.append("dbm")
        if str(self.opsi_status).upper() == "ENABLED":
            result.append("opsi")
        if str(self.data_safe_status).upper() == "ENABLED":
            result.append("datasafe")
        return tuple(result)

    @property
    def missing_services(self) -> tuple[str, ...]:
        return tuple(s for s in ("dbm", "opsi", "datasafe") if s not in self.enabled_services)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "state": self.state,
            "dbm_status": self.dbm_status,
            "opsi_status": self.opsi_status,
            "data_safe_status": self.data_safe_status,
            "enabled_services": list(self.enabled_services),
            "missing_services": list(self.missing_services),
        }


@dataclass(frozen=True)
class CompartmentInventory:
    name: str
    id: str
    subnets: tuple[SubnetInfo, ...] = ()
    vaults: tuple[VaultInfo, ...] = ()
    databases: tuple[DatabaseInfo, ...] = ()
    autonomous: tuple[DatabaseInfo, ...] = ()
    dbm_private_endpoints: tuple[dict[str, Any], ...] = ()
    opsi_private_endpoints: tuple[dict[str, Any], ...] = ()
    data_safe_private_endpoints: tuple[dict[str, Any], ...] = ()
    management_agents: tuple[str, ...] = ()
    bastions: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not any(
            (self.subnets, self.vaults, self.databases, self.autonomous,
             self.dbm_private_endpoints, self.opsi_private_endpoints,
             self.data_safe_private_endpoints,
             self.management_agents, self.bastions)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "id": self.id,
            "subnets": [item.to_dict() for item in self.subnets],
            "vaults": [item.to_dict() for item in self.vaults],
            "databases": [item.to_dict() for item in self.databases],
            "autonomous": [item.to_dict() for item in self.autonomous],
            "dbm_private_endpoints": [pe.get("name") or pe.get("display-name") for pe in self.dbm_private_endpoints],
            "opsi_private_endpoints": [pe.get("display-name") for pe in self.opsi_private_endpoints],
            "data_safe_private_endpoints": [pe.get("display-name") for pe in self.data_safe_private_endpoints],
            "management_agents": list(self.management_agents),
            "bastions": list(self.bastions),
        }


@dataclass(frozen=True)
class Inventory:
    compartments: tuple[CompartmentInventory, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {"compartments": [c.to_dict() for c in self.compartments if not c.is_empty]}


class DiscoveryService:
    def __init__(self, oci: OciCli) -> None:
        self.oci = oci

    def discover(self, compartments: list[dict[str, Any]]) -> Inventory:
        return Inventory(compartments=tuple(self._compartment(c) for c in compartments))

    def _compartment(self, compartment: dict[str, Any]) -> CompartmentInventory:
        cid = str(compartment.get("id"))
        # Pre-fetch the standalone OPSI insight and Data Safe target collections
        # once, then join them back to each DB by OCID (avoids an N+1 lookup per
        # database). Best-effort: an empty read just yields NOT_ENABLED statuses.
        insights = _safe(lambda: self.oci.list_opsi_database_insights(cid), [])
        data_safe_targets = self._data_safe_targets_enriched(cid)
        return CompartmentInventory(
            name=str(compartment.get("name", "")),
            id=cid,
            subnets=self._subnets(cid),
            vaults=self._vaults(cid),
            databases=self._databases(cid, insights, data_safe_targets),
            autonomous=self._autonomous(cid, insights, data_safe_targets),
            dbm_private_endpoints=tuple(_safe(lambda: self.oci.list_db_management_private_endpoints(cid), [])),
            opsi_private_endpoints=tuple(_safe(lambda: self.oci.list_opsi_private_endpoints(cid), [])),
            data_safe_private_endpoints=tuple(_safe(lambda: self.oci.list_data_safe_private_endpoints(cid), [])),
            management_agents=tuple(
                str(a.get("display-name", "")) for a in _safe(lambda: self.oci.list_management_agents(cid), [])
            ),
            bastions=tuple(str(b.get("name", "")) for b in _safe(lambda: self.oci.list_bastions(cid), [])),
        )

    def _subnets(self, cid: str) -> tuple[SubnetInfo, ...]:
        vcns = _safe(lambda: self.oci.list_vcns(cid), [])
        vcns_with_sgw = {
            str(vcn.get("id"))
            for vcn in vcns
            if any(
                str(gw.get("lifecycle-state", "")).upper() == "AVAILABLE"
                for gw in _safe(lambda v=vcn: self.oci.list_service_gateways(cid, str(v.get("id"))), [])
            )
        }
        subnets: list[SubnetInfo] = []
        for vcn in vcns:
            vcn_id = str(vcn.get("id"))
            for subnet in _safe(lambda v=vcn_id: self.oci.list_subnets(cid, v), []):
                subnets.append(
                    SubnetInfo(
                        id=str(subnet.get("id")),
                        name=str(subnet.get("display-name", "")),
                        vcn_id=vcn_id,
                        private=bool(subnet.get("prohibit-public-ip-on-vnic")),
                        has_service_gateway=vcn_id in vcns_with_sgw,
                    )
                )
        return tuple(subnets)

    def _vaults(self, cid: str) -> tuple[VaultInfo, ...]:
        vaults: list[VaultInfo] = []
        for vault in _safe(lambda: self.oci.list_vaults(cid), []):
            if str(vault.get("lifecycle-state", "")).upper() != "ACTIVE":
                continue
            endpoint = str(vault.get("management-endpoint", ""))
            keys = _safe(lambda: self.oci.list_keys(cid, endpoint), []) if endpoint else []
            vaults.append(
                VaultInfo(
                    id=str(vault.get("id")),
                    name=str(vault.get("display-name", "")),
                    management_endpoint=endpoint,
                    keys=tuple((str(k.get("id")), str(k.get("display-name", ""))) for k in keys),
                )
            )
        return tuple(vaults)

    def _data_safe_targets_enriched(self, cid: str) -> list[dict[str, Any]]:
        """List Data Safe targets, enriching each with GET database-details.

        The ``target-database list`` summary has ``database-details = null``; the
        service-name needed to attribute a Base DB target to a specific PDB (vs
        the CDB root) only appears on GET. Best-effort: a failed GET leaves the
        summary as-is (callers fall back to the coarse DB-system match).
        """

        targets = _safe(lambda: self.oci.list_data_safe_targets(cid), [])
        enriched: list[dict[str, Any]] = []
        for target in targets:
            target_id = str(target.get("id"))
            full = _safe(lambda tid=target_id: self.oci.get_data_safe_target(tid), {})
            if isinstance(full, dict) and full.get("database-details"):
                enriched.append({**target, "database-details": full.get("database-details")})
            else:
                enriched.append(target)
        return enriched

    _service_name = staticmethod(service_name_from_record)

    def _databases(
        self,
        cid: str,
        insights: list[dict[str, Any]],
        data_safe_targets: list[dict[str, Any]],
    ) -> tuple[DatabaseInfo, ...]:
        databases: list[DatabaseInfo] = []
        for system in _safe(lambda: self.oci.list_db_systems(cid), []):
            system_id = str(system.get("id"))
            for database in _safe(lambda s=system: self.oci.list_databases(cid, str(s.get("id"))), []):
                databases.append(
                    self._database_info(database, "CDB", insights, data_safe_targets, db_system_id=system_id)
                )
        for pdb in _safe(lambda: self.oci.list_pluggable_databases(cid), []):
            databases.append(self._database_info(pdb, "PDB", insights, data_safe_targets, name_key="pdb-name"))
        return tuple(databases)

    def _autonomous(
        self,
        cid: str,
        insights: list[dict[str, Any]],
        data_safe_targets: list[dict[str, Any]],
    ) -> tuple[DatabaseInfo, ...]:
        return tuple(
            self._database_info(
                adb, "AUTONOMOUS", insights, data_safe_targets, name_key="display-name", kind="autonomous"
            )
            for adb in _safe(lambda: self.oci.list_autonomous_databases(cid), [])
        )

    @staticmethod
    def _database_info(
        record: dict[str, Any],
        role: str,
        insights: list[dict[str, Any]],
        data_safe_targets: list[dict[str, Any]],
        name_key: str = "db-name",
        kind: str = "dbcs",
        db_system_id: str | None = None,
    ) -> DatabaseInfo:
        status_role = "PDB" if role == "PDB" else "CDB"
        db_id = str(record.get("id"))
        return DatabaseInfo(
            id=db_id,
            name=str(record.get(name_key) or record.get("display-name") or ""),
            role=role,
            state=str(record.get("lifecycle-state", "")),
            dbm_status=str(dbm_status(record, kind, status_role) or "NOT_ENABLED"),
            opsi_status=opsi_insight_status(insights, db_id),
            data_safe_status=data_safe_status(
                data_safe_targets, db_id, db_system_id, DiscoveryService._service_name(record)
            ),
        )
