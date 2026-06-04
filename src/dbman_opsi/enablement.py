"""Enable Database Management and Ops Insights for configured targets."""

from __future__ import annotations

from dbman_opsi.config import EnablementConfig, Target
from dbman_opsi.oci_cli import OciCli

CLOUD_REQUIRED_FIELDS = ("resource_id", "password_secret_id", "private_endpoint_id", "service_name", "monitoring_user")


def missing_cloud_fields(target: Target) -> list[str]:
    values = {
        "resource_id": target.resource_id,
        "password_secret_id": target.password_secret_id,
        "private_endpoint_id": target.private_endpoint_id,
        "service_name": target.service_name,
        "monitoring_user": target.monitoring_user,
    }
    return [name for name in CLOUD_REQUIRED_FIELDS if not values[name]]


def cloud_enable_command(target: Target) -> list[str]:
    """Build the enable-database-management argument list for the target's role.

    Containers / non-CDB databases use ``db database enable-database-management``
    (which takes ``--management-type``). Pluggable databases use
    ``db pluggable-database enable-pluggable-database-management`` with
    ``--pluggable-database-id`` and no management type.
    """

    if target.database_role == "PDB":
        return [
            "db",
            "pluggable-database",
            "enable-pluggable-database-management",
            "--pluggable-database-id",
            target.resource_id or "",
            "--password-secret-id",
            target.password_secret_id or "",
            "--private-end-point-id",
            target.private_endpoint_id or "",
            "--service-name",
            target.service_name or "",
            "--user-name",
            target.monitoring_user or "",
        ]
    return [
        "db",
        "database",
        "enable-database-management",
        "--database-id",
        target.resource_id or "",
        "--management-type",
        target.management_type,
        "--password-secret-id",
        target.password_secret_id or "",
        "--private-end-point-id",
        target.private_endpoint_id or "",
        "--service-name",
        target.service_name or "",
        "--user-name",
        target.monitoring_user or "",
    ]


def cloud_modify_command(target: Target) -> list[str]:
    """Build the modify-(pluggable-)database-management argument list.

    Used to *reconcile* an already-enabled Database Management connection — e.g.
    when the service name or monitoring credential changed after the initial
    enable. Without this, a re-run silently keeps stale connection details and
    monitoring stays broken (ORA-12514 wrong service / ORA-01017 wrong password).
    Waits for the database to return to AVAILABLE so sequential CDB-then-PDB
    updates on the same DB system do not collide.
    """

    wait = ["--wait-for-state", "AVAILABLE", "--max-wait-seconds", "900"]
    conn = [
        "--service-name", target.service_name or "",
        "--password-secret-id", target.password_secret_id or "",
        "--private-end-point-id", target.private_endpoint_id or "",
        "--user-name", target.monitoring_user or "",
        "--role", "NORMAL", "--protocol", "TCP", "--port", "1521",
    ]
    if target.database_role == "PDB":
        return [
            "db", "pluggable-database", "modify-pluggable-database-management",
            "--pluggable-database-id", target.resource_id or "",
            *conn, *wait,
        ]
    return [
        "db", "database", "modify-database-management",
        "--database-id", target.resource_id or "",
        "--management-type", target.management_type,
        *conn, *wait,
    ]


class EnablementService:
    def __init__(self, oci: OciCli) -> None:
        self.oci = oci

    def enable_all(self, config: EnablementConfig) -> None:
        for target in config.targets:
            self.enable_target(target)

    def enable_target(self, target: Target) -> None:
        if target.kind == "autonomous":
            self._enable_autonomous(target)
            return
        if target.kind in {"dbcs", "exadata"}:
            self._enable_cloud_database(target)
            return
        if target.kind in {"external-db", "external-exadata"}:
            self._print_external_next_step(target)
            return
        raise ValueError(f"Unsupported target kind: {target.kind}")

    def enable_opsi(self, target: Target) -> None:
        if target.kind not in {"dbcs", "exadata"}:
            return
        self._enable_opsi_pe_comanaged_if_ready(target)

    def _enable_autonomous(self, target: Target) -> None:
        if not target.resource_id:
            raise ValueError(f"Target {target.name} is missing resource_id")
        self.oci.run([
            "db",
            "autonomous-database",
            "enable-autonomous-database-management",
            "--autonomous-database-id",
            target.resource_id,
        ])
        if target.opsi_database_insight_id:
            self.oci.run([
                "opsi",
                "database-insights",
                "enable-autonomous-database",
                "--database-insight-id",
                target.opsi_database_insight_id,
                "--is-advanced-features-enabled",
                "false",
            ])

    # Markers returned by the Database service when Database Management is
    # already on (or its enable request is already in flight). Treated as an
    # idempotent no-op so re-runs proceed to the Ops Insights step.
    DBM_ALREADY_ENABLED_MARKERS = ("already enabled", "already created")

    def _enable_cloud_database(self, target: Target) -> None:
        missing = missing_cloud_fields(target)
        if missing:
            raise ValueError(f"Target {target.name} is missing required fields: {', '.join(missing)}")
        applied = self.oci.run_tolerating(
            cloud_enable_command(target), tolerated=self.DBM_ALREADY_ENABLED_MARKERS
        )
        if not applied:
            # Already enabled — reconcile the connection so a corrected service
            # name or rotated credential actually takes effect (a bare re-enable
            # 409s and would otherwise leave stale, broken monitoring details).
            print(f"Database Management already enabled for {target.name}; reconciling connection")
            self.oci.run(cloud_modify_command(target))
        self._enable_opsi_pe_comanaged_if_ready(target)

    def _enable_opsi_pe_comanaged_if_ready(self, target: Target) -> None:
        shared_missing = [
            name
            for name, value in {
                "compartment_id": target.compartment_id,
                "opsi_private_endpoint_id": target.opsi_private_endpoint_id,
                "opsi_credential_details_file": target.opsi_credential_details_file,
                "service_name": target.service_name,
            }.items()
            if not value
        ]
        if shared_missing:
            print(f"Skipping Ops Insights for {target.name}; missing: {', '.join(shared_missing)}")
            return
        if self._opsi_insight_active(target):
            # Idempotent: an ACTIVE insight already collects, so do not re-create
            # (create-pe-comanaged on an existing insight conflicts / hangs).
            print(f"Ops Insights insight already ACTIVE for {target.name}; skipping create")
            return
        if not target.opsi_database_insight_id:
            self._create_opsi_pe_comanaged(target)
            return
        args = [
            "opsi",
            "database-insights",
            "enable-pe-comanaged-database",
            "--compartment-id",
            target.compartment_id or "",
            "--opsi-private-endpoint-id",
            target.opsi_private_endpoint_id or "",
            "--service-name",
            target.service_name or "",
            "--credential-details",
            f"file://{target.opsi_credential_details_file}",
            "--database-insight-id",
            target.opsi_database_insight_id or "",
        ]
        if target.opsi_connection_details_file:
            args.extend(["--connection-details", f"file://{target.opsi_connection_details_file}"])
        self.oci.run(args)

    def _opsi_insight_active(self, target: Target) -> bool:
        """True when an ACTIVE OPSI insight already exists for this database."""

        compartment = target.compartment_id or ""
        if not compartment or not target.resource_id:
            return False
        # The opsi list endpoint flaps (NotAuthorizedOrNotFound); retry a few
        # times so a transient failure does not fall through to a conflicting create.
        for _ in range(3):
            try:
                insights = self.oci.list_opsi_database_insights(compartment)
            except AttributeError:
                return False
            except RuntimeError:
                continue
            return any(
                insight.get("database-id") == target.resource_id
                and insight.get("lifecycle-state") == "ACTIVE"
                for insight in insights
            )
        return False

    def _create_opsi_pe_comanaged(self, target: Target) -> None:
        missing = [
            name
            for name, value in {
                "resource_id": target.resource_id,
                "private_endpoint_id": target.private_endpoint_id,
                "database_resource_type": target.database_resource_type,
                "deployment_type": target.deployment_type,
            }.items()
            if not value
        ]
        if missing:
            print(f"Skipping Ops Insights for {target.name}; missing: {', '.join(missing)}")
            return
        args = [
            "opsi",
            "database-insights",
            "create-pe-comanged-database",
            "--compartment-id",
            target.compartment_id or "",
            "--database-id",
            target.resource_id or "",
            "--database-resource-type",
            target.database_resource_type,
            "--service-name",
            target.service_name or "",
            "--credential-details",
            f"file://{target.opsi_credential_details_file}",
            "--deployment-type",
            target.deployment_type,
            "--opsi-private-endpoint-id",
            target.opsi_private_endpoint_id or "",
            "--wait-for-state",
            "SUCCEEDED",
            "--max-wait-seconds",
            "1200",
            "--wait-interval-seconds",
            "30",
        ]
        if target.opsi_connection_details_file:
            args.extend(["--connection-details", f"file://{target.opsi_connection_details_file}"])
        # Tolerate a 409 "already exists" so a flaky active-check that fell through
        # does not fail the run when the insight is in fact present.
        created = self.oci.run_tolerating(args, tolerated=("already exists",))
        if not created:
            print(f"Ops Insights insight already exists for {target.name}; left as-is")

    def _print_external_next_step(self, target: Target) -> None:
        print(f"External target {target.name}: run generated Management Agent script, then rerun validate.")
