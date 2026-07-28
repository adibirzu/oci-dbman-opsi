"""Database incident evidence collection and demo payload generation."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dbman_opsi.oci_cli import OciCli
from dbman_opsi.redact import redact_data, redact_text

DEFAULT_SOURCES = ("logan", "dbm", "opsi", "audit", "datasafe")
INTERNAL_ORA_CODES = {"ORA-00600", "ORA-07445"}
COMPILATION_ERROR_CODES = {"ORA-04063", "ORA-06550", "PLS-00103", "PLS-00201", "PLS-00302"}
SAFE_DEMO_ORA_CODES = ("ORA-00001", "ORA-00942", "ORA-01400", "ORA-02291", "ORA-00054", "ORA-04063", "ORA-06550", "ORA-06575")
SCENARIO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class SignalEvent:
    timestamp: str
    source: str
    severity: str
    message: str
    attributes: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "source": self.source,
            "severity": self.severity,
            "message": self.message,
            "attributes": redact_data(self.attributes),
        }


@dataclass(frozen=True)
class CorrelationEvidence:
    source: str
    status: str
    detail: str
    event_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "detail": redact_text(self.detail),
            "event_count": self.event_count,
        }


@dataclass(frozen=True)
class CorrelationCandidate:
    hypothesis: str
    confidence: str
    rationale: str

    def to_dict(self) -> dict[str, str]:
        return {
            "hypothesis": self.hypothesis,
            "confidence": self.confidence,
            "rationale": redact_text(self.rationale),
        }


@dataclass(frozen=True)
class DbIncidentRequest:
    ora_code: str
    database_name: str | None = None
    entity_name: str | None = None
    incident_time: str | None = None
    hours_back: int = 2
    window_minutes: int = 30
    profile: str | None = None
    compartment_id: str | None = None
    include_sources: tuple[str, ...] = DEFAULT_SOURCES
    limit: int = 100


@dataclass(frozen=True)
class DbIncidentEvidenceBundle:
    request: DbIncidentRequest
    source_status: tuple[CorrelationEvidence, ...]
    timeline: tuple[SignalEvent, ...]
    hypotheses: tuple[CorrelationCandidate, ...]
    recommended_questions: tuple[str, ...]
    privacy: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        repeated = _repetition_summary(self.timeline, self.request.ora_code)
        return redact_data(
            {
                "workflow": "db_incident_analysis",
                "request": {
                    "ora_code": self.request.ora_code,
                    "database_name": self.request.database_name,
                    "entity_name": self.request.entity_name,
                    "incident_time": self.request.incident_time,
                    "hours_back": self.request.hours_back,
                    "window_minutes": self.request.window_minutes,
                    "profile": self.request.profile,
                    "compartment_id": self.request.compartment_id,
                    "include_sources": list(self.request.include_sources),
                    "limit": self.request.limit,
                },
                "summary": _summary(self.request, self.timeline, repeated),
                "timeline": [event.to_dict() for event in self.timeline],
                "repetition_scope": repeated,
                "cross_source_evidence": [status.to_dict() for status in self.source_status],
                "hypotheses": [candidate.to_dict() for candidate in self.hypotheses],
                "impact": _impact(self.timeline),
                "next_diagnostics": _next_diagnostics(self.request.ora_code),
                "sr_evidence_package": _sr_package(self.request.ora_code),
                "recommended_questions": list(self.recommended_questions),
                "uncertainty": _uncertainty(self.request.ora_code, self.source_status),
                "privacy": self.privacy,
            }
        )


class DbIncidentEvidenceService:
    """Build a bounded, redacted DB incident evidence bundle from available OCI sources."""

    def __init__(self, oci: OciCli | None = None) -> None:
        self.oci = oci

    def build(self, request: DbIncidentRequest) -> DbIncidentEvidenceBundle:
        statuses: list[CorrelationEvidence] = []
        events: list[SignalEvent] = []
        if "logan" in request.include_sources:
            log_events, status = self._logan(request)
            events.extend(log_events)
            statuses.append(status)
        if "dbm" in request.include_sources:
            events.extend(self._dbm(request, statuses))
        if "opsi" in request.include_sources:
            events.extend(self._opsi(request, statuses))
        if "audit" in request.include_sources:
            events.extend(self._audit(request, statuses))
        if "datasafe" in request.include_sources:
            events.extend(self._datasafe(request, statuses))
        timeline = tuple(sorted(events, key=lambda event: event.timestamp)[: request.limit])
        return DbIncidentEvidenceBundle(
            request=request,
            source_status=tuple(statuses),
            timeline=timeline,
            hypotheses=tuple(_hypotheses(request.ora_code, timeline)),
            recommended_questions=(
                "Was the ORA event isolated or repeated across instances/services?",
                "What OCI Audit, app, host, network, DBM, OPSI, or Data Safe signals changed inside the same window?",
                "Which trace files, alert-log sections, database version, patch level, and workload context are needed for SR triage?",
            ),
            privacy={"redacted": True, "bounded": True, "synthetic_allowed": True},
        )

    def _logan(self, request: DbIncidentRequest) -> tuple[list[SignalEvent], CorrelationEvidence]:
        if self.oci is None or not request.compartment_id:
            return [], CorrelationEvidence("logan", "unavailable", "missing OCI client or compartment_id")
        try:
            namespace = self.oci.get_log_analytics_namespace(request.compartment_id)
            if not namespace:
                return [], CorrelationEvidence("logan", "unavailable", "Log Analytics namespace not found")
            query = build_logan_db_incident_query(request)
            start, end = _window(request)
            payload = self.oci.search_log_analytics(
                namespace,
                query,
                compartment_id=request.compartment_id,
                time_start=start.isoformat(),
                time_end=end.isoformat(),
                limit=request.limit,
            )
            events = _logan_events(payload, request.limit)
            return events, CorrelationEvidence("logan", "ok", "queried Log Analytics for DB incident window", len(events))
        except Exception as exc:  # noqa: BLE001 - source availability belongs in evidence bundle
            return [], CorrelationEvidence("logan", "unavailable", str(exc))

    def _dbm(self, request: DbIncidentRequest, statuses: list[CorrelationEvidence]) -> list[SignalEvent]:
        if self.oci is None or not request.compartment_id:
            statuses.append(CorrelationEvidence("dbm", "unavailable", "missing OCI client or compartment_id"))
            return []
        try:
            managed = self.oci.list_managed_databases(request.compartment_id)
            matched = _match_named(managed, request.database_name or request.entity_name)
            statuses.append(CorrelationEvidence("dbm", "ok", "listed managed databases", len(matched)))
            return [_resource_event("dbm", item, "Managed database context") for item in matched[: request.limit]]
        except Exception as exc:  # noqa: BLE001
            statuses.append(CorrelationEvidence("dbm", "unavailable", str(exc)))
            return []

    def _opsi(self, request: DbIncidentRequest, statuses: list[CorrelationEvidence]) -> list[SignalEvent]:
        if self.oci is None or not request.compartment_id:
            statuses.append(CorrelationEvidence("opsi", "unavailable", "missing OCI client or compartment_id"))
            return []
        try:
            insights = self.oci.list_opsi_database_insights(request.compartment_id)
            matched = _match_named(insights, request.database_name or request.entity_name)
            statuses.append(CorrelationEvidence("opsi", "ok", "listed database insights", len(matched)))
            return [_resource_event("opsi", item, "Ops Insights database context") for item in matched[: request.limit]]
        except Exception as exc:  # noqa: BLE001
            statuses.append(CorrelationEvidence("opsi", "unavailable", str(exc)))
            return []

    def _datasafe(self, request: DbIncidentRequest, statuses: list[CorrelationEvidence]) -> list[SignalEvent]:
        if self.oci is None or not request.compartment_id or not hasattr(self.oci, "list_data_safe_targets"):
            statuses.append(CorrelationEvidence("datasafe", "unavailable", "Data Safe target listing is not available"))
            return []
        try:
            targets = self.oci.list_data_safe_targets(request.compartment_id)
            matched = _match_named(targets, request.database_name or request.entity_name)
            events = [_resource_event("datasafe", item, "Data Safe target context") for item in matched[: request.limit]]
            audit_count = 0
            status = "ok"
            detail = "listed Data Safe targets"
            if hasattr(self.oci, "list_data_safe_audit_events"):
                start, end = _window(request)
                audit_events = self.oci.list_data_safe_audit_events(
                    request.compartment_id,
                    start.isoformat().replace("+00:00", "Z"),
                    end.isoformat().replace("+00:00", "Z"),
                )
                matched_audit = _match_named(audit_events, request.database_name or request.entity_name)
                audit_count = len(matched_audit[: request.limit])
                events.extend(_datasafe_audit_event(item) for item in matched_audit[: request.limit])
                detail = "listed Data Safe targets and audit events"
            statuses.append(CorrelationEvidence("datasafe", status, detail, len(matched) + audit_count))
            return events
        except Exception as exc:  # noqa: BLE001
            statuses.append(CorrelationEvidence("datasafe", "unavailable", str(exc)))
            return []

    def _audit(self, request: DbIncidentRequest, statuses: list[CorrelationEvidence]) -> list[SignalEvent]:
        if self.oci is None or not request.compartment_id or not hasattr(self.oci, "list_audit_events"):
            statuses.append(CorrelationEvidence("audit", "unavailable", "OCI Audit listing is not available"))
            return []
        try:
            start, end = _window(request)
            audit_events = self.oci.list_audit_events(
                request.compartment_id,
                start.isoformat().replace("+00:00", "Z"),
                end.isoformat().replace("+00:00", "Z"),
            )
            statuses.append(CorrelationEvidence("audit", "ok", "listed OCI Audit events for incident window", len(audit_events)))
            return [_audit_event(item) for item in audit_events[: request.limit]]
        except Exception as exc:  # noqa: BLE001
            statuses.append(CorrelationEvidence("audit", "unavailable", str(exc)))
            return []


def build_logan_db_incident_query(request: DbIncidentRequest) -> str:
    """Build a conservative Log Analytics query string with escaped literals."""

    terms = [_ocl_literal(request.ora_code)]
    if request.database_name:
        terms.append(_ocl_literal(request.database_name))
    if request.entity_name:
        terms.append(_ocl_literal(request.entity_name))
    search_terms = " ".join(terms)
    return (
        f"{search_terms} | sort -Time | head {max(1, min(request.limit, 500))}"
    )


def generate_db_incident_demo(output_dir: str | Path, *, apply: bool = False, scenario_id: str = "ora00600-demo") -> list[Path]:
    if SCENARIO_ID_RE.fullmatch(scenario_id) is None:
        raise ValueError("scenario_id must be 1-64 ASCII letters, digits, dots, underscores, or hyphens")
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    lab_id = f"lab-{scenario_id}"
    paths = [
        _write(destination / "README.md", _demo_readme(apply)),
        _write(destination / "run-db-incident-demo.sh", _demo_runner_script(apply)),
        _write(destination / "01-create-lab-schema.sql", _demo_schema_sql(apply)),
        _write(destination / "02-generate-safe-errors.sql", _demo_errors_sql(apply, scenario_id, lab_id)),
        _write(destination / "03-query-evidence.sql", _demo_query_sql(apply)),
        _write(destination / "04-optional-alertlog-marker-sysdba.sql", _demo_alertlog_sql(apply, scenario_id, lab_id)),
        _write(destination / "05-cleanup-lab-schema.sql", _demo_cleanup_sql(apply)),
        _write(destination / "06-install-oracle-sample-schemas.sh", _demo_sample_schema_installer(apply)),
        _write(destination / "07-generate-sample-schema-errors.sql", _demo_sample_schema_errors_sql(apply)),
        _write(destination / "08-local-demo-tooling-preflight.sh", _demo_tooling_preflight_script()),
        _write(destination / "09-db-troubleshooting-queries.sql", _demo_troubleshooting_queries_sql()),
        _write(destination / "10-enable-datasafe-demo-audit.sql", _demo_datasafe_audit_sql(apply)),
        _write(destination / "11-verify-datasafe-demo-audit.sql", _demo_verify_datasafe_audit_sql(apply)),
        _write(destination / "12-check-monitoring-account-status.sql", _demo_monitoring_account_status_sql(apply)),
        _write(destination / "13-remediate-monitoring-account-lock.sql", _demo_monitoring_account_recovery_sql(apply)),
        _write(destination / "MCP-HANDOFF.md", _demo_mcp_handoff()),
        _write(destination / "oci-coordinator-oke-integration" / "README.md", _coordinator_integration_readme()),
        _write(destination / "oci-coordinator-oke-integration" / "db-incident-logan-dashboard.json", _coordinator_logan_dashboard()),
        _write(destination / "oci-coordinator-oke-integration" / "db-incident-agent-drilldowns.json", _coordinator_agent_drilldowns()),
        _write(destination / "oci-coordinator-oke-integration" / "db-incident-playbook.yaml", _coordinator_playbook_yaml()),
        _write(destination / "oci-coordinator-oke-integration" / "queries" / "db_incident_ora_error_timeline.json", _coordinator_detection_query_ora_timeline()),
        _write(destination / "oci-coordinator-oke-integration" / "queries" / "db_incident_compilation_errors.json", _coordinator_detection_query_compile()),
        _write(destination / "oci-coordinator-oke-integration" / "queries" / "db_incident_cross_source_correlation.json", _coordinator_detection_query_cross_source()),
        _write(destination / "DEMO-SEGREGATION.md", _demo_segregation_readme()),
        _write(destination / "LOGAN-QUERIES.md", _demo_logan_queries(scenario_id, lab_id)),
        _write(destination / "RUNBOOK.md", _demo_runbook()),
        _write(destination / "manifest.json", _demo_manifest(scenario_id, lab_id)),
        _write(destination / "observability-demo-targets.yaml", _demo_observability_targets_yaml()),
        _write(destination / "synthetic-db-incident.jsonl", _demo_jsonl(scenario_id, lab_id)),
        _write(destination / "validate-demo-packet.sh", _demo_validator_script()),
        _write(destination / "upload-logan.sh", _demo_upload_script()),
    ]
    os.chmod(destination / "run-db-incident-demo.sh", 0o700)
    os.chmod(destination / "06-install-oracle-sample-schemas.sh", 0o700)
    os.chmod(destination / "08-local-demo-tooling-preflight.sh", 0o700)
    os.chmod(destination / "validate-demo-packet.sh", 0o700)
    os.chmod(destination / "upload-logan.sh", 0o700)
    return paths


def oci_logan_build_db_incident_evidence(
    *,
    ora_code: str,
    database_name: str | None = None,
    entity_name: str | None = None,
    incident_time: str | None = None,
    hours_back: int = 2,
    window_minutes: int = 30,
    profile: str | None = None,
    compartment_id: str | None = None,
    include_sources: tuple[str, ...] = DEFAULT_SOURCES,
    limit: int = 100,
    oci: OciCli | None = None,
) -> dict[str, Any]:
    """MCP-shaped entry point returning bounded DB incident evidence JSON."""

    request = DbIncidentRequest(
        ora_code=ora_code.upper(),
        database_name=database_name,
        entity_name=entity_name,
        incident_time=incident_time,
        hours_back=hours_back,
        window_minutes=window_minutes,
        profile=profile,
        compartment_id=compartment_id,
        include_sources=include_sources,
        limit=limit,
    )
    return DbIncidentEvidenceService(oci).build(request).to_dict()


def _ocl_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _window(request: DbIncidentRequest) -> tuple[datetime, datetime]:
    if request.incident_time:
        center = datetime.fromisoformat(request.incident_time.replace("Z", "+00:00"))
        if center.tzinfo is None:
            center = center.replace(tzinfo=UTC)
        delta = timedelta(minutes=max(1, request.window_minutes))
        return center - delta, center + delta
    end = datetime.now(UTC)
    return end - timedelta(hours=max(1, request.hours_back)), end


def _logan_events(payload: dict[str, Any], limit: int) -> list[SignalEvent]:
    rows = payload.get("items") or payload.get("results") or payload.get("rows") or []
    if isinstance(rows, dict):
        rows = rows.get("items") or []
    events = []
    for row in rows[:limit] if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        events.append(
            SignalEvent(
                timestamp=str(row.get("time") or row.get("Time") or row.get("timestamp") or ""),
                source=str(row.get("source") or row.get("Source") or "logan"),
                severity=str(row.get("severity") or row.get("Severity") or "info"),
                message=redact_text(str(row.get("message") or row.get("Log Content") or row)),
                attributes={key: value for key, value in row.items() if key not in {"message", "Log Content"}},
            )
        )
    return events


def _match_named(items: list[dict[str, Any]], name: str | None) -> list[dict[str, Any]]:
    if not name:
        return items
    needle = name.lower()
    return [
        item
        for item in items
        if needle in str(
            item.get("name")
            or item.get("display-name")
            or item.get("database-name")
            or item.get("target-name")
            or item.get("targetName")
            or item.get("db-name")
            or ""
        ).lower()
    ]


def _resource_event(source: str, item: dict[str, Any], message: str) -> SignalEvent:
    return SignalEvent(
        timestamp=str(item.get("time-updated") or item.get("time-created") or ""),
        source=source,
        severity=str(item.get("lifecycle-state") or item.get("status") or "info"),
        message=message,
        attributes=item,
    )


def _audit_event(item: dict[str, Any]) -> SignalEvent:
    event_type = str(item.get("event-type") or item.get("eventType") or "OCI Audit event")
    principal = item.get("principal-name") or item.get("principalName") or item.get("user-name") or item.get("userName")
    message = event_type if not principal else f"{event_type} by {principal}"
    return SignalEvent(
        timestamp=str(item.get("event-time") or item.get("eventTime") or item.get("time") or ""),
        source="audit",
        severity="info",
        message=message,
        attributes=item,
    )


def _datasafe_audit_event(item: dict[str, Any]) -> SignalEvent:
    event_name = str(item.get("event-name") or item.get("eventName") or item.get("operation") or "Data Safe audit event")
    target_name = item.get("target-name") or item.get("targetName")
    db_user = item.get("db-user-name") or item.get("dbUserName")
    status = str(item.get("operation-status") or item.get("operationStatus") or "info")
    actor = db_user or target_name
    message = event_name if not actor else f"{event_name} on {target_name or 'target'} by {actor}"
    return SignalEvent(
        timestamp=str(item.get("audit-event-time") or item.get("auditEventTime") or item.get("time-collected") or ""),
        source="datasafe",
        severity=status.lower(),
        message=message,
        attributes=item,
    )


def _repetition_summary(events: tuple[SignalEvent, ...], ora_code: str) -> dict[str, Any]:
    matching = [event for event in events if ora_code in event.message or ora_code in json.dumps(event.attributes)]
    sources = sorted({event.source for event in matching})
    return {"matching_events": len(matching), "repeated": len(matching) > 1, "sources": sources}


def _summary(request: DbIncidentRequest, events: tuple[SignalEvent, ...], repeated: dict[str, Any]) -> str:
    matching = [
        event
        for event in events
        if request.ora_code in event.message or request.ora_code in json.dumps(event.attributes)
    ]
    if not matching:
        if events:
            return (
                f"No direct {request.ora_code} log events were found; "
                f"{len(events)} contextual DB/OCI signals were collected for correlation."
            )
        return f"No bounded evidence was available for {request.ora_code}; source status explains missing coverage."
    first = matching[0].timestamp or "unknown start"
    last = matching[-1].timestamp or "unknown end"
    scope = "repeated" if repeated["repeated"] else "isolated in collected evidence"
    return f"{request.ora_code} evidence spans {first} to {last}; the error appears {scope}."


def _hypotheses(ora_code: str, events: tuple[SignalEvent, ...]) -> list[CorrelationCandidate]:
    matching = [event for event in events if ora_code in event.message or ora_code in json.dumps(event.attributes)]
    if ora_code in INTERNAL_ORA_CODES:
        return [
            CorrelationCandidate(
                "Internal database error requiring Oracle trace/version/SR context",
                "medium" if matching else "low",
                f"{ora_code} is not enough to identify a definitive root cause without trace files, exact version, patch level, and workload context.",
            )
        ]
    if ora_code == "ORA-00060":
        return [CorrelationCandidate("Deadlock between sessions or application transactions", "medium", "Deadlocks are real workload errors; collect deadlock graph and blocking SQL.")]
    if ora_code == "ORA-04031":
        return [CorrelationCandidate("Shared pool or memory pressure", "medium", "Correlate with workload changes, SGA settings, and memory advisories.")]
    if ora_code == "ORA-01017":
        return [CorrelationCandidate("Authentication failure or credential drift", "medium", "Check recent credential, wallet, secret, or account changes.")]
    if ora_code in COMPILATION_ERROR_CODES:
        return [
            CorrelationCandidate(
                "Invalid or newly compiled PL/SQL object",
                "medium" if matching else "low",
                "Correlate DBA_ERRORS/USER_ERRORS, object status, deployment timestamp, and caller module/action before treating this as an application outage.",
            )
        ]
    return [CorrelationCandidate("Application or data integrity error", "low", "Collected evidence should be correlated with app deploys, SQL text, and DB alert/audit context.")]


def _impact(events: tuple[SignalEvent, ...]) -> str:
    if any(event.severity.upper() in {"ERROR", "CRITICAL", "FATAL"} for event in events):
        return "Potential user or workload impact; verify failed sessions, application errors, and DB availability metrics."
    return "Impact is unknown from collected evidence; verify session failures, app error rate, and affected services."


def _next_diagnostics(ora_code: str) -> list[str]:
    diagnostics = [
        "Collect alert-log excerpts for the incident window and 30 minutes before it.",
        "Correlate OCI Audit, host, app, VCN/network, DBM, OPSI, and Data Safe signals in the same window.",
    ]
    if ora_code in INTERNAL_ORA_CODES:
        diagnostics.append("Collect incident trace files, database version, RU/RUR patch level, SQL/workload context, and open an Oracle SR if reproducible or service-impacting.")
    if ora_code in COMPILATION_ERROR_CODES:
        diagnostics.extend(
            [
                "Run SHOW ERRORS immediately after compilation when reproducing in SQL*Plus or SQLcl.",
                "Query DBA_ERRORS or USER_ERRORS for owner/name/type/line/position/text/sequence and compare with recent deploy changes.",
                "Check DBA_OBJECTS or USER_OBJECTS object status and dependent objects before recompiling or rolling back.",
            ]
        )
    return diagnostics


def _sr_package(ora_code: str) -> list[str]:
    package = ["alert.log window", "incident timestamp/timezone", "database name/service/instance", "recent changes", "impact statement"]
    if ora_code in INTERNAL_ORA_CODES:
        package.extend(["incident trace files", "ips package if available", "exact database version and patch level"])
    if ora_code in COMPILATION_ERROR_CODES:
        package.extend(["DBA_ERRORS or USER_ERRORS rows", "object DDL/source excerpt", "deployment change record", "dependent object status"])
    return package


def _uncertainty(ora_code: str, statuses: tuple[CorrelationEvidence, ...]) -> str:
    missing = [status.source for status in statuses if status.status != "ok"]
    prefix = f"{ora_code} is an internal error signature, not a root cause by itself. " if ora_code in INTERNAL_ORA_CODES else ""
    if missing:
        return prefix + f"Confidence is limited because these sources were unavailable or incomplete: {', '.join(missing)}."
    return prefix + "Confidence depends on whether the collected window covers the full incident and preceding change period."


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _demo_readme(apply: bool) -> str:
    mode = "apply" if apply else "dry-run"
    return f"""# DB Incident Demo

Mode: {mode}

This package creates a disposable lab schema only when `--apply` was used during generation
and `run-db-incident-demo.sh` is executed with reviewed SQL*Plus connection strings.

The workload creates real, safe Oracle errors (`ORA-00001`, `ORA-00942`, `ORA-01400`,
`ORA-02291`, `ORA-00054`, `ORA-04063`, `ORA-06550`, and PLS compiler diagnostics)
and persists evidence rows in `DBINC_LAB.incident_event_log` so the
troubleshooting flow can query real database state. `04-optional-alertlog-marker-sysdba.sql`
can write reviewed marker lines to the database alert log through `DBMS_SYSTEM.KSDWRT` when
you provide a SYSDBA connection. It does not force internal errors.

Optional Oracle sample schemas:

- Set `DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED=true` to install Oracle's official sample schemas
  into the same demo database after the base lab has run.
- The generated installer downloads `oracle-samples/db-sample-schemas` at runtime and installs
  HR and CO using their upstream install scripts.
- `07-generate-sample-schema-errors.sql` then produces additional safe real errors against HR
  and CO tables and logs the observations back into `DBINC_LAB.incident_event_log`.

ORA-00600/ORA-07445 Log Analytics demo records remain synthetic and are marked with
`synthetic=true`, `scenario_id`, and `lab_id`.

Log upload is gated by `DB_INCIDENT_LOG_UPLOAD_ENABLED=true`.
Use `LOGAN-QUERIES.md` for scenario-scoped Log Analytics searches during the demo.
Use `manifest.json` as the machine-readable packet index for automation and handoff checks.
Use `08-local-demo-tooling-preflight.sh` before demo time to verify Java, OCI CLI, SQLcl, and the configured MCP server command.
Use `09-db-troubleshooting-queries.sql`, `10-enable-datasafe-demo-audit.sql`, `11-verify-datasafe-demo-audit.sql`, and `MCP-HANDOFF.md` for read-only DBA or MCP-agent evidence collection.

Required runtime environment for the runner:

- `DB_INCIDENT_ADMIN_CONNECT`: SQL*Plus connect string for a DBA user that can create/drop the lab schema.
- `DB_INCIDENT_LAB_PASSWORD`: password assigned to the disposable `DBINC_LAB` schema.
- `DB_INCIDENT_PDB_NAME`: optional pluggable database name to switch into before lab-user and sample-schema setup.
- `DB_INCIDENT_PDB_SERVICE`: optional service name for the lab-user connection; defaults to lowercase `DB_INCIDENT_PDB_NAME`.
- `DB_INCIDENT_LAB_EZCONNECT`: optional Easy Connect target such as `//db-host:1521/demo_pdb_service`; used with quoted `DBINC_LAB` credentials.
- `DB_INCIDENT_LAB_CONNECT`: optional SQL*Plus connect string for `DBINC_LAB`; defaults to `DBINC_LAB/$DB_INCIDENT_LAB_PASSWORD[@$DB_INCIDENT_PDB_SERVICE]`.
- `DB_INCIDENT_SYSDBA_CONNECT`: optional SQL*Plus SYSDBA connection for alert-log marker lines.
- `DB_INCIDENT_DATASAFE_AUDIT_ENABLED`: optional gate to create a demo-only unified-audit policy for `DBINC_LAB` logon activity and verify recent audit rows.
- `DB_INCIDENT_DATASAFE_AUDIT_FAILED_LOGIN_ENABLED`: optional gate to produce a reviewed failed-login audit signal with a deliberately wrong password.
- `DB_INCIDENT_DATASAFE_AUDIT_LOOKBACK_MINUTES`: optional lookback window for the local unified-audit verification query; defaults to `120`.
- `DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED`: optional gate for HR/CO sample-schema installation and workload.
- `DB_INCIDENT_SAMPLE_SCHEMA_PASSWORD`: optional password for HR and CO; defaults to `DB_INCIDENT_LAB_PASSWORD`.
- `DB_INCIDENT_SAMPLE_SCHEMA_TABLESPACE`: optional tablespace prompt answer; defaults to `USERS`.
- `DB_INCIDENT_TOOLING_INSTALL`: optional gate to install checksum-verified SQLcl into the packet-local `.tools` directory.
- `DB_INCIDENT_SQLCL_ARCHIVE`: local SQLcl ZIP archive to copy into the packet; set this or `DB_INCIDENT_SQLCL_URL`, not both.
- `DB_INCIDENT_SQLCL_URL`: reviewed HTTPS SQLcl ZIP URL; never defaults to a moving `latest` URL.
- `DB_INCIDENT_SQLCL_SHA256`: required SHA-256 for the selected SQLcl archive or URL.
- `DB_INCIDENT_MCP_COMMAND`: optional reviewed Jeff Smith/SQLcl MCP server launch command for local MCP host checks.
- `NO_COLOR`: disable ANSI colors in generated shell output.
"""


def _shell_style_helpers() -> str:
    return r"""if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET="$(printf '\033[0m')"
  C_BOLD="$(printf '\033[1m')"
  C_BLUE="$(printf '\033[34m')"
  C_GREEN="$(printf '\033[32m')"
  C_YELLOW="$(printf '\033[33m')"
else
  C_RESET=""
  C_BOLD=""
  C_BLUE=""
  C_GREEN=""
  C_YELLOW=""
fi

banner() { printf '\n%s%s%s\n' "$C_BOLD" "$1" "$C_RESET"; }
step() { printf '%s==>%s %s\n' "$C_BLUE" "$C_RESET" "$1"; }
ok() { printf '%sOK%s %s\n' "$C_GREEN" "$C_RESET" "$1"; }
warn() { printf '%sWARN%s %s\n' "$C_YELLOW" "$C_RESET" "$1"; }
fail() { printf '%sFAIL%s %s\n' "$C_YELLOW" "$C_RESET" "$1" >&2; }
info() { printf '    %s\n' "$1"; }"""


def _demo_runner_script(apply: bool) -> str:
    styles = _shell_style_helpers()
    if not apply:
        return """#!/usr/bin/env bash
set -euo pipefail

""" + styles + """

warn "Dry run. Regenerate with --apply to create executable SQL*Plus demo scripts."
"""
    return """#!/usr/bin/env bash
set -euo pipefail

""" + styles + """

: "${DB_INCIDENT_ADMIN_CONNECT:?Set DB_INCIDENT_ADMIN_CONNECT to a reviewed SQL*Plus DBA connect string.}"
: "${DB_INCIDENT_LAB_PASSWORD:?Set DB_INCIDENT_LAB_PASSWORD for the disposable DBINC_LAB schema.}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PDB_NAME="${DB_INCIDENT_PDB_NAME:-}"
PDB_SERVICE="${DB_INCIDENT_PDB_SERVICE:-}"
LAB_EZCONNECT="${DB_INCIDENT_LAB_EZCONNECT:-}"

if [ -n "${DB_INCIDENT_SQL_BIN:-}" ]; then
  SQL_CLIENT=("$DB_INCIDENT_SQL_BIN" -S)
elif command -v sqlplus >/dev/null 2>&1; then
  SQL_CLIENT=(sqlplus -L -S)
elif [ -x "$SCRIPT_DIR/.tools/sqlcl/bin/sql" ]; then
  SQL_CLIENT=("$SCRIPT_DIR/.tools/sqlcl/bin/sql" -S)
  SQL_CLIENT_KIND="sqlcl"
elif command -v sql >/dev/null 2>&1; then
  SQL_CLIENT=(sql -S)
  SQL_CLIENT_KIND="sqlcl"
else
  fail "SQL*Plus or SQLcl is required; run 08-local-demo-tooling-preflight.sh first"
fi

SQL_CLIENT_KIND="${SQL_CLIENT_KIND:-sqlplus}"

java_major() {
  "$1" -version 2>&1 | awk -F '"' '/version/ { split($2, parts, "."); print (parts[1] == "1" ? parts[2] : parts[1]); exit }'
}

select_sqlcl_java() {
  [ "$SQL_CLIENT_KIND" = "sqlcl" ] || return 0
  for candidate in \
    "${DB_INCIDENT_JAVA_HOME:-}/bin/java" \
    "${JAVA_HOME:-}/bin/java" \
    /usr/java/latest/bin/java \
    /usr/java/default/bin/java \
    /usr/lib/jvm/*/bin/java; do
    [ -x "$candidate" ] || continue
    major="$(java_major "$candidate")"
    case "$major" in
      ''|*[!0-9]*) continue ;;
    esac
    if [ "$major" -ge 11 ]; then
      export JAVA_HOME="$(cd "$(dirname "$candidate")/.." && pwd)"
      export PATH="$JAVA_HOME/bin:$PATH"
      info "Using Java $major for SQLcl"
      return 0
    fi
  done
  fail "SQLcl requires Java 11+. Set DB_INCIDENT_JAVA_HOME to an approved JDK or install SQL*Plus."
  exit 2
}

select_sqlcl_java
run_sql() { "${SQL_CLIENT[@]}" /nolog; }

if [ -z "$PDB_SERVICE" ] && [ -n "$PDB_NAME" ]; then
  PDB_SERVICE="$(printf '%s' "$PDB_NAME" | tr '[:upper:]' '[:lower:]')"
fi

if [ -z "$LAB_EZCONNECT" ] && [ -n "$PDB_SERVICE" ]; then
  LAB_EZCONNECT="$PDB_SERVICE"
fi

LAB_CONNECT="${DB_INCIDENT_LAB_CONNECT:-DBINC_LAB/\"${DB_INCIDENT_LAB_PASSWORD}\"${LAB_EZCONNECT:+@${LAB_EZCONNECT}}}"

banner "OCI DB Incident Observability Demo"
info "This demo is not for production use."
info "Artifacts: ${SCRIPT_DIR}"

step "Creating disposable DBINC_LAB schema"
run_sql <<SQL
whenever oserror exit 1
whenever sqlerror exit sql.sqlcode
connect ${DB_INCIDENT_ADMIN_CONNECT}
@"${SCRIPT_DIR}/01-create-lab-schema.sql" "${DB_INCIDENT_LAB_PASSWORD}" "${PDB_NAME}"
exit
SQL
ok "Lab schema ready"

step "Generating safe real Oracle errors and evidence rows"
run_sql <<SQL
whenever oserror exit 1
whenever sqlerror exit sql.sqlcode
connect ${LAB_CONNECT}
@"${SCRIPT_DIR}/02-generate-safe-errors.sql"
exit
SQL
ok "Base incident workload complete"

if [ "${DB_INCIDENT_DATASAFE_AUDIT_ENABLED:-false}" = "true" ]; then
  step "Configuring demo-only Data Safe audit policy for DBINC_LAB"
  run_sql <<SQL
whenever oserror exit 1
whenever sqlerror exit sql.sqlcode
connect ${DB_INCIDENT_ADMIN_CONNECT}
@"${SCRIPT_DIR}/10-enable-datasafe-demo-audit.sql" "${PDB_NAME}"
exit
SQL
  ok "Data Safe audit policy configured"

  step "Generating reviewed Data Safe audit activity"
  run_sql <<SQL
whenever oserror exit 1
whenever sqlerror exit sql.sqlcode
connect ${LAB_CONNECT}
select 'DBINC DEMO AUDIT PRIMER' as audit_primer from dual;
exit
SQL
  ok "Generated successful login/logout audit signal"

  if [ "${DB_INCIDENT_DATASAFE_AUDIT_FAILED_LOGIN_ENABLED:-true}" = "true" ]; then
    if run_sql <<SQL >/dev/null 2>&1; then
whenever oserror exit 1
whenever sqlerror exit sql.sqlcode
connect DBINC_LAB/"${DB_INCIDENT_LAB_PASSWORD}__wrong"${LAB_EZCONNECT:+@${LAB_EZCONNECT}}
exit
SQL
      warn "Failed-login audit primer unexpectedly succeeded"
    else
      ok "Generated reviewed failed-login audit signal"
    fi
  fi

  step "Verifying recent unified audit rows for DBINC_LAB"
  run_sql <<SQL
whenever oserror exit 1
whenever sqlerror exit sql.sqlcode
connect ${DB_INCIDENT_ADMIN_CONNECT}
@"${SCRIPT_DIR}/11-verify-datasafe-demo-audit.sql" "${PDB_NAME}" "${DB_INCIDENT_DATASAFE_AUDIT_LOOKBACK_MINUTES:-120}"
exit
SQL
else
  warn "Skipping Data Safe audit primer; DB_INCIDENT_DATASAFE_AUDIT_ENABLED is not true."
fi

step "Querying captured DBINC_LAB.incident_event_log evidence"
run_sql <<SQL
whenever oserror exit 1
whenever sqlerror exit sql.sqlcode
connect ${LAB_CONNECT}
@"${SCRIPT_DIR}/03-query-evidence.sql"
exit
SQL

if [ -n "${DB_INCIDENT_SYSDBA_CONNECT:-}" ]; then
  step "Writing optional synthetic ORA-00600/ORA-07445 alert-log markers"
  run_sql <<SQL
whenever oserror exit 1
whenever sqlerror exit sql.sqlcode
connect ${DB_INCIDENT_SYSDBA_CONNECT}
@"${SCRIPT_DIR}/04-optional-alertlog-marker-sysdba.sql"
exit
SQL
  ok "Alert-log markers written"
else
  warn "Skipping alert-log marker SQL; DB_INCIDENT_SYSDBA_CONNECT is not set."
fi

if [ "${DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED:-false}" = "true" ]; then
  step "Installing Oracle HR/CO sample schemas for demo workload"
  "${SCRIPT_DIR}/06-install-oracle-sample-schemas.sh"
  step "Generating HR/CO sample-schema errors"
  run_sql <<SQL
whenever oserror exit 1
whenever sqlerror exit sql.sqlcode
connect ${LAB_CONNECT}
@"${SCRIPT_DIR}/07-generate-sample-schema-errors.sql"
exit
SQL
  ok "Oracle sample-schema workload complete"
else
  warn "Skipping Oracle sample schemas; DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED is not true."
fi

ok "Demo workload finished"
printf 'Cleanup is manual: use SQL*Plus or SQLcl, connect with DB_INCIDENT_ADMIN_CONNECT, then run @"%s/05-cleanup-lab-schema.sql" "%s"\\n' "$SCRIPT_DIR" "$PDB_NAME"
"""


def _demo_schema_sql(apply: bool) -> str:
    if not apply:
        return "-- Dry run. Re-run generator with --apply before using this as an execution plan.\n"
    return """set echo on
set serveroutput on
whenever sqlerror exit sql.sqlcode rollback

prompt
prompt ============================================================
prompt DBINC DEMO: disposable lab schema setup
prompt ============================================================

define LAB_USER = DBINC_LAB
define LAB_PASSWORD = "&1"
define PDB_NAME = "&2"

declare
  l_pdb_name varchar2(128) := trim('&&PDB_NAME');
begin
  if l_pdb_name is not null then
    execute immediate 'alter session set container = ' || dbms_assert.simple_sql_name(l_pdb_name);
  end if;
end;
/

declare
  user_count number;
begin
  select count(*) into user_count from dba_users where username = '&&LAB_USER';
  if user_count > 0 then
    execute immediate 'drop user &&LAB_USER cascade';
  end if;
end;
/

create user &&LAB_USER identified by "&&LAB_PASSWORD";
grant create session, create table, create procedure, create sequence to DBINC_LAB;
alter user DBINC_LAB quota 20M on users;

prompt Created disposable DB incident lab schema &&LAB_USER
"""


def _demo_errors_sql(apply: bool, scenario_id: str, lab_id: str) -> str:
    if not apply:
        return "-- Dry run. Safe demo errors would include ORA-00001, ORA-00942, ORA-01400, ORA-02291, ORA-00054, ORA-04063, ORA-06550, and PLS compiler diagnostics.\n"
    return f"""set echo on
set serveroutput on
whenever sqlerror continue

prompt
prompt ============================================================
prompt DBINC DEMO: base workload and safe real ORA errors
prompt ============================================================

define SCENARIO_ID = {scenario_id}
define LAB_ID = {lab_id}

begin
  execute immediate 'drop table child_demo purge';
exception when others then
  if sqlcode != -942 then raise; end if;
end;
/
begin
  execute immediate 'drop table parent_demo purge';
exception when others then
  if sqlcode != -942 then raise; end if;
end;
/
begin
  execute immediate 'drop table incident_event_log purge';
exception when others then
  if sqlcode != -942 then raise; end if;
end;
/

create table incident_event_log (
  id number generated always as identity primary key,
  event_time timestamp default systimestamp not null,
  scenario_id varchar2(128) not null,
  lab_id varchar2(128) not null,
  source varchar2(64) not null,
  severity varchar2(16) not null,
  ora_code varchar2(16),
  module_name varchar2(64),
  action_name varchar2(64),
  client_identifier varchar2(128),
  session_user varchar2(128),
  message varchar2(1000) not null,
  synthetic varchar2(5) default 'false' not null
);

prompt Created incident_event_log evidence table

create table parent_demo (
  id number primary key,
  payload varchar2(100) not null
);

create table child_demo (
  id number primary key,
  parent_id number not null references parent_demo(id),
  payload varchar2(100) not null
);

prompt Created parent_demo and child_demo tables

create or replace procedure log_event(
  p_source varchar2,
  p_severity varchar2,
  p_ora_code varchar2,
  p_message varchar2,
  p_synthetic varchar2 default 'false'
) as
  pragma autonomous_transaction;
  l_module varchar2(64);
  l_action varchar2(64);
begin
  dbms_application_info.read_module(l_module, l_action);
  insert into incident_event_log (
    scenario_id, lab_id, source, severity, ora_code,
    module_name, action_name, client_identifier, session_user,
    message, synthetic
  ) values (
    '&&SCENARIO_ID', '&&LAB_ID', p_source, p_severity, p_ora_code,
    l_module, l_action, sys_context('USERENV', 'CLIENT_IDENTIFIER'), sys_context('USERENV', 'SESSION_USER'),
    p_message, p_synthetic
  );
  commit;
end;
/

create or replace procedure attempt_parent_lock_nowait as
  pragma autonomous_transaction;
begin
  execute immediate 'lock table parent_demo in exclusive mode nowait';
  log_event('db_lock', 'WARN', null, 'Autonomous NOWAIT lock unexpectedly succeeded');
  rollback;
exception
  when others then
    log_event('db_error', 'ERROR', regexp_substr(sqlerrm, 'ORA-[0-9]{{5}}'), 'Autonomous NOWAIT lock conflict: ' || sqlerrm);
    rollback;
end;
/

begin
  dbms_session.set_identifier('&&SCENARIO_ID:&&LAB_ID');
  dbms_application_info.set_module('DBINC_DEMO', 'seed parent row');
  insert into parent_demo values (1, 'parent-one');
  log_event('db_workload', 'INFO', null, 'Seeded parent row for DB incident demo');
  commit;
end;
/

prompt Seeded workload data

declare
  procedure capture_expected_error(p_label varchar2, p_sql varchar2) is
  begin
    dbms_application_info.set_action(p_label);
    execute immediate p_sql;
    log_event('db_workload', 'WARN', null, p_label || ' unexpectedly succeeded');
  exception
    when others then
      log_event('db_error', 'ERROR', regexp_substr(sqlerrm, 'ORA-[0-9]{{5}}'), p_label || ': ' || sqlerrm);
  end;
begin
  dbms_session.set_identifier('&&SCENARIO_ID:&&LAB_ID');
  dbms_application_info.set_module('DBINC_DEMO', 'safe real ORA errors');
  capture_expected_error('duplicate parent primary key', q'[insert into parent_demo values (1, 'duplicate-parent')]');
  capture_expected_error('null child parent id', q'[insert into child_demo values (10, null, 'null-parent')]');
  capture_expected_error('missing parent foreign key', q'[insert into child_demo values (11, 999, 'missing-parent')]');
  capture_expected_error('missing application table', q'[select count(*) from dbinc_missing_orders]');
end;
/

prompt Captured safe real constraint errors

create or replace procedure broken_compile_demo as
begin
  dbinc_missing_package.run_checkout;
end;
/

prompt SHOW ERRORS output for intentionally invalid demo object
show errors procedure broken_compile_demo

declare
  l_count number := 0;
begin
  dbms_session.set_identifier('&&SCENARIO_ID:&&LAB_ID');
  dbms_application_info.set_module('DBINC_DEMO', 'compiler diagnostics');
  for err in (
    select name, type, line, position, text, sequence
    from user_errors
    where name = 'BROKEN_COMPILE_DEMO'
    order by sequence
  ) loop
    l_count := l_count + 1;
    log_event(
      'db_compile',
      'ERROR',
      regexp_substr(err.text, '(ORA|PLS)-[0-9]{{5}}'),
      'Compiler diagnostic for ' || err.type || ' ' || err.name || ' line ' || err.line || ':' || err.position || ' seq ' || err.sequence || ': ' || err.text
    );
  end loop;
  if l_count = 0 then
    log_event('db_compile', 'WARN', null, 'BROKEN_COMPILE_DEMO unexpectedly has no USER_ERRORS rows');
  end if;
end;
/

declare
  procedure capture_expected_call_error(p_label varchar2) is
  begin
    dbms_application_info.set_action(p_label);
    broken_compile_demo;
    log_event('db_compile', 'WARN', null, p_label || ' unexpectedly succeeded');
  exception
    when others then
      log_event('db_compile', 'ERROR', regexp_substr(sqlerrm, 'ORA-[0-9]{{5}}'), p_label || ': ' || sqlerrm);
  end;
begin
  dbms_session.set_identifier('&&SCENARIO_ID:&&LAB_ID');
  dbms_application_info.set_module('DBINC_DEMO', 'invalid object execution');
  capture_expected_call_error('execute invalid PL/SQL object');
end;
/

prompt Captured PL/SQL compiler diagnostics and invalid-object execution errors

declare
begin
  dbms_session.set_identifier('&&SCENARIO_ID:&&LAB_ID');
  dbms_application_info.set_module('DBINC_DEMO', 'safe lock conflict');
  lock table parent_demo in exclusive mode;
  log_event('db_lock', 'INFO', null, 'Held exclusive lock on parent_demo before autonomous NOWAIT probe');
  attempt_parent_lock_nowait;
  rollback;
end;
/

prompt Captured lock-conflict troubleshooting signal

begin
  dbms_session.set_identifier('&&SCENARIO_ID:&&LAB_ID');
  log_event('db_alert_context', 'ERROR', 'ORA-00600', 'Synthetic internal-error marker for correlation only; do not treat as generated database failure', 'true');
  log_event('app', 'ERROR', null, 'Application checkout flow observed database exceptions during DBINC demo');
  log_event('host', 'WARN', null, 'Host CPU queue marker for DBINC demo correlation');
end;
/

commit;

prompt Base DB incident workload complete
"""


def _demo_query_sql(apply: bool) -> str:
    if not apply:
        return "-- Dry run. Evidence query would read DBINC_LAB.incident_event_log.\n"
    return """set linesize 220
set pagesize 100
column event_time format a32
column source format a18
column severity format a8
column ora_code format a10
column module_name format a24
column action_name format a28
column client_identifier format a34
column synthetic format a9
column message format a75

prompt
prompt ============================================================
prompt DBINC DEMO: collected incident evidence timeline
prompt ============================================================

select
  to_char(event_time, 'YYYY-MM-DD"T"HH24:MI:SS.FF3') as event_time,
  source,
  severity,
  ora_code,
  module_name,
  action_name,
  client_identifier,
  synthetic,
  message
from incident_event_log
order by id;

prompt
prompt ============================================================
prompt DBINC DEMO: ORA code repetition summary
prompt ============================================================

select ora_code, count(*) as event_count
from incident_event_log
where ora_code is not null
group by ora_code
order by ora_code;

prompt
prompt ============================================================
prompt DBINC DEMO: source coverage summary
prompt ============================================================

select source, severity, count(*) as event_count
from incident_event_log
group by source, severity
order by source, severity;

prompt
prompt ============================================================
prompt DBINC DEMO: module/action troubleshooting context
prompt ============================================================

select
  nvl(module_name, '<none>') as module_name,
  nvl(action_name, '<none>') as action_name,
  nvl(client_identifier, '<none>') as client_identifier,
  count(*) as event_count
from incident_event_log
group by module_name, action_name, client_identifier
order by event_count desc, module_name, action_name;
"""


def _demo_datasafe_audit_sql(apply: bool) -> str:
    if not apply:
        return "-- Dry run. Demo-only Data Safe audit policy creation would be written here.\n"
    return """set echo on
set serveroutput on
whenever sqlerror exit sql.sqlcode rollback

prompt
prompt ============================================================
prompt DBINC DEMO: configure Data Safe audit policy for DBINC_LAB
prompt ============================================================

define PDB_NAME = "&1"
define AUDIT_POLICY_NAME = DBINC_LAB_DEMO_LOGON_AUDIT

declare
  l_pdb_name varchar2(128) := trim('&&PDB_NAME');
begin
  if l_pdb_name is not null then
    execute immediate 'alter session set container = ' || dbms_assert.simple_sql_name(l_pdb_name);
  end if;
end;
/

declare
begin
  execute immediate 'create audit policy &&AUDIT_POLICY_NAME actions logon';
  dbms_output.put_line('Created unified audit policy &&AUDIT_POLICY_NAME');
exception
  when others then
    if instr(lower(sqlerrm), 'exists') > 0 or instr(lower(sqlerrm), 'already') > 0 then
      dbms_output.put_line('Reusing existing unified audit policy &&AUDIT_POLICY_NAME');
    else
      raise;
    end if;
end;
/

begin
  execute immediate 'audit policy &&AUDIT_POLICY_NAME by DBINC_LAB';
  dbms_output.put_line('Enabled unified audit policy &&AUDIT_POLICY_NAME for DBINC_LAB');
exception
  when others then
    if instr(lower(sqlerrm), 'already') > 0 then
      dbms_output.put_line('Unified audit policy &&AUDIT_POLICY_NAME already enabled for DBINC_LAB');
    else
      raise;
    end if;
end;
/

prompt Data Safe audit policy ready
"""


def _demo_verify_datasafe_audit_sql(apply: bool) -> str:
    if not apply:
        return "-- Dry run. Demo-only Data Safe audit verification query would be written here.\n"
    return """set linesize 220
set pagesize 100
set serveroutput on
whenever sqlerror exit sql.sqlcode rollback

prompt
prompt ============================================================
prompt DBINC DEMO: verify recent unified audit rows for DBINC_LAB
prompt ============================================================

define PDB_NAME = "&1"
define LOOKBACK_MINUTES = "&2"

declare
  l_pdb_name varchar2(128) := trim('&&PDB_NAME');
begin
  if l_pdb_name is not null then
    execute immediate 'alter session set container = ' || dbms_assert.simple_sql_name(l_pdb_name);
  end if;
end;
/

column event_timestamp format a35
column dbusername format a20
column action_name format a24
column return_code format 999999
column userhost format a35
column client_program_name format a28
column object_schema format a20

select
  to_char(event_timestamp, 'YYYY-MM-DD"T"HH24:MI:SS.FF3') as event_timestamp,
  dbusername,
  action_name,
  return_code,
  userhost,
  client_program_name,
  object_schema
from unified_audit_trail
where dbusername = 'DBINC_LAB'
  and event_timestamp >= systimestamp - numtodsinterval(to_number(nvl('&&LOOKBACK_MINUTES', '120')), 'MINUTE')
order by event_timestamp desc;
"""


def _demo_monitoring_account_status_sql(apply: bool) -> str:
    if not apply:
        return "-- Dry run. Monitoring account status query would be written here.\n"
    return """set linesize 220
set pagesize 100
set serveroutput on
whenever sqlerror exit sql.sqlcode rollback

prompt
prompt ============================================================
prompt DBINC DEMO: check monitoring account status
prompt ============================================================

define MONITORING_USER = "&1"

column username format a20
column account_status format a24
column profile format a24
column common format a6
column lock_date format a30
column expiry_date format a30

select
  username,
  account_status,
  profile,
  common,
  to_char(lock_date, 'YYYY-MM-DD"T"HH24:MI:SS') as lock_date,
  to_char(expiry_date, 'YYYY-MM-DD"T"HH24:MI:SS') as expiry_date
from cdb_users
where username = upper(trim('&&MONITORING_USER'))
order by con_id;

prompt
prompt Check current profile policy for failed-login behavior
prompt

column resource_name format a30
column limit format a20

select profile, resource_name, limit
from dba_profiles
where profile in (
  select distinct profile
  from cdb_users
  where username = upper(trim('&&MONITORING_USER'))
)
and resource_name in ('FAILED_LOGIN_ATTEMPTS', 'PASSWORD_LIFE_TIME')
order by profile, resource_name;
"""


def _demo_monitoring_account_recovery_sql(apply: bool) -> str:
    if not apply:
        return "-- Dry run. Monitoring account recovery SQL would be written here.\n"
    return """set echo on
set serveroutput on
whenever sqlerror exit sql.sqlcode rollback

prompt
prompt ============================================================
prompt DBINC DEMO: remediate monitoring account lock loop
prompt ============================================================

define MONITORING_USER = "&1"
define MONITORING_PROFILE = "&2"

declare
  l_profile varchar2(128) := trim('&&MONITORING_PROFILE');
begin
  if l_profile is null then
    raise_application_error(-20001, 'MONITORING_PROFILE is required');
  end if;
  execute immediate 'create profile ' || dbms_assert.simple_sql_name(l_profile) ||
                    ' limit failed_login_attempts unlimited password_life_time unlimited';
  dbms_output.put_line('Created profile ' || l_profile);
exception
  when others then
    if instr(lower(sqlerrm), 'exists') > 0 or instr(lower(sqlerrm), 'already') > 0 then
      dbms_output.put_line('Reusing existing profile ' || trim('&&MONITORING_PROFILE'));
    else
      raise;
    end if;
end;
/

declare
  l_user varchar2(128) := upper(trim('&&MONITORING_USER'));
  l_profile varchar2(128) := trim('&&MONITORING_PROFILE');
begin
  execute immediate 'alter user ' || dbms_assert.simple_sql_name(l_user) ||
                    ' profile ' || dbms_assert.simple_sql_name(l_profile) || ' container=all';
  execute immediate 'alter user ' || dbms_assert.simple_sql_name(l_user) || ' account unlock container=all';
  dbms_output.put_line('Applied profile ' || l_profile || ' and unlocked ' || l_user);
end;
/

prompt
prompt Review the account state below and confirm every monitoring-password consumer is aligned.
prompt

column username format a20
column account_status format a24
column profile format a24
column common format a6

select username, account_status, profile, common
from cdb_users
where username = upper(trim('&&MONITORING_USER'))
order by con_id;
"""


def _demo_alertlog_sql(apply: bool, scenario_id: str, lab_id: str) -> str:
    if not apply:
        return "-- Dry run. Optional SYSDBA script would write reviewed marker lines to the database alert log.\n"
    return f"""set echo on
set serveroutput on
whenever sqlerror exit sql.sqlcode rollback

prompt
prompt ============================================================
prompt DBINC DEMO: optional alert-log synthetic markers
prompt ============================================================

begin
  sys.dbms_system.ksdwrt(2, 'DBINC_DEMO scenario_id={scenario_id} lab_id={lab_id} synthetic=true ORA-00600 marker for Log Analytics correlation only');
  sys.dbms_system.ksdwrt(2, 'DBINC_DEMO scenario_id={scenario_id} lab_id={lab_id} synthetic=true ORA-07445 marker for Log Analytics correlation only');
end;
/

prompt Wrote reviewed synthetic alert-log markers for Log Analytics correlation
"""


def _demo_cleanup_sql(apply: bool) -> str:
    if not apply:
        return "-- Dry run. Cleanup would drop disposable DBINC_LAB schema.\n"
    return """set echo on
whenever sqlerror continue

prompt
prompt ============================================================
prompt DBINC DEMO: cleanup disposable demo schemas
prompt ============================================================

define PDB_NAME = "&1"

declare
  l_pdb_name varchar2(128) := trim('&&PDB_NAME');
begin
  if l_pdb_name is not null then
    execute immediate 'alter session set container = ' || dbms_assert.simple_sql_name(l_pdb_name);
  end if;
end;
/

drop user DBINC_LAB cascade;
drop user HR cascade;
drop user CO cascade;
prompt Dropped disposable DBINC_LAB schema
"""


def _demo_sample_schema_installer(apply: bool) -> str:
    styles = _shell_style_helpers()
    if not apply:
        return """#!/usr/bin/env bash
set -euo pipefail

""" + styles + """

warn "Dry run. Regenerate with --apply and set DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED=true to install HR/CO sample schemas."
"""
    return """#!/usr/bin/env bash
set -euo pipefail

""" + styles + """

if [ "${DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED:-false}" != "true" ]; then
  warn "Oracle sample schema install disabled. Set DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED=true to proceed."
  exit 0
fi

: "${DB_INCIDENT_ADMIN_CONNECT:?Set DB_INCIDENT_ADMIN_CONNECT to install sample schemas.}"
: "${DB_INCIDENT_LAB_PASSWORD:?Set DB_INCIDENT_LAB_PASSWORD before running the demo.}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${DB_INCIDENT_WORK_DIR:-${TMPDIR:-/tmp}/db-incident-sample-schemas}"
ZIP_PATH="${WORK_DIR}/db-sample-schemas-main.zip"
SCHEMA_ROOT="${WORK_DIR}/db-sample-schemas-main"
SAMPLE_PASSWORD="${DB_INCIDENT_SAMPLE_SCHEMA_PASSWORD:-${DB_INCIDENT_LAB_PASSWORD}}"
SAMPLE_TABLESPACE="${DB_INCIDENT_SAMPLE_SCHEMA_TABLESPACE:-USERS}"
SAMPLE_ZIP_URL="${DB_INCIDENT_SAMPLE_SCHEMA_ZIP_URL:-https://github.com/oracle-samples/db-sample-schemas/archive/refs/heads/main.zip}"

if [ -n "${DB_INCIDENT_SQL_BIN:-}" ]; then
  SQL_CLIENT=("$DB_INCIDENT_SQL_BIN" -S)
elif command -v sqlplus >/dev/null 2>&1; then
  SQL_CLIENT=(sqlplus -L -S)
elif [ -x "$SCRIPT_DIR/.tools/sqlcl/bin/sql" ]; then
  SQL_CLIENT=("$SCRIPT_DIR/.tools/sqlcl/bin/sql" -S)
elif command -v sql >/dev/null 2>&1; then
  SQL_CLIENT=(sql -S)
else
  fail "SQL*Plus or SQLcl is required; run 08-local-demo-tooling-preflight.sh first"
  exit 2
fi

run_sql() { "${SQL_CLIENT[@]}" /nolog; }

mkdir -p "$WORK_DIR"
if [ ! -d "$SCHEMA_ROOT" ]; then
  if [ ! -f "$ZIP_PATH" ]; then
    step "Downloading Oracle sample schemas"
    curl -L "$SAMPLE_ZIP_URL" -o "$ZIP_PATH"
  fi
  step "Extracting Oracle sample schemas"
  unzip -q -o "$ZIP_PATH" -d "$WORK_DIR"
fi

rewrite_install_script() {
  source_script="$1"
  rewritten_script="$2"
  escaped_password="$(printf '%s' "$SAMPLE_PASSWORD" | sed 's/[\\/&]/\\\\&/g')"
  escaped_tablespace="$(printf '%s' "$SAMPLE_TABLESPACE" | sed 's/[\\/&]/\\\\&/g')"
  sed \
    -e "s/^ACCEPT pass PROMPT .*$/define pass = \\\"${escaped_password}\\\"/" \
    -e "s/^ACCEPT tbs PROMPT .*$/define tbs = ${escaped_tablespace}/" \
    -e "s/^ACCEPT overwrite_schema PROMPT .*$/define overwrite_schema = YES/" \
    "$source_script" > "$rewritten_script"
}

run_install() {
  schema_dir="$1"
  install_script="$2"
  (
    cd "${SCHEMA_ROOT}/${schema_dir}"
    rewritten_script="./${install_script%.sql}.dbinc.sql"
    rewrite_install_script "$install_script" "$rewritten_script"
  run_sql <<SQL
whenever oserror exit 1
whenever sqlerror exit sql.sqlcode
connect ${DB_INCIDENT_ADMIN_CONNECT}
$(if [ -n "${DB_INCIDENT_PDB_NAME:-}" ]; then cat <<PDBSQL
declare
  l_pdb_name varchar2(128) := '${DB_INCIDENT_PDB_NAME}';
begin
  execute immediate 'alter session set container = ' || dbms_assert.simple_sql_name(l_pdb_name);
end;
/
PDBSQL
fi)
@${rewritten_script}
exit
SQL
  )
}

step "Installing HR sample schema"
run_install human_resources hr_install.sql
step "Installing CO sample schema"
run_install customer_orders co_install.sql

step "Granting demo workload access to HR/CO sample tables"
run_sql <<SQL
whenever oserror exit 1
whenever sqlerror exit sql.sqlcode
connect ${DB_INCIDENT_ADMIN_CONNECT}
$(if [ -n "${DB_INCIDENT_PDB_NAME:-}" ]; then cat <<PDBSQL
declare
  l_pdb_name varchar2(128) := '${DB_INCIDENT_PDB_NAME}';
begin
  execute immediate 'alter session set container = ' || dbms_assert.simple_sql_name(l_pdb_name);
end;
/
PDBSQL
fi)
grant select, insert on hr.employees to DBINC_LAB;
grant select, insert on co.customers to DBINC_LAB;
grant select, insert on co.orders to DBINC_LAB;
grant select on co.stores to DBINC_LAB;
exit
SQL

ok "Installed Oracle sample schemas HR and CO for demo-only observability workload."
"""


def _demo_sample_schema_errors_sql(apply: bool) -> str:
    if not apply:
        return "-- Dry run. Optional HR/CO sample-schema workload would generate real constraint errors and log evidence.\n"
    return """set echo on
set serveroutput on
whenever sqlerror continue

prompt
prompt ============================================================
prompt DBINC DEMO: Oracle HR/CO sample-schema errors
prompt ============================================================

declare
  procedure capture_expected_error(p_source varchar2, p_label varchar2, p_sql varchar2) is
  begin
    dbms_application_info.set_module('DBINC_SAMPLE_SCHEMA_DEMO', p_label);
    execute immediate p_sql;
    log_event(p_source, 'WARN', null, p_label || ' unexpectedly succeeded');
  exception
    when others then
      log_event(p_source, 'ERROR', regexp_substr(sqlerrm, 'ORA-[0-9]{5}'), p_label || ': ' || sqlerrm);
  end;
begin
  dbms_session.set_identifier('DBINC_SAMPLE_SCHEMA_DEMO');
  capture_expected_error(
    'oracle_sample_hr',
    'HR duplicate employee primary key',
    q'[insert into hr.employees (employee_id, first_name, last_name, email, hire_date, job_id) values (100, 'Demo', 'Duplicate', 'DEMO_DUP', sysdate, 'IT_PROG')]'
  );
  capture_expected_error(
    'oracle_sample_hr',
    'HR missing required last name',
    q'[insert into hr.employees (employee_id, first_name, last_name, email, hire_date, job_id) values (999001, 'Demo', null, 'DEMO_NULL', sysdate, 'IT_PROG')]'
  );
  capture_expected_error(
    'oracle_sample_hr',
    'HR invalid department foreign key',
    q'[insert into hr.employees (employee_id, first_name, last_name, email, hire_date, job_id, department_id) values (999002, 'Demo', 'BadDept', 'DEMO_DEPT', sysdate, 'IT_PROG', 999999)]'
  );
  capture_expected_error(
    'oracle_sample_co',
    'CO duplicate customer primary key',
    q'[insert into co.customers (customer_id, email_address, full_name) select min(customer_id), 'demo_duplicate@example.invalid', 'Demo Duplicate' from co.customers]'
  );
  capture_expected_error(
    'oracle_sample_co',
    'CO invalid order customer foreign key',
    q'[insert into co.orders (order_id, order_tms, customer_id, order_status, store_id) select 999999001, systimestamp, 999999001, 'OPEN', min(store_id) from co.stores]'
  );
end;
/

commit;

prompt Oracle sample-schema incident workload complete
"""


def _demo_tooling_preflight_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

""" + _shell_style_helpers() + r"""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
status=0

check_command_required() {
  name="$1"
  install_hint="$2"
  if command -v "$name" >/dev/null 2>&1; then
    ok "$name available: $(command -v "$name")"
  else
    warn "$name not found. $install_hint"
    status=1
  fi
}

check_sqlcl() {
  if [ "${DB_INCIDENT_TOOLING_INSTALL:-false}" = "true" ]; then
    install_sqlcl_if_requested
    return
  fi
  if command -v sql >/dev/null 2>&1; then
    ok "SQLcl available as sql: $(command -v sql)"
    sql -version 2>/dev/null || true
  elif command -v sqlcl >/dev/null 2>&1; then
    ok "SQLcl available as sqlcl: $(command -v sqlcl)"
    sqlcl -version 2>/dev/null || true
  else
    warn "SQLcl not found on PATH."
    install_sqlcl_if_requested
  fi
}

java_major() {
  "$1" -version 2>&1 | awk -F '"' '/version/ { split($2, parts, "."); print (parts[1] == "1" ? parts[2] : parts[1]); exit }'
}

check_sqlcl_java() {
  sqlcl_bin=""
  if [ -x "${SCRIPT_DIR}/.tools/sqlcl/bin/sql" ]; then
    sqlcl_bin="${SCRIPT_DIR}/.tools/sqlcl/bin/sql"
  elif command -v sql >/dev/null 2>&1; then
    sqlcl_bin="$(command -v sql)"
  elif command -v sqlcl >/dev/null 2>&1; then
    sqlcl_bin="$(command -v sqlcl)"
  fi
  [ -n "$sqlcl_bin" ] || return
  for candidate in "${DB_INCIDENT_JAVA_HOME:-}/bin/java" "${JAVA_HOME:-}/bin/java" /usr/java/latest/bin/java /usr/java/default/bin/java /usr/lib/jvm/*/bin/java; do
    [ -x "$candidate" ] || continue
    major="$(java_major "$candidate")"
    case "$major" in ''|*[!0-9]*) continue ;; esac
    if [ "$major" -ge 11 ]; then
      ok "SQLcl-compatible Java $major available: $candidate"
      return
    fi
  done
  warn "SQLcl at $sqlcl_bin requires Java 11+. Set DB_INCIDENT_JAVA_HOME to an approved JDK."
  status=1
}

install_sqlcl_if_requested() {
  if [ "${DB_INCIDENT_TOOLING_INSTALL:-false}" != "true" ]; then
    warn "Set DB_INCIDENT_TOOLING_INSTALL=true with DB_INCIDENT_SQLCL_ARCHIVE or DB_INCIDENT_SQLCL_URL and DB_INCIDENT_SQLCL_SHA256 to install SQLcl packet-locally."
    status=1
    return
  fi
  if [ -z "${DB_INCIDENT_SQLCL_SHA256:-}" ] || ! printf '%s' "$DB_INCIDENT_SQLCL_SHA256" | grep -Eq '^[A-Fa-f0-9]{64}$'; then
    warn "Set DB_INCIDENT_SQLCL_SHA256 to the verified 64-character SHA-256 of the SQLcl archive."
    status=1
    return
  fi
  if [ -n "${DB_INCIDENT_SQLCL_ARCHIVE:-}" ] && [ -n "${DB_INCIDENT_SQLCL_URL:-}" ]; then
    warn "Set exactly one of DB_INCIDENT_SQLCL_ARCHIVE or DB_INCIDENT_SQLCL_URL."
    status=1
    return
  fi
  if [ -z "${DB_INCIDENT_SQLCL_ARCHIVE:-}" ] && [ -z "${DB_INCIDENT_SQLCL_URL:-}" ]; then
    warn "Set DB_INCIDENT_SQLCL_ARCHIVE or a reviewed HTTPS DB_INCIDENT_SQLCL_URL; unverified latest downloads are refused."
    status=1
    return
  fi
  if [ -n "${DB_INCIDENT_SQLCL_URL:-}" ]; then
    case "$DB_INCIDENT_SQLCL_URL" in
      https://*) ;;
      *) warn "DB_INCIDENT_SQLCL_URL must use HTTPS."; status=1; return ;;
    esac
    check_command_required curl "curl is required for the opt-in SQLcl download."
  fi
  check_command_required unzip "unzip is required for the opt-in SQLcl download."
  tools_dir="${SCRIPT_DIR}/.tools"
  sqlcl_zip="${tools_dir}/sqlcl-${DB_INCIDENT_SQLCL_SHA256}.zip"
  mkdir -p "$tools_dir"
  if [ ! -f "$sqlcl_zip" ]; then
    if [ -n "${DB_INCIDENT_SQLCL_ARCHIVE:-}" ]; then
      [ -f "$DB_INCIDENT_SQLCL_ARCHIVE" ] || { warn "SQLcl archive not found: $DB_INCIDENT_SQLCL_ARCHIVE"; status=1; return; }
      step "Copying verified SQLcl archive into packet-local .tools directory"
      cp "$DB_INCIDENT_SQLCL_ARCHIVE" "$sqlcl_zip"
    else
      step "Downloading checksum-pinned SQLcl into packet-local .tools directory"
      curl --fail --location --proto '=https' "$DB_INCIDENT_SQLCL_URL" -o "$sqlcl_zip"
    fi
  fi
  if command -v shasum >/dev/null 2>&1; then
    actual_sha256="$(shasum -a 256 "$sqlcl_zip" | awk '{print $1}')"
  elif command -v sha256sum >/dev/null 2>&1; then
    actual_sha256="$(sha256sum "$sqlcl_zip" | awk '{print $1}')"
  else
    warn "shasum or sha256sum is required to verify the SQLcl archive."
    status=1
    return
  fi
  if [ "$actual_sha256" != "$(printf '%s' "$DB_INCIDENT_SQLCL_SHA256" | tr '[:upper:]' '[:lower:]')" ]; then
    rm -f "$sqlcl_zip"
    warn "SQLcl archive checksum mismatch; archive removed."
    status=1
    return
  fi
  step "Extracting SQLcl"
  unzip -q -o "$sqlcl_zip" -d "$tools_dir"
  if [ -x "${tools_dir}/sqlcl/bin/sql" ]; then
    ok "SQLcl installed locally: ${tools_dir}/sqlcl/bin/sql"
    info "For this shell: export PATH=\"${tools_dir}/sqlcl/bin:$PATH\""
  else
    warn "SQLcl archive extracted, but ${tools_dir}/sqlcl/bin/sql was not found."
    status=1
  fi
}

check_mcp() {
  if [ -n "${DB_INCIDENT_MCP_COMMAND:-}" ]; then
    step "Checking configured DB troubleshooting MCP command"
    if sh -c "${DB_INCIDENT_MCP_COMMAND} --help" >/dev/null 2>&1; then
      ok "DB_INCIDENT_MCP_COMMAND responded to --help"
    else
      warn "DB_INCIDENT_MCP_COMMAND did not respond to --help; verify the Jeff Smith/SQLcl MCP server command before the demo."
    fi
  else
    warn "DB_INCIDENT_MCP_COMMAND is not set. Set it to the reviewed Jeff Smith/SQLcl MCP server launch command used by your MCP host."
  fi
}

banner "DB Incident Local Demo Tooling Preflight"
info "This script checks local demo tools only. It does not connect to OCI or the database."

check_command_required java "SQLcl requires a supported Java runtime."
check_command_required oci "Install OCI CLI and configure a demo profile."
check_sqlcl
check_sqlcl_java
check_mcp

if [ "$status" -eq 0 ]; then
  ok "local demo tooling ready"
else
  warn "local demo tooling has missing prerequisites"
fi

exit "$status"
"""


def _demo_troubleshooting_queries_sql() -> str:
    return """set linesize 220
set pagesize 100
set verify off
set serveroutput on

define OBJECT_NAME = BROKEN_COMPILE_DEMO
define OBJECT_OWNER = DBINC_LAB
define ORA_CODE = ORA-06550

column owner format a24
column object_name format a32
column object_type format a20
column status format a10
column name format a32
column type format a18
column line format 99999
column position format 99999
column text format a120
column referenced_owner format a24
column referenced_name format a32
column referenced_type format a20
column source format a18
column severity format a8
column ora_code format a12
column module_name format a24
column action_name format a28
column client_identifier format a34
column message format a95

prompt
prompt ============================================================
prompt DBINC TROUBLESHOOTING: invalid objects matching &&OBJECT_NAME
prompt ============================================================

select owner, object_name, object_type, status, last_ddl_time
from all_objects
where owner = upper('&&OBJECT_OWNER')
  and object_name like upper('%&&OBJECT_NAME%')
order by owner, object_type, object_name;

prompt
prompt ============================================================
prompt DBINC TROUBLESHOOTING: compiler errors from ALL_ERRORS
prompt ============================================================

select owner, name, type, line, position, sequence, text
from all_errors
where owner = upper('&&OBJECT_OWNER')
  and name like upper('%&&OBJECT_NAME%')
order by owner, name, type, sequence;

prompt
prompt ============================================================
prompt DBINC TROUBLESHOOTING: object dependencies
prompt ============================================================

select owner, name, type, referenced_owner, referenced_name, referenced_type
from all_dependencies
where owner = upper('&&OBJECT_OWNER')
  and name like upper('%&&OBJECT_NAME%')
order by owner, name, referenced_owner, referenced_name;

prompt
prompt ============================================================
prompt DBINC TROUBLESHOOTING: grants that can explain ORA-00942 or PLS-00201
prompt ============================================================

select table_schema, table_name, grantor, grantee, privilege
from all_tab_privs
where grantee in (upper('&&OBJECT_OWNER'), 'PUBLIC')
  and (table_name like '%DEMO%' or table_name like '%ORDERS%' or table_name like '%EMPLOYEES%')
order by table_schema, table_name, grantee, privilege;

prompt
prompt ============================================================
prompt DBINC TROUBLESHOOTING: demo evidence rows matching &&ORA_CODE
prompt ============================================================

select
  to_char(event_time, 'YYYY-MM-DD"T"HH24:MI:SS.FF3') as event_time,
  source,
  severity,
  ora_code,
  module_name,
  action_name,
  client_identifier,
  message
from DBINC_LAB.incident_event_log
where ora_code = upper('&&ORA_CODE')
   or message like '%' || upper('&&ORA_CODE') || '%'
   or message like '%PLS-%'
order by id;

prompt
prompt ============================================================
prompt DBINC TROUBLESHOOTING: current locks and blockers, if privileges allow
prompt ============================================================

declare
  l_has_catalog number := 0;
begin
  select case
           when exists (
             select 1
             from session_privs
             where privilege = 'SELECT ANY DICTIONARY'
           ) or exists (
             select 1
             from session_roles
             where role = 'SELECT_CATALOG_ROLE'
           ) then 1
           else 0
         end
    into l_has_catalog
    from dual;

  if l_has_catalog = 0 then
    dbms_output.put_line('Skipping V$SESSION lock drilldown; connect as a user with SELECT_CATALOG_ROLE or SELECT ANY DICTIONARY.');
    return;
  end if;

  dbms_output.put_line('Catalog privileges detected. Run the query below as the current user or a DBA for live blocker detail:');
  dbms_output.put_line(q'[select sid, serial#, username, module, action, blocking_session, event, state, seconds_in_wait');
  dbms_output.put_line(q'[from v$session]');
  dbms_output.put_line(q'[where username = upper('&&OBJECT_OWNER') or module like 'DBINC%' or blocking_session is not null]');
  dbms_output.put_line(q'[order by blocking_session nulls last, sid;]');
exception
  when others then
    dbms_output.put_line('Skipping V$SESSION lock drilldown: ' || sqlerrm);
end;
/
"""


def _demo_mcp_handoff() -> str:
    return """# DB Troubleshooting MCP Handoff

This packet can be used by a local MCP-backed troubleshooting agent after the demo database is prepared.

## Required Local Tools

- OCI CLI configured for the demo profile.
- SQLcl on `PATH`, or packet-local SQLcl from `DB_INCIDENT_TOOLING_INSTALL=true ./08-local-demo-tooling-preflight.sh`.
- A reviewed Jeff Smith/SQLcl MCP server launch command exported as `DB_INCIDENT_MCP_COMMAND`.
- `DB_INCIDENT_LAB_EZCONNECT` exported when the demo host must reach a PDB listener by Easy Connect.
- `DB_INCIDENT_PDB_NAME` and optional `DB_INCIDENT_PDB_SERVICE` exported when the demo lab runs inside a PDB-local schema.

## Suggested Agent Prompts

- Investigate `ORA-06550` and `PLS-00201` for `DBINC_LAB.BROKEN_COMPILE_DEMO`.
- Use `SHOW ERRORS`, `ALL_ERRORS`, `ALL_OBJECTS`, and `ALL_DEPENDENCIES` evidence before suggesting a fix.
- Correlate `DBINC_LAB.incident_event_log` with Log Analytics records for the same `scenario_id` and `lab_id`.
- Explain whether the error is runtime, compile-time, privilege-related, locking-related, or synthetic alert-log context.

## SQL Evidence Script

Run the read-only SQL script with SQLcl or SQL*Plus after connecting as a user that can see `DBINC_LAB`:

```sql
define OBJECT_OWNER = DBINC_LAB
define OBJECT_NAME = BROKEN_COMPILE_DEMO
define ORA_CODE = ORA-06550
@09-db-troubleshooting-queries.sql
```

Do not run this packet against production databases. It is for showcasing OCI Observability and DB troubleshooting workflows only.
"""


def _coordinator_integration_readme() -> str:
    return """# OCI Coordinator OKE Integration Pack

This folder is a bridge between the `oci-dbman-opsi` DB incident demo and the `oci-coordinator-oke` agent/detection/dashboard conventions.

It is demo-only and not for production use.

## What It Adds

- `db-incident-logan-dashboard.json`: prebuilt dashboard definition with Log Analytics widgets for ORA/PLS errors, source coverage, synthetic marker status, app/host/VCN correlation, and runbook/action links.
- `queries/*.json`: saved-search style Log Analytics detections following the coordinator query JSON convention.
- `db-incident-agent-drilldowns.json`: agent drilldown map for DB Troubleshoot, Log Analytics, Infrastructure, Security, and FinOps agents.
- `db-incident-playbook.yaml`: incident playbook steps that point agents to evidence bundle, SQLcl read-only checks, DBM, OPSI, Data Safe, OCI Audit, and Log Analytics.

## Coordinator Seams Checked

- DB agent prompt exists at `prompts/01-DB-TROUBLESHOOT-AGENT.md`.
- Existing DB skill uses DB Management wait events/top SQL and direct read-only SQL.
- Existing MCP DB tool enforces read-only SQL by default and discovers SQLcl from `SQLCL_PATH` or `sql` on `PATH`.
- Existing detection queries use JSON with `title`, `description`, `query`, `level`, `tags`, and `logsource`.
- Existing dashboard import assets use top-level `_description`, `_version`, and `dashboards`.

## Demo Wiring

1. Generate this packet with `generate-db-incident-demo --apply`.
2. Run `./08-local-demo-tooling-preflight.sh`; set `SQLCL_PATH` or add packet-local `.tools/sqlcl/bin` to `PATH`.
3. Set `DB_INCIDENT_MCP_COMMAND` to the reviewed Jeff Smith/SQLcl MCP server launch command.
4. In coordinator, expose this packet path to the DB agent as local runbook context.
5. Import or copy `queries/*.json` into coordinator Log Analytics detections and `db-incident-logan-dashboard.json` into the dashboard asset path used by the demo.

## Drilldown Questions

- What happened around ORA-06550 on the demo DB?
- Was the PLS-00201 compiler failure isolated or repeated?
- What changed before ORA-00600 synthetic alert-log markers?
- Correlate DB alert logs, app logs, host logs, VCN flow logs, OCI Audit, DBM, OPSI, and Data Safe.
- What evidence should I collect before opening an SR?
"""


def _coordinator_logan_dashboard() -> str:
    payload = {
        "_description": "Demo-only OCI Coordinator dashboard for DB incident troubleshooting across Log Analytics, DBM, OPSI, Data Safe, OCI Audit, app, host, and VCN signals.",
        "_version": "1.0.0",
        "_source": "oci-dbman-opsi",
        "dashboards": [
            {
                "name": "DB Incident Troubleshooting Overview",
                "description": "Shows ORA/PLS error volume, source coverage, synthetic marker status, and incident timeline for the generated DB incident demo.",
                "widgets": [
                    {
                        "title": "ORA/PLS Error Events",
                        "type": "metric",
                        "visualization": "stat",
                        "query": "'ORA-' 'PLS-' | stats count as ErrorEvents",
                    },
                    {
                        "title": "Errors by Code",
                        "type": "bar",
                        "visualization": "bar",
                        "query": "'ORA-' 'PLS-' | extract field='Log Content' '(?<error_code>(ORA|PLS)-[0-9]{5})' | stats count as Events by error_code | sort -Events",
                    },
                    {
                        "title": "Incident Timeline",
                        "type": "timeseries",
                        "visualization": "line",
                        "query": "'scenario_id=' 'lab_id=' | stats count as Events by Time, 'Log Source' | sort -Time",
                    },
                    {
                        "title": "Cross-Source Coverage",
                        "type": "table",
                        "query": "'scenario_id=' 'lab_id=' | stats count as Events by 'Log Source' | sort -Events",
                    },
                    {
                        "title": "Compilation Diagnostics",
                        "type": "table",
                        "query": "'db_compile' 'USER_ERRORS' 'SHOW ERRORS' 'PLS-' | sort -Time",
                    },
                    {
                        "title": "Synthetic Internal Error Markers",
                        "type": "table",
                        "query": "'ORA-00600' 'ORA-07445' 'synthetic=true' | sort -Time",
                    },
                ],
            },
            {
                "name": "DB Incident Agent Drilldowns",
                "description": "Dashboard widgets that point presenters to AI-agent runbooks and drilldown prompts.",
                "widgets": [
                    {
                        "title": "Runbook Links",
                        "type": "markdown",
                        "visualization": "markdown",
                        "content": "- DB troubleshooting: `MCP-HANDOFF.md`\\n- Read-only SQL evidence: `09-db-troubleshooting-queries.sql`\\n- Log Analytics queries: `LOGAN-QUERIES.md`\\n- Demo safety and cleanup: `RUNBOOK.md`",
                    },
                    {
                        "title": "Agent Prompt: Root Cause",
                        "type": "markdown",
                        "visualization": "markdown",
                        "content": "Ask the DB Troubleshoot Agent: `Use the DB incident evidence bundle and 09-db-troubleshooting-queries.sql to explain whether ORA-06550/PLS-00201 is compile-time, privilege-related, or runtime.`",
                    },
                    {
                        "title": "Agent Prompt: Cross Source Correlation",
                        "type": "markdown",
                        "visualization": "markdown",
                        "content": "Ask the Log Analytics Agent: `Correlate ORA/PLS errors, app failures, host signals, VCN context, OCI Audit changes, DBM, OPSI, and Data Safe in the scenario window.`",
                    },
                ],
            },
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _coordinator_agent_drilldowns() -> str:
    payload = {
        "schema_version": "1.0",
        "demo_only": True,
        "agents": [
            {
                "agent": "db-troubleshoot-agent",
                "questions": [
                    "What is the affected DB and why?",
                    "Find the root cause for ORA-06550/PLS-00201 using USER_ERRORS and object dependencies.",
                    "Is ORA-00600 real or synthetic in this demo evidence?",
                ],
                "tools": [
                    "oci_logan_build_db_incident_evidence",
                    "oci_database_list_connections",
                    "oci_database_execute_sql",
                    "oci_dbmgmt_get_wait_events",
                    "oci_dbmgmt_get_top_sql",
                ],
                "runbooks": ["MCP-HANDOFF.md", "09-db-troubleshooting-queries.sql", "RUNBOOK.md"],
            },
            {
                "agent": "log-analytics-agent",
                "questions": [
                    "Show ORA and PLS errors in timeline order.",
                    "Which log sources were present or missing?",
                    "What changed before the first DB alert marker?",
                ],
                "tools": ["oci_logan_build_db_incident_evidence", "Log Analytics saved searches"],
                "runbooks": ["LOGAN-QUERIES.md"],
            },
            {
                "agent": "security-agent",
                "questions": ["Do Data Safe or OCI Audit signals show suspicious login, privilege, or DDL changes?"],
                "tools": ["Data Safe target context", "OCI Audit events"],
                "runbooks": ["RUNBOOK.md"],
            },
            {
                "agent": "infrastructure-agent",
                "questions": ["Do host, VCN, listener, or network logs show a supporting infrastructure cause?"],
                "tools": ["VCN flow logs", "host logs", "OCI Audit"],
                "runbooks": ["LOGAN-QUERIES.md"],
            },
        ],
        "root_cause_patterns": {
            "compile_time": ["PLS-00201", "ORA-06550", "USER_ERRORS", "BROKEN_COMPILE_DEMO"],
            "privilege_or_missing_object": ["ORA-00942", "ALL_TAB_PRIVS", "ALL_OBJECTS"],
            "locking": ["ORA-00054", "v$session", "blocking_session"],
            "internal_error_signature": ["ORA-00600", "ORA-07445", "synthetic=true"],
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _coordinator_playbook_yaml() -> str:
    return """id: db-incident-observability-drilldown
title: DB Incident Observability Drilldown
risk_tier: low
description: Demo-only coordinator playbook for OCI DB incident root-cause analysis.
demo_only: true
trigger:
  rule_id: db-incident-ora-pls-correlation
inputs:
  - ora_code
  - database_name
  - entity_name
  - incident_time
  - compartment_id
steps:
  - id: build-evidence-bundle
    tool: oci_logan_build_db_incident_evidence
    args:
      ora_code: "${inputs.ora_code}"
      database_name: "${inputs.database_name}"
      entity_name: "${inputs.entity_name}"
      incident_time: "${inputs.incident_time}"
      compartment_id: "${inputs.compartment_id}"
      include_sources: "logan,dbm,opsi,audit,datasafe"
    tier: low
    agent: db-troubleshoot-agent
    expected_evidence: "timeline, repetition_scope, cross_source_evidence, hypotheses, missing_source_status"
  - id: compile-error-drilldown
    kind: guidance
    tier: low
    agent: db-troubleshoot-agent
    action: Run read-only SQL from 09-db-troubleshooting-queries.sql for USER_ERRORS, ALL_OBJECTS, ALL_DEPENDENCIES, and grants.
    expected_root_cause: invalid PL/SQL object BROKEN_COMPILE_DEMO references missing package DBINC_MISSING_PACKAGE
  - id: log-analytics-correlation
    tool: oci_logan_execute_query
    args:
      query: "Use detections/oci-log-analytics-detections/queries/apps/db_incident_*.json saved-search OCL for the active ORA/PLS code and time window."
      compartment_id: "${inputs.compartment_id}"
      hours_back: 2
      limit: 100
    tier: low
    agent: log-analytics-agent
    action: Use LOGAN-QUERIES.md and queries/*.json to correlate ORA/PLS, app, host, VCN, OCI Audit, and synthetic alert-log records.
    expected_evidence: first_seen, repeated_or_isolated, sources_present, sources_missing
  - id: managed-service-context
    kind: guidance
    tier: low
    agent: db-troubleshoot-agent
    action: Check DBM wait events/top SQL and OPSI context for concurrent resource pressure.
    expected_evidence: wait_class, top_sql, database_insight_status
  - id: security-context
    kind: guidance
    tier: low
    agent: security-agent
    action: Check Data Safe and OCI Audit for credential, privilege, DDL, or policy changes.
    expected_evidence: audit_changes, datasafe_target_status
  - id: answer
    kind: proposal
    tier: low
    agent: coordinator
    action: Summarize root cause, confidence, uncertainty, impact, diagnostics collected, and next actions.
    expected_sections: [summary, timeline, repetition_scope, cross_source_evidence, hypotheses, impact, next_diagnostics, runbook_links]

approval:
  required_for_tier: medium

outputs:
  - build-evidence-bundle
  - compile-error-drilldown
  - log-analytics-correlation
  - managed-service-context
  - security-context
  - answer
"""


def _coordinator_detection_query_ora_timeline() -> str:
    payload = {
        "title": "DB Incident ORA/PLS Error Timeline",
        "description": "Timeline of Oracle ORA and PLS errors for the DB incident observability demo.",
        "query": "'ORA-' 'PLS-' 'scenario_id=' 'lab_id=' | sort -Time",
        "level": "medium",
        "tags": ["db.incident", "oracle.errors", "logan", "runbook:LOGAN-QUERIES.md"],
        "logsource": {"product": "oci", "service": "loganalytics"},
        "falsepositives": ["Synthetic demo records with synthetic=true"],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _coordinator_detection_query_compile() -> str:
    payload = {
        "title": "DB Incident PL/SQL Compilation Diagnostics",
        "description": "Detects compilation diagnostics and invalid-object evidence for ORA-06550/PLS errors.",
        "query": "'db_compile' 'SHOW ERRORS' 'USER_ERRORS' 'PLS-' 'scenario_id=' 'lab_id=' | sort -Time",
        "level": "medium",
        "tags": ["db.incident", "plsql", "compilation", "runbook:09-db-troubleshooting-queries.sql"],
        "logsource": {"product": "oci", "service": "loganalytics"},
        "falsepositives": ["Expected invalid object created by the demo workload"],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _coordinator_detection_query_cross_source() -> str:
    payload = {
        "title": "DB Incident Cross Source Correlation",
        "description": "Groups DB incident demo records by Log Analytics source to identify missing or supporting telemetry.",
        "query": "'scenario_id=' 'lab_id=' | stats count as Events by 'Log Source' | sort -Events",
        "level": "informational",
        "tags": ["db.incident", "cross_source", "logan", "dbm", "opsi", "datasafe"],
        "logsource": {"product": "oci", "service": "loganalytics"},
        "falsepositives": ["Synthetic demo records generated for OCI Observability showcase"],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _demo_segregation_readme() -> str:
    return """# Demo Segregation

This packet is demo-only and is not for production use.

Use it only to showcase OCI Observability product capabilities across Database Management,
Operations Insights, Log Analytics, OCI Audit correlation, and Data Safe context. The generated
sample-schema workload intentionally creates errors and marker records.

Segregation rules:

- Run against a dedicated demo database or disposable PDB, not an existing production database.
- Keep existing PoC databases in separate config targets and do not set their connect strings in
  `DB_INCIDENT_ADMIN_CONNECT` or `DB_INCIDENT_LAB_CONNECT`.
- Demo targets should opt into all showcase services: `dbm`, `opsi`, `datasafe`, and `logan`.
- Existing PoC targets should keep their current service list and should not inherit demo users,
  sample schemas, or generated incident workload.
- The disposable users are `DBINC_LAB`, `HR`, and `CO`; cleanup drops these users only.
- ORA-00600 and ORA-07445 records are synthetic markers. The scripts do not force internal errors.
"""


def _demo_runbook() -> str:
    return """# DB Incident Demo Runbook

This runbook is for a disposable demo database only. It is not for production use.

## 1. Scope

- Use a dedicated demo database or disposable PDB.
- Keep existing PoC databases in separate config targets.
- Demo target services should be `dbm`, `opsi`, `datasafe`, and `logan`.
- Disposable schemas are `DBINC_LAB`, `HR`, and `CO`.
- `manifest.json` is the machine-readable packet index for automation, demos, and handoff checks.
- Failed-login drills must use the disposable `DBINC_LAB` user only. Do not test bad passwords against the monitoring account.

## 2. Preflight

- Confirm SQL*Plus is installed on the runner.
- Confirm the demo target is visible in Database Management and Operations Insights.
- Confirm Log Analytics source/entity associations are configured for database alert/audit logs and host logs.
- Confirm Data Safe target registration is active if the demo includes security context.

Run the packet validator:

```bash
./validate-demo-packet.sh
./08-local-demo-tooling-preflight.sh

# Optional packet-local SQLcl download if SQLcl is not already on PATH:
DB_INCIDENT_TOOLING_INSTALL=true ./08-local-demo-tooling-preflight.sh
```

## 3. Environment

```bash
export DB_INCIDENT_ADMIN_CONNECT='<reviewed DBA SQL*Plus connect string>'
export DB_INCIDENT_LAB_PASSWORD='<rotated demo-only password>'

# Optional PDB-local lab-user setup:
export DB_INCIDENT_PDB_NAME='<DEMO_PDB_NAME>'
export DB_INCIDENT_PDB_SERVICE='<DEMO_PDB_SERVICE>'
export DB_INCIDENT_LAB_EZCONNECT='//<DEMO_DB_HOST>:1521/<DEMO_PDB_SERVICE>'

# Optional alert-log markers:
export DB_INCIDENT_SYSDBA_CONNECT='<reviewed SYSDBA SQL*Plus connect string>'

# Optional Data Safe audit primer:
export DB_INCIDENT_DATASAFE_AUDIT_ENABLED=true
export DB_INCIDENT_DATASAFE_AUDIT_FAILED_LOGIN_ENABLED=true
export DB_INCIDENT_DATASAFE_AUDIT_LOOKBACK_MINUTES=120

# Optional Oracle sample schemas:
export DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED=true
export DB_INCIDENT_SAMPLE_SCHEMA_PASSWORD="$DB_INCIDENT_LAB_PASSWORD"
export DB_INCIDENT_SAMPLE_SCHEMA_TABLESPACE=USERS

# Optional plain output:
export NO_COLOR=1
```

## 4. Run

```bash
./run-db-incident-demo.sh
```

Expected generated evidence:

- Real safe DB errors in `DBINC_LAB.incident_event_log`.
- Optional unified-audit rows for `DBINC_LAB` when `DB_INCIDENT_DATASAFE_AUDIT_ENABLED=true`.
- Optional HR/CO sample-schema errors when enabled.
- Optional real alert-log marker lines that are explicitly labeled synthetic.
- Synthetic JSONL records for external/app/host/network/alert-log correlation.
- Read-only DBA/MCP troubleshooting queries in `09-db-troubleshooting-queries.sql`.

If the monitoring account used by DBM, OPSI, or Data Safe becomes locked, inspect
and remediate it with the bundled DBA-only SQL:

```bash
sqlplus -L -S /nolog
connect $DB_INCIDENT_ADMIN_CONNECT
@12-check-monitoring-account-status.sql DBSNMP
@13-remediate-monitoring-account-lock.sql DBSNMP C##DBSNMP_MON
exit
```

The recovery script creates or reuses a non-locking common profile and unlocks the
account across containers. Review every monitoring-password consumer before changing
the password itself.

## 5. Correlate

Start with scenario-scoped Log Analytics searches in `LOGAN-QUERIES.md`.

Run an evidence bundle query from the repo root:

```bash
PYTHONPATH=src python -m dbman_opsi.cli db-incident \\
  --profile <PROFILE> \\
  --region <REGION> \\
  --compartment-id <DEMO_COMPARTMENT_OCID> \\
  --ora-code ORA-00600 \\
  --database-name <DEMO_DB_NAME> \\
  --json
```

Use the output to discuss:

- Timeline and what preceded the alert.
- Repetition and affected scope.
- Log Analytics, DBM, OPSI, OCI Audit, and Data Safe source status.
- Why ORA-00600/ORA-07445 are internal error signatures, not root cause by themselves.
- Evidence package needed before an Oracle SR.

## 6. Cleanup

```bash
sqlplus -L -S /nolog
connect $DB_INCIDENT_ADMIN_CONNECT
@05-cleanup-lab-schema.sql
exit
```

Verify `DBINC_LAB`, `HR`, and `CO` do not remain in the demo database unless you intentionally keep the sample schemas for another demo run.
"""


def _demo_logan_queries(scenario_id: str, lab_id: str) -> str:
    return f"""# Log Analytics Query Templates

These queries are scoped to the generated demo identifiers:

- `scenario_id={scenario_id}`
- `lab_id={lab_id}`

They are for demo use only and assume the relevant database alert/audit, host, app, and synthetic JSONL records have been ingested into Log Analytics.

## Incident Timeline

```text
'{scenario_id}' '{lab_id}'
| sort -Time
```

## ORA Internal Error Markers

```text
'ORA-00600' 'ORA-07445' '{scenario_id}' '{lab_id}'
| sort -Time
```

## Safe Real ORA Errors

```text
'ORA-00001' 'ORA-00942' 'ORA-01400' 'ORA-02291' 'ORA-00054' 'ORA-04063' 'ORA-06550' 'ORA-06575' '{scenario_id}' '{lab_id}'
| sort -Time
```

## PL/SQL Compilation Diagnostics

```text
'db_compile' 'SHOW ERRORS' 'USER_ERRORS' 'PLS-' '{scenario_id}' '{lab_id}'
| sort -Time
```

## Source Coverage

```text
'{scenario_id}' '{lab_id}'
| stats count as event_count by 'Log Source'
| sort -event_count
```

## Application And Host Correlation

```text
'Application checkout flow' 'Host CPU queue' 'VCN' '{scenario_id}' '{lab_id}'
| sort -Time
```

## Presenter Notes

- Treat Log Analytics as fast ingestion/search/correlation.
- Use `db-incident --json` for the AI reasoning layer.
- Keep ORA-00600/ORA-07445 framed as synthetic internal-error markers unless they came from a real customer workload.
"""


def _demo_manifest(scenario_id: str, lab_id: str) -> str:
    payload = {
        "schema_version": "1.0",
        "packet": "db_incident_observability_demo",
        "scenario_id": scenario_id,
        "lab_id": lab_id,
        "production_use": False,
        "demo_only": True,
        "segregation": {
            "dedicated_demo_database_required": True,
            "existing_poc_targets_must_remain_separate": True,
            "disposable_users": ["DBINC_LAB", "HR", "CO"],
            "full_demo_services": ["dbm", "opsi", "datasafe", "logan"],
        },
        "artifacts": [
            "README.md",
            "RUNBOOK.md",
            "manifest.json",
            "DEMO-SEGREGATION.md",
            "LOGAN-QUERIES.md",
            "observability-demo-targets.yaml",
            "run-db-incident-demo.sh",
            "validate-demo-packet.sh",
            "upload-logan.sh",
            "01-create-lab-schema.sql",
            "02-generate-safe-errors.sql",
            "03-query-evidence.sql",
            "04-optional-alertlog-marker-sysdba.sql",
            "05-cleanup-lab-schema.sql",
            "06-install-oracle-sample-schemas.sh",
            "07-generate-sample-schema-errors.sql",
            "08-local-demo-tooling-preflight.sh",
            "09-db-troubleshooting-queries.sql",
            "10-enable-datasafe-demo-audit.sql",
            "11-verify-datasafe-demo-audit.sql",
            "12-check-monitoring-account-status.sql",
            "13-remediate-monitoring-account-lock.sql",
            "MCP-HANDOFF.md",
            "oci-coordinator-oke-integration/README.md",
            "oci-coordinator-oke-integration/db-incident-logan-dashboard.json",
            "oci-coordinator-oke-integration/db-incident-agent-drilldowns.json",
            "oci-coordinator-oke-integration/db-incident-playbook.yaml",
            "oci-coordinator-oke-integration/queries/db_incident_ora_error_timeline.json",
            "oci-coordinator-oke-integration/queries/db_incident_compilation_errors.json",
            "oci-coordinator-oke-integration/queries/db_incident_cross_source_correlation.json",
            "synthetic-db-incident.jsonl",
        ],
        "expected_real_errors": ["ORA-00001", "ORA-00942", "ORA-01400", "ORA-02291", "ORA-00054", "ORA-04063", "ORA-06550", "ORA-06575"],
        "expected_compiler_diagnostics": ["PLS-00201"],
        "evidence_fields": [
            "event_time",
            "source",
            "severity",
            "ora_code",
            "module_name",
            "action_name",
            "client_identifier",
            "session_user",
            "message",
            "synthetic",
        ],
        "synthetic_markers": ["ORA-00600", "ORA-07445"],
        "optional_capabilities": {
            "alert_log_markers": "DB_INCIDENT_SYSDBA_CONNECT",
            "datasafe_audit_primer": "DB_INCIDENT_DATASAFE_AUDIT_ENABLED",
            "oracle_sample_schemas": "DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED",
            "log_upload": "DB_INCIDENT_LOG_UPLOAD_ENABLED",
            "db_troubleshooting_mcp": "DB_INCIDENT_MCP_COMMAND",
            "monitoring_account_recovery": "13-remediate-monitoring-account-lock.sql",
        },
        "observability_sources": ["Log Analytics", "Database Management", "Operations Insights", "OCI Audit", "Data Safe"],
        "privacy": {
            "contains_secrets": False,
            "uses_placeholders": True,
            "connect_strings_from_environment": True,
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _demo_observability_targets_yaml() -> str:
    return """# Demo-only target shape. Merge manually into a local, ignored config after replacing placeholders.
# Do not use this for production. Existing PoC DB targets should remain separate entries.
targets:
  - kind: dbcs
    name: demo-observability-db
    compartment_id: <DEMO_COMPARTMENT_OCID>
    resource_id: <DEMO_DATABASE_OCID>
    db_system_id: <DEMO_DB_SYSTEM_OCID>
    service_name: <DEMO_PDB_OR_SERVICE_NAME>
    monitoring_user: DBSNMP
    password_secret_id: <DEMO_DBSNMP_PASSWORD_SECRET_OCID>
    private_endpoint_id: <DEMO_DBM_PRIVATE_ENDPOINT_OCID>
    opsi_private_endpoint_id: <DEMO_OPSI_PRIVATE_ENDPOINT_OCID>
    data_safe_private_endpoint_id: <DEMO_DATA_SAFE_PRIVATE_ENDPOINT_OCID>
    logan_database_entity_id: <DEMO_LOGAN_DATABASE_ENTITY_OCID>
    logan_host_entity_id: <DEMO_LOGAN_HOST_ENTITY_OCID>
    logan_sources:
      - Oracle Database Alert Logs
      - Oracle Database Audit Logs
      - Oracle Database Unified Audit Logs
      - Linux Syslog Logs
    services: [dbm, opsi, datasafe, logan]
    database_role: PDB
    provision: false
"""


def _demo_jsonl(scenario_id: str, lab_id: str) -> str:
    base = datetime.now(UTC).replace(microsecond=0)
    rows = [
        ("oci_audit", "INFO", "DB parameter group changed before alert"),
        ("app", "ERROR", "Checkout service saw database failures"),
        ("db_compile", "ERROR", "PL/SQL compilation diagnostics found in USER_ERRORS for BROKEN_COMPILE_DEMO"),
        ("db_alert", "ERROR", "Synthetic ORA-00600 [kksfbc-reparse-infinite-loop] in alert log"),
        ("host", "WARN", "CPU run queue elevated during incident window"),
        ("vcn_flow", "INFO", "No network drops observed for database listener"),
    ]
    return "".join(
        json.dumps(
            {
                "time": (base + timedelta(minutes=index * 3)).isoformat(),
                "source": source,
                "severity": severity,
                "message": message,
                "synthetic": True,
                "scenario_id": scenario_id,
                "lab_id": lab_id,
            },
            sort_keys=True,
        )
        + "\n"
        for index, (source, severity, message) in enumerate(rows)
    )


def _demo_validator_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

""" + _shell_style_helpers() + """

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
status=0

require_file() {
  path="$1"
  if [ -f "${SCRIPT_DIR}/${path}" ]; then
    ok "found ${path}"
  else
    warn "missing ${path}"
    status=1
  fi
}

require_executable() {
  path="$1"
  if [ -x "${SCRIPT_DIR}/${path}" ]; then
    ok "executable ${path}"
  else
    warn "not executable ${path}"
    status=1
  fi
}

check_shell() {
  path="$1"
  if bash -n "${SCRIPT_DIR}/${path}"; then
    ok "shell syntax ${path}"
  else
    warn "shell syntax failed ${path}"
    status=1
  fi
}

check_command() {
  command_name="$1"
  if command -v "$command_name" >/dev/null 2>&1; then
    ok "command available: ${command_name}"
  else
    warn "command not found: ${command_name}"
  fi
}

check_manifest() {
  path="${SCRIPT_DIR}/manifest.json"
  if grep -q '"packet": "db_incident_observability_demo"' "$path" \
    && grep -q '"production_use": false' "$path" \
    && grep -q '"demo_only": true' "$path" \
    && grep -q '"existing_poc_targets_must_remain_separate": true' "$path" \
    && grep -q '"manifest.json"' "$path" \
    && grep -q '"ORA-00600"' "$path"; then
    ok "manifest safety metadata"
  else
    warn "manifest safety metadata missing or malformed"
    status=1
  fi
}

banner "DB Incident Demo Packet Validation"
info "This validation is local and non-destructive. It does not connect to the database."

for path in \
  README.md \
  RUNBOOK.md \
  manifest.json \
  DEMO-SEGREGATION.md \
  LOGAN-QUERIES.md \
  observability-demo-targets.yaml \
  run-db-incident-demo.sh \
  01-create-lab-schema.sql \
  02-generate-safe-errors.sql \
  03-query-evidence.sql \
  04-optional-alertlog-marker-sysdba.sql \
  05-cleanup-lab-schema.sql \
  06-install-oracle-sample-schemas.sh \
  07-generate-sample-schema-errors.sql \
  08-local-demo-tooling-preflight.sh \
  09-db-troubleshooting-queries.sql \
  10-enable-datasafe-demo-audit.sql \
  11-verify-datasafe-demo-audit.sql \
  12-check-monitoring-account-status.sql \
  13-remediate-monitoring-account-lock.sql \
  MCP-HANDOFF.md \
  oci-coordinator-oke-integration/README.md \
  oci-coordinator-oke-integration/db-incident-logan-dashboard.json \
  oci-coordinator-oke-integration/db-incident-agent-drilldowns.json \
  oci-coordinator-oke-integration/db-incident-playbook.yaml \
  oci-coordinator-oke-integration/queries/db_incident_ora_error_timeline.json \
  oci-coordinator-oke-integration/queries/db_incident_compilation_errors.json \
  oci-coordinator-oke-integration/queries/db_incident_cross_source_correlation.json \
  synthetic-db-incident.jsonl \
  validate-demo-packet.sh \
  upload-logan.sh; do
  require_file "$path"
done

require_executable run-db-incident-demo.sh
require_executable 06-install-oracle-sample-schemas.sh
require_executable 08-local-demo-tooling-preflight.sh
require_executable validate-demo-packet.sh
require_executable upload-logan.sh

check_shell run-db-incident-demo.sh
check_shell 06-install-oracle-sample-schemas.sh
check_shell 08-local-demo-tooling-preflight.sh
check_shell validate-demo-packet.sh
check_shell upload-logan.sh
check_manifest

check_command sqlplus
if [ "${DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED:-false}" = "true" ]; then
  check_command curl
  check_command unzip
fi

if [ -z "${DB_INCIDENT_ADMIN_CONNECT:-}" ]; then
  warn "DB_INCIDENT_ADMIN_CONNECT is not set; live run will stop until it is provided."
fi
if [ -z "${DB_INCIDENT_LAB_PASSWORD:-}" ]; then
  warn "DB_INCIDENT_LAB_PASSWORD is not set; live run will stop until it is provided."
fi

if [ "$status" -eq 0 ]; then
  ok "packet validation complete"
else
  warn "packet validation found structural issues"
fi

exit "$status"
"""


def _demo_upload_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

""" + _shell_style_helpers() + """

if [ "${DB_INCIDENT_LOG_UPLOAD_ENABLED:-false}" != "true" ]; then
  warn "Log Analytics upload disabled. Set DB_INCIDENT_LOG_UPLOAD_ENABLED=true to upload reviewed synthetic JSONL."
  exit 0
fi

step "Preparing synthetic JSONL for Log Analytics upload"
warn "Upload command is environment-specific; import synthetic-db-incident.jsonl with your approved Log Analytics method."
"""


def route_db_incident_question(question: str) -> bool:
    lowered = question.lower()
    return (
        bool(re.search(r"\bora-\d{5}\b", lowered))
        or bool(re.search(r"\bpls-\d{5}\b", lowered))
        or "alert log" in lowered
        or "database incident" in lowered
        or "show errors" in lowered
        or "dba_errors" in lowered
        or "user_errors" in lowered
        or "invalid object" in lowered
        or "compilation error" in lowered
    )
