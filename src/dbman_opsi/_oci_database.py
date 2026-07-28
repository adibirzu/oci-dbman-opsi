"""Database reads: DB systems, databases, PDBs, Autonomous, Exadata infra."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from dbman_opsi._oci_base import _OciBase


class DatabaseCommands(_OciBase):
    def disable_database_management(self, database_id: str) -> None:
        self.run(["db", "database", "disable-database-management", "--database-id", database_id])

    def disable_pluggable_database_management(self, pluggable_database_id: str) -> None:
        self.run([
            "db", "pluggable-database", "disable-pluggable-database-management",
            "--pluggable-database-id", pluggable_database_id,
        ])

    def disable_autonomous_database_management(self, autonomous_database_id: str) -> None:
        self.run([
            "db", "autonomous-database", "disable-autonomous-database-management",
            "--autonomous-database-id", autonomous_database_id,
        ])

    def delete_run_owned_dbcs_test_database(self, database_id: str) -> None:
        """Delete only after the lifecycle executor applied its non-production guard."""

        self.run(["db", "database", "delete", "--database-id", database_id, "--force"])

    def delete_run_owned_autonomous_test_database(self, database_id: str) -> None:
        self.run([
            "db", "autonomous-database", "delete",
            "--autonomous-database-id", database_id,
            "--force",
        ])
    def list_db_systems(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["db", "system", "list", "--compartment-id", compartment_id, "--all"])
        return self._items(data)

    def list_db_homes(
        self,
        compartment_id: str,
        *,
        db_system_id: str | None = None,
        vm_cluster_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if bool(db_system_id) == bool(vm_cluster_id):
            raise ValueError("list_db_homes requires exactly one DB system or VM cluster id")
        data = self.run_json([
            "db", "db-home", "list", "--compartment-id", compartment_id,
            *( ["--db-system-id", db_system_id] if db_system_id else ["--vm-cluster-id", vm_cluster_id] ),
            "--all",
        ])
        return self._items(data)

    def list_databases(self, compartment_id: str, db_system_id: str) -> list[dict[str, Any]]:
        return self._list_databases_by_homes(
            compartment_id,
            db_system_id=db_system_id,
        )

    def list_databases_for_vm_cluster(self, compartment_id: str, vm_cluster_id: str) -> list[dict[str, Any]]:
        return self._list_databases_by_homes(
            compartment_id,
            vm_cluster_id=vm_cluster_id,
        )

    def list_databases_for_db_home(self, compartment_id: str, db_home_id: str) -> list[dict[str, Any]]:
        return self._list_database_pages(compartment_id, "dbHomeId", db_home_id)

    def _list_databases_by_homes(
        self,
        compartment_id: str,
        *,
        db_system_id: str | None = None,
        vm_cluster_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Enumerate every DB home, then page ListDatabases at its native grain.

        The generated CLI exposes DB-system and VM-cluster convenience flags,
        while the REST API's reliable pageable filter is ``dbHomeId``.
        """

        homes = self.list_db_homes(
            compartment_id,
            db_system_id=db_system_id,
            vm_cluster_id=vm_cluster_id,
        )
        seen_ids: set[str] = set()
        databases: list[dict[str, Any]] = []
        for home in homes:
            home_id = home.get("id")
            if not isinstance(home_id, str) or not home_id:
                continue
            for database in self.list_databases_for_db_home(compartment_id, home_id):
                database_id = database.get("id")
                if isinstance(database_id, str) and database_id:
                    if database_id in seen_ids:
                        continue
                    seen_ids.add(database_id)
                databases.append(database)
        return databases

    def _list_database_pages(
        self,
        compartment_id: str,
        parent_parameter: str,
        parent_id: str,
    ) -> list[dict[str, Any]]:
        """Read every page from the database API, preserving first-seen OCID order.

        ``oci db database list`` supports neither the CLI-wide ``--all``
        shortcut nor a page parameter.  ``oci raw-request`` is parser-supported
        and exposes OCI's ``opc-next-page`` response header, so it is the
        reliable pagination path for this otherwise truncated inventory route.
        """

        query = {"compartmentId": compartment_id, parent_parameter: parent_id}
        seen_ids: set[str] = set()
        databases: list[dict[str, Any]] = []
        page: str | None = None
        seen_pages: set[str] = set()

        while True:
            page_query = dict(query)
            if page:
                page_query["page"] = page
            target_uri = (
                f"https://database.{self.region}.oraclecloud.com/20160918/databases?"
                f"{urlencode(page_query)}"
            )
            response = self.run_json([
                "raw-request",
                "--http-method",
                "GET",
                "--target-uri",
                target_uri,
            ])
            for database in self._items(response):
                database_id = database.get("id")
                if isinstance(database_id, str) and database_id:
                    if database_id in seen_ids:
                        continue
                    seen_ids.add(database_id)
                databases.append(database)

            page = self._next_database_page(response)
            if page is None:
                return databases
            if page in seen_pages:
                raise RuntimeError("OCI database list returned a repeated pagination token")
            seen_pages.add(page)

    @staticmethod
    def _next_database_page(response: Any) -> str | None:
        if not isinstance(response, dict):
            return None
        headers = response.get("headers")
        if not isinstance(headers, dict):
            return None
        for name, value in headers.items():
            if str(name).lower() == "opc-next-page" and isinstance(value, str) and value:
                return value
        return None

    def list_cloud_vm_clusters(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["db", "cloud-vm-cluster", "list", "--compartment-id", compartment_id, "--all"])
        return self._items(data)

    def list_vm_clusters(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["db", "vm-cluster", "list", "--compartment-id", compartment_id, "--all"])
        return self._items(data)

    def get_database(self, database_id: str) -> dict[str, Any]:
        return self._data(self.run_json(["db", "database", "get", "--database-id", database_id]))

    def get_db_system(self, db_system_id: str) -> dict[str, Any]:
        return self._data(self.run_json(["db", "system", "get", "--db-system-id", db_system_id]))

    def list_pluggable_databases(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["db", "pluggable-database", "list", "--compartment-id", compartment_id, "--all"])
        return self._items(data)

    def get_pluggable_database(self, pluggable_database_id: str) -> dict[str, Any]:
        return self._data(self.run_json([
            "db",
            "pluggable-database",
            "get",
            "--pluggable-database-id",
            pluggable_database_id,
        ]))

    def list_autonomous_databases(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["db", "autonomous-database", "list", "--compartment-id", compartment_id, "--all"])
        return self._items(data)

    def get_autonomous_database(self, autonomous_database_id: str) -> dict[str, Any]:
        return self._data(self.run_json([
            "db",
            "autonomous-database",
            "get",
            "--autonomous-database-id",
            autonomous_database_id,
        ]))

    def list_exadata_infrastructure(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["db", "cloud-exa-infra", "list", "--compartment-id", compartment_id, "--all"])
        return self._items(data)
