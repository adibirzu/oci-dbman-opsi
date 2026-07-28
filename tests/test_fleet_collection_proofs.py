from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json

import pytest

from dbman_opsi.config import EnablementConfig, Target
from dbman_opsi.validation import ValidationService
from dbman_opsi.fleet import FleetPlan, PhaseCheckpoint, PhaseState, ReadinessVerdict, ResourceEffect, ResourceOwnership, ResourceRecord, TargetManifest, TargetPlan, TargetState, RunManifest
from dbman_opsi.fleet_handoff import CollectionEvidenceImporter, target_handle
from dbman_opsi.fleet_state import FleetStateStore
from dbman_opsi.fleet_status import fleet_status, refresh_collection_readiness


class _Oci:
    def get_dbm_collection_observation(self, _id):
        return {"observation-time": (datetime.now(UTC) - timedelta(seconds=10)).isoformat()}

    def get_opsi_collection_observation(self, _id):
        return {"timestamp": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}


def test_collection_proofs_require_real_fresh_timestamps() -> None:
    config = EnablementConfig(profile="DEFAULT", region="eu-frankfurt-1", targets=(
        Target(kind="dbcs", name="db", resource_id="db", opsi_database_insight_id="insight", services=("dbm", "opsi")),
    ))
    proofs = ValidationService(_Oci()).collection_proofs(config, max_age_seconds=60)  # type: ignore[arg-type]
    assert [(proof.service, proof.status) for proof in proofs] == [("dbm", "collecting"), ("opsi", "stale")]


def test_signed_collection_evidence_is_bound_to_run_target_and_selected_service(tmp_path) -> None:
    plan = FleetPlan("DEFAULT", "r", (TargetPlan("target", "db", "dbcs", "r", services=("dbm",)),))
    store = FleetStateStore(tmp_path / "state.sqlite")
    store.save(RunManifest("run", plan.plan_id, (TargetManifest("target"),)), plan=plan, approved_plan_id=plan.plan_id)
    evidence = {"run_id": "run", "plan_id": plan.plan_id, "target_handle": target_handle("target"), "service": "dbm", "result": "verified", "evidence_timestamp": int(datetime.now(UTC).timestamp())}
    signature = hmac.new(b"collection-test-key", json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
    path = tmp_path / "proof.json"; path.write_text(json.dumps({"evidence": evidence, "signature": signature}))
    updated = CollectionEvidenceImporter(store, plan, signing_key=b"collection-test-key").import_packet(path, approved_plan_id=plan.plan_id)
    assert updated.target("target").resources[0].attributes["service"] == "dbm"


def test_collection_import_promotes_only_after_all_selected_service_proofs(tmp_path) -> None:
    key = b"collection-test-key"
    plan = FleetPlan("DEFAULT", "r", (TargetPlan("target", "db", "dbcs", "r", services=("dbm", "opsi", "logan")),))
    store = FleetStateStore(tmp_path / "state.sqlite")
    completed = TargetManifest("target", state=TargetState.COMPLETE, readiness=ReadinessVerdict.COLLECTING)
    store.save(RunManifest("run", plan.plan_id, (completed,)), plan=plan, approved_plan_id=plan.plan_id)

    for service in ("dbm", "opsi", "logan"):
        evidence = {
            "run_id": "run", "plan_id": plan.plan_id, "target_handle": target_handle("target"),
            "service": service, "result": "verified", "evidence_timestamp": int(datetime.now(UTC).timestamp()),
        }
        signature = hmac.new(key, json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
        path = tmp_path / f"{service}.json"
        path.write_text(json.dumps({"evidence": evidence, "signature": signature}))
        updated = CollectionEvidenceImporter(store, plan, signing_key=key).import_packet(path, approved_plan_id=plan.plan_id)
        if service != "logan":
            assert updated.target("target").readiness is ReadinessVerdict.COLLECTING
    assert updated.target("target").state is TargetState.COMPLETE
    assert updated.target("target").readiness is ReadinessVerdict.READY


def test_collection_import_rejects_future_timestamp(tmp_path) -> None:
    key = b"collection-test-key"
    plan = FleetPlan("DEFAULT", "r", (TargetPlan("target", "db", "dbcs", "r", services=("dbm",)),))
    store = FleetStateStore(tmp_path / "state.sqlite")
    store.save(RunManifest("run", plan.plan_id, (TargetManifest("target"),)), plan=plan, approved_plan_id=plan.plan_id)
    evidence = {
        "run_id": "run", "plan_id": plan.plan_id, "target_handle": target_handle("target"),
        "service": "dbm", "result": "verified",
        "evidence_timestamp": int(datetime.now(UTC).timestamp()) + 3600,
    }
    signature = hmac.new(key, json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
    path = tmp_path / "future.json"
    path.write_text(json.dumps({"evidence": evidence, "signature": signature}))
    with pytest.raises(ValueError, match="stale or missing timestamp"):
        CollectionEvidenceImporter(store, plan, signing_key=key).import_packet(
            path, approved_plan_id=plan.plan_id
        )


def test_ready_status_expires_and_reopens_validation_for_resume() -> None:
    proof = ResourceRecord(
        "collection-proof",
        "digest",
        ResourceOwnership.PREEXISTING,
        False,
        attributes={"service": "dbm", "timestamp": 100, "selected_services": ("dbm",)},
        effect=ResourceEffect.PREEXISTING,
    )
    target = TargetManifest(
        "target",
        state=TargetState.COMPLETE,
        readiness=ReadinessVerdict.READY,
        checkpoints=(PhaseCheckpoint("validation", PhaseState.COMPLETE),),
        resources=(proof,),
    )
    manifest = RunManifest("run", "plan", (target,))

    assert fleet_status(manifest, now=150)["summary"]["ready"] == 1
    assert fleet_status(manifest, now=1_100)["summary"]["ready"] == 0
    refreshed = refresh_collection_readiness(manifest, now=1_100)
    assert refreshed.target("target").state is TargetState.PENDING
    assert refreshed.target("target").readiness is ReadinessVerdict.COLLECTING
    assert refreshed.target("target").checkpoint("validation").state is PhaseState.RETRYABLE
