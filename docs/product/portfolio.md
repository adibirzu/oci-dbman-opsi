# OCI Database Observability Portfolio

Version: 1.1 · Release: Disposable E2E Demo plus fleet-lifecycle implementation · Last reviewed: 2026-07-27

This is the planning index for the disposable DBCS and Autonomous Database release. Runbooks explain operation; PRDs own scope and acceptance.

| Workstream | Owner | Difficulty | PRD | Dependencies | Status |
| --- | --- | --- | --- | --- | --- |
| DBCS/ADB lifecycle (T-01, T-05, T-06) | Terra | High | [Lifecycle](prd-disposable-db-lifecycle.md) | Vault, observability | In progress |
| Vault credential lifecycle (T-02, T-03, T-04) | Terra | High | [Credentials](prd-vault-credential-lifecycle.md) | IAM, DB admin | In progress |
| SQLcl MCP (L-01) | Luna | High | [SQLcl MCP](prd-sqlcl-mcp-integration.md) | Vault, MCP_READONLY | In progress |
| Four-pillar evidence (L-03, L-04) | Luna | High | [Observability 360](prd-observability-360.md) | Lifecycle, credentials | In progress |
| Scenario and release report (L-02, L-05, L-06) | Luna | Medium | [Orchestration](prd-demo-orchestration.md) | All above | In progress |
| Production fleet lifecycle (F-01 to F-07) | Product/release owner | High | [Lifecycle](prd-disposable-db-lifecycle.md), [Credentials](prd-vault-credential-lifecycle.md), [Observability](prd-observability-360.md), [Orchestration](prd-demo-orchestration.md) | Existing enablement, signed handoffs | Local implementation complete; live gates in progress |

| Existing operational surface | Owning PRD |
| --- | --- |
| Terraform DBCS/ADB examples, import outputs, regional provisioning | [Lifecycle](prd-disposable-db-lifecycle.md) |
| Vault prerequisites and named credentials | [Credentials](prd-vault-credential-lifecycle.md) |
| Generated `MCP-HANDOFF.md`, incident packet, SQLcl | [SQLcl MCP](prd-sqlcl-mcp-integration.md) |
| DBM/OPSI/Data Safe/Log Analytics enablement and export | [Observability 360](prd-observability-360.md) |
| E2E script, workshop, teardown | [Orchestration](prd-demo-orchestration.md) |

## Release acceptance

- [ ] Fresh tagged DBCS and ADB lifecycles complete.
- [ ] Four role-specific Vault references exist; one role reset is proven.
- [ ] DBM, OPSI, Data Safe, and Log Analytics have ready/degraded/blocked verdicts.
- [ ] SQLcl MCP uses `MCP_READONLY`; writes are rejected.
- [ ] Sanitized evidence is retained for seven days and tagged live resources are removed.

## Fleet lifecycle delivery ledger

| Task | Owner | Dependency | Rollback/handoff | Local evidence | Live acceptance |
| --- | --- | --- | --- | --- | --- |
| F-01 Immutable plans and state | Fleet lifecycle engineer | Existing config | Stop and resume from `0600` state | Fleet model/state tests | **OWNER INPUT REQUIRED** redacted scratch-state receipt |
| F-02 Discovery and selection | Discovery engineer | F-01 | Read-only; report inaccessible scopes | Discovery/selection/answer tests | **OWNER INPUT REQUIRED** whole-tenancy discovery receipt |
| F-03 Onboard and status | Lifecycle engineer | F-01, F-02 | Exact-plan resume or signed handoff | Executor/CLI tests | **OWNER INPUT REQUIRED** collection timestamps |
| F-04 Safe offboard | Lifecycle engineer | F-01, F-03 | Exact cleanup rerun; owned resources only | Offboarding/facade tests | **OWNER INPUT REQUIRED** zero-run-owned inventory |
| F-05 CLI and portable state | CLI engineer | F-01 to F-04 | Local cache is `0600`; checksum/ETag fail closed | CLI boundary tests | **OWNER INPUT REQUIRED** approved auth transcript |
| F-06 Terraform/security | Terraform/security engineer | Existing modules | Disable through reviewed toggles | Terraform/security gate | **OWNER INPUT REQUIRED** redacted apply/disable receipt |
| F-07 Release documentation | Product/release owner | F-01 to F-06 | Use the signed handoff/acceptance matrix | [Fleet lifecycle runbook](../fleet-lifecycle-runbook.md) and [local verification receipt](fleet-lifecycle-local-verification.md) | **IN PROGRESS / OWNER INPUT REQUIRED** |

No row above represents live OCI proof. Local tests establish implementation
behavior only; the linked runbook names the authority, credential, and redacted
artifact required to close each live gate.

High-difficulty work requires a redacted E2E artifact. Medium work requires test coverage and an operator path. Terra and Luna cross-review acceptance criteria.
