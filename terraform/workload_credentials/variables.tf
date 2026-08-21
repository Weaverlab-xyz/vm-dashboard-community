# ── Workload Credentials connection ──────────────────────────────────────────

variable "beyondtrust_access_token" {
  description = <<-EOT
    Workload Credentials personal access token. Leave empty to use
    BEYONDTRUST_ACCESS_TOKEN from the environment, which is the recommended path.

    A PAT is scoped to whichever site was selected when it was minted and cannot be
    widened. Minting one while the Pathfinder admin tenant is selected, rather than the
    site itself, produces `401 Access denied for this site` on every call — which reads
    like the site is missing the application. See
    docs/integrations/workload-credentials.md.
  EOT
  type        = string
  default     = ""
  sensitive   = true
}

variable "beyondtrust_site_id" {
  description = "Site (tenant) GUID. Empty uses BEYONDTRUST_SITE_ID from the environment."
  type        = string
  default     = ""
}

variable "beyondtrust_api_url" {
  description = "Workload Credentials API base URL. Override only for a non-production platform."
  type        = string
  default     = "https://api.beyondtrust.io"
}

variable "folder" {
  description = "Folder the dynamic secrets are created under."
  type        = string
  default     = "dashboard"
}

variable "create_folder" {
  description = <<-EOT
    Create the folder, rather than expecting it to exist already.

    Set to false when the folder is already there — which is the case if Workload
    Credentials was used as a static-secret backend first, or if an earlier apply got
    part-way. The API rejects a duplicate with `folder_already_exists` and the provider
    has no folder data source, so there is no way to adopt one; this is the lever.

    With this false, the dynamic secrets are filed under var.folder by name instead of
    under the path the create call returned. Identical for a root-level folder.
  EOT
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to the created cloud resources."
  type        = map(string)
  default     = { managed-by = "vm-dashboard" }
}

# ── AWS ──────────────────────────────────────────────────────────────────────

variable "enable_aws" {
  description = "Create the AWS integration, roles and dynamic secrets."
  type        = bool
  default     = true
}

variable "bt_bridge_role_arn" {
  description = <<-EOT
    The BeyondTrust-owned bridge role your integration role must trust. This is the
    confused-deputy anchor: BeyondTrust assumes the bridge, the bridge assumes your
    integration role, and that assumes the target role.

    Defaulted rather than required because getting it wrong is a silent trust failure
    that surfaces only when a mint is attempted. Verify it against current BeyondTrust
    documentation before a production apply — it is theirs to change, not yours.
  EOT
  type        = string
  default     = "arn:aws:iam::071882751376:role/secrets-integration-customer-bridge-link"
}

variable "aws_integration_name" {
  description = "Name of the Workload Credentials AWS integration."
  type        = string
  default     = "vm-dashboard"
}

variable "aws_integration_role_name" {
  description = "IAM role in your account that the BeyondTrust bridge assumes."
  type        = string
  default     = "btp-account-role-secrets-vm-dashboard"
}

variable "aws_target_role_name" {
  description = "IAM role the integration role assumes; this is what carries the dashboard's permissions."
  type        = string
  default     = "vm-dashboard-wlc-target"
}

variable "aws_dashboard_policy_arn" {
  description = <<-EOT
    ARN of the managed policy holding the dashboard's AWS permissions. Leave empty to
    look it up by `aws_dashboard_policy_name`.

    The module deliberately does not contain a copy of that policy. `scripts/sandbox/
    Linux/setup-aws.sh` already builds it from every code path in aws_service.py, and a
    second copy here would drift from it the first time either changed — which is the
    failure mode this repo hits most often. One policy, one owner.
  EOT
  type        = string
  default     = ""
}

variable "aws_dashboard_policy_name" {
  description = "Managed-policy name to look up when no ARN is given. Created by setup-aws.sh."
  type        = string
  default     = "dashboard-app-policy"
}

variable "aws_max_session_duration" {
  description = <<-EOT
    MaxSessionDuration on the target role, in seconds.

    This is the ceiling on the credential TTL, and AWS's default of 3600 SILENTLY CLAMPS
    a longer request — no error, just a shorter credential than asked for. Set it above
    the TTL you want. 4 hours by default because issuances are metered and a longer
    lease means fewer of them.

    Note this may be moot: see the role-chaining cap described on aws_ttl_seconds. A
    generous ceiling here is harmless either way.
  EOT
  type        = number
  default     = 14400

  validation {
    condition     = var.aws_max_session_duration >= 3600 && var.aws_max_session_duration <= 43200
    error_message = "AWS allows 3600-43200 seconds (1-12 hours)."
  }
}

variable "aws_ttl_seconds" {
  description = <<-EOT
    TTL requested for a minted AWS credential.

    Prefer an hour. AWS credentials cannot be revoked early, so a short TTL buys no
    security and multiplies the metered issuance count — at 15 minutes that is roughly
    70,000 issuances a year for one cloud.

    UNVERIFIED, AND THE REASON THE DEFAULT IS EXACTLY AN HOUR: this is a role CHAIN
    (BeyondTrust assumes the bridge, the bridge assumes the integration role, that
    assumes the target role), and AWS limits a role-chained session to a maximum of one
    hour regardless of any role's MaxSessionDuration. If that applies here, a value above
    3600 either errors at generate time or comes back clamped, and aws_max_session_duration
    above 3600 buys nothing.

    It is not validated because the cap depends on how BeyondTrust performs the final
    hop, which cannot be determined from this side — only a live `generate` settles it.
    Raise this above 3600 only after checking the `expiration` on a real lease against
    what was asked for.
  EOT
  type        = number
  default     = 3600

  validation {
    condition     = var.aws_ttl_seconds >= 900 && var.aws_ttl_seconds <= 43200
    error_message = "assumed_role credentials allow 900-43200 seconds (15 minutes to 12 hours)."
  }

  # Caught here because STS does not complain: asking for longer than the role's own
  # MaxSessionDuration hands back a SHORTER credential with no error anywhere. The
  # dashboard would then refresh far more often than configured, and every refresh is a
  # metered issuance.
  validation {
    condition     = var.aws_ttl_seconds <= var.aws_max_session_duration
    error_message = "aws_ttl_seconds cannot exceed aws_max_session_duration - STS would silently clamp it and issue a shorter credential."
  }
}

variable "create_everyday_secret" {
  description = <<-EOT
    Also create the "everyday" dynamic secret, which the dashboard uses for everything
    outside a job. Points at the SAME role with a session policy that denies IAM, so the
    escalation paths exist only while a job is running.

    Session policies intersect and never broaden, which is what makes one role enough.
  EOT
  type        = bool
  default     = true
}

# ── Azure ────────────────────────────────────────────────────────────────────

variable "enable_azure" {
  description = "Create the Azure integration and dynamic secret."
  type        = bool
  default     = false
}

variable "azure_integration_name" {
  description = "Name of the Workload Credentials Azure integration."
  type        = string
  default     = "vm-dashboard-azure"
}

variable "azure_tenant_id" {
  description = "Entra tenant (directory) GUID."
  type        = string
  default     = ""

  validation {
    condition     = !var.enable_azure || var.azure_tenant_id != ""
    error_message = "azure_tenant_id is required when enable_azure is true."
  }
}

variable "azure_integration_client_id" {
  description = <<-EOT
    Application (client) ID of the **integration** app registration — the one Workload
    Credentials authenticates as. You create this once, out of band; the module does not,
    because doing so would put its client secret in Terraform state.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = !var.enable_azure || var.azure_integration_client_id != ""
    error_message = "azure_integration_client_id is required when enable_azure is true."
  }
}

variable "azure_integration_client_secret" {
  description = <<-EOT
    Client secret of the integration app registration.

    Write-only on the BeyondTrust resource, so it is never persisted to state. Increment
    `azure_client_secret_version` to rotate it.
  EOT
  type        = string
  default     = ""
  sensitive   = true
}

variable "azure_client_secret_version" {
  description = "Bump to signal that the integration client secret changed and should be re-applied."
  type        = number
  default     = 1
}

variable "azure_integration_sp_object_id" {
  description = <<-EOT
    Object ID of the integration app's **service principal** (not the application).

    Used to make it an owner of the target app, which is what lets it add and remove
    passwords without any tenant-wide Graph permission. Ownership is the least-privilege
    path; `Application.ReadWrite.All` is the alternative and is far broader.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = !var.enable_azure || var.azure_integration_sp_object_id != ""
    error_message = "azure_integration_sp_object_id is required when enable_azure is true (unless azure_manage_target_ownership is false). Get it with: az ad sp show --id <integration-client-id> --query id."
  }
}

variable "azure_target_application_object_id" {
  description = <<-EOT
    Object ID of the **target** app registration — the one credentials are minted onto.

    This is the Object ID, NOT the Application (client) ID. The provider calls that out
    explicitly because it is the most common mistake in Azure setup, and the resulting
    failure does not name the field.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = !var.enable_azure || var.azure_target_application_object_id != ""
    error_message = "azure_target_application_object_id is required when enable_azure is true. It is the target app's Object ID (az ad app show --id <app> --query id), NOT its appId."
  }
}

variable "azure_manage_target_ownership" {
  description = <<-EOT
    Add the integration service principal as an owner of the target app registration.

    Worth letting Terraform do: the Portal's owners picker accepts users but not service
    principals, so the manual path is CLI-only and easy to skip.
  EOT
  type        = bool
  default     = true
}

variable "azure_ttl_seconds" {
  description = "TTL requested for a minted Azure credential."
  type        = number
  default     = 3600

  validation {
    condition     = var.azure_ttl_seconds >= 3600 && var.azure_ttl_seconds <= 86400
    error_message = "Azure service-principal passwords allow 3600-86400 seconds (1-24 hours)."
  }
}
