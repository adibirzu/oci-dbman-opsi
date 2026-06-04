import builtins

from dbman_opsi.wizard import run_wizard


class FakeOci:
    def list_compartments(self, tenancy_id):
        return [{"id": "compartment-id", "name": "PoC"}]

    def list_vcns(self, compartment_id):
        return [{"id": "vcn-id", "display-name": "vcn"}]

    def list_subnets(self, compartment_id, vcn_id):
        return [{"id": "subnet-id", "display-name": "private"}]

    def list_autonomous_databases(self, compartment_id):
        return [{"id": "adb-id", "display-name": "adb"}]

    def list_vaults(self, compartment_id):
        return [{"id": "vault-id", "display-name": "vault"}]


def test_wizard_discovers_and_selects_resources(monkeypatch) -> None:
    answers = iter(
        [
            "tenancy-id",
            "1",
            "no",
            "1",
            "1",
            "yes",
            "",
            "autonomous",
            "adb",
            "no",
            "1",
            "",
            "",
            "",
            "no",
        ]
    )
    monkeypatch.setattr(builtins, "input", lambda prompt: next(answers))

    config = run_wizard("DEFAULT", "eu-frankfurt-1", FakeOci())  # type: ignore[arg-type]

    assert config.compartment_id == "compartment-id"
    assert config.network.vcn_id == "vcn-id"
    assert config.network.subnet_id == "subnet-id"
    assert config.vault.create_vault is True
    assert config.targets[0].resource_id == "adb-id"


def test_wizard_falls_back_when_discovery_fails(monkeypatch) -> None:
    class BrokenOci:
        def list_compartments(self, tenancy_id):
            raise RuntimeError("not configured")

    answers = iter(
        [
            "tenancy-id",
            "compartment-id",
            "yes",
            "no",
            "vault-id",
            "key-id",
            "no",
        ]
    )
    monkeypatch.setattr(builtins, "input", lambda prompt: next(answers))

    config = run_wizard("DEFAULT", "eu-frankfurt-1", BrokenOci())  # type: ignore[arg-type]

    assert config.compartment_id == "compartment-id"
    assert config.network.create_test_network is True
    assert config.targets == ()
