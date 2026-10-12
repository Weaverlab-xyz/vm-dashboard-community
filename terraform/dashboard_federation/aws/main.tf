# AWS trust for the dashboard's own SPIFFE identity: an IAM OIDC provider for its issuer,
# and the role it assumes with AssumeRoleWithWebIdentity. No access key exists anywhere.
#
# The condition pins the exact subject, spiffe://<trust domain>/dashboard, and the
# audience the dashboard mints for AWS. A trust domain is shared with remote agents and
# service-account workloads, so trusting the domain alone would let any of them in.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 5.81 makes the OIDC provider's thumbprint_list optional; AWS validates a public
      # CA's certificate itself, so there is no thumbprint to pin or rotate.
      version = ">= 5.81"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  issuer_host = trimprefix(var.issuer_url, "https://")
  subject     = "spiffe://${var.trust_domain}/dashboard"
  # IAM allows one OIDC provider per URL: reuse one that exists rather than fail.
  oidc_provider_arn = var.oidc_provider_arn != "" ? var.oidc_provider_arn : one(aws_iam_openid_connect_provider.spiffe[*].arn)
}

resource "aws_iam_openid_connect_provider" "spiffe" {
  count          = var.oidc_provider_arn == "" ? 1 : 0
  url            = var.issuer_url
  client_id_list = ["sts.amazonaws.com"]
  tags           = var.tags
}

resource "aws_iam_role" "dashboard" {
  name                 = var.role_name
  description          = "Assumed by the vm-dashboard's own SPIFFE identity; no stored key."
  max_session_duration = var.max_session_duration

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.issuer_host}:sub" = local.subject
          "${local.issuer_host}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "dashboard" {
  for_each   = toset(var.policy_arns)
  role       = aws_iam_role.dashboard.name
  policy_arn = each.value
}

# With wlc_aws_everyday_source = federation this role is only the EVERYDAY credential:
# jobs take Workload Credentials' provisioning lease. The same deny the WLC everyday
# secret carries, so the request path cannot create or hand out roles.
resource "aws_iam_role_policy" "everyday_only" {
  count = var.everyday_only ? 1 : 0
  name  = "no-privilege-escalation"
  role  = aws_iam_role.dashboard.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "NoPrivilegeEscalation"
      Effect = "Deny"
      Action = [
        "iam:PassRole",
        "iam:CreateRole",
        "iam:AttachRolePolicy",
        "iam:PutRolePolicy",
        "iam:UpdateAssumeRolePolicy",
        "iam:CreateInstanceProfile",
        "iam:AddRoleToInstanceProfile",
        "iam:CreateServiceLinkedRole",
        "sts:AssumeRole",
        "organizations:*",
        "account:*",
      ]
      Resource = "*"
    }]
  })
}
