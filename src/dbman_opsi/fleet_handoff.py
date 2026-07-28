"""Signed, redacted handoff instructions and completion-evidence envelopes."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from dbman_opsi.fleet import (
    FleetPlan,
    PhaseState,
    ReadinessVerdict,
    RunManifest,
    TargetState,
    ResourceEffect,
    ResourceOwnership,
    ResourceRecord,
)
from dbman_opsi.fleet_state import FleetStateStore

_FORBIDDEN = ("password", "secret", "ocid1.", "private key", "token")
_CREDENTIAL_MARKERS = ("password", "private key", "token")
_COMPLETION_RESULTS = frozenset(("completed", "succeeded", "verified"))
_RESOURCE_TYPES_BY_PHASE = {
    "dbm": frozenset(("dbm-cdb", "dbm-pdb", "dbm-autonomous")),
    "credentials": frozenset(("named-credential", "preferred-credential", "database-user", "vault-secret")),
    "opsi": frozenset(("opsi-insight",)),
    "datasafe": frozenset(("datasafe-target",)),
    "agent-log-analytics": frozenset(("logan-association", "logan-entity", "management-agent")),
}


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _contains_sensitive(value: object) -> bool:
    return any(marker in str(value).lower() for marker in _FORBIDDEN)


def target_handle(target_id: str) -> str:
    """Return an opaque stable handle; never serialize a target identifier."""
    return hashlib.sha256(target_id.encode("utf-8")).hexdigest()[:24]


class HandoffPacketWriter:
    def __init__(self, directory: str | Path, *, signing_key: bytes) -> None:
        self.directory = Path(directory)
        self.signing_key = signing_key

    def write(
        self,
        *,
        run_id: str,
        plan_id: str,
        target_id: str,
        phase: str,
        instructions: str,
    ) -> Path:
        if not instructions.strip() or _contains_sensitive(instructions):
            raise ValueError("handoff instructions must be non-empty and redacted")
        issued = {
            "version": 2,
            "run_id": run_id,
            "plan_id": plan_id,
            "target_handle": target_handle(target_id),
            "phase": phase,
            "required_resource_types": sorted(
                _RESOURCE_TYPES_BY_PHASE.get(phase, ())
            ),
            "instructions": instructions,
            "issued_at": int(time.time()),
            "nonce": secrets.token_hex(16),
        }
        digest = hashlib.sha256(_canonical(issued)).hexdigest()
        signed = {"issued": issued, "issued_packet_digest": digest}
        document = {
            **signed,
            "signature": hmac.new(
                self.signing_key, _canonical(signed), hashlib.sha256
            ).hexdigest(),
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        path = (
            self.directory / f"{run_id}-{issued['target_handle']}-{phase}.handoff.json"
        )
        self._write_json(path, document)
        return path

    def reference_for(self, issued_packet: str | Path) -> str:
        document = self._read_issued(issued_packet)
        return "sha256:" + str(document["issued_packet_digest"])

    def write_completion(
        self,
        issued_packet: str | Path,
        *,
        attestation: str,
        result: str,
        evidence_timestamp: int | None = None,
        resource_effects: tuple[dict[str, Any], ...] = (),
    ) -> Path:
        """Create an operator attestation bound to one issued instruction packet."""
        document = self._read_issued(issued_packet)
        issued = document["issued"]
        evidence = {
            "version": 1,
            "run_id": issued["run_id"],
            "plan_id": issued["plan_id"],
            "target_handle": issued["target_handle"],
            "phase": issued["phase"],
            "issued_handoff_ref": "sha256:" + document["issued_packet_digest"],
            "issued_packet_digest": document["issued_packet_digest"],
            "attestation": attestation,
            "result": result,
            "evidence_timestamp": evidence_timestamp or int(time.time()),
            "resource_effects": [dict(item) for item in resource_effects],
            "nonce": secrets.token_hex(16),
        }
        if not self._valid_completion_fields(evidence):
            raise ValueError(
                "completion evidence requires redacted attestation, allowlisted result, and phase resource identities"
            )
        completion = {
            "evidence": evidence,
            "signature": hmac.new(
                self.signing_key, _canonical(evidence), hashlib.sha256
            ).hexdigest(),
        }
        path = Path(issued_packet).with_suffix(".completion.json")
        self._write_json(path, completion)
        return path

    def _read_issued(self, path: str | Path) -> dict[str, Any]:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        signed = {
            "issued": document.get("issued"),
            "issued_packet_digest": document.get("issued_packet_digest"),
        }
        if not isinstance(signed["issued"], dict) or not isinstance(
            document.get("signature"), str
        ):
            raise ValueError("invalid issued handoff packet")
        expected = hmac.new(
            self.signing_key, _canonical(signed), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, document["signature"]):
            raise ValueError("issued handoff packet signature is invalid")
        digest = hashlib.sha256(_canonical(signed["issued"])).hexdigest()
        issued = signed["issued"]
        phase = str(issued.get("phase") or "")
        required_types = issued.get("required_resource_types")
        expected_types = sorted(_RESOURCE_TYPES_BY_PHASE.get(phase, ()))
        instructions = issued.get("instructions")
        if (
            digest != signed["issued_packet_digest"]
            or required_types != expected_types
            or not isinstance(instructions, str)
            or _contains_sensitive(instructions)
        ):
            raise ValueError("issued handoff packet is invalid or not redacted")
        return document

    @staticmethod
    def _valid_completion_fields(evidence: dict[str, Any]) -> bool:
        phase = str(evidence.get("phase") or "")
        raw_effects = evidence.get("resource_effects", ())
        if not isinstance(raw_effects, list):
            return False
        if phase in _RESOURCE_TYPES_BY_PHASE and not raw_effects:
            return False
        if not all(_valid_resource_effect(phase, item) for item in raw_effects):
            return False
        public_attestation = {
            key: value
            for key, value in evidence.items()
            if key != "resource_effects"
        }
        return (
            isinstance(evidence.get("attestation"), str)
            and bool(evidence["attestation"].strip())
            and str(evidence.get("result", "")).lower() in _COMPLETION_RESULTS
            and bool(evidence.get("nonce"))
            and isinstance(evidence.get("evidence_timestamp"), int)
            and evidence["evidence_timestamp"] > 0
            and not _contains_sensitive(public_attestation)
        )

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True)


class HandoffEvidenceImporter:
    def __init__(
        self, store: FleetStateStore, plan: FleetPlan, *, signing_key: bytes
    ) -> None:
        self.store, self.plan, self.signing_key = store, plan, signing_key

    def import_packet(self, path: str | Path, *, approved_plan_id: str) -> RunManifest:
        self.plan.require_approval(approved_plan_id)
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        evidence = document.get("evidence")
        if not isinstance(evidence, dict) or not isinstance(
            document.get("signature"), str
        ):
            raise ValueError("completion evidence envelope is required")
        expected = hmac.new(
            self.signing_key, _canonical(evidence), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, document["signature"]):
            raise ValueError("handoff evidence signature is invalid")
        if not HandoffPacketWriter._valid_completion_fields(evidence):
            raise ValueError("handoff evidence attestation/result is invalid")
        if evidence.get("plan_id") != self.plan.plan_id:
            raise ValueError("handoff evidence belongs to a different plan")
        run = self.store.load(str(evidence.get("run_id")))
        if run is None or run.plan_id != self.plan.plan_id:
            raise ValueError("handoff evidence references an unknown or mismatched run")
        phase = str(evidence.get("phase"))
        targets = [
            target.target_id
            for target in self.plan.targets
            if target_handle(target.target_id) == evidence.get("target_handle")
        ]
        if len(targets) != 1:
            raise ValueError("handoff evidence target binding is invalid")
        target_id = targets[0]
        checkpoint = run.target(target_id).checkpoint(phase)
        if checkpoint is None or checkpoint.state is not PhaseState.HANDED_OFF:
            raise ValueError("handoff evidence does not match a pending handoff")
        expected_ref = "sha256:" + str(evidence.get("issued_packet_digest"))
        if (
            evidence.get("issued_handoff_ref") != expected_ref
            or checkpoint.handoff_ref != expected_ref
        ):
            raise ValueError("handoff evidence issued reference binding is invalid")
        resource_effects = tuple(
            _resource_record_from_handoff(item)
            for item in evidence.get("resource_effects", ())
        )
        updated = run.transition_checkpoint(
            target_id,
            phase,
            PhaseState.COMPLETE,
            message="verified handoff evidence imported",
        )
        target = updated.target(target_id)
        for resource in resource_effects:
            target = target.with_resource(resource)
        updated = updated.with_target(
            replace(
                target, state=TargetState.PENDING, readiness=ReadinessVerdict.CONFIGURED
            )
        )
        self.store.save(updated, plan=self.plan, approved_plan_id=approved_plan_id)
        return updated


class CollectionEvidenceImporter:
    """Accept only signed, current, service-selected collection observations."""

    def __init__(self, store: FleetStateStore, plan: FleetPlan, *, signing_key: bytes) -> None:
        self.store, self.plan, self.signing_key = store, plan, signing_key

    def import_packet(self, path: str | Path, *, approved_plan_id: str, max_age_seconds: int = 900) -> RunManifest:
        self.plan.require_approval(approved_plan_id)
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        evidence = document.get("evidence")
        if not isinstance(evidence, dict) or not isinstance(document.get("signature"), str):
            raise ValueError("signed collection evidence envelope is required")
        expected = hmac.new(self.signing_key, _canonical(evidence), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, document["signature"]):
            raise ValueError("collection evidence signature is invalid")
        if _contains_sensitive(evidence):
            raise ValueError("collection evidence must be redacted")
        if evidence.get("plan_id") != self.plan.plan_id or not isinstance(evidence.get("run_id"), str):
            raise ValueError("collection evidence run/plan binding is invalid")
        run = self.store.load(evidence["run_id"])
        if run is None or run.plan_id != self.plan.plan_id:
            raise ValueError("collection evidence references an unknown run")
        targets = [target for target in self.plan.targets if target_handle(target.target_id) == evidence.get("target_handle")]
        if len(targets) != 1:
            raise ValueError("collection evidence target binding is invalid")
        service = str(evidence.get("service") or "")
        if service not in targets[0].services:
            raise ValueError("collection evidence service is not selected")
        timestamp = evidence.get("evidence_timestamp")
        now = time.time()
        if (
            not isinstance(timestamp, int)
            or timestamp <= 0
            or timestamp > now + 60
            or now - timestamp > max_age_seconds
        ):
            raise ValueError("collection evidence is stale or missing timestamp")
        if str(evidence.get("result", "")).lower() not in _COMPLETION_RESULTS:
            raise ValueError("collection evidence result is invalid")
        digest = hashlib.sha256(_canonical(evidence)).hexdigest()
        target = run.target(targets[0].target_id).with_resource(ResourceRecord(
            "collection-proof", digest, ResourceOwnership.PREEXISTING, False,
            attributes={
                "service": service,
                "timestamp": timestamp,
                "selected_services": tuple(targets[0].services),
            },
            effect=ResourceEffect.PREEXISTING,
        ))
        observed_services = {
            str(resource.attributes.get("service"))
            for resource in target.resources
            if resource.resource_type == "collection-proof"
        }
        # A signed proof is authoritative only after every selected service has
        # supplied one for this exact target.  Preserve an in-flight target's
        # execution state; completed configuration can be promoted to READY.
        if set(targets[0].services) <= observed_services and target.state is TargetState.COMPLETE:
            target = replace(target, readiness=ReadinessVerdict.READY)
        updated = run.with_target(target)
        self.store.save(updated, plan=self.plan, approved_plan_id=approved_plan_id)
        return updated


def _valid_resource_effect(phase: str, value: object) -> bool:
    if not isinstance(value, dict):
        return False
    resource_type = value.get("resource_type")
    resource_ref = value.get("resource_ref")
    effect = value.get("effect")
    attributes = value.get("attributes", {})
    if (
        not isinstance(resource_type, str)
        or resource_type not in _RESOURCE_TYPES_BY_PHASE.get(phase, frozenset())
        or not isinstance(resource_ref, str)
        or not resource_ref.strip()
        or effect not in {item.value for item in ResourceEffect}
        or not isinstance(attributes, dict)
    ):
        return False
    return not any(
        marker in str(attributes).lower() or marker in resource_ref.lower()
        for marker in _CREDENTIAL_MARKERS
    )


def _resource_record_from_handoff(value: dict[str, Any]) -> ResourceRecord:
    effect = ResourceEffect(str(value["effect"]))
    ownership = {
        ResourceEffect.CREATED: ResourceOwnership.OWNED,
        ResourceEffect.REUSED: ResourceOwnership.REUSED,
        ResourceEffect.PREEXISTING: ResourceOwnership.PREEXISTING,
    }[effect]
    return ResourceRecord(
        str(value["resource_type"]),
        str(value["resource_ref"]),
        ownership,
        effect is ResourceEffect.CREATED,
        attributes=value.get("attributes", {}),
        effect=effect,
    )
