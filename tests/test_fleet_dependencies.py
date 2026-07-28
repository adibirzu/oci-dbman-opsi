from __future__ import annotations

import pytest

from dbman_opsi.fleet import CredentialPolicy, TargetPlan
from dbman_opsi.fleet_dependencies import DependencyGraphError, build_dependency_graph, target_plans_from_discovery
from dbman_opsi.fleet_discovery import DiscoveredTarget


def _target(
    target_id: str,
    *,
    role: str = "CDB",
    parent: str | None = None,
    kind: str = "dbcs",
    region: str = "eu-frankfurt-1",
    compartment: str = "compartment",
) -> DiscoveredTarget:
    return DiscoveredTarget(
        target_id=target_id,
        name=target_id,
        kind=kind,
        region=region,
        compartment_id=compartment,
        parent_cdb_id=parent,
        settings={"database_role": role, "database_family": kind},
    )


def test_dependency_graph_orders_cdb_before_pdb() -> None:
    graph = build_dependency_graph((_target("pdb", role="PDB", parent="cdb"), _target("cdb")))

    assert graph.dependencies_for("pdb") == ("cdb",)
    assert [target.target_id for target in graph.ordered_targets()] == ["cdb", "pdb"]


def test_dependency_graph_reuses_task_one_target_plan_model() -> None:
    plans = target_plans_from_discovery(
        (_target("pdb", role="PDB", parent="cdb"), _target("cdb")),
        credential_policy=CredentialPolicy.HANDOFF_REQUIRED,
        services=("dbm", "opsi"),
    )

    assert [(plan.target_id, plan.dependencies, plan.credential_policy) for plan in plans] == [
        ("cdb", (), CredentialPolicy.HANDOFF_REQUIRED),
        ("pdb", ("cdb",), CredentialPolicy.HANDOFF_REQUIRED),
    ]


def test_dependency_graph_validates_task_one_pdb_plans() -> None:
    cdb = TargetPlan(target_id="cdb", name="cdb", kind="dbcs", region="eu-frankfurt-1")
    pdb = TargetPlan(
        target_id="pdb",
        name="pdb",
        kind="dbcs",
        region="eu-frankfurt-1",
        dependencies=("cdb",),
        settings={"database_role": "PDB", "parent_cdb_id": "cdb"},
    )

    assert [target.target_id for target in build_dependency_graph((pdb, cdb)).ordered_targets()] == ["cdb", "pdb"]


def test_dependency_graph_allows_exadata_pdb_only_with_matching_exadata_cdb() -> None:
    graph = build_dependency_graph(
        (_target("pdb", role="PDB", parent="cdb", kind="exadata"), _target("cdb", kind="exadata"))
    )

    assert graph.dependencies_for("pdb") == ("cdb",)


@pytest.mark.parametrize(
    "targets",
    [
        (_target("pdb", role="PDB", parent="parent"), _target("parent", kind="autonomous")),
        (_target("pdb", role="PDB", parent="parent"), _target("parent", region="us-ashburn-1")),
        (_target("pdb", role="PDB", parent="parent"), _target("parent", compartment="other")),
        (_target("pdb", role="PDB", parent="parent"), _target("parent", kind="exadata")),
    ],
)
def test_dependency_graph_rejects_incompatible_cdb_parent(targets: tuple[DiscoveredTarget, ...]) -> None:
    with pytest.raises(DependencyGraphError, match="CDB"):
        build_dependency_graph(targets)


@pytest.mark.parametrize(
    "targets",
    [
        (_target("pdb", role="PDB"),),
        (_target("pdb", role="PDB", parent="missing"),),
        (_target("pdb", role="PDB", parent="other-pdb"), _target("other-pdb", role="PDB", parent="pdb")),
    ],
)
def test_dependency_graph_rejects_invalid_pdb_dependencies(targets: tuple[DiscoveredTarget, ...]) -> None:
    with pytest.raises(DependencyGraphError):
        build_dependency_graph(targets)
