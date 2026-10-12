# Azure trust for the dashboard's own SPIFFE identity: a federated identity credential on
# the app registration it acts as (its azure_client_id). No client secret exists anywhere.
#
# With wlc_azure_everyday_source = federation this is the EVERYDAY app; grant it with
# scripts/wlc/setup-azure-apps.sh --everyday-sp so it never holds role-assignment rights.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    azuread = {
      source  = "hashicorp/azuread"
      version = ">= 3.0"
    }
  }
}

provider "azuread" {}

resource "azuread_application_federated_identity_credential" "dashboard" {
  application_id = "/applications/${var.application_object_id}"
  display_name   = var.credential_name
  description    = "The vm-dashboard's own SPIFFE identity."
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = var.issuer_url
  # The exact subject: the trust domain is shared with agents and other workloads.
  subject = "spiffe://${var.trust_domain}/dashboard"
}
