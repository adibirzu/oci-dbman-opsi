from urllib.parse import parse_qs, urlsplit

from dbman_opsi.oci_cli import OciCli
from dbman_opsi.runner import CommandResult, OciError


class FakeRunner:
    def __init__(self, payload):
        self.payload = payload
        self.commands = []
        self.retry_flags = []

    def run(self, args, cwd=None, check=True, retry_on_transient=False):
        self.commands.append(args)
        self.retry_flags.append(retry_on_transient)
        return CommandResult(tuple(args), self.payload, "", 0)


def test_oci_cli_adds_profile_region_and_json_output() -> None:
    runner = FakeRunner('{"data": [{"id": "vcn-id"}]}')
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    vcns = oci.list_vcns("compartment-id")

    assert vcns == [{"id": "vcn-id"}]
    assert runner.commands[0][:5] == ["oci", "--profile", "DEFAULT", "--region", "eu-frankfurt-1"]
    assert runner.commands[0][-2:] == ["--output", "json"]
    assert runner.retry_flags == [True]


def test_oci_cli_can_use_instance_principal_auth(monkeypatch) -> None:
    runner = FakeRunner('{"data": [{"id": "vcn-id"}]}')
    monkeypatch.setenv("DBMAN_OPSI_OCI_AUTH", "instance_principal")
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    oci.list_vcns("compartment-id")

    assert runner.commands[0][:5] == ["oci", "--region", "eu-frankfurt-1", "--auth", "instance_principal"]
    assert "--profile" not in runner.commands[0]


def test_oci_cli_reads_profile_tenancy_from_config(tmp_path, monkeypatch) -> None:
    config = tmp_path / "oci-config"
    config.write_text("[cap]\ntenancy = tenancy-id\n", encoding="utf-8")
    monkeypatch.setenv("OCI_CONFIG_FILE", str(config))
    oci = OciCli("cap", "eu-frankfurt-1", FakeRunner("{}"))  # type: ignore[arg-type]

    assert oci.profile_tenancy() == "tenancy-id"


def test_oci_cli_lists_known_resource_types() -> None:
    runner = FakeRunner('{"data": []}')
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    assert oci.list_compartments("tenancy-id") == []
    assert oci.list_subscribed_regions("tenancy-id") == []
    assert oci.list_subnets("compartment-id", "vcn-id") == []
    assert oci.list_db_systems("compartment-id") == []
    assert oci.list_databases("compartment-id", "db-system-id") == []
    assert oci.get_database("database-id") == {}
    assert oci.list_autonomous_databases("compartment-id") == []
    assert oci.get_autonomous_database("autonomous-database-id") == {}
    assert oci.list_exadata_infrastructure("compartment-id") == []
    assert oci.list_management_agents("compartment-id") == []
    assert oci.list_vaults("compartment-id") == []
    assert oci.list_secrets("compartment-id") == []
    assert oci.list_db_management_private_endpoints("compartment-id") == []
    assert oci.list_opsi_private_endpoints("compartment-id") == []


def test_region_subscription_list_uses_tenancy_id_not_compartment_id() -> None:
    runner = FakeRunner('{"data": []}')
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    assert oci.list_subscribed_regions("tenancy-id") == []

    command = runner.commands[0]
    assert command[5:8] == ["iam", "region-subscription", "list"]
    assert ["--tenancy-id", "tenancy-id"] == command[8:10]
    assert "--compartment-id" not in command
    assert "--all" in command


class _PagedDatabaseRunner:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.commands: list[list[str]] = []

    def run(self, args, cwd=None, check=True, retry_on_transient=False):
        self.commands.append(args)
        return CommandResult(tuple(args), next(self.responses), "", 0)


def _database_page_query(command: list[str]) -> dict[str, list[str]]:
    assert command[:5] == ["oci", "--profile", "DEFAULT", "--region", "eu-frankfurt-1"]
    assert command[5:9] == ["raw-request", "--http-method", "GET", "--target-uri"]
    assert command[-2:] == ["--output", "json"]
    assert "--all" not in command
    assert "--page" not in command
    assert "--page-token" not in command
    assert "--db-home-id" not in command

    target = urlsplit(command[9])
    assert target.scheme == "https"
    assert target.netloc == "database.eu-frankfurt-1.oraclecloud.com"
    assert target.path == "/20160918/databases"
    return parse_qs(target.query)


def test_oci_cli_database_db_home_route_follows_all_pages_with_stable_deduplication() -> None:
    # Break caught: returning only the first page omits database-c from discovery.
    runner = _PagedDatabaseRunner([
        '{"data": [{"id": "database-b"}, {"id": "database-a"}], "headers": {"opc-next-page": "page-2"}}',
        '{"data": [{"id": "database-a"}, {"id": "database-c"}], "headers": {}}',
    ])
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    databases = oci.list_databases_for_db_home("compartment-id", "db-home-id")

    assert [database["id"] for database in databases] == ["database-b", "database-a", "database-c"]
    assert len(runner.commands) == 2
    assert _database_page_query(runner.commands[0]) == {
        "compartmentId": ["compartment-id"],
        "dbHomeId": ["db-home-id"],
    }
    assert _database_page_query(runner.commands[1]) == {
        "compartmentId": ["compartment-id"],
        "dbHomeId": ["db-home-id"],
        "page": ["page-2"],
    }


def test_oci_cli_lists_all_db_homes_per_system_and_exadata_pages() -> None:
    runner = FakeRunner('{"data": []}')
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    assert oci.list_db_homes("compartment-id", db_system_id="db-system-id") == []
    assert "--db-system-id" in runner.commands[0]
    assert "--all" in runner.commands[0]

    assert oci.list_exadata_infrastructure("compartment-id") == []
    assert "--all" in runner.commands[1]


def test_oci_cli_db_home_topology_and_database_list_reject_invalid_parent_shapes() -> None:
    class _DbHomeRunner:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def run(self, args, **_kwargs):
            self.commands.append(args)
            if args[5:8] == ["db", "db-home", "list"]:
                assert "--db-system-id" in args
                assert "--vm-cluster-id" not in args
                payload = '{"data": [{"id": "home-a"}, {"id": "home-b"}]}'
            else:
                query = _database_page_query(args)
                assert query["compartmentId"] == ["compartment-id"]
                assert query["dbHomeId"][0] in {"home-a", "home-b"}
                payload = '{"data": [{"id": "database-system"}], "headers": {}}'
            return CommandResult(tuple(args), payload, "", 0)

    runner = _DbHomeRunner()
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]
    assert oci.list_db_homes("compartment-id", db_system_id="system-id") == [{"id": "home-a"}, {"id": "home-b"}]
    databases = oci.list_databases("compartment-id", "system-id")

    assert [database["id"] for database in databases] == ["database-system"]
    assert "--all" in runner.commands[0]


def test_oci_cli_database_home_route_handles_exadata_pages_with_stable_deduplication() -> None:
    # Break caught: returning only the first page omits exadata-database-c.
    runner = _PagedDatabaseRunner([
        '{"data": [{"id": "exadata-database-b"}, {"id": "exadata-database-a"}], "headers": {"OPC-Next-Page": "page-2"}}',
        '{"data": [{"id": "exadata-database-a"}, {"id": "exadata-database-c"}], "headers": {}}',
    ])
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    databases = oci.list_databases_for_db_home("compartment-id", "exadata-home-id")

    assert [database["id"] for database in databases] == [
        "exadata-database-b", "exadata-database-a", "exadata-database-c"
    ]
    assert len(runner.commands) == 2
    assert _database_page_query(runner.commands[0]) == {
        "compartmentId": ["compartment-id"],
        "dbHomeId": ["exadata-home-id"],
    }
    assert _database_page_query(runner.commands[1]) == {
        "compartmentId": ["compartment-id"],
        "dbHomeId": ["exadata-home-id"],
        "page": ["page-2"],
    }


def test_oci_cli_vm_cluster_database_route_enumerates_db_homes() -> None:
    runner = _PagedDatabaseRunner([
        '{"data": [{"id": "exadata-home"}]}',
        '{"data": [{"id": "exadata-database"}], "headers": {}}',
    ])
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    databases = oci.list_databases_for_vm_cluster("compartment-id", "vm-cluster-id")

    assert databases == [{"id": "exadata-database"}]
    assert "--vm-cluster-id" in runner.commands[0]
    assert "--all" in runner.commands[0]
    assert _database_page_query(runner.commands[1]) == {
        "compartmentId": ["compartment-id"],
        "dbHomeId": ["exadata-home"],
    }


def test_oci_cli_compartment_list_requests_all_pages() -> None:
    runner = FakeRunner('{"data": []}')
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    assert oci.list_compartments("tenancy-id") == []
    assert "--all" in runner.commands[0]
    assert ["--lifecycle-state", "ACTIVE"] == runner.commands[0][
        runner.commands[0].index("--lifecycle-state") : runner.commands[0].index("--lifecycle-state") + 2
    ]


def test_log_analytics_associated_entity_list_requests_all_pages() -> None:
    runner = FakeRunner('{"data": []}')
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    assert oci.list_log_analytics_associated_entities("namespace", "compartment-id") == []
    command = runner.commands[0]
    assert command[5:8] == ["log-analytics", "assoc", "list-associated-entities"]
    assert "--all" in command


def test_log_analytics_entity_source_association_list_requests_all_pages_for_exact_entity() -> None:
    runner = FakeRunner('{"data": {"items": [{"sourceName": "DBAlertLogSource", "entityId": "entity"}]}}')
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    assert oci.list_log_analytics_entity_source_associations("namespace", "compartment-id", "entity") == [
        {"sourceName": "DBAlertLogSource", "entityId": "entity"}
    ]

    command = runner.commands[0]
    assert command[5:8] == ["log-analytics", "assoc", "list-entity-source-assocs"]
    assert ["--entity-id", "entity"] == command[command.index("--entity-id"):command.index("--entity-id") + 2]
    assert "--all" in command


def test_oci_cli_extracts_nested_items_response() -> None:
    runner = FakeRunner('{"data": {"items": [{"name": "pe"}]}}')
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    assert oci.list_db_management_private_endpoints("compartment-id") == [{"name": "pe"}]


def test_oci_cli_get_methods_unwrap_data() -> None:
    runner = FakeRunner('{"data": {"lifecycle-state": "ACTIVE"}}')
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    assert oci.get_subnet("subnet-id") == {"lifecycle-state": "ACTIVE"}
    assert oci.get_vcn("vcn-id") == {"lifecycle-state": "ACTIVE"}
    assert oci.get_route_table("rt-id") == {"lifecycle-state": "ACTIVE"}
    assert oci.get_security_list("sl-id") == {"lifecycle-state": "ACTIVE"}
    assert oci.get_db_system("db-system-id") == {"lifecycle-state": "ACTIVE"}
    assert oci.get_db_management_private_endpoint("pe-id") == {"lifecycle-state": "ACTIVE"}
    assert oci.get_opsi_private_endpoint("pe-id") == {"lifecycle-state": "ACTIVE"}
    assert oci.get_secret("secret-id") == {"lifecycle-state": "ACTIVE"}
    assert oci.get_group("group-id") == {"lifecycle-state": "ACTIVE"}
    assert oci.get_management_agent("agent-id") == {"lifecycle-state": "ACTIVE"}


def test_oci_cli_list_methods_use_expected_verbs() -> None:
    runner = FakeRunner('{"data": []}')
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    assert oci.list_service_gateways("compartment-id", "vcn-id") == []
    assert oci.list_policies("compartment-id") == []
    assert oci.list_secrets("compartment-id") == []
    assert runner.commands[0][5:8] == ["network", "service-gateway", "list"]
    assert runner.commands[1][5:8] == ["iam", "policy", "list"]
    assert runner.commands[2][5:8] == ["vault", "secret", "list"]


class _StateRunner:
    """Returns a per-lifecycle-state payload; optionally fails on one state.

    Models the OPSI list facade querying one state per call: the multi-state +
    --all combination flaps on the live control plane, so the facade unions
    single-state calls instead.
    """

    def __init__(self, by_state, fail_state=None):
        self.by_state = by_state
        self.fail_state = fail_state
        self.commands = []

    def run(self, args, cwd=None, check=True, retry_on_transient=False):
        self.commands.append(args)
        state = args[args.index("--lifecycle-state") + 1]
        if state == self.fail_state:
            raise RuntimeError("NotAuthorizedOrNotFound")
        return CommandResult(tuple(args), self.by_state.get(state, '{"data": []}'), "", 0)


def test_list_opsi_insights_queries_each_state_and_unions_by_id() -> None:
    runner = _StateRunner({
        "ACTIVE": '{"data": [{"id": "ins-1", "database-id": "db-a", "lifecycle-state": "ACTIVE"}]}',
        "FAILED": '{"data": [{"id": "ins-2", "database-id": "db-b", "lifecycle-state": "FAILED"}]}',
        # ins-1 reappears under another state filter; the union must dedup by OCID.
        "NEEDS_ATTENTION": '{"data": [{"id": "ins-1", "database-id": "db-a", "lifecycle-state": "ACTIVE"}]}',
    })
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    insights = oci.list_opsi_database_insights("compartment-id")

    ids = sorted(i["id"] for i in insights)
    assert ids == ["ins-1", "ins-2"]
    # One call per lifecycle state, each carrying exactly one --lifecycle-state.
    assert len(runner.commands) == len(OciCli.OPSI_INSIGHT_STATES)
    assert all(cmd.count("--lifecycle-state") == 1 for cmd in runner.commands)


def test_list_opsi_insights_tolerates_a_failing_state_call() -> None:
    # A transient failure on one state must not discard the insights gathered
    # from the others (never a false "no insights")...
    runner = _StateRunner(
        {"FAILED": '{"data": [{"id": "ins-2", "database-id": "db-b", "lifecycle-state": "FAILED"}]}'},
        fail_state="ACTIVE",
    )
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    insights, complete = oci.list_opsi_database_insights_complete("compartment-id")

    assert [i["id"] for i in insights] == ["ins-2"]
    # ...but the union is flagged incomplete so callers don't trust it for absence.
    assert complete is False


def test_list_opsi_insights_complete_flag_true_when_all_states_answer() -> None:
    runner = _StateRunner({
        "ACTIVE": '{"data": [{"id": "ins-1", "database-id": "db-a", "lifecycle-state": "ACTIVE"}]}',
    })
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    insights, complete = oci.list_opsi_database_insights_complete("compartment-id")

    assert [i["id"] for i in insights] == ["ins-1"]
    assert complete is True


def test_oci_cli_data_safe_list_and_get_command_shapes() -> None:
    runner = FakeRunner('{"data": []}')
    oci = OciCli("cap", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    assert oci.list_data_safe_targets("compartment-id") == []
    assert oci.list_data_safe_private_endpoints("compartment-id") == []

    runner_get = FakeRunner('{"data": {"id": "dst-1"}}')
    oci_get = OciCli("cap", "eu-frankfurt-1", runner_get)  # type: ignore[arg-type]
    assert oci_get.get_data_safe_target("dst-1") == {"id": "dst-1"}
    cmd = runner_get.commands[0]
    assert cmd[5:9] == ["data-safe", "target-database", "get", "--target-database-id"]


def test_oci_cli_data_safe_audit_event_summary_command_shape() -> None:
    runner = FakeRunner('{"data": {"items": []}}')
    oci = OciCli("cap", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    assert oci.list_data_safe_audit_events("compartment-id", "2026-06-24T09:00:00Z", "2026-06-24T10:00:00Z") == []

    cmd = runner.commands[0]
    assert cmd[5:8] == ["data-safe", "audit-event-summary", "list-audit-events"]
    assert "--scim-query" in cmd


def test_oci_cli_log_analytics_command_shapes(tmp_path) -> None:
    runner = FakeRunner('{"data": {"namespace-name": "logan-ns", "id": "resource-id"}}')
    oci = OciCli("cap", "eu-frankfurt-1", runner)  # type: ignore[arg-type]
    oci.profile_tenancy = lambda: "tenancy-id"  # type: ignore[method-assign]
    payload = tmp_path / "association.json"
    payload.write_text("[{}]")

    runner.outputs = ['{"data": "logan-ns"}', '{"data": {"namespace-name": "logan-ns", "id": "resource-id"}}']
    assert oci.get_log_analytics_namespace("compartment-id") == "logan-ns"
    runner.outputs = ['{"data": "logan-ns"}', '{"data": {"namespace-name": "logan-ns", "id": "resource-id"}}']
    assert oci.onboard_log_analytics_namespace("compartment-id") == "logan-ns"
    runner.outputs = ['{"data": {"items": []}}', '{"data": {"namespace-name": "logan-ns", "id": "resource-id"}}']
    assert oci.create_log_analytics_log_group("logan-ns", "compartment-id", "dbman-logs") == "resource-id"
    runner.outputs = ['{"data": {"status": "accepted"}}', '{"data": {"namespace-name": "logan-ns", "id": "resource-id"}}']
    oci.upsert_log_analytics_association("logan-ns", "compartment-id", str(payload))
    assert oci.search_log_analytics(
        "logan-ns",
        "* | stats count",
        compartment_id="compartment-id",
        time_start="2026-06-25T10:00:00Z",
        time_end="2026-06-25T11:00:00Z",
        limit=25,
    ) == {
        "namespace-name": "logan-ns",
        "id": "resource-id",
    }

    assert runner.commands[0][5:8] == ["os", "ns", "get"]
    assert runner.commands[1][5:8] == ["log-analytics", "namespace", "get"]
    assert runner.commands[2][5:8] == ["os", "ns", "get"]
    assert runner.commands[3][5:8] == ["log-analytics", "namespace", "onboard"]
    assert runner.commands[5][5:8] == ["log-analytics", "log-group", "create"]
    assert runner.commands[6][5:8] == ["log-analytics", "assoc", "upsert-assocs"]
    assert "--compartment-id" in runner.commands[6]
    assert "--items" in runner.commands[6]
    assert runner.commands[7][5:8] == ["log-analytics", "query", "search"]
    assert "--query-string" in runner.commands[7]
    assert "--time-start" in runner.commands[7]
    assert "--time-end" in runner.commands[7]
    assert "--limit" in runner.commands[7]


def test_oci_cli_log_analytics_list_methods_unwrap_items() -> None:
    runner = FakeRunner('{"data": {"items": [{"name": "item"}]}}')
    oci = OciCli("cap", "eu-frankfurt-1", runner)  # type: ignore[arg-type]
    oci.profile_tenancy = lambda: "tenancy-id"  # type: ignore[method-assign]

    assert oci.list_log_analytics_log_groups("logan-ns", "compartment-id") == [{"name": "item"}]
    assert oci.list_log_analytics_entities("logan-ns", "compartment-id") == [{"name": "item"}]
    assert oci.list_log_analytics_sources("logan-ns") == [{"name": "item"}]
    assert oci.list_log_analytics_warnings("logan-ns", "compartment-id") == [{"name": "item"}]
    assert runner.commands[2][5:8] == ["log-analytics", "source", "list-sources"]


class _FailingRunner:
    def __init__(self, error: RuntimeError):
        self.error = error

    def run(self, args, cwd=None, check=True, retry_on_transient=False):
        raise self.error


def test_run_tolerating_handles_typed_oci_error() -> None:
    oci = OciCli(
        "cap",
        "eu-frankfurt-1",
        _FailingRunner(OciError("already enabled")),
    )  # type: ignore[arg-type]

    assert oci.run_tolerating(["db", "enable"], tolerated=("already enabled",)) is False


def test_run_tolerating_does_not_swallow_plain_runtime_error() -> None:
    oci = OciCli(
        "cap",
        "eu-frankfurt-1",
        _FailingRunner(RuntimeError("already enabled")),
    )  # type: ignore[arg-type]

    try:
        oci.run_tolerating(["db", "enable"], tolerated=("already enabled",))
    except RuntimeError as exc:
        assert "already enabled" in str(exc)
    else:
        raise AssertionError("Expected plain RuntimeError to propagate")


def test_oci_cli_create_data_safe_target_is_idempotent_by_name(tmp_path) -> None:
    runner = FakeRunner('{"data": [{"id": "existing", "display-name": "dbmopsi"}]}')
    oci = OciCli("cap", "eu-frankfurt-1", runner)  # type: ignore[arg-type]
    details = tmp_path / "d.json"
    details.write_text("{}")

    # An existing target with the same display name short-circuits creation.
    target_id = oci.create_data_safe_target("compartment-id", "dbmopsi", str(details))
    assert target_id == "existing"
    # Only the list call happened, no create.
    assert all("create" not in cmd for cmd in runner.commands)


def test_oci_cli_create_data_safe_target_builds_create_command(tmp_path) -> None:
    runner = FakeRunner('{"data": {"id": "new-target"}}')
    oci = OciCli("cap", "eu-frankfurt-1", runner)  # type: ignore[arg-type]
    details = tmp_path / "d.json"
    conn = tmp_path / "c.json"
    creds = tmp_path / "cr.json"
    for f in (details, conn, creds):
        f.write_text("{}")

    target_id = oci.create_data_safe_target(
        "compartment-id", "newdb", str(details), str(conn), str(creds)
    )
    assert target_id == "new-target"
    create_cmd = runner.commands[-1]
    assert create_cmd[5:8] == ["data-safe", "target-database", "create"]
    assert f"file://{details}" in create_cmd
    assert f"file://{conn}" in create_cmd
    assert f"file://{creds}" in create_cmd


def test_oci_cli_create_data_safe_private_endpoint_idempotent() -> None:
    runner = FakeRunner('{"data": [{"id": "pe-existing", "display-name": "dbmopsi-datasafe-pe"}]}')
    oci = OciCli("cap", "eu-frankfurt-1", runner)  # type: ignore[arg-type]
    pe = oci.create_data_safe_private_endpoint(
        "compartment-id", "dbmopsi-datasafe-pe", "vcn-1", "subnet-1"
    )
    assert pe == "pe-existing"
    assert all("create" not in cmd for cmd in runner.commands)
