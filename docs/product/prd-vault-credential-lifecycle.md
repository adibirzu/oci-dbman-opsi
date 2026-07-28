# PRD: Vault Credential Lifecycle

Version: 1.0 · Owner: Terra · Tasks: T-02, T-03, T-04 · Status: In progress

## Outcome

Use separate OCI Vault secrets for `DBM_MON`, `DATASAFE_AUDIT`, `MCP_READONLY`, and `DBINC_LAB`. Normal status and Terraform outputs show a reference/version only; secret contents cross an explicitly authorized operator boundary.

## Requirements

- Generate a compliant unique value per role and store it in OCI Vault.
- Bootstrap role-specific least-privilege users idempotently through approved DBCS SYSDBA or ADB administration paths.
- Reset exactly one selected role: alter account, create Vault version, refresh DBM/OPSI/Data Safe bindings, validate or report remediation; never silently accept partial completion.
- IAM limits operator and service access to required secret operations.

## Acceptance

- [ ] Four references are individually retrievable by authorized operators.
- [ ] Bootstrap proves open accounts and expected grants without printing values.
- [ ] A one-role reset preserves service health and all output is redacted.

Operational reference: [security guidance](../security.md).

## Fleet lifecycle implementation tasks

| Task | Owner | Depends on | Rollback/handoff | Local evidence | Live gate |
| --- | --- | --- | --- | --- | --- |
| F-01 Record credential policy in immutable plan | Fleet lifecycle engineer | Existing Vault contract | Reject mismatch; preserve policy without coercion | Fleet answers/lifecycle tests | **OWNER INPUT REQUIRED** redacted Vault-reference receipt |
| F-03 Bind preferred credentials or hand off | Lifecycle engineer and DBA | F-01, F-02 | Signed completion evidence, never a synthetic success | Executor/CLI tests | **OWNER INPUT REQUIRED** per-account receipt |
| F-04 Remove only run-created credential resources | Lifecycle engineer | F-03 | Exact cleanup rerun; reused/preexisting resources remain | Offboarding tests | **OWNER INPUT REQUIRED** cleanup receipt |

Production defaults to `shared-user-unique-secret`, using one reviewed monitoring
username and a unique Vault-backed secret for every independent database account.
`dedicated-user-unique-secret` is also supported. `shared-user-shared-secret` is
restricted to PoC/demo and rejected in production. A common CDB user cannot also
request unique PDB passwords; that case must use independent local PDB users.
These are local contract tests, not evidence that a tenant credential was changed.
