import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from dbman_opsi.db_incident import (
    DbIncidentEvidenceService,
    DbIncidentRequest,
    build_logan_db_incident_query,
    generate_db_incident_demo,
    oci_logan_build_db_incident_evidence,
    route_db_incident_question,
)


class FakeIncidentOci:
    def get_log_analytics_namespace(self, compartment_id):
        return "logan"

    def search_log_analytics(self, namespace, query, **kwargs):
        self.query = query
        return {
            "items": [
                {
                    "Time": "2026-06-24T10:00:00Z",
                    "Source": "db_alert",
                    "Severity": "ERROR",
                    "Log Content": "Synthetic ORA-00600 in alert log for ORDERSDB",
                    "host": "dbhost1",
                    "synthetic": True,
                },
                {
                    "Time": "2026-06-24T10:03:00Z",
                    "Source": "app",
                    "Severity": "ERROR",
                    "Log Content": "ORDERSDB request failures after database alert",
                },
            ]
        }

    def list_managed_databases(self, compartment_id):
        return [{"name": "ORDERSDB", "database-status": "UP", "time-updated": "2026-06-24T10:04:00Z"}]

    def list_opsi_database_insights(self, compartment_id):
        return [{"database-name": "ORDERSDB", "status": "ACTIVE", "time-updated": "2026-06-24T10:05:00Z"}]

    def list_audit_events(self, compartment_id, start_time, end_time):
        return [
            {
                "event-time": "2026-06-24T09:50:00Z",
                "event-type": "com.oraclecloud.databases.updateDatabase",
                "principal-name": "demo-operator",
            }
        ]

    def list_data_safe_targets(self, compartment_id):
        return [{"display-name": "ORDERSDB", "lifecycle-state": "ACTIVE", "time-created": "2026-06-24T09:00:00Z"}]

    def list_data_safe_audit_events(self, compartment_id, start_time, end_time):
        return [
            {
                "audit-event-time": "2026-06-24T09:55:00Z",
                "target-name": "ORDERSDB",
                "db-user-name": "APPUSER",
                "event-name": "LOGON FAILURE",
                "operation-status": "FAILURE",
                "client-ip": "10.0.0.10",
            }
        ]


def test_logan_incident_query_escapes_literals_and_bounds_limit() -> None:
    request = DbIncidentRequest(
        ora_code="ORA-00600",
        database_name="ORDERS'DB",
        incident_time="2026-06-24T10:00:00Z",
        limit=999,
    )

    query = build_logan_db_incident_query(request)

    assert "'ORDERS''DB'" in query
    assert "'ORA-00600' 'ORDERS''DB'" in query
    assert "head 500" in query
    assert "sort -Time" in query
    assert "2026-06-24T09:30:00+00:00" not in query
    assert "2026-06-24T10:30:00+00:00" not in query


def test_db_incident_bundle_includes_cross_source_context_and_uncertainty() -> None:
    request = DbIncidentRequest(
        ora_code="ORA-00600",
        database_name="ORDERSDB",
        incident_time="2026-06-24T10:00:00Z",
        compartment_id="ocid" + "1.compartment.oc1..aaaaaaaa",
    )

    bundle = DbIncidentEvidenceService(FakeIncidentOci()).build(request).to_dict()

    assert bundle["workflow"] == "db_incident_analysis"
    assert "ORA-00600" in bundle["summary"]
    assert bundle["repetition_scope"]["matching_events"] == 1
    assert {status["source"] for status in bundle["cross_source_evidence"]} == {
        "logan",
        "dbm",
        "opsi",
        "audit",
        "datasafe",
    }
    assert any(event["source"] == "audit" for event in bundle["timeline"])
    assert any(event["source"] == "datasafe" and "LOGON FAILURE" in event["message"] for event in bundle["timeline"])
    assert "not a root cause by itself" in bundle["uncertainty"]
    assert any("trace files" in item for item in bundle["sr_evidence_package"])


def test_db_incident_bundle_reports_missing_sources_without_failing() -> None:
    request = DbIncidentRequest(ora_code="ORA-00060", include_sources=("logan", "dbm"))

    bundle = DbIncidentEvidenceService().build(request).to_dict()

    statuses = {status["source"]: status["status"] for status in bundle["cross_source_evidence"]}
    assert statuses == {"logan": "unavailable", "dbm": "unavailable"}
    assert bundle["timeline"] == []


def test_compilation_error_bundle_recommends_user_errors_and_object_status() -> None:
    request = DbIncidentRequest(ora_code="PLS-00201", include_sources=())

    bundle = DbIncidentEvidenceService().build(request).to_dict()

    assert bundle["hypotheses"][0]["hypothesis"] == "Invalid or newly compiled PL/SQL object"
    assert any("SHOW ERRORS" in item for item in bundle["next_diagnostics"])
    assert any("USER_ERRORS" in item for item in bundle["next_diagnostics"])
    assert any("object status" in item for item in bundle["next_diagnostics"])
    assert "DBA_ERRORS or USER_ERRORS rows" in bundle["sr_evidence_package"]


def test_generate_db_incident_demo_is_dry_run_and_marks_synthetic_records(tmp_path: Path) -> None:
    paths = generate_db_incident_demo(tmp_path)

    assert tmp_path / "synthetic-db-incident.jsonl" in paths
    assert tmp_path / "run-db-incident-demo.sh" in paths
    assert tmp_path / "06-install-oracle-sample-schemas.sh" in paths
    assert tmp_path / "08-local-demo-tooling-preflight.sh" in paths
    assert tmp_path / "09-db-troubleshooting-queries.sql" in paths
    assert tmp_path / "10-enable-datasafe-demo-audit.sql" in paths
    assert tmp_path / "11-verify-datasafe-demo-audit.sql" in paths
    assert tmp_path / "12-check-monitoring-account-status.sql" in paths
    assert tmp_path / "13-remediate-monitoring-account-lock.sql" in paths
    assert tmp_path / "MCP-HANDOFF.md" in paths
    assert tmp_path / "oci-coordinator-oke-integration" / "db-incident-logan-dashboard.json" in paths
    assert tmp_path / "DEMO-SEGREGATION.md" in paths
    assert tmp_path / "LOGAN-QUERIES.md" in paths
    assert tmp_path / "RUNBOOK.md" in paths
    assert tmp_path / "manifest.json" in paths
    assert tmp_path / "validate-demo-packet.sh" in paths
    assert "Dry run" in (tmp_path / "01-create-lab-schema.sql").read_text(encoding="utf-8")
    upload = (tmp_path / "upload-logan.sh").read_text(encoding="utf-8")
    assert "DB_INCIDENT_LOG_UPLOAD_ENABLED" in upload
    rows = [
        json.loads(line)
        for line in (tmp_path / "synthetic-db-incident.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows
    assert all(row["synthetic"] is True for row in rows)
    assert all(row["scenario_id"] for row in rows)
    assert any("ORA-00600" in row["message"] for row in rows)


def test_generate_db_incident_demo_rejects_scenario_id_that_can_inject_sql(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="scenario_id"):
        generate_db_incident_demo(
            tmp_path / "packet",
            apply=True,
            scenario_id="safe-id\nprompt injected",
        )

    assert not (tmp_path / "packet").exists()


def test_generate_db_incident_demo_apply_writes_executable_sqlplus_workload(tmp_path: Path) -> None:
    generate_db_incident_demo(tmp_path, apply=True, scenario_id="incident-1")

    runner = (tmp_path / "run-db-incident-demo.sh").read_text(encoding="utf-8")
    setup_sql = (tmp_path / "01-create-lab-schema.sql").read_text(encoding="utf-8")
    workload_sql = (tmp_path / "02-generate-safe-errors.sql").read_text(encoding="utf-8")
    query_sql = (tmp_path / "03-query-evidence.sql").read_text(encoding="utf-8")
    alert_sql = (tmp_path / "04-optional-alertlog-marker-sysdba.sql").read_text(encoding="utf-8")
    cleanup_sql = (tmp_path / "05-cleanup-lab-schema.sql").read_text(encoding="utf-8")
    sample_installer = (tmp_path / "06-install-oracle-sample-schemas.sh").read_text(encoding="utf-8")
    sample_errors = (tmp_path / "07-generate-sample-schema-errors.sql").read_text(encoding="utf-8")
    tooling = (tmp_path / "08-local-demo-tooling-preflight.sh").read_text(encoding="utf-8")
    troubleshooting_sql = (tmp_path / "09-db-troubleshooting-queries.sql").read_text(encoding="utf-8")
    datasafe_audit_sql = (tmp_path / "10-enable-datasafe-demo-audit.sql").read_text(encoding="utf-8")
    datasafe_verify_sql = (tmp_path / "11-verify-datasafe-demo-audit.sql").read_text(encoding="utf-8")
    monitoring_status_sql = (tmp_path / "12-check-monitoring-account-status.sql").read_text(encoding="utf-8")
    monitoring_recovery_sql = (tmp_path / "13-remediate-monitoring-account-lock.sql").read_text(encoding="utf-8")
    mcp_handoff = (tmp_path / "MCP-HANDOFF.md").read_text(encoding="utf-8")
    coordinator_readme = (tmp_path / "oci-coordinator-oke-integration" / "README.md").read_text(encoding="utf-8")
    coordinator_dashboard = json.loads(
        (tmp_path / "oci-coordinator-oke-integration" / "db-incident-logan-dashboard.json").read_text(encoding="utf-8")
    )
    coordinator_drilldowns = json.loads(
        (tmp_path / "oci-coordinator-oke-integration" / "db-incident-agent-drilldowns.json").read_text(encoding="utf-8")
    )
    coordinator_detection = json.loads(
        (tmp_path / "oci-coordinator-oke-integration" / "queries" / "db_incident_compilation_errors.json").read_text(encoding="utf-8")
    )
    coordinator_playbook = (tmp_path / "oci-coordinator-oke-integration" / "db-incident-playbook.yaml").read_text(encoding="utf-8")
    segregation = (tmp_path / "DEMO-SEGREGATION.md").read_text(encoding="utf-8")
    logan_queries = (tmp_path / "LOGAN-QUERIES.md").read_text(encoding="utf-8")
    runbook = (tmp_path / "RUNBOOK.md").read_text(encoding="utf-8")
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    targets = (tmp_path / "observability-demo-targets.yaml").read_text(encoding="utf-8")
    validator = (tmp_path / "validate-demo-packet.sh").read_text(encoding="utf-8")
    upload = (tmp_path / "upload-logan.sh").read_text(encoding="utf-8")

    assert 'SQL_CLIENT=(sqlplus -L -S)' in runner
    assert 'SQL_CLIENT=("$SCRIPT_DIR/.tools/sqlcl/bin/sql" -S)' in runner
    assert 'select_sqlcl_java' in runner
    assert "awk -F '\"' '/version/" in runner
    assert 'SQLcl requires Java 11+' in runner
    assert 'run_sql() { "${SQL_CLIENT[@]}" /nolog; }' in runner
    assert 'fail() {' in runner
    assert 'DB_INCIDENT_MCP_COMMAND is not set' in tooling
    assert 'run_sql() { "${SQL_CLIENT[@]}" /nolog; }' in sample_installer
    assert "C_GREEN" in runner
    assert "banner \"OCI DB Incident Observability Demo\"" in runner
    assert "step \"Creating disposable DBINC_LAB schema\"" in runner
    assert "NO_COLOR" in runner
    assert "DB_INCIDENT_ADMIN_CONNECT" in runner
    assert "PDB_SERVICE" in runner
    assert "LAB_EZCONNECT" in runner
    assert "DB_INCIDENT_DATASAFE_AUDIT_ENABLED" in runner
    assert "DB_INCIDENT_DATASAFE_AUDIT_FAILED_LOGIN_ENABLED" in runner
    assert "10-enable-datasafe-demo-audit.sql" in runner
    assert "11-verify-datasafe-demo-audit.sql" in runner
    assert "DBINC DEMO AUDIT PRIMER" in runner
    assert "Generated reviewed failed-login audit signal" in runner
    assert "tr '[:upper:]' '[:lower:]'" in runner
    assert 'DBINC_LAB/"${DB_INCIDENT_LAB_PASSWORD}"${LAB_EZCONNECT:+@${LAB_EZCONNECT}}' in runner
    assert "whenever oserror exit 1" in runner
    assert "whenever sqlerror exit sql.sqlcode" in runner
    assert "DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED" in runner
    assert "define LAB_PASSWORD = \"&1\"" in setup_sql
    assert "define PDB_NAME = \"&2\"" in setup_sql
    assert "alter session set container =" in setup_sql
    assert "grant create session, create table, create procedure, create sequence to DBINC_LAB" in setup_sql
    assert "DBINC DEMO: disposable lab schema setup" in setup_sql
    assert "create table incident_event_log" in workload_sql
    assert "module_name varchar2(64)" in workload_sql
    assert "client_identifier varchar2(128)" in workload_sql
    assert "dbms_session.set_identifier('&&SCENARIO_ID:&&LAB_ID')" in workload_sql
    assert "dbms_application_info.read_module" in workload_sql
    assert "dbinc_missing_orders" in workload_sql
    assert "broken_compile_demo" in workload_sql
    assert "show errors procedure broken_compile_demo" in workload_sql
    assert "from user_errors" in workload_sql
    assert "PLS" in workload_sql
    assert "DBINC DEMO: base workload and safe real ORA errors" in workload_sql
    assert "capture_expected_error" in workload_sql
    assert "attempt_parent_lock_nowait" in workload_sql
    assert "ORA-00054" not in workload_sql  # produced by Oracle, not faked in SQL text
    assert "from incident_event_log" in query_sql
    assert "DBINC DEMO: collected incident evidence timeline" in query_sql
    assert "DBINC DEMO: source coverage summary" in query_sql
    assert "DBINC DEMO: module/action troubleshooting context" in query_sql
    assert "sys.dbms_system.ksdwrt" in alert_sql
    assert "DBINC DEMO: optional alert-log synthetic markers" in alert_sql
    assert "synthetic=true ORA-00600 marker" in alert_sql
    assert "drop user DBINC_LAB cascade" in cleanup_sql
    assert "drop user HR cascade" in cleanup_sql
    assert "oracle-samples/db-sample-schemas" in sample_installer
    assert "step \"Downloading Oracle sample schemas\"" in sample_installer
    assert "ok \"Installed Oracle sample schemas HR and CO" in sample_installer
    assert "hr_install.sql" in sample_installer
    assert "co_install.sql" in sample_installer
    assert "DB_INCIDENT_WORK_DIR" in sample_installer
    assert '${TMPDIR:-/tmp}/db-incident-sample-schemas' in sample_installer
    assert "rewrite_install_script()" in sample_installer
    assert 'define pass = \\"${escaped_password}\\"' in sample_installer
    assert '.dbinc.sql' in sample_installer
    assert "grant select, insert on hr.employees to DBINC_LAB" in sample_installer
    assert "DB Incident Local Demo Tooling Preflight" in tooling
    assert "check_sqlcl" in tooling
    assert "DB_INCIDENT_TOOLING_INSTALL" in tooling
    assert "DB_INCIDENT_SQLCL_SHA256" in tooling
    assert "DB_INCIDENT_SQLCL_ARCHIVE" in tooling
    assert "unverified latest downloads are refused" in tooling
    assert "sqlcl-latest.zip" not in tooling
    assert ".tools" in tooling
    assert "DB_INCIDENT_MCP_COMMAND" in tooling
    assert "Jeff Smith/SQLcl MCP server" in tooling
    assert "DBINC TROUBLESHOOTING: invalid objects" in troubleshooting_sql
    assert "set serveroutput on" in troubleshooting_sql
    assert "from all_errors" in troubleshooting_sql
    assert "from all_dependencies" in troubleshooting_sql
    assert "from all_tab_privs" in troubleshooting_sql
    assert "from DBINC_LAB.incident_event_log" in troubleshooting_sql
    assert "from v$session" in troubleshooting_sql
    assert "Skipping V$SESSION lock drilldown" in troubleshooting_sql
    assert "Catalog privileges detected. Run the query below" in troubleshooting_sql
    assert "SELECT_CATALOG_ROLE" in troubleshooting_sql
    assert "SELECT ANY DICTIONARY" in troubleshooting_sql
    assert "actions logon" in datasafe_audit_sql
    assert "audit policy &&AUDIT_POLICY_NAME by DBINC_LAB" in datasafe_audit_sql
    assert "DBINC DEMO: configure Data Safe audit policy for DBINC_LAB" in datasafe_audit_sql
    assert "from unified_audit_trail" in datasafe_verify_sql
    assert "dbusername = 'DBINC_LAB'" in datasafe_verify_sql
    assert "LOOKBACK_MINUTES" in datasafe_verify_sql
    assert 'TZH:TZM' not in datasafe_verify_sql
    assert "DBINC DEMO: check monitoring account status" in monitoring_status_sql
    assert "from cdb_users" in monitoring_status_sql
    assert "FAILED_LOGIN_ATTEMPTS" in monitoring_status_sql
    assert "DBINC DEMO: remediate monitoring account lock loop" in monitoring_recovery_sql
    assert "account unlock container=all" in monitoring_recovery_sql
    assert "password_life_time unlimited" in monitoring_recovery_sql
    assert "DB Troubleshooting MCP Handoff" in mcp_handoff
    assert "DB_INCIDENT_MCP_COMMAND" in mcp_handoff
    assert "BROKEN_COMPILE_DEMO" in mcp_handoff
    assert "DB_INCIDENT_LAB_EZCONNECT" in mcp_handoff
    assert "DB_INCIDENT_PDB_NAME" in mcp_handoff
    assert "DB_INCIDENT_PDB_SERVICE" in mcp_handoff
    assert "OCI Coordinator OKE Integration Pack" in coordinator_readme
    assert coordinator_dashboard["_source"] == "oci-dbman-opsi"
    assert coordinator_dashboard["dashboards"][0]["name"] == "DB Incident Troubleshooting Overview"
    assert any(widget["title"] == "Compilation Diagnostics" for widget in coordinator_dashboard["dashboards"][0]["widgets"])
    assert any(widget["title"] == "Runbook Links" for widget in coordinator_dashboard["dashboards"][1]["widgets"])
    assert coordinator_drilldowns["agents"][0]["agent"] == "db-troubleshoot-agent"
    assert "oci_logan_build_db_incident_evidence" in coordinator_drilldowns["agents"][0]["tools"]
    assert coordinator_detection["title"] == "DB Incident PL/SQL Compilation Diagnostics"
    assert "runbook:09-db-troubleshooting-queries.sql" in coordinator_detection["tags"]
    assert "db-incident-observability-drilldown" in coordinator_playbook
    assert "expected_root_cause" in coordinator_playbook
    assert "oracle_sample_hr" in sample_errors
    assert "oracle_sample_co" in sample_errors
    assert "DBINC DEMO: Oracle HR/CO sample-schema errors" in sample_errors
    assert "dbms_session.set_identifier('DBINC_SAMPLE_SCHEMA_DEMO')" in sample_errors
    assert "order_tms" in sample_errors
    assert "not for production use" in segregation
    assert "Log Analytics Query Templates" in logan_queries
    assert "scenario_id=incident-1" in logan_queries
    assert "lab_id=lab-incident-1" in logan_queries
    assert "ORA-00600" in logan_queries
    assert "PL/SQL Compilation Diagnostics" in logan_queries
    assert "PLS-" in logan_queries
    assert "Source Coverage" in logan_queries
    assert "DB Incident Demo Runbook" in runbook
    assert "db-incident" in runbook
    assert "--profile <PROFILE> \\" in runbook
    assert "LOGAN-QUERIES.md" in runbook
    assert "manifest.json" in runbook
    assert "./validate-demo-packet.sh" in runbook
    assert "DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED=true" in runbook
    assert "Do not test bad passwords against the monitoring account" in runbook
    assert "@12-check-monitoring-account-status.sql DBSNMP" in runbook
    assert "@13-remediate-monitoring-account-lock.sql DBSNMP C##DBSNMP_MON" in runbook
    assert "Cleanup" in runbook
    assert manifest["scenario_id"] == "incident-1"
    assert manifest["lab_id"] == "lab-incident-1"
    assert manifest["production_use"] is False
    assert manifest["demo_only"] is True
    assert "manifest.json" in manifest["artifacts"]
    assert "08-local-demo-tooling-preflight.sh" in manifest["artifacts"]
    assert "09-db-troubleshooting-queries.sql" in manifest["artifacts"]
    assert "10-enable-datasafe-demo-audit.sql" in manifest["artifacts"]
    assert "11-verify-datasafe-demo-audit.sql" in manifest["artifacts"]
    assert "12-check-monitoring-account-status.sql" in manifest["artifacts"]
    assert "13-remediate-monitoring-account-lock.sql" in manifest["artifacts"]
    assert "MCP-HANDOFF.md" in manifest["artifacts"]
    assert "oci-coordinator-oke-integration/db-incident-logan-dashboard.json" in manifest["artifacts"]
    assert "oci-coordinator-oke-integration/queries/db_incident_compilation_errors.json" in manifest["artifacts"]
    assert manifest["segregation"]["full_demo_services"] == ["dbm", "opsi", "datasafe", "logan"]
    assert "module_name" in manifest["evidence_fields"]
    assert "client_identifier" in manifest["evidence_fields"]
    assert "ORA-00942" in manifest["expected_real_errors"]
    assert "ORA-06575" in manifest["expected_real_errors"]
    assert "PLS-00201" in manifest["expected_compiler_diagnostics"]
    assert manifest["optional_capabilities"]["db_troubleshooting_mcp"] == "DB_INCIDENT_MCP_COMMAND"
    assert manifest["optional_capabilities"]["datasafe_audit_primer"] == "DB_INCIDENT_DATASAFE_AUDIT_ENABLED"
    assert manifest["optional_capabilities"]["monitoring_account_recovery"] == "13-remediate-monitoring-account-lock.sql"
    assert "ORA-00600" in manifest["synthetic_markers"]
    assert "services: [dbm, opsi, datasafe, logan]" in targets
    assert "DB Incident Demo Packet Validation" in validator
    assert "This validation is local and non-destructive" in validator
    assert "manifest.json" in validator
    assert "08-local-demo-tooling-preflight.sh" in validator
    assert "09-db-troubleshooting-queries.sql" in validator
    assert "10-enable-datasafe-demo-audit.sql" in validator
    assert "11-verify-datasafe-demo-audit.sql" in validator
    assert "12-check-monitoring-account-status.sql" in validator
    assert "13-remediate-monitoring-account-lock.sql" in validator
    assert "MCP-HANDOFF.md" in validator
    assert "oci-coordinator-oke-integration/db-incident-logan-dashboard.json" in validator
    assert "check_manifest" in validator
    assert '"production_use": false' in validator
    assert "manifest safety metadata" in validator
    assert "require_executable validate-demo-packet.sh" in validator
    assert "check_shell validate-demo-packet.sh" in validator
    assert "check_command sqlplus" in validator
    assert "warn \"Log Analytics upload disabled" in upload
    subprocess.run(["bash", "-n", str(tmp_path / "run-db-incident-demo.sh")], check=True)
    subprocess.run(["bash", "-n", str(tmp_path / "06-install-oracle-sample-schemas.sh")], check=True)
    subprocess.run(["bash", "-n", str(tmp_path / "08-local-demo-tooling-preflight.sh")], check=True)
    subprocess.run(["bash", "-n", str(tmp_path / "validate-demo-packet.sh")], check=True)
    subprocess.run(["bash", "-n", str(tmp_path / "upload-logan.sh")], check=True)
    result = subprocess.run(
        [str(tmp_path / "validate-demo-packet.sh")],
        check=True,
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )
    assert "packet validation complete" in result.stdout
    for script_name in [
        "run-db-incident-demo.sh",
        "06-install-oracle-sample-schemas.sh",
        "08-local-demo-tooling-preflight.sh",
        "validate-demo-packet.sh",
        "upload-logan.sh",
    ]:
        assert stat.S_IMODE((tmp_path / script_name).stat().st_mode) == 0o700


def test_route_db_incident_question_detects_ora_and_alert_log_questions() -> None:
    assert route_db_incident_question("What happened around ORA-00600 on ORDERSDB?")
    assert route_db_incident_question("Correlate database alert log events")
    assert route_db_incident_question("Why did SHOW ERRORS return PLS-00201?")
    assert route_db_incident_question("Find invalid object compilation error evidence")
    assert not route_db_incident_question("Show capacity trend for the fleet")


def test_packet_local_sqlcl_rejects_an_archive_with_the_wrong_checksum(tmp_path: Path) -> None:
    generate_db_incident_demo(tmp_path, apply=True, scenario_id="checksum-test")
    archive = tmp_path / "unverified-sqlcl.zip"
    archive.write_bytes(b"not a SQLcl archive")
    environment = {
        **os.environ,
        "DB_INCIDENT_TOOLING_INSTALL": "true",
        "DB_INCIDENT_SQLCL_ARCHIVE": str(archive),
        "DB_INCIDENT_SQLCL_SHA256": "0" * 64,
        "DB_INCIDENT_MCP_COMMAND": "true",
    }

    result = subprocess.run(
        [str(tmp_path / "08-local-demo-tooling-preflight.sh")],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "SQLcl archive checksum mismatch; archive removed." in result.stdout
    assert not list((tmp_path / ".tools").glob("sqlcl-*.zip"))


def test_mcp_shaped_builder_returns_bounded_json() -> None:
    payload = oci_logan_build_db_incident_evidence(
        ora_code="ora-07445",
        database_name="ORDERSDB",
        compartment_id="ocid" + "1.compartment.oc1..aaaaaaaa",
        include_sources=("logan",),
        limit=1,
        oci=FakeIncidentOci(),
    )

    assert payload["request"]["ora_code"] == "ORA-07445"
    assert len(payload["timeline"]) == 1
    assert "not a root cause by itself" in payload["uncertainty"]
