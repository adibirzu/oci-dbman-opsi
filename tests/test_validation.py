from dbman_opsi.config import EnablementConfig, Target
from dbman_opsi.validation import ValidationService


class FakeOci:
    def __init__(self, agents, insights=None, insight_failures=0):
        self.agents = agents
        self.insights = insights or []
        self.insight_failures = insight_failures
        self.insight_calls = 0

    def list_management_agents(self, compartment_id):
        return self.agents

    def list_opsi_database_insights(self, compartment_id):
        self.insight_calls += 1
        if self.insight_calls <= self.insight_failures:
            raise RuntimeError("NotAuthorizedOrNotFound")
        return self.insights

    def get_autonomous_database(self, autonomous_database_id):
        return {"database-management-status": "ENABLED", "operations-insights-status": "ENABLED"}

    def get_database(self, database_id):
        return {"database-management-config": {"management-status": "ENABLED"}}

    def get_pluggable_database(self, pluggable_database_id):
        return {"pluggable-database-management-config": {"management-status": "ENABLED"}}


def test_validation_detects_registered_external_agent() -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="compartment-id",
        targets=(Target(kind="external-db", name="salesdb", compartment_id="compartment-id"),),
    )
    service = ValidationService(FakeOci([{"display-name": "salesdb-agent"}]))  # type: ignore[arg-type]

    assert service.validate(config) == ["salesdb: Management Agent registered"]


def test_validation_reports_missing_agent_and_resource_id() -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        targets=(
            Target(kind="external-db", name="external"),
            Target(kind="dbcs", name="cloud"),
        ),
    )
    service = ValidationService(FakeOci([]))  # type: ignore[arg-type]

    assert service.validate(config) == ["external: Management Agent not found yet", "cloud: missing resource OCID"]


def test_validation_reports_active_opsi_insight() -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="compartment-id",
        targets=(
            Target(kind="autonomous", name="adb", resource_id="adb-id"),
            Target(kind="dbcs", name="dbcs", resource_id="db-id", compartment_id="compartment-id"),
        ),
    )
    insights = [{"database-id": "db-id", "lifecycle-state": "ACTIVE", "status": "ENABLED"}]
    service = ValidationService(FakeOci([], insights))  # type: ignore[arg-type]

    assert service.validate(config) == [
        "adb: Database Management ENABLED; Ops Insights ENABLED",
        "dbcs (CDB): Database Management ENABLED; Ops Insights ACTIVE (ENABLED)",
    ]


def test_validation_surfaces_failed_opsi_insight() -> None:
    # A broken Ops Insights collection must be reported as FAILED, not hidden.
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="compartment-id",
        targets=(Target(kind="dbcs", name="dbcs", resource_id="db-id", compartment_id="compartment-id"),),
    )
    insights = [{"database-id": "db-id", "lifecycle-state": "FAILED", "status": "ENABLED"}]
    service = ValidationService(FakeOci([], insights))  # type: ignore[arg-type]

    assert service.validate(config) == [
        "dbcs (CDB): Database Management ENABLED; Ops Insights FAILED (ENABLED)",
    ]


def test_validation_reports_missing_opsi_insight() -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="compartment-id",
        targets=(Target(kind="dbcs", name="pdb1", resource_id="pdb-id", database_role="PDB", compartment_id="compartment-id"),),
    )
    service = ValidationService(FakeOci([], []))  # type: ignore[arg-type]

    assert service.validate(config) == [
        "pdb1 (PDB): Database Management ENABLED; Ops Insights NOT_FOUND (no Database Insight)",
    ]


def test_validation_retries_then_reads_insight_after_transient_404() -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="compartment-id",
        targets=(Target(kind="dbcs", name="dbcs", resource_id="db-id", compartment_id="compartment-id"),),
    )
    insights = [{"database-id": "db-id", "lifecycle-state": "ACTIVE", "status": "ENABLED"}]
    service = ValidationService(FakeOci([], insights, insight_failures=1))  # type: ignore[arg-type]

    assert service.validate(config) == [
        "dbcs (CDB): Database Management ENABLED; Ops Insights ACTIVE (ENABLED)",
    ]


def test_validation_degrades_to_unknown_when_insight_query_keeps_failing() -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="compartment-id",
        targets=(Target(kind="dbcs", name="dbcs", resource_id="db-id", compartment_id="compartment-id"),),
    )
    service = ValidationService(FakeOci([], insight_failures=5))  # type: ignore[arg-type]

    assert service.validate(config) == [
        "dbcs (CDB): Database Management ENABLED; Ops Insights UNKNOWN (insight query failed; verify in OCI Console)",
    ]


def test_validation_reads_pdb_nested_status() -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="compartment-id",
        targets=(Target(kind="dbcs", name="pdb1", resource_id="pdb-id", database_role="PDB", compartment_id="compartment-id"),),
    )
    insights = [{"database-id": "pdb-id", "lifecycle-state": "ACTIVE", "status": "ENABLED"}]
    service = ValidationService(FakeOci([], insights))  # type: ignore[arg-type]

    assert service.validate(config) == [
        "pdb1 (PDB): Database Management ENABLED; Ops Insights ACTIVE (ENABLED)",
    ]
