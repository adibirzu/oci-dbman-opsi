variable "compartment_id" {
  description = "Compartment OCID that holds the databases, private endpoints, and Vault secret."
  type        = string
}

variable "dbm_private_endpoint_id" {
  description = "Database Management private endpoint OCID."
  type        = string
}

variable "opsi_private_endpoint_id" {
  description = "Operations Insights private endpoint OCID. Required when enable_ops_insights is true."
  type        = string
  default     = null
}

variable "password_secret_id" {
  description = "Vault secret OCID holding the monitoring user's password."
  type        = string
}

variable "monitoring_user" {
  description = "Database monitoring user."
  type        = string
  default     = "DBSNMP"
}

# --- Feature toggles: flip a feature on/off without touching the others. ---
variable "enable_database_management" {
  type    = bool
  default = true
}

variable "enable_ops_insights" {
  type    = bool
  default = true
}

variable "set_preferred_credentials" {
  description = "Create a Vault named credential and wire PC_READ/PC_WRITE for advanced diagnostics."
  type        = bool
  default     = true
}

variable "targets" {
  description = <<-EOT
    Enablement targets keyed by a short name (e.g. "cdb", "pdb1"). For OCI-native
    DBCS the managed-database OCID equals the database / pluggable-database OCID,
    so database_id is reused as the managed-database id. service_name must be the
    REAL listener service (db_unique_name.domain for the CDB, pdb_name.domain for
    a PDB) — the bare DB/PDB name causes ORA-12514.
  EOT
  type = map(object({
    database_id            = string
    database_role          = string # CDB | PDB | NON_CDB
    database_resource_type = string # DATABASE | PLUGGABLE_DATABASE
    service_name           = string
    host_ip                = string
    management_type        = optional(string, "ADVANCED")
  }))
}
