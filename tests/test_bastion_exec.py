from pathlib import Path

import pytest

from dbman_opsi.bastion_exec import BastionSqlRunner
from dbman_opsi.config import Target


class _FakeExec:
    """Records foreground/background commands; returns canned stdout."""

    def __init__(self):
        self.fg: list[list[str]] = []
        self.bg: list[list[str]] = []

    def run(self, argv, input=None):  # noqa: A002 - mirror subprocess signature
        self.fg.append(argv)
        return "OK"

    def run_bg(self, argv):
        self.bg.append(argv)


def _runner(ex, **kw):
    return BastionSqlRunner(
        bastion_id="ocid1.bastion.x",
        target_private_ip="10.0.0.5",
        ssh_key="/keys/id",
        profile="cap",
        region="eu-frankfurt-1",
        exec_fn=ex.run,
        exec_bg_fn=ex.run_bg,
        session_id_fn=lambda: "ocid1.bastionsession.x",
        sleeper=lambda d: None,
        local_port=8022,
        **kw,
    )


def test_runner_creates_session_tunnel_runs_scripts_and_tears_down(tmp_path: Path) -> None:
    s1 = tmp_path / "01.sql"; s1.write_text("-- a")
    s2 = tmp_path / "06.sql"; s2.write_text("-- b")
    ex = _FakeExec()
    target = Target(kind="dbcs", name="cdb", service_name="PDB1")

    out = _runner(ex).__call__(target, [s1, s2])

    flat = " | ".join(" ".join(c) for c in ex.fg)
    # Session created with work-request wait, scripts scp'd + run, session deleted.
    assert "bastion session create-port-forwarding" in flat
    assert "--wait-for-state SUCCEEDED" in flat
    assert ex.bg and "8022:10.0.0.5:22" in " ".join(ex.bg[0])         # tunnel started
    assert sum("scp" in c[0] for c in ex.fg) == 2                      # both scripts copied
    assert any("sqlplus" in " ".join(c) for c in ex.fg)               # executed as sysdba
    assert "session delete" in flat                                   # torn down
    assert "OK" in out


def test_runner_tears_down_even_when_a_script_fails(tmp_path: Path) -> None:
    s1 = tmp_path / "01.sql"; s1.write_text("-- a")
    ex = _FakeExec()

    def boom(argv, input=None):  # noqa: A002
        ex.fg.append(argv)
        if any("sqlplus" in a for a in argv):
            raise RuntimeError("ORA-00942")
        return "OK"

    runner = _runner(ex)
    runner._exec = boom  # type: ignore[assignment]

    with pytest.raises(RuntimeError):
        runner(Target(kind="dbcs", name="cdb", service_name="PDB1"), [s1])

    # The bastion session must still be deleted on failure (cleanup in finally).
    assert any("delete" in " ".join(c) for c in ex.fg)
