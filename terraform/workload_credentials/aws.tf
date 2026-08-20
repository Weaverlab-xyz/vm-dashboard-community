# The AWS side of Workload Credentials: a two-hop trust chain, a target role carrying the
# dashboard's existing permissions, and one or two dynamic secrets over that role.
#
#   BeyondTrust bridge role   (theirs)
#        └─ assumes → integration role   (ours; trust + sts:ExternalId)
#             └─ assumes → target role   (ours; sts:AssumeRole + sts:TagSession)
#                              ^ carries dashboard-app-policy

locals {
  aws_enabled = var.enable_aws ? 1 : 0

  # Both dynamic secrets point at this one role and differ only by session policy.
  aws_common_tags = merge(var.tags, { component = "workload-credentials" })
}

# The external ID is a REQUIRED input on the integration, not something the service hands
# back — the wiki's example reads it as an attribute, which is out of date. Generated here
# so it is strong and so the same value reaches both the integration and the trust
# condition; a human-chosen one is the usual way this control ends up worthless.
resource "random_uuid" "aws_external_id" {
  count = local.aws_enabled
}

# ── The permissions the target role carries ──────────────────────────────────
# Looked up rather than defined. See var.aws_dashboard_policy_arn for why.

data "aws_iam_policy" "dashboard" {
  count = var.enable_aws && var.aws_dashboard_policy_arn == "" ? 1 : 0
  name  = var.aws_dashboard_policy_name
}

locals {
  # Splat + one() rather than [0]: a conditional's untaken branch is not reliably lazy in
  # Terraform, so indexing a count=0 resource can error at plan time even when the guard
  # says it should not be reached. one([]) is null, and coalesce turns that into "".
  dashboard_policy_arn = var.aws_dashboard_policy_arn != "" ? var.aws_dashboard_policy_arn : coalesce(
    one(data.aws_iam_policy.dashboard[*].arn), ""
  )
}

# ── Hop 1: the role BeyondTrust's bridge assumes ─────────────────────────────

resource "aws_iam_role" "integration" {
  count       = local.aws_enabled
  name        = var.aws_integration_role_name
  path        = "/beyondtrust/"
  description = "Assumed by the BeyondTrust Workload Credentials bridge; assumes the dashboard target role."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = var.bt_bridge_role_arn }
      Action    = "sts:AssumeRole"
      Condition = {
        # Confused-deputy prevention: without this, anyone else BeyondTrust serves could
        # name your role ARN and be let in.
        StringEquals = { "sts:ExternalId" = random_uuid.aws_external_id[0].result }
      }
    }]
  })

  tags = local.aws_common_tags
}

# It only needs to reach the target role. Nothing else.
resource "aws_iam_role_policy" "integration_assume_target" {
  count = local.aws_enabled
  name  = "assume-dashboard-target"
  role  = aws_iam_role.integration[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      # sts:TagSession as well as AssumeRole: the dynamic secret's aws_tags become STS
      # session tags, and without this the tagged assume is refused rather than untagged.
      Action   = ["sts:AssumeRole", "sts:TagSession"]
      Resource = aws_iam_role.target[0].arn
    }]
  })
}

# ── Hop 2: the role that actually holds the dashboard's permissions ──────────

resource "aws_iam_role" "target" {
  count       = local.aws_enabled
  name        = var.aws_target_role_name
  description = "Assumed via Workload Credentials; carries the vm-dashboard permission set."

  # The credential TTL ceiling. AWS defaults this to 3600 and silently clamps a longer
  # request, which reads as a bug in the refresh logic rather than a role attribute.
  max_session_duration = var.aws_max_session_duration

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.integration[0].arn }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })

  tags = local.aws_common_tags
}

resource "aws_iam_role_policy_attachment" "target_dashboard" {
  count      = local.aws_enabled
  role       = aws_iam_role.target[0].name
  policy_arn = local.dashboard_policy_arn
}

# ── The Workload Credentials side ────────────────────────────────────────────

resource "beyondtrust_workload_credentials_aws_integration" "this" {
  count       = local.aws_enabled
  name        = var.aws_integration_name
  role_arn    = aws_iam_role.integration[0].arn
  external_id = random_uuid.aws_external_id[0].result

  # The trust policy has to exist before BeyondTrust validates the role, and the inline
  # policy has to exist before the integration is any use.
  depends_on = [aws_iam_role_policy.integration_assume_target]
}

# The provisioning lease: the full permission set. The dashboard mints this when a job
# starts and never pre-warms it.
resource "beyondtrust_workload_credentials_aws_dynamic_secret" "provision" {
  count            = local.aws_enabled
  name             = "dashboard-provision"
  folder           = local.folder_path
  integration_name = beyondtrust_workload_credentials_aws_integration.this[0].name

  credential_type = "assumed_role"
  role_arn        = aws_iam_role.target[0].arn
  ttl             = var.aws_ttl_seconds

  # Session tags, so CloudTrail attributes each API call to the secret that issued the
  # credential. This is why the trust policies above allow sts:TagSession.
  aws_tags = merge(var.tags, { wlc-purpose = "provision" })

  depends_on = [aws_iam_role_policy_attachment.target_dashboard]
}

# The everyday lease: the same role, minus IAM.
#
# Expressed as allow-everything plus an explicit deny rather than an allow-list, for two
# reasons. A session policy INTERSECTS the role's permissions, so `Allow *` grants nothing
# the role does not already have — it just declines to narrow. And Deny always wins, so
# the deny block is exact: it removes precisely the escalation paths and nothing else.
#
# An allow-list would have to enumerate every action the dashboard uses on the request
# path, and would silently break a page the day someone added a service. This cannot.
resource "beyondtrust_workload_credentials_aws_dynamic_secret" "everyday" {
  count            = var.enable_aws && var.create_everyday_secret ? 1 : 0
  name             = "dashboard-everyday"
  folder           = local.folder_path
  integration_name = beyondtrust_workload_credentials_aws_integration.this[0].name

  credential_type = "assumed_role"
  role_arn        = aws_iam_role.target[0].arn
  ttl             = var.aws_ttl_seconds

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InheritTheRole"
        Effect   = "Allow"
        Action   = "*"
        Resource = "*"
      },
      {
        Sid    = "NoPrivilegeEscalation"
        Effect = "Deny"
        Action = [
          # The two the split exists to remove.
          "iam:PassRole",
          "iam:CreateRole",
          "iam:AttachRolePolicy",
          "iam:PutRolePolicy",
          "iam:UpdateAssumeRolePolicy",
          "iam:CreateInstanceProfile",
          "iam:AddRoleToInstanceProfile",
          "iam:CreateServiceLinkedRole",
          # Pivoting to another role would route around the whole thing.
          "sts:AssumeRole",
          "sts:AssumeRoleWithWebIdentity",
          # Nothing on the request path touches these, and they are the other way out.
          "organizations:*",
          "account:*",
        ]
        Resource = "*"
      },
    ]
  })

  aws_tags = merge(var.tags, { wlc-purpose = "everyday" })

  depends_on = [aws_iam_role_policy_attachment.target_dashboard]
}
