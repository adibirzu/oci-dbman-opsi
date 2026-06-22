from pathlib import Path

from dbman_opsi.config import EnablementConfig, NetworkSelection, Target
from dbman_opsi.opsi_diagnostics import (
    db_readiness_sql,
    generate_opsi_diagnostics,
    login_probe_sql,
    oci_control_plane_script,
    readme_text,
)


def _ocid(resource_type: str, suffix: str = "a") -> str:
    return "ocid1" + f".{resource_type}.oc1.." + (suffix * 16)


def test_oci_control_plane_script_checks_failed_opsi_and_work_requests() -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        tenancy_id=_ocid("tenancy", "t"),
        compartment_id=_ocid("compartment", "c"),
        network=NetworkSelection(vcn_id=_ocid("vcn", "v"), subnet_id=_ocid("subnet", "s")),
    )
    target = Target(
        kind="dbcs",
        name="cloud db",
        compartment_id=_ocid("compartment", "d"),
        resource_id=_ocid("database", "a"),
        private_endpoint_id=_ocid("privateendpoint", "p"),
        opsi_private_endpoint_id=_ocid("opsiprivateendpoint", "o"),
        password_secret_id=_ocid("secret", "x"),
        service_name="PDB1.example",
        monitoring_user="DBSNMP",
        opsi_database_insight_id=_ocid("opsidatabaseinsight", "i"),
    )

    script = oci_control_plane_script(config, target)

    assert "database-management managed-database get" in script
    assert "opsi-insights-FAILED" in script
    assert "opsi work-requests list" in script
    assert "service dpd" in script
    assert "service operations-insights" in script
    assert "vault secret get" in script
    assert 'REQUESTED_OCI_AUTH="${DBMAN_OPSI_OCI_AUTH:-}"' in script
    assert '--auth "$REQUESTED_OCI_AUTH"' in script
    assert "fetch_subnet_security" in script
    assert "subnet-route-table" in script
    assert "subnet-security-list-${index}" in script
    assert "fetch_endpoint_nsgs" in script
    assert "network nsg get --nsg-id" in script
    assert "fetch_opsi_work_request_details" in script
    assert "opsi work-requests list" in script
    assert "opsi work-requests get" in script
    assert "No mutating OCI commands" in script


def test_db_readiness_sql_checks_service_user_grants_and_awr_state() -> None:
    sql = db_readiness_sql(
        Target(kind="dbcs", name="cloud db", service_name="PDB1.example", monitoring_user="DBSNMP")
    )

    assert "cdb_services" in sql
    assert "dba_users" in sql
    assert "CREATE SESSION" in sql
    assert "ADMINISTER SQL TUNING SET" in sql
    assert "DBMS_WORKLOAD_REPOSITORY" in sql
    assert "awr_pdb_autoflush_enabled" in sql


def test_login_probe_uses_same_service_and_vault_password_without_storing_it() -> None:
    sql = login_probe_sql(
        Target(kind="dbcs", name="cloud db", service_name="PDB1.example", monitoring_user="DBSNMP")
    )

    assert "accept monitoring_password char hide" in sql
    assert 'connect &monitoring_user/"&monitoring_password"@&connect_identifier' in sql
    assert "can_read_gv_session" in sql
    assert "PDB1.example" in sql


def test_generate_opsi_diagnostics_writes_packet_for_dbcs_and_exadata_only(tmp_path: Path) -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        targets=(
            Target(kind="dbcs", name="cloud db", service_name="PDB1"),
            Target(kind="exadata", name="exa db", service_name="PDB2"),
            Target(kind="autonomous", name="adb", services=("dbm", "opsi")),
            Target(kind="dbcs", name="dbm only", services=("dbm",), service_name="PDB3"),
        ),
    )

    paths = generate_opsi_diagnostics(config, tmp_path)

    assert tmp_path.joinpath("cloud-db", "00-oci-control-plane-diagnostics.sh").exists()
    assert tmp_path.joinpath("cloud-db", "01-diagnose-opsi-db-readiness.sql").exists()
    assert tmp_path.joinpath("cloud-db", "02-test-opsi-login.sql").exists()
    assert tmp_path.joinpath("exa-db", "README.md").exists()
    assert not tmp_path.joinpath("adb").exists()
    assert not tmp_path.joinpath("dbm-only").exists()
    assert any(path.name == "00-oci-control-plane-diagnostics.sh" for path in paths)
    assert tmp_path.joinpath("cloud-db", "00-oci-control-plane-diagnostics.sh").stat().st_mode & 0o111


def test_readme_gives_copy_paste_steps_and_fix_map() -> None:
    readme = readme_text(Target(kind="dbcs", name="cloud db", service_name="PDB1"))

    assert "./00-oci-control-plane-diagnostics.sh ./out" in readme
    assert "@01-diagnose-opsi-db-readiness.sql" in readme
    assert "@02-test-opsi-login.sql" in readme
    assert "dbman-opsi preflight --config <config> --db-check-file opsi-db-readiness.log" in readme
    assert "dbman-opsi enable --config <config> --apply --force-reconcile" in readme
    assert "ORA-01017/ORA-28000/ORA-28001" in readme
    assert "ORA-12514/ORA-12154" in readme
