"""Log Analytics add-on orchestration and payload generation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from dbman_opsi.agent_scripts import (
    _write_ansible_bundle,
    render_agent_install_key_script,
    render_agent_resolve_script,
    render_agent_script,
    render_agent_verify_script,
)
from dbman_opsi.config import EnablementConfig, Target
from dbman_opsi.oci_cli import OciCli

EntityKind = Literal["database", "host", "listener", "adb"]


@dataclass(frozen=True)
class SourceDefinition:
    canonical_name: str
    entity_kind: EntityKind


_SOURCE_ALIASES: dict[str, SourceDefinition] = {
    "oracle database alert logs": SourceDefinition("DBAlertLogSource", "database"),
    "database alert logs": SourceDefinition("DBAlertLogSource", "database"),
    "oracle database alert logs xml": SourceDefinition("DBAlertXMLLogSource", "database"),
    "database xml alert logs": SourceDefinition("DBAlertXMLLogSource", "database"),
    "oracle database audit logs": SourceDefinition("DBAuditLogSource", "database"),
    "database audit logs": SourceDefinition("DBAuditLogSource", "database"),
    "oracle database audit logs xml": SourceDefinition("DBAuditXMLLogSource", "database"),
    "database audit xml logs": SourceDefinition("DBAuditXMLLogSource", "database"),
    "oracle database listener alert logs": SourceDefinition("TNSAlertLogSource", "listener"),
    "database listener alert logs": SourceDefinition("TNSAlertLogSource", "listener"),
    "oracle database listener trace logs": SourceDefinition("TNSTraceLogSource", "listener"),
    "database listener trace logs": SourceDefinition("TNSTraceLogSource", "listener"),
    "oracle database trace logs": SourceDefinition("DBTraceLogSource", "database"),
    "database trace logs": SourceDefinition("DBTraceLogSource", "database"),
    "oracle database unified audit logs": SourceDefinition("unifieddbauditlogfromdbsource122", "database"),
    "oracle unified db audit log source stored in database 12.2": SourceDefinition(
        "unifieddbauditlogfromdbsource122", "database"
    ),
    "oracle unified db audit log source stored in database 12.1": SourceDefinition(
        "unifieddbauditlogfromdbsource121", "database"
    ),
    "database alert logs stored in database": SourceDefinition("DBAlertLogsFromDBSource", "database"),
    "linux syslog logs": SourceDefinition("LinuxSyslogSource", "host"),
    "linux secure logs": SourceDefinition("LinuxSecureLogSource", "host"),
    "linux audit logs": SourceDefinition("AuditLogSource", "host"),
    "linux cron logs": SourceDefinition("LinuxCronLogSource", "host"),
    "linux yum logs": SourceDefinition("LinuxYUMLogSource", "host"),
    "linux yum logs ": SourceDefinition("LinuxYUMLogSource", "host"),
    "linux sudo logs": SourceDefinition("LinuxSudoLogSource", "host"),
    "oci management agent logs": SourceDefinition("MgmtAgentLogSource", "host"),
    "dbalertlogsource": SourceDefinition("DBAlertLogSource", "database"),
    "dbalertxmllogsource": SourceDefinition("DBAlertXMLLogSource", "database"),
    "dbauditlogsource": SourceDefinition("DBAuditLogSource", "database"),
    "dbauditxmllogsource": SourceDefinition("DBAuditXMLLogSource", "database"),
    "tnsalertlogsource": SourceDefinition("TNSAlertLogSource", "listener"),
    "tnstracelogsource": SourceDefinition("TNSTraceLogSource", "listener"),
    "dbtracelogsource": SourceDefinition("DBTraceLogSource", "database"),
    "unifieddbauditlogfromdbsource122": SourceDefinition("unifieddbauditlogfromdbsource122", "database"),
    "unifieddbauditlogfromdbsource121": SourceDefinition("unifieddbauditlogfromdbsource121", "database"),
    "dbalertlogsfromdbsource": SourceDefinition("DBAlertLogsFromDBSource", "database"),
    "linuxsyslogsource": SourceDefinition("LinuxSyslogSource", "host"),
    "linuxsecurelogsource": SourceDefinition("LinuxSecureLogSource", "host"),
    "auditlogsource": SourceDefinition("AuditLogSource", "host"),
    "linuxcronlogsource": SourceDefinition("LinuxCronLogSource", "host"),
    "linuxyumlogsource": SourceDefinition("LinuxYUMLogSource", "host"),
}

DBCS_DATABASE_SOURCES = (
    "DBAlertLogSource",
    "DBAlertXMLLogSource",
    "DBAuditLogSource",
    "DBAuditXMLLogSource",
    "DBTraceLogSource",
    "unifieddbauditlogfromdbsource122",
)
DBCS_HOST_SOURCES = (
    "LinuxSyslogSource",
    "LinuxSecureLogSource",
    "AuditLogSource",
    "LinuxCronLogSource",
    "LinuxYUMLogSource",
)
ADB_SOURCES = (
    "DBAlertLogsFromDBSource",
    "unifieddbauditlogfromdbsource122",
)

_HOST_SOURCE_NAMES = frozenset(source for source, definition in _SOURCE_ALIASES.items() if definition.entity_kind == "host")
_LISTENER_SOURCE_NAMES = frozenset(
    source for source, definition in _SOURCE_ALIASES.items() if definition.entity_kind == "listener"
)


@dataclass(frozen=True)
class LogAnalyticsDecision:
    target: str
    status: str
    detail: str
    logan_database_entity_id: str | None = None
    logan_host_entity_id: str | None = None
    logan_listener_entity_id: str | None = None
    log_group_id: str | None = None
    namespace: str | None = None
    compartment_id: str | None = None
    association_items: tuple[dict[str, Any], ...] = ()
    created_association_items: tuple[dict[str, Any], ...] = ()
    preexisting_association_items: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ResolvedLoganEntities:
    database_entity_id: str | None = None
    host_entity_id: str | None = None
    listener_entity_id: str | None = None
    adb_entity_id: str | None = None


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "target"


def _ocl_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _source_definition(source_name: str) -> SourceDefinition:
    normalized = source_name.strip().lower()
    return _SOURCE_ALIASES.get(normalized, SourceDefinition(source_name, "database"))


def canonical_source_name(source_name: str) -> str:
    return _source_definition(source_name).canonical_name


def _source_entity_kind(source_name: str) -> EntityKind:
    return _source_definition(source_name).entity_kind


def target_logan_sources(target: Target) -> tuple[str, ...]:
    if target.logan_sources:
        raw_sources = target.logan_sources
    elif target.kind == "autonomous":
        raw_sources = ADB_SOURCES
    elif target.kind in {"dbcs", "exadata"}:
        raw_sources = DBCS_DATABASE_SOURCES + DBCS_HOST_SOURCES
    else:
        raw_sources = DBCS_DATABASE_SOURCES

    canonical_sources: list[str] = []
    seen: set[str] = set()
    for raw_source in raw_sources:
        canonical = canonical_source_name(raw_source)
        if canonical in seen:
            continue
        seen.add(canonical)
        canonical_sources.append(canonical)
    return tuple(canonical_sources)


def _association_properties(target: Target) -> list[dict[str, str]]:
    properties: dict[str, str] = {}
    if target.logan_adr_home:
        properties["ADR_HOME"] = target.logan_adr_home
    if target.logan_oracle_home:
        properties["ORACLE_HOME"] = target.logan_oracle_home
    if target.logan_install_home:
        properties["INSTALL_HOME"] = target.logan_install_home
    if target.logan_hostname:
        properties["HOST_NAME"] = target.logan_hostname
    if target.kind == "autonomous":
        properties["SERVICE_NAME"] = target.logan_adb_service_name or target.service_name or target.name
        properties["CREDENTIAL_NAME"] = "DBTCPSCreds"
    return [{"name": name, "value": value} for name, value in sorted(properties.items())]


def association_payload(target: Target, source_name: str, entity_id: str, log_group_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sourceName": canonical_source_name(source_name),
        "entityId": entity_id,
        "logGroupId": log_group_id,
    }
    association_properties = _association_properties(target)
    if association_properties:
        payload["associationProperties"] = association_properties
    agent_id = target.logan_management_agent_id or target.management_agent_id
    if agent_id:
        payload["agentId"] = agent_id
    return payload


def _association_source_entity_key(item: dict[str, Any]) -> tuple[str, str] | None:
    """Normalize OCI's camel/kebab response fields to the source/entity identity."""

    normalized = {
        re.sub(r"[-_]", "", str(name)).lower(): value
        for name, value in item.items()
    }
    source = normalized.get("sourcename")
    entity = normalized.get("entityid")
    if not isinstance(source, str) or not source or not isinstance(entity, str) or not entity:
        return None
    return canonical_source_name(source), entity


def _entity_id_for_source(
    target: Target,
    source_name: str,
    resolved_entities: ResolvedLoganEntities | None = None,
) -> str | None:
    resolved = resolved_entities or ResolvedLoganEntities(
        database_entity_id=target.logan_database_entity_id,
        host_entity_id=target.logan_host_entity_id,
        listener_entity_id=target.logan_listener_entity_id,
        adb_entity_id=target.logan_adb_entity_id,
    )
    if target.kind == "autonomous":
        return resolved.adb_entity_id
    entity_kind = _source_entity_kind(source_name)
    if entity_kind == "host":
        return resolved.host_entity_id
    if entity_kind == "listener":
        return resolved.listener_entity_id
    return resolved.database_entity_id


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_host_facts_script(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail

HOST_NAME="$(hostname -f 2>/dev/null || hostname)"
ORACLE_HOME_CANDIDATE="$(find /u01/app/oracle/product -maxdepth 2 -type d -name 'dbhome_*' 2>/dev/null | head -1 || true)"
ADR_TRACE="$(sudo su - oracle -c "sqlplus -s / as sysdba <<'SQL'
set heading off feedback off pages 0 verify off echo off
whenever sqlerror exit sql.sqlcode
select value from v\\$diag_info where name = 'Diag Trace';
SQL" 2>/dev/null | awk 'NF {print; exit}' || true)"
ADR_HOME=""
if [ -n "$ADR_TRACE" ]; then
  ADR_HOME="${ADR_TRACE%/trace}"
fi

printf 'hostname=%s\\n' "$HOST_NAME"
printf 'oracle_homes=\\n'
find /u01/app/oracle/product -maxdepth 2 -type d 2>/dev/null || true
printf 'adr_homes=\\n'
find /u01/app/oracle/diag -maxdepth 5 -type d 2>/dev/null || true
printf 'candidate_logs=\\n'
for path in /var/log/messages /var/log/secure /var/log/audit/audit.log /var/log/cron /var/log/yum.log /var/log/dnf.log; do
  [ -e "$path" ] && ls -l "$path"
done

cat <<EOF

Suggested ignored-config fields:
logan_hostname: ${HOST_NAME}
logan_oracle_home: ${ORACLE_HOME_CANDIDATE:-<SET_ME>}
logan_adr_home: ${ADR_HOME:-<SET_ME>}
EOF
""",
        encoding="utf-8",
    )
    path.chmod(0o750)


def _write_acl_script(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail

AGENT_USER="${AGENT_USER:-mgmt_agent}"
for path in "$@"; do
  [ -e "$path" ] || continue
  sudo setfacl -m "u:${AGENT_USER}:rX" "$path"
done
echo "Applied read ACLs for ${AGENT_USER}. Use group/world-readable permissions only as a documented fallback."
""",
        encoding="utf-8",
    )
    path.chmod(0o750)


def _write_db_user_sql(path: Path, username: str = "C##DBMAN_LOGAN") -> None:
    path.write_text(
        f"""-- Least-privilege Log Analytics collection user. Run as a DBA; do not use SYS for collection.
define LOGAN_USER={username}
create user &&LOGAN_USER identified by "REPLACE_WITH_ROTATED_PASSWORD";
grant create session to &&LOGAN_USER;
grant select on v_$diag_alert_ext to &&LOGAN_USER;
grant select on v_$diag_trace_file to &&LOGAN_USER;
grant select on unified_audit_trail to &&LOGAN_USER;
grant select on dba_audit_trail to &&LOGAN_USER;
""",
        encoding="utf-8",
    )


def _write_adb_credential_template(path: Path, target: Target) -> None:
    payload = {
        "credentialName": "DBTCPSCreds",
        "userName": target.monitoring_user or "C##DBMAN_LOGAN",
        "password": "${DBMAN_LOGAN_DB_PASSWORD}",
        "walletDirectory": "${DBMAN_LOGAN_ADB_WALLET_DIR}",
        "connectString": target.logan_adb_service_name or target.service_name or "<ADB_SERVICE_NAME>",
    }
    _write_json(path, payload)


def generate_logan_payloads(
    config: EnablementConfig,
    output_dir: str | Path,
    *,
    log_group_id: str | None = None,
    resolved_entities: dict[str, ResolvedLoganEntities] | None = None,
) -> list[Path]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    target_entities = resolved_entities or {}
    effective_log_group_id = log_group_id or config.log_analytics.log_group_id or "<LOG_ANALYTICS_LOG_GROUP_OCID>"
    for target in config.targets:
        if not target.wants("logan"):
            continue
        target_dir = destination / _slug(target.name)
        assoc_dir = target_dir / "associations"
        assoc_dir.mkdir(parents=True, exist_ok=True)
        _write_host_facts_script(target_dir / "00-discover-logan-host-facts.sh")
        paths.append(target_dir / "00-discover-logan-host-facts.sh")
        _write_acl_script(target_dir / "01-grant-logan-log-acls.sh")
        paths.append(target_dir / "01-grant-logan-log-acls.sh")
        _write_db_user_sql(target_dir / "02-create-logan-db-user.sql")
        paths.append(target_dir / "02-create-logan-db-user.sql")
        if target.kind in {"dbcs", "exadata", "external-db", "external-exadata"}:
            install_key_path = target_dir / "03-create-logan-management-agent-install-key.sh"
            install_path = target_dir / "04-install-logan-management-agent.sh"
            verify_path = target_dir / "05-verify-logan-management-agent.sh"
            resolve_path = target_dir / "06-resolve-logan-management-agent.sh"
            install_key_path.write_text(render_agent_install_key_script(target, config), encoding="utf-8")
            install_key_path.chmod(0o750)
            install_path.write_text(render_agent_script(target, config), encoding="utf-8")
            install_path.chmod(0o750)
            verify_path.write_text(render_agent_verify_script(target, config), encoding="utf-8")
            verify_path.chmod(0o750)
            resolve_path.write_text(render_agent_resolve_script(target, config), encoding="utf-8")
            resolve_path.chmod(0o750)
            paths.extend([install_key_path, install_path, verify_path, resolve_path])
            paths.extend(
                _write_ansible_bundle(
                    target_dir,
                    target,
                    config,
                    bootstrap_name="07-bootstrap-logan-management-agent-ansible.sh",
                    run_name="08-run-logan-management-agent-ansible.sh",
                    playbook_name="09-logan-management-agent-playbook.yml",
                    ansible_cfg_name="10-logan-management-agent-ansible.cfg",
                    package_name="11-resolve-logan-management-agent-package-url.sh",
                    install_script_name=install_path.name,
                    verify_script_name=verify_path.name,
                    resolve_name=resolve_path.name,
                )
            )
        if target.kind == "autonomous":
            _write_adb_credential_template(target_dir / "credential-template.json", target)
            paths.append(target_dir / "credential-template.json")
        resolved = target_entities.get(target.name)
        for source in target_logan_sources(target):
            entity_id = _entity_id_for_source(target, source, resolved) or "<LOG_ANALYTICS_ENTITY_OCID>"
            path = assoc_dir / f"{_slug(source)}.json"
            _write_json(path, [association_payload(target, source, entity_id, effective_log_group_id)])
            paths.append(path)
    return paths


class LogAnalyticsService:
    def __init__(self, oci: OciCli) -> None:
        self.oci = oci

    def enable_all(
        self,
        config: EnablementConfig,
        *,
        payload_dir: str | Path = "generated/logan",
        onboard_namespace: bool | None = None,
    ) -> list[LogAnalyticsDecision]:
        namespace = self._resolve_namespace(config, onboard_namespace=onboard_namespace)
        if not namespace:
            return [LogAnalyticsDecision("tenancy", "blocked", "Log Analytics namespace is not set or onboarded")]
        compartment = config.compartment_id or ""
        if not compartment:
            return [LogAnalyticsDecision("tenancy", "blocked", "compartment_id is required for Log Analytics")]

        log_group_id = config.log_analytics.log_group_id
        if not log_group_id and config.log_analytics.create_log_group:
            log_group_id = self.oci.create_log_analytics_log_group(
                namespace, compartment, config.log_analytics.log_group_name
            )
        dry_run = bool(getattr(getattr(self.oci, "runner", None), "dry_run", False))
        if not log_group_id and dry_run and config.log_analytics.create_log_group:
            log_group_id = "<LOG_ANALYTICS_LOG_GROUP_OCID>"
        if not log_group_id:
            return [
                LogAnalyticsDecision(
                    "tenancy",
                    "blocked",
                    "Log Analytics log_group_id is not set and log-group creation is disabled or failed",
                )
            ]

        resolved_entities: dict[str, ResolvedLoganEntities] = {}
        actionable_targets: list[Target] = []
        target_decisions: list[LogAnalyticsDecision] = []
        for target in config.targets:
            if not target.wants("logan"):
                continue
            resolved = self._resolve_target_entities(namespace, compartment, target)
            resolved_entities[target.name] = resolved
            missing = self._missing_entities(target, resolved)
            if missing:
                detail = self._blocked_detail(target, missing)
                target_decisions.append(
                    LogAnalyticsDecision(
                        target.name,
                        "blocked",
                        detail,
                        logan_database_entity_id=resolved.database_entity_id,
                        logan_host_entity_id=resolved.host_entity_id,
                        logan_listener_entity_id=resolved.listener_entity_id,
                        log_group_id=log_group_id,
                    )
                )
                continue
            actionable_targets.append(target)
        actionable_config = replace(config, targets=tuple(actionable_targets))
        generated = generate_logan_payloads(
            actionable_config,
            payload_dir,
            log_group_id=log_group_id,
            resolved_entities=resolved_entities,
        )
        decisions = [
            LogAnalyticsDecision(
                "tenancy",
                "ready",
                f"namespace={namespace}; log_group_id={log_group_id or '<unset>'}; payloads={len(generated)}",
                log_group_id=log_group_id,
            )
        ]
        decisions.extend(target_decisions)
        for target in actionable_targets:
            resolved = resolved_entities.get(target.name, ResolvedLoganEntities())
            association_items: list[dict[str, Any]] = []
            for source in target_logan_sources(target):
                entity_id = _entity_id_for_source(target, source, resolved)
                if not entity_id or not log_group_id:
                    continue
                association_items.append(association_payload(target, source, entity_id, log_group_id))
            created_items, preexisting_items = self._classify_associations(
                namespace,
                compartment,
                association_items,
            )
            if created_items:
                self.oci.upsert_log_analytics_associations(namespace, compartment, created_items)
            decisions.append(
                LogAnalyticsDecision(
                    target.name,
                    "configured",
                    f"source association payloads applied ({len(association_items)} sources)",
                    logan_database_entity_id=resolved.database_entity_id,
                    logan_host_entity_id=resolved.host_entity_id,
                    logan_listener_entity_id=resolved.listener_entity_id,
                    log_group_id=log_group_id,
                    namespace=namespace,
                    compartment_id=compartment,
                    association_items=tuple(association_items),
                    created_association_items=tuple(created_items),
                    preexisting_association_items=tuple(preexisting_items),
                )
            )
        return decisions

    def _classify_associations(
        self,
        namespace: str,
        compartment_id: str,
        association_items: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Separate newly requested source/entity pairs from preexisting ones."""

        existing_by_entity: dict[str, set[tuple[str, str]]] = {}
        created: list[dict[str, Any]] = []
        preexisting: list[dict[str, Any]] = []
        for item in association_items:
            key = _association_source_entity_key(item)
            if key is None:
                # The payload builder always provides both fields. Preserve a
                # malformed response's safety boundary by never claiming it.
                preexisting.append(item)
                continue
            source_name, entity_id = key
            existing = existing_by_entity.get(entity_id)
            if existing is None:
                existing = {
                    existing_key
                    for existing_item in self.oci.list_log_analytics_entity_source_associations(
                        namespace,
                        compartment_id,
                        entity_id,
                    )
                    if (existing_key := _association_source_entity_key(existing_item)) is not None
                }
                existing_by_entity[entity_id] = existing
            if (source_name, entity_id) in existing:
                preexisting.append(item)
            else:
                created.append(item)
        return created, preexisting

    def validation_findings(self, config: EnablementConfig) -> list[str]:
        namespace = config.log_analytics.namespace
        if not namespace:
            return ["Log Analytics: namespace not configured"]
        compartment = config.compartment_id or ""
        warnings = self.oci.list_log_analytics_warnings(namespace, compartment) if compartment else []
        findings = [f"Log Analytics: warnings={len(warnings)}"]
        for target in config.targets:
            if not target.wants("logan"):
                continue
            query = (
                f"'Log Source' != null | where Entity = {_ocl_literal(target.name)} "
                "| stats count as log_count by 'Log Source'"
            )
            result = self.oci.search_log_analytics(namespace, query)
            count = result.get("count") or result.get("total-count") or "available"
            findings.append(f"{target.name}: Log Analytics query result count={count}")
        return findings

    def _resolve_namespace(self, config: EnablementConfig, *, onboard_namespace: bool | None) -> str:
        if config.log_analytics.namespace:
            return config.log_analytics.namespace
        compartment = config.compartment_id or ""
        if not compartment:
            return ""
        should_onboard = config.log_analytics.onboard_namespace if onboard_namespace is None else onboard_namespace
        if should_onboard:
            return self.oci.onboard_log_analytics_namespace(compartment)
        namespace = self.oci.get_log_analytics_namespace(compartment)
        if namespace:
            return namespace
        if getattr(getattr(self.oci, "runner", None), "dry_run", False):
            return "<LOG_ANALYTICS_NAMESPACE>"
        return ""

    def _resolve_target_entities(
        self,
        namespace: str,
        compartment_id: str,
        target: Target,
    ) -> ResolvedLoganEntities:
        if getattr(getattr(self.oci, "runner", None), "dry_run", False):
            agent_id = target.logan_management_agent_id or target.management_agent_id
            database_entity_id = target.logan_database_entity_id
            host_entity_id = target.logan_host_entity_id
            listener_entity_id = target.logan_listener_entity_id
            if target.kind != "autonomous" and agent_id:
                if any(_source_entity_kind(source) == "database" for source in target_logan_sources(target)):
                    database_entity_id = database_entity_id or "<LOG_ANALYTICS_DATABASE_ENTITY_OCID>"
                if any(_source_entity_kind(source) == "host" for source in target_logan_sources(target)):
                    host_entity_id = host_entity_id or "<LOG_ANALYTICS_HOST_ENTITY_OCID>"
                if any(_source_entity_kind(source) == "listener" for source in target_logan_sources(target)):
                    listener_entity_id = listener_entity_id or "<LOG_ANALYTICS_LISTENER_ENTITY_OCID>"
            return ResolvedLoganEntities(
                database_entity_id=database_entity_id,
                host_entity_id=host_entity_id,
                listener_entity_id=listener_entity_id,
                adb_entity_id=target.logan_adb_entity_id,
            )

        if target.kind == "autonomous":
            return ResolvedLoganEntities(adb_entity_id=target.logan_adb_entity_id)

        agent_id = target.logan_management_agent_id or target.management_agent_id
        has_database_sources = any(
            _source_entity_kind(source) == "database" for source in target_logan_sources(target)
        )
        has_host_sources = any(_source_entity_kind(source) == "host" for source in target_logan_sources(target))
        has_listener_sources = any(_source_entity_kind(source) == "listener" for source in target_logan_sources(target))
        if not agent_id and not any(
            (
                target.logan_database_entity_id,
                target.logan_host_entity_id,
                target.logan_listener_entity_id,
            )
        ):
            return ResolvedLoganEntities()
        database_entity_id = target.logan_database_entity_id
        if has_database_sources:
            database_entity_id = database_entity_id or self.oci.create_log_analytics_entity(
                namespace,
                compartment_id,
                f"{target.name}-db",
                "Oracle Database Instance",
                cloud_resource_id=target.resource_id,
                agent_id=agent_id,
            )
        host_entity_id = target.logan_host_entity_id
        if has_host_sources:
            host_entity_id = host_entity_id or self.oci.create_log_analytics_entity(
                namespace,
                compartment_id,
                f"{target.name}-host",
                "Host (Linux)",
                hostname=target.logan_hostname,
                agent_id=agent_id,
            )
        listener_entity_id = target.logan_listener_entity_id
        if has_listener_sources:
            listener_entity_id = listener_entity_id or self.oci.create_log_analytics_entity(
                namespace,
                compartment_id,
                f"{target.name}-listener",
                "Oracle Database Listener",
                hostname=target.logan_hostname,
                agent_id=agent_id,
            )
        return ResolvedLoganEntities(
            database_entity_id=database_entity_id,
            host_entity_id=host_entity_id,
            listener_entity_id=listener_entity_id,
            adb_entity_id=target.logan_adb_entity_id,
        )

    @staticmethod
    def _missing_entities(target: Target, resolved: ResolvedLoganEntities) -> tuple[str, ...]:
        missing: list[str] = []
        if target.kind == "autonomous":
            if not resolved.adb_entity_id:
                missing.append("adb")
            return tuple(missing)
        for source in target_logan_sources(target):
            entity_kind = _source_entity_kind(source)
            if entity_kind == "host" and not resolved.host_entity_id and "host" not in missing:
                missing.append("host")
            elif entity_kind == "listener" and not resolved.listener_entity_id and "listener" not in missing:
                missing.append("listener")
            elif entity_kind == "database" and not resolved.database_entity_id and "database" not in missing:
                missing.append("database")
        return tuple(missing)

    @staticmethod
    def _blocked_detail(target: Target, missing: tuple[str, ...]) -> str:
        needs = ", ".join(missing)
        if target.kind == "autonomous":
            return f"missing Log Analytics {needs} entity id for autonomous collection"
        return (
            f"missing Log Analytics {needs} entity binding; install an OCI Management Agent with the logan plugin "
            "or supply existing management-agent-backed Log Analytics entity IDs in config"
        )
