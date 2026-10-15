variable "project_id" {
  description = "The project POVs build in (gcp_project_id). Both roles are defined there."
  type        = string
}

variable "role_id_prefix" {
  description = "Role ids are <prefix>Read and <prefix>Provision (letters, digits, underscores and periods only)."
  type        = string
  default     = "povCloud"
  validation {
    condition     = can(regex("^[a-zA-Z0-9_.]{3,48}$", var.role_id_prefix))
    error_message = "role_id_prefix may hold letters, digits, underscores and periods only."
  }
}

variable "title_prefix" {
  description = "Role titles are \"<prefix> read\" and \"<prefix> provision\"."
  type        = string
  default     = "POV cloud"
}

variable "service_account_email" {
  description = "The service account the dashboard acts as (gcp_federation_service_account). Gets the read role. Leave blank to only define the roles."
  type        = string
  default     = ""
}

variable "bind_provision" {
  description = "Also bind the provision role to the service account, STANDING. True for the SPIFFE-identity-alone setup; false when Entitle grants it just in time."
  type        = bool
  default     = false
}
