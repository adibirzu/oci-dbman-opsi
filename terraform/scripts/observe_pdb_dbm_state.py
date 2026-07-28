#!/usr/bin/env python3
"""Create a redaction-safe OCI observation receipt for a PDB DBM disable.

The receipt is operator evidence only. Terraform's disable_cdb stage performs
its own fresh OCI-provider read and deliberately does not trust this file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any


_DISABLED = {"DISABLED", "NOT_ENABLED"}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _items(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return []
    items = data.get("items", [])
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _value(item: dict[str, Any], *keys: str) -> object:
    for key in keys:
        if key in item:
            return item[key]
    return None


def _read_managed_database(args: argparse.Namespace, name: str) -> dict[str, Any]:
    command = [args.oci_bin]
    if args.profile:
        command.extend(["--profile", args.profile])
    if args.region:
        command.extend(["--region", args.region])
    command.extend(
        [
            "database-management",
            "managed-database",
            "list",
            "--compartment-id",
            args.compartment_id,
            "--name",
            name,
            "--all",
        ]
    )
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    matches = [
        item
        for item in _items(json.loads(completed.stdout))
        if _value(item, "name") == name
        and _value(item, "compartment-id", "compartment_id") == args.compartment_id
    ]
    if len(matches) != 1:
        raise ValueError(f"Managed Database observation must resolve exactly one item for target {name!r}.")
    return matches[0]


def _feature_statuses(item: dict[str, Any]) -> list[str]:
    configs = _value(item, "dbmgmt-feature-configs", "dbmgmt_feature_configs")
    if not isinstance(configs, list):
        return []
    return [
        str(_value(config, "feature-status", "feature_status") or "").upper()
        for config in configs
        if isinstance(config, dict)
        and str(_value(config, "feature") or "") == "DIAGNOSTICS_AND_MANAGEMENT"
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oci-bin", default="oci")
    parser.add_argument("--profile")
    parser.add_argument("--region")
    parser.add_argument("--compartment-id", required=True)
    parser.add_argument("--lifecycle-id", required=True)
    parser.add_argument("--targets-file", type=Path, required=True)
    args = parser.parse_args()

    raw_targets = json.loads(args.targets_file.read_text(encoding="utf-8"))
    targets = raw_targets.get("pdb_targets", raw_targets) if isinstance(raw_targets, dict) else None
    if not isinstance(targets, dict) or not targets:
        raise ValueError("targets file must contain a non-empty PDB target map.")

    normalized: dict[str, dict[str, str]] = {}
    evidence: list[dict[str, object]] = []
    for key, target in sorted(targets.items()):
        if not isinstance(key, str) or not isinstance(target, dict):
            raise ValueError("each PDB target must be a keyed object.")
        database_id = target.get("database_id")
        name = target.get("managed_database_name")
        if not isinstance(database_id, str) or not database_id or not isinstance(name, str) or not name:
            raise ValueError("each PDB target needs database_id and managed_database_name.")
        normalized[key] = {"database_id": database_id, "managed_database_name": name}
        statuses = _feature_statuses(_read_managed_database(args, name))
        if any(status not in _DISABLED for status in statuses):
            raise ValueError(f"PDB DBM is not disabled for target {key!r}.")
        evidence.append({"target": key, "feature_statuses": statuses})

    target_set_sha256 = hashlib.sha256(
        _canonical({"lifecycle_id": args.lifecycle_id, "targets": normalized})
    ).hexdigest()
    receipt = {
        "contract": "dbm-opsi-pdb-disable-observation:v1",
        "target_set_sha256": target_set_sha256,
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "observer": "oci-cli",
        "source": "database-management managed-database list",
        "nonce": secrets.token_urlsafe(24),
        "source_evidence_sha256": hashlib.sha256(_canonical(evidence)).hexdigest(),
    }
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"PDB DBM observation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
