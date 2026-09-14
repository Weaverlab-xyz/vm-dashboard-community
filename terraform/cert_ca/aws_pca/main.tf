# AWS Private Certificate Authority — a private CA for the Password Safe Certificate
# plugin's `awspca` backend, built to be DESTROYED.
#
# The sibling `../gcp_cas` module is the one to read first: this is its counterpart, and
# where the two differ the difference is AWS's, not a change of mind.
#
# ── Cost is why the timer is not optional here ────────────────────────────────
#
# `web_dashboard/services/expiry_policy.py` records the figures this lab is designed
# around: a GCP CAS DevOps-tier pool bills ~$20/month, an AWS Private CA ~$400/month,
# either way whether or not it ever issues a certificate. That twenty-fold gap is why
# `cert_lab_service.provision` refuses to build an AWS CA on an instance where the expiry
# reaper would stamp no timer — a forgotten pool is an annoyance, a forgotten Private CA
# is a line item somebody has to explain.
#
# ── Teardown ─────────────────────────────────────────────────────────────────
#
# The GCP module needs three separate guards to make `terraform destroy` actually free
# the pool. ACM PCA's shape is different, and two things here are deliberate:
#
#   1. `permanent_deletion_time_in_days` defaults to 30. A deleted CA sits restorable for
#      that window, so the default leaves the longest possible tail on a resource this
#      lab exists to get rid of. 7 is the API's floor and what this module asks for.
#   2. `force_destroy` on the IAM user. Terraform destroys the access key below before
#      the user, so an ordinary teardown is fine — this covers the case where somebody
#      added a second key or an inline policy in the console, which otherwise fails the
#      destroy with "cannot be deleted: entity must be removed from all groups".
#
# Prove destroy before create: apply this module, destroy it immediately, and confirm in
# the console that the CA is gone rather than merely disabled, and that the IAM user went
# with it.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  required_version = ">= 1.3.0"
}

provider "aws" {
  region = var.region
}

# The partition, so the template ARN below is right in GovCloud and China as well as the
# commercial regions. A hardcoded `arn:aws:` is the usual way this module would break
# somewhere nobody tests.
data "aws_partition" "current" {}

# ── Variables ────────────────────────────────────────────────────────────────

variable "region" {
  type        = string
  description = "AWS region that owns the CA — a Private CA is a regional resource, and the `region=` value on the managed-system address"
}

variable "ca_common_name" {
  type        = string
  default     = "Demo Pipeline Root CA"
  description = "CN on the root certificate's subject"
}

variable "ca_organization" {
  type        = string
  default     = "Example"
  description = "O on the root certificate's subject"
}

variable "ca_validity_years" {
  type        = number
  default     = 10
  description = "Lifetime of the self-signed root certificate, matching gcp_cas's 315360000s. ACM PCA takes a unit and a count, so this side needs no duration-format care"
}

variable "key_algorithm" {
  type        = string
  default     = "EC_prime256v1"
  description = "CA key algorithm. The AWS spelling of gcp_cas's EC_P256_SHA256 — change this and signing_algorithm together, an EC key with an RSA signing algorithm is refused at creation"
}

variable "signing_algorithm" {
  type        = string
  default     = "SHA256WITHECDSA"
  description = "Signing algorithm, which must match the key's family — SHA256WITHRSA for an RSA_* key"
}

variable "usage_mode" {
  type        = string
  default     = "GENERAL_PURPOSE"
  description = "GENERAL_PURPOSE issues certificates of any lifetime. SHORT_LIVED_CERTIFICATE is cheaper per certificate but caps them at seven days, which the rotation demo cannot use"
}

variable "ca_path_length" {
  type        = number
  default     = 0
  description = "How many CAs this root may have beneath it. 0 issues end-entity certificates only; 1 lets the Password Safe Subordinate CA platform obtain a subordinate from it. ACM PCA derives no structural flag from a submitted CSR — it builds every certificate from one of its own templates — so this selects which template ARNs the enrollment policy permits, and a root created with 0 must not be allowed to issue a SubordinateCACertificate"

  validation {
    condition     = var.ca_path_length >= 0 && var.ca_path_length <= 1
    error_message = "ca_path_length is 0 (leaf-issuing) or 1 (may sign one subordinate CA, which then issues leaves). Deeper hierarchies are a real PKI design rather than a lab."
  }
}

variable "iam_user_name" {
  type        = string
  default     = "certauth-plugin"
  description = "Name of the IAM user that holds the plugin's enrollment credential. Unique per ACCOUNT, so the default holds for one lab only — a second CA in the same account must pass its own, or its apply fails with EntityAlreadyExists after the CA exists"
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Tags applied to the CA, so the cost breakdown can attribute it"
}

# ── The certificate authority ────────────────────────────────────────────────
#
# Created in PENDING_CERTIFICATE: a ROOT CA is not usable until something signs its own
# CSR and installs the result, which is the two resources after this one. All three are
# one `terraform apply` — the split is ACM PCA's, not a staged rollout.

resource "aws_acmpca_certificate_authority" "this" {
  type       = "ROOT"
  usage_mode = var.usage_mode
  enabled    = true

  # See the header. The default of 30 leaves a deleted CA restorable for a month; 7 is
  # the floor the API accepts.
  permanent_deletion_time_in_days = 7

  certificate_authority_configuration {
    key_algorithm     = var.key_algorithm
    signing_algorithm = var.signing_algorithm

    subject {
      common_name  = var.ca_common_name
      organization = var.ca_organization
    }
  }

  tags = var.tags
}

# Self-signs the CA's own CSR. `template_arn` is what makes the result a CA certificate
# rather than a leaf — with the end-entity template the install below is refused.
resource "aws_acmpca_certificate" "root" {
  certificate_authority_arn   = aws_acmpca_certificate_authority.this.arn
  certificate_signing_request = aws_acmpca_certificate_authority.this.certificate_signing_request
  signing_algorithm           = var.signing_algorithm
  template_arn                = "arn:${data.aws_partition.current.partition}:acm-pca:::template/RootCACertificate/V1"

  validity {
    type  = "YEARS"
    value = var.ca_validity_years
  }
}

# Installs it, which is what moves the CA to ACTIVE. No `certificate_chain`: a root has
# nothing above it, and passing an empty one is an error rather than a no-op.
resource "aws_acmpca_certificate_authority_certificate" "this" {
  certificate_authority_arn = aws_acmpca_certificate_authority.this.arn
  certificate               = aws_acmpca_certificate.root.certificate
}

# ── The enrollment identity ──────────────────────────────────────────────────
#
# This is the CA half of the plugin's functional account, the same role
# `google_service_account.plugin` plays in the GCP module. There the account name is the
# service account's email and the password is the `private_key` FIELD of its JSON key;
# here the two halves are the access key id and the secret, which is the simpler of the
# two to get right.
#
# A static IAM user rather than a role, because the plugin's address grammar has no
# assume-role option — `ps_resource_service._CERT_BACKEND_KEYS` allows `arn`, `region`,
# `sigalg`, `templatearn` and `wait` on an awspca address and nothing else. The secret is
# an output for an operator to move into Password Safe by hand; nothing in the dashboard
# reads or stores it, exactly as with the GCP key.

resource "aws_iam_user" "plugin" {
  name = var.iam_user_name
  # See the header — this is a teardown guard, not a convenience.
  force_destroy = true
  tags          = var.tags
}

resource "aws_iam_user_policy" "plugin" {
  name = "certauth-plugin-issue"
  user = aws_iam_user.plugin.name

  # Scoped to THIS CA and to issuance. The GCP module grants
  # `roles/privateca.certificateRequester`, which covers creating certificates and
  # nothing else; these actions are that role's ACM PCA equivalent — submit a CSR,
  # collect the result, read the CA enough to build a chain, and revoke.
  #
  # `RevokeCertificate` is here because revocation is a real plugin feature rather than a
  # future one: `RevokeOnDisable` defaults ON, so Disable Managed Account revokes the
  # certificate the account holds, and `RevokeOnRenewal` withdraws a superseded one.
  # Without the permission the plugin does not fall back to succeeding-without-revoking —
  # that path is for a backend with NO revocation operation, and ACM PCA has one — so
  # Disable fails with an authorization error that reads like a credential problem.
  #
  # ── The TemplateArn condition, and why the path length decides it ───────────
  #
  # ACM PCA ignores the basic-constraints and key-usage extensions in a submitted CSR and
  # builds the certificate from its template instead. So the template ARN is the only
  # thing that decides whether what comes back is a leaf or a certificate AUTHORITY —
  # which makes it exactly the right thing to scope this policy on, and the scoping is
  # meaningful rather than decorative.
  #
  # On a leaf-only CA the condition pins end-entity templates, so a subordinate request
  # is refused by IAM, at the CA, naming the template. On a subordinate-capable one the
  # condition is dropped, because the plugin legitimately names a
  # SubordinateCACertificate_PathLen{N} template and a condition pinning the end-entity
  # one would refuse it with an authorization error that reads like a credential problem.
  # ``StringNotLikeIfExists``, and the ``IfExists`` half is load-bearing twice over. An
  # absent condition key makes a plain ``StringNotLike`` evaluate FALSE — so it would
  # deny the ordinary case, where the address names no templatearn= at all and the
  # service defaults to EndEntityCertificate/V1, and it would also deny the four actions
  # in this statement that carry no TemplateArn key to compare.
  #
  # Built with merge() rather than a ternary on the Condition key: jsonencode writes a
  # null attribute out as a literal ``"Condition": null``, which IAM rejects, so the key
  # has to be absent rather than null.
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [merge(local.enroll_statement, local.enroll_template_condition)]
  })
}

locals {
  enroll_statement = {
    Effect = "Allow"
    Action = [
      "acm-pca:IssueCertificate",
      "acm-pca:GetCertificate",
      "acm-pca:RevokeCertificate",
      "acm-pca:DescribeCertificateAuthority",
      "acm-pca:GetCertificateAuthorityCertificate",
    ]
    Resource = aws_acmpca_certificate_authority.this.arn
  }

  # Empty on a subordinate-capable CA, so the key is ABSENT rather than null — jsonencode
  # writes a null attribute out as a literal `"Condition": null`, which IAM rejects.
  enroll_template_condition = var.ca_path_length > 0 ? {} : {
    Condition = {
      StringNotLikeIfExists = {
        "acm-pca:TemplateArn" = "arn:${data.aws_partition.current.partition}:acm-pca:::template/SubordinateCACertificate*"
      }
    }
  }
}

resource "aws_iam_access_key" "plugin" {
  user = aws_iam_user.plugin.name
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "ca_arn" {
  value       = aws_acmpca_certificate_authority.this.arn
  description = "The `arn=` value on the managed-system address — the one option an awspca address cannot be built without"
}

output "ca_path_length" {
  value       = var.ca_path_length
  description = "What this root permits beneath it. 0 = leaf-issuing only, and the enrollment policy refuses a SubordinateCACertificate template; 1 = the Password Safe Subordinate CA platform may obtain a subordinate from it. Echoed so the recorded row and the built CA cannot disagree"
}

output "region" {
  value       = var.region
  description = "The `region=` value on the managed-system address"
}

output "ca_chain_pem" {
  # Read off the signing resource rather than off the CA. The CA's own `certificate`
  # attribute is populated by a refresh AFTER activation, so on the apply that creates it
  # this would be empty — and an empty chain is stored on the row and only noticed when
  # the mTLS endpoint refuses every client.
  value       = aws_acmpca_certificate.root.certificate
  description = "The CA chain the mTLS endpoint must trust — feed this to the nginx endpoint playbook as ssl_client_certificate"
  depends_on  = [aws_acmpca_certificate_authority_certificate.this]
}

output "enroll_access_key_id" {
  value       = aws_iam_access_key.plugin.id
  description = "The CA half of the functional account's USERNAME: <access key id>:<bi-run-as-user>"
}

output "enroll_secret_access_key" {
  value       = aws_iam_access_key.plugin.secret
  sensitive   = true
  description = "The functional account's password, used as-is (unlike the gcp_cas key, which is a container). The dashboard reads it from this apply's outputs straight into Password Safe and never stores it on a row"
}
