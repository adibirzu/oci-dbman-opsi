import pytest

from dbman_opsi.sqlcl_mcp import (
    SqlclMcpConfig,
    SqlclMcpError,
    build_sqlcl_mcp_config,
    is_read_only_sql,
)


def test_read_only_sql_allows_select_and_explain_plan() -> None:
    assert is_read_only_sql("select * from dbinc_lab.incident_event_log")
    assert is_read_only_sql("  EXPLAIN PLAN FOR SELECT 1 FROM dual")


@pytest.mark.parametrize(
    "statement",
    [
        "insert into dbinc_lab.incident_event_log values (1)",
        "select 1 from dual; delete from dbinc_lab.incident_event_log",
        "begin null; end;",
        "alter session set current_schema = DBINC_LAB",
    ],
)
def test_read_only_sql_rejects_writes_and_multi_statement_input(statement: str) -> None:
    assert not is_read_only_sql(statement)


def test_mcp_config_has_no_credentials_and_uses_vault_reference() -> None:
    config = build_sqlcl_mcp_config(
        SqlclMcpConfig(
            name="demo-readonly",
            connect_descriptor="<ADB_CONNECT_DESCRIPTOR>",
            secret_id="ocid1.secret.oc1..example",
            username="MCP_READONLY",
        )
    )

    assert config["mcpServers"]["demo-readonly"]["command"] == "sql"
    env = config["mcpServers"]["demo-readonly"]["env"]
    assert env["DBMAN_OPSI_SECRET_ID"] == "ocid1.secret.oc1..example"
    assert "password" not in str(config).lower()
    assert "DBMAN_OPSI_SECRET_VALUE" not in env


def test_mcp_config_rejects_non_readonly_database_user() -> None:
    with pytest.raises(SqlclMcpError, match="MCP_READONLY"):
        build_sqlcl_mcp_config(
            SqlclMcpConfig(
                name="unsafe",
                connect_descriptor="<CONNECT_DESCRIPTOR>",
                secret_id="ocid1.secret.oc1..example",
                username="SYSTEM",
            )
        )
