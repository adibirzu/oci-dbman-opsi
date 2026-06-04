from pathlib import Path

from dbman_opsi.config import EnablementConfig, Target
from dbman_opsi.db_scripts import generate_db_scripts, monitoring_user_sql, validation_sql


def test_monitoring_user_sql_prompts_for_password_and_container() -> None:
    target = Target(kind="dbcs", name="db1", service_name="PDB1", monitoring_user="DBSNMP")

    sql = monitoring_user_sql(target)

    assert "accept monitoring_password char hide" in sql
    assert "alter session set container" in sql
    assert '"&monitoring_password"' not in sql


def test_validation_sql_checks_expected_grants() -> None:
    sql = validation_sql(Target(kind="exadata", name="exa", monitoring_user="DBSNMP"))

    assert "SELECT ANY DICTIONARY" in sql
    assert "SELECT_CATALOG_ROLE" in sql
    assert "DBMS_MONITOR" in sql


def test_generate_db_scripts_for_dbcs_and_exadata_only(tmp_path: Path) -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        targets=(
            Target(kind="dbcs", name="cloud db"),
            Target(kind="exadata", name="exa db"),
            Target(kind="autonomous", name="adb"),
        ),
    )

    paths = generate_db_scripts(config, tmp_path)

    names = {path.name for path in paths}
    assert "01-create-monitoring-user.sql" in names
    assert "04-validate-monitoring-user.sql" in names
    assert (tmp_path / "cloud-db").exists()
    assert (tmp_path / "exa-db").exists()
    assert not (tmp_path / "adb").exists()
