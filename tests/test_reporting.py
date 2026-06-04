from dbman_opsi.checks import (
    CheckResult,
    PreflightReport,
    TargetReport,
    fail,
    manual,
    ok,
)
from dbman_opsi.orchestrator import ConfigureReport, TargetDecision
from dbman_opsi.reporting import print_configure_report, print_preflight_report


def _report() -> PreflightReport:
    return PreflightReport(
        tenancy_checks=(ok("iam.policies", "present"),),
        network_checks=(fail("network.service_gateway", "missing", "create a SGW"),),
        targets=(
            TargetReport(
                name="cloud db",
                kind="dbcs",
                location="oci-native",
                checks=(ok("target.resource", "AVAILABLE"), manual("target.monitoring_user", "verify DB-side")),
            ),
        ),
    )


def test_print_preflight_renders_statuses_and_remediation(capsys) -> None:
    print_preflight_report(_report())
    out = capsys.readouterr().out

    assert "[PASS] iam.policies" in out
    assert "[FAIL] network.service_gateway" in out
    assert "-> create a SGW" in out
    assert "[MANUAL] target.monitoring_user" in out
    assert "NOT READY" in out


def test_print_configure_lists_decisions_and_handoff(capsys) -> None:
    report = ConfigureReport(
        mode="db-side-only",
        preflight=_report(),
        decisions=(TargetDecision("cloud db", "dbcs", "oci-native", "handoff", "packet generated"),),
        handoff_paths=("generated/handoff/cloud-db/HANDOFF.md",),  # type: ignore[arg-type]
    )
    print_configure_report(report)
    out = capsys.readouterr().out

    assert "[HANDOFF] cloud db" in out
    assert "Handoff packets:" in out
    assert "cloud-db/HANDOFF.md" in out


def test_check_result_blocking_semantics() -> None:
    assert CheckResult("x", "fail", "d").blocking
    assert not CheckResult("x", "warn", "d").blocking
    assert not CheckResult("x", "manual", "d").blocking
