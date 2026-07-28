"""Supporting infrastructure reads: bastions and Management Agents."""

from __future__ import annotations

from typing import Any

from dbman_opsi._oci_base import _OciBase


class InfraCommands(_OciBase):
    def list_bastions(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["bastion", "bastion", "list", "--compartment-id", compartment_id])
        return self._items(data)

    def list_management_agents(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["management-agent", "agent", "list", "--compartment-id", compartment_id])
        return self._items(data)

    def get_management_agent(self, agent_id: str) -> dict[str, Any]:
        return self._data(self.run_json(["management-agent", "agent", "get", "--management-agent-id", agent_id]))

    def list_audit_events(self, compartment_id: str, start_time: str, end_time: str) -> list[dict[str, Any]]:
        data = self.run_json([
            "audit",
            "event",
            "list",
            "--compartment-id",
            compartment_id,
            "--start-time",
            start_time,
            "--end-time",
            end_time,
            "--all",
        ])
        return self._items(data)
