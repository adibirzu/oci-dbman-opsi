terraform {
  required_version = ">= 1.5.0, < 2.0.0"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "= 8.24.0"
    }
  }
}

provider "oci" {
  region = var.region
  # Local callers may set a profile. Resource Manager leaves this null and uses
  # the managed OCI authentication injected into the Terraform job.
  config_file_profile = var.config_file_profile
}
