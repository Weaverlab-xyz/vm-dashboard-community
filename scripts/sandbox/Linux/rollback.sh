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
#   ./rollback.sh --cloud oci         # tear down OCI sandbox
#   ./rollback.sh --cloud all         # tear down all four
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

[[ -n "$CLOUD" ]] || die "Specify --cloud aws|azure|gcp|oci|all"
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

    # 2a-peer. A managed EKS cluster owns a VPC peering back to this sandbox VPC
    # (plus a route on the private RT + cross-VPC SG rules), all in the cluster's
    # own Terraform state. Those block VPC teardown — refuse if any peering is
    # still attached; decommission the EKS cluster(s) via the dashboard first.
    local peerings
    peerings="$(aws ec2 describe-vpc-peering-connections --region "$region" \
      --filters "Name=status-code,Values=active,pending-acceptance,provisioning" \
      --query "VpcPeeringConnections[?RequesterVpcInfo.VpcId=='$vpc_id' || AccepterVpcInfo.VpcId=='$vpc_id'].VpcPeeringConnectionId" \
      --output text 2>/dev/null || true)"
    if [[ -n "$peerings" && "$peerings" != "None" ]]; then
      warn "Active VPC peering(s) on $vpc_id: $peerings"
      warn "An EKS cluster is still peered to this VPC. Decommission EKS clusters via the dashboard first, then re-run rollback. Skipping VPC teardown."
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

    # 2a-vpce. Interface VPC endpoints (SSM: ssm/ssmmessages/ec2messages). Created
    # on-demand by the dashboard (or by older setup scripts). Each holds an ENI in
    # the private subnet and references the ssm-vpce SG, so they MUST go before the
    # SG (2b) and subnet (2d) sweeps or those deletes fail — and each keeps billing
    # (~$7/mo) if left behind. There's no AWS waiter, so poll until they clear.
    local vpces
    vpces="$(aws ec2 describe-vpc-endpoints --region "$region" \
      --filters "Name=vpc-id,Values=$vpc_id" \
      --query 'VpcEndpoints[].VpcEndpointId' --output text 2>/dev/null || true)"
    if [[ -n "$vpces" && "$vpces" != "None" ]]; then
      aws ec2 delete-vpc-endpoints --region "$region" --vpc-endpoint-ids $vpces >/dev/null 2>&1 \
        && ok "Deleting VPC endpoints $vpces (waiting for ENIs to detach…)" \
        || warn "Could not delete VPC endpoints $vpces"
      for _ in $(seq 1 24); do  # ~60s budget
        local left
        left="$(aws ec2 describe-vpc-endpoints --region "$region" \
          --vpc-endpoint-ids $vpces \
          --query "VpcEndpoints[?State!='deleted'].VpcEndpointId" --output text 2>/dev/null || true)"
        [[ -z "$left" || "$left" == "None" ]] && break
        sleep 2.5
      done
    fi

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

    # 2c-nat. NAT gateway + its Elastic IP (the k8s node-egress NAT). The NAT must
    # be deleted BEFORE the subnets (it holds an ENI in the public subnet), and the
    # EIP released or it keeps billing while unattached.
    local nats
    nats="$(aws ec2 describe-nat-gateways --region "$region" \
      --filter "Name=vpc-id,Values=$vpc_id" "$filter" \
      --query "NatGateways[?State=='available' || State=='pending'].NatGatewayId" --output text 2>/dev/null || true)"
    for nat in $nats; do
      [[ -z "$nat" || "$nat" == "None" ]] && continue
      aws ec2 delete-nat-gateway --region "$region" --nat-gateway-id "$nat" >/dev/null 2>&1 \
        && ok "Deleting NAT gateway $nat (waiting for it to drain…)" || warn "Could not delete NAT $nat"
      aws ec2 wait nat-gateway-deleted --region "$region" --nat-gateway-ids "$nat" 2>/dev/null \
        || warn "NAT $nat still deleting — if subnet teardown fails, re-run rollback shortly"
    done
    # Release sandbox-tagged Elastic IPs (detached once the NAT is gone).
    local eips
    eips="$(aws ec2 describe-addresses --region "$region" --filters "$filter" \
      --query 'Addresses[].AllocationId' --output text 2>/dev/null || true)"
    for eip in $eips; do
      [[ -z "$eip" || "$eip" == "None" ]] && continue
      aws ec2 release-address --region "$region" --allocation-id "$eip" >/dev/null 2>&1 \
        && ok "Released Elastic IP $eip" || warn "Could not release EIP $eip (may still be attached)"
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

  # 3b. IAM: ecsInstanceRole + instance profile — the EC2 capacity for the
  # tunnel-capable Jumpoint. The EC2 host itself is swept by the tag-based
  # instance termination above; here we unwind the role/profile (only if we
  # tagged it). A role must be removed from its instance profile before either
  # can be deleted.
  if aws iam list-role-tags --role-name ecsInstanceRole 2>/dev/null \
       | jq -e ".Tags[]? | select(.Key==\"$SANDBOX_TAG_KEY\" and .Value==\"$SANDBOX_TAG_VALUE\")" >/dev/null 2>&1; then
    aws iam remove-role-from-instance-profile --instance-profile-name ecsInstanceRole \
      --role-name ecsInstanceRole >/dev/null 2>&1 || true
    aws iam delete-instance-profile --instance-profile-name ecsInstanceRole >/dev/null 2>&1 \
      && ok "Deleted instance profile ecsInstanceRole" || true
    aws iam detach-role-policy --role-name ecsInstanceRole \
      --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role >/dev/null 2>&1 || true
    aws iam delete-role --role-name ecsInstanceRole >/dev/null 2>&1 \
      && ok "Deleted IAM role ecsInstanceRole" \
      || warn "Could not delete ecsInstanceRole"
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

  # Drop any external image-gallery role assignment we added (setup-azure.sh's
  # optional AZURE_IMAGE_GALLERY_RG step). Do this BEFORE deleting the SP so the
  # assignee still resolves. The corp gallery RG itself is NEVER deleted here —
  # only the assignment. The custom role definition is left in place (it may be
  # shared / assignable to other RGs); remove it manually if desired with
  #   az role definition delete --name "Dashboard Image Promoter"
  local gallery_rg gallery_sub
  gallery_rg="$(state_read azure image_gallery_rg)"
  if [[ -n "$gallery_rg" && -n "$sp_id" && "$sp_id" != "null" ]]; then
    gallery_sub="$(state_read azure image_gallery_sub)"
    [[ -n "$gallery_sub" ]] || gallery_sub="$(az account show --query id -o tsv 2>/dev/null || true)"
    az role assignment delete --assignee "$sp_id" \
      --scope "/subscriptions/$gallery_sub/resourceGroups/$gallery_rg" >/dev/null 2>&1 \
      && ok "Removed SP role assignment on external gallery RG $gallery_rg" \
      || warn "Could not remove gallery role assignment on $gallery_rg (already gone or insufficient perms)"
  fi

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
  [[ -n "$project_id" && "$project_id" != "(unset)" ]] || die "No GCP project set."

  section "GCP rollback in $project_id (all regions on the shared sandbox VPC)"

  if (( !ASSUME_YES )); then
    confirm "Delete the GCP sandbox VPC across ALL regions — subnets, routers/NAT, firewall rules (incl. orphaned rancher/GKE rules), serverless egress IPs, PSA range + peering, SA, secret in $project_id?" || return 0
  fi

  local prefix="${SANDBOX_NAME_PREFIX}"
  local vpc="${prefix}-vpc"
  local router="${prefix}-router"
  local nat="${prefix}-nat"

  # Guard: a LIVE Rancher management node (VM tagged `rancher` / named rancher-server)
  # owns firewall rules on this VPC. Don't force-delete under a running node — tear it
  # down via the dashboard first so its runtime state stays consistent (AWS-parity, cf.
  # the active-peering guard in rollback_aws).
  local rancher_nodes
  rancher_nodes="$(gcloud compute instances list --project "$project_id" \
    --filter="tags.items=rancher OR name=rancher-server" --format="value(name)" 2>/dev/null || true)"
  if [[ -n "$rancher_nodes" ]]; then
    warn "Rancher management node(s) present: ${rancher_nodes//$'\n'/ }"
    warn "Tear the Rancher node down via the dashboard (Kubernetes → Rancher) first, then re-run rollback. Aborting."
    return 0
  fi

  # Guard: an ACTIVE GKE↔sandbox VPC peering (a non-co-located cluster lives in its own
  # VPC and only the peering touches this one — no sandbox instance to catch). The
  # servicenetworking peering is ours and is torn down below.
  local gke_peers
  # NB: `peerings list` returns the NETWORK object with its peerings nested, so a bare
  # `value(name)` yields the VPC name (always non-empty when any peering — incl. our own
  # servicenetworking — exists) and misfires this guard. Flatten to the peering rows.
  gke_peers="$(gcloud compute networks peerings list --network "$vpc" --project "$project_id" \
    --flatten="peerings[]" --format="value(peerings.name)" 2>/dev/null | grep -vx 'servicenetworking-googleapis-com' || true)"
  if [[ -n "$gke_peers" ]]; then
    warn "Active non-servicenetworking VPC peering(s) on $vpc: ${gke_peers//$'\n'/ }"
    warn "A GKE cluster is still peered to this VPC. Decommission it via the dashboard first, then re-run rollback. Aborting."
    return 0
  fi

  # Refuse to tear down if user VMs are still running in the VPC.
  local instances
  instances="$(gcloud compute instances list --project "$project_id" \
    --filter "networkInterfaces.network:$vpc" --format="value(name)" 2>/dev/null || true)"
  if [[ -n "$instances" ]]; then
    warn "Instances still running in $vpc: ${instances//$'\n'/ }"
    warn "Terminate them via the dashboard first, then re-run rollback. Aborting."
    return 0
  fi

  # Every region that still has a sandbox subnet or router on the shared VPC. The VPC is
  # global but subnets/router/NAT are regional (same fixed names in each region), so a
  # multi-region sandbox must be torn down per region or the shared-VPC delete blocks.
  local regions
  regions="$({ gcloud compute networks subnets list --project "$project_id" \
                 --filter="network~/${vpc}\$" --format="value(region.basename())" 2>/dev/null
               gcloud compute routers list --project "$project_id" \
                 --filter="network~/${vpc}\$" --format="value(region.basename())" 2>/dev/null; } | sort -u || true)"

  # 1. Release Cloud Run direct-VPC-egress serverless IPs (purpose=SERVERLESS). GCP
  # auto-reserves these in the jumpoint subnet on each ansible/k8s run and never frees
  # them, so they pin the subnet unless released before the subnet delete.
  local addr_rows
  addr_rows="$(gcloud compute addresses list --project "$project_id" \
    --filter="purpose=SERVERLESS AND subnetwork~/${prefix}-" \
    --format="csv[no-heading](name,region.basename())" 2>/dev/null || true)"
  while IFS=, read -r addr areg; do
    [[ -n "$addr" ]] || continue
    gcloud compute addresses delete "$addr" --region "$areg" --project "$project_id" --quiet >/dev/null 2>&1 \
      && ok "Released serverless egress IP $addr ($areg)" \
      || warn "Could not release serverless egress IP $addr ($areg)"
  done <<< "$addr_rows"

  # 2. NAT + Router per region (deleting the router also removes its child Cloud NAT).
  for reg in $regions; do
    gcloud compute routers nats delete "$nat" --router "$router" --router-region "$reg" \
      --project "$project_id" --quiet >/dev/null 2>&1 && ok "Deleted NAT $nat ($reg)" || true
    gcloud compute routers delete "$router" --region "$reg" --project "$project_id" \
      --quiet >/dev/null 2>&1 && ok "Deleted router $router ($reg)" || true
  done

  # 3. Firewall rules. First the sandbox-owned rules (name prefix), then any rule still
  # attached to the VPC — the guards above already refused under a live owner, so what
  # remains (e.g. rancher-server-allow-mgmt, <cluster>-allow-ssh-from-k8s) is orphaned.
  local rules
  rules="$(gcloud compute firewall-rules list --project "$project_id" \
    --filter "name~^${prefix}-" --format="value(name)" 2>/dev/null || true)"
  for r in $rules; do
    gcloud compute firewall-rules delete "$r" --project "$project_id" --quiet >/dev/null 2>&1 \
      && ok "Deleted firewall rule $r" || true
  done
  local vpc_rules
  vpc_rules="$(gcloud compute firewall-rules list --project "$project_id" \
    --filter="network~/${vpc}\$" --format="value(name)" 2>/dev/null || true)"
  for r in $vpc_rules; do
    gcloud compute firewall-rules delete "$r" --project "$project_id" --quiet >/dev/null 2>&1 \
      && ok "Deleted orphaned firewall rule $r" || true
  done

  # 4. Subnets — every sandbox subnet (jumpoint/vm/k8s) across every region on the VPC.
  local subnet_rows
  subnet_rows="$(gcloud compute networks subnets list --project "$project_id" \
    --filter="network~/${vpc}\$" --format="csv[no-heading](name,region.basename())" 2>/dev/null || true)"
  while IFS=, read -r sn sreg; do
    [[ -n "$sn" ]] || continue
    gcloud compute networks subnets delete "$sn" --region "$sreg" --project "$project_id" --quiet >/dev/null 2>&1 \
      && ok "Deleted subnet $sn ($sreg)" \
      || warn "Could not delete subnet $sn ($sreg)"
  done <<< "$subnet_rows"

  # 5. servicenetworking peering + reserved PSA range (Cloud SQL private-IP path) — both
  # are VPC-scoped and pin the network delete.
  gcloud compute networks peerings delete servicenetworking-googleapis-com \
    --network "$vpc" --project "$project_id" --quiet >/dev/null 2>&1 \
    && ok "Removed servicenetworking peering on $vpc" || true
  gcloud compute addresses delete "${prefix}-psa-range" --global --project "$project_id" --quiet >/dev/null 2>&1 \
    && ok "Deleted PSA range ${prefix}-psa-range" || true

  # 6. VPC.
  if gcloud compute networks describe "$vpc" --project "$project_id" >/dev/null 2>&1; then
    gcloud compute networks delete "$vpc" --project "$project_id" --quiet >/dev/null 2>&1 \
      && ok "Deleted VPC $vpc" \
      || warn "Could not delete VPC $vpc (check for lingering attachments)"
  fi

  # 7. Secret.
  gcloud secrets delete "${prefix}-ssh-keypair" --project "$project_id" --quiet >/dev/null 2>&1 \
    && ok "Deleted secret ${prefix}-ssh-keypair" || true

  # 8. Storage / promote-staging GCS bucket — empty then delete. (Bucket-scoped
  # IAM bindings go with the bucket; no separate cleanup step needed.)
  local storage_bucket="${project_id}-${prefix}-storage"
  if gcloud storage buckets describe "gs://$storage_bucket" --project "$project_id" >/dev/null 2>&1; then
    gcloud storage rm "gs://$storage_bucket" --recursive --project "$project_id" --quiet >/dev/null 2>&1 \
      && ok "Deleted GCS bucket gs://$storage_bucket" \
      || warn "Could not delete bucket gs://$storage_bucket (may have retained objects)"
  fi

  # 9. Service account (revoke role bindings + delete the SA).
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

# ── OCI rollback ─────────────────────────────────────────────────────────────
rollback_oci() {
  require_cmd oci
  require_cmd jq
  local profile="${OCI_PROFILE:-DEFAULT}"
  local cfgfile="${OCI_CLI_CONFIG_FILE:-$HOME/.oci/config}"
  ensure_logged_in "oci" "oci iam region list --profile $profile" "Run: oci setup config"

  # Tenancy from state or the CLI config (needed to locate the compartment).
  local tenancy
  tenancy="${OCI_TENANCY_OCID:-$(awk -v p="[$profile]" '$0==p{i=1;next} /^\[/{i=0} i&&/^tenancy[[:space:]]*=/{sub(/^tenancy[[:space:]]*=[[:space:]]*/,"");print;exit}' "$cfgfile" 2>/dev/null || true)}"
  local region; region="${OCI_REGION:-$(awk -v p="[$profile]" '$0==p{i=1;next} /^\[/{i=0} i&&/^region[[:space:]]*=/{sub(/^region[[:space:]]*=[[:space:]]*/,"");print;exit}' "$cfgfile" 2>/dev/null || true)}"
  local OCIC=(oci --profile "$profile")
  [[ -n "$region" ]] && OCIC+=(--region "$region")

  local prefix="${SANDBOX_NAME_PREFIX}"
  local compartment; compartment="$(state_read oci compartment)"
  if [[ -z "$compartment" ]]; then
    [[ -n "$tenancy" ]] || die "No compartment in state and no tenancy in $cfgfile — cannot locate the OCI sandbox."
    compartment="$("${OCIC[@]}" iam compartment list --compartment-id "$tenancy" --all \
      --query "data[?name=='$prefix'].id | [0]" --raw-output 2>/dev/null || true)"
  fi
  if [[ -z "$compartment" || "$compartment" == "null" ]]; then
    info "No OCI sandbox compartment found — nothing to roll back."
    state_clear oci; return 0
  fi

  section "OCI rollback in compartment ${compartment:0:20}… (region ${region:-default})"
  if (( !ASSUME_YES )); then
    confirm "Delete OCI sandbox VCN, subnets, gateways, security list in this compartment?" || return 0
  fi

  # Refuse if user VMs are still running (don't auto-terminate lab VMs).
  local running
  running="$("${OCIC[@]}" compute instance list --compartment-id "$compartment" --all \
    --query "data[?\"lifecycle-state\"=='RUNNING'].\"display-name\"" --raw-output 2>/dev/null || true)"
  if [[ -n "$running" && "$running" != "[]" ]]; then
    warn "Instances still running: $running"
    warn "Terminate them via the dashboard first, then re-run rollback. Aborting."
    return 0
  fi

  # Helper: find a network resource id by display-name in the compartment.
  _oci_find() {  # $1=subcommand (space-sep) $2=name
    # shellcheck disable=SC2086
    "${OCIC[@]}" $1 list --compartment-id "$compartment" --all \
      --query "data[?\"display-name\"=='$2' && \"lifecycle-state\"!='TERMINATED'].id | [0]" \
      --raw-output 2>/dev/null || true
  }
  _oci_del() {  # $1=subcommand $2=id-flag $3=id $4=label
    [[ -z "$3" || "$3" == "null" ]] && return 0
    # shellcheck disable=SC2086
    "${OCIC[@]}" $1 delete $2 "$3" --force --wait-for-state TERMINATED >/dev/null 2>&1 \
      && ok "Deleted $4" || warn "Could not delete $4 (retry rollback if a dependency is still draining)"
  }

  local vcn; vcn="$(_oci_find "network vcn" "${prefix}-vcn")"
  if [[ -n "$vcn" && "$vcn" != "null" ]]; then
    # Subnets first (they hold route-table + security-list references).
    for sn in "${prefix}-public-subnet" "${prefix}-vm-subnet" "${prefix}-db-subnet"; do
      _oci_del "network subnet" --subnet-id "$(_oci_find "network subnet" "$sn")" "subnet $sn"
    done
    _oci_del "network route-table"   --rt-id            "$(_oci_find "network route-table" "${prefix}-public-rt")"  "public route table"
    _oci_del "network route-table"   --rt-id            "$(_oci_find "network route-table" "${prefix}-private-rt")" "private route table"
    _oci_del "network security-list" --security-list-id "$(_oci_find "network security-list" "${prefix}-sl")"       "security list"
    _oci_del "network nat-gateway"      --nat-gateway-id "$(_oci_find "network nat-gateway" "${prefix}-nat")"        "NAT gateway"
    _oci_del "network internet-gateway" --ig-id          "$(_oci_find "network internet-gateway" "${prefix}-igw")"  "Internet gateway"
    _oci_del "network vcn"           --vcn-id "$vcn" "VCN ${prefix}-vcn"
  else
    info "No sandbox VCN found in the compartment."
  fi

  # Vault + secret can only be SCHEDULED for deletion (no hard delete); they're
  # near-free and self-contained, so we leave them with a note rather than
  # thread the RFC3339 timestamp + management-endpoint plumbing here.
  local vault; vault="$(state_read oci vault)"
  [[ -n "$vault" ]] && warn "Vault $vault + its SSH secret persist — schedule their deletion in the OCI console (KMS → Vaults) if you want them gone."

  # Delete the sandbox compartment we created (best-effort; async + slow). Only
  # if it carries our freeform tag AND we didn't reuse a caller-supplied one.
  if [[ -z "${OCI_COMPARTMENT_OCID:-}" ]]; then
    local tag
    tag="$("${OCIC[@]}" iam compartment get --compartment-id "$compartment" \
      --query 'data."freeform-tags"."managed-by"' --raw-output 2>/dev/null || true)"
    if [[ "$tag" == "dashboard-sandbox" ]]; then
      "${OCIC[@]}" iam compartment delete --compartment-id "$compartment" --force >/dev/null 2>&1 \
        && ok "Compartment deletion submitted (async — can take several minutes)" \
        || warn "Could not delete compartment (must be empty first; re-run after resources drain)"
    fi
  fi

  state_clear oci
  ok "OCI sandbox state cleared"
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
case "$CLOUD" in
  aws)   rollback_aws ;;
  azure) rollback_azure ;;
  gcp)   rollback_gcp ;;
  oci)   rollback_oci ;;
  all)   rollback_aws; rollback_azure; rollback_gcp; rollback_oci ;;
  *) die "Invalid --cloud value: $CLOUD (expected aws|azure|gcp|oci|all)" ;;
esac

ok "Rollback complete."
