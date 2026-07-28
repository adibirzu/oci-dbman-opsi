import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "demo-db-incident-e2e.sh"
BASTION_SCRIPT = ROOT / "scripts" / "run-via-disposable-bastion.sh"
CONSOLE_SCRIPT = ROOT / "scripts" / "open-disposable-console.sh"
RUNBOOK = ROOT / "docs" / "demo-db-incident-e2e.md"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DEMO_JUMPHOST_", "DEMO_BASTION_", "DEMO_DB_", "DB_INCIDENT_"))
    }
    merged_env.update(env or {})
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_demo_db_incident_e2e_script_has_valid_bash_syntax() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], cwd=ROOT, text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr


def test_disposable_bastion_runner_has_valid_bash_syntax_and_scoped_lookup() -> None:
    result = subprocess.run(
        ["bash", "-n", str(BASTION_SCRIPT)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    script = BASTION_SCRIPT.read_text(encoding="utf-8")

    assert result.returncode == 0, result.stderr
    assert 'LIFECYCLE_ID="${LIFECYCLE_ID:?' in script
    assert "dbman_opsi_lifecycle" in script
    assert "eval " not in script
    assert "OCI_CLI_CONFIG_FILE" in script


def test_disposable_console_fallback_is_lifecycle_scoped_and_cleans_up() -> None:
    result = subprocess.run(
        ["bash", "-n", str(CONSOLE_SCRIPT)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    script = CONSOLE_SCRIPT.read_text(encoding="utf-8")

    assert result.returncode == 0, result.stderr
    assert 'LIFECYCLE_ID="${LIFECYCLE_ID:?' in script
    assert "dbman_opsi_lifecycle" in script
    assert "instance-console-connection delete" in script


def test_demo_script_rejects_cli_region_that_differs_from_target_config(tmp_path: Path) -> None:
    config = tmp_path / "target.yaml"
    config.write_text("profile: demo\nregion: us-chicago-1\ncompartment_id: example\n", encoding="utf-8")

    result = _run(
        "wait-db",
        env={
            "CONFIG": str(config),
            "PROFILE": "demo",
            "REGION": "eu-frankfurt-1",
            "DB_SYSTEM_NAME": "disposable-db",
        },
    )

    assert result.returncode == 2
    assert "does not match config region" in result.stdout


def test_demo_db_incident_e2e_help_and_tasks_are_actionable() -> None:
    help_result = _run("--help")
    tasks_result = _run("tasks")

    assert help_result.returncode == 0
    assert "jumphost-copy" in help_result.stdout
    assert "jumphost-run" in help_result.stdout
    assert "logan-scenario-check" in help_result.stdout
    assert "wait-db" in help_result.stdout
    assert "DEMO_JUMPHOST_SSH_KEY" in help_result.stdout
    assert tasks_result.returncode == 0
    assert "Management Agent with Log Analytics plugin" in tasks_result.stdout
    assert "Data Safe audit primer" in tasks_result.stdout
    assert "Do not test bad passwords against DBSNMP" in tasks_result.stdout
    assert "13-remediate-monitoring-account-lock.sql" in tasks_result.stdout
    assert "logan-scenario-check" in tasks_result.stdout
    assert "oci-coordinator-oke /chat" in tasks_result.stdout
    assert "Wait for DBCS to become AVAILABLE" in tasks_result.stdout


def test_demo_db_incident_e2e_jumphost_copy_requires_ssh_key_without_leaking_secrets(tmp_path: Path) -> None:
    env = {
        "DEMO_JUMPHOST_HOST": "127.0.0.1",
        "OUTPUT_DIR": str(tmp_path / "packet"),
        "DB_INCIDENT_ADMIN_CONNECT": "admin/secret@example",
        "DB_INCIDENT_LAB_PASSWORD": "lab-secret",
    }

    result = _run("jumphost-copy", env=env)

    assert result.returncode == 2
    assert "DEMO_JUMPHOST_SSH_KEY is required" in result.stdout
    assert "admin/secret@example" not in result.stdout + result.stderr
    assert "lab-secret" not in result.stdout + result.stderr


def test_demo_db_incident_e2e_remote_execution_keeps_secrets_ephemeral() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "ssh_stdin <<EOF" in script
    assert ".db-incident-env" not in script
    assert "chmod -R a+rX" not in script
    assert "--preserve-env=DB_INCIDENT_ADMIN_CONNECT,DB_INCIDENT_LAB_PASSWORD" in script
    assert "DB_INCIDENT_DATASAFE_AUDIT_ENABLED=$q_datasafe_audit_enabled" in script
    assert "DB_INCIDENT_DATASAFE_AUDIT_FAILED_LOGIN_ENABLED=$q_datasafe_failed_login" in script
    assert "DB_INCIDENT_DATASAFE_AUDIT_ENABLED,DB_INCIDENT_DATASAFE_AUDIT_FAILED_LOGIN_ENABLED" in script
    assert 'DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED:-false' in script
    assert 'sudo chown -R oracle "$remote_packet_dir"' in script


def test_demo_jumphost_preflight_forwards_only_verified_sqlcl_download_inputs() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "DB_INCIDENT_TOOLING_INSTALL=$q_tooling_install" in script
    assert "DB_INCIDENT_SQLCL_URL=$q_sqlcl_url" in script
    assert "DB_INCIDENT_SQLCL_SHA256=$q_sqlcl_sha256" in script
    assert "DB_INCIDENT_SQLCL_ARCHIVE=$q_" not in script


def test_demo_scenario_id_is_reused_from_the_generated_packet() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'SCENARIO_ID="${SCENARIO_ID:-}"' in script
    assert '"$OUTPUT_DIR/manifest.json"' in script
    assert 'resolve_scenario_id' in script


def test_demo_packet_excludes_rebuildable_tool_caches() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "COPYFILE_DISABLE=1 tar --no-xattrs --exclude='.tools' --exclude='._*'" in script
    assert "sudo -n rm -rf" in script
    assert "tar --no-same-owner --no-same-permissions -xzf" in script


def test_demo_db_incident_e2e_docs_describe_remote_secret_handling() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert "SSH stdin" in runbook
    assert "does not place them in generated files or in the SSH command arguments" in runbook
    assert "Management Agent ingestion" in runbook
    assert "oci_logan_build_db_incident_evidence" in runbook
    assert "Do not probe `DBSNMP` or other monitoring users with bad passwords" in runbook
    assert "13-remediate-monitoring-account-lock.sql" in runbook
    assert "DB_INCIDENT_SQLCL_SHA256" in runbook


def test_demo_db_incident_public_files_do_not_embed_tenancy_details() -> None:
    checked_files = [
        SCRIPT,
        RUNBOOK,
        ROOT / "docs" / "db-incident-troubleshooting.md",
    ]
    forbidden_fragments = [
        "ocid1" + ".",
        "defense" + "demo",
        "CAP_",
        "cap" + "-db",
        "dbman" + "ops",
        "jump" + "box",
        "dbman" + "opsi-bastion",
        "observability-bastion",
        "oci-demo-management-bastion",
        "JumpBoxExtern",
        "130" + ".61.",
        "161" + ".153.",
        "144" + ".24.",
        "129" + ".153.",
        "141" + ".147.",
        "82" + ".77.",
        "109" + ".166.",
        "fr4zq" + "fimuxtr",
        "aaaadhp5ewo4e" + "aaaaaaaaafs7q",
        "axfo51" + "x8x2ap",
        "axoxd" + "ievda5j",
    ]

    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked_files)

    for fragment in forbidden_fragments:
        assert fragment not in combined
