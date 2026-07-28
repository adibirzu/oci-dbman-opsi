"""KMS / Vault reads: vaults, keys, secrets."""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any

from dbman_opsi._oci_base import _OciBase


class VaultCommands(_OciBase):
    def schedule_run_owned_secret_deletion(
        self, secret_id: str, *, not_before: datetime | None = None
    ) -> None:
        """Schedule, rather than immediately purge, a lifecycle-owned secret."""

        args = ["vault", "secret", "schedule-secret-deletion", "--secret-id", secret_id]
        if not_before is not None:
            args.extend(["--time-of-deletion", not_before.isoformat()])
        self.run(args)
    def list_vaults(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["kms", "management", "vault", "list", "--compartment-id", compartment_id, "--all"])
        return self._items(data)

    def list_keys(self, compartment_id: str, management_endpoint: str) -> list[dict[str, Any]]:
        data = self.run_json([
            "kms",
            "management",
            "key",
            "list",
            "--compartment-id",
            compartment_id,
            "--endpoint",
            management_endpoint,
        ])
        return self._items(data)

    def list_secrets(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["vault", "secret", "list", "--compartment-id", compartment_id, "--all"])
        return self._items(data)

    def get_secret(self, secret_id: str) -> dict[str, Any]:
        return self._data(self.run_json(["vault", "secret", "get", "--secret-id", secret_id]))

    def get_secret_bundle_content(self, secret_id: str) -> str:
        """Return plaintext only to an explicit caller-authorized reveal boundary.

        Normal command/status paths must use ``get_secret`` and expose metadata
        only. This method intentionally performs no logging or persistence.
        """

        payload = self._data(self.run_json(["secrets", "secret-bundle", "get", "--secret-id", secret_id]))
        bundle = payload.get("secret-bundle-content")
        if not isinstance(bundle, dict) or not isinstance(bundle.get("content"), str):
            raise ValueError("Vault secret bundle did not contain a base64 content value")
        try:
            return base64.b64decode(bundle["content"], validate=True).decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("Vault secret bundle content is not valid UTF-8 base64") from exc
