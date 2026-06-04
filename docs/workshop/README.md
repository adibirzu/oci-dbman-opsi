# Workshop: Enable OCI Database Management And Operations Insights

This workshop walks through an end-to-end enablement flow for Database Management and Operations Insights across DBCS, Autonomous Database, Exadata, and external database targets.

Use placeholders for every tenancy value. Do not paste real OCIDs, hostnames, usernames, or credentials into workshop notes.

## Lab 1: Prepare The Environment

Run this in OCI Cloud Shell or on a local workstation with OCI CLI and Terraform installed:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
dbman-opsi doctor
```

Expected result: `READY: python, oci, terraform`.

## Lab 2: Discover Or Select Targets

Start the wizard and choose an existing compartment, VCN, subnet, and database target:

```bash
dbman-opsi plan --profile <OCI_PROFILE> --region <OCI_REGION> --output dbman-opsi.local.yaml
```

For DBCS, select the target database and keep the monitoring user as `DBSNMP` unless your policy requires a custom user.

For Autonomous Database, choose the existing Autonomous Database resource. Database Management and Operations Insights can be validated directly from OCI status.

For Exadata, select the Exadata infrastructure or database target and use the generated database SQL scripts before OCI service enablement.

For external databases, generate and run the Management Agent bootstrap script on the host, then validate agent registration.

## Lab 3: Provision OCI Prerequisites

Generate Terraform variables for repeatable network and IAM setup:

```bash
dbman-opsi provision --config dbman-opsi.local.yaml --render-only
terraform -chdir=terraform/examples/zero-start-poc plan
```

Create Database Management and Operations Insights private endpoints:

```bash
dbman-opsi prepare-prereqs --config dbman-opsi.local.yaml --dry-run
dbman-opsi prepare-prereqs --config dbman-opsi.local.yaml --apply
```

If the database monitoring credential must be stored in Vault, export it only in the current shell:

```bash
export DBMAN_OPSI_DB_PASSWORD='<prompted-value>'
dbman-opsi prepare-prereqs --config dbman-opsi.local.yaml --password-env DBMAN_OPSI_DB_PASSWORD --apply
unset DBMAN_OPSI_DB_PASSWORD
```

## Lab 3b: Verify Prerequisites (Read-Only Gate)

Before any change, confirm every prerequisite is in place. `preflight` only reads:

```bash
dbman-opsi preflight --config dbman-opsi.local.yaml
```

It reports PASS/FAIL/WARN/MANUAL for each of:

- IAM service-principal policies (`database-management`, `operations-insights`, `dpd`)
- Network: subnet state, **Service Gateway + route rule to OCI Services**, listener ports
- Database Management and Operations Insights private endpoints (ACTIVE, right subnet)
- Vault secret holding the monitoring password
- Monitoring database user and grants (verified DB-side — marked MANUAL)
- External targets: Management Agent registered with `dbmgmt` and `opsi` plugins

A `FAIL` includes the exact remediation. Use `--json` to feed an automation runner.

## Lab 4: Run Database-Side Setup

Generate database SQL scripts:

```bash
dbman-opsi generate-db-scripts --config dbman-opsi.local.yaml --output generated/db-scripts
```

Run the scripts on DBCS or Exadata with SQLcl or SQL*Plus as an administrative user:

```sql
@01-create-monitoring-user.sql
@02-grant-basic-monitoring.sql
@04-validate-monitoring-user.sql
```

Use `03-grant-advanced-diagnostics.sql` only after reviewing local security and licensing policy.

## Lab 5: Enable And Validate Collection

Generate Operations Insights payloads and fill any placeholders:

```bash
dbman-opsi generate-opsi-payloads --config dbman-opsi.local.yaml --output generated/opsi-payloads
```

Enable services. The orchestrated path runs the prerequisite gate first, skips
targets that are already enabled, and only enables when everything passes:

```bash
dbman-opsi configure --config dbman-opsi.local.yaml              # plan: gate only
dbman-opsi configure --config dbman-opsi.local.yaml --apply      # enable when ready
```

If a DBA must run the database steps separately, generate handoff packets instead
of enabling directly:

```bash
dbman-opsi configure --config dbman-opsi.local.yaml --db-side-only --output generated/handoff
```

Each packet (`generated/handoff/<target>/HANDOFF.md`) contains the ordered SQL
scripts plus the exact OCI enable command to run once the database side is done.

The lower-level `enable` verb is still available for a single direct step:

```bash
dbman-opsi enable --config dbman-opsi.local.yaml --dry-run
dbman-opsi enable --config dbman-opsi.local.yaml --apply
```

Validate:

```bash
dbman-opsi validate --config dbman-opsi.local.yaml
```

The validation output should show Database Management enabled and Operations Insights enabled or ready for Database Insight validation.

## Resource Manager Path

Use the Deploy to Oracle Cloud button in the repository README to launch the Terraform stack in any tenant. Resource Manager provisions only OCI-side prerequisites. Database credentials and database-side SQL execution remain explicit workshop steps.

