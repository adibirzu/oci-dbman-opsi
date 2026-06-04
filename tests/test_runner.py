from dbman_opsi.runner import CommandRunner


def test_dry_run_runner_prints_redacted_command(capsys) -> None:
    runner = CommandRunner(dry_run=True)

    result = runner.run(["oci", "db", "get", "--database-id", "ocid1" + ".database.oc1..example"])

    assert result.returncode == 0
    assert result.json() == {}
    assert "ocid1" + "." not in capsys.readouterr().out


def test_runner_raises_on_failed_command() -> None:
    runner = CommandRunner(dry_run=False)

    try:
        runner.run(["python3", "-c", "import sys; sys.stderr.write('boom'); sys.exit(7)"])
    except RuntimeError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError")


def test_runner_redacts_failed_command() -> None:
    runner = CommandRunner(dry_run=False)

    try:
        runner.run(["python3", "-c", "import sys; sys.exit(7)", "ocid1" + ".database.oc1..example"])
    except RuntimeError as exc:
        assert "ocid1" + "." not in str(exc)
    else:
        raise AssertionError("Expected RuntimeError")
