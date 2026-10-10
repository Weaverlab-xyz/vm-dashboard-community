# Three workflows, one per sensitivity tier. resources.tf sets one of them on each
# dashboard group's Entitle resource, so a request for that group's role is approved
# the way its tier says:
#
#   auto_approve    -- no human. Baseline + every *-read group.
#   single_approver -- one approval from single_approver_group. *-write + workgroups.
#   two_approver    -- two sequential approvals. *-delete + dashboard-admin.
#
# A workflow rule applies to requests up to its `under_duration`; each tier has one
# rule, whose ceiling is the longest duration that tier allows. The resource's
# allowed_durations (resources.tf) is what stops a requester asking for more.

locals {
  durations = {
    auto_approve    = var.auto_approve_durations
    single_approver = var.single_approver_durations
    two_approver    = var.two_approver_durations
  }
  ceiling = { for tier, d in local.durations : tier => max(d...) }
}

resource "entitle_workflow" "auto_approve" {
  name = "vm-dashboard-auto-approve"
  rules = [{
    sort_order     = 1
    under_duration = local.ceiling.auto_approve
    any_schedule   = false
    approval_flow = {
      steps = [{
        sort_order        = 1
        operator          = "or"
        approval_entities = [{ type = "Automatic" }]
      }]
    }
  }]
}

resource "entitle_workflow" "single_approver" {
  name = "vm-dashboard-single-approver"
  rules = [{
    sort_order     = 1
    under_duration = local.ceiling.single_approver
    any_schedule   = false
    approval_flow = {
      steps = [{
        sort_order = 1
        operator   = "or"
        approval_entities = [{
          type  = "DirectoryGroup"
          group = { id = local.approver_group_id.single }
        }]
      }]
    }
  }]
}

resource "entitle_workflow" "two_approver" {
  name = "vm-dashboard-two-approver"
  rules = [{
    sort_order     = 1
    under_duration = local.ceiling.two_approver
    any_schedule   = false
    approval_flow = {
      # Two steps run in order: the second approver acts only after the first.
      steps = [
        {
          sort_order = 1
          operator   = "or"
          approval_entities = [{
            type  = "DirectoryGroup"
            group = { id = local.approver_group_id.two_first }
          }]
        },
        {
          sort_order = 2
          operator   = "or"
          approval_entities = [{
            type  = "DirectoryGroup"
            group = { id = local.approver_group_id.two_second }
          }]
        },
      ]
    }
  }]
}
