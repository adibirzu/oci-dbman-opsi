# Runbook: dbman-opsi End-to-End Enablement & Verification (cap)

Reproducible record of running the full `dbman-opsi` flow against a live Base
Database Service deployment in the **cap** staging tenancy (`eu-frankfurt-1`),
and the defects found and fixed along the way.

All tenant-specific values (OCIDs, IPs, passwords, service GUIDs) are redacted —
resolve them from the gitignored local config and `~/.claude/private/`.

Targets: one CDB (`DBMOPSI`) + one PDB (`PDB1`), both `management_type: ADVANCED`,
DBSNMP monitoring user, OPSI PE-comanaged with `CREDENTIALS_BY_VAULT`.

---

## Phase 0 — Confirm live infra (read-only)

```bash
oci iam tenancy get --tenancy-id <cap-tenancy> --query 'data.name'      # -> <cap-tenancy-name>
oci db database get --database-id <cdb> --query 'data."lifecycle-state"' # -> AVAILABLE
oci db database get --database-id <cdb> --query 'data."database-management-config"'  # ENABLED / ADVANCED
oci db pluggable-database get --pluggable-database-id <pdb> --query 'data."pluggable-database-management-config"'
oci database-management private-endpoint get --private-endpoint-id <dbm-pe> --query 'data."lifecycle-state"'   # ACTIVE
oci opsi opsi-private-endpoint get --opsi-private-endpoint-id <opsi-pe> --query 'data."lifecycle-state"'        # ACTIVE
oci vault secret get --secret-id <secret> --query 'data."lifecycle-state"'  # ACTIVE
```

Expected: tenancy confirmed; CDB & PDB `AVAILABLE`; both PEs `ACTIVE`;
Vault secret `ACTIVE`.

## Phase 1 — doctor + preflight (read-only)

```bash
dbman-opsi doctor                                          # python/oci/terraform OK
dbman-opsi preflight --config dbman-opsi.cap.local.yaml    # verdict: READY
```

Known non-blocking WARNs in cap: `service-gateway list` and `route-table get`
return `NotAuthorizedOrNotFound` (the cap user lacks those VCN reads); the
security-list 1521 heuristic warns when an NSG (not the security list) covers
the port. `target.monitoring_user` is `[MANUAL]` — proven DB-side in Phase 2.

## Phase 2 — DBSNMP connectivity proof (bastion)

```bash
# fresh port-forward session to the DB node :22, then tunnel local 8022 -> :22
oci bastion session create-port-forwarding --bastion-id <bastion> \
  --ssh-public-key-file <pub> --target-private-ip <db-ip> --target-port 22 \
  --session-ttl 10800 --wait-for-state SUCCEEDED
ssh -i <key> -N -L 8022:<db-ip>:22 -p 22 <session-ocid>@host.bastion.<region>.oci.oraclecloud.com ...
ssh -i <key> -p 8022 opc@localhost   # then: sudo su - oracle
```

On the DB host as `oracle`:

```bash
lsnrctl status     # <-- lists the REAL listener services (critical, see Defect 1)
sqlplus / as sysdba <<'SQL'
  select username, account_status from dba_users where username='DBSNMP';     -- expect OPEN
  alter session set container=PDB1;
  select username, account_status from dba_users where username='DBSNMP';
SQL
# prove OPSI-style TCP login per real service:
sqlplus -L DBSNMP/<pw>@<db-ip>:1521/<real-service>
```

## Phase 3 — generators (idempotent)

```bash
dbman-opsi generate-db-scripts     --config dbman-opsi.cap.local.yaml --output generated/db-scripts
dbman-opsi generate-opsi-payloads  --config dbman-opsi.cap.local.yaml --output generated/cap-opsi-payloads
dbman-opsi generate-agent-scripts  --config dbman-opsi.cap.local.yaml
```

## Phase 4 — enable + validate (the no-errors gate)

```bash
dbman-opsi enable   --config dbman-opsi.cap.local.yaml --apply
dbman-opsi validate --config dbman-opsi.cap.local.yaml
# final state checks:
oci opsi work-requests list -c <cmpt> --sort-order DESC      # CREATE_DATABASE_INSIGHT SUCCEEDED 100%
oci database-management managed-database list -c <cmpt>      # DBMOPSI, PDB1 -> ADVANCED
```

End state: DBM `ADVANCED` on both; OPSI insights `ACTIVE / ENABLED`,
`database-connection-status-details: SUCCESS`; 0 FAILED.

---

## Defects found & fixed

### Defect 1 — OPSI insight create fails at 80% (`DbcsEntityChangeWorkflowFailed`)

Two independent root causes, both DB-connection failures the OPSI create test
surfaces (DBM hid them because it connects by OCID, not service+credential):

1. **Wrong `service_name`** — config used the bare names `DBMOPSI` / `PDB1`, but
   the listener only registers domain-qualified services
   (`<db_unique_name>.<domain>` for the CDB, `<pdb_name>.<domain>` for the PDB) →
   **ORA-12514**. Fixed by setting the real services in
   `dbman-opsi.cap.local.yaml` and regenerating OPSI payloads.
2. **DBSNMP credential drift** — the Vault secret password didn't match the DB
   (**ORA-01017**) and itself violated the DB verify function (**ORA-20000:
   >=2 special characters**), so it had never been applied. Fixed by setting a
   compliant DBSNMP password (`ALTER USER DBSNMP IDENTIFIED BY "<pw>"
   CONTAINER=ALL`) and syncing it into the Vault secret
   (`oci vault secret update-base64`).

To re-create after a failed attempt, the insight must be **disabled before
delete** (`oci opsi database-insights disable` then `... delete --force`).

See `KB.md` for the full diagnosis path (work-request errors via `oci
raw-request`, `lsnrctl status`, per-service `sqlplus` probes).

### Defect 2 — `enable` not idempotent (code fix)

`enable --apply` aborted on the already-enabled DBM **409 IncorrectState** before
reaching OPSI. Added `OciCli.run_tolerating()` so an already-enabled DBM is a
no-op and the flow continues. (`src/dbman_opsi/oci_cli.py`,
`src/dbman_opsi/enablement.py`; tests in `tests/test_enablement.py`.)

### Defect 3 — `validate` blind to OPSI state (code fix)

`validate` printed a generic "requires Database Insight validation" for every
target, so FAILED insights looked identical to healthy ones. It now queries the
insight lifecycle (`OciCli.list_opsi_database_insights`, all lifecycle states)
and reports `ACTIVE (ENABLED)` / `FAILED (ENABLED)` / `NOT_FOUND` / `UNKNOWN`,
with a retry on transient 404. (`src/dbman_opsi/validation.py`; tests in
`tests/test_validation.py`.)

## Known cap quirk (root-caused in Defect 6)

The OPSI `database-insights list` control-plane endpoint is **non-deterministic**:
it flaps between the full set, a partial set, and an exit-0 empty list (and
sometimes `NotAuthorizedOrNotFound`) for the same compartment, call to call — the
multi-`--lifecycle-state` + `--all` query shape makes it worse. Authoritative reads
that don't depend on it: a single-resource `database-insights get` **by insight
OCID** (reliable 10/10), the SUCCEEDED `CREATE_DATABASE_INSIGHT` work request, and
`database-connection-status-details: SUCCESS` on the insight. See Defect 6.

## Defect 4 — DBM monitoring stays Stopped after re-enable (stale service name)

DBM was first enabled with the wrong service name, and the idempotent re-run only
tolerated the already-enabled 409 and **skipped** DBM, so the corrected service
name never took effect — monitoring stayed Stopped (ORA-12514). Reconciled in place
with `modify-(pluggable-)database-management` (service name + current secret);
`database-status` then flipped to **UP**. `enable` now reconciles automatically on
an already-enabled DBM (`cloud_modify_command` in `enablement.py`), so repeat
runs / ORM are self-healing for connection drift.

## Defect 5 — DBSNMP lock loop after password rotation

See `KB.md`. Rotating DBSNMP broke the DBCS local agent (old password) which
re-locked the account (ORA-28000), taking DBM + OPSI down. Fixed by moving DBSNMP
to a non-locking common profile `C##DBSNMP_MON`
(FAILED_LOGIN_ATTEMPTS/PASSWORD_LIFE_TIME UNLIMITED).

## Defect 6 — `validate` false `NOT_FOUND` from the flaky OPSI list (code fix)

Re-running the full e2e (2026-06-05) surfaced that `validate` (Defect 3's
list-based path) reported `Ops Insights NOT_FOUND` for the CDB and PDB while both
insights were `ACTIVE / SUCCESS`. Root cause: the aggregated `database-insights
list` flaps (0/2/7 items call-to-call), worsened by the 5-`--lifecycle-state` +
`--all` shape; `validate` matched the target against one flaky response. Fix:

- `OciCli.list_opsi_database_insights` now queries **one lifecycle state per call
  and unions** by insight OCID (each call fault-tolerant) instead of the broken
  single multi-state call.
- New `OciCli.get_opsi_database_insight(insight_id)`; `validate` prefers this
  **reliable GET by insight OCID** (`target.opsi_database_insight_id`, now persisted
  in config), falling back to the list only to discover an unknown OCID.
- List-fallback verdict model never emits a false `NOT_FOUND`: positive hit is
  authoritative (then GET); `NOT_FOUND` only on a stable non-empty list reproducibly
  missing the target; empty/varying → `UNKNOWN`.
- (`src/dbman_opsi/oci_cli.py`, `src/dbman_opsi/validation.py`; tests in
  `tests/test_oci_cli.py`, `tests/test_validation.py`.) After the fix `validate`
  reports `ACTIVE (ENABLED)` for both targets deterministically. Full KB entry:
  `KB.md` → "2026-06-05 OPSI list flap".

## Defect 7 — Performance Hub privileges (DB-side grant)

The OCI Console Performance Hub showed "Performance Hub requires granting of
appropriate user privileges." The DBM monitoring user `DBSNMP` had the basic +
advanced monitoring grants but not the Performance Hub / AWR set. Applied as SYSDBA
(via bastion port-forward → `ssh opc` with the DB-system key → `sudo su - oracle` →
`sqlplus / as sysdba`), using `CONTAINER=ALL` so the CDB common user covers CDB+PDB:

```sql
grant create procedure to DBSNMP container=all;
grant select any dictionary to DBSNMP container=all;
grant select_catalog_role to DBSNMP container=all;
grant alter system to DBSNMP container=all;
grant advisor to DBSNMP container=all;
grant execute on sys.dbms_workload_repository to DBSNMP container=all;
```

The toolkit now generates these in `03-grant-advanced-diagnostics.sql` and checks
them in `04-validate-monitoring-user.sql` (`src/dbman_opsi/db_scripts.py`). Verified
present in `CDB$ROOT` and `PDB1`; `dbms_workload_repository.create_snapshot`
succeeded (AWR — the Performance Hub data source — confirmed live, 46 snapshots).

## Final verified state (API)

- DBM: CDB `DBMOPSI` **UP**, PDB `PDB1` **UP** (ADVANCED).
- OPSI: DBMOPSI + PDB1 **ACTIVE**, `database-connection-status-details: SUCCESS`.
- `validate` reports `Ops Insights ACTIVE (ENABLED)` for both, deterministically
  (via GET-by-OCID), across repeated runs.
- Performance Hub: DBSNMP holds the AWR/advisor privileges in CDB+PDB; AWR snapshot
  creation succeeds. Reopen Performance Hub in the Console.

## Phase 5 — OCI Console screenshots

Captured via **CDP attach** (extension-free; see `~/.claude/CLAUDE.md` "Browser
Automation"). User launches Chrome with `--remote-debugging-port=9222
--user-data-dir=~/.oci-cdp-profile`, logs in normally, then
`~/oci-cli/bin/python /tmp/oci_cdp_capture.py` connects over CDP and screenshots.

Working console routes (eu-frankfurt-1): DB systems `/dbaas/dbsystems`,
Ops Insights overview `/opsi/database-insights`. DBM and per-resource detail pages
are SPA routes that redirect when deep-linked — navigate via the console menu and
screenshot the DBMOPSI/PDB1 detail pages (now showing **Monitoring: UP**).

Raw captures go to `docs/screenshots/raw/` (gitignored). Redact OCIDs / tenancy
name / db_unique_name / DB IDs (crop or blur) before committing any version.
