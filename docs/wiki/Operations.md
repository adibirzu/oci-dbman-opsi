# Operations and Diagnostics

## Service notes

- **DBM:** CDB/non-CDB and PDB use distinct enablement paths. Preferred
  credentials for advanced diagnostics are Vault-backed.
- **OPSI:** Insight registration is not collection proof. Use `validate`,
  `process-insights`, and generated diagnostics to find credential, private
  endpoint, network, or agent gaps.
- **Data Safe:** Target registration, audit profile/trail setup, and delivered
  audit events are distinct milestones.
- **Log Analytics:** DBCS/Base DB requires a Management Agent-backed collector;
  ADB requires an approved private collector, wallet, and credential registration
  outside Terraform state. Default logs are alert, listener, and audit.

## Useful commands

```bash
dbman-opsi preflight --config dbman-opsi.local.yaml --json
dbman-opsi generate-opsi-diagnostics --config dbman-opsi.local.yaml \
  --output generated/opsi-diagnostics
dbman-opsi process-insights --config dbman-opsi.local.yaml --json
dbman-opsi db-incident --profile <PROFILE> --region <REGION> \
  --compartment-id <COMPARTMENT_OCID> --ora-code ORA-00600 --json
dbman-opsi journal --last --json
```

`db-incident` builds a bounded redacted evidence bundle across Log Analytics,
DBM, OPSI, Audit, and Data Safe. It reports missing sources instead of inventing
evidence. The `generate-db-incident-demo` and `scripts/demo-*` workflows are
for approved disposable demonstrations only, not production remediation.
