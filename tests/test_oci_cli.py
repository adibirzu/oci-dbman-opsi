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
