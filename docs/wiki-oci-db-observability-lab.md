# OCI DB Observability Wiki And Lab Guide

This page is the operator wiki for the `dbman-opsi` demo environment. It explains
the product capabilities, breaks down each OCI component, and gives a replicable
step-by-step lab flow for the demo environment.

This guide is for demo and lab use only. It is not a production deployment guide.
Use dedicated demo databases or disposable PDBs only. Keep tenancy names, OCIDs,
hostnames, IPs, connect strings, passwords, wallets, and secrets in ignored local
files or environment variables.

## Purpose

The project demonstrates how to combine four OCI pillars into one troubleshooting
workflow for Oracle databases:

1. `Database Management (DBM)` for managed-database state, waits, AWR, ADDM, and
   top-SQL drilldowns.
2. `Operations Insights (OPSI)` for database insight, capacity, SQL insights, and
   fleet context.
3. `Data Safe` for audit and security context.
4. `Log Analytics` for fast log ingestion, search, correlation, and dashboarding.

The AI layer sits on top of those pillars. The point is not just to detect an
ORA code. The point is to answer:

- what happened,
- when it started,
- what changed before it,
- whether it repeated,
- what the database, host, audit, and application signals show in the same window,
- what evidence is still missing,
- and what the DBA or SRE should do next.

## Product Capabilities

### Database Management

DBM covers the managed-database operational view:

- managed database registration and health,
- Performance Hub,
- AWR and ADDM-based analysis,
- wait events and session context,
- SQL tuning and top SQL workflows,
- preferred credentials for advanced diagnostics.

In this project, DBM is used for:

- validating that the target is really managed,
- proving that the monitoring user is healthy,
- giving the AI workflow direct operational context around the incident window.

### Operations Insights

OPSI adds fleet and trend context:

- Database Insight lifecycle and status,
- capacity trend and forecast,
- SQL insight and SQL explorer views,
- multi-database and multi-region context,
- longer-horizon trend analysis beyond a single incident spike.

In this project, OPSI is used for:

- confirming the database insight is healthy,
- surfacing fleet or capacity pressure around the same time,
- supporting root-cause discussion with longer-range context.

### Data Safe

Data Safe adds security and audit context:

- target database registration,
- audit service-account path,
- audit profiles and audit trails,
- audit event summaries,
- security assessment and user/security context.

Important distinction:

- `target registration` means Data Safe knows about the database,
- `audit collection` means audit profiles and audit trails actually exist and are
  started,
- `audit events` only appear after collection is provisioned and the database is
  producing compatible audit records.

This distinction matters in the demo. A target can be `ACTIVE` and still produce
zero Data Safe audit rows if audit collection was never provisioned.

### Log Analytics

Log Analytics is the main ingestion and search layer:

- database alert, audit, listener, trace, and host log collection,
- Management Agent-backed ingestion for DBCS/Base DB,
- saved queries and dashboards,
- cross-source timeline correlation,
- scenario-based searches using `scenario_id` and `lab_id`.

In this project, Log Analytics is used for:

- collecting the DB-side and host-side evidence,
- correlating ORA, PLS, host, and synthetic markers,
- feeding the AI evidence bundle with bounded search results.

## AI Troubleshooting Layer

Two AI-facing surfaces are central in the demo:

1. `dbman-opsi db-incident`
2. `oci-coordinator-oke` agent workflows using the generated integration assets

The AI workflow should produce:

- summary,
- timeline,
- repetition and scope,
- cross-source evidence status,
- hypotheses with confidence,
- impact,
- next diagnostics,
- SR package guidance,
- uncertainty and missing-source explanation.

## Component Deep Dives

### Configuration And Secrets

The project intentionally separates public-safe code from private operator state.

Public repo:

- code,
- docs,
- generated public-safe templates,
- tests,
- sanitized dashboards and payload examples.

Private local state:

- ignored `*.local.yaml`,
- `.env.local`,
- OCI Vault secret references,
- SSH keys,
- DB connect strings,
- any tenant-specific topology.

The safe rule is simple: repo files describe the workflow; ignored files supply
the real tenant values.

### Management Agent Path

For DBCS/Base DB, Log Analytics collection depends on a Management Agent-backed
path. In this project the Management Agent flow is generated into the repo under
`generated/logan...`.

The Management Agent path is responsible for:

- installing the agent on the DB host or collector host,
- enabling the Log Analytics plugin,
- associating the right database and host log sources,
- making alert, audit, listener, trace, and syslog data searchable.

Without this layer, DBM and OPSI can still be enabled while Log Analytics remains
blind to the actual logs.

### DB Monitoring User

The monitoring user is often `DBSNMP` in the PoC path. That is acceptable for a
demo, but it creates coupling:

- DBM,
- OPSI,
- Data Safe service-account usage,
- and local DB consumers can all depend on the same account.

That is why this project now includes explicit recovery for `ORA-28000` lockouts.

The packet scripts:

- `12-check-monitoring-account-status.sql`
- `13-remediate-monitoring-account-lock.sql`

exist because a shared monitoring account can break multiple OCI surfaces at once.

### Data Safe Registration Versus Data Collection

This is the most common operator misunderstanding in the demo:

- `target database ACTIVE` does not mean audit collection is configured.

The minimum end-to-end Data Safe path is:

1. target registration,
2. service-account privileges,
3. audit profile creation,
4. audit trail discovery,
5. audit trail start,
6. database-side audit activity,
7. Data Safe audit-event visibility,
8. optional export into OCI Logging and Log Analytics.

If steps 3 to 5 never happen, Data Safe audit event APIs stay empty even when the
database already has real unified audit rows.

### DB Incident Demo Design

The DB incident demo is intentionally conservative:

- it creates safe, reproducible Oracle errors,
- it records them into a disposable schema,
- it can emit synthetic internal-error markers for correlation,
- it does not attempt to force Oracle internal corruption or real `ORA-00600`.

Real DB-side signals generated by the packet include:

- `ORA-00001`
- `ORA-00942`
- `ORA-01400`
- `ORA-02291`
- `ORA-00054`
- `PLS-00201`
- invalid-object execution diagnostics

Optional signals include:

- `DBINC_LAB` unified audit logon rows,
- synthetic ORA-00600 / ORA-07445-style markers,
- HR/CO sample-schema workload errors.

## Step-By-Step Lab

This lab assumes:

- a dedicated demo DB or disposable PDB,
- ignored local config and env files are already populated,
- OCI CLI access is valid,
- the operator understands this is not a production workflow.

### Phase 1: Prepare The Local Operator Environment

1. Create and activate the virtualenv.
2. Install the package with dev dependencies.
3. Copy `.env.local.example` to `.env.local`.
4. Populate ignored local variables only.
5. Run:

```bash
dbman-opsi doctor
dbman-opsi preflight --config <IGNORED_DEMO_CONFIG_PATH>
```

Expected outcome:

- CLI tools are present,
- OCI access works,
- target configuration is readable,
- missing prerequisites are identified before live changes.

### Phase 2: Validate The Four Pillars

1. Confirm the target opts into the right `services`.
2. Validate DBM and OPSI state:

```bash
dbman-opsi validate --config <IGNORED_DEMO_CONFIG_PATH>
```

3. Check Data Safe target inventory:

```bash
scripts/demo-datasafe-log-export.sh targets
```

4. Check Log Analytics and connector state:

```bash
scripts/demo-datasafe-log-export.sh status
```

Expected outcome:

- DBM and OPSI reachable,
- Data Safe target presence understood,
- Log Analytics bridge state visible,
- audit-profile / audit-trail gap visible if collection is not provisioned.

### Phase 3: Generate And Stage The DB Incident Packet

1. Generate the packet:

```bash
PYTHONPATH=src python -m dbman_opsi.cli generate-db-incident-demo \
  --output generated/db-incident-demo-e2e \
  --apply \
  --scenario-id <DEMO_SCENARIO_ID>
```

2. Validate the packet:

```bash
generated/db-incident-demo-e2e/validate-demo-packet.sh
```

3. Review:

- `RUNBOOK.md`
- `LOGAN-QUERIES.md`
- `MCP-HANDOFF.md`

Expected outcome:

- packet is internally consistent,
- lab scripts exist,
- handoff assets are ready.

### Phase 4: Prepare The DB Host Path

For DBCS/Base DB with host-local SYSDBA access:

1. Use OCI Bastion or the approved demo host path.
2. Connect to the DB host as the OS user that can become `oracle`.
3. Stage the generated packet under an `oracle`-owned directory.
4. Run the packet from that `oracle`-owned directory.

This matters because DB-host packet execution often uses:

- `sqlplus / as sysdba`
- local listener reachability,
- local OS ownership and file permissions

Expected outcome:

- `oracle` can read and execute the packet,
- the packet runs in the right container/PDB context.

### Phase 5: Run The Real Demo Workload

Set the required environment:

```bash
export DB_INCIDENT_ADMIN_CONNECT='<REVIEWED_ADMIN_CONNECT>'
export DB_INCIDENT_LAB_PASSWORD='<DISPOSABLE_PASSWORD>'
export DB_INCIDENT_PDB_NAME='<DEMO_PDB_NAME>'
export DB_INCIDENT_PDB_SERVICE='<DEMO_PDB_SERVICE>'
export DB_INCIDENT_LAB_EZCONNECT='//<DEMO_DB_HOST>:1521/<DEMO_PDB_SERVICE>'
export DB_INCIDENT_DATASAFE_AUDIT_ENABLED=true
export DB_INCIDENT_DATASAFE_AUDIT_FAILED_LOGIN_ENABLED=true
```

Then run:

```bash
./run-db-incident-demo.sh
```

Expected outcome:

- `DBINC_LAB` schema is created,
- safe real ORA/PLS errors are generated,
- `incident_event_log` is populated,
- unified audit rows for `DBINC_LAB` exist if the audit primer is enabled.

### Phase 6: Recover The Monitoring Account If Needed

If monitoring breaks or `ORA-28000` appears:

```bash
sqlplus -L -S /nolog
connect <ADMIN_CONNECT>
@12-check-monitoring-account-status.sql DBSNMP
@13-remediate-monitoring-account-lock.sql DBSNMP C##DBSNMP_MON
exit
```

Expected outcome:

- `DBSNMP` becomes `OPEN`,
- the account moves to the non-locking profile,
- DBM/OPSI/Data Safe consumers stop relocking it.

### Phase 7: Verify Log Analytics And AI Evidence

1. Query the scenario markers:

```bash
scripts/demo-db-incident-e2e.sh logan-scenario-check
```

2. Build the evidence bundle:

```bash
scripts/demo-db-incident-e2e.sh logan-check
```

3. Use the generated `oci-coordinator-oke` assets for agent-side drilldown.

Expected outcome:

- Log Analytics returns bounded incident-window results,
- the evidence bundle shows source coverage and missing sources clearly,
- the AI workflow produces a useful incident narrative.

### Phase 8: Provision Data Safe Audit Collection

If `status` shows:

- targets present,
- audit profiles `0`,
- audit trails `0`,

then Data Safe audit collection is not provisioned yet.

That state means:

- DB-side unified audit may be healthy,
- Data Safe target registration may be healthy,
- Data Safe audit-event APIs will still remain empty.

The operator must then provision:

1. audit profile,
2. audit trail discovery,
3. audit trail start,

for the active target.

Expected outcome:

- `scripts/demo-datasafe-log-export.sh status` shows non-zero audit profiles and
  audit trails,
- Data Safe audit events begin to appear after collection starts,
- `scripts/demo-datasafe-log-export.sh --apply sync` can push those events into
  OCI Logging and Log Analytics.

### Phase 9: Export Data Safe Audit Into Log Analytics

After audit collection is real:

```bash
scripts/demo-datasafe-log-export.sh --apply sync
scripts/demo-datasafe-log-export.sh status
```

Expected outcome:

- Data Safe audit rows are visible,
- the custom log bridge is populated,
- Log Analytics sees `dbman-opsi-datasafe-audit` rows,
- correlation queries can include security activity.

### Phase 10: Cleanup

When the demo is done:

```bash
sqlplus -L -S /nolog
connect <ADMIN_CONNECT>
@05-cleanup-lab-schema.sql <DEMO_PDB_NAME>
exit
```

Expected outcome:

- disposable demo schemas are removed,
- demo-only workload artifacts do not remain in the target database.

## Recommended Reading Order

For a new operator, the fastest path is:

1. [README.md](../README.md)
2. [docs/architecture.md](architecture.md)
3. [docs/demo-db-incident-e2e.md](demo-db-incident-e2e.md)
4. [docs/datasafe-log-analytics.md](datasafe-log-analytics.md)
5. [docs/demo-db-incident-e2e.md](demo-db-incident-e2e.md)

## Operator Checklist

- Use a dedicated demo DB or disposable PDB only.
- Keep all real tenant values in ignored files or environment variables.
- Treat target registration and audit collection as separate Data Safe milestones.
- Use `DBINC_LAB` for failed-login drills, never `DBSNMP`.
- Stage host-run packets into an `oracle`-owned path before execution.
- Prefer Management Agent-backed Log Analytics collection for DBCS/Base DB.
- Expect AI answers to explain missing-source state, not just observed errors.
