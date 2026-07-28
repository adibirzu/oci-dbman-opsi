# Fleet Lifecycle Operator Runbook

This runbook covers the plan-gated fleet interface. It is deliberately separate
from the existing expert `configure` flow. A green local test, a successful OCI
registration, or an instruction-only handoff is **not** proof that data collection
is ready. No live OCI acceptance result is recorded in this repository.

## Authority and operating modes

| Mode | Permitted intent | Database deletion | Required authority |
| --- | --- | --- | --- |
| PoC | Approved disposable targets; optional test DBCS/ADB provisioning | Only a run-created test database with the cleanup plan's typed confirmation | Approved disposable-compartment change authority |
| Demo | Approved disposable demonstration with a unique lifecycle tag | Same as PoC | Demo owner plus change-window approval |
| Production | Existing services only | Never | Exact plan approval, production change approval, and target-owner authority |

Use API-key profile, security-token profile, instance principal, or resource
principal authentication. The tool records modes and references only; it must not
receive or print token, key, password, wallet, or secret material. Production answer
files require `approval-required` authority; shared passwords and production test
database provisioning are rejected.

The default policy is `shared-user-unique-secret`: the reviewed monitoring
username (default `DBMAN_MON`) is reused while every independent database account
gets its own Vault-backed secret. `dedicated-user-unique-secret` creates a distinct
username and secret per target. `shared-user-shared-secret` is limited to PoC/demo
and requires the explicit warning path; production rejects it. A common CDB user
cannot be combined with unique PDB passwords. Use independent local PDB users when
the same username must have different PDB passwords. Keep answer files, selection
files, local SQLite state, completion packets, and any downloaded Object Storage
cache outside version control and mode `0600`.

## Scope, filtering, and answer files

Discovery is read-only and starts from all subscribed regions plus all accessible
compartments. Failed region or compartment reads make the result incomplete and
block planning; they are not treated as an empty scope. It recognizes DBCS CDB/PDB,
ADB, Exadata, external database, and external Exadata targets. CDB is a dependency
of each PDB; onboarding follows CDB then PDB, while cleanup reverses that order.

An answer file selects services, optional disposable provisioning, credential
policy, log preset, authority mode, concurrency, retention, and filters. Filters
can combine regions, compartments, kinds, lifecycle states, tags, name glob,
current service state, explicit IDs, exclusions, and `all_discovered`. Exclusions
always win. Add `--selection-file` for an explicit CSV (`target_id` column) or YAML
(`targets` or `target_ids` list) multiselect. Keep identifiers in ignored local
files; do not commit tenancy inventory.

Example local answer file (no credentials or target identifiers shown):

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

The default log preset is `alert-listener-audit`. `extended` and `none` are
explicit alternatives. Association/configuration is only `collecting`; Log
Analytics needs a current searchable result, DBM needs its collection timestamp,
and OPSI needs its observation timestamp before the combined requested service set
may report `ready`. OCI propagation, including OPSI, can take up to 24 hours.

## Commands and exit codes

Run all commands from an approved host with the selected OCI identity. Replace only
the placeholders locally; never paste tenant values into evidence or source files.

```bash
# Read-only discovery and reviewed plan. Exit 10 means plan-only; no writes occurred.
dbman-opsi onboard --region <REGION> --answers fleet-answers.local.yaml \
  --selection-file selected-targets.local.yaml --non-interactive --plan-only

# Re-run after human review using the exact printed plan ID.
dbman-opsi onboard --region <REGION> --answers fleet-answers.local.yaml \
  --non-interactive --approval <EXACT_PLAN_ID> --state .fleet-state/fleet.sqlite \
  --handoff-key .fleet-state/handoff.key --handoff-dir generated/fleet-handoffs

# Resume only the checkpoint-safe phases of a known run.
dbman-opsi resume --region <REGION> --run-id <RUN_ID> --approval <EXACT_PLAN_ID> \
  --state .fleet-state/fleet.sqlite --instance-principal \
  --handoff-key .fleet-state/handoff.key --handoff-dir generated/fleet-handoffs

# Sanitized lifecycle status (human Markdown or machine JSON).
dbman-opsi fleet-status --region <REGION> --run-id <RUN_ID> --json \
  --state .fleet-state/fleet.sqlite

# Print the reverse, ownership-safe cleanup plan (exit 10).
dbman-opsi offboard --region <REGION> --run-id <RUN_ID> --plan-only \
  --state .fleet-state/fleet.sqlite

# Execute only after reviewing the displayed cleanup plan ID.
dbman-opsi offboard --region <REGION> --run-id <RUN_ID> --approval <EXACT_CLEANUP_PLAN_ID> \
  --state .fleet-state/fleet.sqlite
```

Public `fleet-status` output exposes a run-scoped opaque `target_handle`, verdict,
and phase states only. Raw target IDs, resource references, OCIDs, arguments, and
topology remain in private state. `offboard --plan-only` similarly exposes ordered
operation, `target_kind`, opaque target/resource handles, ownership and
`created`/`enabled_by_run` flags, plus handoff/database-confirmation indicators.
Each action contains exactly `order`, `operation`, `target_kind`, `target_handle`,
`resource_handle`, `ownership`, `created`, `enabled_by_run`, `handoff_required`,
and `requires_database_confirmation`; its exact cleanup plan ID continues to bind
the private immutable plan used for approval.

Use `--security-token`, `--instance-principal`, or `--resource-principal` instead
of the default named profile when appropriate. Object Storage state is opt-in:
add `--state-backend object --state-namespace <NAMESPACE> --state-bucket <BUCKET>
--state-object <OBJECT>` to every command in the run. The local cache remains
`0600`. Download verifies checksum, run ID, plan ID, and schema version. Upload
uses the prior ETag with `if-match` (or create-only when no version exists), so a
concurrent writer fails closed rather than being overwritten. Treat either integrity
or upload-conflict failure as a resume blocker.

Exit codes are: `0` success/ready or complete cleanup; `2` degraded, handed-off,
or partial cleanup; `3` blocked onboarding; `4` exact approval mismatch; `5` invalid
input/policy; `6` missing run or plan; and `10` plan-only. A `collecting` status may
return `0` while explicitly remaining short of live collection acceptance; inspect
the verdict and required evidence rather than using the process status alone.

Normal resume re-enters pending, retryable, or handed-off phases and never
replays completed phases. After remediating a non-authorization terminal
failure, `resume --retry-failed` explicitly reopens failed phases and children
that were blocked by those failed dependencies for the same exact approved
plan. It does not reopen authorization blocks.

## Handoffs, resume, and cleanup

For an onboard plan that needs private per-target references, supply an ignored,
owner-managed `--bindings` YAML/JSON file with mode `0600`. It may contain Vault,
endpoint, authority, account-group, and service-name references, but never a
plaintext password. Bindings become private immutable plan intent and therefore
change the exact approval hash; public plan output intentionally shows that
bindings were supplied but not their values. `onboard --plan-only` prints the
complete sanitized review surface (mode, services, policy, provisioning,
authority, concurrency, dependencies, and log choices) and exits `10`.

Use `--handoff-key <0600-file> --handoff-dir <private-directory>` to issue signed
onboarding handoff packets. Import a signed completion before resuming:

```bash
dbman-opsi import-handoff --region <REGION> --run-id <RUN_ID> \
  --approval <PLAN_ID> --evidence <completion.json> --handoff-key <0600-key>
```

For a manually handed-off cleanup action, use the corresponding exact cleanup
approval and packet:

```bash
dbman-opsi import-cleanup-handoff --region <REGION> --run-id <RUN_ID> \
  --approval <CLEANUP_PLAN_ID> --evidence <cleanup-completion.json> \
  --handoff-key <0600-key>
```

If a supported bounded OCI observation query is unavailable, an approved
per-service signer can provide a redacted, timestamped completion observation:

```bash
dbman-opsi import-collection-evidence --region <REGION> --run-id <RUN_ID> \
  --approval <PLAN_ID> --evidence <service-proof.json> --handoff-key <0600-key>
```

The evidence must bind the exact run, plan, opaque target handle, selected
service, allowlisted result, and a fresh timestamp. Timestamps beyond the bounded
clock-skew allowance are rejected. Readiness is recomputed from unexpired
per-service proofs during status and resume; an expired proof reopens validation.
The evidence is retained as a private non-owning collection-proof record and
cannot authorize cleanup.

The importer binds the packet to its run, plan, opaque target, and phase; an
unsigned checkpoint or a packet for another binding is rejected. A local
transactional run lease is acquired before the first checkpoint or OCI phase;
a second local actor fails before it can make a service call. Leases expire after
an interrupted process stops renewing them, after which a reviewed resume can
acquire the run.

When `--state-backend object` is selected, onboarding, resume, cleanup, and
handoff import first acquire a separate conditional Object Storage lease object.
The lock is bound to the run and plan and is replaced only with its current ETag
after expiry; an object-store conflict is a fail-closed write blocker. This is
separate from the state upload ETag and prevents two hosts from making OCI calls
before either has uploaded a checkpoint.

When approved DB/host access, a Management Agent, endpoint information, or a
credential binding is unavailable, the executor checkpoints `handed-off` and
emits a signed packet using opaque target handles. The DBA/host administrator must
return a signed completion envelope with an attestation and allowed result.
Resource-creating service handoffs must also include the phase-allowlisted resource
identity and observed effect (`created`, `reused`, or `preexisting`). The importer
derives cleanup ownership from that effect and refuses to close such a phase when
the identity is absent. These topology-bearing completion envelopes remain private
`0600` state; only their redacted digest belongs in a release report. Instruction-
only packets, invalid signatures, wrong run/plan/target bindings, and credential
material are rejected.

`429` and transient `5xx` failures retry with bounded jitter. A resource-already-
exists `409` is idempotent only when it is unambiguous; conflicts in progress stay
retryable/partial. Authentication/authorization failures open the circuit breaker,
block additional write attempts, and require identity/policy remediation before a
new approved run. An interruption checkpoints its current phase as retryable;
`resume` does not repeat completed checkpoints. Local scale acceptance runs the
real executor, approval, checkpoint, status, and zero-action offboard path at 1,
100, and 1000 targets using an in-memory checkpoint store and one validation phase;
focused tests cover the complete nine-phase ordering and SQLite durability. Keep
answer-file concurrency between 1 and 8 and expect eventual control-plane consistency
rather than simultaneous readiness.

Offboarding builds the reverse plan from the stored run manifest only. It removes
Log Analytics associations, OPSI, run-created Data Safe target registrations, PDB
DBM, CDB DBM, and then only resources both created/owned and enabled by the run.
An owned resource without a supported direct route becomes a signed cleanup
handoff instead of being omitted. Reused and preexisting resources remain. A
repeated completed offboard is a no-op. Retain sanitized action metadata for seven
days; do not retain secret values, OCIDs, endpoints, or topology in public evidence.

## Scratch-tenancy acceptance matrix (not yet executed)

All rows below are **IN PROGRESS / OWNER INPUT REQUIRED**. Before execution, the
release owner must supply: approved scratch tenancy/compartment and region, target
family access, an approved OCI auth mode, least-privilege policies, Vault references
and DBA/host-admin access where applicable, a change window, and a redaction-safe
evidence destination. For production, also supply service-owner approval and confirm
that offboarding is service-only.

| Target family | Required live sequence | Automatic/handoff boundary | Redacted evidence template | Exit condition |
| --- | --- | --- | --- | --- |
| DBCS CDB and PDB | Fresh disposable provision where approved; enable CDB then PDB; prove DBM/OPSI/Log Analytics collection; interrupt/resume; offboard PDB then CDB; run a second offboard | DB/host work may hand off; signed DBA completion required | `evidence/live/<run>/dbcs-cdb-pdb.json` with plan/run digests, phase verdicts, collection timestamps, cleanup digest | Final discovery shows zero run-owned resources |
| ADB | Fresh disposable provision where approved; enable requested pillars; prove collection; interrupt/resume; offboard; second run | Wallet/agent/credential boundary may hand off | `evidence/live/<run>/adb.json` with redacted pillar queries and cleanup digest | Final discovery shows zero run-owned resources |
| Exadata | Existing or scratch approved CDB/PDB enablement; prove collection; interruption/resume; service-only offboard | DBA/host administration is explicit | `evidence/live/<run>/exadata.json` | No run-owned service associations remain |
| External DB | Register through approved Management Agent; prove collection; interruption/resume; offboard | Host-admin/DBA signed completion is expected | `evidence/live/<run>/external-db.json` | No run-owned agent/service association remains |
| External Exadata | Register through approved Management Agent; prove collection; interruption/resume; offboard | Host-admin/DBA signed completion is expected | `evidence/live/<run>/external-exadata.json` | No run-owned agent/service association remains |

For each row, record fresh provision (when allowed), enablement, collection query
timestamps, an intentional safe interruption and exact-plan resume, automatic and
handoff path results, second-run idempotency, offboard plan/approval, and final
inventory query. Redact target identifiers, names, endpoints, secret references,
and credentials. Do not mark a row complete without the release owner attaching a
current redacted receipt at the stated private evidence location.
