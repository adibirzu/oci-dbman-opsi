from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml
import pytest

from dbman_opsi.cli import main
from dbman_opsi.fleet import (
    FleetPlan,
    ResourceEffect,
    ResourceOwnership,
    ResourceRecord,
    ReadinessVerdict,
    TargetPlan,
)
from dbman_opsi.fleet_discovery import DiscoveredTarget, FleetDiscoveryResult
from dbman_opsi.fleet_executor import PhaseOutcome
from dbman_opsi.fleet_handoff import HandoffPacketWriter, target_handle
from dbman_opsi.fleet_offboarding import CleanupHandoffRequired, CleanupHandoffPacketWriter
from dbman_opsi.fleet_state import FleetStateStore
from dbman_opsi.fleet_state import RunLeaseError


def _signed(payload: dict[str, object], key: bytes) -> dict[str, object]:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {"evidence": payload, "signature": hmac.new(key, canonical, hashlib.sha256).hexdigest()}


def test_public_cli_onboard_resume_collect_and_offboard_are_plan_bound(tmp_path: Path, monkeypatch, capsys) -> None:
    """Exercise the public argparse handlers through a complete redacted lifecycle."""
    state = tmp_path / "fleet.sqlite"
    answers = tmp_path / "answers.yaml"
    bindings = tmp_path / "bindings.yaml"
    key_path = tmp_path / "handoff.key"
    handoff_dir = tmp_path / "handoffs"
    cleanup_dir = tmp_path / "cleanup"
    answers.write_text(
        yaml.safe_dump(
            {
                "deployment_mode": "poc",
                "services": ["dbm", "opsi", "datasafe", "logan"],
                "credential_policy": "shared-user-unique-secret",
                "authority_mode": "approval-required",
                "log_preset": "extended",
                "retention_days": 7,
                "max_concurrency": 1,
            }
        )
    )
    bindings.write_text(yaml.safe_dump({"targets": {"ocid1.database.oc1..target": {
        "password_secret_id": "vault://secret-ref",
        "private_endpoint_id": "ref:dbm-endpoint",
        "management_agent_id": "ref:agent",
    }}}))
    os.chmod(bindings, 0o600)
    key = b"acceptance-signing-key-0123456789"
    key_path.write_bytes(key)
    os.chmod(key_path, 0o600)

    discovery = FleetDiscoveryResult(
        tenancy_id="tenancy",
        regions=("eu-frankfurt-1",),
        compartments=(("compartment", "compartment"),),
        targets=(DiscoveredTarget(
            target_id="ocid1.database.oc1..target",
            name="production-db",
            kind="dbcs",
            region="eu-frankfurt-1",
            compartment_id="compartment",
            resource_id="ocid1.db.oc1..resource",
        ),),
    )
    monkeypatch.setattr("dbman_opsi.cli.FleetDiscovery.discover_result", lambda self: discovery)
    monkeypatch.setattr("dbman_opsi.cli._lifecycle_oci", lambda *args, **kwargs: object())

    handoff_enabled = {"value": False}
    crash_once = {"value": False}
    crash_reconciled = {"value": False}
    dbm_writes: list[str] = []
    calls: list[str] = []
    resources = (
        ResourceRecord("dbm-cdb", "dbm-ref", ResourceOwnership.OWNED, True, effect=ResourceEffect.CREATED),
        ResourceRecord("opsi-insight", "opsi-ref", ResourceOwnership.OWNED, True, effect=ResourceEffect.CREATED, attributes={"insight_id": "opsi-ref"}),
        ResourceRecord("preferred-credential", "credential-ref", ResourceOwnership.PREEXISTING, False, effect=ResourceEffect.PREEXISTING),
        ResourceRecord("database-user", "user-ref", ResourceOwnership.OWNED, True, effect=ResourceEffect.CREATED),
        ResourceRecord("datasafe-target", "datasafe-ref", ResourceOwnership.OWNED, True, effect=ResourceEffect.CREATED),
        ResourceRecord("logan-association", "logan-ref", ResourceOwnership.OWNED, True, effect=ResourceEffect.CREATED),
    )

    def handlers(self):
        def success(phase):
            def run(target):
                calls.append(phase)
                if phase == "dbm" and crash_once["value"]:
                    crash_once["value"] = False
                    dbm_writes.append("created")
                    raise KeyboardInterrupt("crash after service egress")
                if phase == "prerequisites" and not handoff_enabled["value"]:
                    return PhaseOutcome.handoff("approved operator authority is required")
                if phase == "validation":
                    return PhaseOutcome(readiness=ReadinessVerdict.COLLECTING, resources=())
                phase_resources = {
                    "dbm": ((ResourceRecord("dbm-cdb", "dbm-ref", ResourceOwnership.REUSED, False, effect=ResourceEffect.REUSED),) if crash_reconciled["value"] else (resources[0],)),
                    "credentials": (resources[2], resources[3]),
                    "opsi": (resources[1],),
                    "datasafe": (resources[4],),
                    "agent-log-analytics": (resources[5],),
                }
                return PhaseOutcome(resources=phase_resources.get(phase, ()))
            return run
        return {phase: success(phase) for phase in (
            "prerequisites", "test-databases", "vault-endpoints", "db-host-automation",
            "dbm", "credentials", "opsi", "datasafe", "agent-log-analytics", "validation",
        )}

    monkeypatch.setattr("dbman_opsi.cli.LifecycleOperations.handlers", handlers)

    common = ["--region", "eu-frankfurt-1", "--state", str(state), "--answers", str(answers), "--bindings", str(bindings)]
    assert main(["onboard", *common, "--plan-only", "--handoff-key", str(key_path), "--handoff-dir", str(handoff_dir)]) == 10
    review = json.loads(capsys.readouterr().out)
    plan_id = review["plan_id"]
    assert "ocid1" not in json.dumps(review).lower()
    assert review["settings"]["services"] == ["datasafe", "dbm", "logan", "opsi"]
    assert main(["onboard", *common, "--approval", "wrong"]) == 4
    assert not state.exists()

    assert main(["onboard", *common, "--approval", plan_id, "--handoff-key", str(key_path), "--handoff-dir", str(handoff_dir)]) in {2, 3}
    evidence = capsys.readouterr().out
    run_id = next(line.split("`")[1] for line in evidence.splitlines() if line.startswith("- Run:"))
    issued = next(handoff_dir.glob("*.handoff.json"))
    completion = HandoffPacketWriter(handoff_dir, signing_key=key).write_completion(issued, attestation="authority approved", result="completed")
    assert main(["import-handoff", *common[:4], "--run-id", run_id, "--approval", plan_id, "--evidence", str(completion), "--handoff-key", str(key_path)]) in {0, 2, 3}
    handoff_enabled["value"] = True
    assert main(["resume", *common[:4], "--run-id", run_id, "--approval", plan_id, "--handoff-key", str(key_path), "--handoff-dir", str(handoff_dir)]) in {0, 2, 3}
    assert {"dbm", "opsi", "datasafe", "agent-log-analytics"}.issubset(set(calls))

    store = FleetStateStore(state)
    plan = store.load_plan(run_id)
    assert plan is not None
    for service in ("dbm", "opsi", "logan", "datasafe"):
        payload = {
            "version": 1, "run_id": run_id, "plan_id": plan.plan_id,
            "target_handle": target_handle("ocid1.database.oc1..target"),
            "service": service, "result": "verified", "evidence_timestamp": int(time.time()),
        }
        packet = tmp_path / f"{service}.json"
        packet.write_text(json.dumps(_signed(payload, key)))
        assert main(["import-collection-evidence", *common[:4], "--run-id", run_id, "--approval", plan.plan_id, "--evidence", str(packet), "--handoff-key", str(key_path)]) in {0, 2}

    assert main(["resume", *common[:4], "--run-id", run_id, "--approval", plan.plan_id, "--handoff-key", str(key_path), "--handoff-dir", str(handoff_dir)]) in {0, 2, 3}
    status = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert status["summary"]["ready"] == 1
    held_packet = tmp_path / "held-import.json"
    held_payload = {
        "version": 1, "run_id": run_id, "plan_id": plan.plan_id,
        "target_handle": target_handle("ocid1.database.oc1..target"),
        "service": "dbm", "result": "verified", "evidence_timestamp": int(time.time()),
    }
    held_packet.write_text(json.dumps(_signed(held_payload, key)))
    with monkeypatch.context() as lease_patch:
        lease_patch.setattr(FleetStateStore, "acquire_lease", lambda *args, **kwargs: False)
        with pytest.raises(RunLeaseError):
            main(["import-collection-evidence", *common[:4], "--run-id", run_id, "--approval", plan.plan_id, "--evidence", str(held_packet), "--handoff-key", str(key_path)])
    persisted = store.load(run_id)
    assert persisted is not None
    persisted_resources = {(item.resource_type, item.resource_ref, item.ownership.value, item.enabled_by_run) for item in persisted.target("ocid1.database.oc1..target").resources}
    assert {("dbm-cdb", "dbm-ref", "owned", True), ("opsi-insight", "opsi-ref", "owned", True), ("preferred-credential", "credential-ref", "preexisting", False), ("database-user", "user-ref", "owned", True), ("datasafe-target", "datasafe-ref", "owned", True), ("logan-association", "logan-ref", "owned", True)} <= persisted_resources

    cleanup_calls: list[str] = []
    entered_cleanup = threading.Event()
    release_cleanup = threading.Event()
    block_cleanup = {"value": True}
    def cleanup(self, action):
        cleanup_calls.append(action.operation)
        if block_cleanup["value"]:
            block_cleanup["value"] = False
            entered_cleanup.set()
            release_cleanup.wait(timeout=10)
        if action.operation == "delete-database-user":
            raise CleanupHandoffRequired("approved credential cleanup adapter is required")
    monkeypatch.setattr("dbman_opsi.cli.OciCleanupOperations.execute_cleanup", cleanup)

    assert main(["offboard", "--region", "eu-frankfurt-1", "--state", str(state), "--run-id", run_id, "--plan-only"]) == 10
    cleanup_review = json.loads(capsys.readouterr().out)
    assert cleanup_review["action_count"] > 0
    assert "ocid1" not in json.dumps(cleanup_review).lower()
    assert [item["operation"] for item in cleanup_review["actions"]] == sorted((item["operation"] for item in cleanup_review["actions"]), key=lambda op: {"dissociate-log-analytics": 0, "disable-opsi": 1, "unregister-data-safe": 2, "disable-dbm-cdb": 3, "delete-database-user": 4}[op])
    cleanup_plan_id = cleanup_review["cleanup_plan_id"]
    with monkeypatch.context() as lease_patch:
        lease_patch.setattr(FleetStateStore, "acquire_lease", lambda *args, **kwargs: False)
        with pytest.raises(RunLeaseError):
            main(["offboard", "--region", "eu-frankfurt-1", "--state", str(state), "--run-id", run_id, "--approval", cleanup_plan_id, "--handoff-key", str(key_path), "--handoff-dir", str(cleanup_dir)])
    assert cleanup_calls == []
    with ThreadPoolExecutor(max_workers=1) as pool:
        first_offboard = pool.submit(main, ["offboard", "--region", "eu-frankfurt-1", "--state", str(state), "--run-id", run_id, "--approval", cleanup_plan_id, "--handoff-key", str(key_path), "--handoff-dir", str(cleanup_dir)])
        assert entered_cleanup.wait(timeout=5)
        with pytest.raises(RunLeaseError):
            main(["offboard", "--region", "eu-frankfurt-1", "--state", str(state), "--run-id", run_id, "--approval", cleanup_plan_id, "--handoff-key", str(key_path), "--handoff-dir", str(cleanup_dir)])
        release_cleanup.set()
        assert first_offboard.result(timeout=10) in {0, 2}
    issued_cleanup = next(cleanup_dir.glob("*.cleanup-handoff.json"))
    cleanup_completion = CleanupHandoffPacketWriter(cleanup_dir, signing_key=key).write_completion(issued_cleanup, attestation="credential cleanup approved", result="completed")
    assert main(["import-cleanup-handoff", "--region", "eu-frankfurt-1", "--state", str(state), "--run-id", run_id, "--approval", cleanup_plan_id, "--evidence", str(cleanup_completion), "--handoff-key", str(key_path)]) in {0, 2}
    before_repeat = list(cleanup_calls)
    assert main(["offboard", "--region", "eu-frankfurt-1", "--state", str(state), "--run-id", run_id, "--approval", cleanup_plan_id, "--handoff-key", str(key_path), "--handoff-dir", str(cleanup_dir)]) in {0, 2}
    assert cleanup_calls == before_repeat

    crash_run = "00000000-0000-0000-0000-000000000099"
    before_crash_dbm = calls.count("dbm")
    crash_once["value"] = True
    with monkeypatch.context() as crash_patch:
        crash_patch.setattr("dbman_opsi.cli.uuid.uuid4", lambda: uuid.UUID(crash_run))
        with pytest.raises(KeyboardInterrupt, match="crash after service egress"):
            main(["onboard", *common, "--approval", plan.plan_id, "--handoff-key", str(key_path), "--handoff-dir", str(handoff_dir)])
    crash_reconciled["value"] = True
    assert main(["resume", *common[:4], "--run-id", crash_run, "--approval", plan.plan_id, "--handoff-key", str(key_path), "--handoff-dir", str(handoff_dir)]) in {0, 2, 3}
    assert len(dbm_writes) == 1
    crash_manifest = store.load(crash_run)
    assert crash_manifest is not None
    crash_resource = next(item for item in crash_manifest.target("ocid1.database.oc1..target").resources if item.resource_type == "dbm-cdb")
    assert not crash_resource.cleanup_allowed
    capsys.readouterr()
    assert main(["offboard", "--region", "eu-frankfurt-1", "--state", str(state), "--run-id", crash_run, "--plan-only"]) == 10
    crash_cleanup = json.loads(capsys.readouterr().out)
    assert all(item["operation"] != "disable-dbm-cdb" for item in crash_cleanup["actions"])
