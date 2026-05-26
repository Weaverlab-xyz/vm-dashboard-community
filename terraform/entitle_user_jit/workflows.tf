# Three workflows — one per sensitivity tier. Each `entitle_resource` in
# `resources.tf` references one of these by id.
#
# Per design §6.4 (cloud-identity-jit.md auto-approval shape; same
# pattern applies to user-side JIT):
#   IF requester in policy scope AND duration ≤ max
#   THEN approver: {automatic | single | two}
#
# Workflow `approval_steps` documents the chain Entitle walks before
# issuing the grant. `step_type = "Automatic Approval"` is Entitle's
# native no-human path; `step_type = "User Approval"` routes to the
# named identifier (group or user).

resource "entitle_workflow" "auto_approve" {
  name        = "vm-dashboard-auto-approve"
  description = "Auto-approve up to ${var.auto_approve_max_minutes} min. Baseline + *-read tier."

  max_duration_minutes = var.auto_approve_max_minutes

  approval_steps {
    step_type = "Automatic Approval"
  }
}

resource "entitle_workflow" "single_approver" {
  name        = "vm-dashboard-single-approver"
  description = "One approver, up to ${var.single_approver_max_minutes} min. *-write + workgroup tier."

  max_duration_minutes = var.single_approver_max_minutes

  approval_steps {
    step_type = "User Approval"
    approver  = var.single_approver_group
  }
}

resource "entitle_workflow" "two_approver" {
  name        = "vm-dashboard-two-approver"
  description = "Two approvers, up to ${var.two_approver_max_minutes} min. *-delete + admin tier."

  max_duration_minutes = var.two_approver_max_minutes

  approval_steps {
    step_type = "User Approval"
    approver  = var.two_approver_group
    # The Entitle provider docs document `count = 2` on a User Approval
    # step as the canonical "require two distinct approvers from the
    # group" knob. If the operator's tenant ships a different schema
    # (older / newer provider), this is the one line most likely to
    # need adjustment — Terraform will surface a clear error on plan.
    count = 2
  }
}
