# dbman-opsi Knowledge Base

This KB captures implementation and live-tenancy troubleshooting notes for OCI Database Management (DBM) and Operations Insights (OPSI). Keep tenant-specific values out of this file: no OCIDs, IP addresses, usernames beyond generic service users, secrets, Bastion session IDs, or private topology.

## 2026-06-26 Live Management Agent + Log Analytics + Data Safe Drift Fixes

### Management Agent image `object-url` was not directly downloadable

- Symptom: the generated Log Analytics host install packet resolved a Management
  Agent `object-url`, but both workstation and DB VM downloads returned `404`.
- Root cause: the OCI CLI `management-agent agent-image list` response included
  object metadata that was valid for authenticated `oci os object get`, but the
  raw `object-url` was not a reliable anonymous download path in this tenancy /
  CLI combination.
- Fix:
  - update `src/dbman_opsi/agent_scripts.py` so the generated resolver script
    emits object namespace/bucket/name/checksum metadata and can download the
    RPM locally with `oci os object get`;
  - keep `AGENT_RPM_URL` only as a hint, not the preferred path.
- Validation: live authenticated object download succeeded; the RPM checksum
  matched the image metadata checksum.

### OCI Management Agent install needed Java 8 on the DB VM

- Symptom: the Management Agent RPM preinstall script failed on the DB VM with a
  Java version gate while `/usr/bin/java` still pointed at Java 11.
- Root cause: this agent build required Java 8u281+ for the installer path used
  on the target DBCS host.
- Fix:
  - install Java 8 on the DB VM for the live demo;
  - update the generated Linux install script to detect/install Java 8, set
    `JAVA_HOME`, and use that runtime during the RPM + setup flow.
- Validation: live Management Agent install succeeded on the DB VM and the
  agent registered with the Log Analytics plugin.

### Log Analytics association flow was functionally correct but too slow per-source

- Symptom: the live `configure --with-log-analytics` path appeared stuck during
  source association.
- Root cause:
  - one `log-analytics assoc upsert-assocs` call was being issued per source;
  - each call returned a Log Analytics config work request, so the total latency
    stacked badly for DBCS targets with many sources.
- Fix:
  - batch source associations into a single `upsert-assocs` call per target via
    `src/dbman_opsi/log_analytics.py` and `src/dbman_opsi/_oci_loganalytics.py`;
  - keep the per-source JSON payload files on disk for operator visibility.
- Validation: the narrowed live `LogAnalyticsService.enable_all(...)` run
  completed successfully for the DBCS target and applied the full source set in
  one batch.

### Existing DBCS Data Safe registration was live; local config was stale

- Symptom: the local ignored config still showed `datasafe` absent and had no
  Data Safe target or private endpoint OCIDs for the current DBCS target.
- Root cause: live tenancy state had moved ahead of the local ignored config.
- Fix:
  - verify the target database's parent DB system and match it against existing
    Data Safe registrations via `associated-resource-ids`;
  - update the local ignored config to include the DB system, Data Safe private
    endpoint, Data Safe target, and `datasafe` service membership.
- Validation: the live DB incident evidence bundle reported Data Safe source
  status `ok` instead of `unavailable`.

### Repo needed a first-class demo operator path for Data Safe audit export

- Symptom: the repo could register Data Safe targets, but it did not document or
  automate the bridge from Data Safe audit events into OCI Logging / Log Analytics.
- Fix:
  - add `scripts/demo-datasafe-log-export.sh`;
  - add `docs/datasafe-log-analytics.md`;
  - add sanitized dashboard/query asset generation for the demo.
- Validation: script syntax tests pass and the workflow is now documented with
  explicit `--apply` gates and demo-only scope.

### Evidence bundle and operator scripts needed live Data Safe audit visibility

- Symptom:
  - the DB incident evidence bundle only used Data Safe for target inventory;
  - the export script could create the bridge, but operators had no bounded
    status view for recent Data Safe audit rows or Log Analytics hits.
- Fix:
  - add `list_data_safe_audit_events(...)` to the OCI CLI facade;
  - extend `src/dbman_opsi/db_incident.py` so Data Safe contributes both target
    context and recent audit events to the evidence timeline;
  - extend `scripts/demo-datasafe-log-export.sh` with `targets` and `status`
    commands, connector wait logic, and sanitized table output;
  - update docs to describe the replicable end-to-end order.
- Validation:
  - targeted tests passed for the new OCI CLI command shape, evidence bundle,
    and script help surface;
  - live `status` confirmed the custom-log connector is ACTIVE, target
    registration is present, and recent audit/log counts are visible.

### Live failed-login testing can lock the monitoring account and break observability

- Symptom: a deliberate wrong-password probe against the monitoring account
  caused `ORA-28000`, after which DBM/OPSI/Data Safe drilldowns lost their DB
  service-user path until the account was unlocked.
- Root cause:
  - failed-login drills were safe when scoped to `DBINC_LAB`, but not when
    aimed at the shared monitoring account;
  - the demo packet documented Data Safe audit generation but did not carry the
    DBA-only monitoring-account inspection and recovery SQL alongside it.
- Fix:
  - add `12-check-monitoring-account-status.sql` and
    `13-remediate-monitoring-account-lock.sql` to the generated DB incident
    packet;
  - update the packet runbook, `scripts/demo-db-incident-e2e.sh`, and the Data
    Safe export docs to make `DBINC_LAB` the only approved failed-login drill
    target and point operators to the recovery SQL for `ORA-28000`.
- Validation:
  - targeted packet-generation tests cover the new artifacts and manifest
    metadata;
  - shell/docs tests assert the warning remains visible in the operator path.

### Data Safe audit verification query used the wrong timestamp format model

- Symptom: the live DB incident packet reached the Data Safe audit verification
  step, but `11-verify-datasafe-demo-audit.sql` failed with
  `ORA-01821: date format not recognized`.
- Root cause: the generated query formatted `UNIFIED_AUDIT_TRAIL.EVENT_TIMESTAMP`
  with `TZH:TZM`, but the local DB type/implicit conversion path on the demo DB
  did not accept that timezone suffix.
- Fix:
  - change the generated `TO_CHAR(event_timestamp, ...)` format in
    `src/dbman_opsi/db_incident.py` to `YYYY-MM-DD\"T\"HH24:MI:SS.FF3` without
    the timezone fields;
  - add a regression assertion in `tests/test_db_incident.py`.
- Validation:
  - the packet generator test now asserts the timezone format suffix is absent;
  - the corrected script can be regenerated and rerun directly on the DB host
    without rebuilding the whole demo flow.

### Data Safe target ACTIVE did not mean audit collection was provisioned

- Symptom:
  - the demo PDB target and private endpoint were both `ACTIVE`;
  - the DB host showed real `UNIFIED_AUDIT_TRAIL` rows for `DBINC_LAB`;
  - `scripts/demo-datasafe-log-export.sh status` still showed zero Data Safe
    audit events and zero Log Analytics rows for the Data Safe custom source.
- Root cause:
  - target registration alone was not enough;
  - no Data Safe audit profile or audit trail resources existed in the
    compartment yet, so Data Safe had nothing to collect even after the DB-side
    audit policy and activity were valid.
- Fix:
  - extend `scripts/demo-datasafe-log-export.sh status` to report audit profile
    and audit trail counts alongside target counts;
  - update `docs/datasafe-log-analytics.md` so operators know to distinguish
    target registration from audit collection provisioning.
- Validation:
  - live status can now reveal the difference between "registered target, no
    audit collection" and "audit collection exists but no recent rows."

## 2026-06-25 DB Incident Observability Demo E2E

### Scope

- Area: `generate-db-incident-demo`, `scripts/demo-db-incident-e2e.sh`, generated SQL*Plus packet, Log Analytics evidence workflow, `oci-coordinator-oke` handoff assets.
- Goal: run a full demo-only DB incident workflow against a dedicated demo PDB, generate real Oracle errors plus synthetic alert-log markers, install Oracle HR/CO sample schemas, and verify DB-side and OCI-side troubleshooting paths.

### Real demo errors now generated successfully

- `ORA-00001` duplicate primary key
- `ORA-01400` null-in-not-null column
- `ORA-02291` foreign-key parent missing
- `ORA-00942` missing object
- `ORA-00054` NOWAIT lock conflict
- `PLS-00201` / `PLS-00905` / `ORA-06550` invalid-object and compiler diagnostics
- Reviewed synthetic alert-log markers for `ORA-00600` / `ORA-07445` correlation only

### CDB root schema creation failed with `ORA-65096`

- Symptom: the first live run failed creating `DBINC_LAB` with `ORA-65096: invalid common user or role name`.
- Root cause: the runner connected as local SYSDBA in CDB root and attempted to create a local demo user there.
- Fix:
  - add `DB_INCIDENT_PDB_NAME` support to the generated setup and cleanup SQL;
  - `ALTER SESSION SET CONTAINER` before user create/drop;
  - pass the PDB name only through the SSH-stdin remote process environment.
- Validation: live run created `DBINC_LAB` in the demo PDB successfully.

### PDB-aware values were not propagated to the remote workload

- Symptom: local `.env.local` had PDB settings, but the remote packet behaved as if they were unset.
- Root cause: `jumphost-run` only exported four DB incident variables into the remote workload environment.
- Fix: propagate `DB_INCIDENT_PDB_NAME`, `DB_INCIDENT_PDB_SERVICE`, `DB_INCIDENT_LAB_CONNECT`, and later `DB_INCIDENT_LAB_EZCONNECT`.
- Validation: the remote workload environment included the new variables and the setup SQL received the PDB name without persisting a credential file.

### Lab-user connect syntax was fragile across shell and SQL*Plus boundaries

- Symptoms:
  - `ORA-12154` when the lab-user service name was unresolved on the DB host
  - `ORA-12541` when testing against `127.0.0.1` and no listener was bound there
  - `SP2-0306: Invalid option` when a full connect string with quoting crossed the wrapper boundary badly
- Root causes:
  - the listener was bound on the DB host address, not loopback;
  - the earlier `DB_INCIDENT_LAB_CONNECT` approach embedded too much quoting in one variable.
- Fix:
  - introduce `DB_INCIDENT_LAB_EZCONNECT` as the target-only Easy Connect string;
  - make the generated runner build `DBINC_LAB/"$DB_INCIDENT_LAB_PASSWORD"@<target>` itself;
  - keep `DB_INCIDENT_LAB_CONNECT` as an override, but prefer `DB_INCIDENT_LAB_EZCONNECT` for DB-host execution.
- Validation: live DBINC_LAB connections succeeded and the workload executed end to end in the PDB.

### Disposable demo password failed policy checks

- Symptom: `ORA-28003` plus password verify message requiring two or more special characters.
- Root cause: the demo DB password verify function was stricter than the initial disposable password choice.
- Fix: rotate the local-only lab password to a demo-safe value that satisfies the verify function and still works with SQL*Plus when quoted by the runner.
- Validation: `DBINC_LAB`, `HR`, and `CO` users were created successfully in the live run.

### Incident evidence table and procedures hit `ORA-01031`

- Symptom: `incident_event_log` creation and `log_event` / `attempt_parent_lock_nowait` / `broken_compile_demo` creation failed with insufficient privileges.
- Root cause: the disposable lab schema only had `CREATE SESSION` and `CREATE TABLE`.
- Fix: grant `CREATE PROCEDURE` and `CREATE SEQUENCE` during lab-schema setup.
- Validation: the live workload created tables, procedures, compiler diagnostics, and lock-conflict evidence successfully.

### Generated query failed on `ORA-01821`

- Symptom: `03-query-evidence.sql` failed formatting `event_time`.
- Root cause: the column is plain `TIMESTAMP`, but the query used a timezone format model (`TZH:TZM`).
- Fix: remove the timezone suffix from the `TO_CHAR` format in generated evidence and troubleshooting queries.
- Validation: the live evidence timeline, repetition summary, and source coverage query completed successfully.

### Oracle sample schema installers were interactive

- Symptom: `hr_install.sql` and `co_install.sql` stopped on `ACCEPT` prompts and raised “password is mandatory”.
- Root cause: upstream sample-schema install scripts are interactive even when invoked from a SQL*Plus here-doc.
- Fix:
  - download the official Oracle sample schema archive at runtime;
  - rewrite the upstream `ACCEPT pass`, `ACCEPT tbs`, and `ACCEPT overwrite_schema` lines into non-interactive `DEFINE` statements in temporary `*.dbinc.sql` copies;
  - execute those rewritten copies from the original schema directories so relative `@@...` includes still resolve.
- Validation: live HR and CO installs completed and verified their row counts.

### Sample schema work directory was not writable to `oracle`

- Symptom: the installer failed creating `oracle-db-sample-schemas` under the copied packet directory.
- Root cause: the packet tree was copied by the SSH user and not writable by the `oracle` OS user.
- Fix: move sample-schema download/extract work into `DB_INCIDENT_WORK_DIR` with default `${TMPDIR:-/tmp}/db-incident-sample-schemas`.
- Validation: HR and CO were downloaded, extracted, installed, and granted to `DBINC_LAB`.

### Read-only troubleshooting query pack had data-dictionary mismatches

- Symptoms:
  - the privileges section failed against `ALL_TAB_PRIVS`;
  - the final lock/session section failed when `V$SESSION` was not visible to `DBINC_LAB`.
- Root causes:
  - `ALL_TAB_PRIVS` in this environment exposes `TABLE_SCHEMA`, not the originally queried column shape;
  - the disposable lab schema does not have catalog privileges for `V$SESSION`.
- Fix:
  - query `TABLE_SCHEMA` in the privileges section;
  - make the `V$SESSION` section non-fatal with `whenever sqlerror continue`.
- Validation:
  - invalid-object, compiler-error, privilege, and evidence-row sections returned useful results live;
  - `V$SESSION` is now treated as optional context instead of a hard failure.

### Fresh packet copies could execute stale files

- Symptom: remote runs sometimes used older generated content even after local fixes.
- Root cause: repeated copies into the same remote packet directory made it easy to confuse stale and current payloads during iterative debugging.
- Fix: for live validation, use a new timestamped `OUTPUT_DIR` per run.
- Validation: the successful end-to-end run used a unique packet directory and matched the latest generated content.

### Generated runner should fail on connect errors before running SQL blocks

- Symptom: a failed `connect` could still leave later SQL text running and printing misleading section headers.
- Root cause: the wrapper entered SQL files after `connect` without an early `whenever sqlerror exit`.
- Fix: add `whenever oserror exit 1` and `whenever sqlerror exit sql.sqlcode` before each `connect` in the generated shell runner and sample-schema installer.
- Validation: later failures surfaced immediately at the connect step instead of degrading into follow-on noise.

### OCI-side evidence bundle worked; Log Analytics scenario query stayed empty

- Symptom:
  - `scripts/demo-db-incident-e2e.sh logan-check` returned a bounded `db_incident_analysis` bundle with source status for Log Analytics, DBM, OPSI, and Data Safe;
  - `scripts/demo-db-incident-e2e.sh logan-scenario-check` returned zero matches for the fresh scenario even after the DB-side alert-log marker write succeeded.
- Interpretation:
  - the OCI evidence service path is working and source reachability is confirmed;
  - the specific alert-log marker records were not yet visible in Log Analytics during this validation window.
- Likely causes:
  - ingestion lag; or
  - Management Agent source/entity association drift for the alert log on the demo DB host.
- Fix path:
  - keep using Management Agent ingestion for the live demo;
  - verify alert-log source associations on the DB entity;
  - rerun `logan-scenario-check` after the next ingestion interval;
  - use the DB-side evidence timeline plus the OCI evidence bundle even when the fresh marker lines are not yet searchable.
- Validation:
  - DB-side execution, DBM list, OPSI list, and Data Safe list all succeeded live;
  - Log Analytics search for the scenario was still zero-row at the time of validation.

### Log Analytics DBCS source-association path used stale source names, stale CLI verb, and the wrong payload shape

- Symptom:
  - `dbman-opsi log-analytics --apply` could not configure DB log ingestion reliably;
  - the repo emitted friendly display names like `Oracle Database Alert Logs`;
  - the OCI CLI in this environment has no `log-analytics source upsert-association` command.
- Root cause:
  - the service expects canonical built-in source names such as `DBAlertLogSource`, `DBAuditLogSource`, `LinuxSyslogSource`, and `unifieddbauditlogfromdbsource122`;
  - the current OCI CLI uses `log-analytics assoc upsert-assocs`;
  - the API expects an `items` list with `associationProperties`, not a single object with `sourceProperties`.
- Fix:
  - normalize legacy/friendly source names to OCI canonical source names;
  - switch the CLI facade to `assoc upsert-assocs`;
  - emit generated association payloads as a JSON list using `associationProperties`;
  - persist any resolved log group or entity OCIDs back into the ignored local config during `--apply`.
- Validation:
  - focused tests now pass for source normalization, payload generation, CLI command shape, config persistence, and dry-run behavior.

### DBCS/Base DB Log Analytics ingestion needs a Management Agent-backed entity, not a detached manual entity

- Symptom:
  - live `log-analytics --apply` against the demo DBCS target created standalone Log Analytics entities, but OCI rejected every source association with `Entity is either not ready for association or not in the passed in compartment`;
  - the DB host had no OCI Management Agent installation.
- Root cause:
  - for the tested DBCS/Base DB path, OCI Log Analytics source associations require a Management Agent-backed ingestion path;
  - DBM/OPSI being enabled on an OCI-native database is not enough to make DB alert/audit/host log collection work in Log Analytics.
- Fix:
  - stop auto-creating detached entities when no Management Agent path is configured;
  - block early with a precise message: install a Management Agent with the `logan` plugin or supply existing Management Agent-backed entity OCIDs;
  - keep `logan_hostname`, `logan_oracle_home`, and `logan_adr_home` in the ignored local config so a later Management Agent install can reuse the same payloads.
- Validation:
  - live `dbman-opsi log-analytics --apply` now exits cleanly with a blocked target instead of an OCI traceback;
  - temporary detached entities created during validation were deleted from the demo compartment afterward.

### Management Agent install flow is now generated into the project for DBCS/Base DB Log Analytics demos

- Change:
  - `generate-agent-scripts` now emits install-key, host install, host verify, and OCI agent resolve scripts not only for external targets but also for `logan`-enabled DBCS/Exadata targets;
  - `generate-logan-payloads` now emits the same Management Agent packet directly inside each Log Analytics target directory as:
    - `03-create-logan-management-agent-install-key.sh`
    - `04-install-logan-management-agent.sh`
    - `05-verify-logan-management-agent.sh`
    - `06-resolve-logan-management-agent.sh`
    - `11-resolve-logan-management-agent-package-url.sh`
  - the same generators now emit an operator-side Ansible bundle for Linux collector installs:
    - `generate-agent-scripts`: `<target>-agent-resolve-package-url.sh`, `<target>-agent-ansible-bootstrap.sh`, `<target>-agent-ansible-run.sh`, `<target>-agent-ansible-playbook.yml`, `<target>-agent-ansible.cfg`
    - `generate-logan-payloads`: `07-bootstrap-logan-management-agent-ansible.sh`, `08-run-logan-management-agent-ansible.sh`, `09-logan-management-agent-playbook.yml`, `10-logan-management-agent-ansible.cfg`
  - generated install-key retrieval now uses the current OCI CLI flag `--install-key-id`.
  - generated host install and Ansible wrapper scripts accept either a local `AGENT_RPM` file or an OCI-resolved HTTPS `AGENT_RPM_URL`; remote downloads also require `AGENT_RPM_SHA256` and are verified before installation.
  - generated response/install-key files use private permissions, and temporary response files are removed on exit (including the Ansible remote install path).
- Purpose:
  - remove the manual OCI CLI/install-key/install/lookup steps from the demo setup path;
  - provide the same install path through either direct host execution or an operator-side Ansible run through a jumphost;
  - ensure the Management Agent is configured with the required plugin set and the operator has a repeatable way to resolve the resulting agent OCID back into ignored config.
- Validation:
  - targeted CLI/log analytics tests were updated and passed;
  - generated shell scripts were rendered locally and syntax-checked with `bash -n`.

### Operator guidance now encoded in the project

- `KB.md`: this error/solution map.
- `README.md`, `docs/demo-db-incident-e2e.md`, `docs/db-incident-troubleshooting.md`:
  - document `DB_INCIDENT_PDB_NAME`, `DB_INCIDENT_PDB_SERVICE`, and `DB_INCIDENT_LAB_EZCONNECT`;
  - call out that `DB_INCIDENT_LAB_EZCONNECT` is the preferred DB-host execution path;
  - state that Log Analytics scenario searches can lag behind a completed DB-side run.

## 2026-06-04 CAP DBM/OPSI End-To-End Enablement

### OCI CLI database discovery parser failure

- Symptom: `oci db database list` failed with generated CLI parser warnings about duplicated parameters.
- Scope: Base Database Service discovery.
- Root cause: The installed OCI CLI generated command shape is unreliable for direct database listing in this environment.
- Fix: Use OCI Python SDK discovery with the sequence: compartment -> DB system -> DB homes -> databases -> pluggable databases.
- Validation: SDK discovery found the target DB system, CDB, and PDB while the CLI path failed.

### CDB/PDB orchestration used stale preflight state

- Symptom: A PDB could remain blocked by "parent CDB not enabled" even when the same `configure --apply` run enabled the parent CDB first.
- Root cause: Decisions were computed from one initial preflight snapshot.
- Fix: Process ordered targets sequentially and carry forward parent CDB IDs that were enabled or confirmed in the same run.
- Validation: Added regression coverage for "PDB listed first, CDB enabled first, PDB enabled second."

### DBM enabled does not mean OPSI enabled

- Symptom: `configure --apply` skipped targets when Database Management was already enabled, leaving OPSI Database Insights uncreated.
- Root cause: The orchestrator treated DBM enabled as complete target success.
- Fix: If DBM is already enabled but OPSI credential payloads are ready, continue with the OPSI create/enable step.
- Validation: Added regression coverage for DBM-enabled and OPSI-missing targets.

### OPSI PE co-managed create payload issues

- Symptom: OPSI `create-pe-comanged-database` rejected payloads with `Cannot provide both opsiPrivateEndpointId and dbmPrivateEndpointId`.
- Root cause: The create path accepts the OPSI private endpoint, not both OPSI and DBM private endpoint IDs.
- Fix: Send only `--opsi-private-endpoint-id` for `create-pe-comanged-database`.
- Validation: Unit coverage updated and live command progressed past this validation.

### OPSI database resource type mismatch

- Symptom: OPSI create rejected `ORACLE_DATABASE` as unsupported.
- Root cause: The OPSI API expects OCI resource type strings, not older guessed labels.
- Fix: Use `database` for Base Database Service CDB/non-CDB targets and `pluggabledatabase` for PDB targets.
- Validation: Live command progressed past resource-type validation after config update.

### OPSI `DbcsEntityChangeWorkflowFailed`

- Symptom: OPSI `CREATE_DATABASE_INSIGHT` work request failed after "Starting data collections" with `DbcsEntityChangeWorkflowFailed`; Database Insight list remained empty.
- Known-good state: DBM managed-database inventory listed both the CDB and PDB as VM/ADVANCED, and DBSNMP was OPEN with required grants in both containers.
- Likely cause: OPSI could not start collection with the initial credential/source configuration or secret-access scope.
- Fix path:
  - Prefer Vault-backed `CREDENTIALS_BY_VAULT` payloads for demos.
  - Store the DBSNMP password in OCI Vault.
  - Confirm IAM allows DBM/OPSI principals to read that secret.
  - Retry OPSI create and inspect work request logs/errors.
- Validation status: DBM and DB-side grants validated; OPSI retry remains the next live verification step after Vault payload/config update.

### DB-side access through Bastion

- Symptom: Managed SSH Bastion session required a target Compute instance OCID, but the DBCS flow exposed DB node/VNIC metadata.
- Fix: Use a Bastion port-forwarding session to the DB node private IP on port 22, then SSH to `127.0.0.1:<local-port>` with an authorized key.
- Operational note: If no local key matches the DB system key, add a temporary public key to the DB system authorized keys while preserving existing keys. Remove it after the demo.
- Validation: SSH through the Bastion port-forward reached the DB node and allowed SQL*Plus execution as the Oracle OS user.

### DBSNMP password length

- Symptom: Rotating DBSNMP with a long generated quoted password failed with `ORA-00972: identifier is too long`.
- Root cause: The generated password exceeded what this 19c/profile combination accepted in the SQL statement.
- Fix: Use a shorter generated password that still satisfies complexity rules. Keep it in ignored local storage and OCI Vault only.
- Validation: DBSNMP remained OPEN and required grants were visible in CDB root and PDB.

### Network preflight warnings

- Symptom: Subnet was readable, but route table and service gateway reads returned `NotAuthorizedOrNotFound`; security list check did not prove listener ingress.
- Root cause: Network resources may be in a compartment or policy scope not fully readable by the current principal, or NSGs may be used instead of security lists.
- Fix: Treat these as warnings when target DBM/DB-side validation succeeds, but verify actual private endpoint/listener connectivity through DBM/OPSI work requests and collection startup.

## 2026-06-05 Performance Hub "requires granting of appropriate user privileges"

### Performance Hub greys out / prompts to grant DBSNMP privileges

- Symptom: OCI Console **Performance Hub** for a DBM-managed DBCS shows
  "Performance Hub requires granting of appropriate user privileges. After granting
  the required privileges, reopen Performance Hub." On-demand tasks (AWR, ADDM, ASH
  Analytics, SQL Tuning, Real-Time SQL Monitoring) are unavailable.
- Root cause: the DBM monitoring user (`DBSNMP`) had only the basic + advanced
  *monitoring* grants, not the Performance Hub set (which needs to run advisors and
  the workload repository).
- Fix: as SYSDBA, grant the exact set the Console asks for. `DBSNMP` is a CDB common
  user, so `CONTAINER=ALL` from the root covers the CDB **and** every PDB at once:
  ```sql
  grant create procedure to DBSNMP container=all;
  grant select any dictionary to DBSNMP container=all;
  grant select_catalog_role to DBSNMP container=all;
  grant alter system to DBSNMP container=all;
  grant advisor to DBSNMP container=all;
  grant execute on sys.dbms_workload_repository to DBSNMP container=all;
  ```
  (Diagnostics and/or Tuning Pack licensing applies — review before granting.)
- Toolkit: `03-grant-advanced-diagnostics.sql` now emits these (per container) and
  `04-validate-monitoring-user.sql` checks them. `src/dbman_opsi/db_scripts.py`,
  tests in `tests/test_db_scripts.py`.
- Live DB access for the grant: bastion **port-forward** session to the DB node
  `:22`, `ssh opc` with the DB-system key (kept under `generated/cap-ssh/`,
  gitignored — *not* the bastion-session key, which only authenticates the tunnel),
  `sudo su - oracle`, `sqlplus / as sysdba`, run the grants, then delete the bastion
  session.
- Validation: grants present in `dba_sys_privs` / `dba_role_privs` / `dba_tab_privs`
  in **both `CDB$ROOT` and `PDB1`**; `dbms_workload_repository.create_snapshot`
  succeeded (AWR — the Performance Hub data source — is live). Reopen Performance Hub.

## 2026-06-07 ADDM Spotlight / AWR Explorer empty for a PDB (+ ORA-13750)

### Performance Hub ADDM/AWR show no data for a PDB; SQL Tuning Set create fails

- Symptoms (DBM-managed Base DB, CDB+PDB):
  - **ADDM Spotlight** (PDB): "There are no ADDM analysis details available for the
    time period... Check the current AWR snapshot interval and retention period."
  - **AWR Explorer** (PDB): "No AWR snapshots were found for the selected database.
    Please enable automatic AWR snapshot collection or manually load AWR data for
    this PDB."
  - **Create SQL Tuning Set**: `ORA-13750 - User "DBSNMP" has not been granted the
    ADMINISTER SQL TUNING SET privilege.`
- Root cause: in a CDB, automatic AWR snapshots run at the **root only** by default
  (`AWR_PDB_AUTOFLUSH_ENABLED=FALSE`), so PDB-level ADDM/AWR have nothing to analyze.
  And the DBM monitoring user lacked the SQL-Tuning-Set admin privilege.
- Fix (as SYSDBA; verified live on cap CDB `DBMOPSI` + `PDB1`):
  ```sql
  -- SQL Tuning Set privilege (fixes ORA-13750); DBSNMP is a CDB common user
  grant administer sql tuning set     to DBSNMP container=all;
  grant administer any sql tuning set to DBSNMP container=all;

  -- Enable PDB-level AWR: master switch at root, then per-PDB interval
  alter system set awr_pdb_autoflush_enabled = true scope=both;          -- in CDB$ROOT
  alter session set container = PDB1;
  alter system set awr_pdb_autoflush_enabled = true scope=both;          -- in the PDB
  exec dbms_workload_repository.modify_snapshot_settings(interval=>60, retention=>11520);
  exec dbms_workload_repository.create_snapshot;                          -- seed
  ```
- Gotcha — **ADDM by PDB dbid**: inside a PDB, `dba_hist_snapshot` lists *both* the
  root snapshots and the PDB's own. `DBMS_ADDM.ANALYZE_DB` analyzes the PDB's
  `CON_DBID`, so the snapshot pair must be filtered to that dbid or you hit
  `ORA-13703 ... snapshots not found`:
  ```sql
  select min(snap_id), max(snap_id) into l_beg, l_end from (
    select snap_id from dba_hist_snapshot
    where dbid = sys_context('USERENV','CON_DBID')
    order by snap_id desc fetch first 2 rows only);
  dbms_addm.analyze_db(l_task, l_beg, l_end, sys_context('USERENV','CON_DBID'));
  ```
  Note: `ANALYZE_DB`'s first arg is **IN OUT task_name** — passing it positionally
  *and* as `task_name =>` raises `PLS-00703 multiple instances of named argument`.
- OOTB: the toolkit now generates `05-enable-performance-hub.sql` (AWR autoflush +
  PDB snapshot interval + seed) and adds the SQL-Tuning-Set grants to
  `03-grant-advanced-diagnostics.sql`. Run 05 for the CDB and each PDB.
  (`src/dbman_opsi/db_scripts.py`, tests in `tests/test_db_scripts.py`.)
- Validation: after the fix, `awr_pdb_autoflush_enabled=TRUE`, PDB AWR interval 1h /
  retention 8d, PDB AWR snapshots collecting, `DBMS_ADDM.ANALYZE_DB` task COMPLETED
  with a report ("ADDM detected that the system is a PDB"), and SQL Tuning Set
  creation succeeds.

## 2026-06-05 OPSI list flap → false `validate` NOT_FOUND (get-by-id fix)

### `validate` reports OPSI `NOT_FOUND` while insights are ACTIVE

- Symptom: `dbman-opsi validate` prints `Ops Insights NOT_FOUND (no Database Insight)`
  for the CDB and/or PDB even though `oci opsi database-insights list ...
  --lifecycle-state ACTIVE` shows them `ACTIVE` with
  `database-connection-status-details: SUCCESS`.
- Root cause (the real one): the OPSI `database-insights list` control plane in cap
  is **non-deterministic**. Passing the full `--lifecycle-state` set
  (`CREATING UPDATING ACTIVE FAILED NEEDS_ATTENTION`) together with `--all` in a
  **single** call makes it flap between the full set, a partial set, and an exit-0
  **empty** list for the same compartment, call to call (observed bouncing 0 / 2 / 7
  items within seconds). `validate` matched the target `database-id` against
  whatever that one flaky list happened to return → frequent false `NOT_FOUND`.
  This was previously mislabeled a "known cap quirk"; it is partly self-inflicted by
  the multi-state query shape.
- Reliable signal (measured): a **single-resource**
  `oci opsi database-insights get --database-insight-id <ocid>` is rock-solid
  (10/10 `ACTIVE` for both CDB and PDB across back-to-back calls), where the
  aggregated list flaps. A single `--lifecycle-state ACTIVE` list is stable in good
  windows but still drops to empty in bad windows — not trustworthy alone.
- Fix (code):
  1. `OciCli.list_opsi_database_insights` now queries **one lifecycle state per call
     and unions** results by insight OCID (each per-state call is individually
     fault-tolerant), instead of the broken multi-state + `--all` single call.
  2. New `OciCli.get_opsi_database_insight(insight_id)` (single-resource GET).
  3. `validate` prefers the reliable GET: it reads `target.opsi_database_insight_id`
     (now persisted in config) and calls `database-insights get`; only when the OCID
     is unknown does it fall back to the list, and a positive list hit is then GET
     for the authoritative state.
  4. List-fallback verdict model never emits a *false* `NOT_FOUND`: a positive
     `database-id` hit is authoritative; a negative is `NOT_FOUND` only from a
     **clean window** — every attempt answered, every answer was a **complete**
     per-state union (no lifecycle state skipped by a failed call), non-empty, and
     the **same id-set on ≥2 attempts** without the target. Any empty / erroring /
     incomplete / varying read makes the window inconclusive →
     `UNKNOWN (insight query failed; verify in OCI Console)`. (`list_opsi_database_insights_complete`
     carries the completeness flag; hardening per Codex review — an insight hiding
     in a skipped `FAILED` state can no longer be mistaken for absent.)
  5. Persisted both insight OCIDs in `dbman-opsi.cap.local.yaml`
     (`opsi_database_insight_id:` per target) so `validate` is deterministic.
- Files: `src/dbman_opsi/oci_cli.py`, `src/dbman_opsi/validation.py`. Tests:
  `tests/test_oci_cli.py` (per-state union + fault tolerance),
  `tests/test_validation.py` (get-by-id, positive-authoritative, stability-gated
  NOT_FOUND, varying-list → UNKNOWN).
- Validation: after the fix, `validate` reports `Ops Insights ACTIVE (ENABLED)` for
  both CDB and PDB on repeated runs.
- Discipline note (debugging pitfall that cost time here): `CommandRunner(dry_run=...)`
  **defaults to `True`**. In dry-run mode `run()` returns a stub `{}` for *every*
  call, so `OciCli(profile, region, CommandRunner())` (default) makes every read
  return empty — indistinguishable from the flaky-endpoint symptom. When
  reproducing read-only behavior in a REPL, pass `CommandRunner(dry_run=False)`.
  The CLI's read paths (`validate`, `preflight`, `configure` reads) correctly use
  `dry_run=False`.

## 2026-06-07 Redaction in the data path broke OCID-keyed joins (Data Safe detection)

- Symptom: live `discover` reported **Data Safe ENABLED for every database** in the
  compartment, including databases with no registered Data Safe target. The same
  matcher returned the correct NOT_ENABLED when called directly in a REPL with a
  pasted real OCID.
- Scope: any feature that joins two OCI resources by OCID parsed out of CLI output
  — the new discovery pillar matching (OPSI insight / Data Safe target per DB),
  `create_named_credential`/`set_preferred_named_credential` id linkage,
  `find_managed_database_id`, and `validate`'s insight id-set comparison.
- Root cause: `CommandRunner.run()` ran `redact_text()` over `process.stdout`
  **before** `CommandResult.json()` parsed it, so every OCID became the literal
  string `<OCI_OCID>`. With both sides of a join collapsed to the same token,
  `wanted & candidate_ids` matched everything-to-everything. Redaction (a display
  concern) was wrongly applied in the data path.
- Fix:
  - Runner returns RAW stdout/stderr for `.json()`. Only the dry-run command echo
    and the `RuntimeError` message stay redacted (those are user-facing text).
  - Redact at the display boundary instead: CLI `--json` output wraps `to_dict()`
    in `redact_data()`. Human `discover` output already prints real OCIDs on
    purpose (operators copy them into config; it is their own tenancy).
  - Second bug in the same area: the Data Safe `target-database list` summary has
    `database-details = null` and carries the registered DB OCID in
    `associated-resource-ids`; the matcher now reads that and no longer treats a
    target's own `id` as a DB reference.
- Validation: live on cap — DBMOPSI/PDB1 correctly NOT_ENABLED for Data Safe; the
  three registered ATP targets ENABLED; an unregistered ATP NOT_ENABLED.
- Prevention: never redact in a value that downstream logic parses. Redact only at
  print/serialize boundaries (`--json`, `sanitized()`, log lines, error strings).

## 2026-06-07 Provisioning a new Base DB via zero-start-poc terraform (apply-time failures)

The example planned cleanly but failed at apply with a sequence of issues; all are
now fixed in `terraform/examples/zero-start-poc`:

1. **`Attempt to index null value` on the AD data source.** The `provider "oci"`
   block only set `region`, so it used default auth (wrong/empty tenancy) and
   `oci_identity_availability_domains` returned null. Fix: add
   `config_file_profile = var.config_file_profile` and pass the profile (e.g.
   `cap`). Symptom is generic — any data source silently returns empty.
2. **`vm-block-storage-gb` LimitExceeded.** This is a **Database** service limit
   (not Block Volume), enforced **per availability domain** (1050 GB/AD here). The
   existing DB system filled AD-1 (1024/1050); AD-2/AD-3 were empty. The terraform
   hardcoded `ads[0]`. Fix: `availability_domain_index` var to pin a DB system to
   an AD with headroom. Check with:
   `oci limits resource-availability get --service-name database --limit-name vm-block-storage-gb --availability-domain <AD> --compartment-id <tenancy>`.
3. **`domain name cannot be null` on LaunchDbSystem.** The subnet has no DNS label,
   so the DB system can't derive its network domain and one must be passed
   explicitly. Fix: `domain = var.dbcs_domain`; reuse the existing DB system's
   domain (`oci db system get ... --query 'data.domain'`).
4. **Flex shape needs explicit sizing.** `VM.Standard.E4.Flex` requires
   `cpu_core_count`, and a VM DB system requires `data_storage_size_in_gb`
   (min 256). Added both as vars.

Secrets (`ssh_public_keys`, `db_admin_password`) go in a gitignored
`secrets.auto.tfvars.json` (now `*.auto.tfvars*` and `*.tfvars` are gitignored),
never in `render_tfvars` or committed files.

## 2026-06-07 Data Safe target stuck NEEDS_ATTENTION (ORA-01017) + DBSNMP rotation

- Symptom: `data-safe target-database` registers but stays NEEDS_ATTENTION with
  `lifecycle-details = "Failed to connect to database. ORA-01017: invalid
  username/password"`. The network path is fine (DS PE reached the listener) —
  only the credential failed.
- Root cause: the stored DBSNMP password was stale, and DBSNMP could not be reset
  to it because the **CDB** password verify function requires **2+ special
  characters** (`ORA-20000`). DBSNMP is a **common user**, so its password must be
  changed from the root with `alter user DBSNMP identified by "..." container=ALL`
  — an in-PDB `alter user` fails with `ORA-65066` ("must apply to all containers").
- Fix (single-account POC): rotate DBSNMP to a policy-compliant password
  CONTAINER=ALL via Bastion, then keep the stack consistent by updating BOTH the
  Vault secret (DBM + OPSI both read it via `passwordSecretId`) and the Data Safe
  target credentials (`data-safe target-database update --credentials file://... --force`).
  DBM monitoring stayed `UP`, OPSI `ENABLED`, Data Safe target reached `ACTIVE`.
- `data-safe private-endpoint create` and `target-database update` return WORK
  REQUESTS: `--wait-for-state` takes `SUCCEEDED`, not `ACTIVE`. `update` also needs
  `--force` to skip the confirmation prompt non-interactively.
- Detection nuance: a DATABASE_CLOUD_SERVICE target registered with a PDB service
  name associates (in `associated-resource-ids`) with the **DB system**, so
  discovery attributes Data Safe to the CDB/DB-system level, not the individual PDB.

## Current Demo Validation Checklist

- DB system lifecycle: AVAILABLE.
- CDB lifecycle: AVAILABLE.
- PDB lifecycle: AVAILABLE.
- DBM private endpoint: ACTIVE.
- OPSI private endpoint: ACTIVE.
- CDB DBM status: ENABLED.
- PDB DBM status: ENABLED.
- DBM managed database inventory: CDB and PDB listed as VM/ADVANCED.
- DBSNMP: OPEN in CDB root and PDB.
- Grants: `CREATE SESSION`, `SELECT ANY DICTIONARY`, `SELECT_CATALOG_ROLE`; advanced grants present in the live test.
- OPSI Database Insight: pending successful create after Vault-backed credential payload is wired into config.

## Repeatable Diagnostic Commands

Use these patterns with local variables. Do not paste raw command output containing OCIDs or IPs into committed files.

```bash
oci database-management managed-database list \
  -c "$COMPARTMENT_OCID" \
  --deployment-type VM \
  --management-option ADVANCED

oci database-management work-request list \
  -c "$COMPARTMENT_OCID" \
  --sort-order DESC

oci opsi database-insights list \
  -c "$COMPARTMENT_OCID"

oci opsi work-requests list \
  -c "$COMPARTMENT_OCID" \
  --sort-order DESC
```

SQL validation:

```sql
select username, account_status
from dba_users
where username = 'DBSNMP';

select privilege
from dba_sys_privs
where grantee = 'DBSNMP'
  and privilege in ('CREATE SESSION', 'SELECT ANY DICTIONARY', 'ANALYZE ANY', 'ANALYZE ANY DICTIONARY')
order by privilege;

select granted_role
from dba_role_privs
where grantee = 'DBSNMP'
  and granted_role = 'SELECT_CATALOG_ROLE';
```

### OPSI CREATE_DATABASE_INSIGHT fails at 80% — DbcsEntityChangeWorkflowFailed

- Symptom: `oci opsi database-insights create-pe-comanged-database` (PE-comanaged DBCS)
  reaches 80% then FAILED. Work-request error (via REST
  `GET /20200630/workRequests/{id}/errors`): `Failed to create Database Insight.,
  Error: DbcsEntityChangeWorkflowFailed`. Work-request logs stop at
  `Starting data collections` / `Fetch system infrastructure details`.
  Database Management ADVANCED on the same DB/user/port looks fine, masking the issue.
- Scope: Base Database Service CDB and PDB, OPSI Database Insight with
  `CREDENTIALS_BY_VAULT` over an OPSI private endpoint.
- Why DBM hid it: DBM connects by managed-database OCID and reports lifecycle
  `ENABLED` even when its data-path auth is broken; only OPSI's create runs an
  explicit connect-and-collect test, so OPSI is the first place the failure surfaces.
- Root cause (two independent defects, both fatal to OPSI):
  1. **Wrong service name.** OPSI `connection-details.serviceName` was set to the
     bare DB/PDB name (e.g. `DBMOPSI`, `PDB1`). The listener registers no such
     service — real services are domain-qualified
     (`<db_unique_name>.<db_domain>` for the CDB root, `<pdb_name>.<db_domain>`
     for the PDB). Connecting with the bare name returns **ORA-12514**.
  2. **Credential drift.** The monitoring-user (DBSNMP) password stored in the
     Vault secret did not match the database. Connecting with the correct service
     returned **ORA-01017**. The Vault password also violated the DB password
     verify function (**ORA-20000: password must contain 2 or more special
     characters**), so it could never have been applied — the secret was written
     but the `ALTER USER` had silently been rejected at provisioning time.
- Diagnosis path (no Console needed):
  - `oci opsi work-requests list` → find FAILED `CREATE_DATABASE_INSIGHT`.
  - `oci raw-request --http-method GET --target-uri .../workRequests/{id}/errors`
    and `.../logs` (the `oci opsi work-requests` CLI group has no errors/logs
    subcommand in 3.81.x).
  - On the DB host (bastion port-forward to :22 → `sqlplus / as sysdba`):
    `lsnrctl status` to list real services; test
    `DBSNMP/<pw>@<db_ip>:1521/<service>` for each candidate to separate
    ORA-12514 (wrong service) from ORA-01017 (wrong password).
  - Repeated bad-password probes will lock DBSNMP (**ORA-28000**); unlock with
    `ALTER USER DBSNMP ACCOUNT UNLOCK CONTAINER=ALL`.
- Fix:
  1. Set a policy-compliant DBSNMP password (>=2 special chars, mixed case,
     digit) and sync it to the Vault secret:
     `ALTER USER DBSNMP IDENTIFIED BY "<pw>" CONTAINER=ALL;` then
     `oci vault secret update-base64 --secret-id <id> --secret-content-content <b64>`.
  2. Correct `service_name` in the config to the real listener service, regenerate
     the OPSI payloads, then disable+delete the FAILED insights
     (`disable` first — a FAILED insight cannot be deleted directly:
     "Database Insight should be disabled before it can be deleted") and re-run
     `enable --apply`.
- Validation: new `CREATE_DATABASE_INSIGHT` SUCCEEDED 100%; insight
  `lifecycle-state ACTIVE`, `database-connection-status-details SUCCESS` for both
  CDB and PDB.

### enable is not idempotent — DBM 409 aborts before OPSI

- Symptom: `dbman-opsi enable --apply` crashes with
  `IncorrectState: Either DatabaseManagement is already enabled or request to
  enable it is already created.` (HTTP 409) and never reaches the Ops Insights
  step. Hits every re-run once DBM is enabled.
- Root cause: `EnablementService._enable_cloud_database` called the DBM enable
  unconditionally and let the runner raise, so a benign already-enabled state
  killed the whole flow.
- Fix: `OciCli.run_tolerating(args, tolerated)` swallows errors whose message
  contains an idempotent marker ("already enabled" / "already created") and
  re-raises anything else; `_enable_cloud_database` uses it and continues to OPSI.

### validate could not see OPSI collection state (silent OPSI failure)

- Symptom: `dbman-opsi validate` printed
  "Ops Insights requires Database Insight validation" for every DBCS/Exadata
  target regardless of the real state, so a fleet of FAILED insights looked the
  same as healthy ones.
- Fix: `validate` now calls `OciCli.list_opsi_database_insights` (querying all
  lifecycle states explicitly, since the list excludes FAILED by default), matches
  by `database-id == target.resource_id`, and reports the real
  `lifecycle-state (status)` — e.g. `ACTIVE (ENABLED)`, `FAILED (ENABLED)`,
  `NOT_FOUND (no Database Insight)`. Retries once on transient
  NotAuthorizedOrNotFound and degrades to `UNKNOWN (...)` rather than lying.
- Note: in CAP the OPSI `database-insights list` endpoint intermittently returns
  NotAuthorizedOrNotFound / empty even when insights exist and are ACTIVE; the
  authoritative cross-checks are the SUCCEEDED `CREATE_DATABASE_INSIGHT` work
  request and `database-connection-status-details: SUCCESS` on the insight.

### DBSNMP re-locks after password rotation (ORA-28000 lock loop)

- Symptom: after rotating the DBSNMP password (to fix OPSI/DBM credential drift),
  DBM monitoring goes green briefly then flips to **Stopped** / red timeline, and
  OPSI collection stalls ("Needs attention"). Console error:
  `ORA-28000 - The account is locked` (`DB_Account_Lock`). Account status cycles
  OPEN -> LOCKED within minutes of being unlocked.
- Root cause: on Base Database Service the **local Oracle Cloud Agent** monitors
  the DB as DBSNMP using the password set at provisioning time. Rotating DBSNMP's
  password without updating that agent leaves a consumer repeatedly authenticating
  with the old password; it trips the profile's `FAILED_LOGIN_ATTEMPTS` and locks
  the account, which then knocks out DBM and OPSI (collateral damage) since they
  share the same DB user.
- Fix (break the lock loop): put DBSNMP on a dedicated non-locking common profile.
  ```sql
  CREATE PROFILE C##DBSNMP_MON LIMIT FAILED_LOGIN_ATTEMPTS UNLIMITED PASSWORD_LIFE_TIME UNLIMITED;
  ALTER USER DBSNMP PROFILE C##DBSNMP_MON CONTAINER=ALL;   -- common profile needs C## prefix (ORA-65140 otherwise)
  ALTER USER DBSNMP ACCOUNT UNLOCK CONTAINER=ALL;
  ```
  A bare `ACCOUNT UNLOCK` is not enough — the stale agent re-locks it within
  minutes. With the non-locking profile, the stale consumer's failures no longer
  lock the account, so DBM (via DBM PE) and OPSI (via OPSI PE) — which use the
  correct password from the Vault secret — connect and stay connected.
- Prevention: avoid rotating DBSNMP unless every consumer is updated. If a rotation
  is unavoidable, assign the non-locking monitoring profile first. DBM monitoring
  status takes a few minutes to re-poll from UNKNOWN/Stopped back to healthy after
  the account is fixed.
- Related: DBM "Credential required ... Advanced diagnostics preferred credential
  is not set" is a separate item — the managed database's `PC_READ`/`PC_WRITE`
  preferred credentials are `NOT_SET` (only `MONITORING` is `SET`). Set them with
  `oci database-management preferred-credential update --type BASIC` (userName
  DBSNMP, role NORMAL, passwordSecretId <vault-secret>) or via the Console banner;
  it gates on-demand advanced tasks, not basic collection.

### DBM monitoring stays Stopped after re-enable — stale connection (wrong service name)

- Symptom: Database Management monitoring shows **Stopped** (red timeline, Console
  `database-status: UNKNOWN/Stopped`) even after the DBSNMP account is unlocked and
  the Vault password is correct. The DBM "Managed database details" still loads but
  never collects.
- Root cause: DBM was first enabled with the wrong `--service-name` (bare
  `DBMOPSI`/`PDB1`). A later `enable --apply` only **tolerated** the
  already-enabled 409 and skipped DBM, so the corrected service name (and rotated
  credential) never reached the DBM connection — it kept resolving a non-existent
  service (ORA-12514) and could not connect.
- Fix: reconcile the existing DBM connection in place with the corrected values —
  no disable/re-enable needed:
  ```bash
  oci db database modify-database-management --database-id <cdb> \
    --management-type ADVANCED --service-name <db_unique_name>.<domain> \
    --password-secret-id <secret> --private-end-point-id <dbm-pe> \
    --user-name DBSNMP --role NORMAL --protocol TCP --port 1521 \
    --wait-for-state AVAILABLE          # NOTE: DB lifecycle state, not work-request SUCCEEDED
  oci db pluggable-database modify-pluggable-database-management --pluggable-database-id <pdb> \
    --service-name <pdb>.<domain> --password-secret-id <secret> --private-end-point-id <dbm-pe> \
    --user-name DBSNMP --role NORMAL --protocol TCP --port 1521 --wait-for-state AVAILABLE
  ```
  After the modify, `database-status` flips to `UP` within a minute or two.
- Code: `enable` now reconciles automatically — on an already-enabled DBM it calls
  `cloud_modify_command` (modify-(pluggable-)database-management) so a corrected
  service name / rotated credential actually takes effect on re-run (needed for
  repeatable ORM/script enablement). `src/dbman_opsi/enablement.py`.
- Console URL bases (eu-frankfurt-1): DB systems `cloud.oracle.com/dbaas/dbsystems`.
  (The Database Management / Ops Insights SPA routes are not the obvious
  `/dbmgmt` or `/opsi`; navigate via the console menu rather than guessing.)

### Disposable Terraform VCN rejected with `400 Invalid tags`

- Symptom: OCI rejects `oci_core_vcn` creation with `400-InvalidParameter,
  Invalid tags` before any network resource is created.
- Root cause: this tenancy rejects dotted freeform tag keys such as
  `dbman-opsi.lifecycle`.
- Fix: use OCI-compatible underscore keys consistently across all lifecycle
  resources: `dbman_opsi_lifecycle`, `dbman_opsi_disposable`, and
  `dbman_opsi_evidence_retain`. Re-plan before retrying; the failed request does
  not need cleanup.

### DBCS launch rejected because `sshPublicKeys[0]` has invalid type

- Symptom: `LaunchDbSystem` returns `400 InvalidParameter` and identifies an SSH
  key beginning with `[` as invalid.
- Root cause: local `.env` supplied a bracketed SSH key value which was wrapped a
  second time as a Terraform list, yielding a literal `"[ssh-rsa ...]"` value.
- Fix: normalize the local value exactly once to a Terraform JSON list of public
  keys. Never log the key or place it in committed tfvars.

### Bastion create response contains a work-request ID, not the Bastion ID

- Symptom: `bastion session create-managed-ssh` fails `NotAuthorizedOrNotFound`
  when passed the ID captured from `bastion bastion create --query data.id`.
- Root cause: the asynchronous create response exposes a `bastionworkrequest`
  identifier at that path, not the active `bastion` resource identifier.
- Fix: wait for the work request, then resolve the active Bastion through
  `bastion bastion list` (or the work-request resource metadata) and use that
  resource ID for all session operations. Keep work-request and resource IDs in
  separate transient variables.

### OCI list APIs can return empty or stall during asynchronous provisioning

- Symptom: filtered `compute instance`, `bastion`, or OPSI list calls may return
  empty output or exceed normal response time immediately after create.
- Fix: use bounded OCI connection/read timeouts, then query the specific resource
  ID once it is known. Do not submit another create while Terraform/state or a
  work request is active. Treat an empty list as inconclusive, not as absence.

### Managed SSH Bastion session rejects a newly launched jump host

- Symptom: `create-managed-ssh` returns `InvalidParameter` stating that the
  Bastion plugin must be enabled on the target instance.
- Root cause: Oracle Cloud Agent plugin configuration on a newly launched image
  did not have the Bastion plugin available/running yet.
- Fix: enable Oracle Cloud Agent with management/monitoring/plugins enabled,
  wait for plugin startup, then retry. If the plugin control plane remains
  unreliable, use a Bastion **port-forwarding** session instead; it does not
  require the Managed SSH plugin.

### Bastion port-forward command contains two local placeholders

- Symptom: executing OCI-provided SSH metadata fails with
  `bash: localPort: No such file or directory` or a missing identity file.
- Root cause: port-forward metadata contains both `<privateKey>` and
  `<localPort>` placeholders. Shell parameter substitution can also drop a
  leading slash when the replacement starts with `/`.
- Fix: use a temporary session-specific SSH key, replace both placeholders, and
  preserve an absolute private-key path. Use a dedicated temporary known-hosts
  file with `StrictHostKeyChecking=accept-new`; never disable host-key checking
  globally or write session keys into the repository.

### Resource Manager public-schema gate rejects sensitive-word descriptions

- Symptom: `test_resource_manager_schema_exists_for_public_stack` fails when
  `schema.yaml` contains the word `password`, even in explanatory text.
- Root cause: the public-stack readiness gate intentionally bans credential-like
  terms from the Resource Manager input surface to prevent a stack from inviting
  secret input.
- Fix: keep the schema credential-free and use neutral wording such as
  “plaintext credential values”; route all secret values through Vault and
  ignored local runtime configuration.

### Bastion port-forward SSH returns `Permission denied (publickey)` after creation

- Symptom: OCI-provided SSH metadata reaches the Bastion, but authentication
  fails even with the matching session private key.
- Root cause: inspect the specific session before debugging key material. A
  session can already be `DELETED`; its retained metadata is not usable for a
  new connection.
- Fix: create a fresh port-forward session with a fresh temporary public key,
  poll `bastion session get` until `ACTIVE`, and only then run its generated SSH
  metadata command with the matching private key. Do not reuse metadata from a
  deleted session.

### Disposable cleanup queries the wrong OCI region

- Symptom: a resource known to exist in the selected deployment region returns
  `NotAuthorizedOrNotFound`, or cleanup appears to make no progress.
- Root cause: the OCI CLI profile has a default home region while the disposable
  stack was deployed in another selected region. A bare `oci` command silently
  uses that profile default.
- Fix: every demo operation passes the selected `--profile` and `--region`; the
  incident runner now derives those values from the local target config and
  stops before making OCI changes when the environment region differs from the
  target config. The disposable Bastion runner also requires a lifecycle ID and
  matches both the display name and lifecycle tag, so it cannot select a
  similarly named resource from another demo run.
