"""IAM reads: compartments, policies, groups."""

from __future__ import annotations

from typing import Any

from dbman_opsi._oci_base import _OciBase


class IamCommands(_OciBase):
    def list_subscribed_regions(self, tenancy_id: str) -> list[dict[str, Any]]:
        """List the tenancy's subscribed OCI regions without changing state."""

        data = self.run_json([
            "iam",
            "region-subscription",
            "list",
            "--tenancy-id",
            tenancy_id,
            "--all",
        ])
        return self._items(data)

    def list_compartments(self, tenancy_id: str) -> list[dict[str, Any]]:
        data = self.run_json([
            "iam",
            "compartment",
            "list",
            "--compartment-id",
            tenancy_id,
            "--compartment-id-in-subtree",
            "true",
            "--access-level",
            "ACCESSIBLE",
            "--lifecycle-state",
            "ACTIVE",
            "--all",
        ])
        return self._items(data)

    def list_policies(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["iam", "policy", "list", "--compartment-id", compartment_id, "--all"])
        return self._items(data)

    def get_group(self, group_id: str) -> dict[str, Any]:
        return self._data(self.run_json(["iam", "group", "get", "--group-id", group_id]))
