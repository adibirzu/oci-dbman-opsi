"""Portable, checksum-bound copies of the protected local fleet state.

Object Storage encryption is service-side.  This module intentionally uploads
only the SQLite artifact, never answer files, environment files, or secrets.
"""

from __future__ import annotations

import hashlib
import os
import re
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class StateConflictError(RuntimeError):
    pass


class StateIntegrityError(RuntimeError):
    pass


class ObjectStorage(Protocol):
    def get_object_state(self, namespace: str, bucket: str, name: str) -> tuple[bytes, str | None, dict[str, str]]: ...
    def put_object_state(self, namespace: str, bucket: str, name: str, body: bytes, *, if_match: str | None, metadata: dict[str, str]) -> str | None: ...


@dataclass(frozen=True)
class PortableStateBinding:
    run_id: str
    plan_id: str
    checksum: str
    version: str | None = None


@dataclass(frozen=True)
class RemoteLease:
    run_id: str
    plan_id: str
    owner: str
    version: str | None
    expires_at: float


class RemoteLeaseHeartbeat:
    """Renew a conditional Object Storage lease while lifecycle work blocks.

    ``assert_held`` is the remote fencing check used immediately around OCI
    facade calls.  On any failed conditional renewal the lease is considered
    lost permanently; callers must not checkpoint or make another request.
    """

    def __init__(
        self,
        backend: "ObjectStorageStateBackend",
        lease: RemoteLease,
        *,
        ttl_seconds: float = 120.0,
        interval_seconds: float | None = None,
    ) -> None:
        self.backend = backend
        self._lease = lease
        self.ttl_seconds = max(0.03, ttl_seconds)
        self.interval_seconds = interval_seconds or max(0.01, self.ttl_seconds / 3)
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="portable-fleet-lease-heartbeat", daemon=True)
        self._thread.start()

    def assert_held(self) -> None:
        if self._lost.is_set():
            raise StateConflictError("portable fleet lease was lost")

    def close(self) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.1, self.interval_seconds * 2))
        if not self._lost.is_set():
            with self._lock:
                self.backend.release_lease(self._lease)

    def _run(self) -> None:
        while not self._stopped.wait(self.interval_seconds):
            try:
                with self._lock:
                    self._lease = self.backend.renew_lease(self._lease, ttl_seconds=self.ttl_seconds)
            except StateConflictError:
                self._lost.set()
                return


class ObjectStorageStateBackend:
    """Synchronize a 0600 local SQLite cache using optimistic object versions."""

    def __init__(self, client: ObjectStorage, *, namespace: str, bucket: str, name: str, cache_path: str | Path) -> None:
        self.client, self.namespace, self.bucket, self.name = client, namespace, bucket, name
        self.cache_path = Path(cache_path)

    @staticmethod
    def _checksum(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    def upload(self, *, run_id: str, plan_id: str, expected_version: str | None = None) -> PortableStateBinding:
        body = self.cache_path.read_bytes()
        checksum = self._checksum(body)
        metadata = {"run-id": run_id, "plan-id": plan_id, "sha256": checksum, "schema-version": "1", "content-type": "application/vnd.sqlite3"}
        try:
            if _contains_plaintext_secret(body):
                raise StateIntegrityError("portable state contains apparent plaintext secret material")
            version = self.client.put_object_state(self.namespace, self.bucket, self.name, body, if_match=expected_version, metadata=metadata)
        except StateIntegrityError:
            raise
        except Exception as exc:
            raise StateConflictError("portable state upload conflict") from exc
        return PortableStateBinding(run_id, plan_id, checksum, version)

    def download(self, *, run_id: str, plan_id: str, expected_checksum: str | None = None) -> PortableStateBinding:
        body, version, metadata = self.client.get_object_state(self.namespace, self.bucket, self.name)
        checksum = self._checksum(body)
        if metadata.get("schema-version") != "1" or metadata.get("run-id") != run_id or metadata.get("plan-id") != plan_id or metadata.get("sha256") != checksum:
            raise StateIntegrityError("portable state run, plan, or checksum binding does not match")
        if expected_checksum and checksum != expected_checksum:
            raise StateIntegrityError("portable state checksum does not match")
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
        os.chmod(self.cache_path, 0o600)
        return PortableStateBinding(run_id, plan_id, checksum, version)

    @property
    def lease_name(self) -> str:
        return self.name + ".fleet-lease"

    def acquire_lease(self, *, run_id: str, plan_id: str, owner: str, ttl_seconds: float = 120.0, now: float | None = None) -> RemoteLease:
        """Conditionally acquire an Object Storage lease before an OCI write.

        A fresh object is create-only (`if_match=None`); replacing an expired
        lease requires its exact ETag.  Any ambiguous object-store error is a
        conflict, never permission to continue.
        """
        timestamp = time.time() if now is None else now
        previous_version: str | None = None
        try:
            body, previous_version, _metadata = self.client.get_object_state(self.namespace, self.bucket, self.lease_name)
            previous = json.loads(body.decode("utf-8"))
            if not isinstance(previous, dict) or float(previous.get("expires_at", 0)) > timestamp:
                raise StateConflictError("portable fleet lease is held by another actor")
        except StateConflictError:
            raise
        except Exception:
            # Object absence is represented differently by OCI clients.  The
            # following create-only put is the authority; if it was not absent,
            # that put must fail closed.
            previous_version = None
        expires_at = timestamp + ttl_seconds
        payload = json.dumps({"run_id": run_id, "plan_id": plan_id, "owner": owner, "expires_at": expires_at}, sort_keys=True).encode("utf-8")
        try:
            version = self.client.put_object_state(self.namespace, self.bucket, self.lease_name, payload, if_match=previous_version, metadata={"run-id": run_id, "plan-id": plan_id, "lease-owner": owner, "expires-at": str(expires_at), "schema-version": "1"})
        except Exception as exc:
            raise StateConflictError("portable fleet lease acquisition conflict") from exc
        return RemoteLease(run_id, plan_id, owner, version, expires_at)

    def renew_lease(self, lease: RemoteLease, *, ttl_seconds: float = 120.0, now: float | None = None) -> RemoteLease:
        return self._replace_lease(lease, expires_at=(time.time() if now is None else now) + ttl_seconds)

    def release_lease(self, lease: RemoteLease, *, now: float | None = None) -> None:
        # The state facade intentionally has no delete primitive.  An expired,
        # owner-bound marker is safe: another actor must still win a conditional
        # replacement before proceeding.
        self._replace_lease(lease, expires_at=time.time() if now is None else now)

    def _replace_lease(self, lease: RemoteLease, *, expires_at: float) -> RemoteLease:
        payload = json.dumps({"run_id": lease.run_id, "plan_id": lease.plan_id, "owner": lease.owner, "expires_at": expires_at}, sort_keys=True).encode("utf-8")
        try:
            version = self.client.put_object_state(self.namespace, self.bucket, self.lease_name, payload, if_match=lease.version, metadata={"run-id": lease.run_id, "plan-id": lease.plan_id, "lease-owner": lease.owner, "expires-at": str(expires_at), "schema-version": "1"})
        except Exception as exc:
            raise StateConflictError("portable fleet lease was lost") from exc
        return RemoteLease(lease.run_id, lease.plan_id, lease.owner, version, expires_at)


def _contains_plaintext_secret(body: bytes) -> bool:
    text = body.decode("utf-8", errors="ignore")
    return bool(re.search(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|(?:password|secret_value)\s*[:=]\s*[^\s<]", text, re.I))
