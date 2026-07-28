# Fleet Lifecycle

## Plan first

```bash
dbman-opsi onboard --region <REGION> \
  --answers fleet-answers.local.yaml \
  --selection-file selected-targets.local.yaml \
  --non-interactive --plan-only --state .fleet-state/fleet.sqlite
```

This is read-only, prints a sanitized immutable plan and exits `10`. Review the
exact plan ID before any writes.

## Apply and resume

```bash
dbman-opsi onboard --region <REGION> --answers fleet-answers.local.yaml \
  --non-interactive --approval <EXACT_PLAN_ID> \
  --state .fleet-state/fleet.sqlite

dbman-opsi resume --region <REGION> --run-id <RUN_ID> \
  --approval <EXACT_PLAN_ID> --state .fleet-state/fleet.sqlite

dbman-opsi fleet-status --region <REGION> --run-id <RUN_ID> \
  --state .fleet-state/fleet.sqlite --json
```

The executor checkpoints phases and resumes only incomplete/retryable/handoff
work. Independent targets continue after failure, while PDBs wait for their CDB.
Authorization failures stop further writes.

## Handoffs and collection evidence

Use private mode-`0600` HMAC keys and import only matching signed evidence:

```bash
dbman-opsi import-handoff --region <REGION> --run-id <RUN_ID> \
  --approval <EXACT_PLAN_ID> --evidence completion.json \
  --handoff-key <PRIVATE_0600_KEY>

dbman-opsi import-collection-evidence --region <REGION> --run-id <RUN_ID> \
  --approval <EXACT_PLAN_ID> --evidence service-proof.json \
  --handoff-key <PRIVATE_0600_KEY>
```

`configured` and `collecting` are not `ready`; current collection observation
or unexpired matching proof is required for a ready verdict.

## Safe offboarding

```bash
dbman-opsi offboard --region <REGION> --run-id <RUN_ID> \
  --state .fleet-state/fleet.sqlite --plan-only
dbman-opsi offboard --region <REGION> --run-id <RUN_ID> \
  --state .fleet-state/fleet.sqlite --approval <EXACT_CLEANUP_PLAN_ID>
```

Cleanup follows reverse dependencies and affects only resources the run both
owned and enabled. Reused/pre-existing resources are preserved. Production never
deletes databases.
