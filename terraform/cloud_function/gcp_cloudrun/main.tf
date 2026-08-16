terraform {
  required_providers {
    google = {
      source = "hashicorp/google"
      # cloudfunctions2 (Cloud Run functions, "2nd gen"). Pinned to the 6.x line,
      # which is already in the image's provider pre-cache alongside 5.x.
      version = "~> 6.0"
    }
  }
  required_version = ">= 1.3.0"
}

provider "google" {
  project = var.project
  region  = var.region
}

# ── Variables ────────────────────────────────────────────────────────────────

variable "project" {
  type        = string
  description = "GCP project id"
}

variable "region" {
  type        = string
  description = "Region for the function"
}

variable "name" {
  type        = string
  description = "Function name (unique per project/region)"
}

variable "workload" {
  type        = string
  description = "Workload packaged into the zip; surfaced to the handler as FN_WORKLOAD"
}

# build_config.source accepts ONLY storage_source or repo_source — there is no
# inline option, which is why every cloud in this feature uploads to an object
# store rather than embedding the archive.
variable "package_bucket" {
  type        = string
  description = "GCS bucket holding the function package"
}

variable "package_object" {
  type        = string
  description = "GCS object name (function-packages/<fn_id>/<sha256>.zip). Changing it is what triggers a rebuild."
}

variable "shared_secret" {
  type        = string
  sensitive   = true
  description = "Bearer secret the handler verifies on every request (fnruntime.auth)"
}

variable "auth_mode" {
  type        = string
  default     = "run_invoker"
  description = "Front door: run_invoker (callers need roles/run.invoker, i.e. a signed OIDC token) or none (public; the shared secret is then the only gate)"
  validation {
    condition     = contains(["run_invoker", "none"], var.auth_mode)
    error_message = "auth_mode must be run_invoker or none."
  }
}

variable "runtime" {
  type        = string
  default     = "python312"
  description = "Cloud Functions Python runtime id"
}

variable "timeout_seconds" {
  type    = number
  default = 60
}

variable "memory_mb" {
  type    = number
  default = 256
}

variable "max_instances" {
  type        = number
  default     = 3
  description = "Capped low by default: a grant webhook should never fan out, and an unbounded ceiling turns a retry storm into a bill."
}

variable "service_account_email" {
  type        = string
  default     = ""
  description = "Runtime service account. Blank uses the project default compute SA (broad — set this in anything but a demo)."
}

variable "environment" {
  type        = map(string)
  default     = {}
  description = "Extra NON-SECRET environment variables"
}

variable "ingress_settings" {
  type        = string
  default     = "ALLOW_ALL"
  description = "ALLOW_ALL (reachable by Entitle) or ALLOW_INTERNAL_AND_GCLB"
}

# An EXISTING Secret Manager secret injected as FN_DB_ADMIN_PASSWORD, for workloads
# that need a database credential (db_grant). Platform-resolved: the value never
# passes through Terraform state or the function's describe output, and the handler
# needs no SDK to read it. Empty = not injected.
variable "db_admin_secret" {
  type        = string
  default     = ""
  description = "Secret Manager secret ID (not a version) holding the DB admin password"
}

# ── Networking (optional) ─────────────────────────────────────────────────────
#
# A Serverless VPC Access connector, NOT Direct VPC egress: cloudfunctions2's
# service_config exposes only vpc_connector. Direct egress lives on the underlying
# google_cloud_run_v2_service, which would mean giving up cloudfunctions2 and
# owning the container build.
#
# Reference an EXISTING connector. One runs a minimum of two e2-micro instances
# (~$26/mo) whether or not the function is ever invoked, so creating one per
# function would cost more than the functions and make destroy take minutes.

variable "vpc_connector" {
  type        = string
  default     = ""
  description = "Existing Serverless VPC Access connector name or self_link. Empty = no VPC attachment."
}

locals {
  vpc_attached = length(trimspace(var.vpc_connector)) > 0
}

resource "google_cloudfunctions2_function" "this" {
  name        = var.name
  location    = var.region
  description = "vm-dashboard cloud function (${var.workload})"

  build_config {
    runtime     = var.runtime
    entry_point = "main"
    source {
      storage_source {
        bucket = var.package_bucket
        object = var.package_object
      }
    }
  }

  service_config {
    available_memory      = "${var.memory_mb}M"
    timeout_seconds       = var.timeout_seconds
    max_instance_count    = var.max_instances
    ingress_settings      = var.ingress_settings
    service_account_email = var.service_account_email != "" ? var.service_account_email : null

    vpc_connector                 = local.vpc_attached ? var.vpc_connector : null
    vpc_connector_egress_settings = local.vpc_attached ? "PRIVATE_RANGES_ONLY" : null

    environment_variables = merge(var.environment, {
      FN_WORKLOAD     = var.workload
      FN_CLOUD        = "gcp"
      FN_REGION       = var.region
      FN_NAME         = var.name
      FN_NETWORK_MODE = local.vpc_attached ? "vpc" : "public"
      FN_NETWORK      = var.vpc_connector
    })

    # Kept out of environment_variables so it is not rendered into plan output
    # alongside the non-secret settings.
    secret_environment_variables {
      key        = "FN_SHARED_SECRET"
      project_id = var.project
      secret     = google_secret_manager_secret.shared_secret.secret_id
      version    = "latest"
    }

    dynamic "secret_environment_variables" {
      for_each = var.db_admin_secret != "" ? [1] : []
      content {
        key        = "FN_DB_ADMIN_PASSWORD"
        project_id = var.project
        secret     = var.db_admin_secret
        version    = "latest"
      }
    }
  }

  labels = {
    managed_by = "vm-dashboard"
    workload   = replace(var.workload, "_", "-")
  }
}

# GCP is the one cloud here with a first-class place to put the secret, so use it:
# the function reads it at cold start and it never appears in the function's own
# describe output.
resource "google_secret_manager_secret" "shared_secret" {
  secret_id = "${var.name}-fn-secret"
  replication {
    auto {}
  }
  labels = {
    managed_by = "vm-dashboard"
  }
}

resource "google_secret_manager_secret_version" "shared_secret" {
  secret      = google_secret_manager_secret.shared_secret.id
  secret_data = var.shared_secret
}

resource "google_secret_manager_secret_iam_member" "accessor" {
  count     = var.service_account_email != "" ? 1 : 0
  secret_id = google_secret_manager_secret.shared_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_email}"
}

# Public invocation, when the operator has chosen to let the shared secret be the
# only gate (simplest thing that works with Entitle's plain header auth).
resource "google_cloud_run_service_iam_member" "public" {
  count    = var.auth_mode == "none" ? 1 : 0
  project  = var.project
  location = var.region
  service  = google_cloudfunctions2_function.this.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "resource_id" {
  value       = google_cloudfunctions2_function.this.id
  description = "Fully-qualified function id"
}

output "invoke_url" {
  value       = google_cloudfunctions2_function.this.service_config[0].uri
  description = "HTTPS endpoint — what an Entitle REST integration posts to"
}

output "network_mode" {
  value       = local.vpc_attached ? "vpc" : "public"
  description = "Whether the function egresses through a VPC connector"
}
