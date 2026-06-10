#!/usr/bin/env bash
# AWS sandbox bootstrap for the VM Dashboard.
#
# Creates a small, isolated VPC where:
#   • A public subnet hosts the BeyondTrust ECS Fargate Jumpoint task — it
#     reaches the internet via an Internet Gateway so the Jumpoint can phone
#     home to PRA's relay.
#   • A private subnet hosts deployed EC2 instances — no IGW route, only
#     local VPC traffic, so the lab VMs themselves never reach the internet
#     except through the Jumpoint.
#
# Also creates an SSH keypair JSON in Secrets Manager and an IAM role for
# Fargate task execution. Prints a config block to paste into the dashboard's
# /setup wizard.

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"

require_supported_os
require_cmd aws
require_cmd jq

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-2}}"
NAME="${SANDBOX_NAME_PREFIX}"

ensure_logged_in "aws" \
  "aws sts get-caller-identity --region $REGION" \
  "Run: aws configure  (or: aws sso login)"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
section "AWS sandbox in account $ACCOUNT_ID, region $REGION"

# Common CLI tag spec used on every create call.
tag_spec() {
  local resource="$1" name="$2"
  printf '%s' \
    "ResourceType=$resource,Tags=[{Key=Name,Value=$name},{Key=$SANDBOX_TAG_KEY,Value=$SANDBOX_TAG_VALUE}]"
}

# ── 1. VPC ────────────────────────────────────────────────────────────────────
section "VPC"
VPC_ID="$(aws ec2 describe-vpcs --region "$REGION" \
  --filters "Name=tag:$SANDBOX_TAG_KEY,Values=$SANDBOX_TAG_VALUE" \
  --query 'Vpcs[0].VpcId' --output text 2>/dev/null || true)"
if [[ "$VPC_ID" == "None" || -z "$VPC_ID" ]]; then
  VPC_ID="$(aws ec2 create-vpc --region "$REGION" \
    --cidr-block 10.99.0.0/16 \
    --tag-specifications "$(tag_spec vpc "${NAME}-vpc")" \
    --query 'Vpc.VpcId' --output text)"
  aws ec2 modify-vpc-attribute --region "$REGION" --vpc-id "$VPC_ID" --enable-dns-hostnames
  ok "Created VPC $VPC_ID (10.99.0.0/16)"
else
  ok "Reusing VPC $VPC_ID"
fi
state_write aws vpc_id "$VPC_ID"

# ── 2. Internet Gateway (only the public subnet routes to it) ─────────────────
section "Internet Gateway"
IGW_ID="$(aws ec2 describe-internet-gateways --region "$REGION" \
  --filters "Name=tag:$SANDBOX_TAG_KEY,Values=$SANDBOX_TAG_VALUE" \
  --query 'InternetGateways[0].InternetGatewayId' --output text 2>/dev/null || true)"
if [[ "$IGW_ID" == "None" || -z "$IGW_ID" ]]; then
  IGW_ID="$(aws ec2 create-internet-gateway --region "$REGION" \
    --tag-specifications "$(tag_spec internet-gateway "${NAME}-igw")" \
    --query 'InternetGateway.InternetGatewayId' --output text)"
  aws ec2 attach-internet-gateway --region "$REGION" --vpc-id "$VPC_ID" --internet-gateway-id "$IGW_ID"
  ok "Created and attached IGW $IGW_ID"
else
  ok "Reusing IGW $IGW_ID"
fi
state_write aws igw_id "$IGW_ID"

# ── 3. Subnets ────────────────────────────────────────────────────────────────
section "Subnets"
AZ="$(aws ec2 describe-availability-zones --region "$REGION" \
  --query 'AvailabilityZones[0].ZoneName' --output text)"
# A second AZ is required for the RDS DB subnet group (RDS spans >= 2 AZs).
AZ2="$(aws ec2 describe-availability-zones --region "$REGION" \
  --query 'AvailabilityZones[1].ZoneName' --output text)"

create_subnet() {
  local cidr="$1" name="$2" subnet_az="${3:-$AZ}"
  local existing
  existing="$(aws ec2 describe-subnets --region "$REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=tag:Name,Values=$name" \
    --query 'Subnets[0].SubnetId' --output text 2>/dev/null || true)"
  if [[ "$existing" != "None" && -n "$existing" ]]; then
    printf '%s' "$existing"; return
  fi
  aws ec2 create-subnet --region "$REGION" \
    --vpc-id "$VPC_ID" --cidr-block "$cidr" --availability-zone "$subnet_az" \
    --tag-specifications "$(tag_spec subnet "$name")" \
    --query 'Subnet.SubnetId' --output text
}

PUBLIC_SUBNET_ID="$(create_subnet 10.99.1.0/24 "${NAME}-public")"
PRIVATE_SUBNET_ID="$(create_subnet 10.99.2.0/24 "${NAME}-private")"
# Two private DB subnets in distinct AZs — the RDS DB subnet group needs >= 2 AZs.
# Dedicated to managed databases; the VM subnets above are left untouched.
DB_SUBNET_A_ID="$(create_subnet 10.99.3.0/24 "${NAME}-db-a" "$AZ")"
DB_SUBNET_B_ID="$(create_subnet 10.99.4.0/24 "${NAME}-db-b" "$AZ2")"
ok "Public subnet (Jumpoint) $PUBLIC_SUBNET_ID"
ok "Private subnet (VMs)    $PRIVATE_SUBNET_ID"
ok "DB subnet A ($AZ)        $DB_SUBNET_A_ID"
ok "DB subnet B ($AZ2)        $DB_SUBNET_B_ID"
state_write aws public_subnet_id  "$PUBLIC_SUBNET_ID"
state_write aws private_subnet_id "$PRIVATE_SUBNET_ID"
state_write aws db_subnet_a_id    "$DB_SUBNET_A_ID"
state_write aws db_subnet_b_id    "$DB_SUBNET_B_ID"

# ── 4. Route tables ───────────────────────────────────────────────────────────
section "Route tables"
make_rt() {
  local name="$1"
  local existing
  existing="$(aws ec2 describe-route-tables --region "$REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=tag:Name,Values=$name" \
    --query 'RouteTables[0].RouteTableId' --output text 2>/dev/null || true)"
  if [[ "$existing" != "None" && -n "$existing" ]]; then
    printf '%s' "$existing"; return
  fi
  aws ec2 create-route-table --region "$REGION" --vpc-id "$VPC_ID" \
    --tag-specifications "$(tag_spec route-table "$name")" \
    --query 'RouteTable.RouteTableId' --output text
}

PUBLIC_RT_ID="$(make_rt "${NAME}-public-rt")"
PRIVATE_RT_ID="$(make_rt "${NAME}-private-rt")"

# Public RT: 0.0.0.0/0 → IGW. Idempotent — create-route is a no-op if the
# route already exists.
aws ec2 create-route --region "$REGION" --route-table-id "$PUBLIC_RT_ID" \
  --destination-cidr-block 0.0.0.0/0 --gateway-id "$IGW_ID" >/dev/null 2>&1 || true

# Associate subnets to RTs (skip if already associated).
associate_rt() {
  local rt_id="$1" subnet_id="$2"
  local existing
  existing="$(aws ec2 describe-route-tables --region "$REGION" \
    --route-table-ids "$rt_id" \
    --query "RouteTables[0].Associations[?SubnetId=='$subnet_id'].RouteTableAssociationId" \
    --output text)"
  if [[ -z "$existing" || "$existing" == "None" ]]; then
    aws ec2 associate-route-table --region "$REGION" \
      --route-table-id "$rt_id" --subnet-id "$subnet_id" >/dev/null
  fi
}
associate_rt "$PUBLIC_RT_ID"  "$PUBLIC_SUBNET_ID"
associate_rt "$PRIVATE_RT_ID" "$PRIVATE_SUBNET_ID"
associate_rt "$PRIVATE_RT_ID" "$DB_SUBNET_A_ID"
associate_rt "$PRIVATE_RT_ID" "$DB_SUBNET_B_ID"
ok "Public  RT $PUBLIC_RT_ID  → IGW (0.0.0.0/0)"
ok "Private RT $PRIVATE_RT_ID → local VPC only (VMs + DBs)"

# ── 4b. RDS DB subnet group ───────────────────────────────────────────────────
# The managed-database feature deploys private RDS instances (no public
# endpoint) into this group; access is brokered only through the PRA tunnel.
section "RDS DB subnet group"
DB_SUBNET_GROUP_NAME="${NAME}-db"
DBG_EXISTS="$(aws rds describe-db-subnet-groups --region "$REGION" \
  --db-subnet-group-name "$DB_SUBNET_GROUP_NAME" \
  --query 'DBSubnetGroups[0].DBSubnetGroupName' --output text 2>/dev/null || true)"
if [[ -n "$DBG_EXISTS" && "$DBG_EXISTS" != "None" ]]; then
  ok "Reusing DB subnet group $DB_SUBNET_GROUP_NAME"
else
  aws rds create-db-subnet-group --region "$REGION" \
    --db-subnet-group-name "$DB_SUBNET_GROUP_NAME" \
    --db-subnet-group-description "Private subnet group for dashboard-managed databases ($NAME)" \
    --subnet-ids "$DB_SUBNET_A_ID" "$DB_SUBNET_B_ID" \
    --tags "Key=Name,Value=$DB_SUBNET_GROUP_NAME" "Key=$SANDBOX_TAG_KEY,Value=$SANDBOX_TAG_VALUE" >/dev/null
  ok "Created DB subnet group $DB_SUBNET_GROUP_NAME ($AZ, $AZ2)"
fi
state_write aws db_subnet_group_name "$DB_SUBNET_GROUP_NAME"

# ── 5. Security groups ────────────────────────────────────────────────────────
section "Security groups"
make_sg() {
  local name="$1" desc="$2"
  local existing
  existing="$(aws ec2 describe-security-groups --region "$REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=$name" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
  if [[ "$existing" != "None" && -n "$existing" ]]; then
    printf '%s' "$existing"; return
  fi
  aws ec2 create-security-group --region "$REGION" \
    --vpc-id "$VPC_ID" --group-name "$name" --description "$desc" \
    --tag-specifications "$(tag_spec security-group "$name")" \
    --query 'GroupId' --output text
}

JUMPOINT_SG="$(make_sg "${NAME}-jumpoint-sg" "Jumpoint ECS task - outbound to internet, ingress from VPC")"
VM_SG="$(make_sg "${NAME}-vm-sg" "Sandbox VMs - egress within VPC only, ingress SSH from Jumpoint SG")"

# Wipe default egress so we control rules explicitly. revoke is idempotent
# enough — it errors if absent, swallow.
aws ec2 revoke-security-group-egress --region "$REGION" --group-id "$VM_SG" \
  --ip-permissions '[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]' >/dev/null 2>&1 || true

# VM SG: egress to VPC only, ingress SSH from Jumpoint SG.
aws ec2 authorize-security-group-egress --region "$REGION" --group-id "$VM_SG" \
  --ip-permissions '[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"10.99.0.0/16"}]}]' >/dev/null 2>&1 || true
aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$VM_SG" \
  --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":22,\"ToPort\":22,\"UserIdGroupPairs\":[{\"GroupId\":\"$JUMPOINT_SG\"}]}]" >/dev/null 2>&1 || true

ok "Jumpoint SG $JUMPOINT_SG (default egress 0.0.0.0/0)"
ok "VM SG       $VM_SG (egress: VPC only; ingress 22/tcp from Jumpoint SG)"
state_write aws jumpoint_sg "$JUMPOINT_SG"
state_write aws vm_sg "$VM_SG"

# ── 6. SSH keypair JSON in Secrets Manager ────────────────────────────────────
section "SSH keypair (Secrets Manager)"
SSH_SECRET_NAME="dashboard/sandbox/ssh-keypair"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

if aws secretsmanager describe-secret --region "$REGION" --secret-id "$SSH_SECRET_NAME" >/dev/null 2>&1; then
  ok "Reusing existing secret $SSH_SECRET_NAME"
else
  ssh-keygen -t rsa -b 4096 -N "" -C "dashboard-sandbox" -f "$TMPDIR/key" >/dev/null
  PUB="$(cat "$TMPDIR/key.pub")"
  PRIV="$(cat "$TMPDIR/key")"
  jq -n --arg pub "$PUB" --arg priv "$PRIV" \
    '{public_key:$pub, private_key:$priv}' > "$TMPDIR/keypair.json"
  aws secretsmanager create-secret --region "$REGION" \
    --name "$SSH_SECRET_NAME" \
    --description "Dashboard sandbox SSH keypair (autogenerated)" \
    --secret-string "file://$TMPDIR/keypair.json" \
    --tags "Key=$SANDBOX_TAG_KEY,Value=$SANDBOX_TAG_VALUE" >/dev/null
  ok "Created secret $SSH_SECRET_NAME"
fi
state_write aws ssh_secret "$SSH_SECRET_NAME"

# ── 7. ECS cluster + task execution role ─────────────────────────────────────
section "ECS cluster + execution role"
ECS_CLUSTER="bt-jumpoint"
aws ecs create-cluster --region "$REGION" --cluster-name "$ECS_CLUSTER" \
  --tags "key=$SANDBOX_TAG_KEY,value=$SANDBOX_TAG_VALUE" >/dev/null 2>&1 || true
ok "ECS cluster: $ECS_CLUSTER"

ROLE_NAME="ecsTaskExecutionRole"
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  ok "Reusing IAM role $ROLE_NAME"
else
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]
    }' \
    --tags "Key=$SANDBOX_TAG_KEY,Value=$SANDBOX_TAG_VALUE" >/dev/null
  # A just-created IAM role can briefly 404 ("NoSuchEntity") on attach — retry
  # until it has propagated. Once this succeeds the role is consistent, so the
  # unconditional get-role below it needs no retry of its own.
  retry 8 5 aws iam attach-role-policy --role-name "$ROLE_NAME" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
  ok "Created IAM role $ROLE_NAME"
fi
ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text)"
state_write aws ecs_role_arn "$ROLE_ARN"

# ── 7b. Image-hub S3 bucket + promote-runner IAM ─────────────────────────────
# Provisions the prerequisites the dashboard's automated cross-cloud image
# promote runner needs (see docs/image-management.md, runners/promote/README.md):
#
#   • An S3 bucket that doubles as (a) the image-registry hub for the active
#     storage backend and (b) the staging bucket the promote-runner Fargate
#     task writes converted VHDs to (under promote-staging/).
#   • An ECS task role with s3:PutObject on that bucket — the runner
#     container's IAM principal during the upload step.
#   • The well-known `vmimport` IAM service role that AWS's ec2:ImportImage
#     assumes when reading the staged VHD to create the resulting AMI.
section "Image-hub S3 bucket + promote-runner IAM"

STORAGE_BUCKET="${NAME}-storage-${ACCOUNT_ID}"
if aws s3api head-bucket --bucket "$STORAGE_BUCKET" --region "$REGION" >/dev/null 2>&1; then
  ok "Reusing S3 bucket $STORAGE_BUCKET"
else
  # us-east-1 has its own create-bucket dance (no LocationConstraint allowed).
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$STORAGE_BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$STORAGE_BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  fi
  aws s3api put-bucket-tagging --bucket "$STORAGE_BUCKET" \
    --tagging "TagSet=[{Key=$SANDBOX_TAG_KEY,Value=$SANDBOX_TAG_VALUE}]" >/dev/null 2>&1 || true
  # Lock down public access — this bucket holds VHDs the dashboard
  # presigns short-lived URLs for; it should never serve anonymous reads.
  aws s3api put-public-access-block --bucket "$STORAGE_BUCKET" \
    --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true \
    >/dev/null 2>&1 || true
  ok "Created S3 bucket $STORAGE_BUCKET"
fi
state_write aws storage_bucket "$STORAGE_BUCKET"

PROMOTE_TASK_ROLE_NAME="${NAME}-promote-runner-task"
if aws iam get-role --role-name "$PROMOTE_TASK_ROLE_NAME" >/dev/null 2>&1; then
  ok "Reusing IAM role $PROMOTE_TASK_ROLE_NAME"
else
  aws iam create-role --role-name "$PROMOTE_TASK_ROLE_NAME" \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]
    }' \
    --tags "Key=$SANDBOX_TAG_KEY,Value=$SANDBOX_TAG_VALUE" >/dev/null
  ok "Created IAM role $PROMOTE_TASK_ROLE_NAME"
fi
# Retry the inline-policy put: on the fresh-create path above, the role may not
# have propagated yet ("NoSuchEntity"). Success here also gates the get-role.
retry 8 5 aws iam put-role-policy --role-name "$PROMOTE_TASK_ROLE_NAME" \
  --policy-name "promote-runner-s3-write" \
  --policy-document "$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:AbortMultipartUpload",
        "s3:ListBucketMultipartUploads",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": "arn:aws:s3:::${STORAGE_BUCKET}/promote-staging/*"
    }
  ]
}
JSON
)"
PROMOTE_TASK_ROLE_ARN="$(aws iam get-role --role-name "$PROMOTE_TASK_ROLE_NAME" --query 'Role.Arn' --output text)"
ok "Granted promote-runner task role S3 write on s3://$STORAGE_BUCKET/promote-staging/*"
state_write aws promote_task_role_arn "$PROMOTE_TASK_ROLE_ARN"

# vmimport service role — well-known name AWS expects unless overridden via
# aws_vmimport_role_name in the dashboard config. ec2:ImportImage assumes
# this role server-side to read the staged VHD + write the resulting AMI.
VMIMPORT_ROLE_NAME="vmimport"
if aws iam get-role --role-name "$VMIMPORT_ROLE_NAME" >/dev/null 2>&1; then
  ok "Reusing IAM role $VMIMPORT_ROLE_NAME"
else
  aws iam create-role --role-name "$VMIMPORT_ROLE_NAME" \
    --assume-role-policy-document "$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Service": "vmie.amazonaws.com"},
      "Action": "sts:AssumeRole",
      "Condition": {"StringEquals": {"sts:ExternalId": "vmimport"}}
    }
  ]
}
JSON
)" \
    --tags "Key=$SANDBOX_TAG_KEY,Value=$SANDBOX_TAG_VALUE" >/dev/null
  ok "Created IAM role $VMIMPORT_ROLE_NAME"
fi
retry 8 5 aws iam put-role-policy --role-name "$VMIMPORT_ROLE_NAME" \
  --policy-name "vmimport-s3-and-ec2" \
  --policy-document "$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:GetObjectAcl",
        "s3:GetBucketLocation",
        "s3:GetBucketAcl",
        "s3:ListBucket",
        "s3:PutObject",
        "s3:AbortMultipartUpload",
        "s3:ListBucketMultipartUploads",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": [
        "arn:aws:s3:::${STORAGE_BUCKET}",
        "arn:aws:s3:::${STORAGE_BUCKET}/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "ec2:ModifySnapshotAttribute",
        "ec2:CopySnapshot",
        "ec2:RegisterImage",
        "ec2:Describe*"
      ],
      "Resource": "*"
    }
  ]
}
JSON
)"
ok "Granted $VMIMPORT_ROLE_NAME read+write on s3://$STORAGE_BUCKET/* + ec2 image-import perms"

# ── 7c. Dashboard IAM user — programmatic creds for the app ───────────────────
# The dashboard process needs an AWS access key to call EC2 / ECS / S3 /
# Secrets Manager. Operators historically had to bring their own user; the
# net effect was per-feature permission errors months after the sandbox came
# up (e.g. ec2:ExportImage failing because the operator's user lacked it).
# Create a sandbox-tagged user with a single inline policy covering every
# code path in web_dashboard/services/aws_service.py. Re-runs reuse the
# cached secret from $HOME/.dashboard-sandbox/aws/ rather than rotating
# (AWS allows at most 2 access keys per user; rotation churn is unhelpful
# for a developer sandbox).
section "Dashboard IAM user"
DASHBOARD_USER_NAME="${SANDBOX_NAME_PREFIX}-app"
DASHBOARD_POLICY_NAME="dashboard-app-policy"

if aws iam get-user --user-name "$DASHBOARD_USER_NAME" >/dev/null 2>&1; then
  ok "Reusing IAM user $DASHBOARD_USER_NAME"
  DASHBOARD_USER_EXISTED=1
else
  aws iam create-user --user-name "$DASHBOARD_USER_NAME" \
    --tags "Key=$SANDBOX_TAG_KEY,Value=$SANDBOX_TAG_VALUE" >/dev/null
  ok "Created IAM user $DASHBOARD_USER_NAME"
  DASHBOARD_USER_EXISTED=0
fi

# Inline user policies are capped at 2048 bytes; this policy is well over,
# so it's a customer-managed policy (6144-byte quota + versioning). On
# re-runs we create-policy-version --set-as-default so edits propagate
# without rotating the access key.
DASHBOARD_POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${DASHBOARD_POLICY_NAME}"
DASHBOARD_POLICY_DOC="$(jq -c . <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DashboardEC2Manage",
      "Effect": "Allow",
      "Action": [
        "ec2:Describe*",
        "ec2:RunInstances",
        "ec2:StartInstances",
        "ec2:StopInstances",
        "ec2:TerminateInstances",
        "ec2:RebootInstances",
        "ec2:ModifyInstanceAttribute",
        "ec2:CreateTags",
        "ec2:DeleteTags",
        "ec2:CopyImage",
        "ec2:CreateImage",
        "ec2:RegisterImage",
        "ec2:DeregisterImage",
        "ec2:DeleteSnapshot",
        "ec2:ModifySnapshotAttribute",
        "ec2:CopySnapshot",
        "ec2:CreateSecurityGroup",
        "ec2:DeleteSecurityGroup",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:AuthorizeSecurityGroupEgress",
        "ec2:RevokeSecurityGroupIngress",
        "ec2:RevokeSecurityGroupEgress",
        "ec2:CreateKeyPair",
        "ec2:DeleteKeyPair",
        "ec2:ImportKeyPair",
        "ec2:GetPasswordData"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DashboardVMImportExport",
      "Effect": "Allow",
      "Action": [
        "ec2:ExportImage",
        "ec2:DescribeExportImageTasks",
        "ec2:CancelExportTask",
        "ec2:ImportImage",
        "ec2:DescribeImportImageTasks",
        "ec2:CancelImportTask"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DashboardRDS",
      "Effect": "Allow",
      "Action": [
        "rds:CreateDBInstance",
        "rds:DeleteDBInstance",
        "rds:ModifyDBInstance",
        "rds:RebootDBInstance",
        "rds:DescribeDBInstances",
        "rds:CreateDBSubnetGroup",
        "rds:DeleteDBSubnetGroup",
        "rds:ModifyDBSubnetGroup",
        "rds:DescribeDBSubnetGroups",
        "rds:DescribeDBEngineVersions",
        "rds:DescribeOrderableDBInstanceOptions",
        "rds:AddTagsToResource",
        "rds:RemoveTagsFromResource",
        "rds:ListTagsForResource"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DashboardPassRoles",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": [
        "arn:aws:iam::${ACCOUNT_ID}:role/${VMIMPORT_ROLE_NAME}",
        "arn:aws:iam::${ACCOUNT_ID}:role/ecsTaskExecutionRole",
        "arn:aws:iam::${ACCOUNT_ID}:role/${PROMOTE_TASK_ROLE_NAME}"
      ]
    },
    {
      "Sid": "DashboardECS",
      "Effect": "Allow",
      "Action": [
        "ecs:CreateCluster",
        "ecs:DescribeClusters",
        "ecs:ListClusters",
        "ecs:RegisterTaskDefinition",
        "ecs:DeregisterTaskDefinition",
        "ecs:DescribeTaskDefinition",
        "ecs:ListTaskDefinitions",
        "ecs:RunTask",
        "ecs:StopTask",
        "ecs:DescribeTasks",
        "ecs:ListTasks"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DashboardServiceLinkedRole",
      "Effect": "Allow",
      "Action": "iam:CreateServiceLinkedRole",
      "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/aws-service-role/ecs.amazonaws.com/AWSServiceRoleForECS*",
      "Condition": {
        "StringLike": {"iam:AWSServiceName": "ecs.amazonaws.com"}
      }
    },
    {
      "Sid": "DashboardSecretsManager",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:ListSecrets",
        "secretsmanager:DescribeSecret",
        "secretsmanager:CreateSecret",
        "secretsmanager:PutSecretValue",
        "secretsmanager:UpdateSecret",
        "secretsmanager:DeleteSecret",
        "secretsmanager:TagResource"
      ],
      "Resource": "arn:aws:secretsmanager:*:${ACCOUNT_ID}:secret:dashboard/*"
    },
    {
      "Sid": "DashboardSecretsManagerListAll",
      "Effect": "Allow",
      "Action": "secretsmanager:ListSecrets",
      "Resource": "*"
    },
    {
      "Sid": "DashboardS3Storage",
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:GetObject",
        "s3:GetObjectAcl",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
        "s3:ListBucketMultipartUploads",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": [
        "arn:aws:s3:::${SANDBOX_NAME_PREFIX}-storage-*",
        "arn:aws:s3:::${SANDBOX_NAME_PREFIX}-storage-*/*"
      ]
    },
    {
      "Sid": "DashboardLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams",
        "logs:GetLogEvents",
        "logs:PutRetentionPolicy"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DashboardSTS",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    }
  ]
}
JSON
)"

if aws iam get-policy --policy-arn "$DASHBOARD_POLICY_ARN" >/dev/null 2>&1; then
  # Policy exists — push a new version. Managed policies cap at 5 versions
  # total; if we're already at 5, prune the oldest non-default first.
  VERSION_COUNT="$(aws iam list-policy-versions --policy-arn "$DASHBOARD_POLICY_ARN" \
    --query 'length(Versions)' --output text)"
  if [[ "$VERSION_COUNT" -ge 5 ]]; then
    OLDEST_VID="$(aws iam list-policy-versions --policy-arn "$DASHBOARD_POLICY_ARN" \
      --query 'Versions[?!IsDefaultVersion]|[-1].VersionId' --output text)"
    aws iam delete-policy-version --policy-arn "$DASHBOARD_POLICY_ARN" \
      --version-id "$OLDEST_VID" >/dev/null
  fi
  aws iam create-policy-version --policy-arn "$DASHBOARD_POLICY_ARN" \
    --policy-document "$DASHBOARD_POLICY_DOC" --set-as-default >/dev/null
  ok "Updated managed policy $DASHBOARD_POLICY_NAME (new default version)"
else
  aws iam create-policy --policy-name "$DASHBOARD_POLICY_NAME" \
    --policy-document "$DASHBOARD_POLICY_DOC" \
    --tags "Key=$SANDBOX_TAG_KEY,Value=$SANDBOX_TAG_VALUE" >/dev/null
  ok "Created managed policy $DASHBOARD_POLICY_NAME"
fi

# References both a possibly-just-created user and a possibly-just-created
# managed policy ARN — either can lag. Retry until both have propagated; this
# also gates the create-access-key below (once attach succeeds the user is
# consistent), so that non-idempotent create needs no retry of its own.
retry 8 5 aws iam attach-user-policy --user-name "$DASHBOARD_USER_NAME" \
  --policy-arn "$DASHBOARD_POLICY_ARN"
ok "Attached $DASHBOARD_POLICY_NAME to $DASHBOARD_USER_NAME"

# Access-key management. Cache the secret in the same ~/.dashboard-sandbox
# state dir the rest of the script uses so re-runs can re-print it.
CACHED_ACCESS_KEY_ID="$(state_read aws access_key_id)"
CACHED_SECRET_ACCESS_KEY="$(state_read aws secret_access_key)"
EXISTING_KEYS_JSON="$(aws iam list-access-keys --user-name "$DASHBOARD_USER_NAME" --output json 2>/dev/null || echo '{"AccessKeyMetadata":[]}')"
EXISTING_KEY_COUNT="$(echo "$EXISTING_KEYS_JSON" | jq '.AccessKeyMetadata | length')"

if [[ -n "$CACHED_ACCESS_KEY_ID" && -n "$CACHED_SECRET_ACCESS_KEY" ]] && \
   echo "$EXISTING_KEYS_JSON" | jq -e --arg id "$CACHED_ACCESS_KEY_ID" \
     '.AccessKeyMetadata[] | select(.AccessKeyId==$id)' >/dev/null; then
  AWS_ACCESS_KEY_ID="$CACHED_ACCESS_KEY_ID"
  AWS_SECRET_ACCESS_KEY="$CACHED_SECRET_ACCESS_KEY"
  ok "Reusing cached access key $AWS_ACCESS_KEY_ID (from $HOME/.dashboard-sandbox/aws/)"
elif [[ "$EXISTING_KEY_COUNT" -eq 0 ]]; then
  KEY_JSON="$(aws iam create-access-key --user-name "$DASHBOARD_USER_NAME" --output json)"
  AWS_ACCESS_KEY_ID="$(echo "$KEY_JSON" | jq -r '.AccessKey.AccessKeyId')"
  AWS_SECRET_ACCESS_KEY="$(echo "$KEY_JSON" | jq -r '.AccessKey.SecretAccessKey')"
  state_write aws access_key_id "$AWS_ACCESS_KEY_ID"
  state_write aws secret_access_key "$AWS_SECRET_ACCESS_KEY"
  chmod 0600 "$(state_dir aws)/access_key_id" "$(state_dir aws)/secret_access_key" 2>/dev/null || true
  chmod 0700 "$(state_dir aws)" 2>/dev/null || true
  ok "Created access key $AWS_ACCESS_KEY_ID (secret cached at $(state_dir aws)/secret_access_key, mode 0600)"
else
  warn "IAM user $DASHBOARD_USER_NAME has $EXISTING_KEY_COUNT existing access key(s) but no cached secret on this host."
  warn "AWS does not let us re-read the secret of an existing key. Two ways forward:"
  warn "  1. Recover: aws iam list-access-keys --user-name $DASHBOARD_USER_NAME ; aws iam delete-access-key --user-name $DASHBOARD_USER_NAME --access-key-id <id> ; then re-run this script to mint a fresh key."
  warn "  2. Clean restart: ./scripts/sandbox/Linux/rollback.sh --cloud aws ; then re-run this script."
  AWS_ACCESS_KEY_ID="<existing-key-from-AWS-Console>"
  AWS_SECRET_ACCESS_KEY="<rotate-or-rollback-see-warning-above>"
fi

# ── 8. Print config to paste into /setup ──────────────────────────────────────
_cfg=(
  "aws_region=$REGION"
  "aws_default_subnet_id=$PRIVATE_SUBNET_ID            # Deploy form's default subnet for new EC2 instances"
  "aws_default_security_group_id=$VM_SG               # Deploy form's default SG (VM-tier, no internet egress)"
  "aws_db_subnet_group_name=$DB_SUBNET_GROUP_NAME      # Managed-DB deploys: private RDS subnet group (2 AZs)"
  "aws_db_security_group_id=$VM_SG                     # Managed-DB deploys: reuse the VM-tier SG (no internet egress)"
  "ec2_ssh_key_secret=$SSH_SECRET_NAME                 # JSON {public_key,private_key} for EC2 cloud-init + Ansible"
  "bt_ecs_cluster=$ECS_CLUSTER                          # ECS cluster the Jumpoint Fargate task runs in"
  "bt_ecs_task_family=bt-jumpoint"
  "bt_ecs_image=beyondtrust/sra-jumpoint                # or your ECR mirror"
  "bt_ecs_execution_role_arn=$ROLE_ARN"
  "bt_ecs_jumpoint_subnet_id=$PUBLIC_SUBNET_ID         # Jumpoint task lands here (public, IGW-routed)"
  "bt_ecs_jumpoint_security_group_id=$JUMPOINT_SG      # SG for the Jumpoint task"
  ""
  "# Image-registry hub + automated cross-cloud promote:"
  "storage_s3_bucket=$STORAGE_BUCKET                                       # Image hub + promote staging"
  "storage_active_backend=s3                                                  # Active asset backend"
  "storage_hub_backend=s3                                                     # Image hub (defaults to active if unset)"
  "promote_runner_image=chrweav/dashboard-promote-runner:latest         # Public multi-arch image; override to your ECR for a private/air-gapped registry"
  "promote_runner_ecs_cluster=$ECS_CLUSTER                                    # Reuses Jumpoint cluster"
  "promote_runner_ecs_execution_role_arn=$ROLE_ARN                            # Image pull + CloudWatch logs"
  "promote_runner_ecs_task_role_arn=$PROMOTE_TASK_ROLE_ARN                    # S3 PutObject on the staging bucket"
  "promote_runner_ecs_subnet_id=$PUBLIC_SUBNET_ID                             # Runner needs egress to the presigned source URL"
  "promote_runner_ecs_security_group_ids=$JUMPOINT_SG                         # Reuses Jumpoint SG (egress 443)"
  "aws_vmimport_role_name=$VMIMPORT_ROLE_NAME                                 # Service role ec2:ImportImage assumes"
  ""
  "# Sandbox-provisioned AWS credentials for the dashboard IAM user ($DASHBOARD_USER_NAME):"
  "aws_access_key_id=$AWS_ACCESS_KEY_ID"
  "aws_secret_access_key=$AWS_SECRET_ACCESS_KEY"
  "aws_ecs_docker_deploy_key=…   # BeyondTrust SRA Jumpoint deploy key (paste manually)"
)
print_dashboard_config "AWS sandbox configuration" "${_cfg[@]}"
write_config_json aws "${_cfg[@]}"   # machine-readable twin for onboard-sandbox.sh

cat <<EOF
Sandbox topology summary

  VPC ${VPC_ID} (10.99.0.0/16)
    ├─ public  ${PUBLIC_SUBNET_ID}  (10.99.1.0/24) → IGW → internet  [Jumpoint ECS]
    └─ private ${PRIVATE_SUBNET_ID}  (10.99.2.0/24) → no internet     [user EC2s]

To tear it down:
  ./scripts/sandbox/Linux/rollback.sh --cloud aws

EOF
