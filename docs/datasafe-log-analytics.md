# Data Safe To Log Analytics

Owning product requirement: [Observability 360](product/prd-observability-360.md).

This repo's Data Safe export flow is **demo-only**. It is meant to showcase OCI
Observability and AI troubleshooting capabilities on segregated PoC databases,
not to be copied unchanged into production.

## What it does

`scripts/demo-datasafe-log-export.sh` covers the missing bridge between Data
Safe audit collection and Log Analytics correlation:

1. Reuse or create an **OCI Logging** log group and custom log for Data Safe
   audit events.
2. Reuse or create a dedicated **Log Analytics** log group for those audit
   records.
3. Reuse or create **Service Connector Hub** connectors for:
   - OCI Logging custom log -> Log Analytics
   - OCI Audit -> Log Analytics
4. Seed and sync recent **Data Safe audit events** into the OCI Logging custom
   log so the demo has searchable records.
5. Write sanitized **dashboard/query assets** under `generated/datasafe-observability/`.
6. Expose a safe **status surface** so operators can validate targets, service
   connectors, and recent event counts without printing raw OCIDs in the
   normal command path.

## Replicable end-to-end order

The bridge only makes sense after the database is already wired for DB
observability and Data Safe registration.

1. Install the OCI **Management Agent** on the DB VM and configure Log
   Analytics source associations:

   ```bash
   dbman-opsi generate-agent-scripts --config <ignored-demo-config> --output generated/agents
   dbman-opsi configure --config <ignored-demo-config> --apply --with-log-analytics --skip-credentials
   ```

2. Prepare the database-side **Data Safe service account**:

   ```bash
   dbman-opsi generate-db-scripts --config <ignored-demo-config> --output generated/db-scripts
   sqlplus / as sysdba @generated/db-scripts/<target>/06-enable-data-safe.sql
   ```

3. Register the database as a **Data Safe target**:

   ```bash
   export DBMAN_OPSI_DBSNMP_PASSWORD='<service-account-password>'
   dbman-opsi data-safe --config <ignored-demo-config> --apply --user DBSNMP --password-env DBMAN_OPSI_DBSNMP_PASSWORD
   ```

4. Create or reuse the **OCI Logging / Service Connector / Log Analytics**
   bridge:

   ```bash
   scripts/demo-datasafe-log-export.sh --apply apply
   ```

5. Push recent **Data Safe audit events** into the OCI Logging custom log:

   ```bash
   scripts/demo-datasafe-log-export.sh --apply sync
   ```

6. Validate the end-to-end state:

   ```bash
   scripts/demo-datasafe-log-export.sh targets
   scripts/demo-datasafe-log-export.sh status
   ```

If the compartment has no recent Data Safe audit rows yet, the fastest demo-safe
way to create them is to run the DB incident packet with:

```bash
export DB_INCIDENT_DATASAFE_AUDIT_ENABLED=true
export DB_INCIDENT_DATASAFE_AUDIT_FAILED_LOGIN_ENABLED=true
generated/db-incident-demo/run-db-incident-demo.sh
```

That creates a bounded unified-audit policy for `DBINC_LAB`, generates reviewed
successful and failed login activity, and gives Data Safe real audit records to
export.

Do not create those failed-login rows by probing `DBSNMP` or other monitoring
users with a wrong password. Use the disposable `DBINC_LAB` path only. If the
monitoring account is already locked, recover it with the packet-local DBA SQL:
`12-check-monitoring-account-status.sql` and
`13-remediate-monitoring-account-lock.sql`.

## Commands

```bash
scripts/demo-datasafe-log-export.sh prereq
scripts/demo-datasafe-log-export.sh plan
scripts/demo-datasafe-log-export.sh targets
scripts/demo-datasafe-log-export.sh --apply apply
scripts/demo-datasafe-log-export.sh --apply sync
scripts/demo-datasafe-log-export.sh status
scripts/demo-datasafe-log-export.sh dashboard
```

The script reads tenant-specific values from environment variables or the
ignored local config:

- `CONFIG`
- `PROFILE` / `OCI_PROFILE`
- `REGION` / `OCI_REGION`
- `HOURS_LOOKBACK`

`targets` and `status` are the normal demo-safe operator views. They keep the
output bounded and avoid raw tenant identifiers in the default table output.

## How it fits the troubleshooting workflow

After the bridge is in place:

- Data Safe target registration tells the agent which databases are under
  security monitoring.
- Data Safe audit events become searchable in OCI Logging / Log Analytics.
- `dbman-opsi db-incident` and `scripts/demo-db-incident-e2e.sh logan-check`
  can combine:
  - DB alert / host logs from the Management Agent path
  - DBM and OPSI context
  - OCI Audit events
  - Data Safe target context and audit activity

This is the intended path for `oci-coordinator-oke` drilldowns when the prompt
asks for credential failures, privilege changes, suspicious DDL, or cross-source
incident timelines.

## Reading the status output

- `Data Safe target databases`: registration inventory for the configured
  compartment.
- `Recent Data Safe audit events`: how many Data Safe audit rows exist in the
  lookback window before export.
- `Recent Log Analytics rows for dbman-opsi-datasafe-audit`: how many rows are
  already searchable in Log Analytics after the bridge.

If the first count is non-zero and the second is zero, the export bridge has
not been seeded yet. If both are zero, the most common causes are:

1. no recent audited DB activity in the lookback window,
2. Data Safe audit collection is not enabled for the target policy you expect,
3. Log Analytics ingestion lag after connector changes.

`status` also reports the compartment-wide counts for Data Safe audit profiles
and audit trails. A target database can be `ACTIVE` and still produce zero
audit events when:

1. the target registration exists, but no Data Safe audit profile has been
   created yet,
2. an audit profile exists, but audit trails were not discovered and started,
3. the service account was previously locked and Data Safe has not resumed
   collection yet.

## Current live-state note

In the live demo tenancy used during validation, the Data Safe target and
private endpoint for the current DBCS target already existed; the local config
simply did not reflect them. The script above addresses the **log export**
portion that was still missing from this repo's documented operator path.
