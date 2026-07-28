from __future__ import annotations

from types import SimpleNamespace

from dbman_opsi.config import EnablementConfig, LogAnalyticsSelection, Target
from dbman_opsi.fleet import FleetPlan, RunManifest, TargetManifest, TargetPlan
from dbman_opsi.fleet_offboarding import CleanupPlanner, OciCleanupOperations
from dbman_opsi.fleet_operations import LifecycleOperations
from dbman_opsi.log_analytics import LogAnalyticsService, association_payload


def test_log_analytics_classifies_exact_existing_source_entity_associations_before_upsert(tmp_path) -> None:
    # Break caught: blindly upserting both items makes cleanup claim the alert
    # association that existed before this lifecycle run.
    existing = {"sourceName": "DBAlertLogSource", "entityId": "entity", "logGroupId": "old-group"}
    calls: list[list[dict[str, object]]] = []

    class Oci:
        runner = SimpleNamespace(dry_run=False)

        def list_log_analytics_entity_source_associations(self, namespace, compartment_id, entity_id):
            assert (namespace, compartment_id, entity_id) == ("namespace", "compartment", "entity")
            return [existing]

        def upsert_log_analytics_associations(self, namespace, compartment_id, items):
            assert (namespace, compartment_id) == ("namespace", "compartment")
            calls.append(items)

    target = Target(
        kind="autonomous", name="database", services=("logan",), logan_adb_entity_id="entity",
        logan_sources=("DBAlertLogSource", "DBAuditLogSource"),
    )
    config = EnablementConfig(
        profile="DEFAULT", region="eu-frankfurt-1", compartment_id="compartment",
        log_analytics=LogAnalyticsSelection(namespace="namespace", log_group_id="group"), targets=(target,),
    )

    decision = LogAnalyticsService(Oci()).enable_all(config, payload_dir=tmp_path)[-1]
    created = association_payload(target, "DBAuditLogSource", "entity", "group")

    assert tuple(getattr(decision, "created_association_items", ())) == (created,)
    assert tuple(getattr(decision, "preexisting_association_items", ())) == (
        association_payload(target, "DBAlertLogSource", "entity", "group"),
    )
    assert calls == [[created]]


def test_log_analytics_association_cleanup_only_dissociates_items_created_by_this_run() -> None:
    # Break caught: a lifecycle run claims an already-associated source as owned
    # and its later cleanup deletes an association that it did not create.
    created_item = {"sourceName": "DBAuditLogSource", "entityId": "entity", "logGroupId": "group"}
    reused_item = {"sourceName": "DBAlertLogSource", "entityId": "entity", "logGroupId": "group"}
    target = TargetPlan(
        "database", "database", "dbcs", "eu-frankfurt-1", compartment_id="compartment",
        services=("logan",), settings={"management_agent_id": "agent", "logan_namespace": "namespace"},
    )
    plan = FleetPlan("DEFAULT", "eu-frankfurt-1", (target,))
    operations = LifecycleOperations(plan, object())
    operations.log_analytics = SimpleNamespace(enable_all=lambda _config: [
        SimpleNamespace(
            target="database",
            status="configured",
            detail="two exact associations classified",
            association_items=(created_item, reused_item),
            created_association_items=(created_item,),
            preexisting_association_items=(reused_item,),
            logan_database_entity_id=None,
            logan_host_entity_id=None,
            logan_listener_entity_id=None,
        )
    ])

    outcome = operations.agent_log_analytics(target)
    manifest = RunManifest("run-1", plan.plan_id, (TargetManifest("database", resources=outcome.resources),))
    cleanup = CleanupPlanner(plan, manifest).build()

    assert len(outcome.resources) == 2
    assert len(cleanup.actions) == 1
    assert cleanup.actions[0].operation == "dissociate-log-analytics"
    assert cleanup.actions[0].arguments == {
        "region": "eu-frankfurt-1",
        "namespace": "namespace",
        "compartment_id": "compartment",
        "items": (created_item,),
    }

    deleted: list[tuple[str, str, list[dict[str, str]]]] = []
    oci = SimpleNamespace(
        delete_log_analytics_associations=lambda namespace, compartment_id, items: deleted.append(
            (namespace, compartment_id, items)
        )
    )
    OciCleanupOperations(oci).execute_cleanup(cleanup.actions[0])

    assert deleted == [("namespace", "compartment", [created_item])]
