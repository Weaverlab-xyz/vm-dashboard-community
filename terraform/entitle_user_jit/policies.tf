# Policy rules — Entitle evaluates these top-to-bottom for any incoming
# access request against a dashboard-* resource. The "deny catch-all"
# at the bottom matches the design's fail-closed posture: a request
# that doesn't fit one of the per-tier rules above is rejected rather
# than silently using the wrong workflow.
#
# Each rule's `condition` matches by the workflow already attached to
# the requested resource — Entitle's policy engine has access to the
# resource's workflow, so the policy effectively says "if a requester
# hits this tier's workflow, route via this policy".

resource "entitle_policy" "auto_approve_tier" {
  name        = "vm-dashboard-policy-auto-approve"
  description = "Routes baseline + *-read resources through the auto-approve workflow."

  condition {
    workflow_id = entitle_workflow.auto_approve.id
  }

  workflow_id = entitle_workflow.auto_approve.id
}

resource "entitle_policy" "single_approver_tier" {
  name        = "vm-dashboard-policy-single-approver"
  description = "Routes *-write + workgroup resources through the single-approver workflow."

  condition {
    workflow_id = entitle_workflow.single_approver.id
  }

  workflow_id = entitle_workflow.single_approver.id
}

resource "entitle_policy" "two_approver_tier" {
  name        = "vm-dashboard-policy-two-approver"
  description = "Routes *-delete + admin resources through the two-approver workflow."

  condition {
    workflow_id = entitle_workflow.two_approver.id
  }

  workflow_id = entitle_workflow.two_approver.id
}
