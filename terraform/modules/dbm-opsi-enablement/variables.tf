variable "compartment_id" {
  description = "Compartment OCID supplied by the caller; do not set a tenant default."
  type        = string
}

variable "dbm_private_endpoint_id" {
  description = "Existing DBM private endpoint OCID supplied by the caller."
  type        = string
}

variable "opsi_private_endpoint_id" {
  description = "Existing OPSI private endpoint OCID; required only when OPSI is enabled."
  type        = string
  default     = null
}

variable "enable_database_management" {
  description = "Master DBM intent. true is enable; false is permitted only in an explicit disable stage."
  type        = bool
  default     = true
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
  description = "Master OPSI toggle. false removes only Terraform-owned insights."
  type        = bool
  default     = true
}

variable "set_preferred_credentials" {
  description = "Create Vault-backed named credentials; preferred-credential assignment remains a reviewed lifecycle operation."
  type        = bool
  default     = true
}

variable "enable_all_current_pdbs" {
  description = "CDB-only enable control; never use it as a disable shortcut."
  type        = bool
  default     = false
}

variable "auto_enable_future_pdbs" {
  description = "CDB-only enable control; disabled automatically outside the enable stage."
  type        = bool
  default     = false
}

variable "disable_all_pdbs_with_cdb" {
  description = "Retained for compatibility but forbidden in disable_cdb; PDBs must be disabled in disable_pdb first."
  type        = bool
  default     = false
}

variable "lifecycle_id" {
  description = "Required ownership/lifecycle tag value; no tenant-specific default is provided."
  type        = string
}

variable "owner_tag" {
  description = "Required accountable owner tag, such as a team or service label."
  type        = string
}

variable "additional_freeform_tags" {
  description = "Additional non-sensitive ownership tags."
  type        = map(string)
  default     = {}
}

variable "targets" {
  description = "Sanitized target descriptors. Passwords, host IPs, and tenant defaults are deliberately not accepted."
  type = map(object({
    database_id                = string
    managed_database_name      = optional(string, "") # exact OCI Managed Database identity for live disable observation
    database_role              = string               # CDB | PDB | NON_CDB
    database_resource_type     = string               # database | pluggabledatabase
    service_name               = string
    password_secret_id         = string # Vault reference, never a plaintext password
    monitoring_user            = optional(string, "DBSNMP")
    role                       = optional(string, "NORMAL")
    protocol                   = optional(string, "TCP")
    port                       = optional(number, 1521)
    management_type            = optional(string, "ADVANCED")
    parent_target_key          = optional(string, "")
    enable_database_management = optional(bool, true)
    enable_ops_insights        = optional(bool, true)
  }))

  validation {
    condition     = alltrue([for target in values(var.targets) : contains(["CDB", "PDB", "NON_CDB"], target.database_role)])
    error_message = "database_role must be CDB, PDB, or NON_CDB."
  }

  validation {
    condition     = alltrue([for target in values(var.targets) : target.database_role != "PDB" || trimspace(target.managed_database_name) != ""])
    error_message = "Every PDB target must provide managed_database_name for the authoritative disable_cdb observation."
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
    condition = alltrue([
      for target in values(var.targets) : target.database_role != "CDB" || trimspace(target.parent_target_key) == ""
    ])
    error_message = "A CDB target must not declare parent_target_key."
  }

  validation {
    condition     = alltrue([for target in values(var.targets) : target.protocol == "TCP" && target.port >= 1 && target.port <= 65535])
    error_message = "Every target must use explicit TCP and a listener port between 1 and 65535."
  }

  validation {
    condition     = alltrue([for target in values(var.targets) : contains(["NORMAL", "SYSDBA", "SYSOPER", "SYSASM"], target.role)])
    error_message = "role must be an OCI-supported database connection role."
  }

  validation {
    condition     = alltrue([for target in values(var.targets) : length(trimspace(target.password_secret_id)) > 0])
    error_message = "Every production target must provide a non-empty Vault password_secret_id."
  }
}
