output "dashboard_settings" {
  description = "What to set in the dashboard: the GCP audience and service account under its SPIFFE identity, gcp_project_id, then clear the stored service-account key."
  value = {
    dashboard_spiffe_aud_gcp       = local.audience
    gcp_federation_service_account = var.service_account_email
    gcp_project_id                 = var.project_id
  }
}

output "principal" {
  value = local.principal
}
