variable "entitle_api_key" {
  description = "Bearer token for the Entitle API. Created in the Entitle UI under API Keys."
  type        = string
  sensitive   = true
}

variable "entitle_integration_id" {
  description = <<-EOT
    ID of the existing Entra <-> Entitle integration. Its sync is what puts each
    dashboard-* group into Entitle as a resource, with the group's Entra object id as
    the resource's external_id; this module adopts those resources, it does not create
    them. Created once in the Entitle UI; copy the id from there.
  EOT
  type        = string
}

variable "application_name" {
  description = "Tag put on every dashboard group resource, so the catalog can be filtered to them."
  type        = string
  default     = "VM Dashboard"
}

variable "single_approver_group" {
  description = <<-EOT
    Name of the Entitle directory group whose members approve the single-approver tier
    (every dashboard-*-write group and the per-workgroup membership groups). Matched
    exactly against Entitle's directory groups; the plan fails unless exactly one matches.
  EOT
  type        = string
}

variable "two_approver_group" {
  description = <<-EOT
    Name of the Entitle directory group that gives the FIRST approval on the two-approver
    tier (every dashboard-*-delete group and dashboard-admin). Matched exactly.
  EOT
  type        = string
}

variable "two_approver_second_group" {
  description = <<-EOT
    Name of the directory group that gives the SECOND approval on the two-approver tier.
    Empty means the same group as two_approver_group. Use a different group when the two
    approvals must come from two different people: whether one member of a single group
    can approve both steps is Entitle's behaviour, not something this module enforces.
  EOT
  type        = string
  default     = ""
}

# Durations are in SECONDS and must come from Entitle's fixed list:
# 1800, 3600, 10800, 21600, 43200, 57600, 86400, 259200, 604800, ... (-1 = unlimited).
# The largest value in each list is also the tier's workflow ceiling.
variable "auto_approve_durations" {
  description = "Durations a requester may pick on the auto-approve tier (baseline + *-read)."
  type        = list(number)
  default     = [1800, 3600]
}

variable "single_approver_durations" {
  description = "Durations a requester may pick on the single-approver tier (*-write + workgroup)."
  type        = list(number)
  default     = [3600, 10800, 43200, 86400]
}

variable "two_approver_durations" {
  description = "Durations a requester may pick on the two-approver tier (*-delete + admin)."
  type        = list(number)
  default     = [3600, 10800, 21600]
}

variable "groups" {
  description = <<-EOT
    Map of dashboard-* groups whose Entitle resources this module configures.
    Populated from `oauth_group_mappings` by the bootstrap_entitle_app.py
    wrapper. Phase 1 wrote the Entra group ids there, and each one is the
    external_id Entitle's Entra sync gave that group's resource.

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
