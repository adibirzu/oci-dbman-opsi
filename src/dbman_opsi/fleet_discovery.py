"""Read-only, fail-closed discovery of database targets across an OCI tenancy."""

from __future__ import annotations

import concurrent.futures
import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from dbman_opsi.oci_cli import OciCli
from dbman_opsi.redact import redact_text
from dbman_opsi.runner import OciAuthError
from dbman_opsi.status import data_safe_status, dbm_status, opsi_insight_status, opsi_status


DEFAULT_DISCOVERY_WORKERS = 8


@dataclass(frozen=True)
class DiscoveryFinding:
    """An explicit discovery gap; it prevents a result from being planned."""

    scope: str
    message: str


class DiscoveryScopeError(RuntimeError):
    """Raised when callers request targets from an incomplete discovery result."""


def _read(call: Callable[[], Any], default: Any) -> tuple[Any, str | None]:
    """Read once, retry once, and report failure instead of pretending it is empty."""

    error: Exception | None = None
    for _attempt in range(2):
        try:
            return call(), None
        except Exception as exc:  # noqa: BLE001 - OCI facade errors are intentionally isolated
            error = exc
    return default, str(error or "unknown OCI read failure")


def _parallel_map(function: Callable[[Any], Any], values: Iterable[Any], max_workers: int) -> list[Any]:
    materialized = list(values)
    if len(materialized) < 2 or max_workers == 1:
        return [function(value) for value in materialized]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(materialized))) as executor:
        return list(executor.map(function, materialized))


def _value(record: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value):
            return str(value)
    return None


def _tags(record: Mapping[str, Any]) -> Mapping[str, str]:
    values: dict[str, str] = {}
    freeform = record.get("freeform-tags")
    if isinstance(freeform, Mapping):
        values.update({str(key): str(value) for key, value in freeform.items()})
    defined = record.get("defined-tags")
    if isinstance(defined, Mapping):
        for namespace, entries in defined.items():
            if isinstance(entries, Mapping):
                values.update({f"{namespace}.{key}": str(value) for key, value in entries.items()})
    return MappingProxyType(dict(sorted(values.items())))


def _service_states(record: Mapping[str, Any], kind: str, role: str) -> Mapping[str, str]:
    explicit = record.get("service-states") or record.get("service-state")
    values: dict[str, str] = dict(explicit) if isinstance(explicit, Mapping) else {}
    management = dbm_status(dict(record), kind, role)
    if management:
        values.setdefault("dbm", str(management))
    operations = opsi_status(dict(record), kind)
    if operations:
        values.setdefault("opsi", str(operations))
    return MappingProxyType(dict(sorted((str(key), str(value)) for key, value in values.items())))


def _linked_ids(record: Mapping[str, Any]) -> set[str]:
    details = record.get("database-details")
    values: set[Any] = {
        record.get("database-id"),
        record.get("resource-id"),
        record.get("associated-resource-id"),
        record.get("database-resource-id"),
        details.get("database-id") if isinstance(details, Mapping) else None,
    }
    associated = record.get("associated-resource-ids")
    if isinstance(associated, list):
        values.update(associated)
    return {str(value) for value in values if value}


def _external_kind(record: Mapping[str, Any]) -> str | None:
    description = " ".join(
        str(record.get(key, ""))
        for key in ("database-type", "database-resource-type", "deployment-type", "resource-type")
    ).upper()
    if "EXTERNAL" not in description:
        return None
    return "external-exadata" if "EXADATA" in description else "external-db"


def _managed_dbm_state(record: Mapping[str, Any]) -> str:
    """Use managed-database evidence for external targets, not native DB fields."""

    status = _value(record, "database-status", "status")
    if status:
        return status.upper()
    lifecycle = _value(record, "lifecycle-state")
    if lifecycle:
        return "ENABLED" if lifecycle.upper() in {"ACTIVE", "CREATING", "UPDATING"} else lifecycle.upper()
    return "MANAGED"


def _logan_state(
    entities: list[dict[str, Any]],
    associated_entities: list[dict[str, Any]],
    target: "DiscoveredTarget",
) -> str:
    resource_id = target.resource_id or target.target_id
    matching_entity_ids: set[str] = set()
    for entity in entities:
        linked = _linked_ids(entity)
        linked.update(
            str(entity.get(key))
            for key in ("cloud-resource-id", "resource-id", "source-entity-id")
            if entity.get(key)
        )
        if resource_id in linked:
            matching_entity_ids.add(str(entity.get("id") or entity.get("entity-id") or ""))
    for association in associated_entities:
        entity_id = str(association.get("entity-id") or association.get("id") or "")
        if entity_id in matching_entity_ids:
            state = str(association.get("lifecycle-state") or association.get("status") or "ACCEPTED").upper()
            return "ENABLED" if state in {"ACCEPTED", "SUCCEEDED", "ACTIVE"} else state
    return "NOT_ENABLED"


@dataclass(frozen=True)
class DiscoveredTarget:
    """A stable read-only observation used by selection and planning layers."""

    target_id: str
    name: str
    kind: str
    region: str
    compartment_id: str
    lifecycle_state: str = "UNKNOWN"
    resource_id: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)
    service_states: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)
    parent_cdb_id: str | None = None
    settings: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not all((self.target_id, self.name, self.kind, self.region, self.compartment_id)):
            raise ValueError("discovered targets require identity, name, kind, region, and compartment")
        object.__setattr__(self, "tags", MappingProxyType(dict(sorted(self.tags.items()))))
        object.__setattr__(self, "service_states", MappingProxyType(dict(sorted(self.service_states.items()))))
        object.__setattr__(self, "settings", MappingProxyType(dict(sorted(self.settings.items()))))


@dataclass(frozen=True)
class FleetDiscoveryResult:
    """Observed scope and targets; incomplete results are not plan-ready."""

    tenancy_id: str
    regions: tuple[str, ...]
    compartments: tuple[tuple[str, str], ...]
    targets: tuple[DiscoveredTarget, ...]
    findings: tuple[DiscoveryFinding, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.findings

    def require_complete(self) -> "FleetDiscoveryResult":
        if not self.complete:
            details = "; ".join(
                f"{redact_text(finding.scope)}: {redact_text(finding.message)}"
                for finding in self.findings
            )
            raise DiscoveryScopeError(f"fleet discovery is incomplete: {details}")
        return self


class FleetDiscovery:
    """Enumerate target families using only bounded, retry-safe OCI reads."""

    def __init__(
        self,
        oci: OciCli,
        *,
        region_client: Callable[[str], OciCli] | None = None,
        max_workers: int = DEFAULT_DISCOVERY_WORKERS,
    ) -> None:
        if not 1 <= max_workers <= DEFAULT_DISCOVERY_WORKERS:
            raise ValueError(f"max_workers must be between 1 and {DEFAULT_DISCOVERY_WORKERS}")
        self.oci = oci
        self.region_client = region_client or self._default_region_client
        self.max_workers = max_workers

    def _default_region_client(self, region: str) -> OciCli:
        if all(hasattr(self.oci, attribute) for attribute in ("profile", "runner")):
            return OciCli(self.oci.profile, region, self.oci.runner, auth=getattr(self.oci, "auth", None))
        return self.oci

    def discover(self, tenancy_id: str | None = None) -> tuple[DiscoveredTarget, ...]:
        """Return targets only when all scope and service-state reads are complete."""

        return self.discover_result(tenancy_id).require_complete().targets

    def discover_result(self, tenancy_id: str | None = None) -> FleetDiscoveryResult:
        tenancy, tenancy_error = (tenancy_id, None) if tenancy_id else _read(self.oci.profile_tenancy, None)
        if not tenancy:
            message = tenancy_error or "OCI profile tenancy is unavailable"
            return FleetDiscoveryResult("", (), (), (), (DiscoveryFinding("tenancy", message),))
        regions, region_findings = self._regions(str(tenancy))
        compartments, compartment_findings = self._compartments(str(tenancy))
        findings = [*region_findings, *compartment_findings]
        observations: list[DiscoveredTarget] = []
        for region in regions:
            targets, regional_findings = self._discover_region(region, compartments)
            observations.extend(targets)
            findings.extend(regional_findings)
        deduplicated: dict[str, DiscoveredTarget] = {}
        for target in sorted(observations, key=_target_sort_key):
            deduplicated.setdefault(target.target_id, target)
        return FleetDiscoveryResult(
            str(tenancy),
            regions,
            compartments,
            tuple(sorted(deduplicated.values(), key=_target_sort_key)),
            tuple(sorted(findings, key=lambda finding: (finding.scope, finding.message))),
        )

    def _regions(self, tenancy_id: str) -> tuple[tuple[str, ...], list[DiscoveryFinding]]:
        records, error = _read(lambda: self.oci.list_subscribed_regions(tenancy_id), [])
        names = {_value(record, "region-name", "region-key", "name") for record in records if isinstance(record, Mapping)}
        names.discard(None)
        findings: list[DiscoveryFinding] = []
        if error:
            findings.append(DiscoveryFinding("regions", error))
        if not names and getattr(self.oci, "region", None):
            names.add(str(self.oci.region))
        if not names:
            findings.append(DiscoveryFinding("regions", "no subscribed regions were returned"))
        return tuple(sorted(names)), findings

    def _compartments(self, tenancy_id: str) -> tuple[tuple[tuple[str, str], ...], list[DiscoveryFinding]]:
        records, error = _read(lambda: self.oci.list_compartments(tenancy_id), [])
        if error:
            return (), [DiscoveryFinding("compartments", error)]
        seen: dict[str, str] = {tenancy_id: tenancy_id}
        for record in records:
            lifecycle_state = _value(record, "lifecycle-state")
            if lifecycle_state and lifecycle_state.upper() != "ACTIVE":
                continue
            if isinstance(record, Mapping) and (identifier := _value(record, "id")):
                seen.setdefault(identifier, _value(record, "name", "display-name") or identifier)
        return tuple(sorted(seen.items())), []

    def _discover_region(
        self,
        region: str,
        compartments: tuple[tuple[str, str], ...],
    ) -> tuple[list[DiscoveredTarget], list[DiscoveryFinding]]:
        client = self.region_client(region)
        batches = _parallel_map(
            lambda item: self._discover_compartment(client, region, *item),
            compartments,
            self.max_workers,
        )
        return (
            [target for targets, _findings in batches for target in targets],
            [finding for _targets, findings in batches for finding in findings],
        )

    def _call(
        self,
        client: OciCli,
        method: str,
        *args: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        callback = getattr(client, method, None)
        if not callable(callback):
            return [], None
        result, error = _read(lambda: callback(*args), [])
        return (result if isinstance(result, list) else []), error

    def _discover_compartment(
        self,
        client: OciCli,
        region: str,
        compartment_id: str,
        _compartment_name: str,
    ) -> tuple[list[DiscoveredTarget], list[DiscoveryFinding]]:
        findings: list[DiscoveryFinding] = []

        def read(method: str, *args: str) -> list[dict[str, Any]]:
            values, error = self._call(client, method, *args)
            if error:
                findings.append(DiscoveryFinding(f"{region}/{compartment_id}/{method}", error))
            return values

        systems = read("list_db_systems", compartment_id)
        targets: list[DiscoveredTarget] = []
        families: dict[str, tuple[str, str, str]] = {}
        for system in systems:
            system_id = _value(system, "id")
            if not system_id:
                continue
            family = "exadata" if "EXA" in str(system.get("shape", "")).upper() else "dbcs"
            self._read_db_home_topology(client, compartment_id, db_system_id=system_id, findings=findings, region=region)
            for record in read("list_databases", compartment_id, system_id):
                target = self._target(record, family, region, compartment_id, db_system_id=system_id)
                targets.append(target)
                if target.resource_id:
                    families[target.resource_id] = (family, "db_system", system_id)

        seen_vm_clusters: set[str] = set()
        for method in ("list_cloud_vm_clusters", "list_vm_clusters"):
            for cluster in read(method, compartment_id):
                cluster_id = _value(cluster, "id")
                if not cluster_id or cluster_id in seen_vm_clusters:
                    continue
                seen_vm_clusters.add(cluster_id)
                self._read_db_home_topology(client, compartment_id, vm_cluster_id=cluster_id, findings=findings, region=region)
                for record in read("list_databases_for_vm_cluster", compartment_id, cluster_id):
                    target = self._target(record, "exadata", region, compartment_id, vm_cluster_id=cluster_id)
                    targets.append(target)
                    if target.resource_id:
                        families[target.resource_id] = ("exadata", "vm_cluster", cluster_id)

        for record in read("list_pluggable_databases", compartment_id):
            parent = _value(record, "container-database-id", "database-id", "cdb-id", "parent-database-id")
            family, parent_type, parent_id = families.get(parent or "", ("dbcs", "db_system", _value(record, "db-system-id") or ""))
            targets.append(
                self._target(
                    record,
                    family,
                    region,
                    compartment_id,
                    role="PDB",
                    db_system_id=parent_id if parent_type == "db_system" and parent_id else None,
                    vm_cluster_id=parent_id if parent_type == "vm_cluster" and parent_id else None,
                )
            )

        for record in read("list_autonomous_databases", compartment_id):
            targets.append(self._target(record, "autonomous", region, compartment_id))

        native_ids = {target.resource_id for target in targets if target.resource_id}
        for record in read("list_managed_databases", compartment_id):
            if native_ids.intersection(_linked_ids(record)):
                continue
            if kind := _external_kind(record):
                targets.append(
                    self._target(
                        record,
                        kind,
                        region,
                        compartment_id,
                        managed_dbm_state=_managed_dbm_state(record),
                    )
                )

        return self._with_service_states(client, region, compartment_id, targets, findings), findings

    def _read_db_home_topology(
        self,
        client: OciCli,
        compartment_id: str,
        *,
        findings: list[DiscoveryFinding],
        region: str,
        db_system_id: str | None = None,
        vm_cluster_id: str | None = None,
    ) -> None:
        method = getattr(client, "list_db_homes", None)
        if not callable(method):
            return
        parameters = inspect.signature(method).parameters
        if "db_system_id" in parameters or "vm_cluster_id" in parameters:
            _homes, error = _read(
                lambda: method(compartment_id, db_system_id=db_system_id, vm_cluster_id=vm_cluster_id),
                [],
            )
        else:
            # Compatibility fallback is based solely on the callable's legacy
            # signature, never on whether an otherwise valid list is empty.
            parent_id = db_system_id or vm_cluster_id
            _homes, error = _read(lambda: method(compartment_id, parent_id), [])
        if error:
            findings.append(DiscoveryFinding(f"{region}/{compartment_id}/list_db_homes", error))

    def _with_service_states(
        self,
        client: OciCli,
        region: str,
        compartment_id: str,
        targets: list[DiscoveredTarget],
        findings: list[DiscoveryFinding],
    ) -> list[DiscoveredTarget]:
        if not targets:
            return targets
        insights, insight_error = self._opsi_insights(client, compartment_id)
        safe_targets, safe_error = self._call(client, "list_data_safe_targets", compartment_id)
        entities, associated_entities, log_error, log_namespace_onboarding_required = self._log_entities(
            client,
            compartment_id,
        )
        for scope, error in (("opsi", insight_error), ("datasafe", safe_error), ("logan", log_error)):
            if error:
                findings.append(DiscoveryFinding(f"{region}/{compartment_id}/{scope}", error))
        enriched: list[DiscoveredTarget] = []
        for target in targets:
            states = dict(target.service_states)
            resource_id = target.resource_id or target.target_id
            role = str(target.settings.get("database_role", "CDB"))
            if insight_error:
                states["opsi"] = "UNKNOWN"
            else:
                states["opsi"] = opsi_insight_status(insights, resource_id)
            if safe_error:
                states["datasafe"] = "UNKNOWN"
            else:
                states["datasafe"] = data_safe_status(
                    safe_targets,
                    resource_id,
                    db_system_id=target.settings.get("db_system_id"),
                    service_name=target.name if role == "PDB" else None,
                )
            states["logan"] = "UNKNOWN" if log_error else _logan_state(entities, associated_entities, target)
            settings = dict(target.settings)
            if log_namespace_onboarding_required:
                settings["logan_onboard_namespace"] = True
            enriched.append(replace(target, service_states=states, settings=settings))
        return enriched

    def _opsi_insights(self, client: OciCli, compartment_id: str) -> tuple[list[dict[str, Any]], str | None]:
        complete_method = getattr(client, "list_opsi_database_insights_complete", None)
        if callable(complete_method):
            result, error = _read(lambda: complete_method(compartment_id), ([], False))
            if error:
                return [], error
            if not isinstance(result, tuple) or len(result) != 2:
                return [], "OPSI database insight read returned an invalid completeness result"
            insights, complete = result
            if not complete:
                return list(insights) if isinstance(insights, list) else [], "OPSI database insight read was incomplete"
            return list(insights) if isinstance(insights, list) else [], None
        return self._call(client, "list_opsi_database_insights", compartment_id)

    def _log_entities(
        self,
        client: OciCli,
        compartment_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None, bool]:
        namespace_method = getattr(client, "get_log_analytics_namespace", None)
        entity_method = getattr(client, "list_log_analytics_entities", None)
        if not callable(namespace_method) or not callable(entity_method):
            return [], [], None, False
        try:
            namespace = namespace_method(compartment_id)
        except OciAuthError as exc:
            # OCI deliberately uses NotAuthorizedOrNotFound when a subscribed
            # region has not yet been onboarded to Log Analytics. Treat that
            # state as an approval-gated regional prerequisite. The eventual
            # onboard call remains authoritative and will still fail closed if
            # the caller genuinely lacks permission.
            if "NotAuthorizedOrNotFound" in str(exc):
                return [], [], None, True
            return [], [], str(exc), False
        except Exception as exc:
            return [], [], str(exc), False
        if not namespace:
            return [], [], None, True
        entities, entity_error = self._call(client, "list_log_analytics_entities", str(namespace), compartment_id)
        associations, association_error = self._call(
            client,
            "list_log_analytics_associated_entities",
            str(namespace),
            compartment_id,
        )
        return entities, associations, entity_error or association_error, False

    @staticmethod
    def _target(
        record: Mapping[str, Any],
        kind: str,
        region: str,
        compartment_id: str,
        *,
        role: str = "CDB",
        db_system_id: str | None = None,
        vm_cluster_id: str | None = None,
        managed_dbm_state: str | None = None,
    ) -> DiscoveredTarget:
        resource_id = _value(record, "id")
        name = _value(record, "display-name", "pdb-name", "db-name", "name") or resource_id or "unnamed"
        target_id = resource_id or f"{region}:{compartment_id}:{kind}:{name}"
        parent = _value(record, "container-database-id", "database-id", "cdb-id", "parent-database-id") if role == "PDB" else None
        settings: dict[str, Any] = {"database_role": role, "database_family": kind}
        if db_system_id:
            settings["db_system_id"] = db_system_id
        if vm_cluster_id:
            settings["vm_cluster_id"] = vm_cluster_id
        return DiscoveredTarget(
            target_id=target_id,
            resource_id=resource_id,
            name=name,
            kind=kind,
            region=region,
            compartment_id=compartment_id,
            lifecycle_state=_value(record, "lifecycle-state", "status") or "UNKNOWN",
            tags=_tags(record),
            service_states={**_service_states(record, kind, role), **({"dbm": managed_dbm_state} if managed_dbm_state else {})},
            parent_cdb_id=parent,
            settings=settings,
        )


def _target_sort_key(target: DiscoveredTarget) -> tuple[str, str, str, str, str]:
    return (target.region, target.compartment_id, target.kind, target.name, target.target_id)
