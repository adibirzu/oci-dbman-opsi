"""Small OCI CLI facade used by discovery, enablement, and validation."""

from __future__ import annotations

from typing import Any

from dbman_opsi.runner import CommandRunner


class OciCli:
    def __init__(self, profile: str, region: str, runner: CommandRunner) -> None:
        self.profile = profile
        self.region = region
        self.runner = runner

    def run_json(self, args: list[str]) -> Any:
        result = self.runner.run(self._base_args() + args + ["--output", "json"])
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
        except RuntimeError as exc:
            message = str(exc)
            if any(marker in message for marker in tolerated):
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
        return ["oci", "--profile", self.profile, "--region", self.region]

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
        ])
        return self._items(data)

    def list_vcns(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["network", "vcn", "list", "--compartment-id", compartment_id])
        return self._items(data)

    def list_subnets(self, compartment_id: str, vcn_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["network", "subnet", "list", "--compartment-id", compartment_id, "--vcn-id", vcn_id])
        return self._items(data)

    def list_db_systems(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["db", "system", "list", "--compartment-id", compartment_id])
        return self._items(data)

    def list_databases(self, compartment_id: str, db_system_id: str) -> list[dict[str, Any]]:
        data = self.run_json([
            "db",
            "database",
            "list",
            "--compartment-id",
            compartment_id,
            "--db-system-id",
            db_system_id,
        ])
        return self._items(data)

    def get_database(self, database_id: str) -> dict[str, Any]:
        return self._data(self.run_json(["db", "database", "get", "--database-id", database_id]))

    def get_db_system(self, db_system_id: str) -> dict[str, Any]:
        return self._data(self.run_json(["db", "system", "get", "--db-system-id", db_system_id]))

    def list_pluggable_databases(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["db", "pluggable-database", "list", "--compartment-id", compartment_id])
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
        data = self.run_json(["db", "autonomous-database", "list", "--compartment-id", compartment_id])
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
        data = self.run_json(["db", "cloud-exa-infra", "list", "--compartment-id", compartment_id])
        return self._items(data)

    def list_management_agents(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["management-agent", "agent", "list", "--compartment-id", compartment_id])
        return self._items(data)

    def list_vaults(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["kms", "management", "vault", "list", "--compartment-id", compartment_id])
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

    def list_bastions(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["bastion", "bastion", "list", "--compartment-id", compartment_id])
        return self._items(data)

    def list_db_management_private_endpoints(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["database-management", "private-endpoint", "list", "--compartment-id", compartment_id, "--all"])
        return self._items(data)

    def list_opsi_private_endpoints(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["opsi", "opsi-private-endpoint", "list", "--compartment-id", compartment_id, "--all"])
        return self._items(data)

    # OPSI database-insights list excludes FAILED/terminal states by default, so
    # the non-ACTIVE states are queried explicitly to surface broken insights
    # during validate.
    OPSI_INSIGHT_STATES = ("CREATING", "UPDATING", "ACTIVE", "FAILED", "NEEDS_ATTENTION")

    def list_opsi_database_insights(self, compartment_id: str) -> list[dict[str, Any]]:
        """List OPSI database insights across all relevant lifecycle states.

        Root-cause note (cap, eu-frankfurt-1): combining the full ``--lifecycle-state``
        set with ``--all`` in a *single* call makes the OPSI list control plane
        flap — it intermittently returns an empty or partial page for the same
        compartment (observed bouncing between 0, 2, and 7 items call-to-call).
        A single-lifecycle-state query is stable (ACTIVE-only returned the same
        full set 10/10 times). So query one state per call and union the results
        by insight OCID: reliable, and still surfaces FAILED/terminal insights
        that the default ACTIVE-only list hides.

        Per-state calls are individually fault-tolerant: a transient failure on
        one state is skipped so it cannot discard the insights already gathered
        from the other states. If every state call fails the result is empty,
        which callers treat as inconclusive rather than authoritative absence.

        Note: a skipped state means the union is *incomplete* — an insight that
        only exists in the failed state (e.g. ``FAILED``) is silently absent. A
        caller that needs to assert *absence* must use
        :meth:`list_opsi_database_insights_complete` and refuse to conclude
        "not found" from an incomplete read.
        """

        return self.list_opsi_database_insights_complete(compartment_id)[0]

    def list_opsi_database_insights_complete(
        self, compartment_id: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """Per-state union plus a completeness flag.

        Returns ``(insights, complete)`` where ``complete`` is ``False`` if any
        per-lifecycle-state call failed (so the union may be missing insights
        that live in the failed state). Callers can trust a *positive* match
        regardless of ``complete``, but must treat a *negative* result from an
        incomplete read as inconclusive rather than authoritative absence.
        """

        merged: dict[str, dict[str, Any]] = {}
        complete = True
        for state in self.OPSI_INSIGHT_STATES:
            args = [
                "opsi",
                "database-insights",
                "list",
                "--compartment-id",
                compartment_id,
                "--compartment-id-in-subtree",
                "true",
                "--all",
                "--lifecycle-state",
                state,
            ]
            try:
                items = self._items(self.run_json(args))
            except RuntimeError:
                complete = False
                continue
            for item in items:
                key = str(item.get("id") or item.get("database-id"))
                merged[key] = item
        return list(merged.values()), complete

    def get_opsi_database_insight(self, insight_id: str) -> dict[str, Any]:
        """Get a single OPSI database insight by its OCID.

        Unlike the aggregated ``database-insights list`` (which flaps between
        empty/partial/full on the cap control plane), a single-resource GET by
        insight OCID is reliable — the authoritative way to read an insight's
        lifecycle/connection state once its OCID is known.
        """

        return self._data(self.run_json([
            "opsi",
            "database-insights",
            "get",
            "--database-insight-id",
            insight_id,
        ]))

    # --- Database Management named & preferred credentials ---

    def list_managed_databases(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json([
            "database-management", "managed-database", "list",
            "--compartment-id", compartment_id, "--all",
        ])
        return self._items(data)

    def find_managed_database_id(self, compartment_id: str, name: str) -> str | None:
        for managed in self.list_managed_databases(compartment_id):
            if managed.get("name") == name:
                return managed.get("id")
        return None

    def get_managed_database_status(self, managed_database_id: str) -> str | None:
        """Return the managed database's monitoring status (e.g. UP / DOWN / UNKNOWN)."""

        data = self.run_json([
            "database-management", "managed-database", "get",
            "--managed-database-id", managed_database_id,
        ])
        return self._data(data).get("database-status")

    def list_named_credentials(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json([
            "database-management", "named-credential", "list",
            "--compartment-id", compartment_id, "--all",
        ])
        return self._items(data)

    def create_named_credential(
        self,
        compartment_id: str,
        name: str,
        user_name: str,
        password_secret_id: str,
        associated_resource: str,
        role: str = "NORMAL",
        access_mode: str = "RESOURCE_PRINCIPAL",
    ) -> str:
        """Create a RESOURCE_PRINCIPAL named credential and return its OCID.

        Idempotent: if a named credential with ``name`` already exists in the
        compartment, its OCID is returned instead of creating a duplicate.
        """

        for existing in self.list_named_credentials(compartment_id):
            if existing.get("name") == name:
                return str(existing.get("id"))
        data = self.run_json([
            "database-management", "named-credential",
            "create-named-credential-basic-named-credential-content",
            "--compartment-id", compartment_id,
            "--name", name,
            "--scope", "RESOURCE",
            "--type", "ORACLE_DB",
            "--content-user-name", user_name,
            "--content-role", role,
            "--content-password-secret-id", password_secret_id,
            "--content-password-secret-access-mode", access_mode,
            "--associated-resource", associated_resource,
        ])
        return str(self._data(data).get("id"))

    def list_preferred_credentials(self, managed_database_id: str) -> list[dict[str, Any]]:
        data = self.run_json([
            "database-management", "preferred-credential", "list",
            "--managed-database-id", managed_database_id,
        ])
        return self._items(data)

    def set_preferred_named_credential(
        self, managed_database_id: str, credential_name: str, named_credential_id: str
    ) -> None:
        """Point a managed database's preferred credential at a named credential.

        Uses the dedicated ``update-preferred-credential-update-named-preferred-credential-details``
        verb; the generic ``preferred-credential update --type NAMED_CREDENTIAL``
        mis-maps the body and fails with RelatedResourceNotAuthorizedOrNotFound.
        """

        self.run([
            "database-management", "preferred-credential",
            "update-preferred-credential-update-named-preferred-credential-details",
            "--managed-database-id", managed_database_id,
            "--credential-name", credential_name,
            "--named-credential-id", named_credential_id,
        ])

    # --- read-only lookups used by preflight / configure ---

    def get_subnet(self, subnet_id: str) -> dict[str, Any]:
        return self._data(self.run_json(["network", "subnet", "get", "--subnet-id", subnet_id]))

    def get_vcn(self, vcn_id: str) -> dict[str, Any]:
        return self._data(self.run_json(["network", "vcn", "get", "--vcn-id", vcn_id]))

    def get_route_table(self, route_table_id: str) -> dict[str, Any]:
        return self._data(self.run_json(["network", "route-table", "get", "--rt-id", route_table_id]))

    def get_security_list(self, security_list_id: str) -> dict[str, Any]:
        return self._data(self.run_json(["network", "security-list", "get", "--security-list-id", security_list_id]))

    def list_service_gateways(self, compartment_id: str, vcn_id: str) -> list[dict[str, Any]]:
        data = self.run_json([
            "network",
            "service-gateway",
            "list",
            "--compartment-id",
            compartment_id,
            "--vcn-id",
            vcn_id,
            "--all",
        ])
        return self._items(data)

    def get_db_management_private_endpoint(self, endpoint_id: str) -> dict[str, Any]:
        return self._data(self.run_json([
            "database-management",
            "private-endpoint",
            "get",
            "--private-endpoint-id",
            endpoint_id,
        ]))

    def get_opsi_private_endpoint(self, endpoint_id: str) -> dict[str, Any]:
        return self._data(self.run_json([
            "opsi",
            "opsi-private-endpoint",
            "get",
            "--opsi-private-endpoint-id",
            endpoint_id,
        ]))

    def get_secret(self, secret_id: str) -> dict[str, Any]:
        return self._data(self.run_json(["vault", "secret", "get", "--secret-id", secret_id]))

    def list_policies(self, compartment_id: str) -> list[dict[str, Any]]:
        data = self.run_json(["iam", "policy", "list", "--compartment-id", compartment_id, "--all"])
        return self._items(data)

    def get_management_agent(self, agent_id: str) -> dict[str, Any]:
        return self._data(self.run_json(["management-agent", "agent", "get", "--management-agent-id", agent_id]))
