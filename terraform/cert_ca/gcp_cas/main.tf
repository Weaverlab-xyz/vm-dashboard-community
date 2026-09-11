# Google Cloud Certificate Authority Service — a private CA for the Password Safe
# Certificate plugin's `gcpcas` backend, built to be DESTROYED.
#
# Cost is the reason this module exists in the shape it does. A CAS pool on the DevOps
# tier is ~$20/month plus ~$0.30 per certificate; the Enterprise tier is an order of
# magnitude more and buys certificate records this lab does not need. The AWS Private CA
# equivalent is ~$400/month standing, which is why GCP is the lab default.
#
# ── Teardown is the hard part, and every guard below exists for it ────────────
#
# CAS resists deletion by default, in three separate ways, and each one leaves a pool
# still billing:
#
#   1. `deletion_protection` defaults to TRUE on a certificate authority. A destroy
#      fails outright.
#   2. A CA that has ISSUED anything refuses to delete unless
#      `ignore_active_certificates_on_deletion` is set. The lab issues certificates on
#      purpose, so this is the normal case, not the edge case.
#   3. A deleted CA enters a 30-day soft-delete grace period, and a pool cannot be
#      deleted while it still holds one. `skip_grace_period` is what actually frees the
#      pool in the same `terraform destroy`.
#
# Prove destroy before create: apply this module, destroy it immediately, and confirm in
# the console that the pool and CA are gone rather than pending deletion.
#
# ── And a destroyed id is gone for good ──────────────────────────────────────
#
# The other half of the same behaviour, which only shows up on the REBUILD: CAS never
# releases a resource id. Once the pool is deleted, projects/<p>/locations/<l>/caPools/
# <pool_id> is reserved permanently, and an apply that asks for it again dies with
#
#   Error code 3, message: Previously used CaPool ids may not be reused.
#
# after the service account and its key have already been created. So `pool_id` must be
# single-use — cert_lab_service generates one with a random suffix, and anything driving
# this module by hand has to do the same. `service_account_id` is the same shape of trap
# one namespace up: it is unique per PROJECT, so its default only works for one lab at a
# time, and the caller passes a per-lab id.
#
# ── The lifetime is a protobuf Duration, so it is SECONDS ────────────────────
#
# `lifetime` reaches the API as a google.protobuf.Duration, which takes a count of seconds
# with an `s` suffix and nothing else. Terraform's own duration spelling — "87600h" — is
# a plain string to the provider, so it survives validate and plan and dies at create with
#
#   Error 400: Invalid value at 'certificate_authority.lifetime', Illegal duration format;
#   duration must end with 's'
#
# by which point the pool, the service account, its key and the IAM binding all exist, and
# the pool id is spent for good under the rule above — every retry costs a new one. The
# variable's `validation` block is what moves that failure back to plan time.

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
  required_version = ">= 1.3.0"
}

provider "google" {
  project = var.project
  region  = var.location
}

# ── Variables ────────────────────────────────────────────────────────────────

variable "project" {
  type        = string
  description = "GCP project id that owns the CA pool and the enrollment service account"
}

variable "location" {
  type        = string
  description = "CAS region. The pool, its CAs and any certificate template are all location-scoped and must agree"
}

variable "pool_id" {
  type        = string
  description = "CA pool id — this is the `pool=` value on the plugin's managed-system address. SINGLE-USE: CAS reserves a deleted pool's id permanently, so a rebuild needs a new one (see the header)"
}

variable "tier" {
  type        = string
  default     = "DEVOPS"
  description = "DEVOPS (~$20/mo, no certificate records) or ENTERPRISE. DevOps is the lab choice"
}

variable "ca_id" {
  type        = string
  default     = "demo-root"
  description = "Certificate authority id within the pool"
}

variable "ca_common_name" {
  type        = string
  default     = "Demo Pipeline Root CA"
  description = "CN of the root CA certificate"
}

variable "ca_organization" {
  type        = string
  default     = "Example"
  description = "O of the root CA certificate"
}

variable "ca_lifetime" {
  type        = string
  default     = "315360000s"
  description = "Root CA validity as a protobuf Duration: a count of SECONDS with an `s` suffix (315360000s = 10 years). A Terraform-style unit suffix such as 87600h reaches the API unchanged and fails at create, after the pool exists — see the header"

  validation {
    condition     = can(regex("^[0-9]+([.][0-9]+)?s$", var.ca_lifetime))
    error_message = "ca_lifetime must be seconds with an `s` suffix, e.g. 315360000s for 10 years. CAS takes a protobuf Duration and rejects unit spellings like 87600h."
  }
}

variable "key_algorithm" {
  type        = string
  default     = "EC_P256_SHA256"
  description = "CA key spec. RSA_PKCS1_2048_SHA256 etc. also valid — the plugin's sigalg= must match this FAMILY, not the subject key it generates"
}

variable "service_account_id" {
  type        = string
  default     = "certauth-plugin"
  description = "Account id for the enrollment service account the plugin authenticates as. Unique per PROJECT, so the default holds for one lab only — a second CA in the same project must pass its own, or its apply fails with alreadyExists after the pool exists"
}

variable "labels" {
  type        = map(string)
  default     = {}
  description = "Labels applied to the pool, so the cost breakdown can attribute it"
}

# ── The CA pool ──────────────────────────────────────────────────────────────

resource "google_privateca_ca_pool" "this" {
  name     = var.pool_id
  project  = var.project
  location = var.location
  tier     = var.tier
  labels   = var.labels

  publishing_options {
    # The lab's consumers get the chain out of this module's `ca_chain_pem` output and
    # into an nginx `ssl_client_certificate`, so neither AIA nor CRL publishing is used.
    # A DevOps-tier pool keeps no certificate records and cannot publish a CRL anyway.
    publish_ca_cert = true
    publish_crl     = false
  }
}

# ── The root CA ──────────────────────────────────────────────────────────────

resource "google_privateca_certificate_authority" "this" {
  pool                     = google_privateca_ca_pool.this.name
  certificate_authority_id = var.ca_id
  project                  = var.project
  location                 = var.location
  lifetime                 = var.ca_lifetime
  type                     = "SELF_SIGNED"

  # The three teardown guards. See the header — without all three a `terraform destroy`
  # leaves a pool that is still billing.
  deletion_protection                    = false
  ignore_active_certificates_on_deletion = true
  skip_grace_period                      = true

  key_spec {
    algorithm = var.key_algorithm
  }

  config {
    subject_config {
      subject {
        common_name  = var.ca_common_name
        organization = var.ca_organization
      }
    }
    x509_config {
      ca_options {
        is_ca = true
        # One level: this root signs leaf certificates directly, and nothing below it
        # may itself be a CA.
        max_issuer_path_length = 0
      }
      key_usage {
        base_key_usage {
          cert_sign = true
          crl_sign  = true
        }
        extended_key_usage {
          # The pool must permit what the plugin asks for. A leaf requesting clientAuth
          # from a CA whose EKU set excludes it is refused at issuance, which surfaces as
          # an opaque CAS error rather than a policy message.
          client_auth = true
          server_auth = true
        }
      }
    }
  }
}

# ── The enrollment identity ──────────────────────────────────────────────────
#
# This is the CA half of the plugin's functional account. Its EMAIL becomes the account
# name and the `private_key` field of its JSON key becomes the account password — that
# field's value alone, PEM armour and all, never the whole JSON file. The plugin detects
# a pasted JSON file and says so, but it is the most common setup mistake — which is why
# the dashboard now composes the account itself, out of this apply's outputs, rather than
# printing the key for somebody to paste.
#
# The split between the two credentials is on the LAST colon, and a PEM private key
# contains none, so the key's own `-----BEGIN PRIVATE KEY-----` armour survives intact.

resource "google_service_account" "plugin" {
  account_id   = var.service_account_id
  project      = var.project
  display_name = "Password Safe Certificate plugin"
  description  = "Enrollment identity for the Password Safe Certificate plugin's gcpcas backend"
}

resource "google_privateca_ca_pool_iam_member" "requester" {
  ca_pool = google_privateca_ca_pool.this.id
  # certificateRequester covers privateca.certificates.create and nothing else — the
  # plugin submits CSRs and never manages the pool.
  role   = "roles/privateca.certificateRequester"
  member = "serviceAccount:${google_service_account.plugin.email}"
}

resource "google_service_account_key" "plugin" {
  service_account_id = google_service_account.plugin.name
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "pool_id" {
  value       = google_privateca_ca_pool.this.name
  description = "CA pool id — the `pool=` value on the managed-system address"
}

output "pool_resource_name" {
  value       = google_privateca_ca_pool.this.id
  description = "Full resource name, projects/<p>/locations/<l>/caPools/<pool>"
}

output "location" {
  value       = var.location
  description = "The `location=` value on the managed-system address"
}

output "ca_chain_pem" {
  value       = join("\n", google_privateca_certificate_authority.this.pem_ca_certificates)
  description = "The CA chain the mTLS endpoint must trust — feed this to the nginx endpoint playbook as ssl_client_certificate"
}

output "service_account_email" {
  value       = google_service_account.plugin.email
  description = "The CA half of the functional account's USERNAME: <email>:<bi-run-as-user>"
}

output "service_account_key_json" {
  value       = base64decode(google_service_account_key.plugin.private_key)
  sensitive   = true
  description = "The whole JSON key. The functional account password takes the `private_key` FIELD out of this, not the file — cert_ps_service._ca_credential does that extraction, so the mistake cannot be made by hand"
}
