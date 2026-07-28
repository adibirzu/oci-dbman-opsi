"""Deterministic, side-effect-free selection of observed fleet targets."""

from __future__ import annotations

import csv
import fnmatch
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

import yaml

from dbman_opsi.fleet_discovery import DiscoveredTarget


def _normal(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip().lower() for value in values if str(value).strip()}))


def _ids(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip() for value in values if str(value).strip()}))


@dataclass(frozen=True)
class TargetSelection:
    """Composable filter and inclusion rules for a discovered tenancy fleet.

    ``all_discovered`` means filters start from every observed target.  When it
    is false, a target must be explicitly named by ``target_ids`` (including
    IDs loaded from a CSV/YAML selection file) before the same safety filters
    are applied.  Exclusions always win.
    """

    regions: tuple[str, ...] = ()
    compartments: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    lifecycle_states: tuple[str, ...] = ()
    tags: Mapping[str, str] = field(default_factory=dict, compare=False)
    name_pattern: str | None = None
    service_states: Mapping[str, str] = field(default_factory=dict, compare=False)
    target_ids: tuple[str, ...] = ()
    exclude_target_ids: tuple[str, ...] = ()
    all_discovered: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "regions", _normal(self.regions))
        object.__setattr__(self, "compartments", _ids(self.compartments))
        object.__setattr__(self, "kinds", _normal(self.kinds))
        object.__setattr__(self, "lifecycle_states", _normal(self.lifecycle_states))
        object.__setattr__(self, "tags", MappingProxyType(dict(sorted((str(key), str(value)) for key, value in self.tags.items()))))
        object.__setattr__(self, "service_states", MappingProxyType(dict(sorted((str(key).lower(), str(value).lower()) for key, value in self.service_states.items()))))
        object.__setattr__(self, "target_ids", _ids(self.target_ids))
        object.__setattr__(self, "exclude_target_ids", _ids(self.exclude_target_ids))
        if self.name_pattern == "":
            object.__setattr__(self, "name_pattern", None)

    def with_file(self, path: str | Path) -> "TargetSelection":
        """Return this selection augmented by target IDs from a CSV/YAML file."""

        return TargetSelection(
            regions=self.regions,
            compartments=self.compartments,
            kinds=self.kinds,
            lifecycle_states=self.lifecycle_states,
            tags=self.tags,
            name_pattern=self.name_pattern,
            service_states=self.service_states,
            target_ids=(*self.target_ids, *load_selection_ids(path)),
            exclude_target_ids=self.exclude_target_ids,
            all_discovered=self.all_discovered,
        )


def load_selection_ids(path: str | Path) -> tuple[str, ...]:
    """Load explicit target IDs from a narrow CSV or YAML selection file."""

    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".csv":
        with source.open(newline="", encoding="utf-8") as handle:
            rows = csv.DictReader(handle)
            if not rows.fieldnames or "target_id" not in rows.fieldnames:
                raise ValueError("CSV selection files require a target_id column")
            return _ids(row.get("target_id", "") for row in rows)
    if suffix not in {".yaml", ".yml"}:
        raise ValueError("selection files must be CSV or YAML")
    with source.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    values = data.get("targets", data.get("target_ids", ())) if isinstance(data, Mapping) else data
    if not isinstance(values, list):
        raise ValueError("YAML selection files require a targets or target_ids list")
    return _ids(item.get("target_id", item.get("id", "")) if isinstance(item, Mapping) else item for item in values)


def select_targets(
    targets: Iterable[DiscoveredTarget],
    selection: TargetSelection = TargetSelection(),
) -> tuple[DiscoveredTarget, ...]:
    """Apply all selection rules and return a deterministic, duplicate-free set."""

    included = set(selection.target_ids)
    excluded = set(selection.exclude_target_ids)
    matched: dict[str, DiscoveredTarget] = {}
    for target in sorted(targets, key=_target_sort_key):
        if target.target_id in excluded:
            continue
        if not selection.all_discovered and target.target_id not in included:
            continue
        if _matches(target, selection):
            matched.setdefault(target.target_id, target)
    return tuple(sorted(matched.values(), key=_target_sort_key))


def _matches(target: DiscoveredTarget, selection: TargetSelection) -> bool:
    if selection.regions and target.region.lower() not in selection.regions:
        return False
    if selection.compartments and target.compartment_id not in selection.compartments:
        return False
    if selection.kinds and target.kind.lower() not in selection.kinds:
        return False
    if selection.lifecycle_states and target.lifecycle_state.lower() not in selection.lifecycle_states:
        return False
    if selection.name_pattern and not fnmatch.fnmatchcase(target.name, selection.name_pattern):
        return False
    if any(target.tags.get(key) != value for key, value in selection.tags.items()):
        return False
    return all(target.service_states.get(service, "").lower() == state for service, state in selection.service_states.items())


def _target_sort_key(target: DiscoveredTarget) -> tuple[str, str, str, str, str]:
    return (target.region, target.compartment_id, target.kind, target.name, target.target_id)
