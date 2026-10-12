variable "issuer_url" {
  description = "The dashboard's SPIFFE issuer, e.g. https://agents.example.com/spiffe."
  type        = string
  validation {
    condition     = startswith(var.issuer_url, "https://") && !endswith(var.issuer_url, "/")
    error_message = "issuer_url must be https:// with no trailing slash, exactly as the dashboard shows it."
  }
}

variable "trust_domain" {
  description = "The dashboard's SPIFFE trust domain, without spiffe://."
  type        = string
  validation {
    condition     = var.trust_domain != "" && !strcontains(var.trust_domain, "://")
    error_message = "trust_domain is a bare name such as dashboard.example.com."
  }
}

variable "project_id" {
  description = "The project that holds the pool and that the dashboard works in (its gcp_project_id)."
  type        = string
}

variable "pool_id" {
  type    = string
  default = "vm-dashboard"
}

variable "provider_id" {
  type    = string
  default = "spiffe"
}

variable "service_account_email" {
  description = "A service account the identity impersonates (the one whose key you are retiring). Blank to grant project_roles to the principal directly instead."
  type        = string
  default     = ""
}

variable "project_roles" {
  description = "Roles granted to the federated principal directly, when not impersonating a service account."
  type        = list(string)
  default     = []
}
