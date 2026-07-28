"""Ownership-safe, plan-gated reverse cleanup for fleet lifecycle runs.

This module deliberately plans only from the durable run manifest.  A resource
that was discovered or reused is therefore never inferred to be safe to remove
from its name, tags, or a fresh tenancy lookup.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from dbman_opsi.fleet import DeploymentMode, FleetPlan, PlanApprovalMismatch, RunManifest, TargetPlan
from dbman_opsi.fleet_handoff import target_handle
from dbman_opsi.runner import OciAlreadyDone, OciNotFound


def _normalized_resource_kind(value: str) -> str:
    """Normalize spelling only; never infer a destructive kind from a substring."""

    return re.sub(r"-+", "-", value.strip().lower().replace("_", "-").replace(" ", "-"))


_ENDPOINT_ROUTES = {
    "dbm-private-endpoint": "delete_db_management_private_endpoint",
    "database-management-private-endpoint": "delete_db_management_private_endpoint",
    "opsi-private-endpoint": "delete_opsi_private_endpoint",
    "operations-insights-private-endpoint": "delete_opsi_private_endpoint",
    "data-safe-private-endpoint": "delete_data_safe_private_endpoint",
    "datasafe-private-endpoint": "delete_data_safe_private_endpoint",
}
_NETWORK_ROUTES = {
    "subnet": "delete_run_owned_subnet",
    "vcn": "delete_run_owned_vcn",
    "route-table": "delete_run_owned_route_table",
    "security-list": "delete_run_owned_security_list",
}
_ENDPOINT_NETWORK_TOKENS = frozenset(
    ("endpoint", "subnet", "vcn", "route", "table", "security", "list", "network", "gateway")
)


class DatabaseDeletionRefused(ValueError):
    """Raised when a cleanup attempts the separately guarded database deletion."""


class CleanupHandoffRequired(RuntimeError):
    """The resource requires an approved DB/host operations adapter."""


def _canonical_envelope(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _redacted(value: object) -> bool:
    return not any(marker in str(value).lower() for marker in ("password", "secret", "ocid1.", "private key", "token"))


def cleanup_action_handle(action_id: str) -> str:
    """Opaque action reference suitable for an operator-facing cleanup packet."""

    return target_handle("cleanup:" + action_id)


def _freeze_arguments(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("cleanup action argument keys must be strings")
        return MappingProxyType({key: _freeze_arguments(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_arguments(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"unsupported cleanup action argument type: {type(value).__name__}")


def _canonical_arguments(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical_arguments(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, tuple):
        return [_canonical_arguments(item) for item in value]
    return value


@dataclass(frozen=True)
class CleanupAction:
    """One deterministic, manifest-authorized reverse-lifecycle operation."""

    operation: str
    target_id: str
    target_kind: str
    resource_type: str
    resource_ref: str
    arguments: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", _freeze_arguments(self.arguments))

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "target_id": self.target_id,
            "target_kind": self.target_kind,
            "resource_type": self.resource_type,
            "resource_ref": self.resource_ref,
            "arguments": _canonical_arguments(self.arguments),
        }

    @property
    def action_id(self) -> str:
        payload = json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def normalized_resource_kind(self) -> str:
        return _normalized_resource_kind(self.resource_type)

    @property
    def deletes_test_database(self) -> bool:
        return self.operation == "delete-test-database"


@dataclass(frozen=True)
class CleanupPlan:
    """An immutable cleanup plan with its own exact-approval identity."""

    run_id: str
    source_plan_id: str
    deployment_mode: DeploymentMode
    actions: tuple[CleanupAction, ...]
    evidence_retention_days: int = 7

    def __post_init__(self) -> None:
        if not self.run_id or not self.source_plan_id:
            raise ValueError("cleanup plans require a run and source plan")
        if self.evidence_retention_days != 7:
            raise ValueError("cleanup evidence retention must be exactly seven days")
        if self.deployment_mode is DeploymentMode.PRODUCTION and any(
            action.deletes_test_database for action in self.actions
        ):
            raise DatabaseDeletionRefused("production cleanup plans may not delete databases")
        object.__setattr__(self, "actions", tuple(self.actions))

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_plan_id": self.source_plan_id,
            "deployment_mode": self.deployment_mode.value,
            "evidence_retention_days": self.evidence_retention_days,
            "actions": [action.canonical_dict() for action in self.actions],
        }

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @property
    def plan_id(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @property
    def database_confirmation(self) -> str:
        """The literal second confirmation required for test database deletion."""

        return f"DELETE TEST DATABASES FOR RUN {self.run_id}"

    @property
    def requires_database_confirmation(self) -> bool:
        return any(action.deletes_test_database for action in self.actions)

    def require_approval(self, approved_plan_id: str) -> None:
        if approved_plan_id != self.plan_id:
            raise PlanApprovalMismatch("approval does not match the reviewed cleanup plan")


def public_cleanup_summary(plan: CleanupPlan) -> dict[str, Any]:
    """Return the operator-review surface without manifest resource references.

    Every planned action has already passed the ownership and enabled-by-run
    gate.  The full arguments and resource references remain solely in the
    private immutable cleanup plan that its ID commits to.
    """

    actions = []
    for index, action in enumerate(plan.actions, start=1):
        actions.append(
            {
                "order": index,
                "operation": action.operation,
                "target_kind": action.target_kind,
                "target_handle": target_handle(f"{plan.run_id}:{action.target_id}"),
                "resource_handle": cleanup_action_handle(action.action_id),
                "ownership": "owned",
                "created": True,
                "enabled_by_run": True,
                "handoff_required": action.operation == "handoff-cleanup",
                "requires_database_confirmation": action.deletes_test_database,
            }
        )
    return {
        "cleanup_plan_id": plan.plan_id,
        "action_count": len(actions),
        "requires_database_confirmation": plan.requires_database_confirmation,
        "actions": actions,
    }


class CleanupPlanner:
    """Build a reverse dependency plan from only owned, run-enabled resources."""

    _PHASE_ORDER = {
        "dissociate-log-analytics": 0,
        "disable-opsi": 1,
        "unregister-data-safe": 2,
        "disable-dbm-pdb": 2,
        "disable-dbm-cdb": 3,
        "delete-named-credential": 4,
        "delete-database-user": 4,
        "delete-secret": 4,
        "delete-endpoint": 5,
        "delete-network": 5,
        "handoff-cleanup": 5,
        "delete-test-database": 6,
    }

    def __init__(
        self,
        plan: FleetPlan,
        manifest: RunManifest,
        *,
        delete_test_databases: bool = False,
    ) -> None:
        if manifest.plan_id != plan.plan_id:
            raise ValueError("cleanup manifest is not bound to the supplied fleet plan")
        self.plan = plan
        self.manifest = manifest
        self.delete_test_databases = delete_test_databases

    def build(self) -> CleanupPlan:
        target_plans = {target.target_id: target for target in self.plan.targets}
        actions: list[CleanupAction] = []
        for target in self.manifest.targets:
            target_plan = target_plans.get(target.target_id)
            if target_plan is None:
                raise ValueError(f"cleanup manifest target is absent from fleet plan: {target.target_id}")
            for resource in target.resources:
                # Both signals are required.  This protects a resource created by
                # a different lifecycle action and services that were already on.
                if not resource.cleanup_allowed:
                    continue
                operation = self._operation_for(resource.resource_type, target_plan.kind)
                if operation is None:
                    operation = "handoff-cleanup"
                if operation == "delete-test-database":
                    if not self.delete_test_databases:
                        continue
                    if self.plan.deployment_mode is DeploymentMode.PRODUCTION:
                        raise DatabaseDeletionRefused("production cleanup never deletes databases")
                actions.append(
                    CleanupAction(
                        operation=operation,
                        target_id=target.target_id,
                        target_kind=target_plan.kind,
                        resource_type=resource.resource_type,
                        resource_ref=resource.resource_ref,
                        arguments=self._arguments_for(
                            operation, target_plan, resource.attributes, resource.resource_ref, resource.resource_type
                        ),
                    )
                )
        actions.sort(
            key=lambda action: (
                self._PHASE_ORDER[action.operation],
                action.target_id,
                action.resource_type,
                action.resource_ref,
            )
        )
        return CleanupPlan(
            run_id=self.manifest.run_id,
            source_plan_id=self.plan.plan_id,
            deployment_mode=self.plan.deployment_mode,
            actions=tuple(actions),
        )

    @staticmethod
    def _arguments_for(
        operation: str,
        target: TargetPlan,
        attributes: Mapping[str, Any],
        resource_ref: str,
        resource_type: str,
    ) -> Mapping[str, Any]:
        values = dict(attributes)
        base: dict[str, Any] = {"region": target.region}
        if operation == "dissociate-log-analytics":
            return {
                **base,
                "namespace": values.get("namespace") or target.settings.get("logan_namespace"),
                "compartment_id": values.get("compartment_id") or target.compartment_id,
                "items": values.get("items", ()),
            }
        if operation == "disable-opsi":
            return {**base, "insight_id": values.get("insight_id", resource_ref)}
        if operation == "unregister-data-safe":
            return {
                **base,
                "target_database_id": values.get("target_database_id", resource_ref),
            }
        if operation == "disable-dbm-pdb":
            return {
                **base,
                "pluggable_database_id": values.get("pluggable_database_id", resource_ref),
                "feature": values.get("feature") or values.get("dbm_feature"),
            }
        if operation == "disable-dbm-cdb":
            return {
                **base,
                "database_id": values.get("database_id", resource_ref),
                "feature": values.get("feature") or values.get("dbm_feature"),
                "can_disable_all_pdbs": bool(values.get("can_disable_all_pdbs", False)),
            }
        if operation == "delete-named-credential":
            return {**base, "credential_id": values.get("credential_id", resource_ref)}
        if operation == "delete-database-user":
            return {**base, "database_user": values.get("database_user", resource_ref)}
        if operation == "delete-secret":
            return {**base, "secret_id": values.get("secret_id", resource_ref)}
        if operation == "delete-endpoint":
            return {
                **base,
                "resource_kind": _normalized_resource_kind(resource_type),
                "endpoint_id": values.get("endpoint_id", resource_ref),
                "unused": values.get("unused") is True,
            }
        if operation == "delete-network":
            return {
                **base,
                "resource_kind": _normalized_resource_kind(resource_type),
                "network_id": values.get("network_id", resource_ref),
                "unused": values.get("unused") is True,
            }
        if operation == "delete-test-database":
            return {
                **base,
                "database_id": values.get("database_id", resource_ref),
                "database_family": values.get("database_family"),
            }
        if operation == "handoff-cleanup":
            return {
                **base,
                "resource_kind": _normalized_resource_kind(resource_type),
                "handoff_reason": values.get("handoff_reason")
                or f"no supported OCI cleanup route for run-owned {resource_type}",
            }
        return base

    @staticmethod
    def _operation_for(resource_type: str, target_kind: str) -> str | None:
        value = _normalized_resource_kind(resource_type)
        kind = target_kind.lower()
        if value in _ENDPOINT_ROUTES:
            return "delete-endpoint"
        if value in _NETWORK_ROUTES:
            return "delete-network"
        if set(value.split("-")) & _ENDPOINT_NETWORK_TOKENS:
            return "handoff-cleanup"
        if "log" in value and ("association" in value or "assoc" in value):
            return "dissociate-log-analytics"
        if "opsi" in value:
            return "disable-opsi"
        if value == "datasafe-target":
            return "unregister-data-safe"
        if "dbm" in value or "database-management" in value:
            return "disable-dbm-pdb" if "pdb" in value or "pdb" in kind else "disable-dbm-cdb"
        if "credential" in value:
            return "delete-named-credential"
        if value in {"database-user", "db-user", "monitoring-user"}:
            return "delete-database-user"
        if "secret" in value:
            return "delete-secret"
        if "test" in value and ("database" in value or value.endswith("-db")):
            return "delete-test-database"
        return None


class CleanupOperations(Protocol):
    """OCI adapter boundary; implementations receive only manifest-planned work."""

    def execute_cleanup(self, action: CleanupAction) -> None: ...


class CleanupHandoffPacketWriter:
    """Issue signed, redacted cleanup instructions and completion envelopes."""

    def __init__(self, directory: str | Path, *, signing_key: bytes) -> None:
        self.directory = Path(directory)
        self.signing_key = signing_key

    def write(self, *, plan: CleanupPlan, action: CleanupAction, instructions: str) -> Path:
        if not instructions.strip() or not _redacted(instructions):
            raise ValueError("cleanup handoff instructions must be non-empty and redacted")
        issued = {
            "version": 1,
            "run_id": plan.run_id,
            "cleanup_plan_id": plan.plan_id,
            "action_id": action.action_id,
            "action_handle": cleanup_action_handle(action.action_id),
            "action_kind": action.operation,
            "instructions": instructions,
            "issued_at": int(time.time()),
            "nonce": secrets.token_hex(16),
        }
        if not _redacted(issued):
            raise ValueError("cleanup handoff binding is not redacted")
        digest = hashlib.sha256(_canonical_envelope(issued)).hexdigest()
        signed = {"issued": issued, "issued_packet_digest": digest}
        document = {
            **signed,
            "signature": hmac.new(self.signing_key, _canonical_envelope(signed), hashlib.sha256).hexdigest(),
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{issued['action_handle']}.cleanup-handoff.json"
        self._write_json(path, document)
        return path

    def binding_for(self, issued_packet: str | Path) -> dict[str, str]:
        document = self._read_issued(issued_packet)
        issued = document["issued"]
        return {
            "action_handle": str(issued["action_handle"]),
            "action_kind": str(issued["action_kind"]),
            "issued_handoff_ref": "sha256:" + str(document["issued_packet_digest"]),
            "issued_packet_digest": str(document["issued_packet_digest"]),
        }

    def write_completion(
        self, issued_packet: str | Path, *, attestation: str, result: str, evidence_timestamp: int | None = None
    ) -> Path:
        document = self._read_issued(issued_packet)
        issued = document["issued"]
        evidence = {
            "version": 1,
            "run_id": issued["run_id"],
            "cleanup_plan_id": issued["cleanup_plan_id"],
            "action_id": issued["action_id"],
            "action_handle": issued["action_handle"],
            "action_kind": issued["action_kind"],
            "issued_handoff_ref": "sha256:" + document["issued_packet_digest"],
            "issued_packet_digest": document["issued_packet_digest"],
            "attestation": attestation,
            "result": result,
            "evidence_timestamp": evidence_timestamp or int(time.time()),
            "nonce": secrets.token_hex(16),
        }
        if not self._valid_completion(evidence):
            raise ValueError("cleanup completion requires redacted attestation and allowlisted result")
        completion = {
            "evidence": evidence,
            "signature": hmac.new(self.signing_key, _canonical_envelope(evidence), hashlib.sha256).hexdigest(),
        }
        path = Path(issued_packet).with_suffix(".cleanup-completion.json")
        self._write_json(path, completion)
        return path

    def _read_issued(self, path: str | Path) -> dict[str, Any]:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        signed = {"issued": document.get("issued"), "issued_packet_digest": document.get("issued_packet_digest")}
        if not isinstance(signed["issued"], dict) or not isinstance(document.get("signature"), str):
            raise ValueError("invalid cleanup handoff packet")
        expected = hmac.new(self.signing_key, _canonical_envelope(signed), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, document["signature"]):
            raise ValueError("cleanup handoff packet signature is invalid")
        if hashlib.sha256(_canonical_envelope(signed["issued"])).hexdigest() != signed["issued_packet_digest"]:
            raise ValueError("cleanup handoff packet digest is invalid")
        if not _redacted(signed["issued"]):
            raise ValueError("cleanup handoff packet is not redacted")
        return document

    @staticmethod
    def _valid_completion(evidence: Mapping[str, Any]) -> bool:
        return (
            isinstance(evidence.get("attestation"), str)
            and bool(str(evidence["attestation"]).strip())
            and str(evidence.get("result", "")).lower() in {"completed", "succeeded", "verified"}
            and isinstance(evidence.get("evidence_timestamp"), int)
            and int(evidence["evidence_timestamp"]) > 0
            and bool(evidence.get("nonce"))
            and _redacted(evidence)
        )

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
        descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True)


class OciCleanupOperations:
    """Production adapter that maps each approved action to its OCI facade call."""

    def __init__(self, oci: Any) -> None:
        self.oci = oci

    def execute_cleanup(self, action: CleanupAction) -> None:
        values = action.arguments
        if action.operation == "dissociate-log-analytics":
            items = [dict(item) for item in self._required(values, "items")]
            self.oci.delete_log_analytics_associations(
                self._required(values, "namespace"),
                self._required(values, "compartment_id"),
                items,
            )
            return
        if action.operation == "disable-opsi":
            self.oci.disable_opsi_database_insight(self._required(values, "insight_id"))
            return
        if action.operation == "unregister-data-safe":
            self.oci.delete_data_safe_target(
                self._required(values, "target_database_id")
            )
            return
        if action.operation == "disable-dbm-pdb":
            if not values.get("feature"):
                self.oci.disable_pluggable_database_management(
                    self._required(values, "pluggable_database_id")
                )
                return
            self.oci.disable_dbm_pdb(
                self._required(values, "pluggable_database_id"), self._required(values, "feature")
            )
            return
        if action.operation == "disable-dbm-cdb":
            if not values.get("feature"):
                database_id = self._required(values, "database_id")
                if action.target_kind == "autonomous":
                    self.oci.disable_autonomous_database_management(database_id)
                else:
                    self.oci.disable_database_management(database_id)
                return
            self.oci.disable_dbm_cdb(
                self._required(values, "database_id"),
                self._required(values, "feature"),
                can_disable_all_pdbs=bool(values.get("can_disable_all_pdbs", False)),
            )
            return
        if action.operation == "delete-named-credential":
            self.oci.delete_named_credential(self._required(values, "credential_id"))
            return
        if action.operation == "delete-database-user":
            raise CleanupHandoffRequired("database-user removal requires the signed DB operations adapter")
        if action.operation == "delete-secret":
            self.oci.schedule_run_owned_secret_deletion(self._required(values, "secret_id"))
            return
        if action.operation == "delete-endpoint":
            self._require_unused(values)
            endpoint_id = self._required(values, "endpoint_id")
            route = _ENDPOINT_ROUTES.get(action.normalized_resource_kind)
            if route is None:
                raise CleanupHandoffRequired("endpoint kind is not supported by the OCI cleanup adapter")
            getattr(self.oci, route)(endpoint_id)
            return
        if action.operation == "delete-network":
            self._require_unused(values)
            network_id = self._required(values, "network_id")
            route = _NETWORK_ROUTES.get(action.normalized_resource_kind)
            if route is None:
                raise CleanupHandoffRequired("network kind is not supported by the OCI cleanup adapter")
            getattr(self.oci, route)(network_id)
            return
        if action.operation == "delete-test-database":
            database_id = self._required(values, "database_id")
            family = str(values.get("database_family") or action.resource_type).lower()
            if "autonomous" in family or "adb" in family:
                self.oci.delete_run_owned_autonomous_test_database(database_id)
            else:
                self.oci.delete_run_owned_dbcs_test_database(database_id)
            return
        if action.operation == "handoff-cleanup":
            raise CleanupHandoffRequired(str(self._required(values, "handoff_reason")))
        raise CleanupHandoffRequired(f"unsupported cleanup operation: {action.operation}")

    @staticmethod
    def _required(values: Mapping[str, Any], name: str) -> Any:
        value = values.get(name)
        if value is None or value == "" or value == ():
            raise CleanupHandoffRequired(f"cleanup action is missing required manifest argument: {name}")
        return value

    @staticmethod
    def _require_unused(values: Mapping[str, Any]) -> None:
        if values.get("unused") is not True:
            raise CleanupHandoffRequired("endpoint/network use has not been proven absent")


@dataclass(frozen=True)
class CleanupEvidenceMetadata:
    """Sanitized, seven-day retention metadata; never includes resource references."""

    run_id: str
    cleanup_plan_id: str
    retained_until: str
    completed_actions: int
    failed_actions: int

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "cleanup_plan_id": self.cleanup_plan_id,
            "retained_until": self.retained_until,
            "completed_actions": self.completed_actions,
            "failed_actions": self.failed_actions,
        }


@dataclass(frozen=True)
class CleanupExecution:
    """Resumable outcome keyed by opaque action digests, not OCI identifiers."""

    cleanup_plan_id: str
    action_states: dict[str, str]
    evidence: CleanupEvidenceMetadata | None

    @property
    def partial(self) -> bool:
        return any(state != "complete" for state in self.action_states.values())

    @property
    def complete(self) -> bool:
        return all(state == "complete" for state in self.action_states.values())


class CleanupExecutor:
    """Execute reverse cleanup independently, preserving failed work for resume."""

    def __init__(
        self,
        plan: CleanupPlan,
        store: Any,
        operations: CleanupOperations,
        *,
        handoff_writer: CleanupHandoffPacketWriter | None = None,
    ) -> None:
        self.plan = plan
        self.store = store
        self.operations = operations
        self.handoff_writer = handoff_writer

    def execute(
        self,
        *,
        approved_plan_id: str,
        database_confirmation: str | None = None,
        now: datetime | None = None,
    ) -> CleanupExecution:
        self.plan.require_approval(approved_plan_id)
        timestamp = now or datetime.now(UTC)
        purge = getattr(self.store, "purge_expired_cleanup_evidence", None)
        if callable(purge):
            purge(now=timestamp)
        if self.plan.deployment_mode is DeploymentMode.PRODUCTION and self.plan.requires_database_confirmation:
            raise DatabaseDeletionRefused("production cleanup never deletes databases")
        if self.plan.requires_database_confirmation and database_confirmation != self.plan.database_confirmation:
            raise DatabaseDeletionRefused("typed database deletion confirmation does not match this cleanup run")

        stored = self.store.load_cleanup_state(
            run_id=self.plan.run_id, cleanup_plan_id=self.plan.plan_id
        ) or {}
        raw_states = stored.get("action_states", {})
        states = {
            str(action_id): str(state)
            for action_id, state in raw_states.items()
        } if isinstance(raw_states, dict) else {}
        raw_handoffs = stored.get("handoffs", {})
        handoffs = {
            str(action_id): {str(key): str(value) for key, value in binding.items()}
            for action_id, binding in raw_handoffs.items()
            if isinstance(binding, dict)
        } if isinstance(raw_handoffs, dict) else {}
        raw_evidence = stored.get("evidence", {})
        retained_until = (
            str(raw_evidence.get("retained_until"))
            if isinstance(raw_evidence, dict) and raw_evidence.get("retained_until")
            else None
        )
        latest: CleanupExecution | None = None
        for action in self.plan.actions:
            # Complete checkpoints make an exact repeated offboard a true no-op.
            if states.get(action.action_id) == "complete":
                continue
            try:
                self.operations.execute_cleanup(action)
            except (OciNotFound, OciAlreadyDone):
                states[action.action_id] = "complete"
            except CleanupHandoffRequired:
                if self.handoff_writer is None:
                    # Do not create an unsigned, resumable cleanup handoff.
                    states[action.action_id] = "failed"
                    latest = self._save(states, handoffs=handoffs, now=timestamp, retained_until=retained_until)
                    retained_until = latest.evidence.retained_until
                    continue
                states[action.action_id] = "handed-off"
                if self.handoff_writer is not None and action.action_id not in handoffs:
                    instructions = action.arguments.get("handoff_reason")
                    if not isinstance(instructions, str) or not _redacted(instructions):
                        instructions = "Complete the reviewed lifecycle cleanup action with approved operator access."
                    issued = self.handoff_writer.write(
                        plan=self.plan, action=action, instructions=instructions
                    )
                    handoffs[action.action_id] = self.handoff_writer.binding_for(issued)
            except Exception:
                # Do not abort independent cleanup actions.  The next exact
                # invocation replays only these failed action digests.
                states[action.action_id] = "failed"
            else:
                states[action.action_id] = "complete"
            latest = self._save(
                states, handoffs=handoffs, now=timestamp, retained_until=retained_until
            )
            retained_until = latest.evidence.retained_until
        # An all-complete repeated cleanup must not mutate its checkpoint or
        # extend the seven-day evidence window.
        if latest is not None:
            return latest
        if states and retained_until is None:
            # The terminal checkpoint survived evidence expiry specifically to
            # preserve idempotency.  Do not silently recreate expired evidence.
            return CleanupExecution(self.plan.plan_id, dict(states), None)
        return self._execution(states, now=timestamp, retained_until=retained_until)

    def _save(
        self,
        states: dict[str, str],
        *,
        handoffs: Mapping[str, Mapping[str, str]],
        now: datetime | None,
        retained_until: str | None,
    ) -> CleanupExecution:
        execution = self._execution(states, now=now, retained_until=retained_until)
        evidence = execution.evidence
        if evidence is None:  # _save is only used for a newly executed action.
            raise RuntimeError("cannot persist cleanup execution without evidence metadata")
        # Only opaque action ids and aggregate evidence are persisted.  Detailed
        # failures often contain OCIDs or endpoint names, so they are excluded.
        state: dict[str, object] = {"action_states": dict(states), "evidence": evidence.to_dict()}
        if handoffs:
            state["handoffs"] = {key: dict(value) for key, value in handoffs.items()}
        self.store.save_cleanup_state(
            run_id=self.plan.run_id,
            cleanup_plan_id=self.plan.plan_id,
            state=state,
        )
        return execution

    def _execution(
        self,
        states: dict[str, str],
        *,
        now: datetime | None,
        retained_until: str | None,
    ) -> CleanupExecution:
        timestamp = now or datetime.now(UTC)
        evidence = CleanupEvidenceMetadata(
            run_id=self.plan.run_id,
            cleanup_plan_id=self.plan.plan_id,
            retained_until=retained_until
            or (timestamp + timedelta(days=self.plan.evidence_retention_days)).isoformat(),
            completed_actions=sum(state == "complete" for state in states.values()),
            failed_actions=sum(state == "failed" for state in states.values()),
        )
        return CleanupExecution(self.plan.plan_id, dict(states), evidence)


def _execution_from_cleanup_state(plan: CleanupPlan, state: Mapping[str, Any]) -> CleanupExecution:
    raw_states = state.get("action_states", {})
    states = {str(key): str(value) for key, value in raw_states.items()} if isinstance(raw_states, dict) else {}
    raw_evidence = state.get("evidence")
    evidence = None
    if isinstance(raw_evidence, dict):
        try:
            evidence = CleanupEvidenceMetadata(
                run_id=str(raw_evidence["run_id"]),
                cleanup_plan_id=str(raw_evidence["cleanup_plan_id"]),
                retained_until=str(raw_evidence["retained_until"]),
                completed_actions=int(raw_evidence["completed_actions"]),
                failed_actions=int(raw_evidence["failed_actions"]),
            )
        except (KeyError, TypeError, ValueError):
            evidence = None
    return CleanupExecution(plan.plan_id, states, evidence)


class CleanupHandoffEvidenceImporter:
    """Verify signed manual completion before closing an immutable cleanup action."""

    def __init__(self, store: Any, plan: CleanupPlan, *, signing_key: bytes) -> None:
        self.store = store
        self.plan = plan
        self.signing_key = signing_key

    def import_packet(self, path: str | Path, *, approved_plan_id: str) -> CleanupExecution:
        self.plan.require_approval(approved_plan_id)
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        evidence = document.get("evidence")
        if not isinstance(evidence, dict) or not isinstance(document.get("signature"), str):
            raise ValueError("cleanup completion evidence envelope is required")
        expected_signature = hmac.new(
            self.signing_key, _canonical_envelope(evidence), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected_signature, document["signature"]):
            raise ValueError("cleanup completion evidence signature is invalid")
        if not CleanupHandoffPacketWriter._valid_completion(evidence):
            raise ValueError("cleanup completion evidence attestation/result is invalid")
        if evidence.get("cleanup_plan_id") != self.plan.plan_id:
            raise ValueError("cleanup completion evidence belongs to a different plan")
        if evidence.get("run_id") != self.plan.run_id:
            raise ValueError("cleanup completion evidence belongs to a different run")
        action_id = str(evidence.get("action_id") or "")
        actions = [action for action in self.plan.actions if action.action_id == action_id]
        if len(actions) != 1:
            raise ValueError("cleanup completion evidence action binding is invalid")
        action = actions[0]
        if (
            evidence.get("action_handle") != cleanup_action_handle(action.action_id)
            or evidence.get("action_kind") != action.operation
        ):
            raise ValueError("cleanup completion evidence action handle binding is invalid")
        state = self.store.load_cleanup_state(
            run_id=self.plan.run_id, cleanup_plan_id=self.plan.plan_id
        )
        if not isinstance(state, dict):
            raise ValueError("cleanup completion evidence references an unknown cleanup run")
        raw_handoffs = state.get("handoffs")
        binding = raw_handoffs.get(action_id) if isinstance(raw_handoffs, dict) else None
        expected_ref = "sha256:" + str(evidence.get("issued_packet_digest"))
        expected_binding = {
            "action_handle": cleanup_action_handle(action.action_id),
            "action_kind": action.operation,
            "issued_handoff_ref": expected_ref,
            "issued_packet_digest": str(evidence.get("issued_packet_digest")),
        }
        if not isinstance(binding, dict) or any(
            evidence.get(name) != expected
            for name, expected in {
                "action_handle": cleanup_action_handle(action.action_id),
                "action_kind": action.operation,
                "issued_handoff_ref": expected_ref,
                "issued_packet_digest": evidence.get("issued_packet_digest"),
            }.items()
        ) or any(binding.get(name) != expected for name, expected in expected_binding.items()):
            raise ValueError("cleanup completion evidence issued handoff binding is invalid")
        raw_states = state.get("action_states")
        if not isinstance(raw_states, dict):
            raise ValueError("cleanup completion evidence has no cleanup checkpoint")
        current = raw_states.get(action_id)
        if current == "complete":
            return _execution_from_cleanup_state(self.plan, state)
        if current != "handed-off":
            raise ValueError("cleanup completion evidence does not match a pending handoff")
        updated = dict(state)
        updated_states = {str(key): str(value) for key, value in raw_states.items()}
        updated_states[action_id] = "complete"
        updated["action_states"] = updated_states
        self.store.save_cleanup_state(
            run_id=self.plan.run_id, cleanup_plan_id=self.plan.plan_id, state=updated
        )
        return _execution_from_cleanup_state(self.plan, updated)
