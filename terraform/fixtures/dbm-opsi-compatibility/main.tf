# Sanitized validation fixture only.  This is deliberately not a tfvars file
# and contains no tenant defaults, OCIDs, passwords, hosts, or state.
module "compatibility" {
  source = "../../modules/dbm-opsi-compatibility"

  compartment_id          = var.compartment_id
  dbm_private_endpoint_id = var.dbm_private_endpoint_id
  lifecycle_id            = var.lifecycle_id
  owner_tag               = var.owner_tag
  targets                 = var.targets
}
