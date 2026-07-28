# Ops Insights PoC Update: Cross-Region Monitoring And Diagnostics

This update records the public-safe changes added for the end-to-end OCI Database
Management, Ops Insights, and Data Safe PoC. Tenant-specific identifiers,
regions, hostnames, IP addresses, OCIDs, credentials, and SQL details stay in
ignored local files or redacted screenshots.

## New Capabilities

- **Cross-region Ops Insights monitoring**: `init-region` creates a second-region
  provisioning config, and `cross-region` writes/prints the region selector set
  for Ops Insights Data Object Explorer plus the supported Configuration and
  Capacity dashboards.
- **Chicago DBCS provisioning path**: the paid second-region DBCS flow uses
  variables from `.env.local`, renders a regional Terraform work directory, and
  imports Terraform outputs back into the ignored local config.
- **Advanced diagnostics by default**: generated DB scripts grant the monitoring
  user the Performance Hub, AWR, ADDM, and SQL tuning privileges needed for the
  Database Management full feature set.
- **Host firewall handoff**: every DBCS/Exadata packet includes
  `00-check-host-firewall.sh`, which checks `firewalld` or `iptables` and prints
  the exact TCP listener-port commands before applying anything.
- **Process Insights diagnostics**: `process-insights` distinguishes host
  inventory/resource summaries from missing top-process rows, so the PoC can
  explain when a host collector path is required instead of treating the Console
  as broken.
- **OPSI diagnostic packet**: `generate-opsi-diagnostics` captures read-only OCI
  control-plane evidence and DB-side SQL probes for failed DBCS/Exadata Ops
  Insights enablement.

## Deployment And Validation Checks

Before publishing, validate the CLI and Terraform surfaces rather than exposing
tenant state:

```bash
python -m pytest
terraform -chdir=terraform/examples/zero-start-poc fmt -check
terraform -chdir=terraform/examples/zero-start-poc init -backend=false
terraform -chdir=terraform/examples/zero-start-poc validate
dbman-opsi doctor
```

For a live customer or PoC run, keep the mutable and sensitive values local:

```bash
cp .env.local.example .env.local
chmod 600 .env.local
dbman-opsi provision --config dbman-opsi.local.yaml --render-only
dbman-opsi preflight --config dbman-opsi.local.yaml
dbman-opsi configure --config dbman-opsi.local.yaml --apply
dbman-opsi validate --config dbman-opsi.local.yaml
dbman-opsi process-insights --config dbman-opsi.local.yaml --interval P7D
```

Only sanitized screenshots from `docs/screenshots/` are committed. Raw OCI
Console captures remain under `docs/screenshots/raw/`, which is ignored by Git,
and must be redacted before use in the README, workshop, or blog.

## Test Evidence

The repository test suite covers the new behavior with focused unit tests:

- `tests/test_cross_region.py` verifies multi-region plan generation.
- `tests/test_regional_provisioning.py` verifies second-region provisioning
  config rendering.
- `tests/test_db_scripts.py` verifies advanced diagnostics and firewall script
  generation.
- `tests/test_process_insights.py` verifies Process Insights collection
  diagnostics.
- `tests/test_opsi_diagnostics.py` verifies the read-only OPSI evidence packet.
- `tests/test_public_repo_readiness.py` verifies public docs, screenshots, and
  sensitive-data hygiene.
