# PRD: Observability 360

Version: 1.0 · Owner: Luna · Tasks: L-03, L-04 · Status: In progress

## Outcome

Validate Database Management, Operations Insights, Data Safe, and Log Analytics as distinct collection pillars. Registration alone is not evidence of collection.

## Requirements

- Report DBM managed state and OPSI insight state.
- Report Data Safe target, audit profile, audit trail, and a real audit event separately.
- Report Log Analytics association and searchable evidence query separately.
- Provide dashboards for health, capacity, audit activity, incident timeline, credential lifecycle, and teardown evidence; filter by scenario ID and lifecycle tag.

## Acceptance

- [ ] One JSON/human report marks every pillar ready, degraded, or blocked.
- [ ] Fresh DBCS and ADB runs populate expected dashboard panels.
- [ ] Data Safe proof includes target, profile, trail, event, connector, and searchable Log Analytics row.

Operational references: [Data Safe to Log Analytics](../datasafe-log-analytics.md), [DB incident troubleshooting](../db-incident-troubleshooting.md).

## Fleet lifecycle implementation tasks

| Task | Owner | Depends on | Rollback/handoff | Local evidence | Live gate |
| --- | --- | --- | --- | --- | --- |
| F-02 Preserve requested services and log preset in planning | Discovery engineer | F-01 | Re-plan; no write occurs during discovery | Answers/selection tests | **OWNER INPUT REQUIRED** reviewed selection receipt |
| F-03 Configure pillars and report collection separately | Lifecycle engineer | F-01, F-02 | Handoff if approved host/agent proof is unavailable | Executor/collection tests | **OWNER INPUT REQUIRED** current collection queries |
| F-04 Reverse Log Analytics, OPSI, Data Safe, and DBM associations safely | Lifecycle engineer | F-03 | Exact idempotent cleanup plan | Offboarding tests | **OWNER INPUT REQUIRED** dissociation/unregistration receipt |

`configured` and `collecting` are not `ready`. The default log preset is alert,
listener, and audit; only an independent searchable Log Analytics result, plus the
requested DBM/OPSI collection proofs, may close the corresponding live gate.
