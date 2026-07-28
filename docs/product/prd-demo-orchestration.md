# PRD: Demo Orchestration and Release Evidence

Version: 1.0 · Owner: Luna · Tasks: L-02, L-05, L-06 · Status: In progress

## Outcome

One operator flow runs provision, Vault/user bootstrap, SQLcl MCP check, incident scenario, four-pillar validation, sanitized reporting, and teardown; no untracked live resources remain.

## Requirements

- Use a lifecycle ID and teardown trap in the E2E runner.
- Credential UX lists references/versions, requires explicit role selection and confirmation for retrieval/reset, and gives rollback/remediation guidance.
- Emit redacted `release-evidence.json` and Markdown verdicts for provision, bootstrap, reset, MCP, scenario, pillars, and teardown.
- Keep only sanitized evidence queryable for seven days after stack removal.

## Acceptance

- [ ] DBCS and ADB reports include all phase verdicts.
- [ ] Evidence contains no password, private key, OCID, or topology data.
- [ ] Teardown executes after success, failure, or interruption.
- [ ] No active task lacks owner, difficulty, dependency, and acceptance criteria.

Operational references: [DB incident demo](../demo-db-incident-e2e.md), [workshop](../workshop/README.md), `scripts/demo-db-incident-e2e.sh`.

## Fleet lifecycle release task

| Task | Owner | Depends on | Rollback/handoff | Local evidence | Live gate |
| --- | --- | --- | --- | --- | --- |
| F-07 Scratch-tenancy acceptance and release evidence | Product/release owner | F-01 to F-06 | Stop at the first unapproved write; use signed handoff where access is absent | [Fleet lifecycle runbook](../fleet-lifecycle-runbook.md), fake-fleet acceptance tests | **IN PROGRESS / OWNER INPUT REQUIRED** redacted receipts for every target family |

The release report must distinguish local implementation evidence from live OCI
evidence. It must never promote a local fake-fleet result, a registration response,
or an instruction-only handoff to a completed scratch-tenancy acceptance result.
