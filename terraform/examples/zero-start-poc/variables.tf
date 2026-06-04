variable "tenancy_ocid" {
  type        = string
  description = "OCI tenancy OCID."
}

variable "compartment_ocid" {
  type        = string
  description = "OCI compartment OCID for PoC resources."
}

variable "region" {
  type        = string
  description = "OCI region."
}

variable "create_test_network" {
  type        = bool
  description = "Create a PoC VCN/subnet."
  default     = false
}

variable "vcn_ocid" {
  type        = string
  description = "Existing VCN OCID."
  default     = null
}

variable "subnet_ocid" {
  type        = string
  description = "Existing subnet OCID."
  default     = null
}

variable "test_vcn_cidr" {
  type        = string
  description = "PoC VCN CIDR."
  default     = "10.44.0.0/16"
}

variable "test_subnet_cidr" {
  type        = string
  description = "PoC private subnet CIDR."
  default     = "10.44.10.0/24"
}

variable "create_vault" {
  type        = bool
  description = "Create a PoC vault/key."
  default     = false
}

variable "vault_ocid" {
  type        = string
  description = "Existing vault OCID."
  default     = null
}

variable "key_ocid" {
  type        = string
  description = "Existing key OCID."
  default     = null
}

variable "policy_name" {
  type        = string
  description = "IAM policy name."
}

variable "policy_description" {
  type        = string
  description = "IAM policy description."
}

variable "policy_statements" {
  type        = list(string)
  description = "IAM policy statements."
}

variable "targets" {
  type = list(object({
    kind            = string
    name            = string
    resource_id     = optional(string)
    provision       = bool
    management_type = string
  }))
  description = "Database targets selected by the wizard."
  default     = []
}

variable "ssh_public_keys" {
  type        = list(string)
  description = "SSH public keys for provisioned DBCS VM DB systems."
  default     = []
}

variable "db_admin_password" {
  type        = string
  description = "Admin password for provisioned DBCS databases. Pass through TF_VAR_db_admin_password."
  sensitive   = true
  default     = null
}

variable "adb_admin_password" {
  type        = string
  description = "Admin password for provisioned Autonomous Databases. Pass through TF_VAR_adb_admin_password."
  sensitive   = true
  default     = null
}

variable "dbcs_shape" {
  type        = string
  description = "DBCS shape for zero-start PoC provisioning."
  default     = "VM.Standard.E4.Flex"
}

variable "db_version" {
  type        = string
  description = "Oracle Database version for provisioned DBCS systems."
  default     = "19.0.0.0"
}

variable "adb_compute_count" {
  type        = number
  description = "ECPU/core count for provisioned Autonomous Databases."
  default     = 2
}

variable "adb_storage_tbs" {
  type        = number
  description = "Storage in TB for provisioned Autonomous Databases."
  default     = 1
}
