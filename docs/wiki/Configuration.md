# Configuration and Options

## Per-target configuration

Use guided discovery to create an ignored configuration file:

```bash
dbman-opsi discover --profile <PROFILE> --region <REGION> \
  --compartment <COMPARTMENT_OCID> --subtree
dbman-opsi plan --profile <PROFILE> --region <REGION> \
  --output dbman-opsi.local.yaml
dbman-opsi preflight --config dbman-opsi.local.yaml --json
```

Supported target kinds: `dbcs`, `autonomous`, `exadata`, `external-db`, and
`external-exadata`. A target declares `dbm`, `opsi`, `datasafe`, and/or `logan`;
the default is `dbm, opsi`. `loganalytics` remains a compatibility alias for
`logan`. A PDB requires its parent CDB to be enabled first.

## Fleet answers

Keep fleet policy in a private `0600` answer file, without identifiers or
secrets:

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

`max_concurrency` is 1–8. Production requires `approval-required`, disallows
test database provisioning and shared-password policies, and forbids a common
CDB user with unique PDB passwords. Use `--selection-file` with a private CSV
`target_id` column or YAML target list. Exclusions always override selection.
