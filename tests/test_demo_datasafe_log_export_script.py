import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "demo-datasafe-log-export.sh"


def test_demo_datasafe_log_export_script_has_valid_bash_syntax() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], cwd=ROOT, text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr


def test_demo_datasafe_log_export_script_help_mentions_demo_only_apply_and_sync() -> None:
    result = subprocess.run([str(SCRIPT), "--help"], cwd=ROOT, text=True, capture_output=True, check=False)

    assert result.returncode == 0
    assert "Demo only" in result.stdout
    assert "--apply" in result.stdout
    assert "sync" in result.stdout
    assert "Log Analytics" in result.stdout
    assert "status" in result.stdout
    assert "targets" in result.stdout


def test_datasafe_runbook_warns_against_monitoring_user_failed_login_drills() -> None:
    runbook = (ROOT / "docs" / "datasafe-log-analytics.md").read_text(encoding="utf-8")

    assert "Do not create those failed-login rows by probing `DBSNMP`" in runbook
    assert "13-remediate-monitoring-account-lock.sql" in runbook
    assert "audit profile" in runbook
    assert "audit trail" in runbook
