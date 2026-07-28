"""OCI Log Analytics command facade."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from dbman_opsi._oci_base import _OciBase


class LogAnalyticsCommands(_OciBase):
    def _object_storage_namespace(self) -> str:
        return str(self.run_json(["os", "ns", "get"]).get("data") or "")

    def get_log_analytics_namespace(self, compartment_id: str) -> str:
        _ = compartment_id
        namespace = self._object_storage_namespace()
        data = self.run_json([
            "log-analytics", "namespace", "get",
            "--namespace-name", namespace,
        ])
        payload = self._data(data)
        return str(payload.get("namespace-name") or payload.get("namespace") or "")

    def onboard_log_analytics_namespace(self, compartment_id: str) -> str:
        _ = compartment_id
        namespace = self._object_storage_namespace()
        data = self.run_json([
            "log-analytics", "namespace", "onboard",
            "--namespace-name", namespace,
        ])
        payload = self._data(data)
        return str(payload.get("namespace-name") or payload.get("namespace") or "")

    def list_log_analytics_log_groups(self, namespace: str, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json([
            "log-analytics", "log-group", "list",
            "--namespace-name", namespace,
            "--compartment-id", compartment_id,
            "--all",
        ])
        return self._items(data)

    def create_log_analytics_log_group(self, namespace: str, compartment_id: str, display_name: str) -> str:
        for existing in self.list_log_analytics_log_groups(namespace, compartment_id):
            if existing.get("display-name") == display_name or existing.get("name") == display_name:
                return str(existing.get("id") or "")
        data = self.run_json([
            "log-analytics", "log-group", "create",
            "--namespace-name", namespace,
            "--compartment-id", compartment_id,
            "--display-name", display_name,
        ])
        return str(self._data(data).get("id") or "")

    def list_log_analytics_entities(self, namespace: str, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json([
            "log-analytics", "entity", "list",
            "--namespace-name", namespace,
            "--compartment-id", compartment_id,
            "--all",
        ])
        return self._items(data)

    def list_log_analytics_associated_entities(self, namespace: str, compartment_id: str) -> list[dict[str, Any]]:
        """List entities with at least one configured Log Analytics source association."""

        data = self.run_json([
            "log-analytics", "assoc", "list-associated-entities",
            "--namespace-name", namespace,
            "--compartment-id", compartment_id,
            "--all",
        ])
        return self._items(data)

    def list_log_analytics_entity_source_associations(
        self,
        namespace: str,
        compartment_id: str,
        entity_id: str,
    ) -> list[dict[str, Any]]:
        """List every source association for one entity before mutating it."""

        data = self.run_json([
            "log-analytics", "assoc", "list-entity-source-assocs",
            "--namespace-name", namespace,
            "--compartment-id", compartment_id,
            "--entity-id", entity_id,
            "--all",
        ])
        return self._items(data)

    def create_log_analytics_entity(
        self,
        namespace: str,
        compartment_id: str,
        name: str,
        entity_type_name: str,
        properties_file: str | None = None,
        cloud_resource_id: str | None = None,
        hostname: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        for existing in self.list_log_analytics_entities(namespace, compartment_id):
            if existing.get("name") == name or existing.get("display-name") == name:
                return str(existing.get("id") or "")
        args = [
            "log-analytics", "entity", "create",
            "--namespace-name", namespace,
            "--compartment-id", compartment_id,
            "--name", name,
            "--entity-type-name", entity_type_name,
        ]
        if cloud_resource_id:
            args.extend(["--cloud-resource-id", cloud_resource_id])
        if hostname:
            args.extend(["--hostname", hostname])
        if agent_id:
            args.extend(["--agent-id", agent_id])
        if properties_file:
            args.extend(["--properties", f"file://{properties_file}"])
        data = self.run_json(args)
        return str(self._data(data).get("id") or "")

    def list_log_analytics_sources(self, namespace: str) -> list[dict[str, Any]]:
        data = self.run_json([
            "log-analytics", "source", "list-sources",
            "--namespace-name", namespace,
            "--compartment-id", self.profile_tenancy(),
            "--all",
        ])
        return self._items(data)

    def upsert_log_analytics_associations(
        self,
        namespace: str,
        compartment_id: str,
        items: list[dict[str, Any]],
    ) -> None:
        with NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
            json.dump(items, handle)
            handle.write("\n")
            temp_path = handle.name
        try:
            self.run([
                "log-analytics", "assoc", "upsert-assocs",
                "--namespace-name", namespace,
                "--compartment-id", compartment_id,
                "--items", f"file://{temp_path}",
            ])
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def delete_log_analytics_associations(
        self,
        namespace: str,
        compartment_id: str,
        items: list[dict[str, Any]],
    ) -> None:
        """Remove specifically recorded source associations, never an entire namespace."""

        with NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
            json.dump(items, handle)
            handle.write("\n")
            temp_path = handle.name
        try:
            self.run([
                "log-analytics", "assoc", "delete-assocs",
                "--namespace-name", namespace,
                "--compartment-id", compartment_id,
                "--items", f"file://{temp_path}",
            ])
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def upsert_log_analytics_association(self, namespace: str, compartment_id: str, payload_file: str) -> None:
        payload = json.loads(Path(payload_file).read_text(encoding="utf-8"))
        items = payload if isinstance(payload, list) else [payload]
        self.upsert_log_analytics_associations(namespace, compartment_id, items)

    def list_log_analytics_warnings(self, namespace: str, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json([
            "log-analytics", "warning", "list",
            "--namespace-name", namespace,
            "--compartment-id", compartment_id,
            "--all",
        ])
        return self._items(data)

    def search_log_analytics(
        self,
        namespace: str,
        query: str,
        compartment_id: str | None = None,
        time_start: str | None = None,
        time_end: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        resolved_compartment_id = compartment_id or self.profile_tenancy()
        args = [
            "log-analytics", "query", "search",
            "--namespace-name", namespace,
            "--compartment-id", resolved_compartment_id,
            "--query-string", query,
            "--sub-system", "LOG",
        ]
        if time_start:
            args.extend(["--time-start", time_start])
        if time_end:
            args.extend(["--time-end", time_end])
        if limit is not None:
            args.extend(["--limit", str(max(1, min(limit, 500)))])
        return self._data(self.run_json(args))
