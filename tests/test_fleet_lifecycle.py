from dataclasses import FrozenInstanceError
from pathlib import Path
import sqlite3
import stat

import pytest

from dbman_opsi.config import EnablementConfig, Target
from dbman_opsi.evidence import evidence_json, evidence_markdown
from dbman_opsi.fleet import (
    CheckpointTransitionError,
    DiscoveryScope,
    FleetPlan,
    PhaseState,
    PlanApprovalMismatch,
    ReadinessVerdict,
    ResourceOwnership,
    ResourceRecord,
    RunManifest,
    TargetManifest,
    TargetPlan,
    fleet_plan_from_config,
    fleet_plan_from_dict,
)
from dbman_opsi.fleet_state import FleetStateStore, RunPlanBindingError


def _plan(name: str = "one") -> FleetPlan:
    return FleetPlan(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        targets=(TargetPlan(target_id="target-1", name=name, kind="dbcs", region="eu-frankfurt-1"),),
    )


def test_discovery_scope_and_safe_plan_metadata_are_hashed_roundtrip_and_redacted() -> None:
    target = TargetPlan("ocid1.database.private", "db", "dbcs", "private-region")
    first = FleetPlan("DEFAULT", "private-region", (target,), discovery_scope=DiscoveryScope(subscribed_regions=("private-region",), accessible_compartments=("ocid1.compartment.private",)), prerequisite_actions=("VAULT_ENDPOINTS",), risk_codes=("RISK_NETWORK",), estimated_resource_counts={"dbm": 1})
    second = FleetPlan("DEFAULT", "private-region", (target,), discovery_scope=DiscoveryScope(), prerequisite_actions=("VAULT_ENDPOINTS",), risk_codes=("RISK_NETWORK",), estimated_resource_counts={"dbm": 1})
    assert first.plan_id != second.plan_id
    assert fleet_plan_from_dict(first.canonical_dict()).canonical_dict() == first.canonical_dict()
    from dbman_opsi.fleet import public_plan_summary
    rendered = str(public_plan_summary(first))
    assert "ocid1" not in rendered and "private-region" not in rendered


def test_plan_metadata_rejects_public_label_injection_and_invalid_counts() -> None:
    target = TargetPlan("target", "db", "dbcs", "region")
    with pytest.raises(ValueError, match="public-safe"):
        FleetPlan("DEFAULT", "region", (target,), risk_codes=("ocid1.database.secret",))
    with pytest.raises(ValueError, match="non-negative"):
        FleetPlan("DEFAULT", "region", (target,), estimated_resource_counts={"dbm": -1})
    with pytest.raises(ValueError, match="ownership policy"):
        FleetPlan("DEFAULT", "region", (target,), ownership_policy="delete-reused")


def test_fleet_plan_id_is_stable_for_equivalent_target_order() -> None:
    first = FleetPlan(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        targets=(
            TargetPlan(target_id="b", name="second", kind="dbcs", region="eu-frankfurt-1"),
            TargetPlan(target_id="a", name="first", kind="autonomous", region="eu-frankfurt-1"),
        ),
    )
    second = FleetPlan(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        targets=(
            TargetPlan(target_id="a", name="first", kind="autonomous", region="eu-frankfurt-1"),
            TargetPlan(target_id="b", name="second", kind="dbcs", region="eu-frankfurt-1"),
        ),
    )

    assert first.plan_id == second.plan_id
    assert first.canonical_json() == second.canonical_json()


def test_fleet_plan_is_immutable_and_rejects_wrong_approval() -> None:
    plan = FleetPlan(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        targets=(TargetPlan(target_id="target", name="one", kind="dbcs", region="eu-frankfurt-1"),),
        settings={"nested": {"safe": True}},
    )

    with pytest.raises(FrozenInstanceError):
        plan.region = "us-chicago-1"  # type: ignore[misc]
    with pytest.raises(TypeError):
        plan.settings["nested"]["safe"] = False  # type: ignore[index]
    with pytest.raises(PlanApprovalMismatch):
        plan.require_approval("not-the-plan")
    plan.require_approval(plan.plan_id)


def test_checkpoint_is_retryable_but_cannot_reopen_complete_phase() -> None:
    run = RunManifest("run-1", "plan-1", (TargetManifest("target-1"),))

    running = run.transition_checkpoint("target-1", "dbm", PhaseState.RUNNING)
    retryable = running.transition_checkpoint("target-1", "dbm", PhaseState.RETRYABLE)
    complete = retryable.transition_checkpoint("target-1", "dbm", PhaseState.RUNNING).transition_checkpoint(
        "target-1", "dbm", PhaseState.COMPLETE
    )

    assert complete.target("target-1").checkpoint("dbm").attempts == 2  # type: ignore[union-attr]
    with pytest.raises(CheckpointTransitionError):
        complete.transition_checkpoint("target-1", "dbm", PhaseState.RUNNING)


def test_same_state_checkpoint_update_keeps_new_handoff_and_work_request_references() -> None:
    run = RunManifest("run-1", "plan-1", (TargetManifest("target-1"),)).transition_checkpoint(
        "target-1", "dbm", PhaseState.HANDED_OFF
    )

    enriched = run.transition_checkpoint(
        "target-1",
        "dbm",
        PhaseState.HANDED_OFF,
        handoff_ref="handoff-1",
        work_request_ref="work-request-1",
        message="awaiting operator",
    )

    checkpoint = enriched.target("target-1").checkpoint("dbm")
    assert checkpoint is not None
    assert checkpoint.attempts == 0
    assert checkpoint.handoff_ref == "handoff-1"
    assert checkpoint.work_request_ref == "work-request-1"
    assert checkpoint.message == "awaiting operator"


def test_only_lifecycle_owned_resources_enabled_by_the_run_allow_cleanup() -> None:
    owned = ResourceRecord("secret", "secret-ref", ResourceOwnership.OWNED, enabled_by_run=True)
    reused = ResourceRecord("endpoint", "endpoint-ref", ResourceOwnership.REUSED, enabled_by_run=True)
    preexisting = ResourceRecord("database", "database-ref", ResourceOwnership.PREEXISTING, enabled_by_run=True)

    assert owned.cleanup_allowed
    assert not reused.cleanup_allowed
    assert not preexisting.cleanup_allowed


def test_state_store_migrates_permissions_and_finds_resumable_runs(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    store = FleetStateStore(database)
    plan = _plan()
    manifest = RunManifest(
        "run-1",
        plan.plan_id,
        (TargetManifest("target-1", local_proof=ReadinessVerdict.READY, live_oci_proof=ReadinessVerdict.COLLECTING),),
    ).transition_checkpoint(
        "target-1", "dbm", PhaseState.RUNNING
    )

    store.save(manifest, plan=plan, approved_plan_id=plan.plan_id)

    assert store.schema_version == 2
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert store.load("run-1") == manifest
    assert store.resume_candidates(plan_id=plan.plan_id) == (manifest,)


def test_state_store_requires_approved_exact_plan_and_cannot_rebind_a_run(tmp_path: Path) -> None:
    store = FleetStateStore(tmp_path / "state.sqlite")
    first_plan = _plan("first")
    first_run = RunManifest("run-1", first_plan.plan_id, (TargetManifest("target-1"),))

    with pytest.raises(PlanApprovalMismatch):
        store.save(first_run, plan=first_plan, approved_plan_id="wrong")
    store.save(first_run, plan=first_plan, approved_plan_id=first_plan.plan_id)

    second_plan = _plan("second")
    rebound_run = RunManifest("run-1", second_plan.plan_id, (TargetManifest("target-1"),))
    with pytest.raises(RunPlanBindingError):
        store.save(rebound_run, plan=second_plan, approved_plan_id=second_plan.plan_id)
    assert store.load("run-1") == first_run


def test_state_store_upgrades_database_with_no_migrations(tmp_path: Path) -> None:
    database = tmp_path / "old.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE legacy (value TEXT)")
    connection.commit()
    connection.close()

    store = FleetStateStore(database)

    assert store.schema_version == 2
    assert store.load("missing") is None


def test_state_store_secure_creates_database_before_migration_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "state.sqlite"

    def fail_migration(self: FleetStateStore) -> None:
        raise sqlite3.DatabaseError("simulated migration failure")

    monkeypatch.setattr(FleetStateStore, "_migrate", fail_migration)

    with pytest.raises(sqlite3.DatabaseError, match="simulated migration failure"):
        FleetStateStore(database)
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_untouched_pending_targets_are_resume_candidates(tmp_path: Path) -> None:
    plan = _plan()
    manifest = RunManifest("run-pending", plan.plan_id, (TargetManifest("target-1"),))
    store = FleetStateStore(tmp_path / "state.sqlite")

    store.save(manifest, plan=plan, approved_plan_id=plan.plan_id)

    assert manifest.resumable
    assert store.resume_candidates() == (manifest,)


def test_evidence_excludes_topology_and_secret_references() -> None:
    manifest = RunManifest(
        "run-1",
        "plan-1",
        (
            TargetManifest(
                "ocid1.database.oc1..topology",
                readiness=ReadinessVerdict.READY,
                local_proof=ReadinessVerdict.READY,
                live_oci_proof=ReadinessVerdict.BLOCKED,
            ).with_resource(
                ResourceRecord("secret", "ocid1.secret.oc1..credential", ResourceOwnership.OWNED, enabled_by_run=True)
            ),
        ),
    ).transition_checkpoint(
        "ocid1.database.oc1..topology",
        "dbm",
        PhaseState.HANDED_OFF,
        handoff_ref="/private/host/topology-packet",
        work_request_ref="ocid1.workrequest.oc1..work-request",
    )

    payload = evidence_json(manifest)
    markdown = evidence_markdown(manifest)

    for forbidden in ("ocid1", "credential", "topology", "private/host", "work-request"):
        assert forbidden not in payload
        assert forbidden not in markdown
    assert '"local_proof":"ready"' in payload
    assert '"live_oci_proof":"blocked"' in payload


def test_evidence_sanitizes_untrusted_public_labels_and_markdown() -> None:
    unsafe = "dbm\n## leaked [link](https://host.example) password=not-safe"
    manifest = RunManifest("run\n# injected", "plan-1", (TargetManifest("target-1"),)).transition_checkpoint(
        "target-1", unsafe, PhaseState.RUNNING
    )
    manifest = manifest.with_target(
        manifest.target("target-1").with_resource(ResourceRecord(unsafe, "resource-ref", ResourceOwnership.OWNED))
    )

    payload = evidence_json(manifest)
    markdown = evidence_markdown(manifest)

    for forbidden in ("leaked", "link", "host.example", "password", "## leaked", "# injected"):
        assert forbidden not in payload
        assert forbidden not in markdown


def test_old_config_import_preserves_compatible_target_intent_without_secret_reference() -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        targets=(
            Target(
                kind="dbcs",
                name="orders",
                resource_id="resource-ref",
                password_secret_id="secret-ref",
                service_name="orders-service",
                monitoring_user="dbmon",
                database_resource_type="pluggable-database",
                external_host="db-host.example",
                external_os="linux",
                db_system_id="db-system-ref",
                private_endpoint_id="dbm-endpoint-ref",
                opsi_private_endpoint_id="opsi-endpoint-ref",
                data_safe_target_id="datasafe-target-ref",
                data_safe_private_endpoint_id="datasafe-endpoint-ref",
                management_agent_id="agent-ref",
                services=("dbm", "opsi", "logan"),
            ),
        ),
    )

    plan = fleet_plan_from_config(config)

    assert plan.profile == config.profile
    assert plan.targets[0].target_id == "resource-ref"
    assert plan.targets[0].services == ("dbm", "logan", "opsi")
    assert "secret-ref" not in plan.canonical_json()
    settings = plan.targets[0].settings
    assert settings["service_name"] == "orders-service"
    assert settings["monitoring_user"] == "dbmon"
    assert settings["database_resource_type"] == "pluggable-database"
    assert settings["external_host"] == "db-host.example"
    assert settings["external_os"] == "linux"
    assert settings["db_system_id"] == "db-system-ref"
    assert settings["private_endpoint_id"] == "dbm-endpoint-ref"
    assert settings["opsi_private_endpoint_id"] == "opsi-endpoint-ref"
    assert settings["data_safe_target_id"] == "datasafe-target-ref"
    assert settings["data_safe_private_endpoint_id"] == "datasafe-endpoint-ref"
    assert settings["management_agent_id"] == "agent-ref"


def test_old_config_import_allows_a_pdb_whose_cdb_is_outside_the_selected_fleet() -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        targets=(
            Target(
                kind="dbcs",
                name="pdb",
                service_name="pdb-service",
                database_role="PDB",
                parent_cdb_id="cdb-outside-fleet",
            ),
        ),
    )

    plan = fleet_plan_from_config(config)

    assert plan.targets[0].dependencies == ()


def test_plan_settings_reject_non_json_mutable_values_deterministically() -> None:
    with pytest.raises(TypeError, match="unsupported plan setting"):
        FleetPlan(
            profile="DEFAULT",
            region="eu-frankfurt-1",
            targets=(TargetPlan(target_id="target", name="one", kind="dbcs", region="eu-frankfurt-1"),),
            settings={"not_json": {"mutable"}},
        )
