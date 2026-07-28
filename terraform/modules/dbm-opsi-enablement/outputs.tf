output "enablement_summary" {
  description = "Safe aggregate status only; does not expose IDs, secrets, service names, or topology."
  value = {
    dbm_target_count  = length(oci_database_management_database_dbm_features_management.dbm_cdb) + length(oci_database_management_database_dbm_features_management.dbm_pdb) + length(oci_database_management_database_dbm_features_management.dbm_standalone)
    opsi_target_count = length(oci_opsi_database_insight.insight)
    dbm_enabled       = var.dbm_operation_stage == "enable"
    opsi_enabled      = var.enable_ops_insights
    operation_stage   = var.dbm_operation_stage
  }
}

output "dbm_operation_receipt" {
  description = "Non-secret staged-operation metadata. disable_cdb does not consume this output: it performs a fresh OCI provider observation during its plan."
  value = {
    stage                            = var.dbm_operation_stage
    pdb_target_set_digest            = local.pdb_target_set_digest
    authoritative_observation_source = "oracle/oci managed_database data source"
    remaining_target_keys            = local.remaining_target_keys
    next_required_operation          = var.dbm_operation_stage == "disable_pdb" ? "run terraform/scripts/observe_pdb_dbm_state.py for operator evidence, then plan disable_cdb without a receipt" : null
  }
}
