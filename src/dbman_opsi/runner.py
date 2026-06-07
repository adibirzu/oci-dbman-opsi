"""Command runner for OCI CLI and Terraform calls."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbman_opsi.redact import redact_text


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int

    def json(self) -> Any:
        if not self.stdout.strip():
            return None
        return json.loads(self.stdout)


class CommandRunner:
    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run

    def run(self, args: list[str], cwd: str | Path | None = None, check: bool = True) -> CommandResult:
        safe_args = tuple(args)
        if self.dry_run:
            print(redact_text("+ " + " ".join(safe_args)))
            return CommandResult(safe_args, "{}", "", 0)

        process = subprocess.run(
            safe_args,
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=True,
            text=True,
        )
        # Return RAW stdout/stderr: callers parse OCIDs out of this for resource
        # joins (discovery's pillar matching, named-credential id lookup, etc.).
        # Redaction is a *display* concern and is applied at the print boundary
        # (CLI --json output, sanitized config). Redacting here silently collapses
        # every OCID to "<OCI_OCID>", which makes OCID-keyed joins match
        # everything-to-everything. Error messages are still redacted because they
        # are surfaced to the user as text.
        if check and process.returncode != 0:
            safe_command = redact_text(" ".join(safe_args))
            raise RuntimeError(
                f"Command failed ({process.returncode}): {safe_command}\n{redact_text(process.stderr)}"
            )
        return CommandResult(safe_args, process.stdout, process.stderr, process.returncode)
