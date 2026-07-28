"""Public-safe evidence summaries for fleet lifecycle runs."""

from __future__ import annotations

import json
import re
from typing import Any

from dbman_opsi.fleet import RunManifest
from dbman_opsi.redact import redact_text


_SAFE_PUBLIC_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SENSITIVE_MARKERS = (
    "ocid",
    "secret",
    "password",
    "wallet",
    "private",
    "host",
    "endpoint",
    "vault",
    "tenancy",
    "compartment",
    "region",
    "http:",
    "https:",
    "/",
)


def _public_text(value: object) -> str:
    """Allow only compact non-topological labels in public evidence."""

    if not isinstance(value, str):
        return "redacted"
    redacted = redact_text(value)
    if not _SAFE_PUBLIC_LABEL.fullmatch(redacted) or any(marker in redacted.lower() for marker in _SENSITIVE_MARKERS):
        return "redacted"
    return redacted


def evidence_dict(manifest: RunManifest) -> dict[str, Any]:
    """Return a summary deliberately excluding all topology and secret references."""

    return {
        "schema_version": manifest.schema_version,
        "run_id": _public_text(manifest.run_id),
        "plan_id": _public_text(manifest.plan_id),
        "targets": [
            {
                "state": _public_text(target.state.value),
                "readiness": _public_text(target.readiness.value),
                "local_proof": _public_text(target.local_proof.value),
                "live_oci_proof": _public_text(target.live_oci_proof.value),
                "checkpoints": [
                    {
                        "phase": _public_text(checkpoint.phase),
                        "state": _public_text(checkpoint.state.value),
                        "attempts": checkpoint.attempts,
                        "handoff_recorded": checkpoint.handoff_ref is not None,
                        "work_request_recorded": checkpoint.work_request_ref is not None,
                    }
                    for checkpoint in target.checkpoints
                ],
                "resources": [
                    {
                        "resource_type": _public_text(resource.resource_type),
                        "ownership": _public_text(resource.ownership.value),
                        "cleanup_allowed": resource.cleanup_allowed,
                    }
                    for resource in target.resources
                ],
            }
            for target in manifest.targets
        ],
    }


def evidence_json(manifest: RunManifest) -> str:
    return json.dumps(evidence_dict(manifest), sort_keys=True, separators=(",", ":"))


def evidence_markdown(manifest: RunManifest) -> str:
    """Render the redacted evidence as a compact human-readable status report."""

    summary = evidence_dict(manifest)
    lines = ["# Fleet lifecycle evidence", "", f"- Run: `{summary['run_id']}`", f"- Plan: `{summary['plan_id']}`"]
    for index, target in enumerate(summary["targets"], start=1):
        lines.extend(
            (
                "",
                f"## Target {index}",
                "",
                f"- State: `{target['state']}`",
                f"- Readiness: `{target['readiness']}`",
                f"- Local proof: `{target['local_proof']}`",
                f"- Live OCI proof: `{target['live_oci_proof']}`",
            )
        )
        for checkpoint in target["checkpoints"]:
            lines.append(f"- Phase `{checkpoint['phase']}`: `{checkpoint['state']}` (attempts: {checkpoint['attempts']})")
        for resource in target["resources"]:
            lines.append(f"- Resource `{resource['resource_type']}`: `{resource['ownership']}`")
    return "\n".join(lines) + "\n"
