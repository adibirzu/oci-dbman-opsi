# Task 3 report: resumable onboarding executor and fleet status

## Delivered

- Added a plan-gated `FleetOnboardingExecutor` with durable phase checkpoints:
  prerequisites, optional test databases, Vault/endpoints, DB/host automation,
  DBM, preferred credentials, OPSI, Management Agent/Log Analytics, and
  validation.
- Added bounded target and per-service concurrency, jittered retries for OCI
  429/5xx responses, idempotent existing-resource 409 handling, and a shared
  authorization circuit breaker.
- Enforced dependency waves so a failed or handed-off CDB blocks its PDB while
  unrelated targets continue.
- Added a machine-readable fleet status projection. OPSI/DBM registration is
  `collecting`; only explicit validation collection proof produces `ready`.
- Added `0600` signed, redacted DBA/host handoff packets and a verified evidence
  import path. Evidence import reopens the target for remaining phases.
- Made handoff checkpoints resumable in the existing fleet state model.

## Tests

- TDD red check: the new focused suite initially failed with the expected
  `ModuleNotFoundError` before implementation.
- Focused: `/Users/abirzu/oci-cli/bin/python3.11 -m pytest -q --no-cov tests/test_fleet_executor.py`
  - 8 passed
- Full: `/Users/abirzu/oci-cli/bin/python3.11 -m pytest -q`
  - 493 passed, 90.41% coverage

## Scope and constraints

- The coordinator accepts phase handlers so existing DBM, OPSI, credential,
  Log Analytics, prerequisite, and validation service implementations remain
  the sole owners of OCI command construction and idempotency behavior.
- Durable state stores only references/messages; password generation and secret
  material are not accepted by the executor or packet formats.
- No CLI, offboarding, Terraform, product-documentation, or plan-document
  changes were made.

## Fix round 1: reviewer hardening

- Replaced instruction-packet import with a signed completion-evidence envelope.
  It requires a non-empty redacted operator attestation, an allowlisted result,
  timestamp, nonce, and an exact binding to the issued packet digest/reference,
  run, plan, opaque target handle, and phase. Issued instructions alone cannot
  be imported as completion proof.
- Handoff packets and filenames no longer serialize target identifiers or OCI
  OCIDs. They use an opaque SHA-256-derived target handle; OCI-backed targets
  can still complete a verified evidence import.
- Restricted 409 reuse success to explicit already-exists/already-enabled or
  duplicate semantics. Generic conflict is failed and update-in-progress is
  retryable under the normal retry policy.
- Resumes now process only resumable targets and deterministically no-op fully
  terminal runs, preserving blocked and failed durable state. Block events are
  checkpointed on the current phase rather than always on prerequisites.

### Fix-round verification

- Focused: `/Users/abirzu/oci-cli/bin/python3.11 -m pytest -q --no-cov tests/test_fleet_executor.py`
  - 24 passed
- Full: `/Users/abirzu/oci-cli/bin/python3.11 -m pytest -q`
  - 509 passed, 90.53% coverage
