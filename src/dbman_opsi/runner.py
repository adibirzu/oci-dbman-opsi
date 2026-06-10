"""Command runner for OCI CLI and Terraform calls."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbman_opsi.journal import RunJournal
from dbman_opsi.redact import redact_text

log = logging.getLogger(__name__)


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
    def __init__(
        self,
        dry_run: bool = True,
        *,
        journal: RunJournal | None = None,
        run_id: str | None = None,
        clock: Callable[[], float] = time.perf_counter,
        verbose: bool = False,
    ) -> None:
        self.dry_run = dry_run
        self.journal = journal
        self.run_id = run_id
        self._clock = clock
        self.verbose = verbose

    def run(self, args: list[str], cwd: str | Path | None = None, check: bool = True) -> CommandResult:
        safe_args = tuple(args)
        start = self._clock()
        if self.dry_run:
            result = CommandResult(safe_args, "{}", "", 0)
            duration_ms = self._duration_ms(start)
            log.info(redact_text("+ " + " ".join(safe_args)))
            self._record(safe_args, result.returncode, duration_ms)
            self._log_timing(safe_args, result.returncode, duration_ms)
            return result

        process = subprocess.run(
            safe_args,
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=True,
            text=True,
        )
        duration_ms = self._duration_ms(start)
        self._record(safe_args, process.returncode, duration_ms)
        self._log_timing(safe_args, process.returncode, duration_ms)
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

    def _duration_ms(self, start: float) -> int:
        return max(0, int(round((self._clock() - start) * 1000)))

    def _record(self, args: tuple[str, ...], returncode: int, duration_ms: int) -> None:
        if self.journal is None:
            return
        self.journal.record(
            argv=args,
            returncode=returncode,
            duration_ms=duration_ms,
            dry_run=self.dry_run,
        )

    def _log_timing(self, args: tuple[str, ...], returncode: int, duration_ms: int) -> None:
        if not self.verbose:
            return
        log.info(
            "command returncode=%s duration_ms=%s argv=%s",
            returncode,
            duration_ms,
            redact_text(" ".join(args)),
        )
