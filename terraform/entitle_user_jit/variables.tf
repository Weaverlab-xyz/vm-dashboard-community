variable "entitle_api_key" {
  description = "Bearer token for the Entitle API. Created in the Entitle UI under API Keys."
  type        = string
  sensitive   = true
}

variable "entitle_integration_id" {
  description = <<-EOT
    ID of the existing Entra <-> Entitle directory integration. Entitle
    needs this to resolve directory_group_id references on each
    `entitle_resource`. Created once via the Entitle UI; we read the id
    from there and pass it in.
  EOT
  type        = string
}

variable "application_name" {
  description = "Display name of the Entitle virtual application that bundles the dashboard's groups."
  type        = string
  default     = "VM Dashboard"
}

variable "application_description" {
  description = "Description shown in Entitle's catalog for end users."
  type        = string
  default     = "JIT access to the VM Dashboard's scoped permissions (aws / azure / gcp / vms / images / ...)."
}

variable "single_approver_group" {
  description = <<-EOT
    Identifier (group name or user email — whatever the configured
    Entitle integration resolves) for the single-approver workflow
    tier. Used by every dashboard-*-write group + the per-workgroup
    membership groups.
  EOT
  type        = string
}

variable "two_approver_group" {
  description = <<-EOT
    Identifier for the two-approver workflow tier. Used by every
    dashboard-*-delete group and the high-value dashboard-admin
    group. Two distinct approvals are required (Entitle handles the
    fan-out automatically when this is a group rather than a user).
  EOT
  type        = string
}

variable "auto_approve_max_minutes" {
  description = "TTL ceiling for the auto-approve tier (baseline + *-read)."
  type        = number
  default     = 120
}

variable "single_approver_max_minutes" {
  description = "TTL ceiling for the single-approver tier (*-write + workgroup)."
  type        = number
  default     = 1440
}

variable "two_approver_max_minutes" {
  description = "TTL ceiling for the two-approver tier (*-delete + admin)."
  type        = number
  default     = 480
}

variable "groups" {
  description = <<-EOT
    Map of dashboard-* groups to provision as Entitle resources.
    Populated from `oauth_group_mappings` by the bootstrap_entitle_app.py
    wrapper — Phase 1 wrote the Entra group ids there, and this file
    threads them into the `entitle_resource.directory_group_id` field.

    Each entry:
      display_name      — Entra group display name (lowercase, kebab-case)
      directory_group_id — Entra group object id
      description       — surfaced in the Entitle catalog
      tier              — one of: auto_approve | single_approver | two_approver
  EOT
  type = map(object({
    display_name       = string
    directory_group_id = string
    description        = string
    tier               = string
  }))
  default = {}

  validation {
    condition = alltrue([
      for k, g in var.groups : contains(
        ["auto_approve", "single_approver", "two_approver"], g.tier,
      )
    ])
    error_message = "Each group's tier must be one of auto_approve, single_approver, two_approver."
  }
}
