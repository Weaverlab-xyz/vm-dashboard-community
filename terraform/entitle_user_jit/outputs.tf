# The ids the dashboard and later phases need. `resource_ids` is what the Settings ->
# Integrations -> Entitle panel's resource-id map takes (entitle_resource_ids_json), for
# the 403 page's request-access deep link.

output "workflow_ids" {
  description = "Per-tier workflow ids; useful for audit-log joins."
  value       = local.workflow_id_by_tier
}

output "resource_ids" {
  description = "Map of dashboard-* group key -> Entitle resource id. The 403 page's request-access deep links use these."
  value = {
    for k, r in entitle_resource_synced.dashboard_group : k => r.id
  }
}

output "resource_count" {
  description = "Number of Entitle resources configured. Should match the dashboard-* group count from Phase 1."
  value       = length(var.groups)
}
