# Shared between the clouds: the folder both sets of dynamic secrets live under.
#
# One resource rather than one per cloud. A folder is a path, not a cloud-scoped thing,
# and branching on which clouds are enabled produced a conditional that indexed an empty
# tuple when neither was.

locals {
  any_cloud_enabled = var.enable_aws || var.enable_azure
}

# Not created when the folder is already there. There is no folder DATA SOURCE in the
# provider (only the two integrations have one), so an existing folder cannot be adopted
# declaratively -- the choice is create it or reference it by name.
#
# This matters more than it looks: anyone who used Workload Credentials as a static-secret
# backend first already has this folder, and so does anyone re-running after a partial
# apply. Both got `folder already exists: dashboard (code: folder_already_exists)` and had
# no way forward except editing the module.
resource "beyondtrust_workload_credentials_folder" "this" {
  count = local.any_cloud_enabled && var.create_folder ? 1 : 0
  name  = var.folder
  tags  = var.tags
}

locals {
  # Safe to read unconditionally: every consumer is itself gated on a cloud being enabled,
  # and the folder exists whenever any of them is.
  #
  # The coalesce is what makes create_folder = false work: with the resource absent,
  # one() is null and the plain folder NAME is used instead of the path the API returned.
  # Those are the same string for a folder at the root, which is the only shape the
  # dashboard creates -- a nested folder would need the path spelled out in var.folder.
  folder_path = coalesce(one(beyondtrust_workload_credentials_folder.this[*].path), var.folder)
}
