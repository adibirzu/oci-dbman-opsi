variable "compartment_id" { type = string }
variable "data_safe_private_endpoint_id" { type = string }
variable "lifecycle_id" { type = string }
variable "owner_tag" { type = string }

variable "enable_plaintext_data_safe_demo" {
  description = "DEMO ONLY: creates Data Safe targets using a password that can enter Terraform state."
  type        = bool
  default     = false
}

variable "allow_plaintext_data_safe_demo" {
  description = "DEMO ONLY: explicit acknowledgement required before Data Safe plaintext Terraform use."
  type        = bool
  default     = false
}

variable "data_safe_password" {
  description = "DEMO ONLY: plaintext password. Supply through a local ignored environment variable; never commit it."
  type        = string
  sensitive   = true
  default     = null
}

check "plaintext_demo_declaration_gate" {
  assert {
    condition     = !var.enable_plaintext_data_safe_demo || var.allow_plaintext_data_safe_demo
    error_message = "DEMO ONLY: the Data Safe plaintext password requires explicit acknowledgement."
  }
}

variable "targets" {
  type = map(object({
    db_system_id    = string
    service_name    = string
    monitoring_user = optional(string, "DBSNMP")
    port            = optional(number, 1521)
  }))
  default = {}
}
