import os
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "pre-push"
SECURITY_GATE = ROOT / "scripts" / "security-gate.py"


def _isolated_git_env() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("GIT_")
    }


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=_isolated_git_env(),
    )


def _init_repo(repo: Path) -> None:
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "test@example.invalid")
    _run_git(repo, "config", "user.name", "Test User")
    (repo / ".gitleaks.toml").write_text("title = \"test\"\n", encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "security-gate.py").write_text(SECURITY_GATE.read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "README.md").write_text("clean\n", encoding="utf-8")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "initial")


def _run_hook(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HOOK)],
        cwd=repo,
        text=True,
        input="",
        capture_output=True,
        check=False,
        env=_isolated_git_env(),
    )


def _synthetic_ocid() -> str:
    return "ocid1" + ".tenancy.oc1.." + ("a" * 40)


def test_pre_push_script_is_executable() -> None:
    mode = HOOK.stat().st_mode

    assert mode & stat.S_IXUSR


def test_pre_push_blocks_synthetic_identifier_in_working_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "leak.txt").write_text(f"id={_synthetic_ocid()}\n", encoding="utf-8")

    result = _run_hook(tmp_path)

    assert result.returncode == 1
    assert "real OCI identifier" in result.stderr
    assert "leak.txt" in result.stderr


def test_pre_push_allows_synthetic_identifier_in_markdown(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "notes.md").write_text(f"placeholder-ish {_synthetic_ocid()}\n", encoding="utf-8")

    result = _run_hook(tmp_path)

    assert result.returncode == 0


def test_pre_push_passes_clean_tree_with_parent_git_environment(
    tmp_path: Path, monkeypatch
) -> None:
    parent_repo = tmp_path / "parent"
    child_repo = tmp_path / "child"
    parent_repo.mkdir()
    child_repo.mkdir()
    _init_repo(parent_repo)
    parent_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=parent_repo,
        check=True,
        capture_output=True,
        text=True,
        env=_isolated_git_env(),
    ).stdout.strip()

    inherited = {
        "GIT_COMMON_DIR": str(parent_repo / ".git"),
        "GIT_CONFIG": str(parent_repo / ".git" / "config"),
        "GIT_CONFIG_COUNT": "0",
        "GIT_DIR": str(parent_repo / ".git"),
        "GIT_IMPLICIT_WORK_TREE": "0",
        "GIT_INDEX_FILE": str(parent_repo / ".git" / "index"),
        "GIT_PREFIX": "nested/",
        "GIT_WORK_TREE": str(parent_repo),
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)

    _init_repo(child_repo)

    result = _run_hook(child_repo)

    assert result.returncode == 0
    assert (child_repo / ".git").is_dir()
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=parent_repo,
            check=True,
            capture_output=True,
            text=True,
            env=_isolated_git_env(),
        ).stdout.strip()
        == parent_head
    )


def test_pre_push_embeds_format_patterns_not_real_identifiers() -> None:
    script = HOOK.read_text(encoding="utf-8")
    banned_fragments = [
        _synthetic_ocid(),
        "fr4zq" + "fimuxtr",
        "axoxd" + "ievda5j",
        "id9y6" + "mi8tcky",
        "aaaadhp5ewo4e" + "aaaaaaaaafs7q",
        "axfo51" + "x8x2ap",
    ]

    for fragment in banned_fragments:
        assert fragment not in script
