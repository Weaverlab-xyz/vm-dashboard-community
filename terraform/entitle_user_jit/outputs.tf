# Surface the IDs Phase 3 (end-to-end JIT request flow) + downstream
# audits will need. Operators wire these into the dashboard's
# Settings → Integrations → Entitle panel — specifically the
# "Request access portal URL" that the Phase 4 UI affordances point
# at on the 403 page.

output "application_id" {
  description = "Entitle id of the VM Dashboard virtual application bundle."
  value       = entitle_bundle.vm_dashboard.id
}

output "workflow_ids" {
  description = "Per-tier workflow ids; useful for audit-log joins."
  value = {
    auto_approve    = entitle_workflow.auto_approve.id
    single_approver = entitle_workflow.single_approver.id
    two_approver    = entitle_workflow.two_approver.id
  }
}

output "resource_ids" {
  description = "Map of dashboard-* group name → Entitle resource id. Phase 4 deep-links from the dashboard's 403 page use these."
  value = {
    for k, _ in var.groups : k => entitle_resource.dashboard_group[k].id
  }
}

output "resource_count" {
  description = "Number of Entitle resources provisioned. Should match the dashboard-* group count from Phase 1."
  value       = length(var.groups)
}
