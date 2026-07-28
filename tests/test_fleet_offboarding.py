from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from dbman_opsi.fleet import (
    DeploymentMode,
    FleetPlan,
    ResourceOwnership,
    ResourceRecord,
    RunManifest,
    TargetManifest,
    TargetPlan,
)
from dbman_opsi.fleet_offboarding import (
    CleanupPlan,
    CleanupExecutor,
    CleanupAction,
    CleanupHandoffRequired,
    CleanupHandoffEvidenceImporter,
    CleanupHandoffPacketWriter,
    OciCleanupOperations,
    CleanupPlanner,
    DatabaseDeletionRefused,
)
from dbman_opsi.fleet_state import FleetStateStore
from dbman_opsi.fleet_operations import LifecycleOperations
from dbman_opsi.runner import OciAlreadyDone, OciAuthError, OciNotFound


def _plan(*targets: TargetPlan, mode: DeploymentMode = DeploymentMode.POC) -> FleetPlan:
    return FleetPlan(
        profile="DEFAULT", region="eu-frankfurt-1", targets=targets, deployment_mode=mode
    )


def _target(target_id: str, kind: str = "dbcs") -> TargetPlan:
    return TargetPlan(
        target_id=target_id,
        name=target_id,
        kind=kind,
        region="eu-frankfurt-1",
    )


def test_real_datasafe_lifecycle_resource_is_unregistered_once(tmp_path: Path) -> None:
    target = TargetPlan(
        "adb",
        "adb",
        "autonomous",
        "eu-frankfurt-1",
        compartment_id="compartment",
        resource_id="autonomous-database",
        services=("datasafe",),
    )
    plan = _plan(target)

    class _DataSafeOci:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def list_data_safe_targets(self, _compartment_id: str):
            return []

        def create_data_safe_target(self, **_kwargs):
            return "datasafe-target"

        def delete_data_safe_target(self, target_database_id: str) -> None:
            self.deleted.append(target_database_id)

    oci = _DataSafeOci()
    lifecycle = LifecycleOperations(plan, oci)  # type: ignore[arg-type]
    lifecycle.data_safe.credential_provider = lambda _target: ("DATASAFE_USER", "runtime-only")
    outcome = lifecycle.datasafe(target)
    assert outcome.resources[0].resource_type == "datasafe-target"
    assert outcome.resources[0].cleanup_allowed

    manifest = RunManifest(
        "run",
        plan.plan_id,
        (TargetManifest("adb", resources=outcome.resources),),
    )
    cleanup = CleanupPlanner(plan, manifest).build()
    assert [action.operation for action in cleanup.actions] == ["unregister-data-safe"]
    store = FleetStateStore(tmp_path / "state.sqlite")
    executor = CleanupExecutor(cleanup, store, OciCleanupOperations(oci))
    assert executor.execute(approved_plan_id=cleanup.plan_id).complete
    assert executor.execute(approved_plan_id=cleanup.plan_id).complete
    assert oci.deleted == ["datasafe-target"]


def test_cleanup_plan_reverses_service_dependencies_and_disables_pdb_before_cdb() -> None:
    plan = _plan(_target("cdb", "dbcs-cdb"), _target("pdb", "dbcs-pdb"))
    manifest = RunManifest(
        "run-1",
        plan.plan_id,
        (
            TargetManifest(
                "cdb",
                resources=(
                    ResourceRecord(
                        "dbm-cdb", "managed-cdb", ResourceOwnership.OWNED, True,
                        {"feature": "DIAGNOSTICS_AND_MANAGEMENT"},
                    ),
                    ResourceRecord("opsi-insight", "opsi-cdb", ResourceOwnership.OWNED, True),
                    ResourceRecord(
                        "log-analytics-association", "logan-cdb", ResourceOwnership.OWNED, True,
                        {"namespace": "logan", "compartment_id": "compartment", "items": [{"entityId": "entity"}]},
                    ),
                ),
            ),
            TargetManifest(
                "pdb",
                resources=(
                    ResourceRecord(
                        "dbm-pdb", "managed-pdb", ResourceOwnership.OWNED, True,
                        {"feature": "DIAGNOSTICS_AND_MANAGEMENT"},
                    ),
                ),
            ),
        ),
    )

    cleanup = CleanupPlanner(plan, manifest).build()

    assert [(action.operation, action.target_id) for action in cleanup.actions] == [
        ("dissociate-log-analytics", "cdb"),
        ("disable-opsi", "cdb"),
        ("disable-dbm-pdb", "pdb"),
        ("disable-dbm-cdb", "cdb"),
    ]
    assert cleanup.actions[0].arguments["items"] == ({"entityId": "entity"},)
    assert cleanup.actions[2].arguments == {
        "region": "eu-frankfurt-1",
        "pluggable_database_id": "managed-pdb",
        "feature": "DIAGNOSTICS_AND_MANAGEMENT",
    }


def test_lifecycle_dbm_cleanup_uses_database_service_verbs_for_cdb_pdb_and_autonomous() -> None:
    class Oci:
        def __init__(self): self.calls = []
        def disable_database_management(self, value): self.calls.append(("cdb", value))
        def disable_pluggable_database_management(self, value): self.calls.append(("pdb", value))
        def disable_autonomous_database_management(self, value): self.calls.append(("autonomous", value))

    oci = Oci()
    operations = OciCleanupOperations(oci)
    for action in (
        CleanupAction("disable-dbm-pdb", "pdb", "dbcs", "dbm-pdb", "pdb-id", {"pluggable_database_id": "pdb-id"}),
        CleanupAction("disable-dbm-cdb", "cdb", "dbcs", "dbm-cdb", "cdb-id", {"database_id": "cdb-id"}),
        CleanupAction("disable-dbm-cdb", "adb", "autonomous", "dbm-autonomous", "adb-id", {"database_id": "adb-id"}),
    ):
        operations.execute_cleanup(action)
    assert oci.calls == [("pdb", "pdb-id"), ("cdb", "cdb-id"), ("autonomous", "adb-id")]


class _Operations:
    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[tuple[str, str]] = []

    def execute_cleanup(self, action) -> None:
        self.calls.append((action.operation, action.resource_ref))
        if action.resource_ref in self.failures:
            raise self.failures[action.resource_ref]


def test_cleanup_continues_independent_actions_and_resumes_only_failed_work(tmp_path) -> None:
    plan = _plan(_target("db"))
    manifest = RunManifest(
        "run-1",
        plan.plan_id,
        (TargetManifest("db", resources=(
            ResourceRecord("opsi-insight", "opsi", ResourceOwnership.OWNED, True),
            ResourceRecord("dbm-cdb", "dbm", ResourceOwnership.OWNED, True),
        )),),
    )
    cleanup = CleanupPlanner(plan, manifest).build()
    store = FleetStateStore(tmp_path / "state.sqlite")
    first_ops = _Operations({"opsi": RuntimeError("500 unavailable")})

    first = CleanupExecutor(cleanup, store, first_ops).execute(approved_plan_id=cleanup.plan_id)

    assert first.partial
    assert first_ops.calls == [("disable-opsi", "opsi"), ("disable-dbm-cdb", "dbm")]
    second_ops = _Operations()
    second = CleanupExecutor(cleanup, store, second_ops).execute(approved_plan_id=cleanup.plan_id)
    assert not second.partial
    assert second_ops.calls == [("disable-opsi", "opsi")]


def test_cleanup_preserves_reused_preexisting_and_not_enabled_resources() -> None:
    plan = _plan(_target("db"))
    manifest = RunManifest(
        "run-1", plan.plan_id,
        (TargetManifest("db", resources=(
            ResourceRecord("secret", "owned-enabled", ResourceOwnership.CREATED, True),
            ResourceRecord("secret", "reused", ResourceOwnership.REUSED, True),
            ResourceRecord("secret", "preexisting", ResourceOwnership.PREEXISTING, True),
            ResourceRecord("secret", "not-enabled", ResourceOwnership.CREATED, False),
            ResourceRecord("named-credential", "existing-credential", ResourceOwnership.REUSED, False),
        )),),
    )

    cleanup = CleanupPlanner(plan, manifest).build()

    assert [(item.operation, item.resource_ref) for item in cleanup.actions] == [
        ("delete-secret", "owned-enabled")
    ]


def test_database_deletion_needs_its_own_typed_confirmation_and_is_refused_in_production(tmp_path) -> None:
    plan = _plan(_target("test-db"))
    manifest = RunManifest(
        "run-1", plan.plan_id,
        (TargetManifest("test-db", resources=(
            ResourceRecord("dbcs-test-database", "test-db", ResourceOwnership.CREATED, True),
        )),),
    )
    cleanup = CleanupPlanner(plan, manifest, delete_test_databases=True).build()
    with pytest.raises(DatabaseDeletionRefused, match="typed database"):
        CleanupExecutor(cleanup, FleetStateStore(tmp_path / "state.sqlite"), _Operations()).execute(
            approved_plan_id=cleanup.plan_id, database_confirmation="DELETE ALL"
        )
    ops = _Operations()
    CleanupExecutor(cleanup, FleetStateStore(tmp_path / "state.sqlite"), ops).execute(
        approved_plan_id=cleanup.plan_id, database_confirmation=cleanup.database_confirmation
    )
    assert ops.calls == [("delete-test-database", "test-db")]

    production = _plan(_target("test-db"), mode=DeploymentMode.PRODUCTION)
    prod_manifest = RunManifest("run-2", production.plan_id, manifest.targets)
    with pytest.raises(DatabaseDeletionRefused, match="production cleanup"):
        CleanupPlanner(production, prod_manifest, delete_test_databases=True).build()


def test_cleanup_requires_exact_approval_and_only_typed_absence_is_idempotent(tmp_path) -> None:
    plan = _plan(_target("db"))
    manifest = RunManifest(
        "run-1", plan.plan_id,
        (TargetManifest("db", resources=(
            ResourceRecord("opsi-insight", "opsi", ResourceOwnership.CREATED, True),
            ResourceRecord("dbm-cdb", "dbm", ResourceOwnership.CREATED, True),
        )),),
    )
    cleanup = CleanupPlanner(plan, manifest).build()
    store = FleetStateStore(tmp_path / "state.sqlite")
    executor = CleanupExecutor(
        cleanup, store, _Operations({"opsi": OciNotFound("404"), "dbm": OciAlreadyDone("already disabled")})
    )
    with pytest.raises(ValueError, match="approval does not match"):
        executor.execute(approved_plan_id="wrong")
    result = executor.execute(
        approved_plan_id=cleanup.plan_id, now=datetime(2026, 7, 27, tzinfo=UTC)
    )
    assert result.complete
    assert result.evidence.retained_until == "2026-08-03T00:00:00+00:00"
    persisted = store.load_cleanup_state(run_id="run-1", cleanup_plan_id=cleanup.plan_id)
    assert persisted and "opsi" not in str(persisted) and "dbm" not in str(persisted)

    conflict = CleanupExecutor(
        cleanup, FleetStateStore(tmp_path / "conflict.sqlite"), _Operations({"opsi": RuntimeError("409 resource in use")})
    ).execute(approved_plan_id=cleanup.plan_id)
    assert conflict.partial


def test_repeated_completed_cleanup_is_a_noop(tmp_path) -> None:
    plan = _plan(_target("db"))
    manifest = RunManifest("run-1", plan.plan_id, (TargetManifest("db", resources=(
        ResourceRecord("opsi-insight", "opsi", ResourceOwnership.CREATED, True),
    )),))
    cleanup = CleanupPlanner(plan, manifest).build()
    store = FleetStateStore(tmp_path / "state.sqlite")
    first = _Operations()
    first_result = CleanupExecutor(cleanup, store, first).execute(
        approved_plan_id=cleanup.plan_id, now=datetime(2026, 7, 27, tzinfo=UTC)
    )
    repeated = _Operations()
    result = CleanupExecutor(cleanup, store, repeated).execute(
        approved_plan_id=cleanup.plan_id, now=datetime(2026, 8, 27, tzinfo=UTC)
    )
    assert result.complete
    assert repeated.calls == []
    assert first_result.evidence is not None
    assert result.evidence is None


def test_concrete_oci_cleanup_adapter_maps_structured_actions_without_evidence_refs() -> None:
    class Oci:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def delete_log_analytics_associations(self, *args) -> None:
            self.calls.append(("logan", args))

        def disable_opsi_database_insight(self, *args) -> None:
            self.calls.append(("opsi", args))

        def disable_dbm_pdb(self, *args) -> None:
            self.calls.append(("pdb", args))

        def disable_dbm_cdb(self, *args, **kwargs) -> None:
            self.calls.append(("cdb", args + (kwargs,)))

        def schedule_run_owned_secret_deletion(self, *args) -> None:
            self.calls.append(("secret", args))

    oci = Oci()
    adapter = OciCleanupOperations(oci)
    adapter.execute_cleanup(CleanupAction(
        "dissociate-log-analytics", "db", "dbcs", "log-analytics-association", "assoc",
        {"namespace": "namespace", "compartment_id": "compartment", "items": [{"entityId": "entity"}]},
    ))
    adapter.execute_cleanup(CleanupAction(
        "disable-opsi", "db", "dbcs", "opsi-insight", "insight", {"insight_id": "insight"}
    ))
    adapter.execute_cleanup(CleanupAction(
        "disable-dbm-pdb", "pdb", "dbcs-pdb", "dbm-pdb", "pdb-id",
        {"pluggable_database_id": "pdb-id", "feature": "DIAGNOSTICS_AND_MANAGEMENT"},
    ))
    adapter.execute_cleanup(CleanupAction(
        "disable-dbm-cdb", "cdb", "dbcs-cdb", "dbm-cdb", "cdb-id",
        {"database_id": "cdb-id", "feature": "DIAGNOSTICS_AND_MANAGEMENT", "can_disable_all_pdbs": True},
    ))
    adapter.execute_cleanup(CleanupAction(
        "delete-secret", "db", "dbcs", "secret", "secret-id", {"secret_id": "secret-id"}
    ))

    assert oci.calls == [
        ("logan", ("namespace", "compartment", [{"entityId": "entity"}])),
        ("opsi", ("insight",)),
        ("pdb", ("pdb-id", "DIAGNOSTICS_AND_MANAGEMENT")),
        ("cdb", ("cdb-id", "DIAGNOSTICS_AND_MANAGEMENT", {"can_disable_all_pdbs": True})),
        ("secret", ("secret-id",)),
    ]


def test_expired_terminal_cleanup_evidence_is_purged_but_incomplete_state_is_retained(tmp_path) -> None:
    store = FleetStateStore(tmp_path / "state.sqlite")
    expired = "2026-08-03T00:00:00+00:00"
    store.save_cleanup_state(
        run_id="complete", cleanup_plan_id="plan-complete",
        state={"action_states": {"action": "complete"}, "evidence": {"retained_until": expired}},
    )
    store.save_cleanup_state(
        run_id="failed", cleanup_plan_id="plan-failed",
        state={"action_states": {"action": "failed"}, "evidence": {"retained_until": expired}},
    )

    assert store.purge_expired_cleanup_evidence(now=datetime(2026, 8, 4, tzinfo=UTC)) == 1
    complete = store.load_cleanup_state(run_id="complete", cleanup_plan_id="plan-complete")
    incomplete = store.load_cleanup_state(run_id="failed", cleanup_plan_id="plan-failed")
    assert complete == {"action_states": {"action": "complete"}}
    assert incomplete == {"action_states": {"action": "failed"}, "evidence": {"retained_until": expired}}


def test_adapter_covers_owned_endpoint_network_and_test_database_deletes_and_handoffs_db_users(tmp_path) -> None:
    class Oci:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def delete_opsi_private_endpoint(self, value: str) -> None:
            self.calls.append(("opsi-endpoint", value))

        def delete_db_management_private_endpoint(self, value: str) -> None:
            self.calls.append(("dbm-endpoint", value))

        def delete_data_safe_private_endpoint(self, value: str) -> None:
            self.calls.append(("data-safe-endpoint", value))

        def delete_run_owned_subnet(self, value: str) -> None:
            self.calls.append(("subnet", value))

        def delete_run_owned_vcn(self, value: str) -> None:
            self.calls.append(("vcn", value))

        def delete_run_owned_route_table(self, value: str) -> None:
            self.calls.append(("route-table", value))

        def delete_run_owned_security_list(self, value: str) -> None:
            self.calls.append(("security-list", value))

        def delete_run_owned_dbcs_test_database(self, value: str) -> None:
            self.calls.append(("dbcs", value))

        def delete_run_owned_autonomous_test_database(self, value: str) -> None:
            self.calls.append(("adb", value))

    oci = Oci()
    adapter = OciCleanupOperations(oci)
    for action in (
        CleanupAction("delete-endpoint", "db", "dbcs", "opsi-private-endpoint", "opsi", {"endpoint_id": "opsi", "unused": True}),
        CleanupAction("delete-endpoint", "db", "dbcs", "dbm-private-endpoint", "dbm", {"endpoint_id": "dbm", "unused": True}),
        CleanupAction("delete-endpoint", "db", "dbcs", "data-safe-private-endpoint", "data-safe", {"endpoint_id": "data-safe", "unused": True}),
        CleanupAction("delete-network", "db", "dbcs", "subnet", "subnet", {"network_id": "subnet", "unused": True}),
        CleanupAction("delete-network", "db", "dbcs", "vcn", "vcn", {"network_id": "vcn", "unused": True}),
        CleanupAction("delete-network", "db", "dbcs", "route-table", "route-table", {"network_id": "route-table", "unused": True}),
        CleanupAction("delete-network", "db", "dbcs", "security-list", "security-list", {"network_id": "security-list", "unused": True}),
        CleanupAction("delete-test-database", "db", "dbcs", "dbcs-test-database", "dbcs", {"database_id": "dbcs"}),
        CleanupAction("delete-test-database", "adb", "adb", "adb-test-database", "adb", {"database_id": "adb", "database_family": "autonomous"}),
    ):
        adapter.execute_cleanup(action)
    with pytest.raises(CleanupHandoffRequired, match="database-user"):
        adapter.execute_cleanup(CleanupAction("delete-database-user", "db", "dbcs", "database-user", "user"))

    assert oci.calls == [
        ("opsi-endpoint", "opsi"), ("dbm-endpoint", "dbm"), ("data-safe-endpoint", "data-safe"),
        ("subnet", "subnet"), ("vcn", "vcn"), ("route-table", "route-table"),
        ("security-list", "security-list"), ("dbcs", "dbcs"), ("adb", "adb"),
    ]


def test_missing_dbm_arguments_and_database_users_are_handed_off_not_marked_complete(tmp_path) -> None:
    class Oci:
        def disable_dbm_cdb(self, *args, **kwargs) -> None:
            raise AssertionError("missing feature must be rejected before OCI invocation")

    plan = CleanupPlan(
        run_id="run-1", source_plan_id="source", deployment_mode=DeploymentMode.POC,
        actions=(
            CleanupAction("disable-dbm-cdb", "db", "dbcs", "dbm-cdb", "database", {"database_id": "database"}),
            CleanupAction("delete-database-user", "db", "dbcs", "database-user", "monitoring-user"),
        ),
    )
    result = CleanupExecutor(plan, FleetStateStore(tmp_path / "state.sqlite"), OciCleanupOperations(Oci())).execute(
        approved_plan_id=plan.plan_id
    )

    assert result.partial and not result.complete
    # A manual cleanup path without a signing writer is fail-closed; an
    # unsigned checkpoint is not resumable handoff evidence.
    assert set(result.action_states.values()) == {"failed"}


def test_handed_off_cleanup_retries_with_approved_adapter_then_completed_rerun_is_noop(tmp_path) -> None:
    plan = CleanupPlan(
        run_id="run-1", source_plan_id="source", deployment_mode=DeploymentMode.POC,
        actions=(CleanupAction("delete-database-user", "db", "dbcs", "database-user", "monitoring-user"),),
    )
    store = FleetStateStore(tmp_path / "state.sqlite")

    class Handoff:
        def execute_cleanup(self, action) -> None:
            raise CleanupHandoffRequired("waiting for approved SQL adapter")

    first = CleanupExecutor(
        plan, store, Handoff(),
        handoff_writer=CleanupHandoffPacketWriter(tmp_path / "handoffs", signing_key=b"cleanup-test-key"),
    ).execute(approved_plan_id=plan.plan_id)
    assert set(first.action_states.values()) == {"handed-off"}

    completed = _Operations()
    second = CleanupExecutor(plan, store, completed).execute(approved_plan_id=plan.plan_id)
    assert second.complete
    assert completed.calls == [("delete-database-user", "monitoring-user")]

    repeated = _Operations()
    third = CleanupExecutor(plan, store, repeated).execute(approved_plan_id=plan.plan_id)
    assert third.complete
    assert repeated.calls == []


def test_only_unambiguous_not_found_is_idempotent_not_authorized_or_not_found_is_failed(tmp_path) -> None:
    plan = CleanupPlan(
        run_id="run-1", source_plan_id="source", deployment_mode=DeploymentMode.POC,
        actions=(CleanupAction("disable-opsi", "db", "dbcs", "opsi-insight", "insight"),),
    )
    found = CleanupExecutor(
        plan, FleetStateStore(tmp_path / "found.sqlite"), _Operations({"insight": OciNotFound("404")})
    ).execute(approved_plan_id=plan.plan_id)
    ambiguous = CleanupExecutor(
        plan, FleetStateStore(tmp_path / "ambiguous.sqlite"), _Operations({"insight": OciAuthError("NotAuthorizedOrNotFound")})
    ).execute(approved_plan_id=plan.plan_id)

    assert found.complete
    assert ambiguous.partial and set(ambiguous.action_states.values()) == {"failed"}


@pytest.mark.parametrize(
    ("resource_type", "expected_operation"),
    [
        ("opsi-private-endpoint", "delete-endpoint"),
        ("dbm-private-endpoint", "delete-endpoint"),
        ("data-safe-private-endpoint", "delete-endpoint"),
        ("subnet", "delete-network"),
        ("vcn", "delete-network"),
        ("route-table", "delete-network"),
        ("security-list", "delete-network"),
        ("unknown-private-endpoint", "handoff-cleanup"),
        ("service-gateway", "handoff-cleanup"),
        ("subnet-security-list-rule", "handoff-cleanup"),
        ("route-table-association", "handoff-cleanup"),
        ("opsi-private-endpoint-shadow", "handoff-cleanup"),
        ("custom-vcn-endpoint", "handoff-cleanup"),
    ],
)
def test_planner_emits_only_concrete_endpoint_network_actions_or_explicit_handoffs(
    resource_type: str, expected_operation: str
) -> None:
    class Oci:
        def delete_opsi_private_endpoint(self, _value) -> None: ...
        def delete_db_management_private_endpoint(self, _value) -> None: ...
        def delete_data_safe_private_endpoint(self, _value) -> None: ...
        def delete_run_owned_subnet(self, _value) -> None: ...
        def delete_run_owned_vcn(self, _value) -> None: ...
        def delete_run_owned_route_table(self, _value) -> None: ...
        def delete_run_owned_security_list(self, _value) -> None: ...

    plan = _plan(_target("db"))
    manifest = RunManifest(
        "run-1", plan.plan_id,
        (TargetManifest("db", resources=(
            ResourceRecord(resource_type, "resource", ResourceOwnership.CREATED, True, {"unused": True}),
        )),),
    )

    cleanup = CleanupPlanner(plan, manifest).build()

    assert cleanup.actions[0].operation == expected_operation
    if expected_operation == "handoff-cleanup":
        assert "handoff_reason" in cleanup.actions[0].arguments
        with pytest.raises(CleanupHandoffRequired):
            OciCleanupOperations(Oci()).execute_cleanup(cleanup.actions[0])
    else:
        OciCleanupOperations(Oci()).execute_cleanup(cleanup.actions[0])


def test_signed_cleanup_completion_evidence_closes_original_handoff_without_resource_ref(tmp_path: Path) -> None:
    plan, store, writer, issued = _issued_cleanup_handoff(tmp_path)
    first = _execution_for_handoff(plan, store, writer)
    assert set(first.action_states.values()) == {"handed-off"}
    assert "ocid1" not in issued.read_text(encoding="utf-8")
    completion = writer.write_completion(issued, attestation="Operator removed the approved endpoint", result="completed")

    completed = CleanupHandoffEvidenceImporter(store, plan, signing_key=b"test-key").import_packet(
        completion, approved_plan_id=plan.plan_id
    )

    assert completed.complete
    assert "ocid1" not in completion.read_text(encoding="utf-8")
    assert CleanupHandoffEvidenceImporter(store, plan, signing_key=b"test-key").import_packet(
        completion, approved_plan_id=plan.plan_id
    ).complete


def _issued_cleanup_handoff(tmp_path: Path) -> tuple[CleanupPlan, FleetStateStore, CleanupHandoffPacketWriter, Path]:
    action = CleanupAction(
        "handoff-cleanup", "db", "dbcs", "unknown-private-endpoint",
        "ocid1.privateendpoint.oc1..sensitive", {"handoff_reason": "unsupported endpoint family"},
    )
    plan = CleanupPlan("run-1", "source", DeploymentMode.POC, (action,))
    store = FleetStateStore(tmp_path / "state.sqlite")
    writer = CleanupHandoffPacketWriter(tmp_path / "handoffs", signing_key=b"test-key")
    _execution_for_handoff(plan, store, writer)
    issued = next((tmp_path / "handoffs").glob("*.cleanup-handoff.json"))
    return plan, store, writer, issued


def _execution_for_handoff(
    plan: CleanupPlan, store: FleetStateStore, writer: CleanupHandoffPacketWriter
):
    return CleanupExecutor(plan, store, OciCleanupOperations(object()), handoff_writer=writer).execute(
        approved_plan_id=plan.plan_id
    )


def _resign_cleanup_completion(path: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    evidence = document["evidence"]
    document["signature"] = hmac.new(
        b"test-key", json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize("field,value", [
    ("cleanup_plan_id", "wrong-plan"),
    ("run_id", "wrong-run"),
    ("action_id", "wrong-action"),
    ("action_kind", "wrong-kind"),
    ("issued_handoff_ref", "sha256:wrong-ref"),
    ("issued_packet_digest", "wrong-digest"),
])
def test_cleanup_completion_rejects_wrong_signed_binding(tmp_path: Path, field: str, value: str) -> None:
    plan, store, writer, issued = _issued_cleanup_handoff(tmp_path)
    _execution_for_handoff(plan, store, writer)
    completion = writer.write_completion(issued, attestation="Operator completed cleanup", result="completed")
    document = json.loads(completion.read_text(encoding="utf-8"))
    document["evidence"][field] = value
    completion.write_text(json.dumps(document), encoding="utf-8")
    _resign_cleanup_completion(completion)

    with pytest.raises(ValueError, match="cleanup completion evidence"):
        CleanupHandoffEvidenceImporter(store, plan, signing_key=b"test-key").import_packet(
            completion, approved_plan_id=plan.plan_id
        )


def test_cleanup_completion_rejects_instruction_only_invalid_signature_and_repaired_plan(tmp_path: Path) -> None:
    plan, store, writer, issued = _issued_cleanup_handoff(tmp_path)
    _execution_for_handoff(plan, store, writer)
    importer = CleanupHandoffEvidenceImporter(store, plan, signing_key=b"test-key")
    with pytest.raises(ValueError, match="completion evidence envelope"):
        importer.import_packet(issued, approved_plan_id=plan.plan_id)
    completion = writer.write_completion(issued, attestation="Operator completed cleanup", result="completed")
    document = json.loads(completion.read_text(encoding="utf-8"))
    document["evidence"]["attestation"] = "tampered"
    completion.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="signature"):
        importer.import_packet(completion, approved_plan_id=plan.plan_id)

    completion = writer.write_completion(issued, attestation="Operator completed cleanup", result="completed")

    repaired = CleanupPlan(
        plan.run_id, plan.source_plan_id, plan.deployment_mode,
        (CleanupAction("handoff-cleanup", "db", "dbcs", "unknown-private-endpoint", "new-ref", {"handoff_reason": "repaired"}),),
    )
    assert repaired.plan_id != plan.plan_id
    with pytest.raises(ValueError, match="different plan"):
        CleanupHandoffEvidenceImporter(store, repaired, signing_key=b"test-key").import_packet(
            completion, approved_plan_id=repaired.plan_id
        )
