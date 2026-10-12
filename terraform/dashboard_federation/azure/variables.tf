variable "issuer_url" {
  description = "The dashboard's SPIFFE issuer, e.g. https://agents.example.com/spiffe. Entra fetches discovery and keys from it, so it must be public with a certificate from a public CA."
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

variable "application_object_id" {
  description = "Object ID (not the client id) of the app registration in azure_client_id: az ad app show --id <client id> --query id."
  type        = string
}

variable "credential_name" {
  type    = string
  default = "vm-dashboard-spiffe"
}
