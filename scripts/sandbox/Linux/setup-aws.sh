#!/usr/bin/env bash
# AWS sandbox bootstrap for the VM Dashboard.
#
# Creates a small, isolated VPC where:
#   • A public subnet hosts the BeyondTrust Jumpoint host (ECS-on-EC2, launched
#     on demand by the dashboard) and the ECS Fargate ansible/promote runners —
#     they reach the internet via an Internet Gateway (the host/runners get a
#     public IP) so the Jumpoint can phone home to PRA's relay.
#   • A private subnet hosts deployed EC2 instances — no IGW route. Their
#     outbound internet is provided by a shared, on-demand NAT instance the
#     dashboard creates on the first EC2 deploy and removes with the last VM
#     (so there is no standing NAT gateway or Elastic IP).
#
# Also creates an SSH keypair JSON in Secrets Manager and IAM roles for the
# ECS task execution + on-demand Jumpoint host. Prints a config block to paste
# into the dashboard's /setup wizard.

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
# Managed Kubernetes (EKS) no longer needs sandbox subnets — the
# terraform/k8s_cluster/aws_eks module builds its OWN VPC + subnets + NAT-instance
# egress per cluster and peers back to this VPC (see the aws_vpc_id /
# aws_private_route_table_id config emitted at the end).
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

# Move a subnet onto a route table, REPLACING any existing association (a subnet
# can have only one). associate_rt above can't do this — associate-route-table
# errors Resource.AlreadyAssociated when the subnet already belongs to another RT
# (e.g. an older sandbox put the k8s subnets on the private RT before egress
# existed). Idempotent: no-op when the subnet is already on the target RT.
move_subnet_to_rt() {
  local rt_id="$1" subnet_id="$2"
  local cur_rt assoc
  cur_rt="$(aws ec2 describe-route-tables --region "$REGION" \
    --filters "Name=association.subnet-id,Values=$subnet_id" \
    --query 'RouteTables[0].RouteTableId' --output text 2>/dev/null || true)"
  if [[ -z "$cur_rt" || "$cur_rt" == "None" ]]; then
    aws ec2 associate-route-table --region "$REGION" \
      --route-table-id "$rt_id" --subnet-id "$subnet_id" >/dev/null
  elif [[ "$cur_rt" != "$rt_id" ]]; then
    assoc="$(aws ec2 describe-route-tables --region "$REGION" \
      --filters "Name=association.subnet-id,Values=$subnet_id" \
      --query "RouteTables[0].Associations[?SubnetId=='$subnet_id'].RouteTableAssociationId | [0]" \
      --output text)"
    aws ec2 replace-route-table-association --region "$REGION" \
      --association-id "$assoc" --route-table-id "$rt_id" >/dev/null
  fi
}
associate_rt "$PUBLIC_RT_ID"  "$PUBLIC_SUBNET_ID"
associate_rt "$PRIVATE_RT_ID" "$PRIVATE_SUBNET_ID"
associate_rt "$PRIVATE_RT_ID" "$DB_SUBNET_A_ID"
associate_rt "$PRIVATE_RT_ID" "$DB_SUBNET_B_ID"
ok "Public  RT $PUBLIC_RT_ID  → IGW (0.0.0.0/0)"
ok "Private RT $PRIVATE_RT_ID → local VPC only (VMs + DBs)"

# ── 4a. K8s node egress — now owned by the EKS Terraform build ────────────────
# The sandbox no longer stands up a NAT for k8s (no standing NAT cost). Managed
# Kubernetes (EKS) is self-contained like AKS/GKE: the aws_eks module builds its
# OWN VPC + public/private subnets + NAT instance per cluster (torn down with the
# cluster) and VPC-peers back to this sandbox VPC for direct management-plane
# access. The dashboard passes the peering inputs emitted at the end
# (aws_vpc_id / aws_vpc_cidr / aws_private_route_table_id).
state_write aws private_rt_id "$PRIVATE_RT_ID"

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

# ── 4c. RDS parameter group with rds.force_ssl=0 ──────────────────────────────
# Dashboard-managed DBs are reached ONLY through a BeyondTrust PRA *protocol*
# tunnel, which proxies the cleartext PostgreSQL wire protocol (for credential
# injection + session recording) — it has no backend-TLS option. RDS PG15+
# defaults to rds.force_ssl=1, which rejects the tunnel's plaintext
# jumpoint→RDS connection. We pre-create a group with force_ssl off here (the
# scoped dashboard IAM user can't create parameter groups) and hand its name to
# the dashboard via aws_db_parameter_group_name; the db_postgres module
# references it. Safe posture: the instance is private-only, only the jumpoint
# SG can reach it, and the client→jumpoint hop is PRA-encrypted.
section "RDS parameter group (force_ssl off, for the PRA tunnel)"
DB_PARAM_GROUP_NAME="clouddb-nossl-pg16"
if aws rds describe-db-parameter-groups --region "$REGION" \
     --db-parameter-group-name "$DB_PARAM_GROUP_NAME" >/dev/null 2>&1; then
  ok "Reusing DB parameter group $DB_PARAM_GROUP_NAME"
else
  aws rds create-db-parameter-group --region "$REGION" \
    --db-parameter-group-name "$DB_PARAM_GROUP_NAME" \
    --db-parameter-group-family postgres16 \
    --description "Dashboard managed DBs: force_ssl off (reached via PRA protocol tunnel)" \
    --tags "Key=Name,Value=$DB_PARAM_GROUP_NAME" "Key=$SANDBOX_TAG_KEY,Value=$SANDBOX_TAG_VALUE" >/dev/null
  ok "Created DB parameter group $DB_PARAM_GROUP_NAME"
fi
# force_ssl is a dynamic parameter — applies without a reboot.
aws rds modify-db-parameter-group --region "$REGION" \
  --db-parameter-group-name "$DB_PARAM_GROUP_NAME" \
  --parameters "ParameterName=rds.force_ssl,ParameterValue=0,ApplyMethod=immediate" >/dev/null
ok "Set rds.force_ssl=0 on $DB_PARAM_GROUP_NAME"
state_write aws db_parameter_group_name "$DB_PARAM_GROUP_NAME"

# ── 4d. RDS MySQL parameter group with require_secure_transport=0 ─────────────
# MySQL's cleartext knob for the PRA tunnel — the analog of rds.force_ssl=0,
# which doesn't exist for MySQL. A mysql8.4-family group with
# require_secure_transport=0 lets the tunnel's plaintext jumpoint→RDS connection
# through. The db_mysql module references it via aws_db_mysql_parameter_group_name.
# Engine is MySQL 8.4 so the master user defaults to caching_sha2_password, which
# the BeyondTrust PRA MySQL tunnel requires — 8.0's mysql_native_password is
# rejected and RDS won't let default_authentication_plugin be changed on 8.0.
section "RDS MySQL parameter group (require_secure_transport off, for the PRA tunnel)"
DB_MYSQL_PARAM_GROUP_NAME="clouddb-nossl-mysql84"
if aws rds describe-db-parameter-groups --region "$REGION" \
     --db-parameter-group-name "$DB_MYSQL_PARAM_GROUP_NAME" >/dev/null 2>&1; then
  ok "Reusing DB parameter group $DB_MYSQL_PARAM_GROUP_NAME"
else
  aws rds create-db-parameter-group --region "$REGION" \
    --db-parameter-group-name "$DB_MYSQL_PARAM_GROUP_NAME" \
    --db-parameter-group-family mysql8.4 \
    --description "Dashboard managed DBs: require_secure_transport off (reached via PRA protocol tunnel)" \
    --tags "Key=Name,Value=$DB_MYSQL_PARAM_GROUP_NAME" "Key=$SANDBOX_TAG_KEY,Value=$SANDBOX_TAG_VALUE" >/dev/null
  ok "Created DB parameter group $DB_MYSQL_PARAM_GROUP_NAME"
fi
# require_secure_transport is a dynamic parameter — applies without a reboot.
aws rds modify-db-parameter-group --region "$REGION" \
  --db-parameter-group-name "$DB_MYSQL_PARAM_GROUP_NAME" \
  --parameters "ParameterName=require_secure_transport,ParameterValue=0,ApplyMethod=immediate" >/dev/null
ok "Set require_secure_transport=0 on $DB_MYSQL_PARAM_GROUP_NAME"
state_write aws db_mysql_parameter_group_name "$DB_MYSQL_PARAM_GROUP_NAME"

# RDS also needs its service-linked role before the FIRST CreateDBInstance in
# an account. RDS normally auto-creates it, but that requires
# iam:CreateServiceLinkedRole — which the scoped dashboard user (7c) doesn't
# get. Pre-create it here with the operator's privileged login; otherwise the
# first provision fails with "Verify that you have permission to create
# service linked role".
SLR_EXISTS="$(aws iam get-role --role-name AWSServiceRoleForRDS \
  --query 'Role.RoleName' --output text 2>/dev/null || true)"
if [[ -n "$SLR_EXISTS" && "$SLR_EXISTS" != "None" ]]; then
  ok "Reusing RDS service-linked role AWSServiceRoleForRDS"
else
  aws iam create-service-linked-role --aws-service-name rds.amazonaws.com >/dev/null
  ok "Created RDS service-linked role AWSServiceRoleForRDS"
fi

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
# Also allow outbound HTTP 80 / HTTPS 443 / DNS 53 to the internet so VMs routed
# through the on-demand NAT instance can reach package mirrors + repos. Harmless
# before a NAT exists — the private route table blackholes 0.0.0.0/0 until then.
aws ec2 authorize-security-group-egress --region "$REGION" --group-id "$VM_SG" \
  --ip-permissions '[{"IpProtocol":"tcp","FromPort":80,"ToPort":80,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]},{"IpProtocol":"tcp","FromPort":443,"ToPort":443,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]},{"IpProtocol":"tcp","FromPort":53,"ToPort":53,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]},{"IpProtocol":"udp","FromPort":53,"ToPort":53,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]' >/dev/null 2>&1 || true
aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$VM_SG" \
  --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":22,\"ToPort\":22,\"UserIdGroupPairs\":[{\"GroupId\":\"$JUMPOINT_SG\"}]}]" >/dev/null 2>&1 || true

# DB SG: attached to dashboard-managed RDS instances (the /databases feature).
# The PRA protocol tunnel terminates on the Jumpoint, which then dials the
# private DB endpoint — so ingress is the three engine ports from the Jumpoint
# SG only. Default egress wiped: RDS initiates no outbound connections.
DB_SG="$(make_sg "${NAME}-db-sg" "Managed databases - ingress DB ports from Jumpoint SG only, no egress")"
aws ec2 revoke-security-group-egress --region "$REGION" --group-id "$DB_SG" \
  --ip-permissions '[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]' >/dev/null 2>&1 || true
for _db_port in 5432 3306 1433; do   # postgres (live), mysql / sqlserver (Phase 3)
  aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$DB_SG" \
    --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":$_db_port,\"ToPort\":$_db_port,\"UserIdGroupPairs\":[{\"GroupId\":\"$JUMPOINT_SG\"}]}]" >/dev/null 2>&1 || true
done

# NAT SG: attached to the on-demand shared NAT instance the dashboard creates on
# the first EC2 deploy. Ingress all from the VPC (forwarded traffic from private
# VMs), egress all to the internet (default rule left in place). Pre-created here
# (SGs are free) so teardown never races an SG delete against the terminating
# NAT's ENI.
NAT_SG="$(make_sg "${NAME}-nat-sg" "On-demand NAT instance - ingress from VPC, egress all")"
aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$NAT_SG" \
  --ip-permissions '[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"10.99.0.0/16"}]}]' >/dev/null 2>&1 || true

ok "Jumpoint SG $JUMPOINT_SG (default egress 0.0.0.0/0)"
ok "VM SG       $VM_SG (egress: VPC + internet 80/443/53; ingress 22/tcp from Jumpoint SG)"
ok "DB SG       $DB_SG (ingress 5432/3306/1433 from Jumpoint SG; no egress)"
ok "NAT SG      $NAT_SG (ingress all from VPC; egress all — for the on-demand NAT instance)"
state_write aws jumpoint_sg "$JUMPOINT_SG"
state_write aws vm_sg "$VM_SG"
state_write aws db_sg "$DB_SG"
state_write aws nat_sg "$NAT_SG"

# ── 5b. SSM interface VPC endpoints (private SSM path for onboarded VMs) ───────
# The AWS Systems Manager Password Safe onboarding manages Linux EC2 over SSM
# SendCommand. The VM SG egresses to the VPC only (internet was revoked above),
# so an onboarded VM reaches the SSM control plane ONLY through interface
# endpoints. Create the three the agent needs (ssm, ssmmessages, ec2messages)
# with private DNS, so the public SSM hostnames resolve to them VPC-wide — no NAT
# or public IP required. Small hourly cost per endpoint while the sandbox is up;
# set SANDBOX_SSM_ENDPOINTS=0 to skip (e.g. if you only onboard VMs over SSH).
if [[ "${SANDBOX_SSM_ENDPOINTS:-1}" != "0" ]]; then
  section "SSM VPC endpoints"
  SSM_VPCE_SG="$(make_sg "${NAME}-ssm-vpce-sg" "SSM interface endpoints - HTTPS ingress from the VPC")"
  aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SSM_VPCE_SG" \
    --ip-permissions '[{"IpProtocol":"tcp","FromPort":443,"ToPort":443,"IpRanges":[{"CidrIp":"10.99.0.0/16"}]}]' >/dev/null 2>&1 || true
  for _svc in ssm ssmmessages ec2messages; do
    _svc_name="com.amazonaws.${REGION}.${_svc}"
    _existing="$(aws ec2 describe-vpc-endpoints --region "$REGION" \
      --filters "Name=vpc-id,Values=$VPC_ID" "Name=service-name,Values=$_svc_name" \
      --query 'VpcEndpoints[0].VpcEndpointId' --output text 2>/dev/null || true)"
    if [[ "$_existing" != "None" && -n "$_existing" ]]; then
      ok "Reusing $_svc endpoint $_existing"
      continue
    fi
    _epid="$(aws ec2 create-vpc-endpoint --region "$REGION" \
      --vpc-id "$VPC_ID" --vpc-endpoint-type Interface --service-name "$_svc_name" \
      --subnet-ids "$PRIVATE_SUBNET_ID" --security-group-ids "$SSM_VPCE_SG" \
      --private-dns-enabled \
      --tag-specifications "$(tag_spec vpc-endpoint "${NAME}-${_svc}")" \
      --query 'VpcEndpoint.VpcEndpointId' --output text)"
    ok "Created $_svc endpoint $_epid"
  done
  state_write aws ssm_vpce_sg "$SSM_VPCE_SG"
else
  ok "SSM VPC endpoints skipped (SANDBOX_SSM_ENDPOINTS=0)"
fi

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

# ── 7a. ECS instance role for the on-demand Jumpoint host ──────────────────────
# The tunnel-capable Jumpoint runs on an EC2 ECS container instance (Fargate
# forbids the NET_ADMIN/NET_RAW/IPC_LOCK caps + /dev/net/tun it needs). The
# DASHBOARD creates and terminates that host on demand (when an EC2 instance or
# database is provisioned / the last one is removed), so we DON'T stand up an
# instance here — we only pre-create the role + instance profile the dashboard
# attaches to it (the dashboard's scoped user can't create IAM roles itself).
section "ECS instance role for the Jumpoint host"

ECS_INSTANCE_ROLE="ecsInstanceRole"
if aws iam get-role --role-name "$ECS_INSTANCE_ROLE" >/dev/null 2>&1; then
  ok "Reusing IAM role $ECS_INSTANCE_ROLE"
else
  aws iam create-role --role-name "$ECS_INSTANCE_ROLE" \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]
    }' \
    --tags "Key=$SANDBOX_TAG_KEY,Value=$SANDBOX_TAG_VALUE" >/dev/null
  retry 8 5 aws iam attach-role-policy --role-name "$ECS_INSTANCE_ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role
  ok "Created IAM role $ECS_INSTANCE_ROLE"
fi
# The Jumpoint host doubles as the SSM Run Command target for the optional
# cloud-DB Password Safe onboarding (the dashboard runs the DB client on it via
# SSM to create the managed DB user). Attach the SSM managed-instance core policy
# so the host's SSM agent registers it as a managed instance (idempotent).
retry 8 5 aws iam attach-role-policy --role-name "$ECS_INSTANCE_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore || \
  warn "could not attach AmazonSSMManagedInstanceCore to $ECS_INSTANCE_ROLE (cloud-DB PS onboarding needs it)"
# Instance profile wraps the role for EC2 attachment (idempotent).
if ! aws iam get-instance-profile --instance-profile-name "$ECS_INSTANCE_ROLE" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$ECS_INSTANCE_ROLE" \
    --tags "Key=$SANDBOX_TAG_KEY,Value=$SANDBOX_TAG_VALUE" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$ECS_INSTANCE_ROLE" \
    --role-name "$ECS_INSTANCE_ROLE" >/dev/null
  ok "Created instance profile $ECS_INSTANCE_ROLE"
else
  ok "Reusing instance profile $ECS_INSTANCE_ROLE"
fi
ECS_INSTANCE_ROLE_ARN="$(aws iam get-role --role-name "$ECS_INSTANCE_ROLE" --query 'Role.Arn' --output text)"

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
        "ec2:*LaunchTemplate*",
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
      "Sid": "DashboardVPCManage",
      "Effect": "Allow",
      "Action": [
        "ec2:*Vpc*",
        "ec2:*Subnet*",
        "ec2:*RouteTable*",
        "ec2:CreateRoute",
        "ec2:DeleteRoute",
        "ec2:ReplaceRoute",
        "ec2:*Address*",
        "ec2:*InternetGateway*"
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
        "arn:aws:iam::${ACCOUNT_ID}:role/ecsInstanceRole",
        "arn:aws:iam::${ACCOUNT_ID}:role/${PROMOTE_TASK_ROLE_NAME}",
        "arn:aws:iam::${ACCOUNT_ID}:role/k8s-*",
        "arn:aws:iam::${ACCOUNT_ID}:role/ec2-ssm-*"
      ]
    },
    {
      "Sid": "DashboardECSOptimizedAMI",
      "Effect": "Allow",
      "Action": ["ssm:GetParameter", "ssm:GetParameters"],
      "Resource": "arn:aws:ssm:*::parameter/aws/service/ecs/optimized-ami/*"
    },
    {
      "Sid": "DashboardSSMRunCommand",
      "Effect": "Allow",
      "Action": ["ssm:SendCommand", "ssm:GetCommandInvocation", "ssm:ListCommandInvocations"],
      "Resource": "*"
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
        "ecs:ListTasks",
        "ecs:ListContainerInstances",
        "ecs:DescribeContainerInstances"
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
    },
    {
      "Sid": "DashboardEKS",
      "Effect": "Allow",
      "Action": "eks:*",
      "Resource": "*"
    },
    {
      "Sid": "DashboardEKSRoles",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:ListAttachedRolePolicies",
        "iam:ListRolePolicies",
        "iam:ListInstanceProfilesForRole",
        "iam:TagRole",
        "iam:UntagRole"
      ],
      "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/k8s-*"
    },
    {
      "Sid": "DashboardEKSServiceLinkedRole",
      "Effect": "Allow",
      "Action": "iam:CreateServiceLinkedRole",
      "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/aws-service-role/eks*.amazonaws.com/*",
      "Condition": {
        "StringLike": {"iam:AWSServiceName": ["eks.amazonaws.com", "eks-nodegroup.amazonaws.com"]}
      }
    },
    {
      "Sid": "DashboardEKSGetRole",
      "Effect": "Allow",
      "Action": "iam:GetRole",
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
  "aws_db_parameter_group_name=$DB_PARAM_GROUP_NAME    # Managed-DB deploys: force_ssl=0 group (PRA protocol tunnel needs a cleartext backend)"
  "aws_db_mysql_parameter_group_name=$DB_MYSQL_PARAM_GROUP_NAME    # Managed-DB MySQL deploys: require_secure_transport=0 group (PRA protocol tunnel needs a cleartext backend)"
  "aws_db_security_group_id=$DB_SG                     # Managed-DB deploys: DB-tier SG (engine ports from Jumpoint SG only)"
  "aws_vpc_id=$VPC_ID                                  # Sandbox VPC the EKS module peers its own VPC back to"
  "aws_vpc_cidr=10.99.0.0/16                           # Sandbox VPC CIDR (EKS peering route target)"
  "aws_private_route_table_id=$PRIVATE_RT_ID           # Sandbox private RT — gets the EKS peering return route"
  "ansible_ecs_subnet_id=$PUBLIC_SUBNET_ID             # ECS Fargate ansible/k8s runners: public subnet (egress via IGW, no NAT)"
  "ansible_ecs_security_group_ids=$JUMPOINT_SG         # runner SG (egress 0.0.0.0/0)"
  "ec2_ssh_key_secret=$SSH_SECRET_NAME                 # JSON {public_key,private_key} for EC2 cloud-init + Ansible"
  "bt_ecs_cluster=$ECS_CLUSTER                          # ECS cluster the Jumpoint Fargate task runs in"
  "bt_ecs_task_family=bt-jumpoint"
  "bt_ecs_image=beyondtrust/sra-jumpoint                # or your ECR mirror"
  "bt_ecs_execution_role_arn=$ROLE_ARN"
  "bt_ecs_launch_type=EC2                               # Jumpoint runs on EC2 capacity — Fargate cannot do protocol tunneling"
  "bt_ecs_host_instance_profile=$ECS_INSTANCE_ROLE     # instance profile the dashboard attaches to the on-demand Jumpoint host"
  "bt_ecs_jumpoint_subnet_id=$PUBLIC_SUBNET_ID         # subnet the dashboard launches the Jumpoint host into (public, IGW-routed)"
  "bt_ecs_jumpoint_security_group_id=$JUMPOINT_SG      # SG for the Jumpoint host"
  ""
  "# On-demand shared NAT instance — created on the first EC2 deploy, removed with the last VM (private-subnet VM outbound internet, no standing NAT/EIP):"
  "aws_nat_instance_enabled=true"
  "aws_nat_security_group_id=$NAT_SG                    # SG for the on-demand NAT instance"
  "aws_nat_instance_type=t4g.nano                       # NAT size (arm64); subnet defaults to the public/IGW subnet, AMI to newest AL2023"
  "aws_nat_instance_name=${NAME}-nat                    # EC2 Name tag (find-or-create key)"
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
    ├─ public  ${PUBLIC_SUBNET_ID}  (10.99.1.0/24) → IGW → internet  [Jumpoint host + ECS Fargate runners]
    ├─ private ${PRIVATE_SUBNET_ID}  (10.99.2.0/24) → internet via on-demand NAT instance (created with the first VM, removed with the last)  [user EC2s]
    └─ db      ${DB_SUBNET_A_ID} / ${DB_SUBNET_B_ID}  (10.99.3-4.0/24, 2 AZs) → no internet  [managed RDS]

  Managed EKS clusters build their OWN VPC + NAT-instance egress per cluster and
  VPC-peer back to this VPC (no sandbox NAT). Decommission clusters before rollback.

Note: the tunnel-capable Jumpoint runs on an EC2 ECS container instance
(t3.small) that the DASHBOARD creates on demand when you provision an EC2
instance or database, and terminates when the last one is removed — so there is
no standing jumpoint cost. (Fargate can't do protocol tunneling.) This script
only pre-creates the ecsInstanceRole it attaches.

To tear it down:
  ./scripts/sandbox/Linux/rollback.sh --cloud aws

EOF
