variable "issuer_url" {
  description = "The dashboard's SPIFFE issuer (Settings → Remote Agents → the dashboard's own SPIFFE identity), e.g. https://agents.example.com/spiffe. AWS fetches discovery and keys from it, so it must be public with a certificate from a public CA."
  type        = string
  validation {
    condition     = startswith(var.issuer_url, "https://") && !endswith(var.issuer_url, "/")
    error_message = "issuer_url must be https:// with no trailing slash, exactly as the dashboard shows it."
  }
}

variable "trust_domain" {
  description = "The dashboard's SPIFFE trust domain, without spiffe://. The role trusts spiffe://<trust_domain>/dashboard only."
  type        = string
  validation {
    condition     = var.trust_domain != "" && !strcontains(var.trust_domain, "://")
    error_message = "trust_domain is a bare name such as dashboard.example.com."
  }
}

variable "aws_region" {
  description = "Region for the provider. IAM is global; this only satisfies the provider."
  type        = string
  default     = "us-east-1"
}

variable "role_name" {
  description = "Name of the role the dashboard assumes. Paste its ARN into aws_federation_role_arn."
  type        = string
  default     = "vm-dashboard-spiffe"
}

variable "oidc_provider_arn" {
  description = "An existing IAM OIDC provider for this issuer. IAM allows one per URL; leave blank to create it."
  type        = string
  default     = ""
}

variable "policy_arns" {
  description = "Managed policies the role carries: the dashboard's policy (dashboard-app-policy), as the stored key's user had."
  type        = list(string)
  default     = []
}

variable "everyday_only" {
  description = "True when jobs use Workload Credentials (wlc_aws_everyday_source = federation): adds an explicit deny on creating or passing roles, as the WLC everyday secret has. Leave false when this role is the dashboard's only AWS credential."
  type        = bool
  default     = false
}

variable "max_session_duration" {
  description = "Role session ceiling in seconds. The dashboard renews before expiry, so an hour is enough."
  type        = number
  default     = 3600
  validation {
    condition     = var.max_session_duration >= 3600 && var.max_session_duration <= 43200
    error_message = "AWS allows 3600-43200 seconds."
  }
}

variable "tags" {
  type    = map(string)
  default = { app = "vm-dashboard" }
}
