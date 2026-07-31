# CAP Canary Validation

The 30 July 2026 Base Database canary is a **partial live acceptance**, not a
production-readiness claim.

## What was proved live

- the approved CAP tenancy and database compartment were preflighted;
- DB systems, CDB/PDBs, backups, Data Guard, DBM, OPSI, Data Safe, Log
  Analytics, and Management Agent dependencies were inventoried together;
- existing private endpoints were selected for reuse;
- DBM/OPSI enablement was submitted to one database with no active
  observability services;
- the workflow identified terminal DBM `FAILED_ENABLING`, a failed OPSI
  insight, and an `ORA-01017` credential mismatch;
- no private endpoint, Management Agent, Log Analytics association, database,
  or backup was created or deleted.

## Product behavior added

- DBM `FAILED_*` fails the command.
- A bounded DBM `ENABLING` timeout fails the command.
- DBM is rechecked after connection reconciliation.
- A failed or needs-attention OPSI insight is repaired in place with the
  approved private endpoint and Vault reference.
- An active insight is reused.
- Log Analytics stays blocked until a Management Agent-backed entity exists.

## Current credential-repair boundary

The database-side user-creation, grants, and login-validation packet completed
through a short-lived Bastion session. The subsequent DBM reconciliation still
reached terminal `FAILED_ENABLING`, so the credential is no longer the proven
blocker. The workflow stopped before OPSI repair; collection remains unproven
until the OCI DBM prerequisite/work-request diagnostic is resolved and DBM
reports `ENABLED`.

OCI-side network, endpoint, Vault, and IAM checks pass, while no failed DBM
work-request record is available. The next gate is a read-only host OS firewall
check for listener reachability from the DBM/OPSI private-endpoint source.

## Remaining gate

The protected monitoring-user/Vault alignment must complete before DBM and OPSI
can be retried. The live gate then requires DBM `ENABLED`, OPSI `ACTIVE` with a
successful connection, a bound Management Agent, source associations, and a
Log Analytics query returning current rows. A second run must be idempotent.

No database is classified as disposable from its name. Deletion requires exact
ownership, dependency and recovery review plus the action-bound destructive
approval identifier. The two databases that already have working observability
dependencies are preserved.

The complete repository receipt is
[CAP Base Database canary validation](https://github.com/adibirzu/oci-dbman-opsi/blob/main/docs/cap-canary-validation.md).
