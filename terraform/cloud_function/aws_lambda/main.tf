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

# ── Variables ────────────────────────────────────────────────────────────────

variable "region" {
  type        = string
  description = "AWS region for the function"
}

variable "name" {
  type        = string
  description = "Lambda function name (unique per region)"
}

variable "workload" {
  type        = string
  description = "Workload packaged into the zip (fnworkloads/<workload>.py); surfaced to the handler as FN_WORKLOAD"
}

# The package is referenced from S3 rather than built inline with archive_file:
# archive_file needs the source tree present in the deploy dir at DESTROY time as
# well as apply, and it zips with real mtimes, which are not stable across the app
# container and the jobs-worker container. S3 also gives a durable, auditable
# artifact and keeps terraform/deployments/<job_id> small.
variable "package_bucket" {
  type        = string
  description = "S3 bucket holding the function package"
}

variable "package_key" {
  type        = string
  description = "S3 key of the package (function-packages/<fn_id>/<sha256>.zip)"
}

variable "package_sha256_b64" {
  type        = string
  description = "base64(sha256(zip)) — what source_code_hash compares against, so an unchanged package is a no-op"
}

variable "shared_secret" {
  type        = string
  sensitive   = true
  description = "Bearer secret the handler verifies on every request (fnruntime.auth). Independent of the front door below."
}

variable "auth_mode" {
  type        = string
  default     = "AWS_IAM"
  description = "Function URL front door: AWS_IAM (SigV4 required) or NONE (public URL; the shared secret is then the only gate)"
  validation {
    condition     = contains(["AWS_IAM", "NONE"], var.auth_mode)
    error_message = "auth_mode must be AWS_IAM or NONE."
  }
}

variable "runtime" {
  type        = string
  default     = "python3.12"
  description = "Lambda Python runtime"
}

variable "timeout_seconds" {
  type        = number
  default     = 60
  description = "Function timeout. Keep above the handler's own probe timeouts."
}

variable "memory_mb" {
  type        = number
  default     = 256
  description = "Memory (also scales CPU)"
}

variable "log_retention_days" {
  type        = number
  default     = 14
  description = "CloudWatch log retention. The log group is managed here so destroy removes it instead of leaking."
}

variable "environment" {
  type        = map(string)
  default     = {}
  description = "Extra NON-SECRET environment variables for the handler"
}

# Secrets Manager read for workloads that resolve a credential at cold start
# (db_grant). AWS is the only one of the three clouds with no platform-resolved
# env-var secret for functions — GCP has secret_environment_variables and Azure has
# Key Vault references — so the function reads it itself, with boto3 the runtime
# already ships. Scoped to named ARNs: a wildcard here would let any workload read
# every secret in the account.
variable "readable_secret_arns" {
  type        = list(string)
  default     = []
  description = "Secrets Manager ARNs this function may read. Empty = no access."
}

# ── Networking (optional) ─────────────────────────────────────────────────────
#
# Empty subnet_ids = a public function with normal internet egress. Non-empty =
# attached to the VPC, which is what lets it reach a private database — and which
# also REMOVES its internet access unless the subnets route through NAT.

variable "subnet_ids" {
  type        = list(string)
  default     = []
  description = "Private subnets to attach the function to. Empty = no VPC attachment."
}

variable "security_group_ids" {
  type        = list(string)
  default     = []
  description = "Security groups for the function ENIs (typically one already allowed into the DB SG)"
}

locals {
  vpc_attached = length(var.subnet_ids) > 0
  tags = {
    ManagedBy = "vm-dashboard"
    Workload  = var.workload
  }
}

# ── IAM ──────────────────────────────────────────────────────────────────────

resource "aws_iam_role" "this" {
  name = "${var.name}-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "basic" {
  role       = aws_iam_role.this.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Attached ONLY when VPC-attached, and mandatory then: without it the function
# creates successfully and every single invoke fails with an ENI error, which
# looks like a code problem and is not one.
resource "aws_iam_role_policy_attachment" "vpc" {
  count      = local.vpc_attached ? 1 : 0
  role       = aws_iam_role.this.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# ── Function ─────────────────────────────────────────────────────────────────

# Managed explicitly (rather than left to Lambda's implicit creation) so that
# retention is bounded and `terraform destroy` takes the logs with it.
resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

resource "aws_lambda_function" "this" {
  function_name = var.name
  role          = aws_iam_role.this.arn
  handler       = "aws_entry.lambda_handler"
  runtime       = var.runtime
  timeout       = var.timeout_seconds
  memory_size   = var.memory_mb

  s3_bucket        = var.package_bucket
  s3_key           = var.package_key
  source_code_hash = var.package_sha256_b64

  environment {
    variables = merge(var.environment, {
      # The ID, never the value. A Lambda's environment is returned in full by
      # lambda:GetFunctionConfiguration — which the AWS-managed ReadOnlyAccess policy
      # grants — so the bearer token living here made every read-only principal in
      # the account able to call the function. fnruntime.secretref resolves it from
      # Secrets Manager at cold start instead, which only this role may read.
      FN_SHARED_SECRET_SECRET_ID = aws_secretsmanager_secret.shared_secret.arn
      FN_WORKLOAD                = var.workload
      FN_CLOUD                   = "aws"
      FN_REGION                  = var.region
      FN_NAME                    = var.name
      FN_NETWORK_MODE            = local.vpc_attached ? "vpc" : "public"
      FN_SUBNETS                 = join(",", var.subnet_ids)
    })
  }

  dynamic "vpc_config" {
    for_each = local.vpc_attached ? [1] : []
    content {
      subnet_ids         = var.subnet_ids
      security_group_ids = var.security_group_ids
    }
  }

  tags = local.tags

  depends_on = [
    aws_iam_role_policy_attachment.basic,
    aws_iam_role_policy_attachment.vpc,
    aws_cloudwatch_log_group.this,
    # Both are needed before the first invocation, and neither is implied by the
    # ARN reference above: a function that starts before the version exists, or
    # before it may read it, fails auth closed with a 500 and looks broken.
    aws_secretsmanager_secret_version.shared_secret,
    aws_iam_role_policy.secrets,
  ]

  lifecycle {
    precondition {
      condition     = !local.vpc_attached || length(var.security_group_ids) > 0
      error_message = "A VPC-attached function needs at least one security group; otherwise its ENIs get the default SG and the DB path silently fails."
    }
  }
}

# GCP puts the bearer secret in Secret Manager and Azure resolves it from Key Vault,
# so AWS is the odd one out — and the most exposed, because a Lambda's environment is
# readable with ReadOnlyAccess. The module owns the secret rather than taking a
# reference: it has no independent lifetime, and requiring an operator to pre-create
# one per function would make the simplest deploy a two-step.
resource "aws_secretsmanager_secret" "shared_secret" {
  name = "${var.name}-fn-secret"
  # No soft-delete window. A per-function secret is worthless once the function is
  # gone, and the 7-day default makes redeploying a function of the SAME NAME fail
  # with "already scheduled for deletion" — a wall you hit right after a destroy,
  # with a message that does not suggest waiting a week is the alternative.
  recovery_window_in_days = 0
  tags                    = local.tags
}

resource "aws_secretsmanager_secret_version" "shared_secret" {
  secret_id     = aws_secretsmanager_secret.shared_secret.id
  secret_string = var.shared_secret
}

# Always created now: the function reads its OWN bearer secret through this policy,
# so it is no longer conditional on a workload needing a credential.
resource "aws_iam_role_policy" "secrets" {
  name = "${var.name}-secrets"
  role = aws_iam_role.this.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["secretsmanager:GetSecretValue"]
      # The function's own secret, plus whatever the workload was granted. Named
      # ARNs only — never a wildcard.
      Resource = concat([aws_secretsmanager_secret.shared_secret.arn], var.readable_secret_arns)
    }]
  })
}

resource "aws_lambda_function_url" "this" {
  function_name      = aws_lambda_function.this.function_name
  authorization_type = var.auth_mode
}

# A function URL with authorization_type = NONE still needs a resource-based policy
# granting lambda:InvokeFunctionUrl to everyone. CreateFunctionUrlConfig does NOT add
# it — the AWS console does, silently, which is why this is easy to miss. Without
# this the public URL answers {"Message":"Forbidden"} on every request, including the
# dashboard's own Test invoke, and it looks like a bad bearer secret rather than a
# missing policy. The shared secret remains the actual gate (fnruntime.auth).
resource "aws_lambda_permission" "public_url" {
  count                  = var.auth_mode == "NONE" ? 1 : 0
  statement_id           = "AllowPublicFunctionUrlInvoke"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.this.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "resource_id" {
  value       = aws_lambda_function.this.arn
  description = "Function ARN"
}

output "invoke_url" {
  value       = aws_lambda_function_url.this.function_url
  description = "HTTPS endpoint — what an Entitle REST integration posts to"
}

output "log_group" {
  value       = aws_cloudwatch_log_group.this.name
  description = "CloudWatch log group carrying the handler's structured JSON lines"
}

output "network_mode" {
  value       = local.vpc_attached ? "vpc" : "public"
  description = "Whether the function was attached to the VPC"
}
