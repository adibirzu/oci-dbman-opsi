import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "security-gate.py"


def _isolated_git_env() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("GIT_")
    }


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=_isolated_git_env(),
    )


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "security-gate.py").write_text(GATE.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/security-gate.py"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=_isolated_git_env(),
    )


def _commit_all(repo: Path) -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")


def test_security_gate_rejects_committed_terraform_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state = repo / "terraform" / "demo.tfstate.backup"
    state.parent.mkdir()
    state.write_text("{}\n", encoding="utf-8")
    _commit_all(repo)

    result = _run(repo)

    assert result.returncode == 1
    assert "Terraform state must not be committed" in result.stderr


def test_security_gate_rejects_public_terraform_ocids_and_plaintext_variables(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tf = repo / "terraform" / "modules" / "bad" / "main.tf"
    tf.parent.mkdir(parents=True)
    raw_ocid = "ocid1" + ".secret.oc1.." + ("a" * 24)
    tf.write_text(
        f'variable "database_password" {{ type = string }}\n# {raw_ocid}\n',
        encoding="utf-8",
    )
    _commit_all(repo)

    result = _run(repo)

    assert result.returncode == 1
    assert "OCI identifier" in result.stderr
    assert "plaintext credential variable" in result.stderr


def test_security_gate_allows_only_exact_loudly_gated_datasafe_demo_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fixture = repo / "terraform" / "examples" / "data-safe-plaintext-demo"
    fixture.mkdir(parents=True)
    (fixture / "variables.tf").write_text(
        '''variable "allow_plaintext_data_safe_demo" { default = false }\nvariable "data_safe_password" { sensitive = true }\ncheck "plaintext_demo_declaration_gate" {\n  assert {\n    condition = !var.enable_plaintext_data_safe_demo || var.allow_plaintext_data_safe_demo\n    error_message = "demo only"\n  }\n}\n''',
        encoding="utf-8",
    )
    (fixture / "main.tf").write_text(
        '''resource "x" "demo" { password = var.data_safe_password }\ncheck "plaintext_demo_use_gate" {\n  assert {\n    condition = !var.enable_plaintext_data_safe_demo || var.allow_plaintext_data_safe_demo\n    error_message = "demo only"\n  }\n}\n''',
        encoding="utf-8",
    )
    _commit_all(repo)

    result = _run(repo)

    assert result.returncode == 0, result.stderr


def test_security_gate_rejects_extra_password_file_beside_the_demo_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fixture = repo / "terraform" / "examples" / "data-safe-plaintext-demo"
    fixture.mkdir(parents=True)
    (fixture / "variables.tf").write_text(
        '''variable "allow_plaintext_data_safe_demo" { default = false }\nvariable "data_safe_password" { sensitive = true }\ncheck "plaintext_demo_declaration_gate" {\n  assert { condition = !var.enable_plaintext_data_safe_demo || var.allow_plaintext_data_safe_demo }\n}\n''',
        encoding="utf-8",
    )
    (fixture / "extra-password.tf").write_text('variable "extra_password" { sensitive = true }\n', encoding="utf-8")
    _commit_all(repo)

    result = _run(repo)

    assert result.returncode == 1
    assert "plaintext credential variable" in result.stderr


def test_security_gate_rejects_shell_wrapper_with_missing_parent_script(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    wrapper = repo / "scripts" / "wrapper.sh"
    wrapper.write_text("#!/bin/sh\nexec ./scripts/missing-parent.sh\n", encoding="utf-8")
    _commit_all(repo)

    result = _run(repo)

    assert result.returncode == 1
    assert "missing parent script" in result.stderr


def test_security_gate_rejects_committed_journals_and_unowned_deletes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "runs").mkdir()
    (repo / "runs" / "journal.jsonl").write_text('{"password":"leak"}\n', encoding="utf-8")
    (repo / "scripts" / "unsafe-destroy.sh").write_text("#!/bin/sh\nterraform destroy\n", encoding="utf-8")
    _commit_all(repo)

    result = _run(repo)

    assert result.returncode == 1
    assert "generated state, evidence, and journals" in result.stderr
    assert "unowned Terraform deletion attempt" in result.stderr


def test_security_gate_rejects_empty_tenant_defaults_and_host_placeholders(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tf = repo / "terraform" / "modules" / "bad" / "main.tf"
    tf.parent.mkdir(parents=True)
    tf.write_text(
        'variable "tenant_ocid" { default = "" }\nresource "x" "y" { host_ip = "" }\n',
        encoding="utf-8",
    )
    _commit_all(repo)

    result = _run(repo)

    assert result.returncode == 1
    assert "empty tenant/topology default" in result.stderr
    assert "host_ip" in result.stderr


def test_security_gate_rejects_private_topology_in_examples_and_public_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    example = repo / "terraform" / "examples" / "bypass" / "main.tf"
    example.parent.mkdir(parents=True)
    example.write_text('resource "x" "y" { host_ip = "10.42.7.9" }\n', encoding="utf-8")
    evidence = repo / "docs" / "redacted-evidence.json"
    evidence.parent.mkdir()
    evidence.write_text('{"endpoint":"192.168.1.7"}\n', encoding="utf-8")
    _commit_all(repo)

    result = _run(repo)

    assert result.returncode == 1
    assert "OCI topology" in result.stderr
