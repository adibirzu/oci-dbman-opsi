# Security and Release

## State and secrets

Fleet state defaults to `.fleet-state/fleet.sqlite` and is private mode `0600`.
Optional OCI Object Storage state validates checksums, schema/run/plan bindings,
uses ETag conditional writes, and uses a separate lease to fail closed on
concurrent writers:

```text
--state-backend object --state-namespace <NAMESPACE>
--state-bucket <PRIVATE_BUCKET> --state-object <RUN_STATE_OBJECT>
```

Never commit secrets, OCI identifiers, Terraform state, generated packets,
wallets, SSH keys, logs, or topology. Use Vault references and environment
variables. Public output is redacted; retain only sanitized evidence for the
approved period (seven days by default).

## Exit codes

`0` complete/success (inspect verdict), `2` degraded/handed-off/partial cleanup,
`3` blocked onboarding, `4` approval mismatch, `5` invalid policy/input, `6`
missing run/plan, and `10` plan-only.

## Verification and live gates

```bash
python -m pytest
python -m pytest -m eval --no-cov
terraform -chdir=terraform/examples/zero-start-poc fmt -check
python scripts/security-gate.py
```

CI adds Python version coverage, Terraform validation/contracts, `pip-audit`,
Bandit, and gitleaks. These are local/repository evidence, not live proof. A
release owner must separately retain approved scope and identity, current
collection timestamps/proofs, handoff outcomes, and final run-owned inventory.
