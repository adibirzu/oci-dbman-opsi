from __future__ import annotations

import dbman_opsi.cli as lifecycle_cli
from dbman_opsi.fleet import FleetPlan, TargetPlan
from dbman_opsi.fleet_auth import AuthMode, OciAuth
from dbman_opsi.fleet_offboarding import CleanupAction
from dbman_opsi.oci_cli import OciCli
from dbman_opsi.runner import CommandResult


class _RegionalRunner:
    def __init__(self, calls: list[tuple[str, str, str]]) -> None:
        self.calls = calls

    def run(self, args, **_kwargs):
        region = args[args.index("--region") + 1]
        if args[5:8] == ["opsi", "database-insights", "enable-autonomous-database"]:
            self.calls.append((region, "enable-opsi", args[args.index("--database-insight-id") + 1]))
        elif args[5:8] == ["vault", "secret", "schedule-secret-deletion"]:
            self.calls.append((region, "delete-secret", args[args.index("--secret-id") + 1]))
        else:
            raise AssertionError(f"unexpected OCI command: {args}")
        return CommandResult(tuple(args), "", "", 0)


def _two_region_plan() -> FleetPlan:
    return FleetPlan(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        targets=(
            TargetPlan(
                "east", "east", "autonomous", "eu-frankfurt-1", services=("opsi",),
                settings={"opsi_database_insight_id": "insight-east"},
            ),
            TargetPlan(
                "west", "west", "autonomous", "us-ashburn-1", services=("opsi",),
                settings={"opsi_database_insight_id": "insight-west"},
            ),
        ),
    )


def test_region_routed_lifecycle_and_cleanup_use_each_target_or_action_region() -> None:
    # Break caught: reusing the seed-region facade sends the west target/action
    # to eu-frankfurt-1 instead of its own us-ashburn-1 control plane.
    plan = _two_region_plan()
    calls: list[tuple[str, str, str]] = []
    created: list[str] = []

    def oci_for_region(region: str) -> OciCli:
        created.append(region)
        return OciCli(
            "DEFAULT",
            region,
            _RegionalRunner(calls),
            auth=OciAuth(AuthMode.API_KEY, "DEFAULT"),
        )

    lifecycle_operations = getattr(lifecycle_cli, "_RegionRoutedLifecycleOperations", None)
    cleanup_operations = getattr(lifecycle_cli, "_RegionRoutedCleanupOperations", None)
    assert lifecycle_operations is not None, "missing target-region lifecycle routing"
    assert cleanup_operations is not None, "missing action-region cleanup routing"

    lifecycle = lifecycle_operations(plan, oci_for_region)
    opsi = lifecycle.handlers()["opsi"]
    assert opsi(plan.targets[0]) is not None
    assert opsi(plan.targets[1]) is not None
    assert opsi(plan.targets[0]) is not None

    assert calls[:3] == [
        ("eu-frankfurt-1", "enable-opsi", "insight-east"),
        ("us-ashburn-1", "enable-opsi", "insight-west"),
        ("eu-frankfurt-1", "enable-opsi", "insight-east"),
    ]
    assert created == ["eu-frankfurt-1", "us-ashburn-1"]

    cleanup = cleanup_operations(oci_for_region)
    cleanup.execute_cleanup(CleanupAction(
        "delete-secret", "east", "autonomous", "secret", "secret-east",
        {"region": "eu-frankfurt-1", "secret_id": "secret-east"},
    ))
    cleanup.execute_cleanup(CleanupAction(
        "delete-secret", "west", "autonomous", "secret", "secret-west",
        {"region": "us-ashburn-1", "secret_id": "secret-west"},
    ))

    assert calls[3:] == [
        ("eu-frankfurt-1", "delete-secret", "secret-east"),
        ("us-ashburn-1", "delete-secret", "secret-west"),
    ]
