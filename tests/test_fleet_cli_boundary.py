from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest

from dbman_opsi.cli import _apply_answer_controls, _materialize_monitoring_target, _validate_credential_bindings, build_parser, main
from dbman_opsi.fleet_answers import FleetAnswers, LogPreset
from dbman_opsi.fleet import CredentialPolicy, DeploymentMode, FleetPlan, ResourceOwnership, ResourceRecord, RunManifest, TargetManifest, TargetPlan
from dbman_opsi.fleet_auth import AuthMode, OciAuth
from dbman_opsi.fleet_portable_state import ObjectStorageStateBackend, RemoteLeaseHeartbeat, StateConflictError, StateIntegrityError
from dbman_opsi.fleet_state import FleetStateStore
from dbman_opsi.fleet_operations import LifecycleOperations, collection_verdict
from dbman_opsi.fleet_discovery import DiscoveryScopeError
from dbman_opsi.credentials import CredentialDecision
from dbman_opsi.log_analytics import LogAnalyticsDecision
from dbman_opsi.fleet import ReadinessVerdict


def _plan() -> FleetPlan:
    return FleetPlan("DEFAULT", "eu-frankfurt-1", (TargetPlan("db", "db", "dbcs", "eu-frankfurt-1"),))


def test_lifecycle_parser_keeps_existing_and_adds_exact_approval_boundary() -> None:
    parser = build_parser()
    assert parser.parse_args(["doctor"]).command == "doctor"
    parsed = parser.parse_args(["onboard", "--region", "eu-frankfurt-1", "--plan-only", "--instance-principal"])
    assert parsed.instance_principal and parsed.plan_only


def test_canonical_credential_policy_materializes_adapter_visible_users() -> None:
    target = TargetPlan("target-a", "db", "dbcs", "eu-frankfurt-1")
    shared = _materialize_monitoring_target(target, FleetAnswers(monitoring_username="dbman_mon"))
    assert shared.settings["monitoring_user"] == "DBMAN_MON"
    answers = FleetAnswers(credential_policy=CredentialPolicy.DEDICATED_USER_UNIQUE_SECRET, monitoring_username="DBMAN_MON")
    first = _materialize_monitoring_target(target, answers)
    second = _materialize_monitoring_target(target, answers)
    other = _materialize_monitoring_target(TargetPlan("target-b", "db", "dbcs", "eu-frankfurt-1"), answers)
    assert first.settings["monitoring_user"] == second.settings["monitoring_user"]
    assert first.settings["monitoring_user"] != other.settings["monitoring_user"]
    assert len(str(first.settings["monitoring_user"])) <= 30


def test_credential_binding_policy_rejects_duplicate_or_differing_secret_refs() -> None:
    first = TargetPlan("a", "a", "dbcs", "r", settings={"password_secret_id": "vault://one"})
    same = TargetPlan("b", "b", "dbcs", "r", settings={"password_secret_id": "vault://one"})
    other = TargetPlan("b", "b", "dbcs", "r", settings={"password_secret_id": "vault://two"})
    with pytest.raises(ValueError, match="unique Vault"):
        _validate_credential_bindings((first, same), FleetAnswers(credential_policy=CredentialPolicy.SHARED_USER_UNIQUE_SECRET))
    with pytest.raises(ValueError, match="identical Vault"):
        _validate_credential_bindings((first, other), FleetAnswers(deployment_mode=DeploymentMode.POC, credential_policy=CredentialPolicy.SHARED_USER_SHARED_SECRET))


def test_status_missing_run_has_deterministic_exit_code(tmp_path: Path) -> None:
    assert main(["fleet-status", "--region", "eu-frankfurt-1", "--state", str(tmp_path / "state.sqlite"), "--run-id", "missing"]) == 6


def test_onboard_incomplete_discovery_is_redacted_and_blocked(monkeypatch, capsys) -> None:
    raw_compartment = "ocid1.compartment.oc1..private-scope"

    def blocked(*_args):
        raise DiscoveryScopeError(
            f"fleet discovery is incomplete: eu-frankfurt-1/{raw_compartment}/database"
        )

    monkeypatch.setattr("dbman_opsi.cli._lifecycle_plan", blocked)

    assert main(
        [
            "onboard",
            "--region",
            "eu-frankfurt-1",
            "--answers",
            "unused.yaml",
            "--non-interactive",
            "--plan-only",
        ]
    ) == 3
    output = capsys.readouterr()
    assert raw_compartment not in output.err
    assert "<OCI_OCID>" in output.err


def test_status_emits_redacted_json_for_saved_run(tmp_path: Path, capsys) -> None:
    target_id = "ocid1.database.oc1..private-target"
    plan = FleetPlan("DEFAULT", "eu-frankfurt-1", (TargetPlan(target_id, "private-name", "dbcs", "eu-frankfurt-1"),))
    store = FleetStateStore(tmp_path / "state.sqlite")
    store.save(RunManifest("run-1", plan.plan_id, (TargetManifest(target_id),)), plan=plan, approved_plan_id=plan.plan_id)
    assert main(["fleet-status", "--region", "eu-frankfurt-1", "--state", str(store.path), "--run-id", "run-1", "--json"]) == 0
    payload = capsys.readouterr().out
    assert target_id not in payload and "ocid" not in payload.lower()
    status = json.loads(payload)
    assert "target_id" not in status["targets"][0]
    assert len(status["targets"][0]["target_handle"]) == 24

    assert main(["fleet-status", "--region", "eu-frankfurt-1", "--state", str(store.path), "--run-id", "run-1"]) == 0
    assert target_id not in capsys.readouterr().out


def test_offboard_plan_only_emits_ordered_opaque_review_plan(tmp_path: Path, capsys) -> None:
    plan = FleetPlan(
        "DEFAULT", "eu-frankfurt-1",
        (TargetPlan("cdb-raw", "cdb", "dbcs-cdb", "eu-frankfurt-1"), TargetPlan("pdb-raw", "pdb", "dbcs-pdb", "eu-frankfurt-1")),
    )
    manifest = RunManifest("run-1", plan.plan_id, (
        TargetManifest("cdb-raw", resources=(ResourceRecord("dbm-cdb", "ocid1.database.oc1..cdb", ResourceOwnership.OWNED, True),)),
        TargetManifest("pdb-raw", resources=(ResourceRecord("dbm-pdb", "ocid1.pluggabledatabase.oc1..pdb", ResourceOwnership.OWNED, True),)),
    ))
    store = FleetStateStore(tmp_path / "state.sqlite")
    store.save(manifest, plan=plan, approved_plan_id=plan.plan_id)

    assert main(["offboard", "--region", "eu-frankfurt-1", "--state", str(store.path), "--run-id", "run-1", "--plan-only"]) == 10
    output = capsys.readouterr().out
    assert "ocid1" not in output and "cdb-raw" not in output and "pdb-raw" not in output
    review = json.loads(output)
    assert [item["operation"] for item in review["actions"]] == ["disable-dbm-pdb", "disable-dbm-cdb"]
    assert all(item["ownership"] == "owned" and item["created"] and item["enabled_by_run"] for item in review["actions"])
    assert all("resource_ref" not in item and len(item["resource_handle"]) == 24 for item in review["actions"])


def test_auth_command_shapes_are_explicit_and_secret_free() -> None:
    assert OciAuth(AuthMode.API_KEY, "P").cli_args(region="r") == ["oci", "--profile", "P", "--region", "r"]
    assert OciAuth(AuthMode.SECURITY_TOKEN, "P").cli_args(region="r")[-2:] == ["--auth", "security_token"]
    assert OciAuth(AuthMode.INSTANCE_PRINCIPAL).cli_args(region="r") == ["oci", "--region", "r", "--auth", "instance_principal"]


def test_object_state_cache_is_private_and_checksum_checked(tmp_path: Path) -> None:
    class Fake:
        def get_object_state(self, namespace, bucket, name):
            body = b"sqlite-state"
            import hashlib
            return body, "v1", {"run-id": "run", "plan-id": "plan", "sha256": hashlib.sha256(body).hexdigest(), "schema-version": "1"}
        def put_object_state(self, namespace, bucket, name, body, *, if_match, metadata):
            assert "secret" not in metadata
            return "v2"
    cache = tmp_path / "cache.sqlite"
    backend = ObjectStorageStateBackend(Fake(), namespace="ns", bucket="bucket", name="run.sqlite", cache_path=cache)
    binding = backend.download(run_id="run", plan_id="plan")
    assert binding.version == "v1"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    backend.upload(run_id="run", plan_id="plan", expected_version="v1")
    try:
        backend.download(run_id="run", plan_id="plan", expected_checksum="wrong")
    except StateIntegrityError:
        pass
    else:
        raise AssertionError("checksum mismatch must fail")


def test_portable_upload_threads_etag_and_preserves_integrity_error(tmp_path: Path) -> None:
    class Fake:
        def get_object_state(self, *args): raise AssertionError("not used")
        def put_object_state(self, *args, **kwargs):
            assert kwargs["if_match"] == "etag-1"
            raise StateIntegrityError("unsafe secret")
    cache = tmp_path / "state.sqlite"; cache.write_bytes(b"safe")
    backend = ObjectStorageStateBackend(Fake(), namespace="n", bucket="b", name="o", cache_path=cache)
    try: backend.upload(run_id="r", plan_id="p", expected_version="etag-1")
    except StateIntegrityError: pass
    else: raise AssertionError("integrity error must not become conflict")


def test_remote_lease_is_conditional_and_an_expired_lease_can_be_replaced(tmp_path: Path) -> None:
    class Fake:
        def __init__(self): self.objects = {}; self.version = 0
        def get_object_state(self, namespace, bucket, name):
            if name not in self.objects: raise RuntimeError("not found")
            return self.objects[name]
        def put_object_state(self, namespace, bucket, name, body, *, if_match, metadata):
            current = self.objects.get(name)
            if current is not None and current[1] != if_match: raise RuntimeError("etag conflict")
            if current is None and if_match is not None: raise RuntimeError("missing")
            self.version += 1; version = f"v{self.version}"; self.objects[name] = (body, version, metadata); return version
    fake = Fake()
    backend = ObjectStorageStateBackend(fake, namespace="n", bucket="b", name="state", cache_path=tmp_path / "cache")
    first = backend.acquire_lease(run_id="run", plan_id="plan", owner="one", now=100, ttl_seconds=10)
    try:
        backend.acquire_lease(run_id="run", plan_id="plan", owner="two", now=101)
    except StateConflictError:
        pass
    else:
        raise AssertionError("second actor must not acquire a live lease")
    second = backend.acquire_lease(run_id="run", plan_id="plan", owner="two", now=111)
    assert second.owner == "two" and second.version != first.version


def test_remote_lease_heartbeat_renews_and_fences_a_lost_conditional_lease(tmp_path: Path) -> None:
    class Fake:
        def __init__(self): self.objects = {}; self.version = 0
        def get_object_state(self, namespace, bucket, name):
            if name not in self.objects: raise RuntimeError("not found")
            return self.objects[name]
        def put_object_state(self, namespace, bucket, name, body, *, if_match, metadata):
            current = self.objects.get(name)
            if current is not None and current[1] != if_match: raise RuntimeError("etag conflict")
            if current is None and if_match is not None: raise RuntimeError("missing")
            self.version += 1; version = f"v{self.version}"; self.objects[name] = (body, version, metadata); return version
    fake = Fake()
    backend = ObjectStorageStateBackend(fake, namespace="n", bucket="b", name="state", cache_path=tmp_path / "cache")
    initial = backend.acquire_lease(run_id="run", plan_id="plan", owner="one", ttl_seconds=0.06)
    heartbeat = RemoteLeaseHeartbeat(backend, initial, ttl_seconds=0.06, interval_seconds=0.01)
    heartbeat.start()
    time.sleep(0.15)
    # It is still live after more than one TTL, so a second actor cannot make
    # any guarded OCI call on the premise of owning the remote lease.
    with pytest.raises(StateConflictError):
        backend.acquire_lease(run_id="run", plan_id="plan", owner="two", ttl_seconds=0.06)
    heartbeat.close()


def test_lifecycle_decisions_and_validation_require_collection_proof(monkeypatch) -> None:
    plan = _plan()
    operations = LifecycleOperations(plan, object())  # services are replaced below
    target = TargetPlan("db", "db", "dbcs", "eu-frankfurt-1", resource_id="db-id", settings={"password_secret_id": "secret", "monitoring_user": "DBSNMP"})
    class Credentials:
        def set_for_target(self, *_): return CredentialDecision("db", "blocked", "missing authority")
    operations.credentials = Credentials()
    assert operations.preferred_credentials(target).handoff_requested
    class Logan:
        def enable_all(self, *_): return [LogAnalyticsDecision("db", "blocked", "association denied")]
    operations.log_analytics = Logan()
    log_target = TargetPlan("db", "db", "dbcs", "eu-frankfurt-1", services=("logan",), settings={"management_agent_id": "agent"})
    assert operations.agent_log_analytics(log_target).handoff_requested
    class LoganConfigured:
        def enable_all(self, *_): return [LogAnalyticsDecision("db", "configured", "association applied")]
    operations.log_analytics = LoganConfigured()
    assert operations.agent_log_analytics(log_target).readiness is ReadinessVerdict.COLLECTING
    monkeypatch.setattr("dbman_opsi.fleet_operations.ValidationService.validate", lambda *_: ["db: Database Management ENABLED; Ops Insights ACTIVE (ENABLED)"])
    assert operations.validation(target).readiness is ReadinessVerdict.COLLECTING
    monkeypatch.setattr("dbman_opsi.fleet_operations.ValidationService.validate", lambda *_: ["db: Log Analytics query result count=5"])
    assert operations.validation(log_target).readiness is ReadinessVerdict.READY


def test_collection_readiness_is_conjunctive_per_requested_service() -> None:
    assert collection_verdict(("logan",), ["db: Log Analytics query result count=5"]) is ReadinessVerdict.READY
    assert collection_verdict(("dbm", "opsi", "logan"), ["db: Log Analytics query result count=5"]) is ReadinessVerdict.COLLECTING
    proofs = ["DBM collection proof timestamp=2026-07-27T00:00:00Z", "OPSI observation timestamp=2026-07-27T00:00:00Z", "db: Log Analytics query result count=5"]
    assert collection_verdict(("dbm", "opsi", "logan"), proofs) is ReadinessVerdict.READY
    assert collection_verdict(("dbm", "opsi", "logan"), proofs + ["OPSI DEGRADED"]) is ReadinessVerdict.DEGRADED
    assert collection_verdict(("dbm", "opsi"), ["Database Management ENABLED", "Ops Insights ACTIVE"]) is ReadinessVerdict.COLLECTING


def test_lifecycle_records_actual_opsi_insight_id_and_credential_ownership() -> None:
    plan = _plan()
    target = TargetPlan(
        "db", "db", "dbcs", "eu-frankfurt-1", resource_id="db-id", compartment_id="comp",
        services=("dbm", "opsi"), settings={"password_secret_id": "vault-secret-ref"},
    )
    class Oci:
        def list_opsi_database_insights(self, compartment):
            assert compartment == "comp"
            return [{"database-id": "db-id", "id": "opsi-insight-ref"}]
    operations = LifecycleOperations(plan, Oci())
    class Enablement:
        def enable_dbm(self, _): return False
        def enable_opsi(self, _): return True
    operations.enablement = Enablement()
    class Credentials:
        def set_for_target(self, *_): return CredentialDecision("db", "set", "PC_READ, PC_WRITE already configured for db")
    operations.credentials = Credentials()
    dbm = operations.dbm(target).resources[0]
    insight = operations.opsi(target).resources[0]
    credential = operations.preferred_credentials(target).resources[0]
    assert (dbm.ownership, dbm.enabled_by_run) == (ResourceOwnership.REUSED, False)
    assert insight.resource_ref == "opsi-insight-ref"
    assert (credential.ownership, credential.enabled_by_run) == (ResourceOwnership.PREEXISTING, False)


def test_lifecycle_handoffs_when_opsi_enablement_has_no_authoritative_insight_id() -> None:
    plan = _plan()
    target = TargetPlan("db", "db", "dbcs", "eu-frankfurt-1", resource_id="db-id", compartment_id="comp", services=("opsi",))
    class Oci:
        def list_opsi_database_insights(self, _compartment): return []
    operations = LifecycleOperations(plan, Oci())
    class Enablement:
        def enable_opsi(self, _): return True
    operations.enablement = Enablement()
    outcome = operations.opsi(target)
    assert outcome.handoff_requested
    assert "Insight ID" in (outcome.message or "")
    assert outcome.resources == ()


def test_answer_controls_are_materialized_into_executable_target_settings() -> None:
    target = TargetPlan("pdb", "pdb", "dbcs", "eu-frankfurt-1", settings={"database_role": "PDB"})
    controlled = _apply_answer_controls(target, FleetAnswers(
        deployment_mode="poc", services=("logan",), provision_test_dbcs=True,
        log_preset=LogPreset.ALERT_LISTENER_AUDIT, pdb_unique_passwords=True,
    ))
    assert controlled.settings["provision"] is True
    assert controlled.settings["account_group"] == "pdb:pdb"
    assert controlled.settings["logan_sources"] == (
        "Oracle Database Alert Logs", "Oracle Database Listener Alert Logs", "Oracle Database Audit Logs",
    )


def test_log_analytics_only_target_uses_target_region_compartment_and_reviewed_bindings() -> None:
    plan = FleetPlan("DEFAULT", "eu-frankfurt-1", ())
    target = TargetPlan(
        "target", "db", "dbcs", "us-ashburn-1", compartment_id="target-compartment",
        services=("logan",), settings={
            "management_agent_id": "agent", "logan_namespace": "reviewed-namespace",
            "logan_log_group_id": "reviewed-log-group",
        },
    )
    operations = LifecycleOperations(plan, object())
    observed = []
    class Logan:
        def enable_all(self, config):
            observed.append(config)
            return [LogAnalyticsDecision("db", "configured", "applied")]
    operations.log_analytics = Logan()
    outcome = operations.agent_log_analytics(target)
    assert outcome.readiness is ReadinessVerdict.COLLECTING
    assert observed[0].region == "us-ashburn-1"
    assert observed[0].compartment_id == "target-compartment"
    assert observed[0].log_analytics.namespace == "reviewed-namespace"
    assert observed[0].log_analytics.log_group_id == "reviewed-log-group"
