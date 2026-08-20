# Shared between the clouds: the folder both sets of dynamic secrets live under.
#
# One resource rather than one per cloud. A folder is a path, not a cloud-scoped thing,
# and branching on which clouds are enabled produced a conditional that indexed an empty
# tuple when neither was.

locals {
  any_cloud_enabled = var.enable_aws || var.enable_azure
}

resource "beyondtrust_workload_credentials_folder" "this" {
  count = local.any_cloud_enabled ? 1 : 0
  name  = var.folder
  tags  = var.tags
}

locals {
  # Safe to read unconditionally: every consumer is itself gated on a cloud being enabled,
  # and the folder exists whenever any of them is.
  folder_path = coalesce(one(beyondtrust_workload_credentials_folder.this[*].path), var.folder)
}
