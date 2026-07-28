"""Concrete adapters from immutable fleet targets to existing service modules."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping

from dbman_opsi.config import EnablementConfig, LogAnalyticsSelection, Target
from dbman_opsi.credentials import CredentialService
from dbman_opsi.enablement import EnablementService
from dbman_opsi.fleet import FleetPlan, ReadinessVerdict, ResourceEffect, ResourceOwnership, ResourceRecord, TargetPlan
from dbman_opsi.fleet_executor import PhaseOutcome
from dbman_opsi.log_analytics import LogAnalyticsService
from dbman_opsi.oci_cli import OciCli
from dbman_opsi.prerequisites import PrerequisiteService
from dbman_opsi.validation import ValidationService
from dbman_opsi.datasafe import DataSafeService


class LifecycleOperations:
    """Use established OCI services only when the reviewed plan is sufficient.

    Missing fields become a durable handoff instead of synthetic success.
    """

    def __init__(self, plan: FleetPlan, oci: OciCli, *, collection_proofs: Mapping[str, tuple[Mapping[str, object], ...]] | None = None) -> None:
        self.plan, self.oci = plan, oci
        self.enablement = EnablementService(oci)
        self.credentials = CredentialService(oci)
        self.prerequisites = PrerequisiteService(oci)
        self.log_analytics = LogAnalyticsService(oci)
        self.data_safe = DataSafeService(oci)
        self.collection_proofs = dict(collection_proofs or {})

    def handlers(self) -> Mapping[str, Callable[[TargetPlan], PhaseOutcome | None]]:
        return {
            "prerequisites": self.prerequisite,
            "test-databases": self.test_databases,
            "vault-endpoints": self.vault_endpoints,
            "db-host-automation": self.host_automation,
            "dbm": self.dbm,
            "credentials": self.preferred_credentials,
            "opsi": self.opsi,
            "datasafe": self.datasafe,
            "agent-log-analytics": self.agent_log_analytics,
            "validation": self.validation,
        }

    def _target(self, plan: TargetPlan) -> Target:
        values = dict(plan.settings)
        allowed = set(Target.__dataclass_fields__)
        values = {key: value for key, value in values.items() if key in allowed}
        values.update({"kind": plan.kind, "name": plan.name, "region": plan.region, "compartment_id": plan.compartment_id, "resource_id": plan.resource_id, "services": tuple(plan.services)})
        return Target(**values)

    def _config(self, plan: TargetPlan) -> EnablementConfig:
        settings = plan.settings
        log_analytics = LogAnalyticsSelection(
            namespace=settings.get("logan_namespace") or self.plan.settings.get("logan_namespace"),
            log_group_id=settings.get("logan_log_group_id") or self.plan.settings.get("logan_log_group_id"),
            log_group_name=str(settings.get("logan_log_group_name") or self.plan.settings.get("logan_log_group_name") or "dbman-opsi-logan"),
            onboard_namespace=bool(settings.get("logan_onboard_namespace", False)),
        )
        return EnablementConfig(
            profile=self.plan.profile,
            region=plan.region,
            compartment_id=plan.compartment_id,
            log_analytics=log_analytics,
            targets=(self._target(plan),),
        )

    @staticmethod
    def _needs(target: Target, *fields: str) -> PhaseOutcome | None:
        missing = [field for field in fields if not getattr(target, field, None)]
        public_labels = {
            "management_agent_id": "management agent reference",
            "opsi_private_endpoint_id": "OPSI endpoint reference",
            "password_secret_id": "credential reference",
            "private_endpoint_id": "DBM endpoint reference",
            "resource_id": "database resource reference",
        }
        return (
            PhaseOutcome.handoff(
                "operator input required: "
                + ", ".join(public_labels.get(field, "approved resource reference") for field in missing)
            )
            if missing
            else None
        )

    def prerequisite(self, plan: TargetPlan) -> PhaseOutcome:
        if plan.settings.get("authority_mode") == "plan-only":
            return PhaseOutcome.handoff("reviewed authority mode is plan-only")
        target = self._target(plan)
        if target.kind in {"external-db", "external-exadata"}:
            return PhaseOutcome.handoff("DBA/host administrator must provide approved Management Agent evidence")
        return PhaseOutcome()

    def test_databases(self, plan: TargetPlan) -> PhaseOutcome:
        if bool(plan.settings.get("provision", False)):
            return PhaseOutcome.handoff("test database provisioning requires reviewed Terraform/disposable workflow inputs")
        return PhaseOutcome()

    def vault_endpoints(self, plan: TargetPlan) -> PhaseOutcome:
        target = self._target(plan)
        # DBM is the only selected lifecycle pillar that needs these DBM refs;
        # Data Safe/Log Analytics-only plans must not be blocked by them.
        required = self._needs(target, "password_secret_id", "private_endpoint_id") if target.kind in {"dbcs", "exadata"} and "dbm" in target.services else None
        if required:
            return required
        return PhaseOutcome()

    def host_automation(self, plan: TargetPlan) -> PhaseOutcome:
        target = self._target(plan)
        if target.kind in {"external-db", "external-exadata"}:
            return PhaseOutcome.handoff("approved host access or imported DBA completion evidence is required")
        return PhaseOutcome()

    def dbm(self, plan: TargetPlan) -> PhaseOutcome:
        if "dbm" not in plan.services:
            return PhaseOutcome(message="skipped: dbm is not selected for target")
        target = self._target(plan)
        required = self._needs(target, "resource_id")
        if required:
            return required
        # Do not use the historical combined adapter here: a DBM-only plan is
        # not authorization to create an OPSI insight.
        created = self.enablement.enable_dbm(target)
        kind = "dbm-autonomous" if target.kind == "autonomous" else "dbm-pdb" if str(target.database_role).upper() == "PDB" else "dbm-cdb"
        ownership = ResourceOwnership.OWNED if created else ResourceOwnership.REUSED
        effect = ResourceEffect.CREATED if created else ResourceEffect.REUSED
        return PhaseOutcome(resources=(ResourceRecord(
            kind, target.resource_id or plan.target_id, ownership, bool(created),
            attributes={"lifecycle_dbm": True, "target_kind": target.kind}, effect=effect,
        ),))

    def preferred_credentials(self, plan: TargetPlan) -> PhaseOutcome:
        target, config = self._target(plan), self._config(plan)
        if "dbm" not in target.services:
            return PhaseOutcome()
        if target.kind == "autonomous":
            return PhaseOutcome(
                message="skipped: Autonomous Database uses service-managed DBM credentials"
            )
        required = self._needs(target, "password_secret_id", "resource_id")
        if required:
            return required
        decision = self.credentials.set_for_target(target, config)
        if decision.status != "set":
            return PhaseOutcome.handoff(f"preferred credentials {decision.status}: {decision.detail}")
        if decision.created:
            if not decision.named_credential_id:
                return PhaseOutcome.handoff("named credential was created but its authoritative identifier was not returned")
            return PhaseOutcome(message=decision.detail, resources=(ResourceRecord(
                "named-credential", decision.named_credential_id, ResourceOwnership.OWNED, True,
                attributes={"credential_id": decision.named_credential_id}, effect=ResourceEffect.CREATED,
            ),))
        # Existing preferred credentials have no cleanup authority.  Do not use
        # the managed database OCID as a synthetic named-credential reference.
        return PhaseOutcome(message=decision.detail, resources=(ResourceRecord(
            "preferred-credential", target.resource_id or plan.target_id,
            ResourceOwnership.PREEXISTING, False, effect=ResourceEffect.PREEXISTING,
        ),))

    def opsi(self, plan: TargetPlan) -> PhaseOutcome:
        if "opsi" not in plan.services:
            return PhaseOutcome(message="skipped: opsi is not selected for target")
        target = self._target(plan)
        if target.kind in {"external-db", "external-exadata"}:
            return PhaseOutcome.handoff("OPSI for external targets requires signed host/DBA completion evidence")
        try:
            created = self.enablement.enable_opsi(target)
        except ValueError as exc:
            return PhaseOutcome.handoff(str(exc))
        reference = self._opsi_reference(target)
        if not reference:
            return PhaseOutcome.handoff("OPSI enablement completed but authoritative Database Insight ID could not be resolved")
        ownership = ResourceOwnership.OWNED if created else ResourceOwnership.REUSED
        effect = ResourceEffect.CREATED if created else ResourceEffect.REUSED
        return PhaseOutcome(resources=(ResourceRecord("opsi-insight", reference, ownership, bool(created), effect=effect),))

    def datasafe(self, plan: TargetPlan) -> PhaseOutcome:
        if "datasafe" not in plan.services:
            return PhaseOutcome(message="skipped: datasafe is not selected for target")
        target = self._target(plan)
        # The generic Data Safe adapter intentionally has no password authority.
        # A binding-backed provider may be installed by the CLI; otherwise stop
        # at the signed handoff boundary instead of attempting a blank password.
        if self.data_safe.credential_provider is None:
            return PhaseOutcome.handoff("Data Safe requires a signed binding-backed credential provider")
        decision = self.data_safe.enable_target(target, self._config(plan))
        if decision.status in {"blocked", "skipped"}:
            return PhaseOutcome.handoff("Data Safe " + decision.detail)
        if not decision.target_id:
            return PhaseOutcome(message=decision.detail)
        ownership = ResourceOwnership.REUSED if target.data_safe_target_id == decision.target_id else ResourceOwnership.OWNED
        effect = ResourceEffect.REUSED if ownership is ResourceOwnership.REUSED else ResourceEffect.CREATED
        return PhaseOutcome(message=decision.detail, resources=(ResourceRecord(
            "datasafe-target",
            decision.target_id,
            ownership,
            ownership is ResourceOwnership.OWNED,
            attributes={"target_database_id": decision.target_id},
            effect=effect,
        ),))

    def agent_log_analytics(self, plan: TargetPlan) -> PhaseOutcome:
        target = self._target(plan)
        if "logan" not in target.services:
            return PhaseOutcome()
        if not (target.management_agent_id or target.logan_management_agent_id):
            return PhaseOutcome.handoff("approved Management Agent and Log Analytics entity identifiers are required")
        config = self._config(plan)
        decisions = self.log_analytics.enable_all(config)
        blocked = [item.detail for item in decisions if item.status == "blocked"]
        if blocked:
            return PhaseOutcome.handoff("Log Analytics blocked: " + "; ".join(blocked))
        # Association is configuration evidence only; validation owns any READY
        # verdict after an independent search result proves collection.
        resources = []
        for decision in decisions:
            if decision.target != target.name or decision.status != "configured":
                continue
            created_items = tuple(getattr(decision, "created_association_items", ()))
            for association in tuple(getattr(decision, "association_items", ())):
                source_name = str(association.get("sourceName") or association.get("source-name") or "source")
                entity_id = str(association.get("entityId") or association.get("entity-id") or "entity")
                created = association in created_items
                ownership = ResourceOwnership.OWNED if created else ResourceOwnership.PREEXISTING
                effect = ResourceEffect.CREATED if created else ResourceEffect.PREEXISTING
                resources.append(ResourceRecord(
                    "logan-association",
                    f"{entity_id}:{source_name}",
                    ownership,
                    created,
                    attributes={
                        "namespace": getattr(decision, "namespace", None) or config.log_analytics.namespace,
                        "compartment_id": getattr(decision, "compartment_id", None) or config.compartment_id,
                        "items": (dict(association),),
                    },
                    effect=effect,
                ))
        return PhaseOutcome(readiness=ReadinessVerdict.COLLECTING, message="Log Analytics associations configured; awaiting searchable records", resources=tuple(resources))

    def _opsi_reference(self, target: Target) -> str | None:
        if target.opsi_database_insight_id:
            return target.opsi_database_insight_id
        # Creation returns only a boolean in the compatibility service. Re-read
        # the authoritative collection before checkpointing so manifests never
        # substitute a database OCID for the actual insight OCID.
        if not target.compartment_id or not target.resource_id:
            return None
        try:
            insights = self.oci.list_opsi_database_insights(target.compartment_id)
        except (AttributeError, RuntimeError):
            return None
        for insight in insights:
            if insight.get("database-id") == target.resource_id and isinstance(insight.get("id"), str):
                return insight["id"]
        return None

    def validation(self, plan: TargetPlan) -> PhaseOutcome:
        validator = ValidationService(self.oci)
        findings = validator.validate(self._config(plan))
        proof_resources: list[ResourceRecord] = []
        # Fresh query results are the only automatic collection promotion path;
        # registration/status text remains merely configured/collecting.
        for proof in validator.collection_proofs(self._config(plan)):
            if proof.collecting and proof.observed_at is not None:
                timestamp = int(proof.observed_at.timestamp())
                marker = "DBM collection proof timestamp=" if proof.service == "dbm" else "OPSI observation timestamp=" if proof.service == "opsi" else "Log Analytics query result count=1 timestamp="
                findings.append(marker + proof.observed_at.isoformat())
                digest = hashlib.sha256(
                    f"{plan.target_id}:{proof.service}:{timestamp}".encode("utf-8")
                ).hexdigest()
                proof_resources.append(ResourceRecord(
                    "collection-proof",
                    digest,
                    ResourceOwnership.PREEXISTING,
                    False,
                    attributes={
                        "service": proof.service,
                        "timestamp": timestamp,
                        "selected_services": tuple(plan.services),
                    },
                    effect=ResourceEffect.PREEXISTING,
                ))
            elif proof.status in {"stale", "absent"}:
                findings.append(f"{proof.service.upper()} {proof.status.upper()}")
        # Signed imports are private manifest records. They are accepted only
        # for the exact target/service and only while fresh; a replayed digest
        # cannot elevate another target or an unselected service.
        now = time.time()
        for proof in self.collection_proofs.get(plan.target_id, ()):
            service = str(proof.get("service", ""))
            timestamp = proof.get("timestamp")
            if (
                service not in plan.services
                or not isinstance(timestamp, int)
                or timestamp > now + 60
                or now - timestamp > 900
            ):
                continue
            marker = "DBM collection proof timestamp=" if service == "dbm" else "OPSI observation timestamp=" if service == "opsi" else "Log Analytics query result count=1 timestamp="
            findings.append(marker + str(timestamp))
        target = self._target(plan)
        verdict = collection_verdict(target.services, findings)
        return PhaseOutcome(
            readiness=verdict,
            message="; ".join(findings),
            resources=tuple(proof_resources),
        )


def collection_verdict(services: tuple[str, ...], findings: list[str]) -> ReadinessVerdict:
    """Require independent, current collection proof for every requested service.

    These narrow machine-oriented markers are intentionally distinct from OCI
    registration/status output (for example ``ACTIVE`` or ``ENABLED``).
    """
    joined = "\n".join(findings)
    upper = joined.upper()
    if any(marker in upper for marker in (" DEGRADED", " FAILED", " ERROR")):
        return ReadinessVerdict.DEGRADED
    proofs = {
        "dbm": any("DBM collection proof timestamp=" in line for line in findings),
        "opsi": any("OPSI observation timestamp=" in line for line in findings),
        "logan": any("Log Analytics query result count=" in line and not line.rstrip().endswith(("=0", "=available")) for line in findings),
    }
    # Services without a defined bounded proof contract are never promoted to
    # READY merely because a control-plane registration exists.
    return ReadinessVerdict.READY if all(proofs.get(service, False) for service in services) else ReadinessVerdict.COLLECTING
