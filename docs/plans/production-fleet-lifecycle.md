# Production-Ready OCI Database Fleet Lifecycle

## Global Constraints

- Preserve all existing low-level CLI commands and existing configuration compatibility.
- Discovery is read-only and defaults to all subscribed regions and accessible compartments.
- Treat CDBs and PDBs as separate targets; enable CDB before PDB and disable PDB before CDB.
- Default credentials are one monitoring username with a unique Vault-backed credential per independent database account. Shared passwords are forbidden in production.
- Never persist plaintext database passwords. Topology-bearing files must be ignored and mode `0600`; public evidence must be redacted.
- Every write is plan-gated, idempotent, checkpointed, and reversible. Continue independent targets after failures.
- Cleanup disables only services enabled by the run and deletes only resources recorded as created and lifecycle-owned. Production never deletes databases.
- Registration is not collection proof. Keep DBM, OPSI, Data Safe, and Log Analytics states distinct.
- Default logs are alert, listener, and audit; evidence retention is seven days.
- Local proof, live-tenancy proof, owner approvals, credentials, and release readiness remain separate evidence gates.

## Task 1: Fleet schemas, immutable plan, and durable state

Implement schema-versioned fleet lifecycle models in new focused modules:

- Enums/types for deployment mode, credential policy, resource ownership, target/phase state, and readiness verdict.
- Immutable `FleetPlan` and `TargetPlan` models with deterministic canonical serialization and SHA-256 plan ID.
- `RunManifest` models with per-target/per-phase checkpoints, retries, handoffs, work-request references, and owned/reused/preexisting resources.
- A SQLite state store using Python stdlib only, with schema migrations, transactional updates, run lookup, resume candidates, and `0600` database permissions.
- Redacted JSON and Markdown evidence generation that excludes topology and secret references.
- Import of the existing `EnablementConfig` into a compatible fleet plan.

Add complete unit tests for deterministic hashes, approval mismatch, migrations, permissions, checkpoint transitions, ownership rules, redaction, and old-config import.

## Task 2: Whole-tenancy discovery, filters, selection, and dependency graph

Implement a read-only fleet discovery/planning layer:

- Enumerate subscribed regions, accessible compartments, and all current target families through the existing OCI facade.
- Deduplicate stable target identities and sort deterministically.
- Add selection filters for region, compartment, kind, lifecycle state, tags, name pattern, current service state, explicit exclusions, all-discovered, and CSV/YAML selection files.
- Build and validate the CDB/PDB dependency graph.
- Add answer-file/questionnaire models covering mode, services, optional DBCS/ADB test provisioning, discovery filters, credential policy, log preset, authority mode, concurrency, and retention.
- Validate production restrictions and common-user/PDB unique-password incompatibility without silently weakening the chosen policy.
- Add bounded parallel reads and preserve the existing retry-once semantics for transient discovery errors.

Add tests for multi-region/compartment aggregation, deduplication, filters, 1/100/1000 targets, deterministic order, invalid dependencies, and every questionnaire/answer branch.

## Task 3: Resumable onboarding executor and fleet status

Implement the plan-gated onboarding engine:

- Require the exact plan ID before writes.
- Execute stages in order: prerequisites, optional test databases, Vault/endpoints, DB/host automation or handoff, CDB/PDB DBM, preferred credentials, OPSI, Management Agent/Log Analytics, validation.
- Reuse the existing services rather than duplicate DBM/OPSI/Data Safe/Log Analytics logic.
- Checkpoint every phase and resume only incomplete/retryable/handoff phases.
- Continue independent targets, block PDBs whose CDB failed, and implement bounded per-service concurrency, jittered transient retry, and an authorization circuit breaker.
- Emit configured/collecting/ready/degraded/blocked/handed-off verdicts and machine-readable fleet status.
- Generate resumable DBA/host-admin handoff packets when explicit approved access is unavailable, plus an evidence-import path.

Add fake-OCI tests for success, existing resources, 409, 429/5xx, authorization failures, interruption/resume, PDB blocking, partial success, and collection-not-ready.

## Task 4: Ownership-safe offboarding

Implement cleanup planning and execution from the recorded manifest:

- Build a reverse dependency plan: Log Analytics dissociation, OPSI disable, PDB then CDB DBM disable, run-created credentials/users/secrets, unused run-created endpoints/network, optional tagged test database deletion.
- Add OCI facade methods for OPSI disable/delete, database/PDB DBM disable, Log Analytics association removal, and supported cleanup lookups.
- Enforce ownership and `enabled_by_run` checks for every action; reused/preexisting resources must be preserved.
- Make repeated cleanup idempotent.
- Require exact plan approval and an additional typed confirmation before deleting PoC/demo DBCS or ADB test databases.
- Reject database deletion in production and retain only sanitized seven-day evidence metadata.

Add tests for reverse ordering, mixed ownership, pre-enabled services, repeated cleanup, production restrictions, confirmation mismatch, and partial cleanup/resume.

## Task 5: Guided CLI, authentication, and portable state backends

Add the public CLI while retaining existing commands:

- `onboard` with interactive or `--answers`, `--plan-only`, reviewed summary, exact plan approval, and non-interactive mode.
- `resume --run-id`, `fleet-status --run-id [--json]`, and `offboard --run-id` with plan/apply approval behavior.
- Auth modes for named profile/security token, instance principal, and resource principal, threaded through the OCI command facade without logging credentials.
- Local SQLite state by default and an optional OCI Object Storage backend that uploads/downloads the encrypted-at-rest state artifact through OCI APIs while retaining local `0600` handling.
- Clear exit behavior for success, partial/blocked, and invalid approval/input.

Add CLI parser/handler tests, authentication command-shape tests, plan-only no-write tests, approval tests, JSON output redaction, and Object Storage fake tests.

## Task 6: Terraform compatibility and security hardening

- Keep `oci_database_management_database_dbm_features_management` as the canonical DBM resource.
- Add a sanitized, opt-in compatibility module using `oci_database_cloud_database_management`, with explicit protocol, port, role, Vault secret, and post-enable managed-database lookups derived from the reviewed attachment patterns.
- Add DBM/OPSI enable and disable toggles per target without placeholder host IPs.
- Ensure no plaintext credential is introduced into Terraform state by the production path; keep Data Safe plaintext Terraform usage demo-only and documented.
- Add lifecycle/ownership tags and safe outputs without secrets/topology.
- Extend repository security checks to reject Terraform state, tenant OCIDs, unsafe generated-file modes, and unsanitized attachment defaults.

Add Terraform formatting/validation and static contract tests. Do not copy or publish the attached state or tenant values.

## Task 7: Product ledgers, runbooks, release gates, and full verification

- Update the portfolio and all affected PRDs with numbered tasks, owners, dependencies, rollback, handoff, local evidence, and live evidence gates.
- Document the three operating modes, whole-tenancy scope, filtering, credential policies, lifecycle commands, state protection, resume/offboard, Log Analytics defaults, and 1/100/1000 fleet behavior.
- Add a scratch-tenancy acceptance runbook for DBCS CDB/PDB, ADB, Exadata, external DB, and external Exadata covering provision, enable, collection, interruption/resume, handoff, offboard, and final inventory.
- Keep live acceptance items open unless current redacted live evidence exists; never convert local proof into live proof.
- Run the entire Python suite, eval suite, Terraform format/validate, docs links, public-repo security audit, and a final clean-worktree check.

## Delivery and evidence ledger

| Task | Owner | Depends on | Local implementation evidence | Live scratch-tenancy / release gate | Rollback and handoff |
| --- | --- | --- | --- | --- | --- |
| 1 | Fleet lifecycle engineer | Existing config compatibility | `tests/test_fleet_lifecycle.py`; `tests/test_fleet_cli_boundary.py`; `tests/test_fleet_executor.py` | **OWNER INPUT REQUIRED**: redacted state/evidence receipt from a scratch tenancy | SQLite manifest is checkpointed; stop writes and retain the `0600` state file for resume. |
| 2 | Discovery engineer | Task 1 | `tests/test_fleet_discovery.py`, `tests/test_fleet_selection.py`, `tests/test_fleet_answers.py` | **OWNER INPUT REQUIRED**: scoped, redacted discovery receipt for all subscribed regions | Discovery is read-only; record inaccessible scopes rather than substituting an empty inventory. |
| 3 | Lifecycle engineer | Tasks 1-2 | `tests/test_fleet_executor.py`, `tests/test_fleet_cli_boundary.py` | **OWNER INPUT REQUIRED**: redacted service and collection timestamps per requested pillar | Resume from the exact approved plan ID, or issue a signed DBA/host-admin handoff. |
| 4 | Lifecycle engineer | Tasks 1, 3 | `tests/test_fleet_offboarding.py`, `tests/test_oci_offboarding_facade.py` | **OWNER INPUT REQUIRED**: redacted final run-owned inventory and cleanup receipt | Re-run the exact cleanup plan; it changes only owned, run-enabled resources. |
| 5 | CLI engineer | Tasks 1-4 | `tests/test_fleet_cli_boundary.py`, `tests/test_fleet_answers.py` | **OWNER INPUT REQUIRED**: redacted CLI transcript using approved auth | Stop at plan-only or an approval mismatch; no inferred approval. |
| 6 | Terraform/security engineer | Existing Terraform modules | Terraform contract tests and `scripts/security-gate.py` | **OWNER INPUT REQUIRED**: redacted apply/disable evidence from an approved disposable environment | Use toggle-driven disable; never introduce state/secret/topology artifacts. |
| 7 | Product/release owner | Tasks 1-6 | Product ledger, lifecycle runbook, fake-fleet acceptance tests, and [public local verification receipt](../product/fleet-lifecycle-local-verification.md) | **IN PROGRESS / OWNER INPUT REQUIRED**: no live OCI run was authorized or supplied | See the operator handoff and acceptance matrix in `docs/fleet-lifecycle-runbook.md`. |

`docs/fleet-lifecycle-runbook.md` is the operator contract and
`docs/product/fleet-lifecycle-local-verification.md` records committed local proof.
Neither a green test nor an OCI control-plane registration is collection proof.
