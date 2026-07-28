"""Immutable answer-file and questionnaire models for fleet planning."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from dbman_opsi.config import ALLOWED_SERVICES, DEFAULT_SERVICES
from dbman_opsi.fleet import CredentialPolicy, DeploymentMode
from dbman_opsi.fleet_selection import TargetSelection


class AuthorityMode(str, Enum):
    """The authorization boundary for a generated fleet plan."""

    PLAN_ONLY = "plan-only"
    APPROVAL_REQUIRED = "approval-required"
    AUTOMATED = "automated"


class LogPreset(str, Enum):
    """Explicit log sources selected for a fleet plan."""

    NONE = "none"
    ALERT_LISTENER_AUDIT = "alert-listener-audit"
    EXTENDED = "extended"


@dataclass(frozen=True)
class QuestionnaireQuestion:
    """One non-interactive questionnaire field and its safe default."""

    key: str
    prompt: str
    default: Any
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class FleetAnswers:
    """Planning answers, deliberately separate from credentials and execution."""

    deployment_mode: DeploymentMode = DeploymentMode.PRODUCTION
    services: tuple[str, ...] = DEFAULT_SERVICES
    provision_test_dbcs: bool = False
    provision_test_autonomous: bool = False
    discovery_filters: TargetSelection = TargetSelection()
    credential_policy: CredentialPolicy = CredentialPolicy.SHARED_USER_UNIQUE_SECRET
    log_preset: LogPreset = LogPreset.ALERT_LISTENER_AUDIT
    authority_mode: AuthorityMode = AuthorityMode.APPROVAL_REQUIRED
    max_concurrency: int = 4
    retention_days: int = 7
    common_user: bool = False
    pdb_unique_passwords: bool = False
    monitoring_username: str = "DBMAN_MON"

    def __post_init__(self) -> None:
        deployment_mode = "poc" if self.deployment_mode == "pilot" else self.deployment_mode
        object.__setattr__(self, "deployment_mode", DeploymentMode(deployment_mode))
        object.__setattr__(self, "credential_policy", CredentialPolicy(self.credential_policy))
        object.__setattr__(self, "authority_mode", AuthorityMode(self.authority_mode))
        # ``standard`` was emitted by the first answer-file revision. Map it to
        # its explicit source set while retaining backwards compatibility.
        preset = "alert-listener-audit" if self.log_preset == "standard" else self.log_preset
        object.__setattr__(self, "log_preset", LogPreset(preset))
        object.__setattr__(self, "services", tuple(sorted({str(service).lower() for service in self.services})))
        username = str(self.monitoring_username).upper()
        if not username.replace("_", "").isalnum() or not username[0].isalpha() or len(username) > 30:
            raise ValueError("monitoring_username must be an Oracle-safe identifier up to 30 characters")
        object.__setattr__(self, "monitoring_username", username)


def fleet_questionnaire() -> tuple[QuestionnaireQuestion, ...]:
    """Return the complete planning questionnaire without prompting or writes."""

    defaults = FleetAnswers()
    return (
        QuestionnaireQuestion("deployment_mode", "Deployment mode", defaults.deployment_mode.value, tuple(mode.value for mode in DeploymentMode)),
        QuestionnaireQuestion("services", "Services to enable", defaults.services, tuple(sorted(ALLOWED_SERVICES))),
        QuestionnaireQuestion("provision_test_dbcs", "Provision an optional DBCS test database", False, ("false", "true")),
        QuestionnaireQuestion("provision_test_autonomous", "Provision an optional Autonomous test database", False, ("false", "true")),
        QuestionnaireQuestion("discovery_filters", "Discovery and selection filters", {}, ()),
        QuestionnaireQuestion("credential_policy", "Credential policy", defaults.credential_policy.value, (
            CredentialPolicy.SHARED_USER_UNIQUE_SECRET.value,
            CredentialPolicy.SHARED_USER_SHARED_SECRET.value,
            CredentialPolicy.DEDICATED_USER_UNIQUE_SECRET.value,
        )),
        QuestionnaireQuestion(
            "log_preset",
            "Log collection preset",
            defaults.log_preset.value,
            tuple(preset.value for preset in LogPreset),
        ),
        QuestionnaireQuestion("authority_mode", "Write authority mode", defaults.authority_mode.value, tuple(mode.value for mode in AuthorityMode)),
        QuestionnaireQuestion("max_concurrency", "Maximum concurrent read or plan operations", defaults.max_concurrency, ()),
        QuestionnaireQuestion("retention_days", "Run evidence retention days", defaults.retention_days, ()),
        QuestionnaireQuestion("common_user", "Use one common monitoring user", False, ("false", "true")),
        QuestionnaireQuestion("pdb_unique_passwords", "Use unique passwords per PDB", False, ("false", "true")),
        QuestionnaireQuestion("monitoring_username", "Monitoring username", defaults.monitoring_username, ()),
    )


def validate_answers(answers: FleetAnswers) -> tuple[str, ...]:
    """Return all safety problems without rewriting an operator's choices."""

    problems: list[str] = []
    invalid_services = sorted(set(answers.services) - set(ALLOWED_SERVICES))
    if invalid_services:
        problems.append(f"services contains unsupported values: {', '.join(invalid_services)}")
    if not answers.services:
        problems.append("at least one service must be selected")
    if not 1 <= answers.max_concurrency <= 8:
        problems.append("max_concurrency must be between 1 and 8")
    if answers.retention_days < 1:
        problems.append("retention_days must be at least 1")
    if answers.deployment_mode is DeploymentMode.PRODUCTION:
        if answers.authority_mode is not AuthorityMode.APPROVAL_REQUIRED:
            problems.append("production requires approval-required authority mode")
        if answers.provision_test_dbcs or answers.provision_test_autonomous:
            problems.append("production cannot provision test databases")
        if answers.credential_policy in (CredentialPolicy.SHARED_USER_SHARED_SECRET, CredentialPolicy.SHARED_PASSWORD):
            problems.append("production forbids shared-password/shared-user-shared-secret credential policy")
    if answers.credential_policy is CredentialPolicy.SHARED_USER_SHARED_SECRET and answers.deployment_mode not in (DeploymentMode.POC, DeploymentMode.DEMO):
        problems.append("shared-user-shared-secret is allowed only in poc/demo with explicit warning")
    if answers.common_user and answers.pdb_unique_passwords:
        problems.append("a common user cannot use unique PDB passwords")
    return tuple(problems)


def require_valid_answers(answers: FleetAnswers) -> FleetAnswers:
    """Return validated answers or explain every unsafe/incompatible choice."""

    problems = validate_answers(answers)
    if problems:
        raise ValueError("invalid fleet answers: " + "; ".join(problems))
    return answers


def answers_from_dict(value: Mapping[str, Any]) -> FleetAnswers:
    """Parse an answer-file mapping, retaining all explicit policy selections."""

    filters = value.get("discovery_filters") or value.get("filters") or {}
    if not isinstance(filters, Mapping):
        raise ValueError("discovery_filters must be a mapping")
    return FleetAnswers(
        deployment_mode="poc" if value.get("deployment_mode") == "pilot" else value.get("deployment_mode", DeploymentMode.PRODUCTION.value),
        services=tuple(value.get("services", DEFAULT_SERVICES)),
        provision_test_dbcs=_boolean(value.get("provision_test_dbcs", False), "provision_test_dbcs"),
        provision_test_autonomous=_boolean(value.get("provision_test_autonomous", False), "provision_test_autonomous"),
        discovery_filters=TargetSelection(**dict(filters)),
        credential_policy=CredentialPolicy(value.get("credential_policy", CredentialPolicy.UNIQUE_VAULT_PER_ACCOUNT.value)),
        log_preset=value.get("log_preset", LogPreset.ALERT_LISTENER_AUDIT.value),
        authority_mode=AuthorityMode(value.get("authority_mode", AuthorityMode.APPROVAL_REQUIRED.value)),
        max_concurrency=int(value.get("max_concurrency", 4)),
        retention_days=int(value.get("retention_days", 7)),
        common_user=_boolean(value.get("common_user", False), "common_user"),
        pdb_unique_passwords=_boolean(value.get("pdb_unique_passwords", False), "pdb_unique_passwords"),
        monitoring_username=str(value.get("monitoring_username", "DBMAN_MON")),
    )


def answers_to_dict(answers: FleetAnswers) -> dict[str, Any]:
    """Return a YAML-safe answer-file payload without credential material."""

    filters = answers.discovery_filters
    return {
        "deployment_mode": answers.deployment_mode.value,
        "services": list(answers.services),
        "provision_test_dbcs": answers.provision_test_dbcs,
        "provision_test_autonomous": answers.provision_test_autonomous,
        "discovery_filters": {
            "regions": list(filters.regions),
            "compartments": list(filters.compartments),
            "kinds": list(filters.kinds),
            "lifecycle_states": list(filters.lifecycle_states),
            "tags": dict(filters.tags),
            "name_pattern": filters.name_pattern,
            "service_states": dict(filters.service_states),
            "target_ids": list(filters.target_ids),
            "exclude_target_ids": list(filters.exclude_target_ids),
            "all_discovered": filters.all_discovered,
        },
        "credential_policy": answers.credential_policy.value,
        "log_preset": answers.log_preset.value,
        "authority_mode": answers.authority_mode.value,
        "max_concurrency": answers.max_concurrency,
        "retention_days": answers.retention_days,
        "common_user": answers.common_user,
        "pdb_unique_passwords": answers.pdb_unique_passwords,
        "monitoring_username": answers.monitoring_username,
    }


def load_answers(path: str | Path) -> FleetAnswers:
    """Load a YAML answer file; validation remains explicit for callers."""

    with Path(path).open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, Mapping):
        raise ValueError("answer files must contain a mapping")
    return answers_from_dict(value)


def _boolean(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "yes", "1"}:
        return True
    if isinstance(value, str) and value.lower() in {"false", "no", "0"}:
        return False
    raise ValueError(f"{field_name} must be a boolean")
