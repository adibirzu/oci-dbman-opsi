output "demo_target_count" {
  description = "Safe aggregate only; never outputs target IDs, credentials, or connection details."
  value       = length(oci_data_safe_target_database.demo)
}
