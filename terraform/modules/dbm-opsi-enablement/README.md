# dbm-opsi-enablement

OCI Database Management (canonical `database_dbm_features_management`) and
optional Operations Insights for reviewed database targets. It accepts Vault
secret references only: never put a database password, host address, or
tenant default in this module or its state.

## CDB/PDB ordering contract

`targets` has an explicit role model: a `PDB` must declare a
`parent_target_key` that names a `CDB` in the same map. The module gives the
PDB DBM resource a Terraform graph dependency on the CDB DBM resource, so an
enable apply completes the CDB operation before every PDB operation.

Terraform cannot safely reverse that in-place update edge. A disable is
therefore deliberately fail-closed and requires two applies:

1. `disable_pdb` sends only PDB DBM disable updates and leaves CDB DBM on.
   Apply it, then run the Terraform-owned observer below for a redaction-safe
   change record.
2. `disable_cdb` performs a fresh OCI provider read during its own plan for
   every PDB's `DIAGNOSTICS_AND_MANAGEMENT` feature. It fails closed unless
   each PDB resolves uniquely and is absent, `DISABLED`, or `NOT_ENABLED`.
   Copied, recomputed, stale, and unsigned receipts are rejected; it never
   uses `disable_all_pdbs_with_cdb` as a shortcut.

Resource addresses are role-stable (`dbm_cdb`, `dbm_pdb`, and
`dbm_standalone`), so changing stage is an explicit provider update rather
than a destroy/recreate transition.

## Upgrade from the previous single-resource release

Do not apply this release directly to a state that contains the older
`oci_database_management_database_dbm_features_management.dbm` address.
Terraform cannot infer how to split one `for_each` resource into the three
role-specific addresses. Before changing the module source, take the approved
encrypted-state backup and use reviewed `terraform state mv` commands for each
existing target, for example:

```sh
terraform state mv \
  'oci_database_management_database_dbm_features_management.dbm["<CDB_KEY>"]' \
  'oci_database_management_database_dbm_features_management.dbm_cdb["<CDB_KEY>"]'
terraform state mv \
  'oci_database_management_database_dbm_features_management.dbm["<PDB_KEY>"]' \
  'oci_database_management_database_dbm_features_management.dbm_pdb["<PDB_KEY>"]'
terraform state mv \
  'oci_database_management_database_dbm_features_management.dbm["<NON_CDB_KEY>"]' \
  'oci_database_management_database_dbm_features_management.dbm_standalone["<NON_CDB_KEY>"]'
```

Run a reviewed refresh-backed plan after the moves. It must show no DBM
delete/create actions before an enable or staged-disable operation proceeds.

## Enable example

```hcl
module "observability" {
  source = "../../modules/dbm-opsi-enablement"

  compartment_id          = var.compartment_id
  dbm_private_endpoint_id = var.dbm_private_endpoint_id
  lifecycle_id            = "<REVIEWED_LIFECYCLE_ID>"
  owner_tag               = "database-platform"

  targets = {
    cdb = {
      database_id            = var.cdb_id
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

Use the default `dbm_operation_stage = "enable"` with
`enable_database_management = true`.

## Two-stage disable example

Keep the same reviewed target map and ownership values for both applies.

```hcl
# First apply: only PDB DBM is disabled; CDB stays on.
enable_database_management = false
dbm_operation_stage        = "disable_pdb"
```

After apply, create an independent operator receipt with the repository-owned
observer. Its output has a target-set digest, completion timestamp, OCI CLI
source, random nonce, and a digest of the observed feature statuses; it never
prints database IDs or secrets.

```sh
python3 terraform/scripts/observe_pdb_dbm_state.py \
  --compartment-id "<COMPARTMENT_ID>" \
  --lifecycle-id "<REVIEWED_LIFECYCLE_ID>" \
  --targets-file /secure/reviewed-pdb-targets.json
```

The ignored targets file is a JSON map whose PDB values contain only
`database_id` and `managed_database_name`. Retain this receipt with the change
record, but do **not** feed it to Terraform: the CDB plan performs its own live
provider observation and rejects all copied receipt values.

```hcl
# Second apply: live-observation-gated CDB disable.
enable_database_management = false
dbm_operation_stage        = "disable_cdb"
disable_all_pdbs_with_cdb  = false
```

## Prerequisites

- Existing DBM private endpoint and, for OPSI, OPSI private endpoint.
- A Vault secret reference for every target monitoring account.
- A monitoring user that can connect to each supplied listener service.
- IAM allowing the DBM managed database principal to read each Vault secret.

Run `terraform fmt -recursive terraform` and `terraform validate` before a
reviewed plan. Do not commit a plan binary, state, credentials, or a
tenant-specific tfvars file.
