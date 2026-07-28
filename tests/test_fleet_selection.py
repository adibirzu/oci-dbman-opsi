from __future__ import annotations

from pathlib import Path

import pytest

from dbman_opsi.fleet_discovery import DiscoveredTarget
from dbman_opsi.fleet_selection import TargetSelection, load_selection_ids, select_targets


def _target(
    target_id: str,
    *,
    region: str = "eu-frankfurt-1",
    compartment: str = "compartment-a",
    kind: str = "dbcs",
    state: str = "AVAILABLE",
    tags: dict[str, str] | None = None,
    services: dict[str, str] | None = None,
) -> DiscoveredTarget:
    return DiscoveredTarget(
        target_id=target_id,
        name=f"{target_id}-orders",
        kind=kind,
        region=region,
        compartment_id=compartment,
        lifecycle_state=state,
        tags=tags or {},
        service_states=services or {},
    )


def test_selection_combines_filters_and_explicit_exclusions_deterministically() -> None:
    selected = select_targets(
        (
            _target("z", tags={"team": "database"}, services={"dbm": "ENABLED"}),
            _target("a", tags={"team": "database"}, services={"dbm": "ENABLED"}),
            _target("skip", tags={"team": "database"}, services={"dbm": "ENABLED"}),
            _target("wrong-region", region="us-ashburn-1", tags={"team": "database"}, services={"dbm": "ENABLED"}),
            _target("wrong-tag", tags={"team": "other"}, services={"dbm": "ENABLED"}),
            _target("wrong-service", tags={"team": "database"}, services={"dbm": "DISABLED"}),
        ),
        TargetSelection(
            regions=("eu-frankfurt-1",),
            compartments=("compartment-a",),
            kinds=("dbcs",),
            lifecycle_states=("available",),
            tags={"team": "database"},
            name_pattern="*-orders",
            service_states={"dbm": "enabled"},
            exclude_target_ids=("skip",),
        ),
    )

    assert [target.target_id for target in selected] == ["a", "z"]


@pytest.mark.parametrize(
    ("filename", "contents"),
    [
        ("selected.csv", "target_id\na\nz\na\n"),
        ("selected.yaml", "targets:\n  - target_id: z\n  - a\n"),
    ],
)
def test_selection_file_ids_are_deduplicated_for_csv_and_yaml(tmp_path: Path, filename: str, contents: str) -> None:
    source = tmp_path / filename
    source.write_text(contents, encoding="utf-8")

    assert load_selection_ids(source) == ("a", "z")
    selected = select_targets((_target("a"), _target("z"), _target("other")), TargetSelection(all_discovered=False).with_file(source))
    assert [target.target_id for target in selected] == ["a", "z"]


def test_selection_requires_explicit_ids_when_all_discovered_is_disabled() -> None:
    assert select_targets((_target("a"),), TargetSelection(all_discovered=False)) == ()
