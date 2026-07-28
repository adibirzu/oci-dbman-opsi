#!/usr/bin/env python3
"""Fail-closed public-repository hygiene checks for Terraform and evidence."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


OCID = re.compile(r"ocid1\.[a-z0-9]+\.oc[0-9]\.[a-z0-9._-]{15,}", re.IGNORECASE)
PLAINTEXT_NAME = re.compile(r'\b(?:variable|output)\s+"([^"\n]*password[^"\n]*)"', re.IGNORECASE)
PLAINTEXT_ASSIGNMENT = re.compile(r"\b(?:admin_)?password\s*=", re.IGNORECASE)
PRIVATE_IP = re.compile(
    r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
)
SCRIPT_REFERENCE = re.compile(r"(?:exec\s+)?(?:\./)?scripts/([A-Za-z0-9_.-]+\.sh)\b")
STATE_NAME = re.compile(r"(?:^|/)\S+\.tfstate(?:\.backup)?$")
EMPTY_TENANT_DEFAULT = re.compile(
    r'variable\s+"[^"\n]*(?:tenancy|compartment|endpoint|ocid)[^"\n]*"\s*\{[^}]*\bdefault\s*=\s*""',
    re.IGNORECASE | re.DOTALL,
)


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [root / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def is_public_surface(path: Path, root: Path) -> bool:
    relative = path.relative_to(root).as_posix()
    if relative.startswith("terraform/"):
        return True
    if relative.startswith(("docs/", "scripts/", ".github/")):
        return path.suffix.lower() in {".md", ".json", ".yaml", ".yml", ".tf", ".sh", ".py"}
    return relative in {"README.md", "KB.md", "CLAUDE.md"}


def _has_gate_contract(content: str, gate_name: str) -> bool:
    return (
        f'check "{gate_name}"' in content
        and "!var.enable_plaintext_data_safe_demo || var.allow_plaintext_data_safe_demo" in content
    )


def has_per_file_datasafe_demo_contract(relative: str, content: str) -> bool:
    if relative == "terraform/examples/data-safe-plaintext-demo/variables.tf":
        return (
            'variable "data_safe_password"' in content
            and 'variable "allow_plaintext_data_safe_demo"' in content
            and _has_gate_contract(content, "plaintext_demo_declaration_gate")
        )
    if relative == "terraform/examples/data-safe-plaintext-demo/main.tf":
        return re.search(r"\bpassword\s*=\s*var\.data_safe_password\b", content) is not None and _has_gate_contract(content, "plaintext_demo_use_gate")
    return False


def plaintext_findings(relative: str, content: str) -> list[str]:
    declarations = [match.group(1).lower() for match in PLAINTEXT_NAME.finditer(content)]
    assignments = PLAINTEXT_ASSIGNMENT.findall(content)
    if not declarations and not assignments:
        return []
    if has_per_file_datasafe_demo_contract(relative, content):
        allowed_declarations = declarations == ["data_safe_password"] or not declarations
        allowed_assignments = not assignments or len(re.findall(r"\bpassword\s*=\s*var\.data_safe_password\b", content)) == len(assignments)
        if allowed_declarations and allowed_assignments:
            return []
    return [f"{relative}: plaintext credential variable or output outside loudly gated Data Safe demo"]


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    for path in tracked_files(root):
        relative = path.relative_to(root).as_posix()
        if STATE_NAME.search(relative):
            findings.append(f"{relative}: Terraform state must not be committed")
        if any(part in {"generated", "runs", ".fleet-state"} for part in Path(relative).parts):
            findings.append(f"{relative}: generated state, evidence, and journals must not be committed")
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if is_public_surface(path, root):
            if OCID.search(content):
                findings.append(f"{relative}: OCI identifier in public artifact")
            if PRIVATE_IP.search(content):
                findings.append(f"{relative}: OCI topology in public artifact")
            if relative.startswith("terraform/"):
                findings.extend(plaintext_findings(relative, content))
                if EMPTY_TENANT_DEFAULT.search(content):
                    findings.append(f"{relative}: empty tenant/topology default is forbidden")
                if path.suffix == ".tf" and re.search(r"\bhost_ip\s*=|\bhost_ip\b", content):
                    findings.append(f"{relative}: OCI topology host_ip in public Terraform")
            if relative.startswith("scripts/") and path.suffix == ".sh":
                if "terraform destroy" in content and "demo_lifecycle_id" not in content:
                    findings.append(f"{relative}: unowned Terraform deletion attempt is forbidden")
                for reference in SCRIPT_REFERENCE.finditer(content):
                    parent = root / "scripts" / reference.group(1)
                    if not parent.is_file():
                        findings.append(f"{relative}: wrapper references missing parent script {reference.group(1)}")
        if path.stat().st_mode & 0o077 and (
            relative.startswith(("runs/", "generated/", ".fleet-state/"))
            or path.suffix in {".jsonl", ".sqlite", ".db"}
        ):
            findings.append(f"{relative}: state/evidence/journal artifact has unsafe permissions")
    return findings


def main() -> int:
    root = Path.cwd().resolve()
    findings = scan(root)
    if findings:
        for finding in findings:
            print(f"ERROR: {finding}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
