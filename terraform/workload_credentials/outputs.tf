# Outputs are shaped around one question: what do I paste into
# Settings → Preview features → Workload Credentials → Configure?

output "dashboard_settings" {
  description = "The values to enter in the dashboard's Workload Credentials panel."
  value = {
    wlc_aws_enabled              = var.enable_aws
    wlc_aws_folder               = var.enable_aws ? local.folder_path : ""
    wlc_aws_secret_name          = var.enable_aws ? "dashboard-provision" : ""
    wlc_aws_readonly_secret_name = var.enable_aws && var.create_everyday_secret ? "dashboard-everyday" : ""
    wlc_azure_enabled            = var.enable_azure
    wlc_azure_folder             = var.enable_azure ? local.folder_path : ""
    wlc_azure_secret_name        = var.enable_azure ? "dashboard-azure" : ""
  }
}

output "aws_integration_role_arn" {
  description = "The role the BeyondTrust bridge assumes. Registered on the integration."
  value       = coalesce(one(aws_iam_role.integration[*].arn), "")
}

output "aws_target_role_arn" {
  description = "The role that carries the dashboard's permissions."
  value       = coalesce(one(aws_iam_role.target[*].arn), "")
}

output "aws_external_id" {
  description = <<-EOT
    The external ID tying the integration to your trust policy. Sensitive: it is half of
    the confused-deputy control, so it is not printed by default.
  EOT
  value       = coalesce(one(random_uuid.aws_external_id[*].result), "")
  sensitive   = true
}

output "aws_max_session_duration" {
  description = <<-EOT
    The ceiling on a minted AWS credential's lifetime.

    Surfaced because it is the one number that fails silently: AWS clamps a longer TTL
    request down to this without erroring, so a lease shorter than configured looks like
    a bug in the dashboard rather than a role attribute.
  EOT
  value       = coalesce(one(aws_iam_role.target[*].max_session_duration), 0)
}

output "aws_dashboard_policy_arn" {
  description = "The managed policy attached to the target role. Owned by setup-aws.sh, not by this module."
  value       = local.dashboard_policy_arn
}

output "next_steps" {
  description = "What to do after a successful apply."
  value = <<-EOT
    1. Settings → Preview features → enable "Workload Credentials (BeyondTrust)".
    2. Configure → paste the Site ID and a PAT minted WITH THIS SITE SELECTED, then the
       folder and secret names from the `dashboard_settings` output.
    3. Secrets page → select the Workload Credentials backend → Test connection. This
       calls GET /session and costs no metered issuance.
    4. Tick the per-cloud boxes. Then use the Check button on the panel to confirm the
       cloud reads "dynamic (WC)" with a lease expiry.
    5. Confirm ONE lease row is shared across both web workers and dash-worker, not three
       — that is what the durable lease table exists for and it cannot be asserted from
       tests.
  EOT
}
