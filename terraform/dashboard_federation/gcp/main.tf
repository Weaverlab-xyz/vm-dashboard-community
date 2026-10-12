# GCP trust for the dashboard's own SPIFFE identity: a workload identity pool and OIDC
# provider for its issuer, and either impersonation of a service account or roles granted
# to the federated principal directly. No service-account key exists anywhere.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
}

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  subject = "spiffe://${var.trust_domain}/dashboard"
  # The token's audience: the provider's full resource name, which is also what the
  # provider accepts by default, so no allowed_audiences list is needed.
  audience  = "//iam.googleapis.com/${google_iam_workload_identity_pool_provider.spiffe.name}"
  principal = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.dashboard.workload_identity_pool_id}/subject/${local.subject}"
}

resource "google_iam_workload_identity_pool" "dashboard" {
  project                   = var.project_id
  workload_identity_pool_id = var.pool_id
  display_name              = "vm-dashboard"
  description               = "The vm-dashboard's own SPIFFE identity."
}

resource "google_iam_workload_identity_pool_provider" "spiffe" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.dashboard.workload_identity_pool_id
  workload_identity_pool_provider_id = var.provider_id
  display_name                       = "SPIFFE issuer"

  attribute_mapping = {
    "google.subject" = "assertion.sub"
  }
  # The exact subject: the trust domain is shared with agents and other workloads.
  attribute_condition = "assertion.sub == '${local.subject}'"

  oidc {
    issuer_uri = var.issuer_url
  }
}

resource "google_service_account_iam_member" "impersonate" {
  count              = var.service_account_email != "" ? 1 : 0
  service_account_id = "projects/${var.project_id}/serviceAccounts/${var.service_account_email}"
  role               = "roles/iam.workloadIdentityUser"
  member             = local.principal
}

resource "google_project_iam_member" "direct" {
  for_each = toset(var.project_roles)
  project  = var.project_id
  role     = each.value
  member   = local.principal
}
