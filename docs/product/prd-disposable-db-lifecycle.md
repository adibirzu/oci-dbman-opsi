# PRD: Disposable Database Lifecycle

Version: 1.0 · Owner: Terra · Tasks: T-01, T-05, T-06 · Status: In progress

## Outcome

Provision one project-tagged DBCS and one project-tagged Autonomous Database, then remove only resources proven to belong to that lifecycle. Retain sanitized Log Analytics evidence for seven days.

## Requirements

- Create isolated network resources only when requested and apply lifecycle, disposable, and evidence-retention tags to project-owned resources.
- Outputs provide IDs, lifecycle ID, retention target, and destroy guidance, never passwords, private keys, or secret values.
- Teardown discovers by lifecycle tag, shows a dependency-ordered plan, and never selects shared resources merely by name.
- E2E always runs teardown through a `finally`/trap path and publishes redacted evidence.

## Acceptance

- [ ] `terraform validate`, plan, apply, import, and destroy work for DBCS and ADB.
- [ ] Destroy plan contains only lifecycle-tagged resources and is idempotent.
- [ ] Seven-day evidence expiry is configured and verified.

Operational references: [workshop](../workshop/README.md), [architecture](../architecture.md).

## Fleet lifecycle implementation tasks

| Task | Owner | Depends on | Rollback/handoff | Local evidence | Live gate |
| --- | --- | --- | --- | --- | --- |
| F-02 Discover and select all reachable targets | Discovery engineer | F-01 | Read-only; inaccessible scopes fail planning | Fleet discovery/selection tests | **OWNER INPUT REQUIRED** redacted multi-region receipt |
| F-03 Onboard CDB before PDB and preserve checkpoints | Lifecycle engineer | F-01, F-02 | Resume exact plan or issue signed DBA handoff | Fleet executor tests | **OWNER INPUT REQUIRED** per-target receipt |
| F-04 Disable PDB before CDB and delete only run-owned resources | Lifecycle engineer | F-01, F-03 | Re-run exact cleanup plan; production refuses DB deletion | Fleet offboarding tests | **OWNER INPUT REQUIRED** final inventory |

Acceptance is local only until the [fleet lifecycle runbook](../fleet-lifecycle-runbook.md)
matrix has a redacted scratch-tenancy receipt. Production offboarding is service-only:
database deletion is never an allowed production rollback.
