"""Read-only diagnostics for Ops Insights Process Insights collection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from dbman_opsi.config import EnablementConfig
from dbman_opsi.oci_cli import OciCli

PROCESS_METRICS = ("CPU", "MEMORY", "VIRTUAL_MEMORY")
RESOURCE_METRICS = ("CPU", "MEMORY", "STORAGE", "NETWORK")


@dataclass(frozen=True)
class HostProcessSummary:
    region: str
    host_name: str
    host_type: str
    lifecycle_state: str
    status: str
    resource_rows: int
    process_rows: int

    @property
    def has_process_data(self) -> bool:
        return self.process_rows > 0

    @property
    def has_resource_data(self) -> bool:
        return self.resource_rows > 0


@dataclass(frozen=True)
class ProcessInsightsReport:
    interval: str
    hosts: tuple[HostProcessSummary, ...]
    importable_macs_cloud_hosts: int
    importable_agent_entities: int

    @property
    def ok(self) -> bool:
        return any(host.has_process_data for host in self.hosts)

    def to_dict(self) -> dict[str, object]:
        return {
            "interval": self.interval,
            "ok": self.ok,
            "importable_macs_cloud_hosts": self.importable_macs_cloud_hosts,
            "importable_agent_entities": self.importable_agent_entities,
            "hosts": [host.__dict__ for host in self.hosts],
        }


class ProcessInsightsService:
    def __init__(
        self,
        oci: OciCli,
        *,
        oci_for_region: Callable[[str], OciCli] | None = None,
    ) -> None:
        self.oci = oci
        self.oci_for_region = oci_for_region or (lambda _region: self.oci)

    def diagnose(self, config: EnablementConfig, *, interval: str = "P7D") -> ProcessInsightsReport:
        compartment_id = config.compartment_id or ""
        if not compartment_id:
            return ProcessInsightsReport(
                interval=interval,
                hosts=(),
                importable_macs_cloud_hosts=0,
                importable_agent_entities=0,
            )

        regions = self._regions(config)
        hosts: list[HostProcessSummary] = []
        importable_cloud_hosts = 0
        importable_agent_entities = 0
        for region in regions:
            oci = self.oci_for_region(region)
            importable_cloud_hosts += len(_safe_list(lambda: oci.list_importable_macs_cloud_hosts(compartment_id)))
            importable_agent_entities += len(_safe_list(lambda: oci.list_importable_agent_entities(compartment_id)))
            for host in _safe_list(lambda: oci.list_opsi_host_insights(compartment_id)):
                host_id = str(host.get("id") or "")
                if not host_id:
                    continue
                hosts.append(self._summarize_host(oci, compartment_id, region, host, interval))
        return ProcessInsightsReport(
            interval=interval,
            hosts=tuple(hosts),
            importable_macs_cloud_hosts=importable_cloud_hosts,
            importable_agent_entities=importable_agent_entities,
        )

    def _summarize_host(
        self,
        oci: OciCli,
        compartment_id: str,
        region: str,
        host: dict[str, object],
        interval: str,
    ) -> HostProcessSummary:
        host_id = str(host.get("id") or "")
        resource_rows = sum(
            len(_safe_list(
                lambda metric=metric: oci.summarize_host_resource_usage(
                    compartment_id=compartment_id,
                    host_insight_id=host_id,
                    resource_metric=metric,
                    analysis_time_interval=interval,
                )
            ))
            for metric in RESOURCE_METRICS
        )
        process_rows = sum(
            len(_safe_list(
                lambda metric=metric: oci.summarize_host_top_processes(
                    compartment_id=compartment_id,
                    host_insight_id=host_id,
                    resource_metric=metric,
                    analysis_time_interval=interval,
                )
            ))
            for metric in PROCESS_METRICS
        )
        return HostProcessSummary(
            region=region,
            host_name=_host_name(host),
            host_type=str(host.get("host-type") or "UNKNOWN"),
            lifecycle_state=str(host.get("lifecycle-state") or "UNKNOWN"),
            status=str(host.get("status") or "UNKNOWN"),
            resource_rows=resource_rows,
            process_rows=process_rows,
        )

    @staticmethod
    def _regions(config: EnablementConfig) -> tuple[str, ...]:
        values = [config.region, *config.monitoring_regions, *(target.region for target in config.targets)]
        return tuple(dict.fromkeys(region for region in values if region))


def format_process_insights_report(report: ProcessInsightsReport) -> str:
    lines = [f"Process Insights interval: {report.interval}"]
    if not report.hosts:
        lines.append("- no host insights found")
    for host in report.hosts:
        verdict = "process data present" if host.has_process_data else "no process samples"
        detail = (
            "host resource rows present"
            if host.has_resource_data and not host.has_process_data
            else f"resource_rows={host.resource_rows}"
        )
        lines.append(
            f"- {host.host_name} [{host.region}] {host.host_type} "
            f"{host.lifecycle_state}/{host.status}: {verdict}; {detail}"
        )
    lines.append(f"Importable MACS cloud hosts: {report.importable_macs_cloud_hosts}")
    lines.append(f"Importable Management Agent entities: {report.importable_agent_entities}")
    if report.hosts and not report.ok:
        lines.append(
            "Next: enable a MACS cloud host or a Management Agent-backed host insight for process collection; "
            "PE co-managed database host insights can show host resource usage without top-process rows."
        )
    return "\n".join(lines)


def _safe_list(call: Callable[[], list[dict[str, object]]]) -> list[dict[str, object]]:
    try:
        return list(call() or [])
    except RuntimeError:
        return []


def _host_name(host: dict[str, object]) -> str:
    for key in ("host-name", "host-display-name", "entity-name", "display-name"):
        value = host.get(key)
        if value:
            return str(value)
    return "<unnamed>"
