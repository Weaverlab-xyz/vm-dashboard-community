#!/usr/bin/env bash
# Tear down the sandbox infra created by setup-aws.sh / setup-azure.sh /
# setup-gcp.sh. Enumerates resources by the `managed-by=dashboard-sandbox`
# tag/label and the `dashboard-sandbox-` name prefix, so it survives a lost
# state file or a partially-completed setup.
#
# Usage:
#   ./rollback.sh --cloud aws         # tear down AWS sandbox
#   ./rollback.sh --cloud azure       # tear down Azure sandbox
#   ./rollback.sh --cloud gcp         # tear down GCP sandbox
#   ./rollback.sh --cloud all         # tear down all three
#   ./rollback.sh --cloud aws -y      # skip the confirmation prompt
#
# DOES NOT touch user-deployed EC2/VM/GCE instances — only the sandbox
# infra (VPC, subnets, SGs/NSGs/firewall rules, IAM artefacts, secrets).
# If you've deployed lab VMs, terminate them first via the dashboard.

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"

CLOUD=""
ASSUME_YES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cloud) CLOUD="$2"; shift 2;;
    -y|--yes) ASSUME_YES=1; shift;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# //; s/^#//'
      exit 0;;
    *) die "Unknown arg: $1";;
  esac
done

[[ -n "$CLOUD" ]] || die "Specify --cloud aws|azure|gcp|all"
require_supported_os

# ── AWS rollback ─────────────────────────────────────────────────────────────
rollback_aws() {
  require_cmd aws
  local region; region="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-2}}"
  ensure_logged_in "aws" "aws sts get-caller-identity --region $region" \
    "Run: aws configure"

  section "AWS rollback in region $region"
  local filter="Name=tag:$SANDBOX_TAG_KEY,Values=$SANDBOX_TAG_VALUE"

  if (( !ASSUME_YES )); then
    confirm "Delete AWS sandbox VPC, subnets, SGs, secrets in $region?" || return 0
  fi

  # 1. Secret first (no dependencies).
  local secret_name="dashboard/sandbox/ssh-keypair"
  if aws secretsmanager describe-secret --region "$region" --secret-id "$secret_name" >/dev/null 2>&1; then
    aws secretsmanager delete-secret --region "$region" --secret-id "$secret_name" \
      --force-delete-without-recovery >/dev/null
    ok "Deleted secret $secret_name"
  fi

  # 2. Find the VPC. If it's gone, skip everything network-related.
  local vpc_id
  vpc_id="$(aws ec2 describe-vpcs --region "$region" --filters "$filter" \
    --query 'Vpcs[0].VpcId' --output text 2>/dev/null || true)"
  if [[ "$vpc_id" != "None" && -n "$vpc_id" ]]; then
    info "VPC $vpc_id"

    # 2a. Delete any leftover EC2 instances inside the VPC. We don't auto-
    # terminate user dashboard-deployed VMs; warn instead.
    local instances
    instances="$(aws ec2 describe-instances --region "$region" \
      --filters "Name=vpc-id,Values=$vpc_id" "Name=instance-state-name,Values=running,stopped,stopping,pending" \
      --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null || true)"
    if [[ -n "$instances" && "$instances" != "None" ]]; then
      warn "Instances still running in $vpc_id: $instances"
      warn "Terminate them via the dashboard first, then re-run rollback. Skipping VPC teardown."
      return 0
    fi

    # 2a-rds. RDS DB subnet groups in this VPC — RDS holds the subnets, so these
    # must go before the subnet sweep below or delete-subnet fails.
    local dbgs
    dbgs="$(aws rds describe-db-subnet-groups --region "$region" \
      --query "DBSubnetGroups[?VpcId=='$vpc_id'].DBSubnetGroupName" --output text 2>/dev/null || true)"
    for g in $dbgs; do
      [[ -z "$g" || "$g" == "None" ]] && continue
      aws rds delete-db-subnet-group --region "$region" --db-subnet-group-name "$g" >/dev/null 2>&1 \
        && ok "Deleted DB subnet group $g" \
        || warn "Could not delete DB subnet group $g (a DB may still be provisioned — decommission it first)"
    done

    # 2b. Security groups (delete sandbox-tagged, but skip the default SG).
    local sgs
    sgs="$(aws ec2 describe-security-groups --region "$region" \
      --filters "$filter" "Name=vpc-id,Values=$vpc_id" \
      --query 'SecurityGroups[?GroupName!=`default`].GroupId' --output text)"
    for sg in $sgs; do
      aws ec2 delete-security-group --region "$region" --group-id "$sg" >/dev/null 2>&1 \
        && ok "Deleted SG $sg" || warn "Could not delete SG $sg (possibly referenced)"
    done

    # 2c. Route table associations + tables (skip the main RT).
    local rts
    rts="$(aws ec2 describe-route-tables --region "$region" --filters "$filter" \
      --query 'RouteTables[].RouteTableId' --output text)"
    for rt in $rts; do
      local assocs
      assocs="$(aws ec2 describe-route-tables --region "$region" \
        --route-table-ids "$rt" \
        --query 'RouteTables[].Associations[?!Main].RouteTableAssociationId' --output text)"
      for assoc in $assocs; do
        aws ec2 disassociate-route-table --region "$region" --association-id "$assoc" >/dev/null 2>&1 || true
      done
      aws ec2 delete-route-table --region "$region" --route-table-id "$rt" >/dev/null 2>&1 \
        && ok "Deleted RT $rt" || warn "Could not delete RT $rt"
    done

    # 2d. Subnets.
    local subnets
    subnets="$(aws ec2 describe-subnets --region "$region" --filters "$filter" \
      --query 'Subnets[].SubnetId' --output text)"
    for s in $subnets; do
      aws ec2 delete-subnet --region "$region" --subnet-id "$s" >/dev/null \
        && ok "Deleted subnet $s"
    done

    # 2e. IGW (detach first).
    local igw
    igw="$(aws ec2 describe-internet-gateways --region "$region" --filters "$filter" \
      --query 'InternetGateways[0].InternetGatewayId' --output text 2>/dev/null || true)"
    if [[ "$igw" != "None" && -n "$igw" ]]; then
      aws ec2 detach-internet-gateway --region "$region" --internet-gateway-id "$igw" --vpc-id "$vpc_id" >/dev/null 2>&1 || true
      aws ec2 delete-internet-gateway --region "$region" --internet-gateway-id "$igw" >/dev/null \
        && ok "Deleted IGW $igw"
    fi

    # 2f. VPC.
    aws ec2 delete-vpc --region "$region" --vpc-id "$vpc_id" >/dev/null \
      && ok "Deleted VPC $vpc_id"
  else
    info "No sandbox-tagged VPC found in $region"
  fi

  # 3. IAM: ecsTaskExecutionRole — only delete if WE created it (has our tag).
  if aws iam list-role-tags --role-name ecsTaskExecutionRole 2>/dev/null \
       | jq -e ".Tags[]? | select(.Key==\"$SANDBOX_TAG_KEY\" and .Value==\"$SANDBOX_TAG_VALUE\")" >/dev/null 2>&1; then
    aws iam detach-role-policy --role-name ecsTaskExecutionRole \
      --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy >/dev/null 2>&1 || true
    aws iam delete-role --role-name ecsTaskExecutionRole >/dev/null 2>&1 \
      && ok "Deleted IAM role ecsTaskExecutionRole" \
      || warn "Could not delete ecsTaskExecutionRole"
  fi

  # 4. Promote-runner task role — same pattern, only delete if we created it.
  local promote_role="${SANDBOX_NAME_PREFIX}-promote-runner-task"
  if aws iam list-role-tags --role-name "$promote_role" 2>/dev/null \
       | jq -e ".Tags[]? | select(.Key==\"$SANDBOX_TAG_KEY\" and .Value==\"$SANDBOX_TAG_VALUE\")" >/dev/null 2>&1; then
    # Delete the inline policy first.
    aws iam delete-role-policy --role-name "$promote_role" \
      --policy-name "promote-runner-s3-write" >/dev/null 2>&1 || true
    aws iam delete-role --role-name "$promote_role" >/dev/null 2>&1 \
      && ok "Deleted IAM role $promote_role" \
      || warn "Could not delete $promote_role"
  fi

  # 5. vmimport — only delete if we tagged it. `vmimport` is a well-known
  # AWS name; an operator may have one pre-existing for unrelated reasons,
  # so the tag check is critical here.
  if aws iam list-role-tags --role-name vmimport 2>/dev/null \
       | jq -e ".Tags[]? | select(.Key==\"$SANDBOX_TAG_KEY\" and .Value==\"$SANDBOX_TAG_VALUE\")" >/dev/null 2>&1; then
    aws iam delete-role-policy --role-name vmimport \
      --policy-name "vmimport-s3-and-ec2" >/dev/null 2>&1 || true
    aws iam delete-role --role-name vmimport >/dev/null 2>&1 \
      && ok "Deleted IAM role vmimport" \
      || warn "Could not delete vmimport"
  fi

  # 5b. Dashboard IAM user — sandbox-tagged only. AWS refuses delete-user
  # while access keys, inline policies, or managed-policy attachments still
  # exist, so unwind in order.
  local dashboard_user="${SANDBOX_NAME_PREFIX}-app"
  if aws iam list-user-tags --user-name "$dashboard_user" 2>/dev/null \
       | jq -e ".Tags[]? | select(.Key==\"$SANDBOX_TAG_KEY\" and .Value==\"$SANDBOX_TAG_VALUE\")" >/dev/null 2>&1; then
    # Access keys first.
    local key_ids
    key_ids="$(aws iam list-access-keys --user-name "$dashboard_user" \
      --query 'AccessKeyMetadata[*].AccessKeyId' --output text 2>/dev/null || true)"
    for k in $key_ids; do
      aws iam delete-access-key --user-name "$dashboard_user" --access-key-id "$k" >/dev/null 2>&1 \
        && ok "Deleted access key $k" \
        || warn "Could not delete access key $k"
    done
    # Detach managed policies (the dashboard-app-policy lives here now).
    local attached_arns
    attached_arns="$(aws iam list-attached-user-policies --user-name "$dashboard_user" \
      --query 'AttachedPolicies[*].PolicyArn' --output text 2>/dev/null || true)"
    for arn in $attached_arns; do
      aws iam detach-user-policy --user-name "$dashboard_user" --policy-arn "$arn" >/dev/null 2>&1 || true
    done
    # Then any inline policies (legacy / defensive — current setup uses managed).
    local policy_names
    policy_names="$(aws iam list-user-policies --user-name "$dashboard_user" \
      --query 'PolicyNames[*]' --output text 2>/dev/null || true)"
    for p in $policy_names; do
      aws iam delete-user-policy --user-name "$dashboard_user" --policy-name "$p" >/dev/null 2>&1 || true
    done
    aws iam delete-user --user-name "$dashboard_user" >/dev/null 2>&1 \
      && ok "Deleted IAM user $dashboard_user" \
      || warn "Could not delete IAM user $dashboard_user"
  fi

  # 5c. Dashboard managed policy — only delete if we tagged it. Customer
  # managed policies can have up to 5 versions; AWS requires all
  # non-default versions deleted before delete-policy.
  local dashboard_policy_arn
  dashboard_policy_arn="$(aws iam list-policies --scope Local --output json 2>/dev/null \
    | jq -r --arg name "dashboard-app-policy" '.Policies[]? | select(.PolicyName==$name) | .Arn' \
    | head -n1 || true)"
  if [[ -n "$dashboard_policy_arn" ]] && \
     aws iam list-policy-tags --policy-arn "$dashboard_policy_arn" 2>/dev/null \
       | jq -e ".Tags[]? | select(.Key==\"$SANDBOX_TAG_KEY\" and .Value==\"$SANDBOX_TAG_VALUE\")" >/dev/null 2>&1; then
    # Delete all non-default versions first.
    local old_vids
    old_vids="$(aws iam list-policy-versions --policy-arn "$dashboard_policy_arn" \
      --query 'Versions[?!IsDefaultVersion].VersionId' --output text 2>/dev/null || true)"
    for vid in $old_vids; do
      aws iam delete-policy-version --policy-arn "$dashboard_policy_arn" \
        --version-id "$vid" >/dev/null 2>&1 || true
    done
    aws iam delete-policy --policy-arn "$dashboard_policy_arn" >/dev/null 2>&1 \
      && ok "Deleted managed policy dashboard-app-policy" \
      || warn "Could not delete dashboard-app-policy"
  fi

  # 6. Storage / promote-staging S3 bucket — empty then delete.
  local storage_bucket="${SANDBOX_NAME_PREFIX}-storage-$(aws sts get-caller-identity --query Account --output text 2>/dev/null)"
  if aws s3api head-bucket --bucket "$storage_bucket" --region "$region" >/dev/null 2>&1; then
    # Versioning might be off in the sandbox, but rm --recursive handles
    # both versioned and unversioned objects.
    aws s3 rm "s3://$storage_bucket" --recursive --region "$region" >/dev/null 2>&1 || true
    aws s3api delete-bucket --bucket "$storage_bucket" --region "$region" >/dev/null 2>&1 \
      && ok "Deleted S3 bucket $storage_bucket" \
      || warn "Could not delete S3 bucket $storage_bucket (may have versioned objects)"
  fi

  state_clear aws
  ok "AWS sandbox state cleared"
}

# ── Azure rollback ───────────────────────────────────────────────────────────
rollback_azure() {
  require_cmd az
  ensure_logged_in "az" "az account show" "Run: az login"

  section "Azure rollback"
  local rg="${SANDBOX_NAME_PREFIX}-rg"

  if ! az group show -n "$rg" >/dev/null 2>&1; then
    info "Resource group $rg does not exist."
  else
    if (( !ASSUME_YES )); then
      confirm "Delete entire resource group $rg (cascades all sandbox resources)?" || return 0
    fi
    info "Deleting resource group $rg (cascade)…"
    az group delete -n "$rg" --yes --no-wait >/dev/null
    ok "Resource group deletion queued (no-wait)"
  fi

  # Service principal — match by display name.
  local sp_name="${SANDBOX_NAME_PREFIX}-sp"
  local sp_id
  sp_id="$(az ad sp list --display-name "$sp_name" --query '[0].appId' -o tsv 2>/dev/null || true)"
  if [[ -n "$sp_id" && "$sp_id" != "null" ]]; then
    az ad sp delete --id "$sp_id" >/dev/null 2>&1 \
      && ok "Deleted service principal $sp_name ($sp_id)" \
      || warn "Could not delete SP (insufficient perms?)"
  fi

  state_clear azure
  ok "Azure sandbox state cleared"
}

# ── GCP rollback ─────────────────────────────────────────────────────────────
rollback_gcp() {
  require_cmd gcloud
  ensure_logged_in "gcloud" "gcloud auth print-access-token --quiet" \
    "Run: gcloud auth login"

  local project_id="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
  local region="${GCP_REGION:-us-central1}"
  [[ -n "$project_id" && "$project_id" != "(unset)" ]] || die "No GCP project set."

  section "GCP rollback in $project_id, region $region"

  if (( !ASSUME_YES )); then
    confirm "Delete GCP sandbox network, NAT, firewall rules, SA, secret in $project_id?" || return 0
  fi

  local prefix="${SANDBOX_NAME_PREFIX}"
  local vpc="${prefix}-vpc"
  local jp_subnet="${prefix}-jumpoint-subnet"
  local vm_subnet="${prefix}-vm-subnet"
  local router="${prefix}-router"
  local nat="${prefix}-nat"

  # Refuse to tear down if user VMs are still running in the VPC.
  local instances
  instances="$(gcloud compute instances list --project "$project_id" \
    --filter "networkInterfaces.network:$vpc" --format="value(name)" 2>/dev/null || true)"
  if [[ -n "$instances" ]]; then
    warn "Instances still running in $vpc: $instances"
    warn "Terminate them via the dashboard first, then re-run rollback. Aborting."
    return 0
  fi

  # 1. NAT + Router (NAT first since it's a child of the router).
  gcloud compute routers nats delete "$nat" --router "$router" --router-region "$region" \
    --project "$project_id" --quiet >/dev/null 2>&1 && ok "Deleted NAT $nat" || true
  gcloud compute routers delete "$router" --region "$region" --project "$project_id" \
    --quiet >/dev/null 2>&1 && ok "Deleted router $router" || true

  # 2. Firewall rules (any rule whose name starts with prefix-).
  local rules
  rules="$(gcloud compute firewall-rules list --project "$project_id" \
    --filter "name~^${prefix}-" --format="value(name)")"
  for r in $rules; do
    gcloud compute firewall-rules delete "$r" --project "$project_id" --quiet >/dev/null \
      && ok "Deleted firewall rule $r"
  done

  # 3. Subnets, then VPC.
  for sn in "$jp_subnet" "$vm_subnet"; do
    if gcloud compute networks subnets describe "$sn" --region "$region" --project "$project_id" >/dev/null 2>&1; then
      gcloud compute networks subnets delete "$sn" --region "$region" --project "$project_id" --quiet >/dev/null \
        && ok "Deleted subnet $sn"
    fi
  done
  if gcloud compute networks describe "$vpc" --project "$project_id" >/dev/null 2>&1; then
    gcloud compute networks delete "$vpc" --project "$project_id" --quiet >/dev/null \
      && ok "Deleted VPC $vpc"
  fi

  # 4. Secret.
  gcloud secrets delete "${prefix}-ssh-keypair" --project "$project_id" --quiet >/dev/null 2>&1 \
    && ok "Deleted secret ${prefix}-ssh-keypair" || true

  # 5. Storage / promote-staging GCS bucket — empty then delete. (Bucket-scoped
  # IAM bindings go with the bucket; no separate cleanup step needed.)
  local storage_bucket="${project_id}-${prefix}-storage"
  if gcloud storage buckets describe "gs://$storage_bucket" --project "$project_id" >/dev/null 2>&1; then
    gcloud storage rm "gs://$storage_bucket" --recursive --project "$project_id" --quiet >/dev/null 2>&1 \
      && ok "Deleted GCS bucket gs://$storage_bucket" \
      || warn "Could not delete bucket gs://$storage_bucket (may have retained objects)"
  fi

  # 6. Service account (revoke role bindings + delete the SA).
  local sa_email="${prefix}-sa@${project_id}.iam.gserviceaccount.com"
  if gcloud iam service-accounts describe "$sa_email" --project "$project_id" >/dev/null 2>&1; then
    for role in roles/compute.admin roles/secretmanager.secretAccessor \
                 roles/iam.serviceAccountUser roles/run.admin roles/run.developer \
                 roles/run.invoker; do
      gcloud projects remove-iam-policy-binding "$project_id" \
        --member "serviceAccount:$sa_email" --role "$role" \
        --condition=None --quiet >/dev/null 2>&1 || true
    done
    gcloud iam service-accounts delete "$sa_email" --project "$project_id" --quiet >/dev/null \
      && ok "Deleted service account $sa_email"
  fi

  state_clear gcp
  ok "GCP sandbox state cleared"
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
case "$CLOUD" in
  aws)   rollback_aws ;;
  azure) rollback_azure ;;
  gcp)   rollback_gcp ;;
  all)   rollback_aws; rollback_azure; rollback_gcp ;;
  *) die "Invalid --cloud value: $CLOUD (expected aws|azure|gcp|all)" ;;
esac

ok "Rollback complete."
