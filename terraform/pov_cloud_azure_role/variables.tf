variable "subscription_id" {
  description = "The subscription POVs build in (azure_subscription_id). Both roles are defined and assignable there only."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-fA-F-]{36}$", var.subscription_id))
    error_message = "subscription_id is the subscription's GUID."
  }
}

variable "name_prefix" {
  description = "Role names are \"<prefix> read\" and \"<prefix> provision\"."
  type        = string
  default     = "POV cloud"
}

variable "read_principal_ids" {
  description = "Service-principal OBJECT ids to assign the read role: the federated everyday app when Workload Credentials serves jobs."
  type        = list(string)
  default     = []
}

variable "provision_principal_ids" {
  description = "Service-principal OBJECT ids to assign the provision role: the federated app when it is the only credential, or the Workload Credentials target app."
  type        = list(string)
  default     = []
}
