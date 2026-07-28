import json

from dbman_opsi.disposable_release import (
    build_release_evidence,
    generate_dashboard_definitions,
    generate_role_bootstrap_sql,
)


def test_role_bootstrap_creates_all_dedicated_identities_without_passwords() -> None:
    sql = generate_role_bootstrap_sql("demo")

    for role in ("DBM_MON", "DATASAFE_AUDIT", "MCP_READONLY", "DBINC_LAB"):
        assert f"accept {role.lower()}_password char hide" in sql
        assert f"grant create session to {role}" in sql
    assert "grant dba" not in sql.lower()
    assert "identified by \"example" not in sql.lower()


def test_dashboard_definitions_cover_the_six_release_panels() -> None:
    dashboards = generate_dashboard_definitions("demo-2026")

    assert set(dashboards) == {
        "db-health",
        "opsi-capacity",
        "datasafe-audit",
        "incident-timeline",
        "credential-lifecycle",
        "teardown-evidence",
    }
    assert all("demo-2026" in json.dumps(payload) for payload in dashboards.values())


def test_release_evidence_is_sanitized_and_requires_every_phase() -> None:
    sensitive_value = "A" * 40
    evidence = build_release_evidence(
        lifecycle_id="demo-2026",
        phases={
            "provision": "passed",
            "vault_bootstrap": "passed",
            "user_bootstrap": "passed",
            "mcp_check": "passed",
            "incident_scenario": "passed",
            "observability": "passed",
            "teardown": "passed",
        },
        details={"secret": sensitive_value, "endpoint": "db.example.oraclecloud.com"},
    )

    assert evidence["verdict"] == "passed"
    assert sensitive_value not in json.dumps(evidence)
    assert "db.example.oraclecloud.com" not in json.dumps(evidence)
