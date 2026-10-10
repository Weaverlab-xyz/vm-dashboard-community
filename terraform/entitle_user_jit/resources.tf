# Each dashboard-* Entra group is already an Entitle resource: the Entra integration's
# sync created it, with the group's object id as its external_id. This module ADOPTS
# those resources with entitle_resource_synced and sets the tier's workflow and
# durations on them. It creates nothing in Entra, and `terraform destroy` only drops
# them from state (the provider sends no DELETE for a synced resource).
#
# The workflow is set on the resource itself, not on a bundle or a policy, because the
# resource is what users request (the dashboard's 403 page deep-links to it). An
# entitle_policy would be wrong here: in Entitle a policy is a BIRTHRIGHT grant, giving
# a group's members the listed roles with no request at all.
#
# A group the sync has not picked up yet fails the plan with the provider's "resource
# not found", naming it. Wait for the next sync (or trigger one in the Entitle UI).

locals {
  workflow_id_by_tier = {
    auto_approve    = entitle_workflow.auto_approve.id
    single_approver = entitle_workflow.single_approver.id
    two_approver    = entitle_workflow.two_approver.id
  }
}

resource "entitle_resource_synced" "dashboard_group" {
  for_each = var.groups

  integration = { id = var.entitle_integration_id }
  external_id = each.value.directory_group_id

  requestable              = true
  workflow                 = { id = local.workflow_id_by_tier[each.value.tier] }
  allowed_durations        = local.durations[each.value.tier]
  user_defined_description = each.value.description
  user_defined_tags        = [var.application_name, "tier:${each.value.tier}"]
}
