"""Machine-readable fleet lifecycle status without topology or secrets."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
import time
from typing import Any

from dbman_opsi.fleet import (
    PhaseCheckpoint,
    PhaseState,
    ReadinessVerdict,
    RunManifest,
    TargetManifest,
    TargetState,
)
from dbman_opsi.fleet_handoff import target_handle


def _fresh_collection_proofs(
    target: TargetManifest,
    *,
    now: float,
    max_age_seconds: int,
    clock_skew_seconds: int,
) -> tuple[set[str], set[str]]:
    fresh: set[str] = set()
    expected: set[str] = set()
    for resource in target.resources:
        if resource.resource_type != "collection-proof":
            continue
        service = resource.attributes.get("service")
        timestamp = resource.attributes.get("timestamp")
        selected = resource.attributes.get("selected_services", ())
        if isinstance(selected, tuple):
            expected.update(str(item) for item in selected)
        if (
            isinstance(service, str)
            and isinstance(timestamp, int)
            and timestamp <= now + clock_skew_seconds
            and now - timestamp <= max_age_seconds
        ):
            fresh.add(service)
    return fresh, expected


def _verdict(
    target: TargetManifest,
    *,
    now: float,
    max_age_seconds: int = 900,
    clock_skew_seconds: int = 60,
) -> ReadinessVerdict:
    if target.readiness in (
        ReadinessVerdict.BLOCKED,
        ReadinessVerdict.HANDED_OFF,
        ReadinessVerdict.DEGRADED,
    ):
        return target.readiness
    if target.readiness is ReadinessVerdict.READY:
        fresh, expected = _fresh_collection_proofs(
            target,
            now=now,
            max_age_seconds=max_age_seconds,
            clock_skew_seconds=clock_skew_seconds,
        )
        if expected and expected <= fresh:
            return ReadinessVerdict.READY
    checkpoints = {checkpoint.phase: checkpoint for checkpoint in target.checkpoints}
    if any(
        checkpoint.state is PhaseState.HANDED_OFF for checkpoint in checkpoints.values()
    ):
        return ReadinessVerdict.HANDED_OFF
    if any(
        checkpoint.state in (PhaseState.BLOCKED, PhaseState.FAILED)
        for checkpoint in checkpoints.values()
    ):
        return ReadinessVerdict.DEGRADED
    if any(
        name in checkpoints and checkpoints[name].state is PhaseState.COMPLETE
        for name in ("dbm", "opsi", "agent-log-analytics")
    ):
        return ReadinessVerdict.COLLECTING
    return ReadinessVerdict.CONFIGURED


def refresh_collection_readiness(
    manifest: RunManifest,
    *,
    now: float | None = None,
    max_age_seconds: int = 900,
    clock_skew_seconds: int = 60,
) -> RunManifest:
    """Downgrade expired READY targets and reopen validation for a safe resume."""

    current_time = time.time() if now is None else now
    updated = manifest
    for target in manifest.targets:
        if target.readiness is not ReadinessVerdict.READY:
            continue
        if _verdict(
            target,
            now=current_time,
            max_age_seconds=max_age_seconds,
            clock_skew_seconds=clock_skew_seconds,
        ) is ReadinessVerdict.READY:
            continue
        checkpoints = tuple(
            replace(checkpoint, state=PhaseState.RETRYABLE)
            if checkpoint.phase == "validation" and checkpoint.state is PhaseState.COMPLETE
            else checkpoint
            for checkpoint in target.checkpoints
        )
        if not any(checkpoint.phase == "validation" for checkpoint in checkpoints):
            checkpoints += (PhaseCheckpoint("validation", PhaseState.RETRYABLE),)
        updated = updated.with_target(
            replace(
                target,
                state=TargetState.PENDING,
                readiness=ReadinessVerdict.COLLECTING,
                checkpoints=checkpoints,
            )
        )
    return updated


def fleet_status(
    manifest: RunManifest,
    *,
    now: float | None = None,
    max_age_seconds: int = 900,
    clock_skew_seconds: int = 60,
) -> dict[str, Any]:
    """Return sanitized status. Completion of an OPSI registration is collecting, not ready."""
    targets = []
    counts: Counter[str] = Counter()
    current_time = time.time() if now is None else now
    for target in manifest.targets:
        verdict = _verdict(
            target,
            now=current_time,
            max_age_seconds=max_age_seconds,
            clock_skew_seconds=clock_skew_seconds,
        )
        counts[verdict.value] += 1
        targets.append(
            {
                # The manifest remains private state.  Bind a stable opaque
                # public handle to this run so a target ID cannot be correlated
                # across runs or used as a topology identifier.
                "target_handle": target_handle(f"{manifest.run_id}:{target.target_id}"),
                "verdict": verdict.value,
                "phases": {
                    checkpoint.phase: checkpoint.state.value
                    for checkpoint in target.checkpoints
                },
            }
        )
    return {
        "run_id": manifest.run_id,
        "plan_id": manifest.plan_id,
        "summary": {name.value: counts[name.value] for name in ReadinessVerdict},
        "targets": targets,
    }
