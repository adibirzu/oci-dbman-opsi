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
            "",  # pillars (defaults to dbm,opsi)
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
    assert config.targets[0].services == ("dbm", "opsi")


class DbcsOci(FakeOci):
    def list_db_systems(self, compartment_id):
        return [{"id": "dbsys-1", "display-name": "dbmopsi"}]

    def list_pluggable_databases(self, compartment_id):
        return []


def test_wizard_captures_data_safe_selection_for_dbcs(monkeypatch) -> None:
    answers = iter(
        [
            "tenancy-id",
            "1",          # compartment
            "no",         # create network? no
            "1",          # vcn
            "1",          # subnet
            "yes",        # create vault? yes
            "",           # add a target? (default yes)
            "dbcs",       # kind
            "dbmopsi",    # name
            "no",         # provision? no
            "1",          # select db system
            "PDB1",       # service name
            "",           # monitoring user
            "",           # password secret
            "",           # private endpoint
            "dbm,opsi,datasafe",  # pillars
            "dspe-1",     # data safe private endpoint
            "no",         # discover PDBs? no
            "no",         # add another target? no
        ]
    )
    monkeypatch.setattr(builtins, "input", lambda prompt: next(answers))

    config = run_wizard("cap", "eu-frankfurt-1", DbcsOci())  # type: ignore[arg-type]

    target = config.targets[0]
    assert target.services == ("dbm", "opsi", "datasafe")
    assert target.wants("datasafe") is True
    # db_system_id captured from the selected DB system (needed for DS registration).
    assert target.db_system_id == "dbsys-1"
    assert target.data_safe_private_endpoint_id == "dspe-1"


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
