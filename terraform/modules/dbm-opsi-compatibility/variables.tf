variable "compartment_id" { type = string }
variable "dbm_private_endpoint_id" { type = string }
variable "opsi_private_endpoint_id" {
  type    = string
  default = null
}
variable "lifecycle_id" { type = string }
variable "owner_tag" { type = string }

variable "enable_database_management" {
  description = "Master DBM intent. false is permitted only in an explicit staged disable."
  type        = bool
  default     = false
}

variable "dbm_operation_stage" {
  description = "DBM operation: enable, disable_pdb (apply first), or live-OCI-observation-gated disable_cdb."
  type        = string
  default     = "enable"

  validation {
    condition     = contains(["enable", "disable_pdb", "disable_cdb"], var.dbm_operation_stage)
    error_message = "dbm_operation_stage must be enable, disable_pdb, or disable_cdb."
  }
}

variable "pdb_disable_verification_receipt" {
  description = "Deprecated compatibility input. Must remain null: disable_cdb rejects copied receipts and reads current OCI PDB feature state during planning."
  type        = string
  default     = null
  sensitive   = true
}

variable "enable_ops_insights" {
  description = "Master compatibility OPSI toggle. false removes only Terraform-owned insights."
  type        = bool
  default     = false
}

variable "targets" {
  description = "Compatibility targets contain Vault references only; no plaintext password or host IP is accepted."
  type = map(object({
    database_id                = string
    managed_database_name      = string # exact post-enable DBM identity; never an OCID
    database_role              = string # CDB | PDB | NON_CDB
    parent_target_key          = optional(string, "")
    database_resource_type     = string
    service_name               = string
    password_secret_id         = string
    ssl_secret_id              = optional(string, null)
    monitoring_user            = optional(string, "DBSNMP")
    role                       = optional(string, "NORMAL")
    protocol                   = optional(string, "TCP")
    port                       = optional(number, 1521)
    management_type            = optional(string, "ADVANCED")
    enable_database_management = optional(bool, false)
    enable_ops_insights        = optional(bool, false)
  }))

  validation {
    condition     = alltrue([for target in values(var.targets) : contains(["CDB", "PDB", "NON_CDB"], target.database_role)])
    error_message = "database_role must be CDB, PDB, or NON_CDB."
  }

  validation {
    condition = alltrue([
      for key, target in var.targets : target.database_role != "PDB" || (
        trimspace(target.parent_target_key) != "" &&
        contains(keys(var.targets), target.parent_target_key) &&
        try(var.targets[target.parent_target_key].database_role, "") == "CDB" &&
        target.database_resource_type == "pluggabledatabase"
      )
    ])
    error_message = "Every PDB target must use database_resource_type=pluggabledatabase and name a CDB parent_target_key in this target map."
  }

  validation {
    condition     = alltrue([for target in values(var.targets) : target.database_role != "CDB" || trimspace(target.parent_target_key) == ""])
    error_message = "A CDB target must not declare parent_target_key."
  }

  validation {
    condition     = alltrue([for target in values(var.targets) : target.protocol == "TCP" && target.port >= 1 && target.port <= 65535])
    error_message = "Compatibility targets require explicit TCP and a listener port between 1 and 65535."
  }
}
