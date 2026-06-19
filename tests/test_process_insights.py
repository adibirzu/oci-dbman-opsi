from dbman_opsi.config import EnablementConfig
from dbman_opsi.process_insights import ProcessInsightsService, format_process_insights_report


class FakeOci:
    def __init__(self, *, process_rows=0, resource_rows=1, importable_cloud=0, importable_agents=0):
        self.process_rows = process_rows
        self.resource_rows = resource_rows
        self.importable_cloud = importable_cloud
        self.importable_agents = importable_agents

    def list_importable_macs_cloud_hosts(self, compartment_id):
        return [{"compute-display-name": "host"}] * self.importable_cloud

    def list_importable_agent_entities(self, compartment_id):
        return [{"entity-name": "host"}] * self.importable_agents

    def list_opsi_host_insights(self, compartment_id):
        return [
            {
                "id": "host-insight-id",
                "host-name": "dbmanopsi",
                "host-type": "COMANAGED-VM-HOST",
                "lifecycle-state": "ACTIVE",
                "status": "ENABLED",
            }
        ]

    def summarize_host_resource_usage(
        self,
        *,
        compartment_id,
        host_insight_id,
        resource_metric,
        analysis_time_interval,
    ):
        return [{"metric": resource_metric}] * self.resource_rows

    def summarize_host_top_processes(
        self,
        *,
        compartment_id,
        host_insight_id,
        resource_metric,
        analysis_time_interval,
    ):
        return [{"metric": resource_metric}] * self.process_rows


def test_process_insights_reports_resource_metrics_without_process_samples() -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="compartment-id",
    )

    report = ProcessInsightsService(FakeOci()).diagnose(config, interval="P7D")  # type: ignore[arg-type]

    assert not report.ok
    assert report.hosts[0].resource_rows == 4
    assert report.hosts[0].process_rows == 0
    assert "no process samples" in format_process_insights_report(report)
    assert "PE co-managed database host insights" in format_process_insights_report(report)


def test_process_insights_passes_when_process_samples_exist() -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="compartment-id",
    )

    report = ProcessInsightsService(
        FakeOci(process_rows=2, importable_cloud=1, importable_agents=1)  # type: ignore[arg-type]
    ).diagnose(config, interval="P30D")

    assert report.ok
    assert report.hosts[0].process_rows == 6
    assert report.importable_macs_cloud_hosts == 1
    assert report.importable_agent_entities == 1
