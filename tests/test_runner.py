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


def test_runner_returns_raw_ocids_for_logic() -> None:
    # The data path must NOT redact OCIDs: discovery/credential joins parse real
    # OCIDs out of command output. Redacting here would collapse every OCID to the
    # same token and make OCID-keyed joins match everything-to-everything.
    runner = CommandRunner(dry_run=False)
    ocid = "ocid1" + ".database.oc1..realexample"

    result = runner.run(["python3", "-c", f"print('{{\"data\": {{\"id\": \"{ocid}\"}}}}')"])

    assert result.json()["data"]["id"] == ocid


def test_runner_redacts_failed_command() -> None:
    runner = CommandRunner(dry_run=False)

    try:
        runner.run(["python3", "-c", "import sys; sys.exit(7)", "ocid1" + ".database.oc1..example"])
    except RuntimeError as exc:
        assert "ocid1" + "." not in str(exc)
    else:
        raise AssertionError("Expected RuntimeError")
