from dbman_opsi.oci_cli import OciCli
from dbman_opsi.runner import CommandResult


class FakeRunner:
    def __init__(self, payload):
        self.payload = payload
        self.commands = []

    def run(self, args, cwd=None, check=True):
        self.commands.append(args)
        return CommandResult(tuple(args), self.payload, "", 0)


def test_oci_cli_adds_profile_region_and_json_output() -> None:
    runner = FakeRunner('{"data": [{"id": "vcn-id"}]}')
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    vcns = oci.list_vcns("compartment-id")

    assert vcns == [{"id": "vcn-id"}]
    assert runner.commands[0][:5] == ["oci", "--profile", "DEFAULT", "--region", "eu-frankfurt-1"]
    assert runner.commands[0][-2:] == ["--output", "json"]


def test_oci_cli_lists_known_resource_types() -> None:
    runner = FakeRunner('{"data": []}')
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    assert oci.list_compartments("tenancy-id") == []
    assert oci.list_subnets("compartment-id", "vcn-id") == []
    assert oci.list_db_systems("compartment-id") == []
    assert oci.list_databases("compartment-id", "db-system-id") == []
    assert oci.get_database("database-id") == {}
    assert oci.list_autonomous_databases("compartment-id") == []
    assert oci.get_autonomous_database("autonomous-database-id") == {}
    assert oci.list_exadata_infrastructure("compartment-id") == []
    assert oci.list_management_agents("compartment-id") == []
    assert oci.list_vaults("compartment-id") == []
    assert oci.list_db_management_private_endpoints("compartment-id") == []
    assert oci.list_opsi_private_endpoints("compartment-id") == []


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
    assert oci.get_management_agent("agent-id") == {"lifecycle-state": "ACTIVE"}


def test_oci_cli_list_methods_use_expected_verbs() -> None:
    runner = FakeRunner('{"data": []}')
    oci = OciCli("DEFAULT", "eu-frankfurt-1", runner)  # type: ignore[arg-type]

    assert oci.list_service_gateways("compartment-id", "vcn-id") == []
    assert oci.list_policies("compartment-id") == []
    assert runner.commands[0][5:8] == ["network", "service-gateway", "list"]
    assert runner.commands[1][5:8] == ["iam", "policy", "list"]


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

    def run(self, args, cwd=None, check=True):
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
