# Least-privilege IAM policies for a POV instance's AWS cloud provider
# (web_dashboard/services/pov_cloud_aws.py). Two managed policies, because a POV install
# can split its credential in two (docs/profiles/pov/public-cloud.md → Keyless AWS):
#
#   pov-cloud-read       what the request path needs: the overview, the environment list,
#                        Test connection, image lookup and the optional price estimate.
#   pov-cloud-provision  read, plus what the worker's jobs need to build, power and tear
#                        down a POV's VPC, subnets, security groups, gateway and instances,
#                        and to build the standard images (Packer, in its own subnet).
#
# With the SPIFFE identity alone, attach provision to the federated role. With SPIFFE for
# everyday calls and Workload Credentials for jobs, attach read to the federated role and
# provision to the Workload Credentials target role.
#
# Every action below is one pov_cloud_aws.py makes. Destructive and mutating actions are
# conditioned on the povManagedBy tag every POV resource is created with, so the role can
# change nothing it did not build. Two exceptions, both forced by EC2 rather than chosen:
# the VPC's MAIN route table exists before it can be tagged, so CreateRoute and its tagging
# cannot be tag-conditioned; and RunInstances must name an AMI and creates a network
# interface, neither of which carries the request's tags.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_partition" "current" {}

locals {
  p   = data.aws_partition.current.partition
  ec2 = "arn:${local.p}:ec2:*:*"

  tag_key   = "povManagedBy"
  tag_value = "vm-dashboard"

  # The resources a POV creates WITH TagSpecifications, by the action that creates them.
  # CreateTags is only allowed as part of these (ec2:CreateAction), plus the one route
  # table that cannot be tagged at creation.
  creating_actions = [
    "CreateVpc", "CreateSubnet", "CreateSecurityGroup", "CreateInternetGateway",
    "RunInstances", "ImportKeyPair", "CreateKeyPair", "CreateImage",
  ]

  request_tagged = var.tag_conditions ? {
    StringEquals = { "aws:RequestTag/${local.tag_key}" = local.tag_value }
  } : null
  resource_tagged = var.tag_conditions ? {
    StringEquals = { "aws:ResourceTag/${local.tag_key}" = local.tag_value }
  } : null

  read_statements = concat([
    {
      Sid      = "PovRead"
      Effect   = "Allow"
      Action   = ["ec2:Describe*"]
      Resource = "*"
    },
    {
      # Image references resolve through AWS's public SSM parameters (latest AMIs).
      Sid      = "PovPublicImageParameters"
      Effect   = "Allow"
      Action   = ["ssm:GetParameter", "ssm:GetParameters"]
      Resource = "arn:${local.p}:ssm:*::parameter/aws/service/*"
    },
    merge({
      # A POV Windows guest's EC2-generated Administrator password, read when the agent
      # fetches a run's bundle -- the request path, hence here rather than in provision.
      # It comes back encrypted to the POV's own key, which only the dashboard holds.
      Sid      = "PovWindowsPassword"
      Effect   = "Allow"
      Action   = ["ec2:GetPasswordData"]
      Resource = "${local.ec2}:instance/*"
    }, local.resource_tagged == null ? {} : { Condition = local.resource_tagged }),
    ], var.allow_pricing ? [{
      # /pov/cloud's optional list-price estimate. The Pricing API has no resources.
      Sid      = "PovPriceEstimate"
      Effect   = "Allow"
      Action   = ["pricing:GetProducts"]
      Resource = "*"
  }] : [])

  # Statements with a null Condition must not carry the key at all.
  provision_raw = [
    {
      Sid      = "PovCreateTagged"
      Action   = ["ec2:CreateVpc", "ec2:CreateInternetGateway", "ec2:CreateSubnet", "ec2:CreateSecurityGroup"]
      Resource = ["${local.ec2}:vpc/*", "${local.ec2}:internet-gateway/*", "${local.ec2}:subnet/*", "${local.ec2}:security-group/*"]
      Cond     = local.request_tagged
      # CreateSubnet / CreateSecurityGroup also authorize against the PARENT vpc, which
      # carries no request tag — the next statement covers it.
    },
    {
      Sid      = "PovCreateInOwnVpc"
      Action   = ["ec2:CreateSubnet", "ec2:CreateSecurityGroup"]
      Resource = ["${local.ec2}:vpc/*"]
      Cond     = local.resource_tagged
    },
    {
      Sid      = "PovLaunchTagged"
      Action   = ["ec2:RunInstances"]
      Resource = ["${local.ec2}:instance/*", "${local.ec2}:volume/*"]
      Cond     = local.request_tagged
    },
    {
      # The POV's own Windows key: imported for one launch, deleted straight after.
      Sid      = "PovWindowsKeyImport"
      Action   = ["ec2:ImportKeyPair"]
      Resource = ["${local.ec2}:key-pair/*"]
      Cond     = local.request_tagged
    },
    {
      Sid      = "PovWindowsKeyUse"
      Action   = ["ec2:RunInstances", "ec2:DeleteKeyPair"]
      Resource = ["${local.ec2}:key-pair/*"]
      Cond     = local.resource_tagged
    },
    {
      Sid      = "PovLaunchIntoOwnNetwork"
      Action   = ["ec2:RunInstances"]
      Resource = ["${local.ec2}:subnet/*", "${local.ec2}:security-group/*"]
      Cond     = local.resource_tagged
    },
    {
      # Any AMI (catalog, public or a literal id), and the network interface EC2 creates
      # for the instance — neither carries the request's tags.
      Sid      = "PovLaunchImageAndInterface"
      Action   = ["ec2:RunInstances"]
      Resource = ["arn:${local.p}:ec2:*::image/*", "${local.ec2}:image/*", "${local.ec2}:network-interface/*"]
      Cond     = null
    },
    {
      Sid      = "PovTagOnCreate"
      Action   = ["ec2:CreateTags"]
      Resource = ["${local.ec2}:*/*"]
      Cond     = { StringEquals = { "ec2:CreateAction" = local.creating_actions } }
    },
    {
      # The VPC's main route table: created by CreateVpc, untaggable until it exists.
      # Its default route and its tag are the calls that cannot be tag-conditioned;
      # ReplaceRoute repoints the image-build network's route at a recreated gateway.
      Sid      = "PovMainRouteTable"
      Action   = ["ec2:CreateRoute", "ec2:ReplaceRoute", "ec2:CreateTags"]
      Resource = ["${local.ec2}:route-table/*"]
      Cond     = null
    },
    {
      Sid = "PovChangeOwnResources"
      Action = [
        "ec2:ModifyVpcAttribute", "ec2:ModifySubnetAttribute",
        "ec2:AttachInternetGateway", "ec2:DetachInternetGateway",
        "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress",
        "ec2:RevokeSecurityGroupEgress",
        "ec2:StartInstances", "ec2:StopInstances", "ec2:TerminateInstances",
        "ec2:DeleteSecurityGroup", "ec2:DeleteSubnet", "ec2:DeleteInternetGateway",
        "ec2:DeleteVpc",
      ]
      Resource = [
        "${local.ec2}:vpc/*", "${local.ec2}:subnet/*", "${local.ec2}:security-group/*",
        "${local.ec2}:internet-gateway/*", "${local.ec2}:instance/*",
      ]
      Cond = local.resource_tagged
    },
    # The standard-image build (POV → Templates → Images): Packer's amazon-ebs builder in
    # the image-build subnet, with povManagedBy on every resource it creates (run_tags,
    # run_volume_tags, snapshot_tags and the AMI's own tags). The launch, the temporary
    # security group, stopping, terminating and the Windows password read are covered
    # above; these are the calls only Packer makes.
    {
      Sid      = "PovImageBuildKey"
      Action   = ["ec2:CreateKeyPair"]
      Resource = ["${local.ec2}:key-pair/*"]
      Cond     = local.request_tagged
    },
    {
      Sid      = "PovImageFromOwnInstance"
      Action   = ["ec2:CreateImage"]
      Resource = ["${local.ec2}:instance/*"]
      Cond     = local.resource_tagged
    },
    {
      # An AMI and its snapshots: tagged by CreateImage itself, then again by Packer's
      # tagging step (a plain CreateTags, so it is the request tags that are checked).
      Sid    = "PovImageTagged"
      Action = ["ec2:CreateImage", "ec2:CreateTags"]
      Resource = [
        "arn:${local.p}:ec2:*::image/*", "${local.ec2}:image/*",
        "arn:${local.p}:ec2:*::snapshot/*", "${local.ec2}:snapshot/*",
      ]
      Cond = local.request_tagged
    },
    {
      # Packer's cleanup of a cancelled build's half-made AMI.
      Sid    = "PovImageCleanup"
      Action = ["ec2:DeregisterImage", "ec2:DeleteSnapshot"]
      Resource = [
        "arn:${local.p}:ec2:*::image/*", "${local.ec2}:image/*",
        "arn:${local.p}:ec2:*::snapshot/*", "${local.ec2}:snapshot/*",
      ]
      Cond = local.resource_tagged
    },
    {
      # Authorize* also creates security-group-rule resources, which carry no tags.
      Sid      = "PovSecurityGroupRules"
      Action   = ["ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupEgress"]
      Resource = ["${local.ec2}:security-group-rule/*"]
      Cond     = null
    },
  ]

  provision_statements = [
    for s in local.provision_raw : merge(
      { Sid = s.Sid, Effect = "Allow", Action = s.Action, Resource = s.Resource },
      s.Cond == null ? {} : { Condition = s.Cond },
    )
  ]
}

resource "aws_iam_policy" "read" {
  name        = "${var.name_prefix}-read"
  description = "vm-dashboard POV cloud provider: read-only (request path)."
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.read_statements
  })
  tags = var.tags
}

resource "aws_iam_policy" "provision" {
  name        = "${var.name_prefix}-provision"
  description = "vm-dashboard POV cloud provider: build, power and tear down POV environments (jobs)."
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = concat(local.read_statements, local.provision_statements)
  })
  tags = var.tags
}
