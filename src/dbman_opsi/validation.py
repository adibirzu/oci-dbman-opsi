"""Validation checks for configured enablement."""

from __future__ import annotations

from dbman_opsi.config import EnablementConfig, Target
from dbman_opsi.oci_cli import OciCli
from dbman_opsi.status import dbm_status, opsi_status


class ValidationService:
    def __init__(self, oci: OciCli) -> None:
        self.oci = oci

    def validate(self, config: EnablementConfig) -> list[str]:
        findings: list[str] = []
        for target in config.targets:
            if target.kind in {"external-db", "external-exadata"}:
                agents = self.oci.list_management_agents(target.compartment_id or config.compartment_id or "")
                matched = [agent for agent in agents if target.name.lower() in str(agent.get("display-name", "")).lower()]
                if not matched:
                    findings.append(f"{target.name}: Management Agent not found yet")
                    continue
                findings.append(f"{target.name}: Management Agent registered")
            elif not target.resource_id:
                findings.append(f"{target.name}: missing resource OCID")
            elif target.kind == "autonomous":
                details = self.oci.get_autonomous_database(target.resource_id)
                dbmgmt = dbm_status(details, target.kind) or "NOT_ENABLED"
                opsi = opsi_status(details, target.kind) or "NOT_ENABLED"
                findings.append(f"{target.name}: Database Management {dbmgmt}; Ops Insights {opsi}")
            elif target.kind in {"dbcs", "exadata"}:
                if target.database_role == "PDB":
                    details = self.oci.get_pluggable_database(target.resource_id)
                else:
                    details = self.oci.get_database(target.resource_id)
                dbmgmt = dbm_status(details, target.kind, target.database_role) or "NOT_ENABLED"
                opsi = self._opsi_insight_state(target, config)
                findings.append(
                    f"{target.name} ({target.database_role}): Database Management {dbmgmt}; "
                    f"Ops Insights {opsi}"
                )
            else:
                findings.append(f"{target.name}: validate Database Management and Ops Insights status in OCI Console/API")
        return findings

    def _opsi_insight_state(self, target: "Target", config: EnablementConfig) -> str:
        """Resolve the real OPSI Database Insight lifecycle for a DBCS/Exadata target.

        Returns e.g. ``ACTIVE (ENABLED)``, ``FAILED (ENABLED)``, or
        ``NOT_FOUND (no Database Insight)`` so a broken Ops Insights collection is
        surfaced instead of hidden behind a generic "needs validation" message.
        """

        compartment = target.compartment_id or config.compartment_id or ""
        if not compartment:
            return "UNKNOWN (no compartment in config)"
        # OCI occasionally returns a transient NotAuthorizedOrNotFound (404) on
        # list calls; retry once before degrading to UNKNOWN so a flaky control
        # plane never masquerades as "no insight".
        insights: list[dict[str, object]] | None = None
        for _ in range(2):
            try:
                insights = self.oci.list_opsi_database_insights(compartment)
                break
            except RuntimeError:
                insights = None
        if insights is None:
            return "UNKNOWN (insight query failed; verify in OCI Console)"
        for insight in insights:
            if insight.get("database-id") == target.resource_id:
                state = insight.get("lifecycle-state") or "UNKNOWN"
                status = insight.get("status") or "UNKNOWN"
                return f"{state} ({status})"
        return "NOT_FOUND (no Database Insight)"
