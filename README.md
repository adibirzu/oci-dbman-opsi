# OCI DB Management And Operations Insights Enablement

[![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/example-org/dbman-opsi/archive/refs/heads/main.zip)

`dbman-opsi` is a public-repo-ready workshop toolkit for enabling OCI Database Management and OCI Operations Insights across:

- Base Database Service / DBCS
- Autonomous Database
- OCI Exadata Database Service
- External databases and external Exadata through OCI Management Agents

The tool runs from OCI Cloud Shell, a local workstation, OCI Resource Manager, or any automation runner that has OCI CLI and Terraform access. Every tenant-specific value is supplied through variables, ignored local config files, OCI Vault, or environment variables.

## Workshop

Start with the workshop guide: [docs/workshop/README.md](docs/workshop/README.md).

The workshop covers discovery, prerequisite provisioning, DBCS and Exadata SQL scripts, Autonomous Database validation, external database Management Agent onboarding, Operations Insights payloads, and final collection validation.

## Screenshots

These screenshots are captured from local public documentation only. They do not show a tenant selector, account name, OCIDs, IP addresses, or credentials.

![README preview](docs/screenshots/readme.png)

![Workshop preview](docs/screenshots/workshop.png)

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]

dbman-opsi doctor
dbman-opsi plan --profile <OCI_PROFILE> --region <OCI_REGION> --output dbman-opsi.local.yaml
dbman-opsi provision --config dbman-opsi.local.yaml --render-only
dbman-opsi prepare-prereqs --config dbman-opsi.local.yaml --dry-run
dbman-opsi generate-db-scripts --config dbman-opsi.local.yaml --output generated/db-scripts
dbman-opsi generate-opsi-payloads --config dbman-opsi.local.yaml --output generated/opsi-payloads
dbman-opsi preflight --config dbman-opsi.local.yaml
dbman-opsi configure --config dbman-opsi.local.yaml          # plan: detect + gate, no changes
dbman-opsi enable --config dbman-opsi.local.yaml --dry-run
dbman-opsi validate --config dbman-opsi.local.yaml
```

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
python3 -m pip install -e .[dev]
dbman-opsi doctor
```

Then run the workshop with `--profile DEFAULT` and your selected region.

## Resource Manager

The Deploy to Oracle Cloud button launches the Terraform stack under `terraform/examples/zero-start-poc`. Resource Manager provisions OCI-side prerequisites such as IAM, workshop networking, and service private endpoints. Database credentials and database-side scripts are handled by the CLI workflow so secrets are not placed in Terraform variables.

For your public fork, update the button URL to your repository archive URL.

## Commands

- `doctor`: check Python, OCI CLI, and Terraform availability. Pass `--profile`/`--region` to also confirm the OCI session is authenticated (not just installed).
- `plan`: discover compartments, networks, databases, Vaults, private endpoints, and agents. For DBCS/Exadata it can also discover pluggable databases (PDBs) and add them as PDB targets linked to their parent CDB.
- `provision`: render Terraform variables and optionally run Terraform.
- `import-tf-outputs`: read `terraform output` and merge the created OCIDs (subnet, VCN, Database Management private endpoint, provisioned database IDs) back into the config so `enable`/`configure` pick them up without manual copy.
- `prepare-prereqs`: create service-side private endpoints and optional Vault secrets from an environment variable.
- `generate-db-scripts`: create database-side SQL scripts for DBCS, Exadata, and external database targets.
- `generate-agent-scripts`: create Management Agent bootstrap scripts for external targets.
- `generate-opsi-payloads`: create Operations Insights JSON payload templates.
- `preflight`: read-only check of every prerequisite (IAM, Service Gateway, route, private endpoints, Vault secret, monitoring user, Management Agent). Supports `--json` and `--db-check-file` (spooled `04-validate-monitoring-user.sql` output) to verify the DB monitoring user instead of leaving it manual.
- `configure`: orchestrated detect → branch-by-location → gate → act flow. `--apply` enables, `--db-side-only` emits DBA handoff packets, `--force` overrides blockers, `--json` for automation.
- `enable`: run OCI Database Management and Operations Insights enablement. Idempotent and self-healing — re-runs tolerate an already-enabled DBM (409) and **reconcile** the connection (so a corrected service name or rotated credential takes effect), skip already-ACTIVE OPSI insights, and (in `--apply`) set the advanced-diagnostics preferred credentials. Use `--skip-credentials` to opt out of the last step.
- `set-credentials`: set the DBM advanced-diagnostics preferred credentials (`PC_READ`/`PC_WRITE`) via a Vault-backed named credential, so on-demand tasks (Performance Hub, AWR, ADDM, SQL Tuning) work. Idempotent; retries the flaky `dbmgmt` control plane and reports blocked targets with remediation.
- `validate`: check service state and collection readiness. Reports the real OPSI Database Insight lifecycle (`ACTIVE`/`FAILED`/`NOT_FOUND`/`UNKNOWN`) per target rather than a generic message.

## End-to-end enablement, Terraform & troubleshooting

- **Reproducible runbook:** [docs/RUNBOOK-e2e-cap.md](docs/RUNBOOK-e2e-cap.md) walks the full
  Phase 0→5 flow (confirm infra → doctor/preflight → DB-side proof → generate →
  enable + validate → Console showcase), with the five defects found and fixed
  running it live.
- **Troubleshooting KB:** [KB.md](KB.md) maps live-tenancy failure signatures to
  root cause + fix (OPSI insight 80% failure, DBM idempotency, DBSNMP lock loop,
  DBM stale-service reconcile, validate blindness). On any error, the CLI also
  prints a *Solution* + *Manual step* from the same remediation map.
- **Declarative / ORM path:** [terraform/modules/dbm-opsi-enablement](terraform/modules/dbm-opsi-enablement)
  is a feature-toggled, `for_each`-driven module (DBM features management, named
  credential, OPSI insight, plus a CLI step for preferred credentials). Pure
  Terraform for teams that prefer Resource Manager over the CLI. `terraform
  validate` passes; apply-test in a scratch tenancy before production.

## Security

Generated local configs contain OCID references needed for automation, but they are ignored by Git. Plaintext database credentials must never be written to config, Terraform variables, screenshots, or documentation. Use OCI Vault and environment variables.

See [docs/security.md](docs/security.md) before publishing screenshots or pushing a public repository.
