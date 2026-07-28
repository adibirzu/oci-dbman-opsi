resource "oci_identity_policy" "dbman_opsi" {
  count          = var.create_identity_policy ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = var.policy_name
  description    = var.policy_description
  statements     = var.policy_statements

  lifecycle {
    precondition {
      condition     = length(var.policy_statements) > 0
      error_message = "create_identity_policy=true requires explicit, reviewed policy_statements."
    }
  }
}

locals {
  name_suffix = substr(sha256(var.demo_lifecycle_id), 0, 8)
  lifecycle_tags = {
    "dbman_opsi_lifecycle"       = var.demo_lifecycle_id
    "dbman_opsi_mode"            = var.deployment_mode
    "dbman_opsi_disposable"      = var.deployment_mode == "production" ? "false" : "true"
    "dbman_opsi_evidence_retain" = "${var.evidence_retention_days}d"
    "dbman_opsi_managed_by"      = "oci-resource-manager"
  }
  database_service_selected = length(setintersection(var.demo_services, toset(["dbm", "opsi", "datasafe"]))) > 0
  dbm_endpoint_required     = length(setintersection(var.demo_services, toset(["dbm", "opsi"]))) > 0
}

resource "terraform_data" "stack_contract" {
  input = {
    deployment_mode   = var.deployment_mode
    lifecycle_id      = var.demo_lifecycle_id
    selected_services = sort(tolist(var.demo_services))
  }

  lifecycle {
    precondition {
      condition     = var.deployment_mode != "production" || !var.create_test_network
      error_message = "production mode forbids create_test_network; select an existing reviewed VCN and subnet."
    }
    precondition {
      condition     = var.create_test_network || (var.vcn_ocid != null && var.subnet_ocid != null)
      error_message = "When create_test_network=false, both vcn_ocid and subnet_ocid are required."
    }
    precondition {
      condition     = !var.create_test_network || (var.test_vcn_cidr != null && var.test_subnet_cidr != null)
      error_message = "When create_test_network=true, both test_vcn_cidr and test_subnet_cidr are required."
    }
    precondition {
      condition     = !local.database_service_selected || var.create_vault || (var.vault_ocid != null && var.key_ocid != null)
      error_message = "DBM, OPSI, and Data Safe require either a lifecycle-owned Vault/key or reviewed existing vault_ocid and key_ocid values."
    }
    precondition {
      condition     = !var.create_data_safe_private_endpoint || contains(var.demo_services, "datasafe")
      error_message = "create_data_safe_private_endpoint requires datasafe in demo_services."
    }
    precondition {
      condition     = !var.enable_observability || length(var.observability_targets) > 0
      error_message = "enable_observability=true requires at least one reviewed observability target."
    }
    precondition {
      condition     = !var.enable_observability || local.dbm_endpoint_required
      error_message = "enable_observability=true requires dbm or opsi in demo_services."
    }
    precondition {
      condition = !var.enable_observability || alltrue([
        for target in values(var.observability_targets) :
        target.password_secret_id != null || var.dbsnmp_secret_id != null
      ])
      error_message = "Every observability target requires a Vault secret reference, either per target or through dbsnmp_secret_id."
    }
  }
}

resource "oci_core_vcn" "test" {
  count          = var.create_test_network ? 1 : 0
  compartment_id = var.compartment_ocid
  cidr_block     = var.test_vcn_cidr
  display_name   = "dbman-opsi-vcn-${local.name_suffix}"
  dns_label      = "dbops${local.name_suffix}"
  freeform_tags  = local.lifecycle_tags

  depends_on = [terraform_data.stack_contract]
}

# The private subnet that hosts the Database Management / Ops Insights private
# endpoints must reach OCI services. Without a Service Gateway + route rule the
# endpoints create successfully but collection silently fails.
data "oci_core_services" "all" {
  count = var.create_test_network ? 1 : 0
}

locals {
  oci_all_services = var.create_test_network ? [
    for svc in data.oci_core_services.all[0].services :
    svc if can(regex("all-.*-services-in-oracle-services-network", svc.cidr_block))
  ] : []
}

resource "oci_core_service_gateway" "test" {
  count          = var.create_test_network ? 1 : 0
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.test[0].id
  display_name   = "dbman-opsi-sgw-${local.name_suffix}"
  freeform_tags  = local.lifecycle_tags

  services {
    service_id = local.oci_all_services[0].id
  }
}

resource "oci_core_route_table" "test" {
  count          = var.create_test_network ? 1 : 0
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.test[0].id
  display_name   = "dbman-opsi-rt-${local.name_suffix}"
  freeform_tags  = local.lifecycle_tags

  route_rules {
    destination       = local.oci_all_services[0].cidr_block
    destination_type  = "SERVICE_CIDR_BLOCK"
    network_entity_id = oci_core_service_gateway.test[0].id
  }
}

resource "oci_core_security_list" "test" {
  count          = var.create_test_network ? 1 : 0
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.test[0].id
  display_name   = "dbman-opsi-sl-${local.name_suffix}"
  freeform_tags  = local.lifecycle_tags

  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }

  # Oracle listener ports for monitoring connections within the subnet.
  ingress_security_rules {
    protocol = "6" # TCP
    source   = var.test_subnet_cidr

    tcp_options {
      min = 1521
      max = 1522
    }
  }

  # OCI Bastion reaches the private jump host inside this disposable VCN. Keep
  # SSH scoped to the demo VCN; never expose port 22 to the internet.
  ingress_security_rules {
    protocol = "6"
    source   = var.test_vcn_cidr

    tcp_options {
      min = 22
      max = 22
    }
  }
}

resource "oci_core_subnet" "test_private" {
  count                      = var.create_test_network ? 1 : 0
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.test[0].id
  cidr_block                 = var.test_subnet_cidr
  display_name               = "dbman-opsi-private-subnet-${local.name_suffix}"
  prohibit_public_ip_on_vnic = true
  dns_label                  = "dbm${local.name_suffix}"
  route_table_id             = oci_core_route_table.test[0].id
  security_list_ids          = [oci_core_security_list.test[0].id]
  freeform_tags              = local.lifecycle_tags
}

locals {
  selected_vcn_id    = var.create_test_network ? oci_core_vcn.test[0].id : var.vcn_ocid
  selected_subnet_id = var.create_test_network ? oci_core_subnet.test_private[0].id : var.subnet_ocid
  logan_targets      = { for target in var.targets : target.name => target if contains(try(target.services, []), "logan") }
}

resource "oci_database_management_db_management_private_endpoint" "dbmgmt" {
  count          = local.dbm_endpoint_required && var.dbm_private_endpoint_id == null ? 1 : 0
  compartment_id = var.compartment_ocid
  name           = "dbman_opsi_dbmgmt_pe_${local.name_suffix}"
  subnet_id      = local.selected_subnet_id
  description    = "Database Management private endpoint managed by dbman-opsi."
  freeform_tags  = local.lifecycle_tags

  depends_on = [terraform_data.stack_contract]
}

resource "oci_opsi_operations_insights_private_endpoint" "opsi" {
  count               = contains(var.demo_services, "opsi") && var.opsi_private_endpoint_id == null ? 1 : 0
  compartment_id      = var.compartment_ocid
  display_name        = "dbman-opsi-opsi-pe-${local.name_suffix}"
  description         = "Operations Insights private endpoint managed by dbman-opsi."
  vcn_id              = local.selected_vcn_id
  subnet_id           = local.selected_subnet_id
  is_used_for_rac_dbs = false
  freeform_tags       = local.lifecycle_tags

  depends_on = [terraform_data.stack_contract]
}

resource "oci_data_safe_data_safe_private_endpoint" "datasafe" {
  count          = contains(var.demo_services, "datasafe") && var.create_data_safe_private_endpoint && var.data_safe_private_endpoint_id == null ? 1 : 0
  compartment_id = var.compartment_ocid
  display_name   = "dbman-opsi-datasafe-pe-${local.name_suffix}"
  description    = "Data Safe private endpoint managed by dbman-opsi."
  vcn_id         = local.selected_vcn_id
  subnet_id      = local.selected_subnet_id
  freeform_tags  = local.lifecycle_tags

  depends_on = [terraform_data.stack_contract]
}

resource "oci_kms_vault" "test" {
  count          = var.create_vault ? 1 : 0
  compartment_id = var.compartment_ocid
  display_name   = "dbman-opsi-vault-${local.name_suffix}"
  vault_type     = "DEFAULT"
  freeform_tags  = local.lifecycle_tags
}

resource "oci_kms_key" "demo" {
  count               = var.create_vault && var.key_ocid == null ? 1 : 0
  compartment_id      = var.compartment_ocid
  display_name        = "dbman-opsi-key-${local.name_suffix}"
  management_endpoint = oci_kms_vault.test[0].management_endpoint
  key_shape {
    algorithm = "AES"
    length    = 32
  }
  freeform_tags = local.lifecycle_tags
}

output "vcn_ocid" {
  value = local.selected_vcn_id
}

output "subnet_ocid" {
  value = local.selected_subnet_id
}

output "db_management_private_endpoint_ocid" {
  value = var.dbm_private_endpoint_id != null ? var.dbm_private_endpoint_id : try(oci_database_management_db_management_private_endpoint.dbmgmt[0].id, null)
}

output "opsi_private_endpoint_ocid" {
  value = var.opsi_private_endpoint_id != null ? var.opsi_private_endpoint_id : try(oci_opsi_operations_insights_private_endpoint.opsi[0].id, null)
}

output "data_safe_private_endpoint_ocid" {
  value = var.data_safe_private_endpoint_id != null ? var.data_safe_private_endpoint_id : try(oci_data_safe_data_safe_private_endpoint.datasafe[0].id, null)
}

output "service_gateway_ocid" {
  value = var.create_test_network ? oci_core_service_gateway.test[0].id : null
}

output "provisioned_dbcs_ids" {
  description = "Provisioning is disabled in this public Terraform stack because OCI database admin passwords would enter state."
  value       = {}
}

output "provisioned_autonomous_database_ids" {
  description = "Provisioning is disabled in this public Terraform stack because OCI database admin passwords would enter state."
  value       = {}
}

output "log_analytics_namespace" {
  value = var.enable_log_analytics ? var.log_analytics_namespace : null
}

output "log_analytics_log_group_ocid" {
  value = var.enable_log_analytics ? var.log_analytics_log_group_ocid : null
}

output "log_analytics_entity_ids" {
  value = {
    for name, target in local.logan_targets : name => {
      database = target.logan_database_entity_id
      host     = target.logan_host_entity_id
      adb      = target.logan_adb_entity_id
    }
  }
}

output "log_analytics_collector_instance_id" {
  value = null
}

output "log_analytics_collector_private_ip" {
  value = null
}

output "disposable_lifecycle" {
  description = "Non-secret scope for safe teardown and evidence retention verification."
  value = {
    lifecycle_id            = var.demo_lifecycle_id
    deployment_mode         = var.deployment_mode
    selected_services       = sort(tolist(var.demo_services))
    evidence_retention_days = var.evidence_retention_days
    destroy_command         = "terraform destroy -var='demo_lifecycle_id=${var.demo_lifecycle_id}'"
  }
}

output "vault_ocid" {
  value = var.create_vault ? oci_kms_vault.test[0].id : var.vault_ocid
}

output "key_ocid" {
  value = var.create_vault ? oci_kms_key.demo[0].id : var.key_ocid
}

# Optional DBM + OPSI enablement (modular, off by default).  Target descriptors
# contain service names and Vault references only; DB node IPs are never placed
# in Terraform variables or state.
module "observability" {
  count  = var.enable_observability ? 1 : 0
  source = "./modules/dbm-opsi-enablement"

  compartment_id = var.compartment_ocid
  dbm_private_endpoint_id = (
    var.dbm_private_endpoint_id != null
    ? var.dbm_private_endpoint_id
    : oci_database_management_db_management_private_endpoint.dbmgmt[0].id
  )
  opsi_private_endpoint_id = (
    var.opsi_private_endpoint_id != null
    ? var.opsi_private_endpoint_id
    : try(oci_opsi_operations_insights_private_endpoint.opsi[0].id, null)
  )
  enable_ops_insights = contains(var.demo_services, "opsi")
  lifecycle_id        = var.demo_lifecycle_id
  owner_tag           = "dbman-opsi-demo"
  targets = {
    for name, target in var.observability_targets : name => merge(target, {
      password_secret_id = target.password_secret_id != null ? target.password_secret_id : var.dbsnmp_secret_id
    })
  }
}
