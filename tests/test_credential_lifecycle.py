from dbman_opsi.credential_lifecycle import (
    DEMO_DATABASE_ROLES,
    CredentialReference,
    build_reset_plan,
    generate_compliant_password,
    public_credential_status,
)


def test_generated_password_is_compliant_and_role_specific() -> None:
    password = generate_compliant_password()

    assert len(password) >= 16
    assert any(character.islower() for character in password)
    assert any(character.isupper() for character in password)
    assert any(character.isdigit() for character in password)
    assert any(character in "_#%+" for character in password)


def test_public_status_exposes_references_not_secret_value() -> None:
    rows = public_credential_status(
        [CredentialReference(role="MCP_READONLY", secret_id="ocid1.secret.oc1..example", version=3)]
    )

    assert rows == [{"role": "MCP_READONLY", "secret_id": "ocid1.secret.oc1..example", "version": 3}]
    assert "password" not in str(rows).lower()


def test_reset_plan_changes_exactly_one_role() -> None:
    plan = build_reset_plan("MCP_READONLY")

    assert plan.role == "MCP_READONLY"
    assert plan.refresh_bindings == ("dbm", "opsi", "datasafe")
    assert set(DEMO_DATABASE_ROLES) == {"DBM_MON", "DATASAFE_AUDIT", "MCP_READONLY", "DBINC_LAB"}


def test_reset_plan_rejects_unknown_role() -> None:
    import pytest

    with pytest.raises(ValueError, match="unsupported"):
        build_reset_plan("SYSTEM")
