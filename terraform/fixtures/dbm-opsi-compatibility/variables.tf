variable "compartment_id" { type = string }
variable "dbm_private_endpoint_id" { type = string }
variable "lifecycle_id" { type = string }
variable "owner_tag" { type = string }
variable "targets" {
  type = map(object({
    database_id            = string
    managed_database_name  = string
    database_role          = string
    parent_target_key      = optional(string, "")
    database_resource_type = string
    service_name           = string
    password_secret_id     = string
    ssl_secret_id          = optional(string, null)
    monitoring_user        = optional(string, "DBSNMP")
    role                   = optional(string, "NORMAL")
    protocol               = optional(string, "TCP")
    port                   = optional(number, 1521)
    management_type        = optional(string, "ADVANCED")
  }))
  default = {}
}
