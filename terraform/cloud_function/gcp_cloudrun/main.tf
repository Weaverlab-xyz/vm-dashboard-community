terraform {
  required_providers {
    google = {
      source = "hashicorp/google"
      # cloudfunctions2 (Cloud Run functions, "2nd gen"). 7.21 is the floor for Direct
      # VPC egress: direct_vpc_network_interface / direct_vpc_egress landed on this
      # resource in GA 7.21.0 (google-beta 7.10.0). The 6.x line exposes vpc_connector
      # ONLY, which is why this module used to require a pre-provisioned ~$26/mo
      # connector. The image's provider pre-cache has a matching 7.x leg — see the
      # tf_provider_init_g7 block in the Dockerfile; without it terraform init here
      # has nothing to resolve against at run time.
      version = "~> 7.21"
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

# Idle instances. 0 (scale to zero) is right for every event-driven workload here and
# is the default. It is WRONG for ps_dbops, and the reason is correctness rather than
# latency: Direct VPC egress documents connection-establishment delays over a minute on
# instance start, and a credential rotation that times out MAY ALREADY HAVE APPLIED the
# change — Password Safe then holds a password the database has replaced. One warm
# instance per region removes that window. CPU throttling can stay at the default: the
# instance staying ALIVE is what keeps the network interface attached; it needs no CPU
# between requests.
variable "min_instances" {
  type        = number
  default     = 0
  description = "Idle instances kept warm. 0 = scale to zero. >0 bills continuously."
}

# 0 leaves the platform default (80 on Cloud Run). A workload that holds a DATABASE
# connection for the life of a request must set this far lower: Cloud SQL's connection
# limit is reached long before Cloud Run decides it needs another instance.
variable "concurrency" {
  type        = number
  default     = 0
  description = "Max simultaneous requests per instance. 0 = platform default."
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
  description = "ALLOW_ALL (reachable by Entitle, and by an on-premises Password Safe Resource Broker) or ALLOW_INTERNAL_AND_GCLB"
  validation {
    condition     = contains(["ALLOW_ALL", "ALLOW_INTERNAL_ONLY", "ALLOW_INTERNAL_AND_GCLB"], var.ingress_settings)
    error_message = "ingress_settings must be ALLOW_ALL, ALLOW_INTERNAL_ONLY or ALLOW_INTERNAL_AND_GCLB."
  }
}

# NAMED callers, for auth_mode = "run_invoker". The two wildcard principals are refused
# rather than discouraged: this module deploys credential-changing workloads, and
# `allAuthenticatedUsers` on such a service means any Google account anywhere. An
# operator who genuinely wants a public function sets auth_mode = "none", which is
# explicit, recorded on the CloudFunction row, and visible in the UI.
variable "invoker_members" {
  type        = list(string)
  default     = []
  description = "IAM members granted roles/run.invoker (e.g. serviceAccount:broker@proj.iam.gserviceaccount.com). Named principals only."
  validation {
    condition = alltrue([
      for m in var.invoker_members :
      !contains(["allusers", "allauthenticatedusers"], lower(trimspace(m)))
    ])
    error_message = "invoker_members may not contain allUsers or allAuthenticatedUsers — use auth_mode = \"none\" if a public endpoint is really what you want."
  }
}

# ── Credentials ───────────────────────────────────────────────────────────────
#
# How a credential reaches a workload on GCP, and the only supported way: name an
# EXISTING Secret Manager secret and let the platform inject it. Terraform passes
# the secret's ID, never its value, so the credential appears in no plan output, no
# state file, and no `gcloud functions describe`. The handler needs no SDK.
#
# `environment` above is the plaintext counterpart and is for NON-secret settings
# only; the dashboard refuses credential-shaped values there.

variable "secret_environment" {
  type        = map(string)
  default     = {}
  description = "Env var name → Secret Manager secret ID (not a version) to inject"
  validation {
    condition = alltrue([
      for key in keys(var.secret_environment) : can(regex("^[A-Za-z_][A-Za-z0-9_]*$", key))
    ])
    error_message = "secret_environment keys must be valid environment variable names."
  }
}

# The original, db_grant-specific spelling of the same thing. Kept so functions
# deployed before secret_environment existed still plan clean; merged into it below.
variable "db_admin_secret" {
  type        = string
  default     = ""
  description = "Secret Manager secret ID (not a version) holding the DB admin password"
}

# ── Networking (optional) ─────────────────────────────────────────────────────
#
# DIRECT VPC EGRESS is the default path: no Serverless VPC Access connector, so
# nothing to pre-provision, nothing billed when no function exists, and the whole
# attachment is created and destroyed with the function itself. A connector, by
# contrast, runs a minimum of two e2-micro instances (~$26/mo) whether or not the
# function is ever invoked, and needs a non-overlapping /28 of its own.
#
# Two things to know about direct egress:
#   * It is REGION-LOCKED — the subnet must live in var.region. Reaching an internal
#     IP in another region silently drops SYNs (see rancher_api_runner.py, which ate
#     this live). Pass a BARE subnet name so it resolves per-region.
#   * Cloud Run reserves IPs in /28 blocks out of the subnet and needs a /26 minimum.
#     Sharing one subnet across functions is supported, so no dedicated subnet.
#
# vpc_connector is kept as an escape hatch for the one thing direct egress cannot do:
# reach another region from a region-pinned function. The provider declares the two
# ConflictsWith each other, so the locals below are mutually exclusive and direct
# wins when both are set — matching the precedence k8s_runner_service documents.

variable "vpc_network" {
  type        = string
  default     = ""
  description = "VPC network for Direct VPC egress. Empty (with vpc_subnetwork empty) = no VPC attachment."
}

variable "vpc_subnetwork" {
  type        = string
  default     = ""
  description = "Subnet for Direct VPC egress. MUST exist in var.region. Use a BARE NAME, not a regional self_link, so it resolves per-region."
}

variable "vpc_egress" {
  type        = string
  default     = "PRIVATE_RANGES_ONLY"
  description = "Which traffic leaves through the VPC: PRIVATE_RANGES_ONLY or ALL_TRAFFIC."
  validation {
    condition     = contains(["PRIVATE_RANGES_ONLY", "ALL_TRAFFIC"], var.vpc_egress)
    error_message = "vpc_egress must be PRIVATE_RANGES_ONLY or ALL_TRAFFIC."
  }
}

variable "vpc_connector" {
  type        = string
  default     = ""
  description = "Legacy escape hatch: an EXISTING Serverless VPC Access connector. Ignored when vpc_network/vpc_subnetwork is set."
}

locals {
  direct_vpc    = trimspace(var.vpc_network) != "" || trimspace(var.vpc_subnetwork) != ""
  use_connector = !local.direct_vpc && trimspace(var.vpc_connector) != ""
  vpc_attached  = local.direct_vpc || local.use_connector

  # What FN_NETWORK reports back to the handler, whichever path is live.
  network_ref = local.direct_vpc ? (
    trimspace(var.vpc_subnetwork) != "" ? trimspace(var.vpc_subnetwork) : trimspace(var.vpc_network)
  ) : trimspace(var.vpc_connector)

  # One map, whichever variable the credential arrived in. secret_environment wins a
  # collision: it is the explicit, current spelling.
  secret_env = merge(
    var.db_admin_secret != "" ? { FN_DB_ADMIN_PASSWORD = var.db_admin_secret } : {},
    var.secret_environment
  )
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
    min_instance_count    = var.min_instances
    ingress_settings      = var.ingress_settings
    service_account_email = var.service_account_email != "" ? var.service_account_email : null

    # null, not 0: the field is "max requests per instance" and 0 is not a legal value —
    # omitting it is how you ask for the platform default.
    max_instance_request_concurrency = var.concurrency > 0 ? var.concurrency : null

    dynamic "direct_vpc_network_interface" {
      for_each = local.direct_vpc ? [1] : []
      content {
        network    = trimspace(var.vpc_network) != "" ? var.vpc_network : null
        subnetwork = trimspace(var.vpc_subnetwork) != "" ? var.vpc_subnetwork : null
      }
    }
    # The enum is PREFIXED on the direct path and bare on the connector path.
    direct_vpc_egress = local.direct_vpc ? "VPC_EGRESS_${var.vpc_egress}" : null

    vpc_connector                 = local.use_connector ? var.vpc_connector : null
    vpc_connector_egress_settings = local.use_connector ? var.vpc_egress : null

    environment_variables = merge(var.environment, {
      FN_WORKLOAD     = var.workload
      FN_CLOUD        = "gcp"
      FN_REGION       = var.region
      FN_NAME         = var.name
      FN_NETWORK_MODE = local.vpc_attached ? "vpc" : "public"
      FN_NETWORK      = local.network_ref
      FN_VPC_EGRESS   = local.direct_vpc ? "direct" : (local.use_connector ? "connector" : "")
      # What the RUNNING function is allowed to assume about its own front door.
      # fnruntime.auth.verify_gcp_oidc trusts the platform to have verified the
      # caller's token, which is only true under run_invoker — so it reads this and
      # fails closed on anything else, rather than trusting a deploy-time check made
      # in another trust domain.
      FN_AUTH_MODE_FRONT_DOOR = var.auth_mode
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
      for_each = local.secret_env
      content {
        key        = secret_environment_variables.key
        project_id = var.project
        secret     = secret_environment_variables.value
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

# The same binding for every OPERATOR-supplied secret. Without it the function
# deploys and then fails at cold start resolving its credential — a permission error
# surfacing as a broken workload, which is the confusing failure this avoids.
# Scoped per secret: no project-level accessor, so a function reads only what it was
# given. Terraform removes the binding on destroy along with the function.
# Keyed by the SECRET, not by the env var that names it: two variables may point at
# one secret, and binding it twice is a duplicate resource, not a second grant.
resource "google_secret_manager_secret_iam_member" "injected" {
  for_each  = var.service_account_email != "" ? toset(values(local.secret_env)) : toset([])
  project   = var.project
  secret_id = each.value
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

# The named-caller half of the same front door. A gen2 function IS a Cloud Run service,
# so this is the binding a Password Safe Resource Broker needs before it can present an
# OIDC token and be let in. Empty by default, which means NOBODY can call the service —
# a deployed-but-unreachable endpoint, which is the safe direction and is visible
# immediately rather than at the first rotation.
resource "google_cloud_run_service_iam_member" "invokers" {
  for_each = toset([for m in var.invoker_members : trimspace(m) if trimspace(m) != ""])
  project  = var.project
  location = var.region
  service  = google_cloudfunctions2_function.this.name
  role     = "roles/run.invoker"
  member   = each.value
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
  description = "Whether the function egresses through the VPC (direct egress or a connector)"
}
