from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from pathlib import Path

import pytest

from dbman_opsi.fleet import FleetPlan, PhaseCheckpoint, PhaseState, ReadinessVerdict, ResourceEffect, ResourceOwnership, ResourceRecord, RunManifest, TargetManifest, TargetPlan, TargetState
from dbman_opsi.fleet_executor import FleetOnboardingExecutor, PhaseOutcome
from dbman_opsi.fleet_state import RunLeaseError
from dbman_opsi.fleet_handoff import HandoffEvidenceImporter, HandoffPacketWriter
from dbman_opsi.fleet_offboarding import CleanupExecutor, CleanupPlanner, OciCleanupOperations
from dbman_opsi.fleet_state import FleetStateStore
from dbman_opsi.fleet_status import fleet_status


def _plan(*targets: TargetPlan) -> FleetPlan:
    return FleetPlan(profile="DEFAULT", region="eu-frankfurt-1", targets=targets)


def _target(name: str, *, dependencies: tuple[str, ...] = ()) -> TargetPlan:
    return TargetPlan(
        target_id=name,
        name=name,
        kind="dbcs",
        region="eu-frankfurt-1",
        dependencies=dependencies,
    )


def _handlers(calls: list[tuple[str, str]], **overrides):
    def handler(phase):
        def run(target):
            calls.append((phase, target.target_id))
            result = overrides.get((phase, target.target_id), overrides.get(phase))
            if isinstance(result, BaseException):
                raise result
            if callable(result):
                return result(target)
            return result

        return run

    return {phase: handler(phase) for phase in FleetOnboardingExecutor.PHASES}


def test_exact_plan_approval_is_required_before_a_checkpoint_write(
    tmp_path: Path,
) -> None:
    plan = _plan(_target("db"))
    store = FleetStateStore(tmp_path / "state.sqlite")
    executor = FleetOnboardingExecutor(plan, store, phase_handlers={})
    with pytest.raises(ValueError, match="approval does not match"):
        executor.execute(approved_plan_id="wrong")
    assert store.find_by_plan(plan.plan_id) == ()


def test_missing_phase_handler_is_durable_handoff_not_silent_success(tmp_path: Path) -> None:
    plan = _plan(_target("db"))
    run = FleetOnboardingExecutor(plan, FleetStateStore(tmp_path / "state.sqlite"), phase_handlers={}).execute(approved_plan_id=plan.plan_id)
    checkpoint = run.target("db").checkpoint("prerequisites")
    assert checkpoint and checkpoint.state is PhaseState.HANDED_OFF
    assert "no approved lifecycle handler" in (checkpoint.message or "")
    assert run.target("db").readiness is ReadinessVerdict.HANDED_OFF


def test_success_checkpoints_each_phase_and_requires_collection_proof_for_ready(
    tmp_path: Path,
) -> None:
    plan = _plan(_target("db"))
    calls: list[tuple[str, str]] = []
    run = FleetOnboardingExecutor(
        plan,
        FleetStateStore(tmp_path / "state.sqlite"),
        phase_handlers=_handlers(
            calls, validation=PhaseOutcome(readiness=ReadinessVerdict.READY)
        ),
    ).execute(approved_plan_id=plan.plan_id)
    assert [phase for phase, _ in calls] == list(FleetOnboardingExecutor.PHASES)
    assert {item.phase for item in run.target("db").checkpoints} == set(
        FleetOnboardingExecutor.PHASES
    )
    assert run.target("db").readiness is ReadinessVerdict.READY


def test_phase_effects_are_atomically_durable_with_the_checkpoint(tmp_path: Path) -> None:
    plan = _plan(_target("db"))
    store = FleetStateStore(tmp_path / "state.sqlite")
    effect = ResourceRecord("dbm-cdb", "private-ref", ResourceOwnership.OWNED, True)
    run = FleetOnboardingExecutor(
        plan, store,
        phase_handlers=_handlers([], dbm=PhaseOutcome(resources=(effect,)), validation=PhaseOutcome(readiness=ReadinessVerdict.READY)),
    ).execute(approved_plan_id=plan.plan_id, run_id="effects")
    persisted = store.load("effects")
    assert persisted == run
    resource = run.target("db").resources[0]
    assert resource.effect is ResourceEffect.CREATED
    assert resource.lifecycle_owned and resource.cleanup_allowed


def test_onboarded_named_credential_cleanup_uses_created_credential_id(tmp_path: Path) -> None:
    plan = _plan(_target("db"))
    created_id = "ocid1.namedcredential.oc1..created"
    credential = ResourceRecord(
        "named-credential", created_id, ResourceOwnership.OWNED, True,
        attributes={"credential_id": created_id}, effect=ResourceEffect.CREATED,
    )
    run = FleetOnboardingExecutor(
        plan,
        FleetStateStore(tmp_path / "state.sqlite"),
        phase_handlers=_handlers([], credentials=PhaseOutcome(resources=(credential,)), validation=PhaseOutcome(readiness=ReadinessVerdict.READY)),
    ).execute(approved_plan_id=plan.plan_id, run_id="credential-cleanup")
    cleanup = CleanupPlanner(plan, run).build()
    action = next(item for item in cleanup.actions if item.operation == "delete-named-credential")

    class Oci:
        deleted: list[str] = []
        def delete_named_credential(self, credential_id: str) -> None:
            self.deleted.append(credential_id)

    oci = Oci()
    OciCleanupOperations(oci).execute_cleanup(action)
    assert oci.deleted == [created_id]


def test_active_lease_rejects_second_actor_before_any_phase_handler_runs(tmp_path: Path) -> None:
    plan = _plan(_target("db"))
    store = FleetStateStore(tmp_path / "state.sqlite")
    assert store.acquire_lease(run_id="same-run", plan_id=plan.plan_id, owner="first")
    calls: list[tuple[str, str]] = []
    with pytest.raises(RunLeaseError):
        FleetOnboardingExecutor(plan, store, phase_handlers=_handlers(calls)).execute(approved_plan_id=plan.plan_id, run_id="same-run")
    assert calls == []


def test_heartbeat_fences_blocked_handler_and_prevents_a_second_actor_egress(tmp_path: Path) -> None:
    """A short lease remains live while an OCI-like handler is blocked."""
    plan = _plan(_target("db"))
    store = FleetStateStore(tmp_path / "state.sqlite")
    entered, release = threading.Event(), threading.Event()
    first_calls: list[str] = []
    second_calls: list[tuple[str, str]] = []

    def blocked(_target):
        first_calls.append("egress")
        entered.set()
        assert release.wait(5)

    first = FleetOnboardingExecutor(
        plan,
        store,
        phase_handlers=_handlers([], prerequisites=blocked),
        lease_ttl_seconds=0.5,
        lease_heartbeat_interval=0.05,
    )
    failure: list[BaseException] = []

    def run_first() -> None:
        try:
            first.execute(approved_plan_id=plan.plan_id, run_id="same-run")
        except BaseException as exc:  # surfaced below to keep the test thread small
            failure.append(exc)

    worker = threading.Thread(target=run_first)
    worker.start()
    assert entered.wait(1)

    def lease_expiry() -> float:
        with store.transaction() as connection:
            row = connection.execute(
                "SELECT expires_at FROM fleet_run_leases WHERE run_id = ?",
                ("same-run",),
            ).fetchone()
        assert row is not None
        return float(row["expires_at"])

    observed_expiry = lease_expiry()
    renewal_deadline = time.monotonic() + 2
    renewed_expiry = observed_expiry
    while renewed_expiry <= observed_expiry and time.monotonic() < renewal_deadline:
        time.sleep(0.01)
        renewed_expiry = lease_expiry()
    assert renewed_expiry > observed_expiry
    while time.time() <= observed_expiry:
        time.sleep(0.01)

    with pytest.raises(RunLeaseError):
        FleetOnboardingExecutor(
            plan, store, phase_handlers=_handlers(second_calls), lease_ttl_seconds=0.5
        ).execute(approved_plan_id=plan.plan_id, run_id="same-run")
    assert first_calls == ["egress"]
    assert second_calls == []
    release.set()
    worker.join(2)
    assert failure == []


def test_lost_lease_cannot_checkpoint_a_stale_handler_result(tmp_path: Path) -> None:
    plan = _plan(_target("db"))
    store = FleetStateStore(tmp_path / "state.sqlite")
    entered, release = threading.Event(), threading.Event()

    def blocked(_target):
        entered.set()
        assert release.wait(2)
        return PhaseOutcome(message="must not persist")

    executor = FleetOnboardingExecutor(
        plan,
        store,
        phase_handlers=_handlers([], prerequisites=blocked),
        lease_ttl_seconds=0.06,
        lease_heartbeat_interval=0.01,
    )
    result: list[BaseException] = []

    def run() -> None:
        try:
            executor.execute(approved_plan_id=plan.plan_id, run_id="lost-run")
        except BaseException as exc:
            result.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(1)
    # Simulate a successful conditional takeover by another actor.  The old
    # heartbeat then loses its owner-fenced renewal before the handler returns.
    with store.transaction() as connection:
        connection.execute("UPDATE fleet_run_leases SET expires_at = 0 WHERE run_id = ?", ("lost-run",))
    assert store.acquire_lease(run_id="lost-run", plan_id=plan.plan_id, owner="other", ttl_seconds=1)
    time.sleep(0.05)
    release.set()
    worker.join(2)
    assert len(result) == 1 and isinstance(result[0], RunLeaseError)
    checkpoint = store.load("lost-run").target("db").checkpoint("prerequisites")  # type: ignore[union-attr]
    assert checkpoint and checkpoint.state is PhaseState.RUNNING
    assert checkpoint.message != "must not persist"


def test_existing_409_is_idempotent_and_transient_errors_retry(tmp_path: Path) -> None:
    plan = _plan(_target("db"))
    attempts = {"dbm": 0, "opsi": 0}
    calls: list[tuple[str, str]] = []

    def dbm(_target):
        attempts["dbm"] += 1
        if attempts["dbm"] == 1:
            raise RuntimeError("409 Conflict: already exists")

    def opsi(_target):
        attempts["opsi"] += 1
        if attempts["opsi"] < 3:
            raise RuntimeError("429 too many requests")

    run = FleetOnboardingExecutor(
        plan,
        FleetStateStore(tmp_path / "state.sqlite"),
        phase_handlers=_handlers(
            calls,
            dbm=dbm,
            opsi=opsi,
            validation=PhaseOutcome(readiness=ReadinessVerdict.READY),
        ),
        sleeper=lambda _: None,
        random_float=lambda: 0,
    ).execute(approved_plan_id=plan.plan_id)
    assert attempts == {"dbm": 1, "opsi": 3}
    assert run.target("db").checkpoint("opsi").state is PhaseState.COMPLETE


def test_authorization_failure_opens_circuit_and_blocks_independent_targets(
    tmp_path: Path,
) -> None:
    plan = _plan(_target("a"), _target("b"))
    calls: list[tuple[str, str]] = []
    run = FleetOnboardingExecutor(
        plan,
        FleetStateStore(tmp_path / "state.sqlite"),
        phase_handlers=_handlers(
            calls, prerequisites=RuntimeError("401 NotAuthorizedOrNotFound")
        ),
    ).execute(approved_plan_id=plan.plan_id)
    assert run.target("a").readiness is ReadinessVerdict.BLOCKED
    assert run.target("b").readiness is ReadinessVerdict.BLOCKED
    assert len(calls) == 1


def test_interruption_is_checkpointed_as_retryable_and_resumes(tmp_path: Path) -> None:
    plan = _plan(_target("db"))
    store = FleetStateStore(tmp_path / "state.sqlite")
    first_calls: list[tuple[str, str]] = []
    first = FleetOnboardingExecutor(
        plan, store, phase_handlers=_handlers(first_calls, dbm=KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        first.execute(approved_plan_id=plan.plan_id, run_id="run-1")
    paused = store.load("run-1")
    assert (
        paused and paused.target("db").checkpoint("dbm").state is PhaseState.RETRYABLE
    )
    second_calls: list[tuple[str, str]] = []
    resumed = FleetOnboardingExecutor(
        plan, store, phase_handlers=_handlers(second_calls)
    ).execute(approved_plan_id=plan.plan_id, run_id="run-1")
    assert ("prerequisites", "db") not in second_calls
    assert resumed.target("db").checkpoint("dbm").state is PhaseState.COMPLETE


def test_resume_reconciles_an_egress_interrupted_before_checkpoint_as_reused(tmp_path: Path) -> None:
    """A post-write interruption must never grant cleanup ownership on resume."""
    plan = _plan(_target("db"))
    store = FleetStateStore(tmp_path / "state.sqlite")
    calls = 0

    def dbm(_target):
        nonlocal calls
        calls += 1
        if calls == 1:
            # Fake OCI performed the enable, then the process died before it
            # could return a phase outcome/checkpoint.
            raise KeyboardInterrupt()
        return PhaseOutcome(resources=(ResourceRecord(
            "dbm-cdb", "db", ResourceOwnership.REUSED, False,
            effect=ResourceEffect.REUSED,
        ),))

    with pytest.raises(KeyboardInterrupt):
        FleetOnboardingExecutor(plan, store, phase_handlers=_handlers([], dbm=dbm)).execute(
            approved_plan_id=plan.plan_id, run_id="crash-after-egress"
        )
    resumed = FleetOnboardingExecutor(plan, store, phase_handlers=_handlers([], dbm=dbm)).execute(
        approved_plan_id=plan.plan_id, run_id="crash-after-egress"
    )
    record = next(item for item in resumed.target("db").resources if item.resource_type == "dbm-cdb")
    assert calls == 2
    assert record.ownership is ResourceOwnership.REUSED
    assert not record.enabled_by_run and not record.cleanup_allowed


def test_failed_cdb_blocks_pdb_but_keeps_unrelated_target_running(
    tmp_path: Path,
) -> None:
    plan = _plan(
        _target("cdb"), _target("pdb", dependencies=("cdb",)), _target("independent")
    )
    calls: list[tuple[str, str]] = []

    def cdb_failure(target):
        if target.target_id == "cdb":
            raise RuntimeError("500 server error")

    run = FleetOnboardingExecutor(
        plan,
        FleetStateStore(tmp_path / "state.sqlite"),
        phase_handlers=_handlers(
            calls,
            dbm=cdb_failure,
            validation=PhaseOutcome(readiness=ReadinessVerdict.READY),
        ),
        retries=0,
    ).execute(approved_plan_id=plan.plan_id)
    assert run.target("pdb").readiness is ReadinessVerdict.BLOCKED
    assert (
        run.target("independent").checkpoint("validation").state is PhaseState.COMPLETE
    )


def test_handoff_evidence_import_completes_only_verified_redacted_packet(
    tmp_path: Path,
) -> None:
    plan = _plan(_target("external"))
    store = FleetStateStore(tmp_path / "state.sqlite")
    writer = HandoffPacketWriter(tmp_path / "handoffs", signing_key=b"test-key")
    run = FleetOnboardingExecutor(
        plan,
        store,
        phase_handlers=_handlers(
            [],
            **{"db-host-automation": PhaseOutcome.handoff("no approved host access")},
        ),
        handoff_writer=writer,
    ).execute(approved_plan_id=plan.plan_id, run_id="run-1")
    checkpoint = run.target("external").checkpoint("db-host-automation")
    assert checkpoint and checkpoint.state is PhaseState.HANDED_OFF
    issued_packet = next((tmp_path / "handoffs").glob("*.handoff.json"))
    completion = writer.write_completion(
        issued_packet, attestation="DBA completed approved work", result="completed"
    )
    updated = HandoffEvidenceImporter(
        store, plan, signing_key=b"test-key"
    ).import_packet(completion, approved_plan_id=plan.plan_id)
    assert (
        updated.target("external").checkpoint("db-host-automation").state
        is PhaseState.COMPLETE
    )


def test_imported_handoff_with_future_phases_resumes_from_next_phase(tmp_path: Path) -> None:
    plan = _plan(_target("external"))
    store = FleetStateStore(tmp_path / "state.sqlite")
    writer = HandoffPacketWriter(tmp_path / "handoffs", signing_key=b"test-key")
    first = FleetOnboardingExecutor(
        plan,
        store,
        phase_handlers=_handlers([], prerequisites=PhaseOutcome.handoff("operator action")),
        handoff_writer=writer,
    ).execute(approved_plan_id=plan.plan_id, run_id="run-future-phases")
    issued = next((tmp_path / "handoffs").glob("*.handoff.json"))
    completion = writer.write_completion(issued, attestation="completed", result="completed")
    imported = HandoffEvidenceImporter(store, plan, signing_key=b"test-key").import_packet(
        completion, approved_plan_id=plan.plan_id
    )
    assert imported.target("external").state is TargetState.PENDING
    assert imported.target("external").checkpoint("prerequisites").state is PhaseState.COMPLETE

    calls: list[tuple[str, str]] = []
    resumed = FleetOnboardingExecutor(plan, store, phase_handlers=_handlers(calls)).execute(
        approved_plan_id=plan.plan_id, run_id="run-future-phases"
    )
    assert ("prerequisites", "external") not in calls
    assert ("dbm", "external") in calls
    assert resumed.target("external").state is TargetState.COMPLETE


def test_resource_creating_handoff_requires_and_persists_cleanup_identity(tmp_path: Path) -> None:
    target = TargetPlan(
        "database",
        "database",
        "autonomous",
        "eu-frankfurt-1",
        services=("datasafe",),
    )
    plan = _plan(target)
    store = FleetStateStore(tmp_path / "state.sqlite")
    writer = HandoffPacketWriter(tmp_path / "handoffs", signing_key=b"test-key")
    FleetOnboardingExecutor(
        plan,
        store,
        phase_handlers=_handlers(
            [], datasafe=PhaseOutcome.handoff("approved Data Safe registration")
        ),
        handoff_writer=writer,
    ).execute(approved_plan_id=plan.plan_id, run_id="run-resource-handoff")
    issued = next((tmp_path / "handoffs").glob("*.handoff.json"))
    with pytest.raises(ValueError, match="resource"):
        writer.write_completion(
            issued,
            attestation="registration completed",
            result="completed",
        )
    completion = writer.write_completion(
        issued,
        attestation="registration completed",
        result="completed",
        resource_effects=(
            {
                "resource_type": "datasafe-target",
                "resource_ref": "datasafe-target-id",
                "effect": "created",
                "attributes": {"target_database_id": "datasafe-target-id"},
            },
        ),
    )
    imported = HandoffEvidenceImporter(
        store, plan, signing_key=b"test-key"
    ).import_packet(completion, approved_plan_id=plan.plan_id)
    resource = imported.target("database").resources[0]
    assert resource.resource_type == "datasafe-target"
    assert resource.cleanup_allowed
    cleanup = CleanupPlanner(plan, imported).build()
    assert [action.operation for action in cleanup.actions] == [
        "unregister-data-safe"
    ]


def test_credentials_handoff_references_imports_and_cleans_owned_secret_once(
    tmp_path: Path,
) -> None:
    plan = _plan(_target("database"))
    store = FleetStateStore(tmp_path / "state.sqlite")
    writer = HandoffPacketWriter(tmp_path / "handoffs", signing_key=b"test-key")
    FleetOnboardingExecutor(
        plan,
        store,
        phase_handlers=_handlers(
            [], credentials=PhaseOutcome.handoff("approved credential work")
        ),
        handoff_writer=writer,
    ).execute(approved_plan_id=plan.plan_id, run_id="run-credential-handoff")
    issued = next((tmp_path / "handoffs").glob("*.handoff.json"))
    assert writer.reference_for(issued).startswith("sha256:")
    completion = writer.write_completion(
        issued,
        attestation="credential work completed",
        result="completed",
        resource_effects=(
            {
                "resource_type": "vault-secret",
                "resource_ref": "vault-secret-id",
                "effect": "created",
                "attributes": {"secret_id": "vault-secret-id"},
            },
        ),
    )
    imported = HandoffEvidenceImporter(
        store, plan, signing_key=b"test-key"
    ).import_packet(completion, approved_plan_id=plan.plan_id)
    cleanup = CleanupPlanner(plan, imported).build()
    assert [action.operation for action in cleanup.actions] == ["delete-secret"]

    class _VaultOci:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def schedule_run_owned_secret_deletion(self, secret_id: str) -> None:
            self.deleted.append(secret_id)

    oci = _VaultOci()
    executor = CleanupExecutor(cleanup, store, OciCleanupOperations(oci))
    assert executor.execute(approved_plan_id=cleanup.plan_id).complete
    assert executor.execute(approved_plan_id=cleanup.plan_id).complete
    assert oci.deleted == ["vault-secret-id"]


def _resign(path: Path, *, key: bytes = b"test-key") -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    payload = document["evidence"]
    document["signature"] = hmac.new(
        key,
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    path.write_text(json.dumps(document), encoding="utf-8")


def test_handoff_instruction_packet_is_not_completion_evidence(tmp_path: Path) -> None:
    writer = HandoffPacketWriter(tmp_path, signing_key=b"test-key")
    plan = _plan(_target("external"))
    store = FleetStateStore(tmp_path / "state.sqlite")
    run = FleetOnboardingExecutor(
        plan,
        store,
        phase_handlers=_handlers(
            [], **{"db-host-automation": PhaseOutcome.handoff("approved work")}
        ),
        handoff_writer=writer,
    ).execute(approved_plan_id=plan.plan_id, run_id="run-1")
    issued = next(tmp_path.glob("*.handoff.json"))
    with pytest.raises(ValueError, match="completion evidence"):
        HandoffEvidenceImporter(store, plan, signing_key=b"test-key").import_packet(
            issued, approved_plan_id=plan.plan_id
        )
    assert (
        run.target("external").checkpoint("db-host-automation").state
        is PhaseState.HANDED_OFF
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("issued_handoff_ref", "sha256:wrong"),
        ("issued_packet_digest", "wrong-digest"),
        ("run_id", "wrong-run"),
        ("plan_id", "wrong-plan"),
        ("target_handle", "wrong-target"),
        ("phase", "wrong-phase"),
    ],
)
def test_handoff_evidence_rejects_wrong_binding(
    tmp_path: Path, field: str, value: str
) -> None:
    plan = _plan(_target("external"))
    store = FleetStateStore(tmp_path / "state.sqlite")
    writer = HandoffPacketWriter(tmp_path / "handoffs", signing_key=b"test-key")
    FleetOnboardingExecutor(
        plan,
        store,
        phase_handlers=_handlers(
            [], **{"db-host-automation": PhaseOutcome.handoff("approved work")}
        ),
        handoff_writer=writer,
    ).execute(approved_plan_id=plan.plan_id, run_id="run-1")
    evidence = writer.write_completion(
        next((tmp_path / "handoffs").glob("*.handoff.json")),
        attestation="DBA attests completion",
        result="completed",
    )
    document = json.loads(evidence.read_text(encoding="utf-8"))
    document["evidence"][field] = value
    evidence.write_text(json.dumps(document), encoding="utf-8")
    _resign(evidence)
    with pytest.raises(ValueError, match="handoff evidence"):
        HandoffEvidenceImporter(store, plan, signing_key=b"test-key").import_packet(
            evidence, approved_plan_id=plan.plan_id
        )


@pytest.mark.parametrize(
    "field,value", [("attestation", ""), ("result", "instructions-issued")]
)
def test_handoff_evidence_requires_attestation_and_allowlisted_result(
    tmp_path: Path, field: str, value: str
) -> None:
    plan = _plan(_target("external"))
    store = FleetStateStore(tmp_path / "state.sqlite")
    writer = HandoffPacketWriter(tmp_path / "handoffs", signing_key=b"test-key")
    FleetOnboardingExecutor(
        plan,
        store,
        phase_handlers=_handlers(
            [], **{"db-host-automation": PhaseOutcome.handoff("approved work")}
        ),
        handoff_writer=writer,
    ).execute(approved_plan_id=plan.plan_id, run_id="run-1")
    evidence = writer.write_completion(
        next((tmp_path / "handoffs").glob("*.handoff.json")),
        attestation="DBA attests completion",
        result="completed",
    )
    document = json.loads(evidence.read_text(encoding="utf-8"))
    document["evidence"][field] = value
    evidence.write_text(json.dumps(document), encoding="utf-8")
    _resign(evidence)
    with pytest.raises(ValueError, match="attestation/result"):
        HandoffEvidenceImporter(store, plan, signing_key=b"test-key").import_packet(
            evidence, approved_plan_id=plan.plan_id
        )


def test_handoff_packet_uses_opaque_target_handle_for_oci_ids(tmp_path: Path) -> None:
    target_id = "ocid1.database.oc1..private-target"
    plan = _plan(
        TargetPlan(
            target_id=target_id, name="external", kind="dbcs", region="eu-frankfurt-1"
        )
    )
    store = FleetStateStore(tmp_path / "state.sqlite")
    writer = HandoffPacketWriter(tmp_path / "handoffs", signing_key=b"test-key")
    FleetOnboardingExecutor(
        plan,
        store,
        phase_handlers=_handlers(
            [], **{"db-host-automation": PhaseOutcome.handoff("approved work")}
        ),
        handoff_writer=writer,
    ).execute(approved_plan_id=plan.plan_id, run_id="run-1")
    issued = next((tmp_path / "handoffs").glob("*.handoff.json"))
    assert target_id not in issued.name and target_id not in issued.read_text(
        encoding="utf-8"
    )
    evidence = writer.write_completion(
        issued, attestation="DBA attests completion", result="completed"
    )
    assert (
        HandoffEvidenceImporter(store, plan, signing_key=b"test-key")
        .import_packet(evidence, approved_plan_id=plan.plan_id)
        .target(target_id)
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("409 Conflict", PhaseState.FAILED),
        ("409 Conflict: update in progress", PhaseState.RETRYABLE),
    ],
)
def test_generic_409_conflict_is_not_treated_as_resource_reuse(
    tmp_path: Path, message: str, expected: PhaseState
) -> None:
    plan = _plan(_target("db"))
    run = FleetOnboardingExecutor(
        plan,
        FleetStateStore(tmp_path / "state.sqlite"),
        phase_handlers=_handlers([], dbm=RuntimeError(message)),
        retries=0,
    ).execute(approved_plan_id=plan.plan_id)
    assert run.target("db").checkpoint("dbm").state is expected


@pytest.mark.parametrize("initial", ["blocked", "failed", "complete"])
def test_resume_does_not_mutate_non_resumable_targets(
    tmp_path: Path, initial: str
) -> None:
    plan = _plan(_target("db"))
    store = FleetStateStore(tmp_path / "state.sqlite")
    outcome = (
        RuntimeError("401 unauthorized")
        if initial == "blocked"
        else RuntimeError("unexpected failure")
    )
    FleetOnboardingExecutor(
        plan, store, phase_handlers=_handlers([], dbm=outcome), retries=0
    ).execute(approved_plan_id=plan.plan_id, run_id="run-1")
    if initial == "complete":
        FleetOnboardingExecutor(plan, store, phase_handlers=_handlers([])).execute(
            approved_plan_id=plan.plan_id, run_id="run-complete"
        )
        run_id = "run-complete"
    else:
        run_id = "run-1"
    before = store.load(run_id)
    calls: list[tuple[str, str]] = []
    after = FleetOnboardingExecutor(
        plan, store, phase_handlers=_handlers(calls)
    ).execute(approved_plan_id=plan.plan_id, run_id=run_id)
    assert calls == []
    assert after == before


def test_explicit_retry_reopens_failed_parent_and_dependency_blocked_child(
    tmp_path: Path,
) -> None:
    parent = _target("cdb")
    child = TargetPlan(
        "pdb",
        "pdb",
        "dbcs",
        "eu-frankfurt-1",
        dependencies=("cdb",),
    )
    plan = _plan(parent, child)
    store = FleetStateStore(tmp_path / "state.sqlite")
    failed = FleetOnboardingExecutor(
        plan,
        store,
        phase_handlers=_handlers([], dbm=RuntimeError("adapter defect")),
        retries=0,
    ).execute(approved_plan_id=plan.plan_id, run_id="run-1")
    assert failed.target("cdb").state is TargetState.FAILED
    assert failed.target("pdb").state is TargetState.BLOCKED

    calls: list[tuple[str, str]] = []
    resumed = FleetOnboardingExecutor(
        plan,
        store,
        phase_handlers=_handlers(calls),
    ).execute(
        approved_plan_id=plan.plan_id,
        run_id="run-1",
        retry_failed=True,
    )
    assert resumed.target("cdb").state is TargetState.COMPLETE
    assert resumed.target("pdb").state is TargetState.COMPLETE
    assert resumed.target("cdb").checkpoint("dbm").attempts == 2


def test_explicit_retry_does_not_reopen_authorization_block(
    tmp_path: Path,
) -> None:
    plan = _plan(_target("db"))
    store = FleetStateStore(tmp_path / "state.sqlite")
    FleetOnboardingExecutor(
        plan,
        store,
        phase_handlers=_handlers([], dbm=RuntimeError("403 unauthorized")),
        retries=0,
    ).execute(approved_plan_id=plan.plan_id, run_id="run-1")

    calls: list[tuple[str, str]] = []
    after = FleetOnboardingExecutor(
        plan,
        store,
        phase_handlers=_handlers(calls),
    ).execute(
        approved_plan_id=plan.plan_id,
        run_id="run-1",
        retry_failed=True,
    )
    assert calls == []
    assert after.target("db").state is TargetState.BLOCKED


def test_late_phase_authorization_block_is_checkpointed_on_that_phase(
    tmp_path: Path,
) -> None:
    plan = _plan(_target("db"))
    run = FleetOnboardingExecutor(
        plan,
        FleetStateStore(tmp_path / "state.sqlite"),
        phase_handlers=_handlers([], opsi=RuntimeError("403 unauthorized")),
    ).execute(approved_plan_id=plan.plan_id)
    assert run.target("db").checkpoint("opsi").state is PhaseState.BLOCKED
    assert run.target("db").checkpoint("prerequisites").state is PhaseState.COMPLETE


def test_status_is_collecting_when_registration_lacks_collection_proof(
    tmp_path: Path,
) -> None:
    plan = _plan(_target("db"))
    run = FleetOnboardingExecutor(
        plan, FleetStateStore(tmp_path / "state.sqlite"), phase_handlers=_handlers([])
    ).execute(approved_plan_id=plan.plan_id)
    status = fleet_status(run)
    assert status["targets"][0]["verdict"] == "collecting"
    assert status["summary"]["ready"] == 0


def test_resume_reopens_expired_ready_validation_and_persists_fresh_proof(
    tmp_path: Path,
) -> None:
    target_plan = TargetPlan(
        "db", "db", "dbcs", "eu-frankfurt-1", services=("dbm",)
    )
    plan = _plan(target_plan)
    expired = ResourceRecord(
        "collection-proof",
        "expired",
        ResourceOwnership.PREEXISTING,
        False,
        attributes={
            "service": "dbm",
            "timestamp": int(time.time()) - 1_000,
            "selected_services": ("dbm",),
        },
        effect=ResourceEffect.PREEXISTING,
    )
    manifest = RunManifest(
        "resume-run",
        plan.plan_id,
        (
            TargetManifest(
                "db",
                state=TargetState.COMPLETE,
                readiness=ReadinessVerdict.READY,
                checkpoints=tuple(
                    PhaseCheckpoint(phase, PhaseState.COMPLETE)
                    for phase in FleetOnboardingExecutor.PHASES
                ),
                resources=(expired,),
            ),
        ),
    )
    store = FleetStateStore(tmp_path / "state.sqlite")
    store.save(manifest, plan=plan, approved_plan_id=plan.plan_id)
    calls: list[tuple[str, str]] = []

    def validation(_target: TargetPlan) -> PhaseOutcome:
        return PhaseOutcome(
            readiness=ReadinessVerdict.READY,
            resources=(
                ResourceRecord(
                    "collection-proof",
                    "fresh",
                    ResourceOwnership.PREEXISTING,
                    False,
                    attributes={
                        "service": "dbm",
                        "timestamp": int(time.time()),
                        "selected_services": ("dbm",),
                    },
                    effect=ResourceEffect.PREEXISTING,
                ),
            ),
        )

    resumed = FleetOnboardingExecutor(
        plan,
        store,
        phase_handlers=_handlers(calls, validation=validation),
    ).execute(approved_plan_id=plan.plan_id, run_id="resume-run")

    assert calls == [("validation", "db")]
    assert fleet_status(resumed)["summary"]["ready"] == 1


def test_public_fleet_status_replaces_ocid_target_id_with_run_scoped_handle() -> None:
    raw_target = "ocid1.database.oc1..private-target"
    manifest = RunManifest("run-a", "plan-a", (TargetManifest(raw_target),))

    first = fleet_status(manifest)
    second = fleet_status(RunManifest("run-b", "plan-a", (TargetManifest(raw_target),)))

    assert raw_target not in str(first)
    assert "target_id" not in first["targets"][0]
    assert first["targets"][0]["target_handle"] != second["targets"][0]["target_handle"]


@pytest.mark.parametrize("count", [1, 100, 1000])
def test_fake_fleet_plan_execute_status_and_empty_offboard_scale_without_topology(
    tmp_path: Path, count: int
) -> None:
    """Exercise the public lifecycle stages at the supported fleet sizes.

    An in-memory checkpoint store eliminates SQLite I/O as a scale variable.
    The scale executor runs the real approval, scheduling, checkpoint, status,
    and cleanup path for every target through one validation phase; focused tests
    above cover the complete nine-phase ordering. SQLite durability is covered
    separately by the state-store tests.
    """
    plan = _plan(*(_target(f"target-{index:04d}") for index in range(count)))
    store = _InMemoryFleetStore()
    def validation(target: TargetPlan) -> PhaseOutcome:
        timestamp = int(time.time())
        return PhaseOutcome(
            readiness=ReadinessVerdict.READY,
            resources=tuple(
                ResourceRecord(
                    "collection-proof",
                    f"{target.target_id}:{service}",
                    ResourceOwnership.PREEXISTING,
                    False,
                    attributes={
                        "service": service,
                        "timestamp": timestamp,
                        "selected_services": target.services,
                    },
                    effect=ResourceEffect.PREEXISTING,
                )
                for service in target.services
            ),
        )

    handlers = _handlers([], validation=validation)

    class _ScaleExecutor(FleetOnboardingExecutor):
        PHASES = ("validation",)

    manifest = _ScaleExecutor(
        plan,
        store,
        phase_handlers=handlers,
        concurrency=8,
        service_concurrency={"read": 8},
    ).execute(approved_plan_id=plan.plan_id, run_id="scale-run")

    status = fleet_status(manifest)
    assert status["summary"]["ready"] == count
    assert len(status["targets"]) == count
    assert "ocid" not in str(status).lower()

    cleanup = CleanupPlanner(plan, manifest).build()
    assert cleanup.actions == ()
    result = CleanupExecutor(cleanup, store, _NoopCleanupOperations()).execute(
        approved_plan_id=cleanup.plan_id
    )
    assert result.complete


class _NoopCleanupOperations:
    def execute_cleanup(self, _action: object) -> None:
        raise AssertionError("an empty cleanup plan must not invoke OCI operations")


class _InMemoryFleetStore:
    """Thread-safe fake store for scale execution; not a durability substitute."""

    def __init__(self) -> None:
        self._runs: dict[str, object] = {}
        self._cleanup: dict[tuple[str, str], dict[str, object]] = {}
        self._lock = threading.Lock()

    def load(self, run_id: str | None):
        with self._lock:
            return self._runs.get(run_id or "")

    def save(self, manifest, *, plan, approved_plan_id) -> None:
        plan.require_approval(approved_plan_id)
        with self._lock:
            self._runs[manifest.run_id] = manifest

    def purge_expired_cleanup_evidence(self, **_kwargs) -> int:
        return 0

    def load_cleanup_state(self, *, run_id: str, cleanup_plan_id: str):
        with self._lock:
            return self._cleanup.get((run_id, cleanup_plan_id))

    def save_cleanup_state(self, *, run_id: str, cleanup_plan_id: str, state: dict[str, object]) -> None:
        with self._lock:
            self._cleanup[(run_id, cleanup_plan_id)] = state
