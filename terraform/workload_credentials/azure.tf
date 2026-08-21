# The Azure side. Structurally simpler than AWS — no trust chain — but with one thing
# that has no AWS equivalent: credentials are minted onto a **pre-existing app
# registration**, so there are two apps and the integration one must be able to add
# passwords to the target one.
#
# The module does not create the integration app. Creating it here would put its client
# secret in Terraform state, and that secret is the root of the whole Azure path. You
# create it once by hand and pass its details in; `client_secret` below is write-only on
# the BeyondTrust resource, so it never lands in state either.

locals {
  azure_enabled = var.enable_azure ? 1 : 0
}

# Ownership rather than a Graph application role. An owner can add and remove credentials
# on the app it owns with no tenant-wide permission at all, which is materially less than
# Application.ReadWrite.All — the usual alternative, which grants it over every app in the
# directory.
#
# Worth having in Terraform specifically because the Portal cannot do it: its owners picker
# accepts users but not service principals, so the manual path is `az ad app owner add`
# and is easy to skip and then spend an afternoon debugging.
resource "azuread_application_owner" "integration_owns_target" {
  count = var.enable_azure && var.azure_manage_target_ownership ? 1 : 0

  application_id  = "/applications/${var.azure_target_application_object_id}"
  owner_object_id = var.azure_integration_sp_object_id
}

resource "beyondtrust_workload_credentials_azure_integration" "this" {
  count     = local.azure_enabled
  name      = var.azure_integration_name
  tenant_id = var.azure_tenant_id
  client_id = var.azure_integration_client_id

  # Write-only: not stored in state and never returned by the API. Rotate by bumping the
  # version alongside a new secret value.
  client_secret         = var.azure_integration_client_secret
  client_secret_version = var.azure_client_secret_version
}

resource "beyondtrust_workload_credentials_azure_dynamic_secret" "dashboard" {
  count            = local.azure_enabled
  name             = "dashboard-azure"
  folder           = local.folder_path
  integration_name = beyondtrust_workload_credentials_azure_integration.this[0].name

  credential_type = "service_principal_password"

  # The Object ID of the target app, NOT its Application (client) ID. The provider says so
  # explicitly because it is the most common mistake here, and the failure it produces does
  # not name the field.
  application_object_id = var.azure_target_application_object_id

  ttl = var.azure_ttl_seconds

  # Ownership has to be in place before the integration can mint onto the target.
  depends_on = [azuread_application_owner.integration_owns_target]
}

# Note what is deliberately absent: any Azure RBAC role assignment.
#
# The target app needs Contributor (or the tighter trio), Storage Blob Data Contributor,
# Key Vault access and possibly subscription-scoped Reader and Cost Management Reader.
# Enumerating that here would be a second, drifting definition of the set
# scripts/sandbox/Linux/setup-azure.sh already applies — the trap this module avoids on
# the AWS side too.
#
# But the grants do NOT arrive on their own. setup-azure.sh creates and grants its OWN
# service principal, so a separate target app registration starts with nothing. An
# earlier version of this comment said otherwise and was wrong.
#
# scripts/wlc/setup-azure-apps.sh handles it by COPYING every role assignment from a
# reference service principal — the one the dashboard authenticates with today — onto
# the target. Parity by construction, and no list to maintain in either place. Run it
# before this module.
#
# See docs/integrations/workload-credentials.md for what those grants are, including the
# subscription-scoped quota read that fails EVERY VM deploy when it is missing.
