from dbman_opsi.config import EnablementConfig, NetworkSelection, Target
from pathlib import Path
import json
import re
import shutil
import subprocess

import pytest

from dbman_opsi.terraform import render_tfvars, run_terraform, write_tfvars


def test_render_tfvars_includes_network_policy_and_targets() -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="compartment-id",
        network=NetworkSelection(create_test_network=True),
        targets=(Target(kind="dbcs", name="db1", provision=True),),
    )

    tfvars = render_tfvars(config)

    assert tfvars["create_test_network"] is True
    assert tfvars["config_file_profile"] == "DEFAULT"
    assert tfvars["targets"] == [
        {
            "kind": "dbcs",
            "name": "db1",
            "resource_id": None,
            "provision": True,
            "management_type": "ADVANCED",
            "services": ["dbm", "opsi"],
            "logan_database_entity_id": None,
            "logan_host_entity_id": None,
            "logan_listener_entity_id": None,
            "logan_adb_entity_id": None,
            "logan_management_agent_id": None,
        }
    ]
    assert tfvars["enable_log_analytics"] is False
    assert "policy_statements" in tfvars


def test_render_tfvars_enables_log_analytics_only_when_selected() -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        targets=(Target(kind="autonomous", name="adb", services=("dbm", "opsi", "logan")),),
    )

    tfvars = render_tfvars(config)

    assert tfvars["enable_log_analytics"] is True
    assert "manage loganalytics-resources-family" in " ".join(tfvars["policy_statements"])  # type: ignore[arg-type]


class FakeRunner:
    def __init__(self) -> None:
        self.calls = []

    def run(self, args, cwd=None):
        self.calls.append((args, cwd))


def test_write_tfvars_and_run_terraform(tmp_path) -> None:
    config = EnablementConfig(profile="DEFAULT", region="eu-frankfurt-1", terraform_dir=str(tmp_path))
    runner = FakeRunner()

    path = write_tfvars(config)
    run_terraform(config, runner)  # type: ignore[arg-type]

    assert path.exists()
    assert [call[0][0] for call in runner.calls] == ["terraform", "terraform"]


def test_disposable_stack_uses_lifecycle_tags_and_never_exports_passwords() -> None:
    root = Path(__file__).parents[1] / "terraform" / "examples" / "zero-start-poc"
    terraform = root.joinpath("main.tf").read_text(encoding="utf-8")
    variables = root.joinpath("variables.tf").read_text(encoding="utf-8")

    assert "local.lifecycle_tags" in terraform
    assert "freeform_tags" in terraform
    assert terraform.count("local.lifecycle_tags") >= 6
    assert 'variable "demo_lifecycle_id"' in variables
    assert 'variable "evidence_retention_days"' in variables
    assert 'output "disposable_lifecycle"' in terraform
    assert 'output "db_admin_password"' not in terraform
    assert 'output "adb_admin_password"' not in terraform


def _terraform_graph(root: Path) -> str:
    if shutil.which("terraform") is None:
        pytest.skip("Terraform CLI is covered by the dedicated Terraform CI job")
    completed = subprocess.run(
        ["terraform", "graph"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def test_all_terraform_roots_lock_oci_provider_8_24() -> None:
    terraform_roots = (
        "examples/data-safe-plaintext-demo",
        "examples/zero-start-poc",
        "fixtures/dbm-opsi-compatibility",
        "modules/dbm-opsi-compatibility",
        "modules/dbm-opsi-enablement",
    )
    root = Path(__file__).parents[1] / "terraform"

    for relative_root in terraform_roots:
        lock = root.joinpath(relative_root, ".terraform.lock.hcl").read_text(encoding="utf-8")
        provider = re.search(
            r'provider "registry\.terraform\.io/oracle/oci" \{(?P<body>.*?)^\}',
            lock,
            re.MULTILINE | re.DOTALL,
        )
        assert provider is not None
        assert re.search(r'^\s*version\s+=\s+"8\.24\.0"$', provider.group("body"), re.MULTILINE)


def test_production_module_enforces_cdb_before_pdb_in_terraform_graph() -> None:
    root = Path(__file__).parents[1] / "terraform" / "modules" / "dbm-opsi-enablement"
    graph = _terraform_graph(root)

    assert 'oci_database_management_database_dbm_features_management.dbm_pdb (expand)' in graph
    assert 'oci_database_management_database_dbm_features_management.dbm_cdb (expand)' in graph
    assert (
        'oci_database_management_database_dbm_features_management.dbm_pdb (expand)" -> '
        '"[root] oci_database_management_database_dbm_features_management.dbm_cdb (expand)'
    ) in graph
    assert (
        'oci_database_management_database_dbm_features_management.dbm_cdb (expand)" -> '
        '"[root] terraform_data.operation_contract (expand)'
    ) in graph


def test_modules_put_authoritative_pdb_observation_before_cdb_disable_in_graph() -> None:
    for module, cdb_resource in (
        ("dbm-opsi-enablement", "oci_database_management_database_dbm_features_management.dbm_cdb"),
        ("dbm-opsi-compatibility", "oci_database_cloud_database_management.compatibility_cdb"),
    ):
        root = Path(__file__).parents[1] / "terraform" / "modules" / module
        graph = _terraform_graph(root)

        assert 'terraform_data.pdb_disable_observation (expand)' in graph
        assert 'data.oci_database_management_managed_database.pdb_disable_observation (expand)' in graph
        assert (
            f'{cdb_resource} (expand)" -> "[root] terraform_data.pdb_disable_observation (expand)'
        ) in graph
        assert (
            'terraform_data.pdb_disable_observation (expand)" -> '
            '"[root] data.oci_database_management_managed_database.pdb_disable_observation (expand)'
        ) in graph


def test_modules_reject_all_forged_stale_or_unsigned_receipt_bypasses() -> None:
    if shutil.which("terraform") is None:
        pytest.skip("Terraform CLI is covered by the dedicated Terraform CI job")
    modules = {
        "dbm-opsi-enablement": "-var=targets={cdb={database_id=\"cdb\",database_role=\"CDB\",database_resource_type=\"database\",service_name=\"cdb\",password_secret_id=\"vault\"},pdb={database_id=\"pdb\",managed_database_name=\"pdb-managed\",database_role=\"PDB\",parent_target_key=\"cdb\",database_resource_type=\"pluggabledatabase\",service_name=\"pdb\",password_secret_id=\"vault\"}}",
        "dbm-opsi-compatibility": "-var=targets={cdb={database_id=\"cdb\",managed_database_name=\"cdb-managed\",database_role=\"CDB\",database_resource_type=\"database\",service_name=\"cdb\",password_secret_id=\"vault\"},pdb={database_id=\"pdb\",managed_database_name=\"pdb-managed\",database_role=\"PDB\",parent_target_key=\"cdb\",database_resource_type=\"pluggabledatabase\",service_name=\"pdb\",password_secret_id=\"vault\"}}",
    }

    for module, targets in modules.items():
        root = Path(__file__).parents[1] / "terraform" / "modules" / module
        for bypass in ("forged", "stale", "unsigned", "wrong-target"):
            completed = subprocess.run(
                [
                    "terraform",
                    "plan",
                    "-refresh=false",
                    "-input=false",
                    "-no-color",
                    "-var=compartment_id=example",
                    "-var=dbm_private_endpoint_id=example",
                    "-var=lifecycle_id=reviewed-lifecycle",
                    "-var=owner_tag=reviewed-owner",
                    "-var=enable_database_management=false",
                    "-var=dbm_operation_stage=disable_cdb",
                    f"-var=pdb_disable_verification_receipt={bypass}",
                    targets,
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )

            assert completed.returncode != 0
            assert "does not accept a copied receipt" in completed.stdout + completed.stderr


def test_compatibility_adapter_enforces_cdb_before_pdb_in_terraform_graph() -> None:
    root = Path(__file__).parents[1] / "terraform" / "modules" / "dbm-opsi-compatibility"
    graph = _terraform_graph(root)

    assert 'oci_database_cloud_database_management.compatibility_pdb (expand)' in graph
    assert 'oci_database_cloud_database_management.compatibility_cdb (expand)' in graph
    assert (
        'oci_database_cloud_database_management.compatibility_pdb (expand)" -> '
        '"[root] oci_database_cloud_database_management.compatibility_cdb (expand)'
    ) in graph
    assert (
        'oci_database_cloud_database_management.compatibility_cdb (expand)" -> '
        '"[root] terraform_data.operation_contract (expand)'
    ) in graph


def test_modules_keep_vault_only_unique_lookup_and_staged_disable_receipts() -> None:
    root = Path(__file__).parents[1] / "terraform" / "modules"
    canonical_main = root.joinpath("dbm-opsi-enablement", "main.tf").read_text(encoding="utf-8")
    canonical_variables = root.joinpath("dbm-opsi-enablement", "variables.tf").read_text(encoding="utf-8")
    canonical_outputs = root.joinpath("dbm-opsi-enablement", "outputs.tf").read_text(encoding="utf-8")
    compatibility_main = root.joinpath("dbm-opsi-compatibility", "main.tf").read_text(encoding="utf-8")
    compatibility_variables = root.joinpath("dbm-opsi-compatibility", "variables.tf").read_text(encoding="utf-8")
    compatibility_outputs = root.joinpath("dbm-opsi-compatibility", "outputs.tf").read_text(encoding="utf-8")
    fixture = Path(__file__).parents[1] / "terraform" / "fixtures" / "dbm-opsi-compatibility" / "main.tf"

    for main, variables, outputs in (
        (canonical_main, canonical_variables, canonical_outputs),
        (compatibility_main, compatibility_variables, compatibility_outputs),
    ):
        assert "password_secret_id" in main
        assert "host_ip" not in main
        assert "data_safe_password" not in variables
        assert 'variable "dbm_operation_stage"' in variables
        assert 'variable "pdb_disable_verification_receipt"' in variables
        assert 'output "dbm_operation_receipt"' in outputs
        assert "disable_pdb" in outputs
        assert "disable_cdb" in main
    assert "length(each.value) == 1" in compatibility_main
    assert "managed_database_id = each.value" in compatibility_main
    assert "managed_database_id = each.value.database_id" not in compatibility_main
    assert "ocid1." not in fixture.read_text(encoding="utf-8")


def test_terraform_observer_emits_non_secret_authoritative_metadata(tmp_path) -> None:
    observer = Path(__file__).parents[1] / "terraform" / "scripts" / "observe_pdb_dbm_state.py"
    targets = tmp_path / "targets.json"
    targets.write_text(
        '{"pdb": {"database_id": "pdb-id", "managed_database_name": "pdb-managed"}}',
        encoding="utf-8",
    )
    fake_oci = tmp_path / "oci"
    fake_oci.write_text(
        "#!/bin/sh\nprintf '%s\\n' '{\"data\": {\"items\": [{\"id\": \"managed-id\", \"name\": \"pdb-managed\", \"compartment-id\": \"compartment\", \"dbmgmt-feature-configs\": []}]}}'\n",
        encoding="utf-8",
    )
    fake_oci.chmod(0o755)

    completed = subprocess.run(
        [
            "python3",
            str(observer),
            "--oci-bin",
            str(fake_oci),
            "--compartment-id",
            "compartment",
            "--lifecycle-id",
            "reviewed-lifecycle",
            "--targets-file",
            str(targets),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    receipt = json.loads(completed.stdout)

    assert receipt["target_set_sha256"]
    assert receipt["completed_at"]
    assert receipt["observer"] == "oci-cli"
    assert receipt["source"] == "database-management managed-database list"
    assert receipt["nonce"]
    assert "pdb-id" not in completed.stdout


def test_terraform_observer_rejects_an_enabled_pdb_feature(tmp_path) -> None:
    observer = Path(__file__).parents[1] / "terraform" / "scripts" / "observe_pdb_dbm_state.py"
    targets = tmp_path / "targets.json"
    targets.write_text(
        '{"pdb": {"database_id": "pdb-id", "managed_database_name": "pdb-managed"}}',
        encoding="utf-8",
    )
    fake_oci = tmp_path / "oci"
    fake_oci.write_text(
        "#!/bin/sh\nprintf '%s\\n' '{\"data\": {\"items\": [{\"id\": \"managed-id\", \"name\": \"pdb-managed\", \"compartment-id\": \"compartment\", \"dbmgmt-feature-configs\": [{\"feature\": \"DIAGNOSTICS_AND_MANAGEMENT\", \"feature-status\": \"ENABLED\"}]}]}}'\n",
        encoding="utf-8",
    )
    fake_oci.chmod(0o755)

    completed = subprocess.run(
        [
            "python3",
            str(observer),
            "--oci-bin",
            str(fake_oci),
            "--compartment-id",
            "compartment",
            "--lifecycle-id",
            "reviewed-lifecycle",
            "--targets-file",
            str(targets),
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "not disabled" in completed.stderr


def test_plaintext_datasafe_terraform_is_loudly_gated_to_demo() -> None:
    root = Path(__file__).parents[1] / "terraform" / "examples" / "data-safe-plaintext-demo"
    main = root.joinpath("main.tf").read_text(encoding="utf-8")
    variables = root.joinpath("variables.tf").read_text(encoding="utf-8")

    assert "DEMO ONLY" in main
    assert "allow_plaintext_data_safe_demo" in main
    assert 'default     = false' in variables
    assert "data_safe_password" in variables
    assert "password_secret_id" not in variables
