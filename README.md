# OCI DB Management And Operations Insights Enablement

[![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/adibirzu/oci-dbman-opsi/archive/refs/heads/main.zip)

`dbman-opsi` is an OCI enablement and lifecycle tool for four
observability/security pillars — **Database Management (DBM)**, **Operations
Insights (OPSI)**, **Data Safe**, and **Log Analytics** — across:

- Base Database Service / DBCS
- Autonomous Database
- OCI Exadata Database Service
- External databases and external Exadata through OCI Management Agents

Each target opts into the pillars it wants via `services` (`dbm`, `opsi`,
`datasafe`, `logan`; default `dbm`+`opsi`). The loader also accepts
`loganalytics` as an alias for `logan`. The tool runs from OCI Cloud Shell, a local
workstation, OCI Resource Manager, or any automation runner that has OCI CLI and
Terraform access. Every tenant-specific value is supplied through variables,
ignored local config files, OCI Vault, or environment variables.

It supports two operating paths: expert per-target enablement and a production
fleet lifecycle with read-only discovery, exact-plan approval, checkpointed
resume, signed DBA/host handoffs, protected state, collection-proof gates, and
ownership-safe offboarding. This is **not an official Oracle product** or an
Oracle-supported deployment tool. Production use still requires owner-approved
access and change control; current live acceptance evidence is owner-gated.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the system view, module map,
command lifecycle, the service-pillar detection model, DB incident
troubleshooting workflow, the read-live/redaction boundary, and the OPSI
validation verdict model (with Mermaid diagrams).

The versioned scope, ownership, dependencies, and release gates for the
disposable DBCS/ADB demonstration are in the
[product portfolio](docs/product/portfolio.md).

For installation, configuration, production options, fleet lifecycle commands,
state/evidence handling, and release gates, start with the
[production operations guide](docs/production-operations-guide.md). For the
full lifecycle contract and unclosed scratch-tenancy acceptance matrix, use the
[fleet lifecycle runbook](docs/fleet-lifecycle-runbook.md).

The repository Wiki mirrors the production guide as page-oriented operator
documentation. The in-repository wiki source is
[docs/wiki-oci-db-observability-lab.md](docs/wiki-oci-db-observability-lab.md).

## Workshop

Start with the workshop guide: [docs/workshop/README.md](docs/workshop/README.md).

The workshop covers discovery, prerequisite provisioning, DBCS and Exadata SQL
scripts, Autonomous Database validation, external database Management Agent
onboarding, Operations Insights payloads, cross-region monitoring, Process
Insights diagnostics, and final collection validation.

## Blog Entry

The public-safe PoC update is captured in
[docs/blog-opsi-poc-update.md](docs/blog-opsi-poc-update.md). It summarizes the
new multi-region Ops Insights showcase, Chicago provisioning path, advanced
diagnostics enablement, host firewall handoff, Process Insights diagnostics, and
the test/deployment checks run before publishing.

## Screenshots

These screenshots are captured from local public documentation and sanitized OCI
Console views only. They do not show a tenant selector, account name, OCIDs, IP
addresses, credentials, SQL IDs, or live SQL detail.

![README preview](docs/screenshots/readme.png)

![Workshop preview](docs/screenshots/workshop.png)

The demo runbook includes the full end-state gallery for Database Management,
Data Safe, Ops Insights capacity dashboards, SQL Insights, DB Performance, and
the Ops Insights multi-region Data Object Explorer flow:
[docs/demo-db-incident-e2e.md](docs/demo-db-incident-e2e.md#validation-status).

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

cp .env.local.example .env.local
chmod 600 .env.local
# edit .env.local locally; do not commit it

dbman-opsi doctor
dbman-opsi discover --profile <OCI_PROFILE> --region <OCI_REGION> --compartment <OCID>  # 3-pillar inventory
dbman-opsi plan --profile <OCI_PROFILE> --region <OCI_REGION> --output dbman-opsi.local.yaml
dbman-opsi init-region --config dbman-opsi.local.yaml --region us-chicago-1 --target-kind dbcs
dbman-opsi provision --config dbman-opsi.local.yaml --render-only
dbman-opsi prepare-prereqs --config dbman-opsi.local.yaml --dry-run
dbman-opsi generate-db-scripts --config dbman-opsi.local.yaml --output generated/db-scripts
dbman-opsi generate-opsi-payloads --config dbman-opsi.local.yaml --output generated/opsi-payloads
dbman-opsi generate-logan-payloads --config dbman-opsi.local.yaml --output generated/logan
dbman-opsi generate-opsi-diagnostics --config dbman-opsi.local.yaml --output generated/opsi-diagnostics
dbman-opsi db-exec --config dbman-opsi.local.yaml            # generate DB scripts + show hybrid run plan
dbman-opsi preflight --config dbman-opsi.local.yaml
dbman-opsi configure --config dbman-opsi.local.yaml          # plan: detect + gate, no changes (DBM+OPSI)
dbman-opsi enable --config dbman-opsi.local.yaml --dry-run
dbman-opsi data-safe --config dbman-opsi.local.yaml          # register Data Safe targets (datasafe pillar)
dbman-opsi configure --config dbman-opsi.local.yaml --apply --with-log-analytics --skip-credentials
dbman-opsi generate-db-incident-demo --output generated/db-incident-demo --apply
dbman-opsi db-incident --profile <PROFILE> --region <REGION> --compartment-id <COMPARTMENT_OCID> --ora-code ORA-00600 --database-name <DB_NAME> --json
dbman-opsi cross-region --config dbman-opsi.local.yaml --regions <HOME_REGION>,<SECOND_REGION>
dbman-opsi validate --config dbman-opsi.local.yaml
```

### DB Incident Demo

The DB incident demo is an observability showcase, not a production workload.
Run it only against a dedicated demo database or disposable PDB. The helper uses
colored shell output by default and all tenancy-specific values come from
variables or ignored local config.

```bash
export PROFILE='<OCI_PROFILE>'
export REGION='<OCI_REGION>'
export CONFIG='<IGNORED_DEMO_CONFIG_PATH>'
export SCENARIO_ID='<DEMO_SCENARIO_ID>'

scripts/demo-db-incident-e2e.sh tasks
scripts/demo-db-incident-e2e.sh prereq
scripts/demo-db-incident-e2e.sh generate
scripts/demo-db-incident-e2e.sh package

export DEMO_JUMPHOST_HOST='<DEMO_JUMPHOST_HOST_OR_IP>'
export DEMO_JUMPHOST_SSH_KEY='<PRIVATE_KEY_PATH>'
export DB_INCIDENT_ADMIN_CONNECT='<DEMO_ADMIN_CONNECT_STRING>'
export DB_INCIDENT_LAB_PASSWORD='<DISPOSABLE_PASSWORD>'
export DB_INCIDENT_PDB_NAME='<DEMO_PDB_NAME>'
export DB_INCIDENT_PDB_SERVICE='<DEMO_PDB_SERVICE>'
export DB_INCIDENT_LAB_EZCONNECT='//<DEMO_DB_HOST>:1521/<DEMO_PDB_SERVICE>'
export DB_INCIDENT_DATASAFE_AUDIT_ENABLED=true
export DB_INCIDENT_DATASAFE_AUDIT_FAILED_LOGIN_ENABLED=true
export DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED=true

scripts/demo-db-incident-e2e.sh jumphost-copy
scripts/demo-db-incident-e2e.sh jumphost-preflight
scripts/demo-db-incident-e2e.sh jumphost-run
scripts/demo-db-incident-e2e.sh logan-scenario-check
scripts/demo-db-incident-e2e.sh logan-check
INCLUDE_SOURCES=logan,dbm,opsi,audit,datasafe scripts/demo-db-incident-e2e.sh logan-check
```

Use deliberate failed-login drills only through the disposable `DBINC_LAB` path in the generated packet. Do not test bad passwords against `DBSNMP` or any other monitoring user; the packet includes DBA-only monitoring-account status and recovery SQL for `ORA-28000` lockouts.

The generated packet creates `DBINC_LAB`, raises safe real Oracle errors,
captures compilation diagnostics, optionally installs Oracle HR/CO sample
schemas, emits Log Analytics query templates and dashboard/playbook assets for
`oci-coordinator-oke`, can also create real demo-only unified-audit rows for
Data Safe export, and includes cleanup SQL. For the full workflow, see
[docs/demo-db-incident-e2e.md](docs/demo-db-incident-e2e.md) and
[docs/db-incident-troubleshooting.md](docs/db-incident-troubleshooting.md).
The broader operator wiki and step-by-step lab is in
[docs/wiki-oci-db-observability-lab.md](docs/wiki-oci-db-observability-lab.md).
For DB-host execution against a PDB-local demo schema, prefer `DB_INCIDENT_LAB_EZCONNECT`
instead of embedding a full quoted connect string in `DB_INCIDENT_LAB_CONNECT`.
The default `logan-check` source set is `logan,dbm,opsi,datasafe`; add
`audit` explicitly when you want OCI Audit correlation in the same query.

### Data Safe Audit Export Demo

For the demo-only Data Safe -> OCI Logging -> Log Analytics bridge, use:

```bash
scripts/demo-datasafe-log-export.sh prereq
scripts/demo-datasafe-log-export.sh plan
scripts/demo-datasafe-log-export.sh targets
scripts/demo-datasafe-log-export.sh --apply apply
scripts/demo-datasafe-log-export.sh --apply sync
scripts/demo-datasafe-log-export.sh status
scripts/demo-datasafe-log-export.sh dashboard
```

This is for showcase environments only. It creates or reuses OCI Logging and
Log Analytics objects, wires Service Connector Hub routes, seeds recent Data
Safe audit events into the custom log, and writes sanitized dashboard/query
assets. `targets` and `status` intentionally avoid printing raw tenant topology
details, so operators can validate the bridge without copying OCIDs or subnet
IDs into screenshots or notes. Details:
[docs/datasafe-log-analytics.md](docs/datasafe-log-analytics.md).

Quote `'.[dev]'` in zsh and other shells that expand square brackets. After
activating `.venv`, use `python -m pip` so pip installs into the active virtual
environment; the interpreter path is `.venv/bin/python` before activation.

`plan` is the guided discovery path. It automatically uses the tenancy OCID from
the selected OCI profile when available, lists active compartments, searches the selected
compartment first, then searches other accessible compartments for reusable
resources. It lets you select existing VCNs, subnets, Vault keys, Vault secrets,
Database Management private endpoints, Ops Insights private endpoints, Data Safe
private endpoints, and database targets. If VCNs already exist, the network
prompt defaults to reusing one instead of creating a PoC network. The wizard also
discovers IAM policies, reports whether the DBM/OPSI service-principal
statements are present, and reuses a discovered policy group name for generated
policy documents. For DBCS and Exadata, select the actual database/CDB target,
not the parent DB system OCID; the wizard tracks the DB system separately when
Data Safe needs it and can add PDB targets in the PDB discovery step. If
discovery cannot read a resource type, the wizard falls back to manual OCID
entry.

`configure` is the orchestrated path: it detects whether each database exists and is
already enabled, branches by location (OCI-native direct vs external Management Agent),
runs the full prerequisite gate (IAM policies, Service Gateway + route, private
endpoints, Vault secret, DB monitoring user), then either enables (`--apply`) or emits a
DB-side handoff packet (`--db-side-only`) for a DBA to run the database steps separately.

Container and pluggable databases are handled distinctly. A target's `database_role`
(`CDB`, `PDB`, or `NON_CDB`) selects the correct OCI verb — CDB/non-CDB use
`db database enable-database-management`; PDBs use
`db pluggable-database enable-pluggable-database-management`. PDB targets carry a
`parent_cdb_id`; `configure` enables the container database first and blocks a PDB
until its parent CDB has Database Management enabled.

Use `--apply` only after reviewing dry-run output.

## Cloud Shell

Cloud Shell already includes OCI CLI. Install the package and verify prerequisites:

```bash
python3 -m pip install -e '.[dev]'
dbman-opsi doctor
```

Then run the workshop with `--profile DEFAULT` and your selected region.

### Customer-tenancy OPSI diagnostics

Use this flow when a customer says DBCS has the OCI-side prerequisites enabled
but Operations Insights still fails and the Console/work-request detail is not
enough. The goal is to capture the whole read-only evidence chain: IAM policy
visibility, DBM/OPSI private endpoint state, subnet route/security controls,
Vault secret state, OPSI failed insight/work-request details, and the DB-side
service/user checks.

Cloud Shell uses the signed-in user's OCI CLI session/profile. From Cloud Shell:

```bash
git clone https://github.com/adibirzu/oci-dbman-opsi.git
cd oci-dbman-opsi
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

dbman-opsi doctor --profile DEFAULT --region <region>
dbman-opsi discover --profile DEFAULT --region <region> --compartment <customer_compartment_ocid> --subtree
dbman-opsi plan --profile DEFAULT --region <region> --output dbman-opsi.customer.local.yaml
dbman-opsi preflight --config dbman-opsi.customer.local.yaml --json > preflight.json
dbman-opsi generate-opsi-diagnostics --config dbman-opsi.customer.local.yaml --output generated/opsi-diagnostics
```

For a customer-tenancy diagnostic host that must use instance principals, run the
same commands after exporting the auth mode. The instance must be in a dynamic
group with enough read visibility for the affected compartments:

```bash
export DBMAN_OPSI_OCI_AUTH=instance_principal
export OCI_CLI_REGION=<region>

dbman-opsi doctor --profile DEFAULT --region <region>
dbman-opsi discover --profile DEFAULT --region <region> --compartment <customer_compartment_ocid> --subtree
dbman-opsi plan --profile DEFAULT --region <region> --output dbman-opsi.customer.local.yaml
dbman-opsi preflight --config dbman-opsi.customer.local.yaml --json > preflight.json
dbman-opsi generate-opsi-diagnostics --config dbman-opsi.customer.local.yaml --output generated/opsi-diagnostics
```

Minimum diagnostic IAM for the operator principal is read-only. A typical
customer policy for the diagnostic dynamic group is:

```text
Allow dynamic-group <diagnostic_dynamic_group> to inspect compartments in tenancy
Allow dynamic-group <diagnostic_dynamic_group> to read all-resources in compartment <target_compartment>
```

If the database, Vault, private endpoints, VCN/subnet, or IAM policies live in
different compartments, grant equivalent read visibility there too. For
production tenants, prefer narrower service families after the first evidence
capture; the broad read policy is meant to remove blind spots during triage.

Run each generated target packet:

```bash
cd generated/opsi-diagnostics/<target-name>

# Cloud Shell / profile auth:
./00-oci-control-plane-diagnostics.sh ./out

# Instance-principal auth:
DBMAN_OPSI_OCI_AUTH=instance_principal ./00-oci-control-plane-diagnostics.sh ./out
```

The OCI-side script is read-only. When `jq` is available it expands the private
network path by reading the subnet route table, subnet security lists, and DBM /
OPSI private endpoint NSGs. It also pulls OPSI work-request detail records for
failed or database-insight work requests. The private endpoint is a
service-managed resource, so you cannot SSH "from" it; use these control-plane
objects plus `managed-database.json` (`database-status` should be `UP`) to prove
whether DBM/OPSI can reach the database over the private endpoint path.

Run the DB-side checks with the DBA:

```sql
sqlplus / as sysdba
spool opsi-db-readiness.log
@01-diagnose-opsi-db-readiness.sql
spool off
exit

sqlplus /nolog
spool opsi-login-probe.log
@02-test-opsi-login.sql
spool off
exit
```

The second script must use the same monitoring user, service name, and Vault
password that DBM/OPSI use. It catches the common hidden causes: stale Vault
password, locked/expired monitoring account, missing dictionary grants, and a
PDB service name that does not route to the expected container.

Send the customer/OCI owner these files from the packet:

- `preflight.json`
- `out/database-resource.json`
- `out/managed-database.json`
- `out/dbm-private-endpoint.json`
- `out/opsi-private-endpoint.json`
- `out/vault-secret.json`
- `out/iam-policies.json`
- `out/subnet.json`, `out/subnet-route-table.json`, `out/subnet-security-list-*.json`
- `out/*-nsg-*.json`
- `out/opsi-insights-FAILED.json`, `out/opsi-work-requests.json`, `out/opsi-work-request-*-detail.json`
- `opsi-db-readiness.log`
- `opsi-login-probe.log`

Policy symptoms to look for:

- `iam-policies.json` does not mention `service dpd`: Database Management may
  not be allowed to read/use the Vault secret or related key.
- `iam-policies.json` does not mention `service operations-insights`: Operations
  Insights may not be allowed to read/use the Vault secret or private endpoint
  path.
- The diagnostic principal cannot read network, Vault, Database Management, or
  OPSI objects: add read visibility for the diagnostic dynamic group/user before
  concluding the resource is missing.

After fixing the failing signal, rerun:

```bash
dbman-opsi preflight --config dbman-opsi.customer.local.yaml --db-check-file generated/opsi-diagnostics/<target-name>/opsi-db-readiness.log
dbman-opsi enable --config dbman-opsi.customer.local.yaml --apply --force-reconcile
dbman-opsi validate --config dbman-opsi.customer.local.yaml
```

## Resource Manager

The Deploy to Oracle Cloud button launches the Terraform stack under `terraform/examples/zero-start-poc`. Resource Manager provisions OCI-side prerequisites such as IAM, workshop networking, and service private endpoints. Database credentials and database-side scripts are handled by the CLI workflow so secrets are not placed in Terraform variables.

## Commands

- `doctor`: check Python, OCI CLI, and Terraform availability. Pass `--profile`/`--region` to also confirm the OCI session is authenticated (not just installed).
- `discover`: read-only inventory of reusable resources (subnets, vaults, databases, endpoints, agents, bastions). Reports the DBM/OPSI/Data Safe status per database — `dbm_status`, `opsi_status`, `data_safe_status`, plus `enabled_services`/`missing_services` — so you can see at a glance what is on. `--json` for automation (OCIDs redacted in JSON), `--subtree` to scan a compartment tree.
- `plan`: discover tenancy/profile context, active compartments, IAM policies, networks, databases, Vaults, Vault secrets, private endpoints, and agents across accessible compartments, then write a config. Prompts per target for which pillars to enable (`dbm`/`opsi`/`datasafe`) and credentials. For DBCS/Exadata it selects database/CDB resources, keeps the parent DB system separately for Data Safe, and can discover pluggable databases (PDBs) as linked child targets.
- `init-region`: create a region-specific provisioning config for a second-region PoC. Defaults to `us-chicago-1` and a provisioned DBCS target; pass `--target-kind autonomous` for Autonomous Database, or `--vcn-id` + `--subnet-id` to reuse an existing regional network instead of creating a test VCN.
- `provision`: render Terraform variables and optionally run Terraform.
- `import-tf-outputs`: read `terraform output` and merge the created OCIDs (subnet, VCN, Database Management private endpoint, provisioned database IDs) back into the config so `enable`/`configure` pick them up without manual copy.
- `prepare-prereqs`: create service-side private endpoints and optional Vault secrets from an environment variable.
- `generate-db-scripts`: create database-side SQL scripts for DBCS, Exadata, and external database targets. Each target packet includes `00-check-host-firewall.sh`, which checks the DB server OS firewall and prints/applies `firewall-cmd` or `iptables` rules for Oracle listener ports.
- `generate-agent-scripts`: create Management Agent bootstrap/install-key/package-URL-resolver/verify/resolve scripts plus a generated Ansible bootstrap/playbook/run bundle for external targets and for `logan`-enabled DBCS/Exadata targets that need a host-side Log Analytics collector path.
- `generate-opsi-payloads`: create Operations Insights JSON payload templates.
- `generate-logan-payloads`: create Log Analytics source/entity association payloads, host facts and ACL scripts, least-privilege DB user SQL, Management Agent install-key/install/package-URL-resolver/verify/resolve scripts plus a generated Ansible bootstrap/playbook/run bundle for DBCS/Base DB collectors, and ADB TCPS credential templates under an ignored output directory. Templates use environment placeholders; wallets, passwords, install keys, and credential JSON are not committed.
- `generate-opsi-diagnostics`: create a per-target read-only diagnostic packet for failed DBCS/Exadata OPSI enablement. It includes an OCI CLI shell script for DBM/OPSI private endpoints, Vault secret state, IAM service-principal policy text, failed OPSI insight/work-request evidence, and SQL scripts that verify DB service routing, monitoring-user grants, and the actual monitoring login using the Vault password.
- `cross-region`: configure and summarize the Ops Insights multi-region POC selection. It writes `monitoring_regions` when `--regions` is supplied, groups OPSI targets by their configured region, and prints the Console checklist for Data Object Explorer plus the Configuration and Capacity dashboards.
- `preflight`: read-only check of every prerequisite (IAM, Service Gateway, route, subnet security rules, host firewall handoff, private endpoints, Vault secret, monitoring user, Management Agent). Supports `--json` and `--db-check-file` (spooled `04-validate-monitoring-user.sql` output) to verify the DB monitoring user instead of leaving it manual.
- `configure`: orchestrated detect → branch-by-location → gate → act flow. `--apply` enables DBM/OPSI and then sets the advanced-diagnostics/administration preferred credentials via a Vault named credential; `--skip-credentials` opts out. `--db-side-only` emits DBA handoff packets, `--force` overrides blockers, `--json` supports automation. Add `--with-data-safe` to also register Data Safe targets for `datasafe`-opted targets; add `--with-log-analytics` to configure Log Analytics payloads/source associations for `logan`-opted targets.
- `enable`: run OCI Database Management and Operations Insights enablement. Idempotent and self-healing — re-runs tolerate an already-enabled DBM (409) and **reconcile** the connection (so a corrected service name or rotated credential takes effect), skip already-ACTIVE OPSI insights, and (in `--apply`) set the advanced-diagnostics preferred credentials. Use `--skip-credentials` to opt out of the last step.
- `set-credentials`: set the DBM advanced-diagnostics preferred credentials (`PC_READ`/`PC_WRITE`) via a Vault-backed named credential, so on-demand tasks (Performance Hub, AWR, ADDM, SQL Tuning) work. Idempotent; retries the flaky `dbmgmt` control plane and reports blocked targets with remediation.
- `data-safe`: register databases as **Data Safe** target databases for targets that opt into the `datasafe` pillar. Creates a Data Safe private endpoint in the DB subnet if needed, prompts for the service-account credentials (DBSNMP default; `--password-env` for non-interactive), registers the target, and persists the target OCID back into the config. Dry-run by default; `--apply` performs live registration.
- `log-analytics`: resolve/onboard the Log Analytics namespace, create/reuse the configured log group, generate sanitized source association payloads, and upsert source/entity associations for targets that opt into `logan`. The command now uses OCI's canonical source names and the current `log-analytics assoc upsert-assocs` API shape. For DBCS/Base DB it blocks early unless a Management Agent-backed Log Analytics path is available, instead of creating detached entities that OCI will reject. Dry-run by default; `--apply` performs live OCI changes. ADB collection expects a private collector host, TCPS wallet, and local `DBTCPSCreds` registration outside Terraform state.
- `generate-db-incident-demo`: create a dry-run or `--apply` SQL*Plus lab packet for DB incident troubleshooting. The executable packet creates a disposable `DBINC_LAB` schema, raises safe real Oracle errors, compiles an intentionally invalid PL/SQL object, captures `SHOW ERRORS`/`USER_ERRORS`-style diagnostics, stores evidence rows in `incident_event_log` with module/action/client identifier context, queries them back, and optionally writes reviewed synthetic ORA-00600/ORA-07445 marker lines to the alert log through SYSDBA. It can also install Oracle's official HR/CO sample schemas from `oracle-samples/db-sample-schemas` when `DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED=true`, then generate additional sample-app errors for observability demos. The generated packet includes `RUNBOOK.md` for operator handoff, `manifest.json` for a machine-readable packet index, `LOGAN-QUERIES.md` for scenario-scoped Log Analytics searches, `08-local-demo-tooling-preflight.sh` for Java/OCI CLI/SQLcl/MCP readiness, `validate-demo-packet.sh` for local non-destructive preflight, colored shell status output, and SQL*Plus evidence timeline/repetition/source summaries; set `NO_COLOR=1` for plain shell output. This is not for production use.
- `db-incident`: build a bounded, redacted evidence bundle for ORA/alert-log troubleshooting across Log Analytics, DBM, OPSI, OCI Audit, and Data Safe when available. The answer includes timeline, repetition/scope, cross-source status, hypotheses, impact, next diagnostics, SR package, and uncertainty.

For the demo-tenancy end-to-end DB incident demo, including jumphost/Bastion prerequisites, real DB workload execution, Management Agent Log Analytics ingestion, LoganAI prompts, and `oci-coordinator-oke` agent workflow, see `docs/demo-db-incident-e2e.md` and run `scripts/demo-db-incident-e2e.sh tasks`.
- `db-exec`: regenerate the DB-side SQL scripts and show the **hybrid run plan** — auto-run via Bastion in non-production tenancies, generate-and-handoff for production profiles. `DBMAN_OPSI_PROD_PROFILES` can hold a comma-separated list of local OCI profile names that must never auto-run DB-side SQL. `--force` treats the run as non-production. `--apply` (with `--bastion-id`/`--target-ip`/`--ssh-key`, and `--answers-file` for accept-prompt answers) auto-runs the scripts on the DB node through a Bastion port-forward session.
- `validate`: check service state and collection readiness. Reports the real OPSI Database Insight lifecycle (`ACTIVE`/`FAILED`/`NOT_FOUND`/`UNKNOWN`) per target rather than a generic message — using a reliable GET-by-OCID and a verdict model that never emits a false `NOT_FOUND` from the flaky list.
- `process-insights`: diagnose Ops Insights **Process Insights** collection. It compares host resource summaries with top-process summaries and reports whether any MACS cloud host or Management Agent entity is importable, which separates database/host resource visibility from per-process collection.
- `journal`: inspect a run's command ledger. Every invocation records one **redacted** JSON line per OCI/Terraform command to `runs/<run_id>.jsonl`; `dbman-opsi journal [RUN_ID] [--last] [--json]` reads it back as a summary (command count, total duration, failing commands). `--last` resolves the newest run.

## Observability, resilience & safety

- **Redacted run-journal** at the single command choke point (`runner.run`) — auditable history of every OCI/Terraform call, with OCIDs/secrets stripped (read it back with `journal`). A global `--verbose` surfaces per-call timing.
- **Typed errors + retry/backoff** — failures are classified (`OciAuthError`/`OciNotFound`/`OciThrottled`/`OciTransient`); throttles always retry, transient errors retry for reads, auth/not-found never retry. Bounded exponential backoff.
- **Boundary validation** — config is validated at load **and** after merging Terraform outputs; malformed kinds/services/OCIDs are rejected before any OCI call (`ConfigError`).
- **Cross-region OPSI showcase** — set top-level `monitoring_regions` and optional per-target `region` for resources outside the home region. `validate` reads each target in its own region, while `cross-region` shows the exact region selector set to use in Ops Insights Data Object Explorer and the supported dashboards.
- **Process Insights diagnostics** — `process-insights --config <config>` is read-only and detects the common PE co-managed host state where host CPU/memory/storage/network summaries exist but top-process rows are empty. Process rows require a MACS cloud-host or Management Agent-backed host insight collector; do not fabricate process data with manual ingestion for a PoC.
- **DB server firewall handoff** — generated target packets include `00-check-host-firewall.sh`. It checks `firewalld` or `iptables`, prints the commands to allow TCP `1521`/`1522`, and applies them only with `--apply`. Set `DBMAN_OPSI_SOURCE_CIDR` to the DBM/OPSI/Data Safe private endpoint subnet or another approved monitoring source CIDR before applying. Override `DBMAN_OPSI_DB_PORTS` for TCPS/custom listener ports.
- **Secrets never committed** — OCIDs/IPs/namespaces are redacted at the display boundary, secret-bearing files are gitignored, and a `scripts/pre-push` hook + CI (gitleaks + bandit + `pip-audit`) gate every change.

## End-to-end enablement, Terraform & troubleshooting

- **Reproducible runbook:** [docs/demo-db-incident-e2e.md](docs/demo-db-incident-e2e.md)
  is the canonical dedicated-demo flow: doctor/preflight, packet generation,
  checksum-verified SQLcl, secure jumphost execution, service validation, and
  final redacted evidence handover.
- **Troubleshooting KB:** [KB.md](KB.md) maps live-tenancy failure signatures to
  root cause + fix (OPSI insight 80% failure, DBM idempotency, DBSNMP lock loop,
  DBM stale-service reconcile, validate blindness, the OCID-redaction-in-data-path
  bug, Data Safe `NEEDS_ATTENTION`/DBSNMP rotation, and the zero-start Terraform
  apply-time failures). On any error, the CLI also prints a *Solution* + *Manual
  step* from the same remediation map.
- **Eval-first regression suite:** [tests/evals/README.md](tests/evals/README.md)
  organizes capability and regression evals by defect signature so each fixed
  defect (e.g. the OPSI list flap, the `validate --dry-run` stub) stays fixed.
- **Declarative / ORM path:** [terraform/modules/dbm-opsi-enablement](terraform/modules/dbm-opsi-enablement)
  is a feature-toggled, `for_each`-driven module (DBM features management, named
  credential, OPSI insight, plus a CLI step for preferred credentials). Pure
  Terraform for teams that prefer Resource Manager over the CLI. `terraform
  validate` passes; apply-test in a scratch tenancy before production.

## Security

Generated local configs contain OCID references needed for automation, but they are ignored by Git. Plaintext database credentials must never be written to config, Terraform variables, screenshots, or documentation. Use OCI Vault and environment variables.

For local paid provisioning, define secrets and sensitive Terraform inputs in
`.env.local` from [.env.local.example](.env.local.example). The CLI loads this
file automatically when present and does not override variables already exported
by CI or Cloud Shell. Users are responsible for maintaining and securing their
own `.env.local` file (`chmod 600` recommended); it is gitignored and must never
be copied into public app code, docs, screenshots, or Terraform variable files.

See [docs/security.md](docs/security.md) before publishing screenshots or pushing a public repository.
Install the pre-push audit by chaining `scripts/pre-push` from the existing hook setup or by using pre-commit; do not point `core.hooksPath` at `scripts`, because that can disable ECC-managed hooks.
