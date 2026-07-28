# Task 4 report: ownership-safe offboarding

## Delivered

- Added immutable `CleanupPlan`/`CleanupAction` models with canonical SHA-256
  plan IDs, exact approval, and the separate literal confirmation required for
  optional test-database deletion.
- Added a manifest-only reverse planner: Log Analytics dissociation, OPSI
  disable, PDB DBM disable before CDB DBM disable, then run-owned credentials,
  users/secrets, endpoints/network, and optional tagged test databases.
- Cleanup requires both `ResourceOwnership.CREATED`/`OWNED` and
  `enabled_by_run`; reused, preexisting, and merely observed resources are not
  planned for mutation.
- Added a resumable cleanup executor. Completed action digests are not replayed;
  failed independent actions continue and replay on the next exact-plan run.
  OCI 404/409 responses are recorded as complete idempotent outcomes.
- Added a state schema migration for cleanup checkpoints. It stores opaque
  action digests plus aggregate sanitized evidence metadata only; metadata has
  a fixed seven-day retention end and is not extended by a repeated no-op.
- Production cleanup refuses test-database deletion. PoC/demo deletion requires
  `DELETE TEST DATABASES FOR RUN <run-id>` in addition to exact plan approval.
- Added focused OCI facade hooks for OPSI/DBM disable/delete, Log Analytics
  dissociation, safe secret scheduling, endpoint/network deletion, and DBCS/ADB
  test-database deletion. Lifecycle planning remains the ownership gate.

## Tests

- TDD red check: the initial focused test failed with the expected
  `ModuleNotFoundError` before the offboarding module existed.
- Focused:
  `/Users/abirzu/oci-cli/bin/python3.11 -m pytest -q --no-cov tests/test_fleet_offboarding.py tests/test_oci_offboarding_facade.py tests/test_oci_dbmgmt.py tests/test_fleet_lifecycle.py tests/test_fleet_executor.py`
  - 53 passed
- Full:
  `/Users/abirzu/oci-cli/bin/python3.11 -m pytest -q`
  - 517 passed, 90.54% coverage

## Scope and constraints

- No public CLI commands, Terraform, or product documentation were added.
- The low-level OCI facade does not decide ownership. It is intentionally
  invoked only through a reviewed cleanup plan derived from the durable run
  manifest, so current-tenancy names/tags cannot broaden deletion authority.
- Database-user deletion remains an explicit operations-adapter responsibility:
  the existing OCI facade has no approved SQL connection context for dropping a
  database principal. The planner emits that action only for a run-owned,
  enabled record and the executor preserves it for safe retry/handoff.

## Fix round 1/5: OCI contract and retention hardening

- Validated the installed OCI CLI help. DBM cleanup now uses
  `disable-database-management-feature --database-id --feature` for CDBs and
  `disable-pluggable-database-management-feature --pluggable-database-id
  --feature` for PDBs. Features are explicit and restricted to the installed
  CLI allowlist; the former managed-database `disable` verb/ID option is no
  longer emitted.
- Vault cleanup now uses `schedule-secret-deletion`. Its time is omitted by
  default so OCI selects its earliest supported deletion time; seven-day
  evidence retention is not used as a live-secret deletion delay.
- Added immutable structured action arguments sourced from manifest resource
  attributes and target settings. The concrete `OciCleanupOperations` adapter
  maps Log Analytics, OPSI, DBM, named credentials, secrets, supported unused
  endpoints/network resources, and DBCS/ADB test databases to OCI facade
  calls. Missing data and DB-user deletion become handed-off, never complete.
- Replaced free-form exception substring completion with typed
  `OciNotFound`/`OciAlreadyDone` classification. Only an unambiguous 404
  resource-not-found and explicit already-disabled/deleted responses are
  idempotent; authorization-ambiguous NotAuthorizedOrNotFound and generic
  409/resource-in-use remain failed and resumable.
- Added terminal-only cleanup evidence expiry. After seven days, sanitized
  metadata is removed while opaque completed action digests remain for
  idempotency; failed/handed-off operational state is retained.

### Fix-round verification

- Focused:
  `/Users/abirzu/oci-cli/bin/python3.11 -m pytest -q --no-cov tests/test_fleet_offboarding.py tests/test_oci_offboarding_facade.py tests/test_runner.py tests/test_fleet_lifecycle.py tests/test_oci_dbmgmt.py`
  - 58 passed
- Full:
  `/Users/abirzu/oci-cli/bin/python3.11 -m pytest -q`
  - 528 passed, 90.48% coverage

## Fix round 2/5: resumability and authority hardening

- Handed-off cleanup actions are no longer terminal skips. An exact-plan rerun
  reattempts the same action digest with a newly approved adapter; only
  completed work is skipped. Repaired structured inputs change the immutable
  action digest and therefore require a new cleanup plan and approval.
- The planner now distinguishes executable private endpoint/network cleanup
  types from unsupported families. DBM, OPSI, and Data Safe endpoints plus
  subnets, VCNs, route tables, and security lists have concrete OCI routes.
  Unknown endpoint/network/gateway families become explicit, reason-carrying,
  resumable `handoff-cleanup` actions at planning time.
- Validated Data Safe, route-table, and security-list delete command shapes
  against the installed OCI CLI. Table-driven planner/adapter tests prove each
  executable emitted endpoint/network action dispatches, while handoffs never
  claim completion.
- `NotAuthorizedOrNotFound` is now classified as authorization-ambiguous and
  fails closed. Only an unambiguous 404/not-found and explicit
  already-disabled/deleted result is an idempotent cleanup completion.

### Fix-round verification

- Focused:
  `/Users/abirzu/oci-cli/bin/python3.11 -m pytest -q --no-cov tests/test_runner.py tests/test_fleet_offboarding.py tests/test_oci_offboarding_facade.py tests/test_fleet_lifecycle.py tests/test_oci_dbmgmt.py`
  - 70 passed
- Full:
  `/Users/abirzu/oci-cli/bin/python3.11 -m pytest -q`
  - 540 passed, 90.64% coverage

## Fix round 3/5: exact resource kinds and signed manual completion

- Endpoint/network planning and OCI dispatch now use closed normalized exact
  kind maps. Only explicit DBM/OPSI/Data Safe private endpoints, subnets,
  VCNs, route tables, and security lists are executable. Near-match,
  composite, gateway, and free-form kinds become explicit resumable handoffs.
- Added signed, redacted cleanup-handoff instructions and completion evidence.
  Completion binds the immutable cleanup plan, original run, action digest,
  opaque action handle, action kind, issued handoff reference/digest, operator
  attestation/result, timestamp, and nonce. It rejects instruction-only,
  tampered, wrong plan/run/action/kind/digest evidence and preserves no OCID.
- An approved adapter can retry a handed-off action under the original plan;
  changed structured arguments cannot. Signed manual completion closes the
  original immutable handoff action and is idempotent on replay.

### Fix-round verification

- Focused:
  `/Users/abirzu/oci-cli/bin/python3.11 -m pytest -q --no-cov tests/test_fleet_offboarding.py tests/test_fleet_executor.py tests/test_fleet_lifecycle.py tests/test_oci_offboarding_facade.py tests/test_runner.py`
  - 101 passed
- Full:
  `/Users/abirzu/oci-cli/bin/python3.11 -m pytest -q`
  - 561 passed, 89.26% coverage
