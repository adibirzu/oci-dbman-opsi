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

variable "deployment_mode" {
  type        = string
  description = "Lifecycle policy mode for this stack: poc, demo, or production."
  default     = "poc"

  validation {
    condition     = contains(["poc", "demo", "production"], var.deployment_mode)
    error_message = "deployment_mode must be poc, demo, or production."
  }
}

variable "demo_lifecycle_id" {
  type        = string
  description = "Required project-owned lifecycle tag value used to discover and destroy only disposable demo resources."
  default     = "dbman-opsi-disposable"

  validation {
    condition     = length(trimspace(var.demo_lifecycle_id)) >= 8
    error_message = "demo_lifecycle_id must be a unique, non-empty ownership identifier of at least eight characters."
  }
}

variable "evidence_retention_days" {
  type        = number
  description = "Retention target for sanitized Log Analytics evidence after teardown. OCI retention configuration is applied by the Log Analytics workflow."
  default     = 7

  validation {
    condition     = var.evidence_retention_days == 7
    error_message = "The disposable release retains sanitized evidence for exactly seven days."
  }
}

variable "demo_services" {
  type        = set(string)
  description = "Demo pillars selected in Resource Manager: dbm, opsi, datasafe, logan. Database-side bootstrap is still required before enablement."
  default     = ["dbm", "opsi", "datasafe", "logan"]

  validation {
    condition     = length(setsubtract(var.demo_services, toset(["dbm", "opsi", "datasafe", "logan"]))) == 0
    error_message = "demo_services may contain only dbm, opsi, datasafe, and logan."
  }
}

variable "create_test_network" {
  type        = bool
  description = "Create a lifecycle-owned PoC/demo VCN and private subnet. Forbidden in production mode."
  default     = true
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
  default     = null

  validation {
    condition     = var.test_vcn_cidr == null || can(cidrnetmask(var.test_vcn_cidr))
    error_message = "test_vcn_cidr must be a valid IPv4 CIDR."
  }
}

variable "test_subnet_cidr" {
  type        = string
  description = "PoC private subnet CIDR."
  default     = null

  validation {
    condition     = var.test_subnet_cidr == null || can(cidrnetmask(var.test_subnet_cidr))
    error_message = "test_subnet_cidr must be a valid IPv4 CIDR."
  }
}

variable "create_vault" {
  type        = bool
  description = "Create a PoC vault/key."
  default     = true
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
  default     = "dbman-opsi-enable-policy"
}

variable "policy_description" {
  type        = string
  description = "IAM policy description."
  default     = "Database observability enablement policy managed by dbman-opsi."
}

variable "policy_statements" {
  type        = list(string)
  description = "IAM policy statements."
  default     = []
}

variable "create_identity_policy" {
  type        = bool
  description = "Create an explicitly supplied IAM policy. Resource Manager defaults this off so reviewed IAM remains owner-managed."
  default     = false
}

variable "targets" {
  type = list(object({
    kind                      = string
    name                      = string
    resource_id               = optional(string)
    provision                 = bool
    management_type           = string
    services                  = optional(list(string), [])
    logan_database_entity_id  = optional(string)
    logan_host_entity_id      = optional(string)
    logan_adb_entity_id       = optional(string)
    logan_management_agent_id = optional(string)
  }))
  description = "Database targets selected by the wizard."
  default     = []
}

variable "enable_log_analytics" {
  type        = bool
  description = "Enable Log Analytics add-on intent. Source associations are managed by dbman-opsi CLI payloads."
  default     = false
}

variable "log_analytics_namespace" {
  type        = string
  description = "Existing Log Analytics namespace. Leave null to resolve/onboard through dbman-opsi log-analytics."
  default     = null
}

variable "log_analytics_onboard_namespace" {
  type        = bool
  description = "Request namespace onboarding in the dbman-opsi Log Analytics workflow."
  default     = false
}

variable "log_analytics_log_group_ocid" {
  type        = string
  description = "Existing Log Analytics log group OCID."
  default     = null
}

variable "log_analytics_log_group_name" {
  type        = string
  description = "Log Analytics log group display name for CLI-managed create/reuse."
  default     = "dbman-opsi-logan"
}

variable "log_analytics_create_log_group" {
  type        = bool
  description = "Create/reuse a Log Analytics log group in the CLI workflow when no OCID is supplied."
  default     = true
}

variable "log_analytics_create_adb_collector" {
  type        = bool
  description = "Reserve intent for a private ADB collector host with Management Agent."
  default     = false
}

variable "ssh_public_keys" {
  type        = list(string)
  description = "SSH public keys for provisioned DBCS VM DB systems."
  default     = []
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

# --- Optional: enable DBM + OPSI on databases provisioned/known to this stack ---
variable "enable_observability" {
  type        = bool
  description = "Call the dbm-opsi-enablement module to enable DBM/OPSI on observability_targets."
  default     = false
}

variable "dbsnmp_secret_id" {
  type        = string
  description = "Vault secret OCID holding the DBSNMP password (required when enable_observability=true)."
  default     = null
}

variable "opsi_private_endpoint_id" {
  type        = string
  description = "Existing OPSI private endpoint OCID. When null and OPSI is selected, this stack creates a lifecycle-owned endpoint."
  default     = null
}

variable "dbm_private_endpoint_id" {
  type        = string
  description = "Existing Database Management private endpoint OCID. When null and DBM or OPSI is selected, this stack creates one."
  default     = null
}

variable "create_data_safe_private_endpoint" {
  type        = bool
  description = "Create a lifecycle-owned Data Safe private endpoint. Data Safe must already be enabled in the region."
  default     = false
}

variable "data_safe_private_endpoint_id" {
  type        = string
  description = "Existing Data Safe private endpoint OCID. Reused resources are never owned or deleted by this stack."
  default     = null
}

variable "observability_targets" {
  description = <<-EOT
    Targets for DBM/OPSI enablement, keyed by short name. service_name is
    runtime-discovered after the database is up; host IPs are deliberately not
    accepted by this public Terraform surface.
  EOT
  type = map(object({
    database_id                = string
    managed_database_name      = optional(string, "")
    database_role              = string
    database_resource_type     = string # "database" | "pluggabledatabase"
    service_name               = string
    management_type            = optional(string, "ADVANCED")
    parent_target_key          = optional(string, "")
    password_secret_id         = optional(string, null)
    monitoring_user            = optional(string, "DBSNMP")
    role                       = optional(string, "NORMAL")
    protocol                   = optional(string, "TCP")
    port                       = optional(number, 1521)
    enable_database_management = optional(bool, true)
    enable_ops_insights        = optional(bool, true)
  }))
  default = {}
}

variable "dbcs_cpu_core_count" {
  description = "OCPU core count for a provisioned Flex-shape DB system."
  type        = number
  default     = 1
}

variable "dbcs_data_storage_gb" {
  description = "Data storage (GB) for a provisioned VM DB system. Minimum 256."
  type        = number
  default     = 256
}

variable "config_file_profile" {
  description = "Optional local OCI CLI profile. Leave null in OCI Resource Manager so its managed authentication is used."
  type        = string
  default     = null
  nullable    = true
}

variable "availability_domain_index" {
  description = "Index into the region's availability domains for provisioned DB systems (0-based). Pin to an AD with DB block-storage headroom."
  type        = number
  default     = 0
}

variable "dbcs_domain" {
  description = "Network domain for a provisioned DB system. Required when the subnet has no DNS label. null derives it from a DNS-enabled subnet."
  type        = string
  default     = null
}
