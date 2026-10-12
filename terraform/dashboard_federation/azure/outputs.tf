output "dashboard_settings" {
  description = "What to set in the dashboard: tick Azure under its SPIFFE identity and clear the stored Azure client secret."
  value = {
    dashboard_spiffe_aud_azure = true
  }
}

output "subject" {
  value = azuread_application_federated_identity_credential.dashboard.subject
}
