# Opt-in adapter for estates that require the legacy cloud-database DBM API.
# It keeps the canonical module's role model, Vault-only credentials, and
# staged CDB/PDB ordering contract.

locals {
  cdb_targets                = { for name, target in var.targets : name => target if target.database_role == "CDB" }
  pdb_targets                = { for name, target in var.targets : name => target if target.database_role == "PDB" }
  standalone_targets         = { for name, target in var.targets : name => target if target.database_role == "NON_CDB" }
  enabled_management_targets = var.dbm_operation_stage == "enable" ? var.targets : {}
  opsi_targets = {
    for name, target in var.targets : name => target
    if var.enable_ops_insights && var.opsi_private_endpoint_id != null && target.enable_ops_insights
  }
  ownership_tags = {
    "dbman_opsi_owner"        = var.owner_tag
    "dbman_opsi_lifecycle_id" = var.lifecycle_id
    "dbman_opsi_managed_by"   = "terraform"
  }
  pdb_observation_targets = var.dbm_operation_stage == "disable_cdb" ? local.pdb_targets : {}
  pdb_target_set = {
    for key, target in local.pdb_targets : key => {
      database_id           = target.database_id
      managed_database_name = target.managed_database_name
    }
  }
  pdb_target_set_digest = sha256(jsonencode({
    lifecycle_id = var.lifecycle_id
    targets      = local.pdb_target_set
  }))
  remaining_target_keys = var.dbm_operation_stage == "disable_pdb" ? sort(keys(local.cdb_targets)) : []
  post_enable_matches = {
    for target, lookup in data.oci_database_management_managed_databases.post_enable_collection : target => [
      for item in lookup.managed_database_collection[0].items : item
      if item.name == local.enabled_management_targets[target].managed_database_name && item.compartment_id == var.compartment_id
    ]
  }
  unique_managed_database_ids = {
    for target, matches in local.post_enable_matches : target => one(matches).id
    if length(matches) == 1
  }
}

resource "terraform_data" "operation_contract" {
  input = {
    stage                 = var.dbm_operation_stage
    remaining_target_keys = local.remaining_target_keys
  }

  lifecycle {
    precondition {
      condition     = var.dbm_operation_stage != "enable" || var.enable_database_management
      error_message = "enable requires enable_database_management=true; use disable_pdb then disable_cdb for an explicit disable."
    }
    precondition {
      condition     = var.dbm_operation_stage != "enable" || alltrue([for target in values(var.targets) : target.enable_database_management])
      error_message = "enable rejects per-target enable_database_management=false; scope an explicit disable_pdb/disable_cdb operation to the reviewed target map instead."
    }
    precondition {
      condition     = !contains(["disable_pdb", "disable_cdb"], var.dbm_operation_stage) || !var.enable_database_management
      error_message = "disable_pdb and disable_cdb require enable_database_management=false so an accidental all-at-once enable/disable plan is rejected."
    }
    precondition {
      condition     = var.pdb_disable_verification_receipt == null
      error_message = "disable_cdb does not accept a copied receipt. It performs a fresh authoritative OCI provider observation of every PDB DBM feature during this plan."
    }
  }
}

data "oci_database_management_managed_databases" "pdb_disable_collection" {
  for_each       = local.pdb_observation_targets
  compartment_id = var.compartment_id
  name           = each.value.managed_database_name
}

locals {
  pdb_disable_matches = {
    for key, lookup in data.oci_database_management_managed_databases.pdb_disable_collection : key => [
      for item in lookup.managed_database_collection[0].items : item
      if item.name == local.pdb_observation_targets[key].managed_database_name && item.compartment_id == var.compartment_id
    ]
  }
  pdb_disable_managed_database_ids = {
    for key, matches in local.pdb_disable_matches : key => one(matches).id
    if length(matches) == 1
  }
}

data "oci_database_management_managed_database" "pdb_disable_observation" {
  for_each            = local.pdb_disable_managed_database_ids
  managed_database_id = each.value
}

resource "terraform_data" "pdb_disable_observation" {
  count = var.dbm_operation_stage == "disable_cdb" ? 1 : 0
  input = {
    target_set_digest = local.pdb_target_set_digest
    observer          = "oracle/oci managed_database data source"
  }

  lifecycle {
    precondition {
      condition     = alltrue([for matches in values(local.pdb_disable_matches) : length(matches) == 1])
      error_message = "disable_cdb requires exactly one Managed Database observation for every PDB in the reviewed target set."
    }
    precondition {
      condition = alltrue([
        for observed in values(data.oci_database_management_managed_database.pdb_disable_observation) : alltrue([
          for feature in observed.dbmgmt_feature_configs : feature.feature != "DIAGNOSTICS_AND_MANAGEMENT" || contains(["DISABLED", "NOT_ENABLED"], upper(feature.feature_status))
        ])
      ])
      error_message = "disable_cdb requires every observed PDB DIAGNOSTICS_AND_MANAGEMENT feature to be absent, DISABLED, or NOT_ENABLED."
    }
  }
}

resource "oci_database_cloud_database_management" "compatibility_cdb" {
  for_each             = local.cdb_targets
  database_id          = each.value.database_id
  enable_management    = var.dbm_operation_stage != "disable_cdb"
  management_type      = each.value.management_type
  private_end_point_id = var.dbm_private_endpoint_id
  protocol             = each.value.protocol
  port                 = each.value.port
  role                 = each.value.role
  service_name         = each.value.service_name
  ssl_secret_id        = each.value.ssl_secret_id

  credentialdetails {
    user_name          = each.value.monitoring_user
    password_secret_id = each.value.password_secret_id
  }

  depends_on = [
    terraform_data.operation_contract,
    terraform_data.pdb_disable_observation,
  ]
}

resource "oci_database_cloud_database_management" "compatibility_pdb" {
  for_each             = local.pdb_targets
  database_id          = each.value.database_id
  enable_management    = var.dbm_operation_stage == "enable"
  management_type      = each.value.management_type
  private_end_point_id = var.dbm_private_endpoint_id
  protocol             = each.value.protocol
  port                 = each.value.port
  role                 = each.value.role
  service_name         = each.value.service_name
  ssl_secret_id        = each.value.ssl_secret_id

  credentialdetails {
    user_name          = each.value.monitoring_user
    password_secret_id = each.value.password_secret_id
  }

  depends_on = [oci_database_cloud_database_management.compatibility_cdb]
}

resource "oci_database_cloud_database_management" "compatibility_standalone" {
  for_each             = local.standalone_targets
  database_id          = each.value.database_id
  enable_management    = var.dbm_operation_stage == "enable"
  management_type      = each.value.management_type
  private_end_point_id = var.dbm_private_endpoint_id
  protocol             = each.value.protocol
  port                 = each.value.port
  role                 = each.value.role
  service_name         = each.value.service_name
  ssl_secret_id        = each.value.ssl_secret_id

  credentialdetails {
    user_name          = each.value.monitoring_user
    password_secret_id = each.value.password_secret_id
  }

  depends_on = [terraform_data.operation_contract]
}

# Resolve the managed DB identity only after enablement. The collection is
# constrained by the caller's compartment and exact managed database name, and
# terraform_data rejects zero or ambiguous matches before the singular read.
data "oci_database_management_managed_databases" "post_enable_collection" {
  for_each       = local.enabled_management_targets
  compartment_id = var.compartment_id
  name           = each.value.managed_database_name

  depends_on = [
    oci_database_cloud_database_management.compatibility_cdb,
    oci_database_cloud_database_management.compatibility_pdb,
    oci_database_cloud_database_management.compatibility_standalone,
  ]
}

resource "terraform_data" "post_enable_match" {
  for_each = local.post_enable_matches
  input    = each.key

  lifecycle {
    precondition {
      condition     = length(each.value) == 1
      error_message = "Post-enable Managed Database lookup must return exactly one identity for each compatibility target."
    }
  }
}

data "oci_database_management_managed_database" "post_enable" {
  for_each            = local.unique_managed_database_ids
  managed_database_id = each.value

  depends_on = [terraform_data.post_enable_match]
}

resource "oci_opsi_database_insight" "insight" {
  for_each                 = local.opsi_targets
  compartment_id           = var.compartment_id
  entity_source            = "PE_COMANAGED_DATABASE"
  database_id              = each.value.database_id
  database_resource_type   = each.value.database_resource_type
  dbm_private_endpoint_id  = var.dbm_private_endpoint_id
  opsi_private_endpoint_id = var.opsi_private_endpoint_id
  freeform_tags            = local.ownership_tags

  credential_details {
    credential_type    = "CREDENTIALS_BY_VAULT"
    user_name          = each.value.monitoring_user
    role               = each.value.role
    password_secret_id = each.value.password_secret_id
  }

  depends_on = [
    oci_database_cloud_database_management.compatibility_cdb,
    oci_database_cloud_database_management.compatibility_pdb,
    oci_database_cloud_database_management.compatibility_standalone,
  ]
}
