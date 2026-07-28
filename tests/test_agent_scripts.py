import subprocess
from pathlib import Path

from dbman_opsi.agent_scripts import generate_agent_scripts, render_agent_script
from dbman_opsi.config import EnablementConfig, Target


def test_render_linux_agent_script_includes_required_plugins() -> None:
    config = EnablementConfig(profile="DEFAULT", region="eu-frankfurt-1", compartment_id="compartment-id")
    target = Target(kind="external-db", name="extdb", external_os="linux")

    script = render_agent_script(target, config)

    assert "Service.plugin.dbmgmt.download=true" in script
    assert "Service.plugin.opsi.download=true" in script
    assert "INSTALL_KEY" in script
    assert 'AGENT_RPM_SHA256="${AGENT_RPM_SHA256:-}"' in script
    assert 'fail "Set AGENT_RPM_SHA256 when AGENT_RPM_URL is used"' in script
    assert 'actual_sha256="$(sha256sum "$AGENT_RPM"' in script
    assert 'fail "SHA256 mismatch for $AGENT_RPM"' in script
    assert "umask 077" in script
    assert "DELETE_INSTALL_KEY_FILE" in script
    assert 'rm -f -- "$RSP_FILE"' in script


def test_generate_agent_scripts_for_external_and_logan_enabled_cloud_targets(tmp_path: Path) -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        targets=(
            Target(kind="external-db", name="external db", external_os="linux"),
            Target(kind="dbcs", name="cloud db", services=("dbm", "opsi", "logan")),
            Target(kind="exadata", name="cloud without logan"),
        ),
    )

    paths = generate_agent_scripts(config, tmp_path)

    names = {path.name for path in paths}
    assert "external-db-agent.sh" in names
    assert "cloud-db-agent.sh" in names
    assert "cloud-db-agent-ansible-run.sh" in names
    assert not any("cloud-without-logan" in name for name in names)
    assert all(path.exists() for path in paths)

    cloud_install = (tmp_path / "cloud-db-agent.sh").read_text(encoding="utf-8")
    install_key = (tmp_path / "cloud-db-agent-create-install-key.sh").read_text(encoding="utf-8")
    cloud_run = (tmp_path / "cloud-db-agent-ansible-run.sh").read_text(encoding="utf-8")
    assert "Service.plugin.logan.download=true" in cloud_install
    assert "StrictHostKeyChecking=no" not in cloud_run
    assert "StrictHostKeyChecking=accept-new" in cloud_run
    assert "UserKnownHostsFile=$KNOWN_HOSTS" in cloud_run
    assert "umask 077" in install_key
    assert 'chmod 600 "$INSTALL_KEY_JSON" "$INSTALL_KEY_FILE"' in install_key
    playbook = (tmp_path / "cloud-db-agent-ansible-playbook.yml").read_text(encoding="utf-8")
    assert "DELETE_INSTALL_KEY_FILE=true" in playbook
    assert "host_key_checking = True" in (tmp_path / "cloud-db-agent-ansible.cfg").read_text(encoding="utf-8")


def test_render_windows_and_generic_agent_scripts() -> None:
    config = EnablementConfig(profile="DEFAULT", region="eu-frankfurt-1", compartment_id="compartment-id")

    windows = render_agent_script(Target(kind="external-db", name="win", external_os="windows"), config)
    solaris = render_agent_script(Target(kind="external-db", name="sol", external_os="solaris"), config)

    assert "setup.bat" in windows
    assert "finally" in windows
    assert "Remove-Item -Force" in windows
    assert "Required plugins: dbmgmt, opsi" in solaris


def test_generated_agent_shell_scripts_have_valid_bash_syntax(tmp_path: Path) -> None:
    config = EnablementConfig(
        profile="DEFAULT",
        region="eu-frankfurt-1",
        compartment_id="ocid" + "1.compartment.oc1..aaaaaaaa",
        targets=(
            Target(
                kind="dbcs",
                name="cloud db",
                services=("logan",),
            ),
        ),
    )

    paths = generate_agent_scripts(config, tmp_path)
    shell_paths = [path for path in paths if path.suffix == ".sh"]
    results = [
        subprocess.run(["bash", "-n", str(path)], text=True, capture_output=True, check=False)
        for path in shell_paths
    ]

    assert shell_paths
    assert [result.stderr for result in results if result.returncode] == []
