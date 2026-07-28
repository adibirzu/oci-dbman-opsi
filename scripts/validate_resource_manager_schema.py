#!/usr/bin/env python3
"""Validate the repository's OCI Resource Manager schema contract."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml


CURRENT_DYNAMIC_TYPES = {
    "oci:core:subnet:id",
    "oci:core:vcn:id",
    "oci:identity:compartment:id",
    "oci:identity:region:name",
    "oci:kms:key:id",
    "oci:kms:vault:id",
}
STATIC_TYPES = {"array", "boolean", "enum", "integer", "number", "string", "text"}


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def validate_schema(stack_root: Path) -> None:
    schema_path = stack_root / "schema.yaml"
    variables_path = stack_root / "variables.tf"
    main_path = stack_root / "main.tf"

    schema = _mapping(yaml.safe_load(schema_path.read_text(encoding="utf-8")), "schema")
    variables = _mapping(schema.get("variables"), "schema.variables")

    if schema.get("schemaVersion") != "1.1.0":
        raise ValueError("schemaVersion must be 1.1.0")
    if schema.get("allowViewState") is not False:
        raise ValueError("allowViewState must be false because Terraform state contains topology")

    grouped: list[str] = []
    for group in schema.get("variableGroups", []):
        grouped.extend(_mapping(group, "variable group").get("variables", []))
    duplicates = sorted({name for name in grouped if grouped.count(name) > 1})
    if duplicates:
        raise ValueError(f"variables appear in multiple groups: {', '.join(duplicates)}")
    missing_group_variables = sorted(set(grouped) - set(variables))
    if missing_group_variables:
        raise ValueError(f"grouped variables missing definitions: {', '.join(missing_group_variables)}")

    terraform_variables = set(
        re.findall(r'^variable\s+"([^"]+)"\s*\{', variables_path.read_text(encoding="utf-8"), re.MULTILINE)
    )
    unknown = sorted(set(variables) - terraform_variables)
    if unknown:
        raise ValueError(f"schema variables missing from Terraform: {', '.join(unknown)}")

    supported = CURRENT_DYNAMIC_TYPES | STATIC_TYPES
    unsupported = sorted(
        {
            str(definition.get("type"))
            for definition in variables.values()
            if isinstance(definition, dict) and definition.get("type") not in supported
        }
    )
    if unsupported:
        raise ValueError(f"unsupported or stale schema types: {', '.join(unsupported)}")

    if variables.get("tenancy_ocid", {}).get("visible") is not False:
        raise ValueError("tenancy_ocid must be injected and hidden")
    if variables.get("deployment_mode", {}).get("enum") != ["poc", "demo", "production"]:
        raise ValueError("deployment_mode must expose poc, demo, and production")
    if variables.get("create_data_safe_private_endpoint", {}).get("default") is not False:
        raise ValueError("Data Safe endpoint creation must remain explicit opt-in")

    terraform_outputs = set(
        re.findall(r'^output\s+"([^"]+)"\s*\{', main_path.read_text(encoding="utf-8"), re.MULTILINE)
    )
    schema_outputs = _mapping(schema.get("outputs"), "schema.outputs")
    missing_outputs = sorted(set(schema_outputs) - terraform_outputs)
    if missing_outputs:
        raise ValueError(f"schema outputs missing from Terraform: {', '.join(missing_outputs)}")

    rendered = schema_path.read_text(encoding="utf-8").lower()
    if "type: password" in rendered:
        raise ValueError("Resource Manager schema must never expose a password input")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stack_root", type=Path)
    args = parser.parse_args()
    validate_schema(args.stack_root.resolve())
    print(f"Resource Manager schema is valid: {args.stack_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
