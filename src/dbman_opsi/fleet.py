"""Schema-versioned, immutable models for a fleet lifecycle plan and run."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from dbman_opsi.config import EnablementConfig


FLEET_SCHEMA_VERSION = 1
_PUBLIC_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_OWNERSHIP_POLICIES = frozenset({"run-owned-only"})


class DeploymentMode(str, Enum):
    """The lifecycle safety profile selected for a fleet run."""

    PRODUCTION = "production"
    POC = "poc"
    # Backward-compatible alias for older answer files.
    PILOT = "poc"
    DEMO = "demo"


class CredentialPolicy(str, Enum):
    """How monitoring credentials may be created or supplied."""

    SHARED_USER_UNIQUE_SECRET = "shared-user-unique-secret"
    SHARED_USER_SHARED_SECRET = "shared-user-shared-secret"
    DEDICATED_USER_UNIQUE_SECRET = "dedicated-user-unique-secret"
    # Import-only legacy spellings. New plans expose only the three policies
    # above; keeping these values avoids breaking durable manifests/YAML.
    UNIQUE_VAULT_PER_ACCOUNT = "unique-vault-per-account"
    EXISTING_VAULT_ONLY = "existing-vault-only"
    HANDOFF_REQUIRED = "handoff-required"
    SHARED_PASSWORD = "shared-password"


@dataclass(frozen=True)
class DiscoveryScope:
    """Immutable tenancy discovery boundary; empty inclusions mean whole scope."""

    subscribed_regions: tuple[str, ...] = ()
    accessible_compartments: tuple[str, ...] = ()
    include_regions: tuple[str, ...] = ()
    exclude_regions: tuple[str, ...] = ()
    include_compartments: tuple[str, ...] = ()
    exclude_compartments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, tuple(sorted(set(str(value) for value in getattr(self, name)))))

    def canonical_dict(self) -> dict[str, Any]:
        return {name: list(getattr(self, name)) for name in self.__dataclass_fields__}


class ResourceOwnership(str, Enum):
    """Whether a resource may be changed or removed by lifecycle cleanup."""

    OWNED = "owned"
    # ``CREATED`` is the lifecycle-facing spelling.  Keep the original value
    # stable for manifests written before offboarding was introduced.
    CREATED = "owned"
    REUSED = "reused"
    PREEXISTING = "preexisting"


class ResourceEffect(str, Enum):
    """The observed effect of one lifecycle operation.

    This is intentionally separate from cleanup ownership: a pre-existing
    resource can be enabled by this run, but it is never consequently owned by
    it.  The explicit spelling also makes persisted manifests reviewable.
    """

    CREATED = "created"
    REUSED = "reused"
    PREEXISTING = "preexisting"


class TargetState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    BLOCKED = "blocked"
    HANDED_OFF = "handed-off"


class PhaseState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    RETRYABLE = "retryable"
    BLOCKED = "blocked"
    HANDED_OFF = "handed-off"


class ReadinessVerdict(str, Enum):
    CONFIGURED = "configured"
    COLLECTING = "collecting"
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    HANDED_OFF = "handed-off"


class PlanApprovalMismatch(ValueError):
    """Raised when a write is not approved for the exact reviewed plan."""


class CheckpointTransitionError(ValueError):
    """Raised when a checkpoint would move backwards or reopen a final phase."""


def _canonical(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if hasattr(value, "canonical_dict"):
        return value.canonical_dict()
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("plan setting mapping keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise TypeError(f"unsupported plan setting type: {type(value).__name__}")


def _frozen_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _freeze(item) for key, item in (value or {}).items()})


@dataclass(frozen=True)
class TargetPlan:
    """A stable, dependency-aware target selected for one fleet plan."""

    target_id: str
    name: str
    kind: str
    region: str
    compartment_id: str | None = None
    resource_id: str | None = None
    services: tuple[str, ...] = ("dbm", "opsi")
    dependencies: tuple[str, ...] = ()
    credential_policy: CredentialPolicy = CredentialPolicy.SHARED_USER_UNIQUE_SECRET
    settings: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.target_id:
            raise ValueError("target_id is required")
        if not self.name:
            raise ValueError("target name is required")
        if not self.kind:
            raise ValueError("target kind is required")
        if not self.region:
            raise ValueError("target region is required")
        if self.target_id in self.dependencies:
            raise ValueError("a target cannot depend on itself")
        object.__setattr__(self, "services", tuple(sorted(set(self.services))))
        object.__setattr__(self, "dependencies", tuple(sorted(set(self.dependencies))))
        object.__setattr__(self, "settings", _frozen_mapping(self.settings))

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "name": self.name,
            "kind": self.kind,
            "region": self.region,
            "compartment_id": self.compartment_id,
            "resource_id": self.resource_id,
            "services": list(self.services),
            "dependencies": list(self.dependencies),
            "credential_policy": self.credential_policy.value,
            "settings": _canonical(self.settings),
        }


@dataclass(frozen=True)
class FleetPlan:
    """A deterministic plan which must be approved before lifecycle writes."""

    profile: str
    region: str
    targets: tuple[TargetPlan, ...]
    deployment_mode: DeploymentMode = DeploymentMode.PRODUCTION
    credential_policy: CredentialPolicy = CredentialPolicy.SHARED_USER_UNIQUE_SECRET
    schema_version: int = FLEET_SCHEMA_VERSION
    settings: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)
    discovery_scope: DiscoveryScope = field(default_factory=DiscoveryScope)
    prerequisite_actions: tuple[str, ...] = ()
    risk_codes: tuple[str, ...] = ()
    estimated_resource_counts: Mapping[str, int] = field(default_factory=dict, compare=False, repr=False)
    ownership_policy: str = "run-owned-only"

    def __post_init__(self) -> None:
        if self.schema_version != FLEET_SCHEMA_VERSION:
            raise ValueError(f"unsupported fleet plan schema version: {self.schema_version}")
        if not self.profile or not self.region:
            raise ValueError("profile and region are required")
        target_ids = [target.target_id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("fleet plan target ids must be unique")
        known_targets = set(target_ids)
        unknown_dependencies = sorted(
            dependency
            for target in self.targets
            for dependency in target.dependencies
            if dependency not in known_targets
        )
        if unknown_dependencies:
            raise ValueError(f"unknown target dependencies: {', '.join(unknown_dependencies)}")
        object.__setattr__(self, "targets", tuple(sorted(self.targets, key=lambda target: target.target_id)))
        object.__setattr__(self, "settings", _frozen_mapping(self.settings))
        if isinstance(self.discovery_scope, DiscoveryScope):
            discovery_scope = DiscoveryScope(**self.discovery_scope.canonical_dict())
        elif isinstance(self.discovery_scope, Mapping):
            discovery_scope = DiscoveryScope(**self.discovery_scope)
        else:
            raise TypeError("discovery_scope must be a DiscoveryScope or mapping")
        prerequisite_actions = tuple(sorted(set(self.prerequisite_actions)))
        risk_codes = tuple(sorted(set(self.risk_codes)))
        if any(not _PUBLIC_CODE_RE.fullmatch(value) for value in prerequisite_actions + risk_codes):
            raise ValueError("prerequisite actions and risk codes must be public-safe uppercase codes")
        if self.ownership_policy not in _OWNERSHIP_POLICIES:
            raise ValueError("unsupported ownership policy")
        counts = dict(self.estimated_resource_counts)
        if any(
            not isinstance(key, str)
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for key, value in counts.items()
        ):
            raise ValueError("estimated resource counts must be non-negative integers")
        object.__setattr__(self, "discovery_scope", discovery_scope)
        object.__setattr__(self, "prerequisite_actions", prerequisite_actions)
        object.__setattr__(self, "risk_codes", risk_codes)
        object.__setattr__(self, "estimated_resource_counts", _frozen_mapping(counts))

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile": self.profile,
            "region": self.region,
            "deployment_mode": self.deployment_mode.value,
            "credential_policy": self.credential_policy.value,
            "settings": _canonical(self.settings),
            "targets": [target.canonical_dict() for target in self.targets],
            "discovery_scope": self.discovery_scope.canonical_dict(),
            "prerequisite_actions": list(self.prerequisite_actions),
            "risk_codes": list(self.risk_codes),
            "estimated_resource_counts": _canonical(self.estimated_resource_counts),
            "ownership_policy": self.ownership_policy,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @property
    def plan_id(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def require_approval(self, approved_plan_id: str) -> None:
        if approved_plan_id != self.plan_id:
            raise PlanApprovalMismatch("approval does not match the reviewed fleet plan")


@dataclass(frozen=True)
class ResourceRecord:
    """A resource observed or created by a run, with explicit cleanup rights."""

    resource_type: str
    resource_ref: str
    ownership: ResourceOwnership
    enabled_by_run: bool = False
    attributes: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)
    effect: ResourceEffect | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", _frozen_mapping(self.attributes))
        effect = self.effect
        if effect is None:
            effect = ResourceEffect.CREATED if self.ownership is ResourceOwnership.OWNED else ResourceEffect(self.ownership.value)
        object.__setattr__(self, "effect", ResourceEffect(effect))

    @property
    def resource_kind(self) -> str:
        """Compatibility-friendly explicit name for the cleanup resource kind."""
        return self.resource_type

    @property
    def lifecycle_owned(self) -> bool:
        return self.ownership is ResourceOwnership.OWNED

    @property
    def cleanup_allowed(self) -> bool:
        return self.ownership is ResourceOwnership.OWNED and self.enabled_by_run


_ALLOWED_PHASE_TRANSITIONS: dict[PhaseState, frozenset[PhaseState]] = {
    PhaseState.PENDING: frozenset((PhaseState.RUNNING, PhaseState.BLOCKED, PhaseState.HANDED_OFF)),
    PhaseState.RUNNING: frozenset((PhaseState.COMPLETE, PhaseState.FAILED, PhaseState.RETRYABLE, PhaseState.BLOCKED, PhaseState.HANDED_OFF)),
    PhaseState.RETRYABLE: frozenset((PhaseState.RUNNING, PhaseState.BLOCKED, PhaseState.HANDED_OFF)),
    PhaseState.FAILED: frozenset((PhaseState.RETRYABLE, PhaseState.BLOCKED, PhaseState.HANDED_OFF)),
    PhaseState.COMPLETE: frozenset(),
    PhaseState.BLOCKED: frozenset(),
    # A handoff is intentionally resumable: a verified DBA/host-admin evidence
    # import, or newly approved automation access, may continue this phase.
    PhaseState.HANDED_OFF: frozenset((PhaseState.RUNNING, PhaseState.COMPLETE, PhaseState.BLOCKED)),
}


@dataclass(frozen=True)
class PhaseCheckpoint:
    phase: str
    state: PhaseState = PhaseState.PENDING
    attempts: int = 0
    handoff_ref: str | None = None
    work_request_ref: str | None = None
    message: str | None = None

    def transition(
        self,
        state: PhaseState,
        *,
        handoff_ref: str | None = None,
        work_request_ref: str | None = None,
        message: str | None = None,
    ) -> "PhaseCheckpoint":
        if state is self.state:
            return PhaseCheckpoint(
                phase=self.phase,
                state=self.state,
                attempts=self.attempts,
                handoff_ref=handoff_ref if handoff_ref is not None else self.handoff_ref,
                work_request_ref=work_request_ref if work_request_ref is not None else self.work_request_ref,
                message=message if message is not None else self.message,
            )
        if state not in _ALLOWED_PHASE_TRANSITIONS[self.state]:
            raise CheckpointTransitionError(f"cannot transition {self.phase} from {self.state.value} to {state.value}")
        return PhaseCheckpoint(
            phase=self.phase,
            state=state,
            attempts=self.attempts + (1 if state is PhaseState.RUNNING else 0),
            handoff_ref=handoff_ref if handoff_ref is not None else self.handoff_ref,
            work_request_ref=work_request_ref if work_request_ref is not None else self.work_request_ref,
            message=message if message is not None else self.message,
        )


@dataclass(frozen=True)
class TargetManifest:
    target_id: str
    state: TargetState = TargetState.PENDING
    readiness: ReadinessVerdict = ReadinessVerdict.CONFIGURED
    checkpoints: tuple[PhaseCheckpoint, ...] = ()
    resources: tuple[ResourceRecord, ...] = ()
    local_proof: ReadinessVerdict = ReadinessVerdict.CONFIGURED
    live_oci_proof: ReadinessVerdict = ReadinessVerdict.CONFIGURED

    def checkpoint(self, phase: str) -> PhaseCheckpoint | None:
        return next((item for item in self.checkpoints if item.phase == phase), None)

    def with_checkpoint(self, checkpoint: PhaseCheckpoint) -> "TargetManifest":
        replacements = {item.phase: item for item in self.checkpoints}
        replacements[checkpoint.phase] = checkpoint
        return TargetManifest(
            target_id=self.target_id,
            state=self.state,
            readiness=self.readiness,
            local_proof=self.local_proof,
            live_oci_proof=self.live_oci_proof,
            checkpoints=tuple(sorted(replacements.values(), key=lambda item: item.phase)),
            resources=self.resources,
        )

    def with_resource(self, resource: ResourceRecord) -> "TargetManifest":
        existing = {(item.resource_type, item.resource_ref): item for item in self.resources}
        key = (resource.resource_type, resource.resource_ref)
        if key in existing and existing[key] != resource:
            raise ValueError(f"conflicting ownership record for {resource.resource_type}")
        existing[key] = resource
        return TargetManifest(
            target_id=self.target_id,
            state=self.state,
            readiness=self.readiness,
            local_proof=self.local_proof,
            live_oci_proof=self.live_oci_proof,
            checkpoints=self.checkpoints,
            resources=tuple(sorted(existing.values(), key=lambda item: (item.resource_type, item.resource_ref))),
        )

    @property
    def resumable(self) -> bool:
        # Checkpoints are sparse: a verified handoff can complete the only
        # checkpoint written so far while later lifecycle phases have never
        # started.  Target state is therefore the authoritative terminal
        # marker; a pending/running target remains resumable even if every
        # recorded checkpoint is complete.
        if self.state in (TargetState.COMPLETE, TargetState.FAILED, TargetState.BLOCKED):
            return False
        return self.state in (TargetState.PENDING, TargetState.RUNNING) or any(
            item.state in (PhaseState.PENDING, PhaseState.RUNNING, PhaseState.RETRYABLE, PhaseState.HANDED_OFF)
            for item in self.checkpoints
        )


@dataclass(frozen=True)
class RunManifest:
    """Durable run state with immutable checkpoint and ownership updates."""

    run_id: str
    plan_id: str
    targets: tuple[TargetManifest, ...]
    schema_version: int = FLEET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.run_id or not self.plan_id:
            raise ValueError("run_id and plan_id are required")
        target_ids = [target.target_id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("run manifest target ids must be unique")
        object.__setattr__(self, "targets", tuple(sorted(self.targets, key=lambda target: target.target_id)))

    def target(self, target_id: str) -> TargetManifest:
        for target in self.targets:
            if target.target_id == target_id:
                return target
        raise KeyError(f"unknown target: {target_id}")

    def with_target(self, target: TargetManifest) -> "RunManifest":
        targets = {item.target_id: item for item in self.targets}
        if target.target_id not in targets:
            raise KeyError(f"unknown target: {target.target_id}")
        targets[target.target_id] = target
        return RunManifest(self.run_id, self.plan_id, tuple(targets.values()), self.schema_version)

    def transition_checkpoint(
        self,
        target_id: str,
        phase: str,
        state: PhaseState,
        **references: str | None,
    ) -> "RunManifest":
        target = self.target(target_id)
        checkpoint = target.checkpoint(phase) or PhaseCheckpoint(phase=phase)
        return self.with_target(target.with_checkpoint(checkpoint.transition(state, **references)))

    def reopen_failed(self) -> "RunManifest":
        """Reopen explicit failures without weakening authorization boundaries.

        This operation is used only by an exact-plan-approved retry. Completed
        phases and their ownership records are preserved. Authorization blocks
        remain terminal; dependency blocks are reopened after their failed
        parent is made retryable.
        """

        reopened = self
        failed_target_ids = {
            target.target_id
            for target in self.targets
            if target.state is TargetState.FAILED
            and any(checkpoint.state is PhaseState.FAILED for checkpoint in target.checkpoints)
        }
        for target in self.targets:
            checkpoints = tuple(
                PhaseCheckpoint(
                    phase=checkpoint.phase,
                    state=PhaseState.RETRYABLE,
                    attempts=checkpoint.attempts,
                    handoff_ref=checkpoint.handoff_ref,
                    work_request_ref=checkpoint.work_request_ref,
                    message="explicit exact-plan retry requested",
                )
                if checkpoint.state is PhaseState.FAILED
                else checkpoint
                for checkpoint in target.checkpoints
            )
            dependency_blocked = any(
                checkpoint.state is PhaseState.BLOCKED
                and str(checkpoint.message or "").startswith("dependency failed:")
                and any(
                    failed_target_id in str(checkpoint.message or "")
                    for failed_target_id in failed_target_ids
                )
                for checkpoint in target.checkpoints
            )
            if dependency_blocked:
                checkpoints = tuple(
                    PhaseCheckpoint(
                        phase=checkpoint.phase,
                        state=PhaseState.RETRYABLE,
                        attempts=checkpoint.attempts,
                        handoff_ref=checkpoint.handoff_ref,
                        work_request_ref=checkpoint.work_request_ref,
                        message="dependency reopened by exact-plan retry",
                    )
                    if checkpoint.state is PhaseState.BLOCKED
                    and str(checkpoint.message or "").startswith("dependency failed:")
                    else checkpoint
                    for checkpoint in checkpoints
                )
            if target.target_id in failed_target_ids or dependency_blocked:
                reopened = reopened.with_target(
                    TargetManifest(
                        target_id=target.target_id,
                        state=TargetState.PENDING,
                        readiness=ReadinessVerdict.DEGRADED,
                        local_proof=target.local_proof,
                        live_oci_proof=target.live_oci_proof,
                        checkpoints=checkpoints,
                        resources=target.resources,
                    )
                )
        return reopened

    @property
    def resumable(self) -> bool:
        return any(target.resumable for target in self.targets)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "targets": [
                {
                    "target_id": target.target_id,
                    "state": target.state.value,
                    "readiness": target.readiness.value,
                    "local_proof": target.local_proof.value,
                    "live_oci_proof": target.live_oci_proof.value,
                    "checkpoints": [
                        {
                            "phase": checkpoint.phase,
                            "state": checkpoint.state.value,
                            "attempts": checkpoint.attempts,
                            "handoff_ref": checkpoint.handoff_ref,
                            "work_request_ref": checkpoint.work_request_ref,
                            "message": checkpoint.message,
                        }
                        for checkpoint in target.checkpoints
                    ],
                    "resources": [
                        {
                            "resource_type": resource.resource_type,
                            "resource_ref": resource.resource_ref,
                            "ownership": resource.ownership.value,
                            "enabled_by_run": resource.enabled_by_run,
                            "attributes": _canonical(resource.attributes),
                            "effect": resource.effect.value,
                        }
                        for resource in target.resources
                    ],
                }
                for target in self.targets
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunManifest":
        return cls(
            run_id=str(value["run_id"]),
            plan_id=str(value["plan_id"]),
            schema_version=int(value.get("schema_version", FLEET_SCHEMA_VERSION)),
            targets=tuple(
                TargetManifest(
                    target_id=str(target["target_id"]),
                    state=TargetState(target.get("state", TargetState.PENDING.value)),
                    readiness=ReadinessVerdict(target.get("readiness", ReadinessVerdict.CONFIGURED.value)),
                    local_proof=ReadinessVerdict(target.get("local_proof", ReadinessVerdict.CONFIGURED.value)),
                    live_oci_proof=ReadinessVerdict(target.get("live_oci_proof", ReadinessVerdict.CONFIGURED.value)),
                    checkpoints=tuple(
                        PhaseCheckpoint(
                            phase=str(checkpoint["phase"]),
                            state=PhaseState(checkpoint.get("state", PhaseState.PENDING.value)),
                            attempts=int(checkpoint.get("attempts", 0)),
                            handoff_ref=checkpoint.get("handoff_ref"),
                            work_request_ref=checkpoint.get("work_request_ref"),
                            message=checkpoint.get("message"),
                        )
                        for checkpoint in target.get("checkpoints", ())
                    ),
                    resources=tuple(
                        ResourceRecord(
                            resource_type=str(resource["resource_type"]),
                            resource_ref=str(resource["resource_ref"]),
                            ownership=ResourceOwnership(resource["ownership"]),
                            enabled_by_run=bool(resource.get("enabled_by_run", False)),
                            attributes=resource.get("attributes", {}),
                            effect=resource.get("effect"),
                        )
                        for resource in target.get("resources", ())
                    ),
                )
                for target in value.get("targets", ())
            ),
        )


def fleet_plan_from_config(
    config: EnablementConfig,
    *,
    deployment_mode: DeploymentMode = DeploymentMode.PRODUCTION,
    credential_policy: CredentialPolicy = CredentialPolicy.SHARED_USER_UNIQUE_SECRET,
) -> FleetPlan:
    """Import the legacy immutable config without carrying secret values into settings."""

    selected_ids = {
        target.resource_id or f"{target.region or config.region}:{target.kind}:{target.name}"
        for target in config.targets
    }
    targets: list[TargetPlan] = []
    for target in config.targets:
        region = target.region or config.region
        target_id = target.resource_id or f"{region}:{target.kind}:{target.name}"
        dependencies = (target.parent_cdb_id,) if target.parent_cdb_id in selected_ids else ()
        settings = {
            "deployment_type": target.deployment_type,
            "database_role": target.database_role,
            "management_type": target.management_type,
            "provision": target.provision,
            "service_name": target.service_name,
            "monitoring_user": target.monitoring_user,
            "database_resource_type": target.database_resource_type,
            "external_host": target.external_host,
            "external_os": target.external_os,
            "db_system_id": target.db_system_id,
            "private_endpoint_id": target.private_endpoint_id,
            "opsi_private_endpoint_id": target.opsi_private_endpoint_id,
            "data_safe_target_id": target.data_safe_target_id,
            "data_safe_private_endpoint_id": target.data_safe_private_endpoint_id,
            "management_agent_id": target.management_agent_id,
            "opsi_database_insight_id": target.opsi_database_insight_id,
            "logan_database_entity_id": target.logan_database_entity_id,
            "logan_host_entity_id": target.logan_host_entity_id,
            "logan_listener_entity_id": target.logan_listener_entity_id,
            "logan_adb_entity_id": target.logan_adb_entity_id,
            "logan_management_agent_id": target.logan_management_agent_id,
            "logan_sources": target.logan_sources,
            "logan_adr_home": target.logan_adr_home,
            "logan_oracle_home": target.logan_oracle_home,
            "logan_install_home": target.logan_install_home,
            "logan_hostname": target.logan_hostname,
            "logan_adb_service_name": target.logan_adb_service_name,
        }
        targets.append(
            TargetPlan(
                target_id=target_id,
                name=target.name,
                kind=target.kind,
                region=region,
                compartment_id=target.compartment_id or config.compartment_id,
                resource_id=target.resource_id,
                services=target.services,
                dependencies=dependencies,
                credential_policy=credential_policy,
                settings=settings,
            )
        )
    return FleetPlan(
        profile=config.profile,
        region=config.region,
        targets=tuple(targets),
        deployment_mode=deployment_mode,
        credential_policy=credential_policy,
        settings={"dry_run": config.dry_run, "monitoring_regions": tuple(sorted(config.monitoring_regions))},
    )


def fleet_plan_from_dict(value: Mapping[str, Any]) -> FleetPlan:
    """Restore a canonical persisted plan for exact-run resume/offboarding."""
    return FleetPlan(
        profile=str(value["profile"]),
        region=str(value["region"]),
        targets=tuple(
            TargetPlan(
                target_id=str(target["target_id"]), name=str(target["name"]), kind=str(target["kind"]),
                region=str(target["region"]), compartment_id=target.get("compartment_id"),
                resource_id=target.get("resource_id"), services=tuple(target.get("services", ())),
                dependencies=tuple(target.get("dependencies", ())),
                credential_policy=CredentialPolicy(target.get("credential_policy", CredentialPolicy.SHARED_USER_UNIQUE_SECRET.value)),
                settings=target.get("settings", {}),
            ) for target in value.get("targets", ())
        ),
        deployment_mode=DeploymentMode(value.get("deployment_mode", DeploymentMode.PRODUCTION.value)),
        credential_policy=CredentialPolicy(value.get("credential_policy", CredentialPolicy.SHARED_USER_UNIQUE_SECRET.value)),
        schema_version=int(value.get("schema_version", FLEET_SCHEMA_VERSION)), settings=value.get("settings", {}),
        discovery_scope=DiscoveryScope(**value.get("discovery_scope", {})),
        prerequisite_actions=tuple(value.get("prerequisite_actions", ())), risk_codes=tuple(value.get("risk_codes", ())),
        estimated_resource_counts=value.get("estimated_resource_counts", {}), ownership_policy=str(value.get("ownership_policy", "run-owned-only")),
    )


def public_plan_summary(plan: FleetPlan) -> dict[str, Any]:
    """Review every hash-affecting safety choice without topology or refs."""
    return {
        "plan_id": plan.plan_id,
        "schema_version": plan.schema_version,
        "mode": plan.deployment_mode.value,
        "credential_policy": plan.credential_policy.value,
        "settings": {key: value for key, value in _canonical(plan.settings).items() if key in {"services", "log_preset", "retention_days", "authority_mode", "max_concurrency", "provision_test_dbcs", "provision_test_autonomous", "common_user", "pdb_unique_passwords", "bindings_supplied"}},
        "target_count": len(plan.targets),
        "discovery": {"subscribed_region_count": len(plan.discovery_scope.subscribed_regions), "accessible_compartment_count": len(plan.discovery_scope.accessible_compartments), "included_region_count": len(plan.discovery_scope.include_regions), "included_compartment_count": len(plan.discovery_scope.include_compartments)},
        "prerequisite_action_count": len(plan.prerequisite_actions),
        "risk_codes": list(plan.risk_codes),
        "estimated_resource_counts": _canonical(plan.estimated_resource_counts),
        "ownership_policy": plan.ownership_policy,
        "targets": [
            {
                "target_handle": hashlib.sha256(target.target_id.encode("utf-8")).hexdigest()[:24],
                "kind": target.kind,
                "services": list(target.services),
                "dependencies": [hashlib.sha256(dep.encode("utf-8")).hexdigest()[:24] for dep in target.dependencies],
                "credential_policy": target.credential_policy.value,
                "settings": {key: value for key, value in _canonical(target.settings).items() if key in {"deployment_type", "database_role", "management_type", "provision", "log_preset", "authority_mode", "credential_policy"}},
            }
            for target in plan.targets
        ],
    }
