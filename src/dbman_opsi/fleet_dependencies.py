"""Validation and deterministic ordering for fleet target dependencies."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from dbman_opsi.fleet import CredentialPolicy, TargetPlan


class DependencyGraphError(ValueError):
    """Raised when an observed/selected target graph is not safely executable."""


@runtime_checkable
class _TargetLike(Protocol):
    target_id: str
    kind: str
    settings: Mapping[str, object]


@dataclass(frozen=True)
class DependencyGraph:
    """An immutable graph which can yield targets in parent-before-child order."""

    targets: tuple[_TargetLike, ...]
    dependencies: Mapping[str, tuple[str, ...]]

    def dependencies_for(self, target_id: str) -> tuple[str, ...]:
        return self.dependencies[target_id]

    def ordered_targets(self) -> tuple[_TargetLike, ...]:
        by_id = {target.target_id: target for target in self.targets}
        remaining = {target_id: set(dependencies) for target_id, dependencies in self.dependencies.items()}
        ordered: list[_TargetLike] = []
        while remaining:
            ready = sorted(target_id for target_id, dependencies in remaining.items() if not dependencies)
            if not ready:
                raise DependencyGraphError("target dependency graph contains a cycle")
            for target_id in ready:
                ordered.append(by_id[target_id])
                del remaining[target_id]
            completed = set(ready)
            for dependencies in remaining.values():
                dependencies.difference_update(completed)
        return tuple(ordered)


def build_dependency_graph(targets: Iterable[_TargetLike]) -> DependencyGraph:
    """Validate CDB/PDB constraints and construct a deterministic graph."""

    materialized = tuple(targets)
    by_id = {target.target_id: target for target in materialized}
    if len(by_id) != len(materialized):
        raise DependencyGraphError("target dependency graph contains duplicate target ids")
    dependencies: dict[str, tuple[str, ...]] = {}
    for target in materialized:
        role = str(target.settings.get("database_role", "CDB")).upper()
        declared = tuple(getattr(target, "dependencies", ()))
        parent = (getattr(target, "parent_cdb_id", None) or target.settings.get("parent_cdb_id")) if role == "PDB" else None
        if role == "PDB" and not parent:
            raise DependencyGraphError(f"PDB target {target.target_id} is missing its CDB dependency")
        target_dependencies = tuple(sorted(set(declared + ((str(parent),) if parent else ()))))
        missing = [dependency for dependency in target_dependencies if dependency not in by_id]
        if missing:
            raise DependencyGraphError(f"target {target.target_id} has unknown dependencies: {', '.join(missing)}")
        if target.target_id in target_dependencies:
            raise DependencyGraphError(f"target {target.target_id} cannot depend on itself")
        if parent:
            parent_target = by_id[parent]
            parent_role = str(parent_target.settings.get("database_role", "CDB")).upper()
            parent_kind = str(parent_target.kind)
            child_kind = str(target.kind)
            parent_region = getattr(parent_target, "region", None)
            child_region = getattr(target, "region", None)
            parent_compartment = getattr(parent_target, "compartment_id", None)
            child_compartment = getattr(target, "compartment_id", None)
            if parent_role != "CDB" or parent_kind not in {"dbcs", "exadata"}:
                raise DependencyGraphError(f"PDB target {target.target_id} must depend on a real CDB database target")
            if child_kind != parent_kind:
                raise DependencyGraphError(f"PDB target {target.target_id} must use the same CDB family as its parent")
            if parent_region != child_region or parent_compartment != child_compartment:
                raise DependencyGraphError(f"PDB target {target.target_id} must share its CDB parent region and compartment")
        dependencies[target.target_id] = target_dependencies
    graph = DependencyGraph(tuple(sorted(materialized, key=lambda target: target.target_id)), dependencies)
    graph.ordered_targets()
    return graph


def target_plans_from_discovery(
    targets: Iterable[_TargetLike],
    *,
    credential_policy: CredentialPolicy,
    services: tuple[str, ...],
) -> tuple[TargetPlan, ...]:
    """Convert validated observations to the accepted immutable Task 1 plans."""

    graph = build_dependency_graph(targets)
    plans: list[TargetPlan] = []
    for target in graph.ordered_targets():
        role = str(target.settings.get("database_role", "CDB")).upper()
        parent = (getattr(target, "parent_cdb_id", None) or target.settings.get("parent_cdb_id")) if role == "PDB" else None
        settings = dict(target.settings)
        if parent:
            settings["parent_cdb_id"] = parent
        plans.append(
            TargetPlan(
                target_id=target.target_id,
                name=getattr(target, "name", target.target_id),
                kind=target.kind,
                region=getattr(target, "region"),
                compartment_id=getattr(target, "compartment_id", None),
                resource_id=getattr(target, "resource_id", None),
                services=services,
                dependencies=graph.dependencies_for(target.target_id),
                credential_policy=credential_policy,
                settings=settings,
            )
        )
    return tuple(plans)
