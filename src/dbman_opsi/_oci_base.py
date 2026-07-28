"""Shared plumbing for the Oci CLI command mixins.

Every domain mixin (network, database, vault, …) inherits this base so it can
reach ``run_json``/``run``/``_items``/``_data`` via ``self`` while living in its
own focused module. ``OciCli`` composes the mixins; their common base collapses
to a single entry in the MRO, so ``__init__`` runs exactly once.
"""

from __future__ import annotations

import configparser
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from dbman_opsi.runner import CommandRunner, OciError
from dbman_opsi.fleet_auth import OciAuth


class _OciBase:
    def __init__(self, profile: str, region: str, runner: CommandRunner, auth: OciAuth | None = None) -> None:
        self.profile = profile
        self.region = region
        self.runner = runner
        self.auth = auth

    def run_json(self, args: list[str]) -> Any:
        result = self.runner.run(
            self._base_args() + args + ["--output", "json"],
            retry_on_transient=True,
        )
        return result.json()

    def run(self, args: list[str]) -> None:
        self.runner.run(self._base_args() + args)

    def run_tolerating(self, args: list[str], tolerated: tuple[str, ...]) -> bool:
        """Run a mutating command, swallowing already-done errors.

        Returns ``True`` when the command actually ran, ``False`` when it failed
        with an error whose message contains any ``tolerated`` marker (an
        idempotent no-op, e.g. a resource that is already enabled). Any other
        failure is re-raised so genuine errors are not hidden.
        """

        try:
            self.run(args)
            return True
        except OciError as exc:
            message = str(exc).lower()
            if any(marker.lower() in message for marker in tolerated):
                return False
            raise

    @staticmethod
    def _items(data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        payload = data.get("data", [])
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return list(payload["items"])
        if isinstance(payload, list):
            return list(payload)
        return []

    @staticmethod
    def _data(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        payload = data.get("data", {})
        return dict(payload) if isinstance(payload, dict) else {}

    def _base_args(self) -> list[str]:
        if self.auth is not None:
            return self.auth.cli_args(region=self.region)
        auth = os.environ.get("DBMAN_OPSI_OCI_AUTH") or os.environ.get("OCI_AUTH")
        if auth:
            return ["oci", "--region", self.region, "--auth", auth]
        return ["oci", "--profile", self.profile, "--region", self.region]

    def profile_tenancy(self) -> str | None:
        """Return the tenancy OCID configured for this OCI CLI profile, if readable."""

        config_file = Path(os.environ.get("OCI_CONFIG_FILE", "~/.oci/config")).expanduser()
        if not config_file.exists():
            return None
        parser = configparser.ConfigParser()
        parser.read(config_file)
        if parser.has_option(self.profile, "tenancy"):
            return parser.get(self.profile, "tenancy")
        return parser.get("DEFAULT", "tenancy", fallback=None)

    def get_object_state(self, namespace: str, bucket: str, name: str) -> tuple[bytes, str | None, dict[str, str]]:
        """Fetch an Object Storage artifact with metadata using OCI CLI get/head."""
        head = self.runner.run(self._base_args() + ["os", "object", "head", "--namespace", namespace, "--bucket-name", bucket, "--name", name, "--output", "json"], retry_on_transient=True).json() or {}
        metadata = dict((head.get("data") or {}).get("metadata") or {})
        entity = (head.get("data") or {}).get("etag") or (head.get("data") or {}).get("e-tag")
        with tempfile.NamedTemporaryFile() as handle:
            self.runner.run(self._base_args() + ["os", "object", "get", "--namespace", namespace, "--bucket-name", bucket, "--name", name, "--file", handle.name, "--no-multipart"], retry_on_transient=True)
            return Path(handle.name).read_bytes(), str(entity) if entity else None, {str(k): str(v) for k, v in metadata.items()}

    def put_object_state(self, namespace: str, bucket: str, name: str, body: bytes, *, if_match: str | None, metadata: dict[str, str]) -> str | None:
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(body); handle.flush()
            args = ["os", "object", "put", "--namespace", namespace, "--bucket-name", bucket, "--name", name, "--file", handle.name, "--metadata", json.dumps(metadata, sort_keys=True), "--no-multipart", "--verify-checksum"]
            if if_match:
                args += ["--if-match", if_match]
            else:
                # Installed OCI CLI exposes create-only protection as
                # --no-overwrite (not --if-none-match).
                args += ["--no-overwrite"]
            self.runner.run(self._base_args() + args)
        head = self.runner.run(self._base_args() + ["os", "object", "head", "--namespace", namespace, "--bucket-name", bucket, "--name", name, "--output", "json"], retry_on_transient=True).json() or {}
        data = head.get("data") or {}
        return str(data.get("etag") or data.get("e-tag") or "") or None
