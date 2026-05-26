# One `entitle_resource` per dashboard-* Entra group. Each resource is
# what end users see in Entitle's catalog and request access to. The
# `workflow_id` chosen depends on the tier carried on the input group
# entry — the bootstrap script computes this from the group name
# (admin/baseline + */read/write/delete tuple + workgroup).

locals {
  workflow_id_by_tier = {
    auto_approve    = entitle_workflow.auto_approve.id
    single_approver = entitle_workflow.single_approver.id
    two_approver    = entitle_workflow.two_approver.id
  }
}

resource "entitle_resource" "dashboard_group" {
  for_each = var.groups

  # Stable name that Entitle uses for catalog lookups + idempotent
  # match-on-rerun. Matches the Entra display_name so an operator
  # cross-checking the two systems sees the same string.
  name        = each.value.display_name
  description = each.value.description

  # The Entitle <-> Entra directory integration knows how to resolve
  # `directory_group_id` against the customer's Entra tenant.
  integration_id     = var.entitle_integration_id
  directory_group_id = each.value.directory_group_id

  workflow_id = lookup(local.workflow_id_by_tier, each.value.tier)
}
