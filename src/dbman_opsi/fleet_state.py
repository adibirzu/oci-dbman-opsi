"""Stdlib-only SQLite persistence for resumable fleet lifecycle runs."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from dbman_opsi.fleet import FLEET_SCHEMA_VERSION, FleetPlan, PlanApprovalMismatch, RunManifest, fleet_plan_from_dict


DEFAULT_FLEET_STATE_PATH = Path(".fleet-state") / "fleet.sqlite"


class RunPlanBindingError(ValueError):
    """Raised when an existing run is incorrectly rebound to another plan."""


class RunLeaseError(RuntimeError):
    """Raised when a run is active in another process."""


class LeaseHeartbeat:
    """Keep one local run lease live and expose a fencing check to callers.

    A checkpoint is not a suitable heartbeat: a control-plane request may take
    longer than the lease TTL.  Callers must check :meth:`assert_held` on both
    sides of an egress boundary so a writer that loses its lease cannot issue a
    later request or persist a stale result.
    """

    def __init__(
        self,
        store: "FleetStateStore",
        *,
        run_id: str,
        owner: str,
        ttl_seconds: float = 120.0,
        interval_seconds: float | None = None,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.owner = owner
        self.ttl_seconds = max(0.03, ttl_seconds)
        self.interval_seconds = interval_seconds or max(0.01, self.ttl_seconds / 3)
        self._stopped = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="fleet-lease-heartbeat", daemon=True)
        self._thread.start()

    def assert_held(self) -> None:
        if self._lost.is_set():
            raise RunLeaseError("fleet run lease expired or was lost")

    def stop(self) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.1, self.interval_seconds * 2))

    def _run(self) -> None:
        while not self._stopped.wait(self.interval_seconds):
            if not self.store.renew_lease(
                run_id=self.run_id, owner=self.owner, ttl_seconds=self.ttl_seconds
            ):
                self._lost.set()
                return


class FleetStateStore:
    """A transaction-safe local state store with conservative file permissions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._secure_create()
        self._migrate()
        self._secure_permissions()

    def _secure_create(self) -> None:
        """Create or harden the database before SQLite has a chance to open it."""

        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        self._secure_permissions()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            existing = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            if 1 not in existing:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS fleet_runs (
                        run_id TEXT PRIMARY KEY,
                        plan_id TEXT NOT NULL,
                        manifest_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS fleet_runs_plan_id_idx ON fleet_runs(plan_id)"
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, time.time()),
                )
            if 2 not in existing:
                # Cleanup checkpoints intentionally keep only action digests and
                # sanitized outcome metadata.  Resource references remain in the
                # 0600 run manifest and are never copied into retention evidence.
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS fleet_cleanup_runs (
                        run_id TEXT NOT NULL,
                        cleanup_plan_id TEXT NOT NULL,
                        state_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(run_id, cleanup_plan_id)
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, time.time()),
                )
            # Plan bodies remain private state.  This auxiliary table is
            # deliberately schema-v2-compatible: older stores can gain it
            # without changing the public migration version contract.
            connection.execute(
                "CREATE TABLE IF NOT EXISTS fleet_run_plans (run_id TEXT PRIMARY KEY, plan_json TEXT NOT NULL)"
            )
            connection.execute("""CREATE TABLE IF NOT EXISTS fleet_run_leases (
                run_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, owner TEXT NOT NULL, expires_at REAL NOT NULL
            )""")

    def _secure_permissions(self) -> None:
        # chmod also fixes pre-existing local state created with a permissive umask.
        os.chmod(self.path, 0o600)

    @property
    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
        return int(row["version"] or 0)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save(self, manifest: RunManifest, *, plan: FleetPlan, approved_plan_id: str) -> None:
        """Persist a run only for its exactly approved, permanently bound plan."""

        if manifest.schema_version != FLEET_SCHEMA_VERSION:
            raise ValueError("unsupported run manifest schema version")
        plan.require_approval(approved_plan_id)
        if manifest.plan_id != plan.plan_id:
            raise PlanApprovalMismatch("run manifest does not match the approved fleet plan")
        now = time.time()
        payload = json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":"))
        plan_payload = plan.canonical_json()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT plan_id FROM fleet_runs WHERE run_id = ?", (manifest.run_id,)
            ).fetchone()
            if existing and existing["plan_id"] != manifest.plan_id:
                raise RunPlanBindingError("run_id is already bound to a different fleet plan")
            connection.execute(
                """
                INSERT INTO fleet_runs(run_id, plan_id, manifest_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    manifest_json=excluded.manifest_json,
                    updated_at=excluded.updated_at
                """,
                (manifest.run_id, manifest.plan_id, payload, now, now),
            )
            connection.execute(
                "INSERT INTO fleet_run_plans(run_id, plan_json) VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET plan_json=excluded.plan_json",
                (manifest.run_id, plan_payload),
            )
        self._secure_permissions()

    def acquire_lease(self, *, run_id: str, plan_id: str, owner: str, ttl_seconds: float = 120.0) -> bool:
        """Atomically acquire an expiring local lease before OCI writes."""
        now = time.time()
        with self.transaction() as connection:
            row = connection.execute("SELECT plan_id, owner, expires_at FROM fleet_run_leases WHERE run_id = ?", (run_id,)).fetchone()
            if row and row["plan_id"] != plan_id:
                raise RunPlanBindingError("run lease is bound to a different plan")
            if row and float(row["expires_at"]) > now and row["owner"] != owner:
                return False
            connection.execute("INSERT INTO fleet_run_leases(run_id, plan_id, owner, expires_at) VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET plan_id=excluded.plan_id, owner=excluded.owner, expires_at=excluded.expires_at", (run_id, plan_id, owner, now + ttl_seconds))
        return True

    def renew_lease(self, *, run_id: str, owner: str, ttl_seconds: float = 120.0) -> bool:
        with self.transaction() as connection:
            result = connection.execute("UPDATE fleet_run_leases SET expires_at = ? WHERE run_id = ? AND owner = ? AND expires_at > ?", (time.time() + ttl_seconds, run_id, owner, time.time()))
        return result.rowcount == 1

    def release_lease(self, *, run_id: str, owner: str) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM fleet_run_leases WHERE run_id = ? AND owner = ?", (run_id, owner))

    def load(self, run_id: str) -> RunManifest | None:
        with self._connect() as connection:
            row = connection.execute("SELECT manifest_json FROM fleet_runs WHERE run_id = ?", (run_id,)).fetchone()
        return RunManifest.from_dict(json.loads(row["manifest_json"])) if row else None

    def load_plan(self, run_id: str) -> FleetPlan | None:
        with self._connect() as connection:
            row = connection.execute("SELECT plan_json FROM fleet_run_plans WHERE run_id = ?", (run_id,)).fetchone()
        if not row or not row["plan_json"]:
            return None
        plan = fleet_plan_from_dict(json.loads(row["plan_json"]))
        return plan

    def save_cleanup_state(
        self, *, run_id: str, cleanup_plan_id: str, state: dict[str, object]
    ) -> None:
        """Persist non-secret cleanup progress for the exact cleanup plan."""

        now = time.time()
        payload = json.dumps(state, sort_keys=True, separators=(",", ":"))
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO fleet_cleanup_runs(run_id, cleanup_plan_id, state_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id, cleanup_plan_id) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                (run_id, cleanup_plan_id, payload, now, now),
            )
        self._secure_permissions()

    def load_cleanup_state(self, *, run_id: str, cleanup_plan_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM fleet_cleanup_runs WHERE run_id = ? AND cleanup_plan_id = ?",
                (run_id, cleanup_plan_id),
            ).fetchone()
        return dict(json.loads(row["state_json"])) if row else None

    def purge_expired_cleanup_evidence(self, *, now: datetime | None = None) -> int:
        """Remove expired terminal-run evidence but retain retryable cleanup state.

        Action digests stay behind after successful expiry so a repeated exact
        cleanup remains idempotent.  Any incomplete/handed-off state retains
        its metadata and operational checkpoint until an operator resolves it.
        """

        cutoff = now or datetime.now(UTC)
        if cutoff.tzinfo is None:
            raise ValueError("cleanup evidence purge time must be timezone-aware")
        removed = 0
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT run_id, cleanup_plan_id, state_json FROM fleet_cleanup_runs"
            ).fetchall()
            for row in rows:
                state = json.loads(row["state_json"])
                if not isinstance(state, dict):
                    continue
                actions = state.get("action_states")
                evidence = state.get("evidence")
                if not isinstance(actions, dict) or not actions or any(
                    value != "complete" for value in actions.values()
                ):
                    continue
                if not isinstance(evidence, dict) or not isinstance(evidence.get("retained_until"), str):
                    continue
                try:
                    retained_until = datetime.fromisoformat(evidence["retained_until"].replace("Z", "+00:00"))
                except ValueError:
                    continue
                if retained_until.tzinfo is None or retained_until > cutoff:
                    continue
                state.pop("evidence", None)
                connection.execute(
                    "UPDATE fleet_cleanup_runs SET state_json = ?, updated_at = ? WHERE run_id = ? AND cleanup_plan_id = ?",
                    (
                        json.dumps(state, sort_keys=True, separators=(",", ":")),
                        time.time(),
                        row["run_id"],
                        row["cleanup_plan_id"],
                    ),
                )
                removed += 1
        self._secure_permissions()
        return removed

    def find_by_plan(self, plan_id: str) -> tuple[RunManifest, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT manifest_json FROM fleet_runs WHERE plan_id = ? ORDER BY created_at, run_id", (plan_id,)
            ).fetchall()
        return tuple(RunManifest.from_dict(json.loads(row["manifest_json"])) for row in rows)

    def resume_candidates(self, *, plan_id: str | None = None) -> tuple[RunManifest, ...]:
        if plan_id is None:
            with self._connect() as connection:
                rows = connection.execute("SELECT manifest_json FROM fleet_runs ORDER BY updated_at, run_id").fetchall()
            runs = tuple(RunManifest.from_dict(json.loads(row["manifest_json"])) for row in rows)
        else:
            runs = self.find_by_plan(plan_id)
        return tuple(run for run in runs if run.resumable)
