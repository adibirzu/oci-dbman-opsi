# dbm-opsi-compatibility

Opt-in adapter for estates that still require
`oci_database_cloud_database_management`. Prefer the canonical
`dbm-opsi-enablement` module for new work. This adapter preserves the same
Vault-only credential contract, explicit CDB/PDB role model, and staged
disable safeguards.

Each PDB target must set `database_role = "PDB"`,
`database_resource_type = "pluggabledatabase"`, and a `parent_target_key`
for a CDB target in the same map. The PDB compatibility resource has a real
Terraform graph dependency on the CDB compatibility resource. After an
enable, the adapter reads the Managed Database collection by the supplied
compartment and exact name, then fails unless exactly one result is found.

## Enable

```hcl
module "legacy_dbm" {
  source = "../../modules/dbm-opsi-compatibility"

  compartment_id                  = var.compartment_id
  dbm_private_endpoint_id         = var.dbm_private_endpoint_id
  lifecycle_id                    = "<REVIEWED_LIFECYCLE_ID>"
  owner_tag                       = "database-platform"
  enable_database_management      = true
  dbm_operation_stage             = "enable"

  targets = {
    cdb = {
      database_id            = var.cdb_id
      managed_database_name  = "<CDB_MANAGED_DATABASE_NAME>"
      database_role          = "CDB"
      database_resource_type = "database"
      service_name           = "<CDB_LISTENER_SERVICE>"
      password_secret_id     = var.dbsnmp_secret_id
    }
    pdb = {
      database_id            = var.pdb_id
      managed_database_name  = "<PDB_MANAGED_DATABASE_NAME>"
      database_role          = "PDB"
      parent_target_key      = "cdb"
      database_resource_type = "pluggabledatabase"
      service_name           = "<PDB_LISTENER_SERVICE>"
      password_secret_id     = var.dbsnmp_secret_id
    }
  }
}
```

## Two-stage disable

```hcl
# Apply and verify PDB disable first.
enable_database_management = false
dbm_operation_stage        = "disable_pdb"
```

Run `terraform/scripts/observe_pdb_dbm_state.py` after the PDB apply to retain
a redaction-safe OCI CLI observation receipt (target-set digest, timestamp,
source, nonce, and observed-state evidence digest). It is operator evidence
only: the CDB plan deliberately does not accept a copied receipt. Instead it
uses fresh OCI provider data-source reads for each PDB and fails closed unless
the DBM feature is absent, `DISABLED`, or `NOT_ENABLED`.

```hcl
enable_database_management       = false
dbm_operation_stage              = "disable_cdb"
```

`disable_cdb` rejects forged, stale, unsigned, or otherwise copied receipt
values and is intentionally not an all-at-once Terraform operation.

## Upgrade from the previous single-resource release

Do not directly apply this release over state containing the old
`oci_database_cloud_database_management.compatibility` address. Terraform
cannot automatically split its `for_each` instances by database role. Back up
the approved encrypted state and move every existing instance to its reviewed
role-specific address before changing module source:

```sh
terraform state mv \
  'oci_database_cloud_database_management.compatibility["<CDB_KEY>"]' \
  'oci_database_cloud_database_management.compatibility_cdb["<CDB_KEY>"]'
terraform state mv \
  'oci_database_cloud_database_management.compatibility["<PDB_KEY>"]' \
  'oci_database_cloud_database_management.compatibility_pdb["<PDB_KEY>"]'
terraform state mv \
  'oci_database_cloud_database_management.compatibility["<NON_CDB_KEY>"]' \
  'oci_database_cloud_database_management.compatibility_standalone["<NON_CDB_KEY>"]'
```

Require a reviewed refresh-backed plan with no compatibility DBM
delete/create actions before running either operation stage.
