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
  description = "CA pool id — this is the `pool=` value on the plugin's managed-system address"
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
  default     = "87600h"
  description = "Root CA validity, in seconds-with-suffix (87600h = 10 years)"
}

variable "key_algorithm" {
  type        = string
  default     = "EC_P256_SHA256"
  description = "CA key spec. RSA_PKCS1_2048_SHA256 etc. also valid — the plugin's sigalg= must match this FAMILY, not the subject key it generates"
}

variable "service_account_id" {
  type        = string
  default     = "certauth-plugin"
  description = "Account id for the enrollment service account the plugin authenticates as"
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
# a pasted JSON file and says so, but it is the most common setup mistake.
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
  description = "The whole JSON key. The functional account password takes the `private_key` FIELD out of this, not the file"
}
