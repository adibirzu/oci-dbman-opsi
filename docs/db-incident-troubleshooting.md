# DB Incident Troubleshooting

Owning product requirement: [SQLcl MCP Integration](product/prd-sqlcl-mcp-integration.md).

Use the `db_incident_analysis` flow when a question mentions an ORA error, alert log, or database incident. Log Analytics is the fast ingestion, search, and correlation layer; AI reasoning should explain context, uncertainty, missing sources, and next diagnostics rather than treating a matching error string as root cause.

## Standard Answer

Include:

- Summary: what happened and when.
- Timeline: alert-log, app, host, OCI Audit, VCN/network, DBM, OPSI, and Data Safe signals in the same window.
- Repetition and scope: isolated versus repeated, affected DB/service/host/app.
- Cross-source evidence: what each source showed and whether the source was unavailable.
- Hypotheses: confidence-ranked and evidence-backed.
- Impact: known or unknown user/workload effect.
- Next diagnostics: exact evidence to collect.
- SR package: trace, alert-log, version, patch, workload, and impact details.
- Uncertainty: what cannot be concluded from the available evidence.

## ORA Templates

### ORA-00060

Treat as a deadlock symptom. Collect the deadlock graph, blocker/waiter sessions, SQL IDs, application transaction names, and recent deploy/config changes. Correlate app retries and user-facing errors around the same minute.

### ORA-04031

Treat as shared pool or memory pressure until proven otherwise. Correlate workload shifts, hard-parse spikes, SGA/shared pool settings, memory advisories, invalidation storms, and recent parameter changes.

### ORA-01017

Treat as authentication failure or credential drift. Correlate wallet/secret rotation, account lock status, password profile changes, service-name changes, app deploys, and Data Safe or audit login failures.

### ORA-00054

Treat as a resource busy or NOWAIT locking conflict. Collect blocker session, locked object, SQL text, module/action, and whether the error came from expected deployment/maintenance automation.

### ORA-00942

Treat as missing table/view, wrong schema qualification, missing privilege, synonym drift, or edition/deploy ordering. Collect current schema, SQL text, object owner, `ALL_OBJECTS`/`DBA_OBJECTS` status, grants, synonyms, and recent DDL/deploy changes.

### ORA-04063, ORA-06550, ORA-06575, and PLS-* compiler diagnostics

Treat as invalid or newly compiled PL/SQL until proven otherwise. Reproduce with SQL*Plus or SQLcl and run `SHOW ERRORS` immediately after compilation. Query `USER_ERRORS` or `DBA_ERRORS` for owner, object name, type, line, position, text, and sequence; then correlate with `USER_OBJECTS`/`DBA_OBJECTS`, dependency status, deploy timestamp, caller module/action, and application error logs.

### ORA-00600 and ORA-07445

Treat as internal error signatures, not definitive root causes. Do not claim root cause from the code alone. Collect incident trace files, alert-log excerpts, exact database version, RU/RUR patch level, SQL/workload context, reproducibility, and impact. Open an Oracle SR when the event repeats, causes service impact, or produces incident trace packages.

## Demo Safety And Live Workload

The demo generator creates dry-run artifacts by default. `--apply` renders executable SQL*Plus scripts and a runner, but still does not execute anything during generation.

The generated shell runners use colored step/status output for demos. Set `NO_COLOR=1` to disable ANSI colors in CI logs or terminals that should stay plain text. The SQL*Plus scripts also print section headers and summary queries for the evidence timeline, ORA-code repetition, and source coverage, so a live run is readable while presenting the observability workflow.

Each generated packet includes `RUNBOOK.md`, a single operator handoff with environment variables, execution steps, correlation command, discussion prompts, and cleanup commands. It also includes `manifest.json` as a machine-readable packet index, `LOGAN-QUERIES.md` with scenario-scoped Log Analytics searches, plus `validate-demo-packet.sh`, a local non-destructive preflight that checks required files, executable bits, generated shell syntax, and local tool availability before a live database run. Packet-local SQLcl installation requires either a reviewed local archive or reviewed HTTPS URL and an exact SHA-256; unverified moving downloads are refused.

The executable workload creates a disposable `DBINC_LAB` schema, creates parent/child demo tables, intentionally raises safe real Oracle errors, compiles an intentionally invalid PL/SQL procedure, queries `USER_ERRORS`, and stores durable evidence rows in `DBINC_LAB.incident_event_log`. The intended real errors include constraint, missing-object, lock-conflict, and invalid-object classes such as `ORA-00001`, `ORA-00942`, `ORA-01400`, `ORA-02291`, `ORA-00054`, `ORA-04063`, `ORA-06550`, and `ORA-06575`, plus PLS compiler diagnostics. Evidence rows also include module, action, client identifier, and session user fields so the live run can be correlated with audit records, ASH/AWR-style diagnostics, app logs, and Log Analytics.

The packet also includes `08-local-demo-tooling-preflight.sh`, which checks Java, OCI CLI, SQLcl, and an operator-provided `DB_INCIDENT_MCP_COMMAND` for the reviewed Jeff Smith/SQLcl MCP server command used by the local MCP host. If SQLcl is not already installed, the operator can set `DB_INCIDENT_TOOLING_INSTALL=true` to download SQLcl into the packet-local `.tools` directory; `DB_INCIDENT_SQLCL_URL` can override the download URL for a mirrored or pre-approved artifact.

For DBA or MCP-agent investigation, `09-db-troubleshooting-queries.sql` provides read-only queries for invalid objects, `ALL_ERRORS`, dependencies, relevant grants, matching `incident_event_log` rows, and current session/blocking context. `MCP-HANDOFF.md` gives local-agent prompts and the expected SQL evidence flow.

The generated packet also includes `oci-coordinator-oke-integration/`, a demo bridge for the sibling coordinator app. It contains prebuilt Log Analytics dashboard JSON, saved-search style query JSON files, a DB incident playbook, and agent drilldown mappings that point to DB Troubleshoot, Log Analytics, Infrastructure, Security, and FinOps-style investigations. These assets let the dashboard show errors, source coverage, synthetic markers, compilation diagnostics, and links/prompts that can be run through the AI agents.

When `DB_INCIDENT_DATASAFE_AUDIT_ENABLED=true`, the generated runner also creates
a demo-only unified-audit policy for `DBINC_LAB` logon activity, triggers
reviewed successful and failed login events, and verifies the resulting local
`UNIFIED_AUDIT_TRAIL` rows. This is the recommended way to produce real Data
Safe audit records for the demo before exporting them into OCI Logging and Log
Analytics.

Keep failed-login drills scoped to `DBINC_LAB`. Do not test bad passwords
against `DBSNMP` or other monitoring accounts in the live demo. The generated
packet includes `12-check-monitoring-account-status.sql` and
`13-remediate-monitoring-account-lock.sql` so a DBA can inspect and recover an
`ORA-28000` monitoring-account lock without improvising commands during the demo.

The optional SYSDBA script writes reviewed marker lines to the database alert log with `DBMS_SYSTEM.KSDWRT` so Log Analytics can ingest alert-log context. These marker lines are real alert-log records but are clearly labeled synthetic for internal-error correlation. The scripts do not attempt to force `ORA-00600` or `ORA-07445`.

When `DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED=true`, the generated sample-schema installer downloads Oracle's official `oracle-samples/db-sample-schemas` source archive at runtime and installs the HR and CO schemas in the demo database. Oracle describes these schemas as free sample schemas used by Oracle Database documentation and examples; HR is a small Human Resources schema and CO is a Customer Orders schema. The generated workload then creates safe, real constraint errors against HR and CO and records the evidence in `DBINC_LAB.incident_event_log`.

Synthetic ORA-00600/ORA-07445-style JSONL records are marked with `synthetic=true`, `scenario_id`, and `lab_id`. Log upload remains disabled unless `DB_INCIDENT_LOG_UPLOAD_ENABLED=true`.

## Demo-Tenancy End-To-End Workflow

For the full replicable demo task list, jumphost/Bastion execution path, Management Agent Log Analytics setup, and LoganAI/coordinator workflow, see `docs/demo-db-incident-e2e.md`.

Use the local checkout when testing new commands so an older installed package is not selected:

```bash
PYTHONPATH=src python -m dbman_opsi.cli db-incident \
  --profile '<OCI_PROFILE>' \
  --region '<OCI_REGION>' \
  --compartment-id <DEMO_DATABASE_COMPARTMENT_OCID> \
  --ora-code ORA-00600 \
  --database-name '<DEMO_DATABASE_NAME>' \
  --include-sources logan,dbm,opsi,datasafe \
  --hours-back 24 \
  --limit 20 \
  --json
```

Use `scripts/demo-db-incident-e2e.sh prereq` to validate the current demo tenancy without publishing environment details. A passing prereq run should confirm:

- Log Analytics namespace and the demo log group are reachable.
- Log Analytics search works with search-term OCL plus `--time-start` / `--time-end`; do not embed timestamp comparisons inside OCL.
- DB Management returns managed database context.
- OPSI returns active database insight context.
- Data Safe API is reachable when enabled for the compartment.
- OCI Audit listing can be slow in broad compartments; keep Audit windows narrow or run it as a separate drilldown.
- Direct on-demand Log Analytics upload can be blocked by local OCI CLI/Python SDK upload media handling. DB alert/audit/host ingestion should therefore use the Management Agent source association path for the live demo, or use Cloud Shell/a newer OCI CLI for on-demand upload validation.
- The repo now uses OCI-canonical Log Analytics source names plus the current `log-analytics assoc upsert-assocs` payload shape. For DBCS/Base DB it will block early if a Management Agent-backed Log Analytics path is missing, rather than creating detached entities that the service rejects.

To generate real database troubleshooting evidence, run the packet only against the dedicated demo DB or disposable PDB:

```bash
PYTHONPATH=src python -m dbman_opsi.cli generate-db-incident-demo \
  --output generated/db-incident-demo \
  --apply \
  --scenario-id '<DEMO_SCENARIO_ID>'

export DB_INCIDENT_ADMIN_CONNECT='<DEMO_ADMIN_CONNECT_STRING>'
export DB_INCIDENT_LAB_PASSWORD='<DISPOSABLE_LAB_PASSWORD>'
export DB_INCIDENT_PDB_NAME='<DEMO_PDB_NAME>'
export DB_INCIDENT_PDB_SERVICE='<DEMO_PDB_SERVICE>'
export DB_INCIDENT_LAB_EZCONNECT='//<DEMO_DB_HOST>:1521/<DEMO_PDB_SERVICE>'
export DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED=true
export DB_INCIDENT_DATASAFE_AUDIT_ENABLED=true
generated/db-incident-demo/run-db-incident-demo.sh
```

After the workload runs, verify DB-side evidence with:

```bash
sql "$DB_INCIDENT_LAB_CONNECT" @generated/db-incident-demo/03-query-evidence.sql
sql "$DB_INCIDENT_LAB_CONNECT" @generated/db-incident-demo/09-db-troubleshooting-queries.sql
```

If the demo runs on the DB host as the `oracle` OS user, set `DB_INCIDENT_LAB_EZCONNECT`
and let the generated runner quote the disposable password automatically.

Log Analytics should collect real DB alert/audit/listener/host logs through Management Agent associations for the demo DB. Use generated `LOGAN-QUERIES.md` and `oci-coordinator-oke-integration/queries/*.json` as LoganAI saved-search starting points. The tested OCL style is:

```text
'ORA-00600' '<DEMO_DATABASE_NAME>' | sort -Time | head 20
```

The DB-side demo workload and OCI-side evidence bundle can complete before the fresh
alert-log markers appear in Log Analytics. If `logan-scenario-check` returns zero rows,
recheck Management Agent source associations and retry after the next ingestion interval.

If `dbman-opsi log-analytics --apply` reports a blocked target with missing database/host entity binding, treat that as an environment prerequisite gap:

1. generate the Log Analytics packet and run `03-create-logan-management-agent-install-key.sh`;
2. install the Management Agent on the DB host or collector host with `04-install-logan-management-agent.sh`, optionally resolving `AGENT_RPM_URL` first with `11-resolve-logan-management-agent-package-url.sh`, or drive the same install from the operator machine with `07-bootstrap-logan-management-agent-ansible.sh` plus `08-run-logan-management-agent-ansible.sh`;
3. verify the agent on-host with `05-verify-logan-management-agent.sh`;
4. resolve and store the agent OCID with `06-resolve-logan-management-agent.sh`;
5. re-run `log-analytics --apply`.

Pass the same question to `oci-coordinator-oke` through `/chat`, for example:

```text
What happened around ORA-00600 on <DEMO_DATABASE_NAME> in the last 24 hours?
Correlate Log Analytics, DBM, OPSI, OCI Audit, and Data Safe. Show missing source status.
```

The coordinator DB Troubleshoot Agent should call `oci_logan_build_db_incident_evidence` first, then use DBM waits/top SQL, OPSI database insight/capacity context, Data Safe target/activity status, and Audit changes as drilldowns. The final answer must distinguish direct ORA evidence from service inventory context.

## Demo Segregation

This path is not for production use. It exists only to showcase OCI Observability product capabilities.

Keep demo-deployed application databases and existing PoC databases as separate config targets. Demo targets can opt into the full showcase service set (`dbm`, `opsi`, `datasafe`, `logan`) and can use the disposable `DBINC_LAB`, `HR`, and `CO` users. Existing PoC targets should keep their current service list and must not inherit demo users, generated incident workload, or sample-schema installation.

## Test Coverage

The generated demo packet is tested end to end at the artifact level: tests generate the `--apply` packet, assert the SQL*Plus runner does not place connect strings on argv, verify the colored shell helpers and demo status sections are present, verify SQL*Plus evidence section headers are emitted, syntax-check generated shell scripts with `sh -n`, run the generated packet validator, and verify HR/CO sample-schema workload, DBA/MCP troubleshooting queries, Log Analytics query templates, runbook, manifest, and segregation files are emitted.

## Reference Basis

- Oracle Database Error Help is the canonical starting point for error text and suggested action.
- PL/SQL compilation troubleshooting uses immediate `SHOW ERRORS` output plus persistent `USER_ERRORS`/`DBA_ERRORS` rows for owner, object, type, line, position, sequence, and text.
- Community troubleshooting pages such as OracleScripts and OracleDayByDay are useful script pattern sources, but the generated packet keeps live actions demo-only, explicit, and reversible.
