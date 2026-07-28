from __future__ import annotations

import pytest

from dbman_opsi.fleet import CredentialPolicy, DeploymentMode
from dbman_opsi.fleet_answers import AuthorityMode, FleetAnswers, LogPreset, answers_from_dict, answers_to_dict, fleet_questionnaire, validate_answers


@pytest.mark.parametrize(
    ("answers", "problem"),
    [
        (
            FleetAnswers(
                deployment_mode=DeploymentMode.PRODUCTION,
                authority_mode=AuthorityMode.AUTOMATED,
            ),
            "approval-required",
        ),
        (
            FleetAnswers(
                deployment_mode=DeploymentMode.PRODUCTION,
                provision_test_dbcs=True,
            ),
            "cannot provision test databases",
        ),
        (
            FleetAnswers(
                common_user=True,
                pdb_unique_passwords=True,
                credential_policy=CredentialPolicy.UNIQUE_VAULT_PER_ACCOUNT,
            ),
            "common user cannot use unique PDB passwords",
        ),
    ],
)
def test_answers_reject_unsafe_production_and_common_user_combinations(
    answers: FleetAnswers,
    problem: str,
) -> None:
    assert any(problem in issue for issue in validate_answers(answers))


def test_answers_preserve_the_explicit_credential_policy() -> None:
    answers = FleetAnswers(
        deployment_mode=DeploymentMode.PILOT,
        authority_mode=AuthorityMode.APPROVAL_REQUIRED,
        credential_policy=CredentialPolicy.HANDOFF_REQUIRED,
        common_user=True,
        pdb_unique_passwords=False,
    )

    assert answers.credential_policy is CredentialPolicy.HANDOFF_REQUIRED
    assert validate_answers(answers) == ()


def test_questionnaire_exposes_only_canonical_credential_policies() -> None:
    question = next(item for item in fleet_questionnaire() if item.key == "credential_policy")
    assert question.default == "shared-user-unique-secret"
    assert question.choices == (
        "shared-user-unique-secret", "shared-user-shared-secret", "dedicated-user-unique-secret",
    )
    assert CredentialPolicy.UNIQUE_VAULT_PER_ACCOUNT not in {CredentialPolicy(value) for value in question.choices}


def test_shared_user_shared_secret_is_poc_demo_only_and_legacy_import_still_parses() -> None:
    assert any("poc/demo" in issue for issue in validate_answers(FleetAnswers(credential_policy=CredentialPolicy.SHARED_USER_SHARED_SECRET)))
    assert validate_answers(FleetAnswers(deployment_mode=DeploymentMode.POC, credential_policy=CredentialPolicy.SHARED_USER_SHARED_SECRET)) == ()
    assert answers_from_dict({"credential_policy": "unique-vault-per-account"}).credential_policy is CredentialPolicy.UNIQUE_VAULT_PER_ACCOUNT


def test_answer_file_model_covers_the_full_questionnaire_without_weakening_choices() -> None:
    answers = answers_from_dict(
        {
            "deployment_mode": "pilot",
            "services": ["dbm", "logan"],
            "provision_test_dbcs": True,
            "provision_test_autonomous": True,
            "discovery_filters": {"regions": ["eu-frankfurt-1"], "all_discovered": False, "target_ids": ["target-a"]},
            "credential_policy": "existing-vault-only",
            "log_preset": "extended",
            "authority_mode": "automated",
            "max_concurrency": 8,
            "retention_days": 7,
            "common_user": True,
            "pdb_unique_passwords": False,
        }
    )

    assert validate_answers(answers) == ()
    assert answers.credential_policy is CredentialPolicy.EXISTING_VAULT_ONLY
    assert answers.discovery_filters.target_ids == ("target-a",)
    assert answers_from_dict(answers_to_dict(answers)) == answers
    assert {question.key for question in fleet_questionnaire()} == {
        "deployment_mode", "services", "provision_test_dbcs", "provision_test_autonomous", "discovery_filters",
        "credential_policy", "log_preset", "authority_mode", "max_concurrency", "retention_days", "common_user", "pdb_unique_passwords",
        "monitoring_username",
    }


@pytest.mark.parametrize(
    ("answers", "problem"),
    [
        (FleetAnswers(services=()), "at least one service"),
        (FleetAnswers(services=("unsupported",)), "unsupported values"),
        (FleetAnswers(max_concurrency=0), "max_concurrency"),
        (FleetAnswers(retention_days=0), "retention_days"),
    ],
)
def test_answer_validation_reports_every_common_invalid_branch(answers: FleetAnswers, problem: str) -> None:
    assert any(problem in issue for issue in validate_answers(answers))


def test_answers_reject_an_unknown_explicit_log_preset() -> None:
    with pytest.raises(ValueError, match="LogPreset"):
        FleetAnswers(log_preset="invalid")


def test_answer_file_booleans_are_parsed_without_truthy_string_coercion() -> None:
    answers = answers_from_dict({"deployment_mode": "pilot", "authority_mode": "automated", "common_user": "false"})

    assert answers.common_user is False


@pytest.mark.parametrize("policy", list(CredentialPolicy))
def test_common_user_and_unique_pdb_passwords_are_incompatible_for_every_policy(policy: CredentialPolicy) -> None:
    answers = FleetAnswers(
        deployment_mode=DeploymentMode.POC,
        authority_mode=AuthorityMode.AUTOMATED,
        credential_policy=policy,
        common_user=True,
        pdb_unique_passwords=True,
    )

    assert any("common user cannot use unique PDB passwords" in issue for issue in validate_answers(answers))


def test_production_forbids_shared_password_policy_and_locked_defaults_are_explicit() -> None:
    answers = FleetAnswers(credential_policy=CredentialPolicy.SHARED_PASSWORD)

    assert answers.retention_days == 7
    assert answers.log_preset is LogPreset.ALERT_LISTENER_AUDIT
    assert any("shared-password" in issue for issue in validate_answers(answers))
    assert DeploymentMode.POC.value == "poc"


def test_deployment_modes_expose_only_poc_demo_and_production_with_pilot_alias() -> None:
    assert [mode.value for mode in DeploymentMode] == ["production", "poc", "demo"]
    assert DeploymentMode.PILOT is DeploymentMode.POC
    assert answers_from_dict({"deployment_mode": "pilot"}).deployment_mode is DeploymentMode.POC
