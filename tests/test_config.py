from pathlib import Path

from dbman_opsi.config import EnablementConfig, Target, load_config, save_config


def test_config_round_trip_preserves_local_references(tmp_path: Path) -> None:
    tenancy_id = "ocid1" + ".tenancy.oc1..aaaaaaaaexample"
    compartment_id = "ocid1" + ".compartment.oc1..bbbbbbbbexample"
    adb_id = "ocid1" + ".autonomousdatabase.oc1..ccccccccexample"
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        tenancy_id=tenancy_id,
        compartment_id=compartment_id,
        targets=(Target(kind="autonomous", name="adb", resource_id=adb_id),),
    )
    path = tmp_path / "config.yaml"

    save_config(path, config)

    loaded = load_config(path)
    assert loaded.profile == "DEFAULT"
    assert loaded.tenancy_id == tenancy_id
    assert loaded.targets[0].kind == "autonomous"


def test_config_sanitized_view_redacts_sensitive_shapes() -> None:
    config = EnablementConfig(profile="DEFAULT", region="eu-frankfurt-1", tenancy_id="ocid1" + ".tenancy.oc1..x")

    assert config.sanitized()["tenancy_id"] == "<OCI_OCID>"
