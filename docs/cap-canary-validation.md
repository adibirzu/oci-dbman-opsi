# CAP Base Database Canary Validation

This page records the sanitized live acceptance evidence for the Base Database
canary exercised on 30 July 2026. It is an operational receipt, not a claim that
every target family or every OCI tenancy has passed acceptance.

## Scope and safety boundary

- OCI context: the approved CAP profile, home region, and database test
  compartment were confirmed by the tenancy-safety preflight.
- Target: one existing Base Database CDB with one PDB and no active DBM, OPSI,
  Data Safe, or Log Analytics collection path at the start of the test.
- Reused infrastructure: active DBM and OPSI private endpoints in the target
  subnet and an existing OCI Bastion.
- Credentials: only an OCI Vault secret reference is stored in configuration.
  The secret value, database password, OCIDs, IP addresses, SSH key path, and
  Log Analytics namespace are excluded from this record.
- Destructive actions: no database, backup, endpoint, agent, or service
  association was deleted.

## Dependency inventory

The read-only inventory joined DB systems, DB homes, CDBs, PDBs, backups, Data
Guard associations, DBM state, OPSI insights, Data Safe targets, Log Analytics
entities, and Management Agents.

| Candidate | Backup / Data Guard posture | Observability dependencies | Decision |
| --- | --- | --- | --- |
| Canary system | No backup; no Data Guard | DBM `FAILED_ENABLING`; OPSI insight `FAILED`; no Data Safe target; no Log Analytics entity | Retain and repair as the dedicated canary |
| Existing observability system A | No backup; no Data Guard | DBM enabled; OPSI active; Data Safe registered | Preserve; not an unused target |
| Existing observability system B | No backup; no Data Guard | DBM enabled; OPSI active; Data Safe registered; Log Analytics entity present | Preserve; not an unused target |

Display names and identifiers remain in the private operator inventory. A name
containing `test`, `demo`, or the project name is not proof of ownership or
safe deletion. Because none of the systems has a usable backup, recreation
cannot be described as rollback.

## Live execution result

The initial Terraform plan reused the existing private endpoints and produced no
endpoint create or delete. CAP authorization did not permit the provider's
Database Management feature operation, so the unchanged Terraform plan could
not complete the service enablement.

The repository's direct per-target workflow then submitted DBM/OPSI enablement.
The OCI database remained available, but service enablement failed:

- DBM reached terminal state `FAILED_ENABLING`;
- OPSI retained an existing failed insight;
- the connection diagnostic was `ORA-01017`, proving that the Vault secret and
  the database monitoring-user password were not aligned;
- no Management Agent or Log Analytics source association was created;
- no OCI private endpoint was created.

This is a partial live acceptance result. It proves context targeting,
dependency discovery, reuse behavior, submission, and failure diagnosis. It
does not prove DBM/OPSI collection or Log Analytics ingestion.

## Automation changes driven by the canary

The workflow now:

1. fails the command when DBM reaches a terminal `FAILED_*` state;
2. fails when DBM remains `ENABLING` beyond the bounded poll window;
3. verifies DBM status after an already-enabled connection is reconciled;
4. discovers an existing failed OPSI insight and changes its private-endpoint
   connection and Vault-backed credential details instead of attempting a
   duplicate create;
5. continues to skip an existing active OPSI insight idempotently;
6. keeps the Log Analytics phase blocked until a Management Agent-backed entity
   is available.
7. waits for the locally bound Bastion SSH tunnel to accept connections before
   transferring a database-side script, resolves an actual Bastion session ID
   rather than an OCI work-request ID, and reports bounded SSH diagnostics.

These behaviors apply to the expert per-target workflow and are reused by fleet
orchestration phases. They replace the manual “check the console and retry”
steps with explicit terminal states and actionable exit failures.

## CAP credential-repair follow-up

The database-side monitoring-user creation, idempotent grants, and login
validation scripts completed through a short-lived Bastion session. An earlier
session-key diagnostic exposed and corrected transport handling for tunnel
readiness and work-request versus session IDs; all temporary sessions were
deleted. No Bastion allowlist, network rule, endpoint, database, or Log
Analytics resource was changed by that diagnostic.

The subsequent DBM reconciliation still reached terminal
`FAILED_ENABLING`. This proves the remaining blocker is in OCI DBM enablement,
not the database monitoring-user credential path. OPSI repair was not attempted
after this fail-closed DBM result, and DBM/OPSI collection remains unproven.

## Remaining live sequence

The next approved canary run is:

1. inspect the OCI DBM enablement prerequisite/work-request diagnostics for the
   terminal failure and correct only the identified dependency;
2. rerun DBM enablement and require `ENABLED`;
3. repair the existing OPSI insight and require `ACTIVE` plus a successful
   connection status;
4. install or bind a Management Agent on the database host;
5. create/reuse Management Agent-backed database, host, and listener entities;
6. upsert the selected database and host log-source associations;
7. query Log Analytics over a bounded window and require returned rows before
   declaring collection ready;
8. rerun the same workflow to prove idempotency;
9. generate an ownership-safe offboarding plan without deleting the database.

Credential changes and database deletion are separate protected operations.
Non-interactive execution requires the exact approval identifier emitted by
the corresponding dry-run. A general verbal approval is recorded as intent but
does not replace that action-bound identifier.

## Database recreation policy

Recreate a database only when all of the following are recorded:

- exact DB system and database identity;
- an ownership marker proving it is disposable or a signed owner decision;
- database, PDB, Data Guard, backup, DBM, OPSI, Data Safe, Log Analytics,
  Bastion, Vault, network, and Terraform-state dependencies;
- an explicit statement that no recoverable backup exists, or the tested
  restore point and recovery objective;
- a reviewed termination plan and its exact destructive approval identifier;
- a reviewed create plan with limits, capacity, private networking, Vault,
  backup policy, and post-create validation;
- final absence evidence for the old run-owned resources and collection proof
  for the replacement.

The fleet lifecycle never deletes a production database. PoC/demo cleanup may
delete only a database recorded as created and owned by that exact run.
