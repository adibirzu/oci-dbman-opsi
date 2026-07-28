import json
from pathlib import Path

from dbman_opsi.config import EnablementConfig, LogAnalyticsSelection, Target
from dbman_opsi.log_analytics import (
    LogAnalyticsDecision,
    LogAnalyticsService,
    association_payload,
    canonical_source_name,
    generate_logan_payloads,
)


def test_association_payload_includes_source_properties_without_secrets() -> None:
    target = Target(
        kind="autonomous",
        name="adb",
        service_name="adb_low",
        logan_adb_entity_id="ocid" + "1.loganalyticsentity.oc1..aaaaaaaa",
    )

    payload = association_payload(target, "Oracle Database Unified Audit Logs", "entity-id", "group-id")

    assert payload["sourceName"] == "unifieddbauditlogfromdbsource122"
    properties = {item["name"]: item["value"] for item in payload["associationProperties"]}
    assert properties["SERVICE_NAME"] == "adb_low"
    assert properties["CREDENTIAL_NAME"] == "DBTCPSCreds"
    assert "password" not in json.dumps(payload).lower()


def test_generate_logan_payloads_writes_scripts_payloads_and_templates(tmp_path: Path) -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        targets=(
            Target(
                kind="autonomous",
                name="Demo ADB",
                services=("dbm", "opsi", "logan"),
                logan_sources=("Oracle Database Unified Audit Logs",),
                logan_adb_service_name="demo_low",
            ),
        ),
    )

    paths = generate_logan_payloads(config, tmp_path)

    assert tmp_path.joinpath("demo-adb", "00-discover-logan-host-facts.sh") in paths
    assert tmp_path.joinpath("demo-adb", "01-grant-logan-log-acls.sh") in paths
    assert tmp_path.joinpath("demo-adb", "02-create-logan-db-user.sql") in paths
    credential = json.loads(tmp_path.joinpath("demo-adb", "credential-template.json").read_text(encoding="utf-8"))
    assert credential["password"] == "${DBMAN_LOGAN_DB_PASSWORD}"
    association_items = json.loads(
        tmp_path.joinpath("demo-adb", "associations", "unifieddbauditlogfromdbsource122.json").read_text(
            encoding="utf-8"
        )
    )
    properties = {item["name"]: item["value"] for item in association_items[0]["associationProperties"]}
    assert properties["SERVICE_NAME"] == "demo_low"


def test_generate_logan_payloads_include_management_agent_packet_for_dbcs(tmp_path: Path) -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="ocid" + "1.compartment.oc1..aaaaaaaa",
        targets=(
            Target(
                kind="dbcs",
                name="Demo DBCS",
                service_name="db_high",
                services=("dbm", "opsi", "logan"),
                logan_hostname="dbhost.example.internal",
            ),
        ),
    )

    paths = generate_logan_payloads(config, tmp_path)

    assert tmp_path.joinpath("demo-dbcs", "03-create-logan-management-agent-install-key.sh") in paths
    assert tmp_path.joinpath("demo-dbcs", "04-install-logan-management-agent.sh") in paths
    assert tmp_path.joinpath("demo-dbcs", "05-verify-logan-management-agent.sh") in paths
    assert tmp_path.joinpath("demo-dbcs", "06-resolve-logan-management-agent.sh") in paths
    assert tmp_path.joinpath("demo-dbcs", "07-bootstrap-logan-management-agent-ansible.sh") in paths
    assert tmp_path.joinpath("demo-dbcs", "08-run-logan-management-agent-ansible.sh") in paths
    assert tmp_path.joinpath("demo-dbcs", "09-logan-management-agent-playbook.yml") in paths
    assert tmp_path.joinpath("demo-dbcs", "10-logan-management-agent-ansible.cfg") in paths
    assert tmp_path.joinpath("demo-dbcs", "11-resolve-logan-management-agent-package-url.sh") in paths
    install = tmp_path.joinpath("demo-dbcs", "04-install-logan-management-agent.sh").read_text(encoding="utf-8")
    assert "Service.plugin.logan.download=true" in install
    assert "AGENT_RPM_URL" in install
    assert "ensure_java8" in install
    assert "JAVA_HOME" in install
    assert "OK" in install
    verify = tmp_path.joinpath("demo-dbcs", "05-verify-logan-management-agent.sh").read_text(encoding="utf-8")
    assert "systemctl status mgmt_agent" in verify
    assert "mgmt_agent_logan.log" in verify
    resolve = tmp_path.joinpath("demo-dbcs", "06-resolve-logan-management-agent.sh").read_text(encoding="utf-8")
    assert "logan_management_agent_id" in resolve
    install_key = tmp_path.joinpath("demo-dbcs", "03-create-logan-management-agent-install-key.sh").read_text(
        encoding="utf-8"
    )
    assert "--install-key-id" in install_key
    assert "--management-agent-install-key-id" in install_key
    runner = tmp_path.joinpath("demo-dbcs", "08-run-logan-management-agent-ansible.sh").read_text(encoding="utf-8")
    assert "ProxyJump" in runner
    assert "AGENT_RPM_URL" in runner
    assert "AGENT_RPM_SHA256" in runner
    assert 'agent_rpm_sha256=$AGENT_RPM_SHA256' in runner
    playbook = tmp_path.joinpath("demo-dbcs", "09-logan-management-agent-playbook.yml").read_text(encoding="utf-8")
    assert "Copy Management Agent RPM" in playbook
    assert "Download Management Agent RPM on target host" in playbook
    assert 'checksum: "sha256:{{ agent_rpm_sha256 }}"' in playbook
    assert "Run generated Management Agent installer" in playbook
    package = tmp_path.joinpath("demo-dbcs", "11-resolve-logan-management-agent-package-url.sh").read_text(
        encoding="utf-8"
    )
    assert "agent-image list" in package
    assert "AGENT_RPM_URL=" in package
    assert "oci --profile \"$PROFILE\" --region \"$REGION\" os object get" in package
    assert "AGENT_RPM_OBJECT_NAMESPACE=" in package
    assert "DOWNLOAD_AGENT" in package


def test_canonical_source_name_normalizes_legacy_display_names() -> None:
    assert canonical_source_name("Oracle Database Alert Logs") == "DBAlertLogSource"
    assert canonical_source_name("Linux Syslog Logs") == "LinuxSyslogSource"
    assert canonical_source_name("Database Audit Logs") == "DBAuditLogSource"


class FakeOci:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.runner = type("Runner", (), {"dry_run": False})()

    def get_log_analytics_namespace(self, compartment_id):
        self.calls.append(("namespace", compartment_id))
        return "logan-namespace"

    def create_log_analytics_log_group(self, namespace, compartment_id, display_name):
        self.calls.append(("log_group", (namespace, compartment_id, display_name)))
        return "ocid" + "1.loganalyticsloggroup.oc1..aaaaaaaa"

    def create_log_analytics_entity(
        self,
        namespace,
        compartment_id,
        name,
        entity_type_name,
        properties_file=None,
        cloud_resource_id=None,
        hostname=None,
        agent_id=None,
    ):
        self.calls.append(
            (
                "entity",
                (namespace, compartment_id, name, entity_type_name, properties_file, cloud_resource_id, hostname, agent_id),
            )
        )
        if entity_type_name == "Host (Linux)":
            return "ocid" + "1.loganalyticsentity.oc1..hostaaaa"
        return "ocid" + "1.loganalyticsentity.oc1..dbaaaaaa"

    def upsert_log_analytics_associations(self, namespace, compartment_id, items):
        self.calls.append(("associations", (namespace, compartment_id, items)))

    def list_log_analytics_entity_source_associations(self, namespace, compartment_id, entity_id):
        self.calls.append(("existing_associations", (namespace, compartment_id, entity_id)))
        return []

    def upsert_log_analytics_association(self, namespace, compartment_id, payload_file):
        self.calls.append(("association", (namespace, compartment_id, payload_file)))


def test_log_analytics_service_dry_run_flow_uses_payload_files(tmp_path: Path) -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="ocid" + "1.compartment.oc1..aaaaaaaa",
        targets=(
            Target(
                kind="dbcs",
                name="dbcs",
                service_name="db_high",
                resource_id="ocid" + "1.database.oc1..aaaaaaaa",
                services=("dbm", "opsi", "logan"),
                logan_sources=("Linux Syslog Logs",),
                management_agent_id="ocid" + "1.managementagent.oc1..aaaaaaaa",
            ),
        ),
    )
    oci = FakeOci()

    decisions = LogAnalyticsService(oci).enable_all(config, payload_dir=tmp_path)

    assert decisions[-1].status == "configured"
    assert any(call[0] == "associations" for call in oci.calls)
    assert any(call[0] == "entity" for call in oci.calls)
    entity_types = [call[1][3] for call in oci.calls if call[0] == "entity"]
    assert entity_types == ["Host (Linux)"]


def test_log_analytics_service_blocks_non_autonomous_targets_without_agent_or_entities(tmp_path: Path) -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="ocid" + "1.compartment.oc1..aaaaaaaa",
        targets=(
            Target(
                kind="dbcs",
                name="dbcs",
                service_name="db_high",
                services=("dbm", "opsi", "logan"),
                logan_sources=("Linux Syslog Logs",),
            ),
        ),
    )
    oci = FakeOci()

    decisions = LogAnalyticsService(oci).enable_all(config, payload_dir=tmp_path)

    assert decisions[-1].status == "blocked"
    assert "Management Agent" in decisions[-1].detail


def test_validation_findings_escape_target_name_in_log_analytics_query() -> None:
    class ValidationOci:
        def __init__(self) -> None:
            self.query = ""

        def list_log_analytics_warnings(self, namespace, compartment_id):
            return []

        def search_log_analytics(self, namespace, query):
            self.query = query
            return {"count": 0}

    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="ocid" + "1.compartment.oc1..aaaaaaaa",
        log_analytics=LogAnalyticsSelection(namespace="logan-namespace"),
        targets=(
            Target(
                kind="autonomous",
                name="orders' | head 500 | 'db",
                services=("logan",),
            ),
        ),
    )
    oci = ValidationOci()

    LogAnalyticsService(oci).validation_findings(config)

    assert "Entity = 'orders'' | head 500 | ''db'" in oci.query


def test_log_analytics_dry_run_uses_placeholder_group_and_plans_associations(tmp_path: Path) -> None:
    class DryRunOci(FakeOci):
        def __init__(self) -> None:
            super().__init__()
            self.runner = type("Runner", (), {"dry_run": True})()

        def get_log_analytics_namespace(self, compartment_id):
            return ""

        def create_log_analytics_log_group(self, namespace, compartment_id, display_name):
            self.calls.append(("log_group", (namespace, compartment_id, display_name)))
            return ""

    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="ocid" + "1.compartment.oc1..aaaaaaaa",
        targets=(
            Target(
                kind="dbcs",
                name="dbcs",
                service_name="db_high",
                services=("logan",),
                logan_sources=("Linux Syslog Logs",),
                management_agent_id="ocid" + "1.managementagent.oc1..aaaaaaaa",
            ),
        ),
    )
    oci = DryRunOci()

    decisions = LogAnalyticsService(oci).enable_all(config, payload_dir=tmp_path)

    assert decisions[-1].status == "configured"
    assert "(1 sources)" in decisions[-1].detail
    assert any(call[0] == "associations" for call in oci.calls)


def test_log_analytics_blocks_when_no_log_group_can_be_resolved(tmp_path: Path) -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="ocid" + "1.compartment.oc1..aaaaaaaa",
        log_analytics=LogAnalyticsSelection(
            namespace="logan-namespace",
            create_log_group=False,
        ),
        targets=(
            Target(
                kind="autonomous",
                name="adb",
                services=("logan",),
                logan_adb_entity_id="ocid" + "1.loganalyticsentity.oc1..aaaaaaaa",
            ),
        ),
    )
    oci = FakeOci()

    decisions = LogAnalyticsService(oci).enable_all(config, payload_dir=tmp_path)

    assert decisions == [
        LogAnalyticsDecision(
            "tenancy",
            "blocked",
            "Log Analytics log_group_id is not set and log-group creation is disabled or failed",
        )
    ]
    assert not any(call[0] == "associations" for call in oci.calls)
