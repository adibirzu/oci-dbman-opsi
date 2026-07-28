"""Plan-gated, resumable coordinator for fleet onboarding writes.

The executor deliberately contains no OCI command construction.  Phase handlers
are adapters around the existing focused services (DBM, OPSI, credentials,
Log Analytics, and validation), keeping their idempotency rules in one place.
"""

from __future__ import annotations

import concurrent.futures
import random
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Protocol

from dbman_opsi.fleet import (
    FleetPlan,
    PhaseState,
    ReadinessVerdict,
    RunManifest,
    TargetManifest,
    TargetPlan,
    TargetState,
    ResourceRecord,
)
from dbman_opsi.fleet_state import FleetStateStore, LeaseHeartbeat, RunLeaseError
from dbman_opsi.fleet_status import refresh_collection_readiness


@dataclass(frozen=True)
class PhaseOutcome:
    """A handler result.  `ready` is only legal with explicit collection proof."""

    readiness: ReadinessVerdict | None = None
    message: str | None = None
    handoff_requested: bool = False
    work_request_ref: str | None = None
    resources: tuple[ResourceRecord, ...] = ()

    @classmethod
    def handoff(
        cls, message: str, *, work_request_ref: str | None = None
    ) -> "PhaseOutcome":
        return cls(
            message=message, handoff_requested=True, work_request_ref=work_request_ref
        )


PhaseHandler = Callable[[TargetPlan], PhaseOutcome | None]


class HandoffWriter(Protocol):
    def write(
        self,
        *,
        run_id: str,
        plan_id: str,
        target_id: str,
        phase: str,
        instructions: str,
    ) -> Path: ...


class FleetOnboardingExecutor:
    """Checkpoint all fleet phases and continue independent targets safely."""

    PHASES = (
        "prerequisites",
        "test-databases",
        "vault-endpoints",
        "db-host-automation",
        "dbm",
        "credentials",
        "opsi",
        "datasafe",
        "agent-log-analytics",
        "validation",
    )
    _SERVICE_FOR_PHASE = {
        "prerequisites": "infra",
        "test-databases": "database",
        "vault-endpoints": "vault",
        "db-host-automation": "host",
        "dbm": "dbm",
        "credentials": "dbm",
        "opsi": "opsi",
        "datasafe": "datasafe",
        "agent-log-analytics": "logan",
        "validation": "read",
    }

    def __init__(
        self,
        plan: FleetPlan,
        store: FleetStateStore,
        *,
        phase_handlers: Mapping[str, PhaseHandler] | None = None,
        concurrency: int = 4,
        service_concurrency: Mapping[str, int] | None = None,
        region_concurrency: Mapping[str, int] | None = None,
        retries: int = 3,
        retry_delay: float = 0.25,
        sleeper: Callable[[float], None] = time.sleep,
        random_float: Callable[[], float] = random.random,
        handoff_writer: HandoffWriter | None = None,
        lease_ttl_seconds: float = 120.0,
        lease_heartbeat_interval: float | None = None,
    ) -> None:
        self.plan = plan
        self.store = store
        self.handlers = dict(phase_handlers or {})
        self.concurrency = max(1, concurrency)
        self.retries = max(0, retries)
        self.retry_delay = max(0.0, retry_delay)
        self.sleeper = sleeper
        self.random_float = random_float
        self.handoff_writer = handoff_writer
        self.lease_ttl_seconds = max(0.03, lease_ttl_seconds)
        self.lease_heartbeat_interval = lease_heartbeat_interval
        limits = service_concurrency or dict(plan.settings.get("service_concurrency", {}))
        # A service limit defaults to one: OCI control planes commonly serialize
        # like-named writes. Callers can opt into a higher reviewed limit.
        self._service_locks = {
            name: threading.BoundedSemaphore(max(1, limits.get(name, 1)))
            for name in set(self._SERVICE_FOR_PHASE.values())
        }
        region_limits = region_concurrency or dict(plan.settings.get("region_concurrency", {}))
        self._region_locks = {
            region: threading.BoundedSemaphore(max(1, int(region_limits.get(region, self.concurrency))))
            for region in {target.region for target in plan.targets}
        }
        self._manifest_lock = threading.RLock()
        self._authorization_failure: str | None = None
        self._manifest: RunManifest | None = None
        self._approved_plan_id = ""
        self._lease_owner: str | None = None
        self._lease_heartbeat: LeaseHeartbeat | None = None

    def execute(
        self,
        *,
        approved_plan_id: str,
        run_id: str | None = None,
        retry_failed: bool = False,
    ) -> RunManifest:
        """Run or resume a plan; approval is checked before reading/writing a run."""
        self.plan.require_approval(approved_plan_id)
        self._approved_plan_id = approved_plan_id
        existing = self.store.load(run_id) if run_id else None
        if existing is not None and existing.plan_id != self.plan.plan_id:
            raise ValueError("run is bound to a different fleet plan")
        if existing is not None:
            existing = refresh_collection_readiness(existing)
            if retry_failed:
                existing = existing.reopen_failed()
        # A previously terminal manifest is a deterministic no-op unless the
        # operator explicitly requests an exact-plan-bound failed-phase retry.
        if existing is not None and not any(
            self._target_is_resumable(target) for target in existing.targets
        ):
            return existing
        candidate_run_id = run_id or str(uuid.uuid4())
        # The lease comes before the first manifest checkpoint and, importantly,
        # before any phase handler can issue an OCI write.
        self._lease_owner = str(uuid.uuid4())
        acquire = getattr(self.store, "acquire_lease", None)
        if callable(acquire) and not acquire(
            run_id=candidate_run_id, plan_id=self.plan.plan_id, owner=self._lease_owner,
            ttl_seconds=self.lease_ttl_seconds,
        ):
            raise RunLeaseError("fleet run is already leased by another actor")
        if self._lease_owner is not None and callable(getattr(self.store, "renew_lease", None)):
            self._lease_heartbeat = LeaseHeartbeat(
                self.store,
                run_id=candidate_run_id,
                owner=self._lease_owner,
                ttl_seconds=self.lease_ttl_seconds,
                interval_seconds=self.lease_heartbeat_interval,
            )
            self._lease_heartbeat.start()
        try:
            self._manifest = existing or RunManifest(
            candidate_run_id,
            self.plan.plan_id,
            tuple(TargetManifest(target.target_id) for target in self.plan.targets),
        )
            self._save()

        # Dependencies are handled in ordered waves; unrelated targets within a
        # wave still use bounded parallelism.
            pending = {
            target.target_id: target
            for target in self.plan.targets
            if self._target_is_resumable(self.manifest.target(target.target_id))
        }
            if not pending:
                return self.manifest
            finished = {
            target.target_id
            for target in self.plan.targets
            if target.target_id not in pending
        }
            while pending:
                wave = [
                target
                for target in pending.values()
                if set(target.dependencies) <= finished
            ]
                if not wave:  # defensive: plans should have been dependency-validated
                    for target in pending.values():
                        self._block(target, "dependency graph cannot be resolved")
                    break
                with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.concurrency
                ) as pool:
                    futures = [pool.submit(self._execute_target, target) for target in wave]
                    for future in futures:
                        future.result()
                for target in wave:
                    pending.pop(target.target_id)
                    finished.add(target.target_id)
            return self.manifest
        finally:
            if self._lease_heartbeat is not None:
                self._lease_heartbeat.stop()
            release = getattr(self.store, "release_lease", None)
            if callable(release):
                release(run_id=candidate_run_id, owner=self._lease_owner)

    @property
    def manifest(self) -> RunManifest:
        if self._manifest is None:
            raise RuntimeError("executor has not started")
        return self._manifest

    def _execute_target(self, target: TargetPlan) -> None:
        if self._authorization_failure:
            self._block(
                target, f"authorization circuit open: {self._authorization_failure}"
            )
            return
        failed_dependencies = [
            dependency
            for dependency in target.dependencies
            if self._target_terminal_failure(dependency)
        ]
        if failed_dependencies:
            self._block(
                target, "dependency failed: " + ", ".join(sorted(failed_dependencies))
            )
            return
        for phase in self.PHASES:
            checkpoint = self.manifest.target(target.target_id).checkpoint(phase)
            if checkpoint and checkpoint.state is PhaseState.COMPLETE:
                continue
            if checkpoint and checkpoint.state in (
                PhaseState.BLOCKED,
                PhaseState.FAILED,
            ):
                return
            if self._authorization_failure:
                self._block(
                    target,
                    f"authorization circuit open: {self._authorization_failure}",
                    phase=phase,
                )
                return
            if not self._run_phase(target, phase):
                return
        current = self.manifest.target(target.target_id)
        verdict = current.readiness
        # Completion of configuration is not collection proof.  A validation
        # handler must explicitly return READY after querying collected data.
        if verdict is ReadinessVerdict.CONFIGURED:
            verdict = ReadinessVerdict.COLLECTING
        self._set_target(target.target_id, TargetState.COMPLETE, verdict)

    def _run_phase(self, target: TargetPlan, phase: str) -> bool:
        self._assert_fenced()
        self._checkpoint(target.target_id, phase, PhaseState.RUNNING)
        handler = self.handlers.get(phase)
        if handler is None:
            # A lifecycle phase is a write or proof boundary.  Treating an
            # unregistered adapter as a successful no-op would fabricate both
            # configuration and validation evidence.
            self._checkpoint(
                target.target_id,
                phase,
                PhaseState.HANDED_OFF,
                message=f"no approved lifecycle handler is configured for phase {phase}",
            )
            self._set_target(
                target.target_id, TargetState.HANDED_OFF, ReadinessVerdict.HANDED_OFF
            )
            return False
        for attempt in range(self.retries + 1):
            try:
                with self._region_locks[target.region], self._service_locks[self._SERVICE_FOR_PHASE[phase]]:
                    # The handler is the egress boundary.  Check immediately
                    # before it and again before its result can be checkpointed.
                    self._assert_fenced()
                    if self._authorization_failure:
                        self._block(
                            target,
                            f"authorization circuit open: {self._authorization_failure}",
                            phase=phase,
                        )
                        return False
                    outcome = handler(target) or PhaseOutcome()
                    self._assert_fenced()
                if outcome.handoff_requested:
                    if self.handoff_writer is None:
                        # A checkpoint without a signed, target-bound packet is
                        # not a handoff protocol and must never be resumable.
                        self._checkpoint(
                            target.target_id, phase, PhaseState.BLOCKED,
                            message="signed handoff writer is required before handoff",
                            resources=outcome.resources,
                        )
                        self._set_target(target.target_id, TargetState.BLOCKED, ReadinessVerdict.BLOCKED)
                        return False
                    handoff_ref = None
                    if self.handoff_writer is not None:
                        issued = self.handoff_writer.write(
                            run_id=self.manifest.run_id,
                            plan_id=self.plan.plan_id,
                            target_id=target.target_id,
                            phase=phase,
                            instructions=outcome.message
                            or "Complete approved operator work and import evidence.",
                        )
                        reference_for = getattr(
                            self.handoff_writer, "reference_for", None
                        )
                        handoff_ref = (
                            reference_for(issued)
                            if callable(reference_for)
                            else str(issued)
                        )
                    self._checkpoint(
                        target.target_id,
                        phase,
                        PhaseState.HANDED_OFF,
                        message=outcome.message,
                        handoff_ref=handoff_ref,
                        work_request_ref=outcome.work_request_ref,
                        resources=outcome.resources,
                    )
                    self._set_target(
                        target.target_id,
                        TargetState.HANDED_OFF,
                        ReadinessVerdict.HANDED_OFF,
                    )
                    return False
                if outcome.readiness is not None:
                    self._set_target(
                        target.target_id, TargetState.RUNNING, outcome.readiness
                    )
                self._checkpoint(
                    target.target_id,
                    phase,
                    PhaseState.COMPLETE,
                    message=outcome.message,
                    work_request_ref=outcome.work_request_ref,
                    resources=outcome.resources,
                )
                return True
            except KeyboardInterrupt:
                self._checkpoint(
                    target.target_id,
                    phase,
                    PhaseState.RETRYABLE,
                    message="interrupted; safe to resume",
                )
                self._set_target(
                    target.target_id, TargetState.PENDING, ReadinessVerdict.DEGRADED
                )
                raise
            except RunLeaseError:
                # A stale actor must not convert lease loss into a retryable
                # checkpoint (nor persist any handler result).
                raise
            except Exception as exc:  # handler errors are classified, not hidden
                message = str(exc)[:500]
                if self._is_authorization_error(message):
                    self._authorization_failure = message
                    self._checkpoint(
                        target.target_id, phase, PhaseState.BLOCKED, message=message
                    )
                    self._set_target(
                        target.target_id, TargetState.BLOCKED, ReadinessVerdict.BLOCKED
                    )
                    return False
                if self._is_idempotent_conflict(message):
                    self._checkpoint(
                        target.target_id,
                        phase,
                        PhaseState.COMPLETE,
                        message="reused existing resource",
                    )
                    return True
                if self._is_transient(message) and attempt < self.retries:
                    self._checkpoint(
                        target.target_id, phase, PhaseState.RETRYABLE, message=message
                    )
                    self.sleeper(
                        self.retry_delay * (2**attempt) * (0.5 + self.random_float())
                    )
                    self._checkpoint(
                        target.target_id,
                        phase,
                        PhaseState.RUNNING,
                        message="retrying transient OCI error",
                    )
                    continue
                state = (
                    PhaseState.RETRYABLE
                    if self._is_transient(message)
                    else PhaseState.FAILED
                )
                self._checkpoint(target.target_id, phase, state, message=message)
                self._set_target(
                    target.target_id, TargetState.FAILED, ReadinessVerdict.DEGRADED
                )
                return False
        return False

    def _checkpoint(
        self, target_id: str, phase: str, state: PhaseState, *, resources: tuple[ResourceRecord, ...] = (), **details: str | None
    ) -> None:
        with self._manifest_lock:
            self._manifest = self.manifest.transition_checkpoint(
                target_id, phase, state, **details
            )
            if resources:
                target = self._manifest.target(target_id)
                for resource in resources:
                    target = target.with_resource(resource)
                self._manifest = self._manifest.with_target(target)
            self._save()

    def _set_target(
        self, target_id: str, state: TargetState, readiness: ReadinessVerdict
    ) -> None:
        with self._manifest_lock:
            current = self.manifest.target(target_id)
            updated = replace(current, state=state, readiness=readiness)
            self._manifest = self.manifest.with_target(updated)
            self._save()

    def _block(
        self, target: TargetPlan, message: str, *, phase: str | None = None
    ) -> None:
        phase = phase or self._next_incomplete_phase(target.target_id)
        checkpoint = self.manifest.target(target.target_id).checkpoint(phase)
        if checkpoint is None or checkpoint.state not in (
            PhaseState.BLOCKED,
            PhaseState.COMPLETE,
        ):
            self._checkpoint(
                target.target_id, phase, PhaseState.BLOCKED, message=message
            )
        self._set_target(
            target.target_id, TargetState.BLOCKED, ReadinessVerdict.BLOCKED
        )

    def _target_terminal_failure(self, target_id: str) -> bool:
        target = self.manifest.target(target_id)
        return target.state in (
            TargetState.FAILED,
            TargetState.BLOCKED,
            TargetState.HANDED_OFF,
        )

    @staticmethod
    def _target_is_resumable(target: TargetManifest) -> bool:
        return (
            target.resumable
            and target.state
            not in (TargetState.BLOCKED, TargetState.FAILED, TargetState.COMPLETE)
            and not any(
                checkpoint.state in (PhaseState.BLOCKED, PhaseState.FAILED)
                for checkpoint in target.checkpoints
            )
        )

    def _next_incomplete_phase(self, target_id: str) -> str:
        target = self.manifest.target(target_id)
        for phase in self.PHASES:
            checkpoint = target.checkpoint(phase)
            if checkpoint is None or checkpoint.state is not PhaseState.COMPLETE:
                return phase
        return self.PHASES[-1]

    def _save(self) -> None:
        self._assert_fenced()
        renew = getattr(self.store, "renew_lease", None)
        if self._lease_heartbeat is None and self._lease_owner is not None and callable(renew) and not renew(run_id=self.manifest.run_id, owner=self._lease_owner, ttl_seconds=self.lease_ttl_seconds):
            raise RunLeaseError("fleet run lease expired or was lost")
        self.store.save(
            self.manifest, plan=self.plan, approved_plan_id=self._approved_plan_id
        )

    def _assert_fenced(self) -> None:
        if self._lease_heartbeat is not None:
            self._lease_heartbeat.assert_held()

    @staticmethod
    def _is_idempotent_conflict(message: str) -> bool:
        value = message.lower()
        return "409" in value and any(
            marker in value
            for marker in (
                "already exists",
                "alreadyexists",
                "already enabled",
                "alreadyenabled",
                "duplicate resource",
                "duplicate entry",
            )
        )

    @staticmethod
    def _is_transient(message: str) -> bool:
        value = message.lower()
        return (
            (
                "409" in value
                and any(
                    marker in value
                    for marker in ("update in progress", "operation in progress")
                )
            )
            or "429" in value
            or any(str(code) in value for code in ("500", "502", "503", "504"))
        )

    @staticmethod
    def _is_authorization_error(message: str) -> bool:
        value = message.lower()
        return any(
            marker in value
            for marker in (
                "401",
                "403",
                "notauthorized",
                "not authorized",
                "unauthorized",
            )
        )
