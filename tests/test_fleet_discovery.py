from __future__ import annotations

import pytest

from dbman_opsi.fleet_discovery import DiscoveryScopeError, FleetDiscovery
from dbman_opsi.oci_cli import OciCli
from dbman_opsi.runner import CommandResult


class _MultiRegionOci:
    def __init__(self, region: str = "eu-frankfurt-1") -> None:
        self.region = region

    def profile_tenancy(self) -> str:
        return "tenancy"

    def list_subscribed_regions(self, tenancy_id: str) -> list[dict[str, str]]:
        assert tenancy_id == "tenancy"
        return [{"region-name": "us-ashburn-1"}, {"region-name": "eu-frankfurt-1"}]

    def list_compartments(self, tenancy_id: str) -> list[dict[str, str]]:
        return [{"id": "compartment-b", "name": "B"}, {"id": "compartment-a", "name": "A"}]

    def list_db_systems(self, compartment_id: str) -> list[dict[str, str]]:
        return [{"id": f"system-{compartment_id}", "display-name": "system"}]

    def list_databases(self, compartment_id: str, db_system_id: str) -> list[dict[str, str]]:
        return [
            {
                "id": f"database-{self.region}-{compartment_id}",
                "display-name": "orders",
                "lifecycle-state": "AVAILABLE",
                "freeform-tags": {"team": "database"},
            },
            # OCI can surface the same resource through separate accessible paths.
            {
                "id": f"database-{self.region}-{compartment_id}",
                "display-name": "orders duplicate",
                "lifecycle-state": "AVAILABLE",
            },
        ]

    def list_pluggable_databases(self, compartment_id: str) -> list[dict[str, str]]:
        return []

    def list_autonomous_databases(self, compartment_id: str) -> list[dict[str, str]]:
        return []

    def list_exadata_infrastructure(self, compartment_id: str) -> list[dict[str, str]]:
        return []

    def list_managed_databases(self, compartment_id: str) -> list[dict[str, str]]:
        return []


def test_discovers_all_regions_and_compartments_with_stable_deduplication() -> None:
    discovery = FleetDiscovery(
        _MultiRegionOci(),
        region_client=lambda region: _MultiRegionOci(region),
        max_workers=4,
    )

    targets = discovery.discover()

    assert [(target.region, target.compartment_id, target.target_id) for target in targets] == [
        ("eu-frankfurt-1", "compartment-a", "database-eu-frankfurt-1-compartment-a"),
        ("eu-frankfurt-1", "compartment-b", "database-eu-frankfurt-1-compartment-b"),
        ("eu-frankfurt-1", "tenancy", "database-eu-frankfurt-1-tenancy"),
        ("us-ashburn-1", "compartment-a", "database-us-ashburn-1-compartment-a"),
        ("us-ashburn-1", "compartment-b", "database-us-ashburn-1-compartment-b"),
        ("us-ashburn-1", "tenancy", "database-us-ashburn-1-tenancy"),
    ]


class _ManyTargetOci:
    region = "eu-frankfurt-1"

    def __init__(self, count: int) -> None:
        self.count = count
        self.compartment_attempts = 0

    def profile_tenancy(self) -> str:
        return "tenancy"

    def list_subscribed_regions(self, _tenancy_id: str) -> list[dict[str, str]]:
        return [{"region-name": self.region}]

    def list_compartments(self, _tenancy_id: str) -> list[dict[str, str]]:
        self.compartment_attempts += 1
        if self.compartment_attempts == 1:
            raise RuntimeError("transient")
        return [{"id": "workloads", "name": "workloads"}]

    def list_db_systems(self, compartment_id: str) -> list[dict[str, str]]:
        return [{"id": "system"}] if compartment_id == "workloads" else []

    def list_databases(self, _compartment_id: str, _db_system_id: str) -> list[dict[str, str]]:
        return [{"id": f"db-{index:04d}", "display-name": f"db-{index:04d}"} for index in range(self.count)]

    def list_pluggable_databases(self, _compartment_id: str) -> list[dict[str, str]]:
        return []

    def list_autonomous_databases(self, _compartment_id: str) -> list[dict[str, str]]:
        return []

    def list_exadata_infrastructure(self, _compartment_id: str) -> list[dict[str, str]]:
        return []

    def list_managed_databases(self, _compartment_id: str) -> list[dict[str, str]]:
        return []


@pytest.mark.parametrize("count", [1, 100, 1000])
def test_discovery_is_retry_safe_and_deterministic_for_fleet_sizes(count: int) -> None:
    oci = _ManyTargetOci(count)

    targets = FleetDiscovery(oci, max_workers=8).discover()

    assert oci.compartment_attempts == 2
    assert len(targets) == count
    assert [target.target_id for target in targets] == sorted(target.target_id for target in targets)


def test_discovery_rejects_unbounded_worker_count() -> None:
    with pytest.raises(ValueError, match="between 1 and 8"):
        FleetDiscovery(_ManyTargetOci(1), max_workers=9)  # type: ignore[arg-type]


def test_failed_scope_enumeration_is_explicit_and_cannot_emit_a_complete_target_set() -> None:
    class _BrokenScopeOci(_ManyTargetOci):
        def list_subscribed_regions(self, _tenancy_id: str) -> list[dict[str, str]]:
            raise RuntimeError("not authorized")

    discovery = FleetDiscovery(_BrokenScopeOci(1))
    result = discovery.discover_result()

    assert not result.complete
    assert result.findings[0].scope == "regions"
    with pytest.raises(DiscoveryScopeError, match="incomplete"):
        discovery.discover()


def test_default_region_facade_uses_each_subscribed_region() -> None:
    class _Runner:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def run(self, args, **_kwargs):
            self.commands.append(args)
            payload = '{"data": [{"region-name": "us-ashburn-1"}, {"region-name": "eu-frankfurt-1"}]}'
            if "region-subscription" not in args:
                payload = '{"data": []}'
            return CommandResult(tuple(args), payload, "", 0)

    runner = _Runner()
    FleetDiscovery(OciCli("DEFAULT", "eu-frankfurt-1", runner)).discover("tenancy")  # type: ignore[arg-type]

    assert any(command[command.index("--region") + 1] == "eu-frankfurt-1" for command in runner.commands)
    assert any(command[command.index("--region") + 1] == "us-ashburn-1" for command in runner.commands)


def test_discovery_reads_compartments_in_bounded_parallelism() -> None:
    import threading

    class _ConcurrentOci(_ManyTargetOci):
        def __init__(self) -> None:
            super().__init__(0)
            self.barrier = threading.Barrier(2, timeout=2)

        def list_compartments(self, _tenancy_id: str) -> list[dict[str, str]]:
            return [{"id": "a", "name": "a"}, {"id": "b", "name": "b"}]

        def list_db_systems(self, compartment_id: str) -> list[dict[str, str]]:
            if compartment_id in {"a", "b"}:
                self.barrier.wait()
            return []

    assert FleetDiscovery(_ConcurrentOci(), max_workers=2).discover() == ()


class _JoinedStateOci:
    region = "eu-frankfurt-1"

    def profile_tenancy(self) -> str:
        return "tenancy"

    def list_subscribed_regions(self, _tenancy_id: str) -> list[dict[str, str]]:
        return [{"region-name": self.region}]

    def list_compartments(self, _tenancy_id: str) -> list[dict[str, str]]:
        return [{"id": "workloads", "name": "workloads"}]

    def list_db_systems(self, compartment_id: str) -> list[dict[str, str]]:
        if compartment_id != "workloads":
            return []
        return [
            {"id": "base-system", "shape": "VM.Standard", "display-name": "base"},
            {"id": "exa-system", "shape": "Exadata.X9M", "display-name": "exa"},
        ]

    def list_databases(self, _compartment_id: str, system_id: str) -> list[dict[str, object]]:
        if system_id == "base-system":
            return [{
                "id": "base-cdb",
                "display-name": "base-cdb",
                "lifecycle-state": "AVAILABLE",
                "database-management-config": {"management-status": "ENABLED"},
            }]
        return [{"id": "exa-cdb", "display-name": "exa-cdb", "lifecycle-state": "AVAILABLE"}]

    def list_pluggable_databases(self, compartment_id: str) -> list[dict[str, str]]:
        return [{"id": "exa-pdb", "pdb-name": "exa-pdb", "container-database-id": "exa-cdb"}] if compartment_id == "workloads" else []

    def list_autonomous_databases(self, _compartment_id: str) -> list[dict[str, str]]:
        return []

    def list_managed_databases(self, compartment_id: str) -> list[dict[str, str]]:
        if compartment_id != "workloads":
            return []
        return [
            {"id": "native-managed", "database-id": "base-cdb", "database-type": "CLOUD"},
            {"id": "external-db", "name": "external", "database-type": "EXTERNAL_DATABASE"},
            {
                "id": "external-exa",
                "name": "external-exa",
                "database-type": "EXTERNAL_EXADATA",
                "database-status": "UP",
                "lifecycle-state": "ACTIVE",
            },
        ]

    def list_opsi_database_insights_complete(self, _compartment_id: str):
        return ([{"id": "opsi", "database-id": "base-cdb", "lifecycle-state": "ACTIVE"}], True)

    def list_data_safe_targets(self, _compartment_id: str) -> list[dict[str, object]]:
        return [{"id": "datasafe", "database-details": {"database-id": "base-cdb"}, "lifecycle-state": "ACTIVE"}]

    def get_log_analytics_namespace(self, _compartment_id: str) -> str:
        return "namespace"

    def list_log_analytics_entities(self, _namespace: str, _compartment_id: str) -> list[dict[str, str]]:
        return [{"id": "entity", "cloud-resource-id": "base-cdb", "lifecycle-state": "ACTIVE"}]

    def list_log_analytics_associated_entities(self, _namespace: str, _compartment_id: str) -> list[dict[str, str]]:
        return [{"entity-id": "entity", "lifecycle-state": "ACCEPTED"}]


def test_discovery_preserves_database_families_excludes_infrastructure_and_joins_service_state() -> None:
    targets = FleetDiscovery(_JoinedStateOci()).discover()
    by_id = {target.target_id: target for target in targets}

    assert set(by_id) == {"base-cdb", "exa-cdb", "exa-pdb", "external-db", "external-exa"}
    assert by_id["exa-pdb"].kind == "exadata"
    assert by_id["exa-pdb"].settings["database_family"] == "exadata"
    assert by_id["base-cdb"].service_states == {
        "datasafe": "ENABLED", "dbm": "ENABLED", "logan": "ENABLED", "opsi": "ENABLED"
    }
    assert by_id["external-db"].kind == "external-db"
    assert by_id["external-exa"].kind == "external-exadata"
    assert by_id["external-exa"].service_states["dbm"] == "UP"


def test_incomplete_opsi_join_marks_scope_incomplete_instead_of_claiming_not_enabled() -> None:
    class _IncompleteOpsi(_JoinedStateOci):
        def list_opsi_database_insights_complete(self, _compartment_id: str):
            return ([], False)

    result = FleetDiscovery(_IncompleteOpsi()).discover_result()

    assert not result.complete
    assert any(finding.scope.endswith("/opsi") for finding in result.findings)
    assert {target.service_states["opsi"] for target in result.targets} == {"UNKNOWN"}


def test_discovery_reads_db_home_topology_but_lists_databases_by_db_system() -> None:
    class _DbHomesOci(_ManyTargetOci):
        def __init__(self) -> None:
            super().__init__(0)
            self.database_home_ids: list[str] = []

        def list_compartments(self, _tenancy_id: str) -> list[dict[str, str]]:
            return [{"id": "workloads", "name": "workloads"}]

        def list_db_systems(self, compartment_id: str) -> list[dict[str, str]]:
            return [{"id": "system"}] if compartment_id == "workloads" else []

        def list_db_homes(self, _compartment_id: str, _system_id: str) -> list[dict[str, str]]:
            return [{"id": "home-a"}, {"id": "home-b"}]

        def list_databases(self, _compartment_id: str, db_system_id: str) -> list[dict[str, str]]:
            self.database_home_ids.append(db_system_id)
            return [{"id": "database-system", "display-name": "system"}]

    oci = _DbHomesOci()
    targets = FleetDiscovery(oci).discover()

    assert oci.database_home_ids == ["system"]
    assert [target.target_id for target in targets] == ["database-system"]


def test_empty_or_missing_db_home_ids_do_not_trigger_database_calls() -> None:
    class _EmptyHomesOci(_ManyTargetOci):
        def __init__(self) -> None:
            super().__init__(0)
            self.database_calls: list[str] = []

        def list_compartments(self, _tenancy_id: str) -> list[dict[str, str]]:
            return [{"id": "workloads", "name": "workloads"}]

        def list_db_systems(self, compartment_id: str) -> list[dict[str, str]]:
            return [{"id": "system"}] if compartment_id == "workloads" else []

        def list_db_homes(self, _compartment_id: str, _system_id: str) -> list[dict[str, str]]:
            return [{"display-name": "missing-id"}]

        def list_databases(self, _compartment_id: str, db_system_id: str) -> list[dict[str, str]]:
            self.database_calls.append(db_system_id)
            return []

    oci = _EmptyHomesOci()
    FleetDiscovery(oci).discover()

    # Database enumeration is parent-scoped once, not evaluated per fake home.
    assert oci.database_calls == ["system"]


def test_discovery_uses_vm_cluster_parent_route_once_per_cluster() -> None:
    class _VmClusterOci(_ManyTargetOci):
        def __init__(self) -> None:
            super().__init__(0)
            self.parents: list[str] = []

        def list_compartments(self, _tenancy_id: str) -> list[dict[str, str]]:
            return [{"id": "workloads", "name": "workloads"}]

        def list_db_systems(self, _compartment_id: str) -> list[dict[str, str]]:
            return []

        def list_cloud_vm_clusters(self, compartment_id: str) -> list[dict[str, str]]:
            return [{"id": "vm-cluster"}] if compartment_id == "workloads" else []

        def list_vm_clusters(self, compartment_id: str) -> list[dict[str, str]]:
            return [{"id": "vm-cluster"}] if compartment_id == "workloads" else []

        def list_db_homes(self, _compartment_id: str, *, db_system_id=None, vm_cluster_id=None) -> list[dict[str, str]]:
            assert db_system_id is None
            assert vm_cluster_id == "vm-cluster"
            return []

        def list_databases_for_vm_cluster(self, _compartment_id: str, vm_cluster_id: str) -> list[dict[str, str]]:
            self.parents.append(vm_cluster_id)
            return [{"id": "exa-cdb", "display-name": "exa-cdb"}]

    oci = _VmClusterOci()
    targets = FleetDiscovery(oci).discover()

    assert oci.parents == ["vm-cluster"]
    assert [(target.target_id, target.kind, target.settings["vm_cluster_id"]) for target in targets] == [
        ("exa-cdb", "exadata", "vm-cluster")
    ]


def test_idless_database_parent_summaries_never_trigger_child_reads() -> None:
    class _IdlessParentsOci(_ManyTargetOci):
        def __init__(self) -> None:
            super().__init__(0)
            self.child_reads = 0

        def list_compartments(self, _tenancy_id: str) -> list[dict[str, str]]:
            return [{"id": "workloads", "name": "workloads"}]

        def list_db_systems(self, compartment_id: str) -> list[dict[str, str]]:
            return [{"display-name": "idless-system"}] if compartment_id == "workloads" else []

        def list_cloud_vm_clusters(self, compartment_id: str) -> list[dict[str, str]]:
            return [{"display-name": "idless-cluster"}] if compartment_id == "workloads" else []

        def list_vm_clusters(self, _compartment_id: str) -> list[dict[str, str]]:
            return []

        def list_db_homes(self, *_args, **_kwargs) -> list[dict[str, str]]:
            self.child_reads += 1
            return []

        def list_databases(self, *_args) -> list[dict[str, str]]:
            self.child_reads += 1
            return []

        def list_databases_for_vm_cluster(self, *_args) -> list[dict[str, str]]:
            self.child_reads += 1
            return []

    oci = _IdlessParentsOci()
    assert FleetDiscovery(oci).discover() == ()
    assert oci.child_reads == 0
