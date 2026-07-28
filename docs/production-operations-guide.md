# Production Operations Guide

`dbman-opsi` is an OCI enablement and lifecycle tool for Database Management
(DBM), Operations Insights (OPSI), Data Safe, and Log Analytics. It supports
DBCS (including CDB/PDB), Autonomous Database, Exadata, external databases,
and external Exadata.

The tool is production-oriented: it separates discovery from writes, requires
an exact reviewed plan before fleet changes, records checkpointed state, and
does not treat registration as proof of collection. It is not an Oracle
product or a substitute for an approved change process. The current repository
has local automated verification; its live scratch-tenancy acceptance matrix
remains open until an owner supplies current redacted evidence.

## What It Does

| Area | Capability | Important boundary |
| --- | --- | --- |
| Discovery | Read-only inventory across subscribed regions and accessible compartments | An inaccessible scope blocks planning; it is never silently treated as empty. |
| Enablement | DBM, OPSI, Data Safe, and Log Analytics target onboarding | Each target explicitly chooses `dbm`, `opsi`, `datasafe`, and/or `logan`. |
| Fleet lifecycle | Plan-gated onboarding, resume, status, signed handoffs, and ownership-safe offboarding | Production never deletes databases. |
| Credentials | OCI Vault references and per-account secret policies | Plaintext passwords are not stored in config, state, Terraform, or public evidence. |
| Evidence | Redacted journals, sanitized fleet status, and bounded incident evidence bundles | Registration/configuration is distinct from current collection proof. |
| Diagnostics | OPSI/DBM prerequisite packets, Process Insights checks, DB incident evidence, and DB-side handoffs | Database and host work remains under DBA/host-owner authority. |

## Installation

### Supported operator hosts

Use OCI Cloud Shell, a controlled workstation, or an automation runner with:

- Python 3.11 or later;
- OCI CLI authenticated as the operating identity;
- Terraform 1.5 or later only when using Terraform provisioning;
- `sqlplus`/SQLcl, OCI Bastion access, and Management Agent access only for the
  workflows that need database or host actions.

```bash
git clone https://github.com/adibirzu/oci-dbman-opsi.git
cd oci-dbman-opsi
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

# Optional local secret inputs. Keep this ignored file private.
cp .env.local.example .env.local
chmod 600 .env.local

dbman-opsi doctor --profile <OCI_PROFILE> --region <OCI_REGION>
```

Cloud Shell already supplies OCI CLI. Install the package as above and use the
signed-in `DEFAULT` profile, or the profile approved by the tenancy owner.

Do not commit `.env.local`, `*.local.yaml`, fleet state, Terraform state,
generated packets, wallets, SSH keys, OCIDs, topology, or evidence containing
those values. They are intentionally ignored by this repository.

## Authentication and authority

The lifecycle commands support one of the following OCI authentication paths:

```bash
# Named OCI CLI profile (default)
dbman-opsi onboard --profile <PROFILE> --region <REGION> ...

# Security-token, instance-principal, or resource-principal identity
dbman-opsi onboard --region <REGION> --security-token ...
dbman-opsi onboard --region <REGION> --instance-principal ...
dbman-opsi onboard --region <REGION> --resource-principal ...
```

Give the operator identity only the scope needed to discover, plan, and execute
the approved service actions. Database changes, host changes, Vault use, private
endpoint work, and Management Agent installation need their own owner-approved
permissions. For a failed-enablement diagnostic packet, begin with read-only
visibility; see [customer-tenancy OPSI diagnostics](../README.md#customer-tenancy-opsi-diagnostics).

## Configuration

There are two interfaces:

1. The established per-target workflow uses a private YAML/JSON config generated
   by `plan`.
2. The fleet workflow uses a private answer file and optional selection/binding
   files. It discovers targets itself and makes an immutable, reviewable plan.

### Per-target config

Create a private config through guided discovery rather than pasting identifiers
into source files:

```bash
dbman-opsi discover --profile <PROFILE> --region <REGION> \
  --compartment <COMPARTMENT_OCID> --subtree
dbman-opsi plan --profile <PROFILE> --region <REGION> \
  --output dbman-opsi.local.yaml
dbman-opsi preflight --config dbman-opsi.local.yaml --json
```

A target can set its kind (`dbcs`, `autonomous`, `exadata`, `external-db`, or
`external-exadata`), CDB/PDB role and dependency, home region, and selected
services. `loganalytics` is accepted as a compatibility alias for `logan`.
The default service set is `dbm, opsi`; Data Safe and Log Analytics are opt-in.

### Fleet answer file

Store this as an ignored file such as `fleet-answers.local.yaml`, with mode
`0600`. It contains policy choices, never secrets or target topology:

```yaml
deployment_mode: production
services: [dbm, opsi, logan]
credential_policy: shared-user-unique-secret
monitoring_username: DBMAN_MON
log_preset: alert-listener-audit
authority_mode: approval-required
max_concurrency: 4
retention_days: 7
common_user: false
pdb_unique_passwords: false
discovery_filters:
  regions: []
  compartments: []
  kinds: []
  lifecycle_states: [AVAILABLE]
  tags: {}
  service_states: {}
  target_ids: []
  exclude_target_ids: []
  all_discovered: true
```

`max_concurrency` must be 1–8. `shared-user-unique-secret` is the production
default. `shared-user-shared-secret` is rejected in production and is allowed
only for explicit PoC/demo use. A common CDB user cannot be combined with unique
PDB passwords. In production, `approval-required` is mandatory and test
database provisioning is rejected.

Use `--selection-file` for a private CSV with a `target_id` column or a private
YAML file containing `targets`/`target_ids`. Selection filters support regions,
compartments, target kinds, lifecycle states, tags, names, service states,
explicit IDs, and exclusions; exclusions always win.

## Production fleet workflow

### 1. Discover and review, with no writes

```bash
dbman-opsi onboard --region <REGION> \
  --answers fleet-answers.local.yaml \
  --selection-file selected-targets.local.yaml \
  --non-interactive --plan-only \
  --state .fleet-state/fleet.sqlite
```

The command reads all subscribed regions and accessible compartments, validates
CDB-before-PDB dependencies, emits a sanitized plan, and exits `10` for
plan-only. Record the exact printed plan ID in the approved change record.

### 2. Apply the exact reviewed plan

```bash
dbman-opsi onboard --region <REGION> \
  --answers fleet-answers.local.yaml --non-interactive \
  --approval <EXACT_PLAN_ID> \
  --state .fleet-state/fleet.sqlite
```

The executor checkpoints prerequisite, credential/endpoint, DB/host handoff,
DBM, preferred credential, OPSI, Management Agent/Log Analytics, and validation
phases. Independent targets continue after a failure; a PDB is blocked if its
CDB cannot complete. Authorization failures open a circuit breaker rather than
continuing writes under an invalid identity.

### 3. Resume, inspect, and prove collection

```bash
dbman-opsi resume --region <REGION> --run-id <RUN_ID> \
  --approval <EXACT_PLAN_ID> --state .fleet-state/fleet.sqlite
dbman-opsi fleet-status --region <REGION> --run-id <RUN_ID> \
  --state .fleet-state/fleet.sqlite --json
```

`configured` or `collecting` is not `ready`. A requested service set is ready
only when its current collection observation/proof is present. OPSI propagation
may take up to 24 hours. Use a signed, redacted per-service collection envelope
when the bounded OCI observation is unavailable:

```bash
dbman-opsi import-collection-evidence --region <REGION> --run-id <RUN_ID> \
  --approval <EXACT_PLAN_ID> --evidence service-proof.json \
  --handoff-key <PRIVATE_0600_KEY>
```

### 4. Handoff protected work

When the database or host owner must act, issue a signed packet from the
onboarding/resume flow with `--handoff-key <PRIVATE_0600_KEY>`. Import only a
matching signed completion:

```bash
dbman-opsi import-handoff --region <REGION> --run-id <RUN_ID> \
  --approval <EXACT_PLAN_ID> --evidence completion.json \
  --handoff-key <PRIVATE_0600_KEY>
```

Packets bind the run, plan, opaque target, and phase. They never authorize a
different run or expose credentials.

### 5. Offboard safely

```bash
# First view the reverse plan; no writes.
dbman-opsi offboard --region <REGION> --run-id <RUN_ID> \
  --state .fleet-state/fleet.sqlite --plan-only

# Apply only the exact reviewed cleanup plan.
dbman-opsi offboard --region <REGION> --run-id <RUN_ID> \
  --state .fleet-state/fleet.sqlite --approval <EXACT_CLEANUP_PLAN_ID>
```

Cleanup removes only associations/services that the run enabled and resources it
recorded as created and owned. It preserves reused and pre-existing resources,
reverses PDB before CDB dependency order, and is idempotent. Production mode
never deletes a database. PoC/demo test-database deletion additionally requires
the reviewed cleanup plan, `--delete-test-databases`, and the typed confirmation
displayed by that plan.

## State, evidence, and retention

The default state store is `.fleet-state/fleet.sqlite`; it is private and mode
`0600`. OCI Object Storage state is optional. Pass the same backend, namespace,
bucket, and object to every command in the run:

```bash
--state-backend object --state-namespace <NAMESPACE> \
--state-bucket <PRIVATE_BUCKET> --state-object <RUN_STATE_OBJECT>
```

Object state verifies checksum, schema version, run/plan binding, and uses ETag
conditions plus a separate lease to fail closed on competing writers. Keep the
local cache private. Retain only sanitized run metadata for the configured
period (the production default is seven days).

Exit codes: `0` complete/success (but inspect the verdict), `2` degraded,
handed-off, or partial cleanup, `3` blocked onboarding, `4` approval mismatch,
`5` invalid input/policy, `6` missing run/plan, and `10` plan-only.

## Service-specific operating notes

- **DBM:** CDB/non-CDB and PDB targets use distinct OCI enablement paths. A PDB
  requires its parent CDB to have DBM first. Preferred credentials for advanced
  diagnostics are Vault-backed.
- **OPSI:** A database insight being registered is not a collection observation.
  Use `validate`, `process-insights`, or the generated diagnostic packet to
  identify missing credentials, private endpoint, network, or agent conditions.
- **Data Safe:** Target registration, audit profiles/trails, and delivered audit
  events are separate milestones. An `ACTIVE` target with no audit trail does
  not produce Data Safe audit results.
- **Log Analytics:** DBCS/Base DB needs a Management Agent-backed collector
  path; the tool blocks detached-entity configuration. The production default
  log preset is `alert-listener-audit`; `extended` and `none` require an explicit
  choice. ADB collection needs an approved private collector, TCPS wallet, and
  local credential registration outside Terraform state.

## Expert per-target workflow

For an approved target config, use dry-run/read-only steps first:

```bash
dbman-opsi preflight --config dbman-opsi.local.yaml
dbman-opsi generate-db-scripts --config dbman-opsi.local.yaml \
  --output generated/db-scripts
dbman-opsi generate-agent-scripts --config dbman-opsi.local.yaml \
  --output generated/agents
dbman-opsi generate-logan-payloads --config dbman-opsi.local.yaml \
  --output generated/logan
dbman-opsi configure --config dbman-opsi.local.yaml --json
```

After change approval, use `configure --apply`. Add `--with-data-safe` and/or
`--with-log-analytics` only for opted-in targets. Prefer `--db-side-only` when
DBA ownership requires a packet. `db-exec --apply` may automate DB-side scripts
through Bastion only in non-production; production profiles generate handoffs.

## Incident and diagnostic operations

```bash
# Bounded, redacted multi-source incident bundle
dbman-opsi db-incident --profile <PROFILE> --region <REGION> \
  --compartment-id <COMPARTMENT_OCID> --ora-code ORA-00600 --json

# Read-only packet for failed DBCS/Exadata OPSI enablement
dbman-opsi generate-opsi-diagnostics --config dbman-opsi.local.yaml \
  --output generated/opsi-diagnostics

# Process Insights coverage diagnosis
dbman-opsi process-insights --config dbman-opsi.local.yaml --json
```

The `generate-db-incident-demo` command and `scripts/demo-*` paths are isolated
demo/lab workflows. Use them only against approved disposable targets; they are
not a production incident remediation procedure.

## Verification and release gate

Run the local checks appropriate to a change before release:

```bash
python -m pytest
python -m pytest -m eval --no-cov
terraform -chdir=terraform/examples/zero-start-poc fmt -check
python scripts/security-gate.py
```

CI additionally runs the Python matrix, Terraform validation/contract tests,
`pip-audit`, Bandit, and gitleaks. A passing local or CI gate does not prove a
live tenancy. The release owner must separately retain redacted evidence for
approved scope, identity/policies, collection timestamps/proofs, DB/host
handoffs, cleanup inventory, and any target-family acceptance run. See the
[fleet lifecycle runbook](fleet-lifecycle-runbook.md) for the acceptance matrix
and the [security guide](security.md) for publication controls.
