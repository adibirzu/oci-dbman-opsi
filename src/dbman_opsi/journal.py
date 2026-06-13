"""Structured per-run command journal."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from dbman_opsi.redact import redact_text


@dataclass(frozen=True)
class JournalEntry:
    ts: float
    run_id: str
    profile: str
    region: str
    argv_redacted: tuple[str, ...]
    returncode: int
    duration_ms: int
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["argv_redacted"] = list(self.argv_redacted)
        return payload


class RunJournal:
    def __init__(
        self,
        *,
        run_id: str,
        profile: str,
        region: str,
        root: str | Path = "runs",
        now: Callable[[], float] = time.time,
    ) -> None:
        self.run_id = run_id
        self.profile = profile
        self.region = region
        self.path = Path(root) / f"{run_id}.jsonl"
        self._now = now

    def record(
        self,
        *,
        argv: Sequence[str],
        returncode: int,
        duration_ms: int,
        dry_run: bool,
    ) -> None:
        entry = JournalEntry(
            ts=self._now(),
            run_id=self.run_id,
            profile=self.profile,
            region=self.region,
            argv_redacted=tuple(redact_text(arg) for arg in argv),
            returncode=returncode,
            duration_ms=duration_ms,
            dry_run=dry_run,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
