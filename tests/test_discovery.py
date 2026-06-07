from dbman_opsi.discovery import DiscoveryService


class FakeOci:
    def list_vcns(self, compartment_id):
        return [{"id": "vcn-1"}]

    def list_service_gateways(self, compartment_id, vcn_id):
        return [{"lifecycle-state": "AVAILABLE"}] if vcn_id == "vcn-1" else []

    def list_subnets(self, compartment_id, vcn_id):
        return [{"id": "subnet-1", "display-name": "private", "prohibit-public-ip-on-vnic": True}]

    def list_vaults(self, compartment_id):
        return [{"id": "vault-1", "display-name": "v", "lifecycle-state": "ACTIVE", "management-endpoint": "https://kms"}]

    def list_keys(self, compartment_id, management_endpoint):
        return [{"id": "key-1", "display-name": "k"}]

    def list_db_systems(self, compartment_id):
        return [{"id": "dbsys-1"}]

    def list_databases(self, compartment_id, db_system_id):
        return [{"id": "cdb-1", "db-name": "DB0424", "lifecycle-state": "AVAILABLE",
                 "database-management-config": None}]

    def list_pluggable_databases(self, compartment_id):
        return [{"id": "pdb-1", "pdb-name": "test", "lifecycle-state": "AVAILABLE",
                 "pluggable-database-management-config": {"management-status": "ENABLED"}}]

    def list_autonomous_databases(self, compartment_id):
        return [{"id": "adb-1", "display-name": "adb", "lifecycle-state": "AVAILABLE",
                 "database-management-status": "NOT_ENABLED"}]

    def list_db_management_private_endpoints(self, compartment_id):
        return [{"name": "dbm-pe"}]

    def list_opsi_private_endpoints(self, compartment_id):
        return [{"display-name": "opsi-pe"}]

    def list_data_safe_private_endpoints(self, compartment_id):
        return [{"display-name": "ds-pe"}]

    def list_opsi_database_insights(self, compartment_id):
        # OPSI insight references the CDB by database-id, ACTIVE.
        return [{"id": "insight-1", "database-id": "cdb-1", "lifecycle-state": "ACTIVE"}]

    def list_data_safe_targets(self, compartment_id):
        # Data Safe target references the parent DB system, ACTIVE.
        return [{"id": "dstarget-1", "lifecycle-state": "ACTIVE",
                 "database-details": {"db-system-id": "dbsys-1"}}]

    def list_management_agents(self, compartment_id):
        return [{"display-name": "agent-1"}]

    def list_bastions(self, compartment_id):
        return [{"name": "bastion-1"}]


def test_discovery_builds_inventory() -> None:
    inventory = DiscoveryService(FakeOci()).discover([{"id": "cmpt-1", "name": "demo-database"}])  # type: ignore[arg-type]

    compartment = inventory.compartments[0]
    assert compartment.subnets[0].has_service_gateway is True
    assert compartment.subnets[0].private is True
    assert compartment.vaults[0].keys == (("key-1", "k"),)
    roles = {db.role for db in compartment.databases}
    assert roles == {"CDB", "PDB"}
    pdb = next(db for db in compartment.databases if db.role == "PDB")
    assert pdb.dbm_status == "ENABLED"
    cdb = next(db for db in compartment.databases if db.role == "CDB")
    assert cdb.dbm_status == "NOT_ENABLED"
    assert compartment.bastions == ("bastion-1",)
    assert compartment.data_safe_private_endpoints == ({"display-name": "ds-pe"},)


def test_discovery_detects_three_pillars_per_db() -> None:
    inventory = DiscoveryService(FakeOci()).discover([{"id": "cmpt-1", "name": "demo-database"}])  # type: ignore[arg-type]

    compartment = inventory.compartments[0]
    cdb = next(db for db in compartment.databases if db.role == "CDB")
    # CDB: OPSI insight matches by database-id, Data Safe matches by db-system-id.
    assert cdb.opsi_status == "ENABLED"
    assert cdb.data_safe_status == "ENABLED"
    assert set(cdb.enabled_services) == {"opsi", "datasafe"}
    assert "dbm" in cdb.missing_services
    pdb = next(db for db in compartment.databases if db.role == "PDB")
    # PDB: DBM enabled, but no OPSI insight / Data Safe target references it.
    assert pdb.enabled_services == ("dbm",)
    assert set(pdb.missing_services) == {"opsi", "datasafe"}
    # to_dict carries the new fields for reporting.
    assert cdb.to_dict()["data_safe_status"] == "ENABLED"


def test_discovery_to_dict_skips_empty() -> None:
    class Empty(FakeOci):
        def list_vcns(self, compartment_id):
            return []

        def list_vaults(self, compartment_id):
            return []

        def list_db_systems(self, compartment_id):
            return []

        def list_pluggable_databases(self, compartment_id):
            return []

        def list_autonomous_databases(self, compartment_id):
            return []

        def list_db_management_private_endpoints(self, compartment_id):
            return []

        def list_opsi_private_endpoints(self, compartment_id):
            return []

        def list_data_safe_private_endpoints(self, compartment_id):
            return []

        def list_management_agents(self, compartment_id):
            return []

        def list_bastions(self, compartment_id):
            return []

    inventory = DiscoveryService(Empty()).discover([{"id": "c", "name": "empty"}])  # type: ignore[arg-type]
    assert inventory.to_dict() == {"compartments": []}
