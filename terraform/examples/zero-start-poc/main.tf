resource "oci_identity_policy" "dbman_opsi" {
  count          = var.create_identity_policy ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = var.policy_name
  description    = var.policy_description
  statements     = var.policy_statements
}

locals {
  lifecycle_tags = {
    "dbman_opsi_lifecycle"       = var.demo_lifecycle_id
    "dbman_opsi_disposable"      = "true"
    "dbman_opsi_evidence_retain" = "${var.evidence_retention_days}d"
  }
}

resource "oci_core_vcn" "test" {
  count          = var.create_test_network ? 1 : 0
  compartment_id = var.compartment_ocid
  cidr_block     = var.test_vcn_cidr
  display_name   = "dbman-opsi-vcn"
  dns_label      = "dbopsdemo"
  freeform_tags  = local.lifecycle_tags
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
  display_name   = "dbman-opsi-sgw"
  freeform_tags  = local.lifecycle_tags

  services {
    service_id = local.oci_all_services[0].id
  }
}

resource "oci_core_route_table" "test" {
  count          = var.create_test_network ? 1 : 0
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.test[0].id
  display_name   = "dbman-opsi-rt"
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
  display_name   = "dbman-opsi-sl"
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
  display_name               = "dbman-opsi-private-subnet"
  prohibit_public_ip_on_vnic = true
  dns_label                  = "dbmopsi"
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
  compartment_id = var.compartment_ocid
  name           = "dbman_opsi_dbmgmt_pe"
  subnet_id      = local.selected_subnet_id
  description    = "Database Management private endpoint for dbman-opsi PoC."
  freeform_tags  = local.lifecycle_tags
}

resource "oci_kms_vault" "test" {
  count          = var.create_vault ? 1 : 0
  compartment_id = var.compartment_ocid
  display_name   = "dbman-opsi-vault"
  vault_type     = "DEFAULT"
  freeform_tags  = local.lifecycle_tags
}

resource "oci_kms_key" "demo" {
  count               = var.create_vault && var.key_ocid == null ? 1 : 0
  compartment_id      = var.compartment_ocid
  display_name        = "dbman-opsi-demo-key"
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
  value = oci_database_management_db_management_private_endpoint.dbmgmt.id
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
  source = "../../modules/dbm-opsi-enablement"

  compartment_id           = var.compartment_ocid
  dbm_private_endpoint_id  = oci_database_management_db_management_private_endpoint.dbmgmt.id
  opsi_private_endpoint_id = var.opsi_private_endpoint_id
  enable_ops_insights      = var.opsi_private_endpoint_id != null
  lifecycle_id             = var.demo_lifecycle_id
  owner_tag                = "dbman-opsi-demo"
  targets = {
    for name, target in var.observability_targets : name => merge(target, {
      password_secret_id = target.password_secret_id != null ? target.password_secret_id : var.dbsnmp_secret_id
    })
  }
}
