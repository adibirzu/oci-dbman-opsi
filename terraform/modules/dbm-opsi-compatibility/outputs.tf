output "compatibility_summary" {
  description = "Safe aggregate status only; post-enable managed-database data is never output."
  value = {
    management_target_count = length(oci_database_cloud_database_management.compatibility_cdb) + length(oci_database_cloud_database_management.compatibility_pdb) + length(oci_database_cloud_database_management.compatibility_standalone)
    managed_database_reads  = length(data.oci_database_management_managed_database.post_enable)
    opsi_target_count       = length(oci_opsi_database_insight.insight)
    operation_stage         = var.dbm_operation_stage
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
