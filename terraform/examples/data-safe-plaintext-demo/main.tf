# DEMO ONLY. The Data Safe API requires a plaintext password and Terraform can
# therefore store it in state. Production registration belongs to the lifecycle
# service, which keeps credential material outside Terraform state.
resource "oci_data_safe_target_database" "demo" {
  for_each       = var.enable_plaintext_data_safe_demo ? var.targets : {}
  compartment_id = var.compartment_id
  display_name   = "${var.lifecycle_id}-${each.key}"
  freeform_tags = {
    "dbman_opsi_owner"        = var.owner_tag
    "dbman_opsi_lifecycle_id" = var.lifecycle_id
    "dbman_opsi_demo_only"    = "true"
  }

  database_details {
    database_type       = "DATABASE_CLOUD_SERVICE"
    infrastructure_type = "ORACLE_CLOUD"
    db_system_id        = each.value.db_system_id
    service_name        = each.value.service_name
    listener_port       = each.value.port
  }

  connection_option {
    connection_type              = "PRIVATE_ENDPOINT"
    datasafe_private_endpoint_id = var.data_safe_private_endpoint_id
  }

  credentials {
    user_name = each.value.monitoring_user
    password  = var.data_safe_password
  }
}

check "plaintext_demo_use_gate" {
  assert {
    condition     = !var.enable_plaintext_data_safe_demo || var.allow_plaintext_data_safe_demo
    error_message = "DEMO ONLY: set allow_plaintext_data_safe_demo=true only with a disposable, access-restricted Terraform state backend."
  }
}
