# Tear down sandbox infra created by Setup-AwsSandbox.ps1 / Setup-AzureSandbox.ps1
# / Setup-GcpSandbox.ps1. Tag/prefix-driven enumeration; refuses to delete if
# user VMs are still running in the sandbox network.
#
# Usage:
#   .\Rollback-Sandbox.ps1 -Cloud aws
#   .\Rollback-Sandbox.ps1 -Cloud azure
#   .\Rollback-Sandbox.ps1 -Cloud gcp
#   .\Rollback-Sandbox.ps1 -Cloud oci
#   .\Rollback-Sandbox.ps1 -Cloud all
#   .\Rollback-Sandbox.ps1 -Cloud aws -Yes      # skip the confirmation prompt

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('aws','azure','gcp','oci','all')]
    [string]$Cloud,

    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir 'lib/Common.ps1')

# ── AWS rollback ─────────────────────────────────────────────────────────────
function Invoke-AwsRollback {
    Assert-Command aws
    $region = if ($env:AWS_REGION) { $env:AWS_REGION }
              elseif ($env:AWS_DEFAULT_REGION) { $env:AWS_DEFAULT_REGION }
              else { 'us-east-2' }
    Assert-LoggedIn 'aws' { aws sts get-caller-identity --region $region --output json } 'Run: aws configure'

    Write-Section "AWS rollback in region $region"
    $filter = "Name=tag:$($Script:SandboxTagKey),Values=$($Script:SandboxTagValue)"

    if (-not $Yes) {
        if (-not (Confirm-Action "Delete AWS sandbox VPC, subnets, SGs, secrets in $region?")) { return }
    }

    # 1. Secret first (no dependencies).
    $secretName = 'dashboard/sandbox/ssh-keypair'
    & aws secretsmanager describe-secret --region $region --secret-id $secretName *> $null
    if ($LASTEXITCODE -eq 0) {
        aws secretsmanager delete-secret --region $region --secret-id $secretName `
            --force-delete-without-recovery | Out-Null
        Write-Ok "Deleted secret $secretName"
    }

    # 2. VPC and dependents.
    $vpcId = (aws ec2 describe-vpcs --region $region --filters $filter `
        --query 'Vpcs[0].VpcId' --output text 2>$null).Trim()
    if ($vpcId -and $vpcId -ne 'None') {
        Write-Info "VPC $vpcId"

        # Refuse if any non-terminated instances still attached to the VPC.
        $instances = (aws ec2 describe-instances --region $region `
            --filters "Name=vpc-id,Values=$vpcId" "Name=instance-state-name,Values=running,stopped,stopping,pending" `
            --query 'Reservations[].Instances[].InstanceId' --output text).Trim()
        if ($instances -and $instances -ne 'None') {
            Write-Warn "Instances still running in $vpcId : $instances"
            Write-Warn 'Terminate them via the dashboard first, then re-run rollback. Skipping VPC teardown.'
            return
        }

        # A managed EKS cluster owns a VPC peering back to this sandbox VPC (plus a
        # route on the private RT + cross-VPC SG rules), all in the cluster's own
        # Terraform state. Those block VPC teardown — refuse if any peering remains.
        $peerings = (aws ec2 describe-vpc-peering-connections --region $region `
            --filters "Name=status-code,Values=active,pending-acceptance,provisioning" `
            --query "VpcPeeringConnections[?RequesterVpcInfo.VpcId=='$vpcId' || AccepterVpcInfo.VpcId=='$vpcId'].VpcPeeringConnectionId" `
            --output text 2>$null).Trim()
        if ($peerings -and $peerings -ne 'None') {
            Write-Warn "Active VPC peering(s) on $vpcId : $peerings"
            Write-Warn 'An EKS cluster is still peered to this VPC. Decommission EKS clusters via the dashboard first, then re-run rollback. Skipping VPC teardown.'
            return
        }

        # RDS DB subnet groups in this VPC — RDS holds the subnets, so these
        # must go before the subnet sweep below or delete-subnet fails.
        $dbgs = (aws rds describe-db-subnet-groups --region $region `
            --query "DBSubnetGroups[?VpcId=='$vpcId'].DBSubnetGroupName" --output text 2>$null).Trim()
        foreach ($g in $dbgs.Split()) {
            if (-not $g -or $g -eq 'None') { continue }
            & aws rds delete-db-subnet-group --region $region --db-subnet-group-name $g *> $null
            if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted DB subnet group $g" }
            else { Write-Warn "Could not delete DB subnet group $g (a DB may still be provisioned — decommission it first)" }
        }

        # Interface VPC endpoints (SSM: ssm/ssmmessages/ec2messages). Created on-demand
        # by the dashboard (or by older setup scripts). Each holds an ENI in the private
        # subnet and references the ssm-vpce SG, so they MUST go before the SG and subnet
        # sweeps or those deletes fail — and each keeps billing (~$7/mo) if left behind.
        $vpces = (aws ec2 describe-vpc-endpoints --region $region `
            --filters "Name=vpc-id,Values=$vpcId" `
            --query 'VpcEndpoints[].VpcEndpointId' --output text 2>$null).Trim()
        if ($vpces -and $vpces -ne 'None') {
            $vpceIds = $vpces.Split()
            & aws ec2 delete-vpc-endpoints --region $region --vpc-endpoint-ids $vpceIds *> $null
            if ($LASTEXITCODE -eq 0) { Write-Ok "Deleting VPC endpoints $vpces (waiting for ENIs to detach…)" }
            else { Write-Warn "Could not delete VPC endpoints $vpces" }
            for ($i = 0; $i -lt 24; $i++) {  # ~60s budget; no AWS waiter for endpoints
                $left = (aws ec2 describe-vpc-endpoints --region $region --vpc-endpoint-ids $vpceIds `
                    --query "VpcEndpoints[?State!='deleted'].VpcEndpointId" --output text 2>$null)
                if (-not $left -or $left.Trim() -eq '' -or $left.Trim() -eq 'None') { break }
                Start-Sleep -Milliseconds 2500
            }
        }

        # Security groups (skip default).
        $sgs = (aws ec2 describe-security-groups --region $region `
            --filters $filter "Name=vpc-id,Values=$vpcId" `
            --query 'SecurityGroups[?GroupName!=`default`].GroupId' --output text).Trim()
        foreach ($sg in $sgs.Split()) {
            if (-not $sg) { continue }
            & aws ec2 delete-security-group --region $region --group-id $sg *> $null
            if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted SG $sg" }
            else { Write-Warn "Could not delete SG $sg (possibly referenced)" }
        }

        # Route tables.
        $rts = (aws ec2 describe-route-tables --region $region --filters $filter `
            --query 'RouteTables[].RouteTableId' --output text).Trim()
        foreach ($rt in $rts.Split()) {
            if (-not $rt) { continue }
            $assocs = (aws ec2 describe-route-tables --region $region --route-table-ids $rt `
                --query 'RouteTables[].Associations[?!Main].RouteTableAssociationId' --output text).Trim()
            foreach ($a in $assocs.Split()) {
                if (-not $a) { continue }
                & aws ec2 disassociate-route-table --region $region --association-id $a *> $null
            }
            & aws ec2 delete-route-table --region $region --route-table-id $rt *> $null
            if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted RT $rt" } else { Write-Warn "Could not delete RT $rt" }
        }

        # NAT gateway + its Elastic IP (the k8s node-egress NAT). The NAT must be
        # deleted BEFORE the subnets (it holds an ENI in the public subnet), and the
        # EIP released or it keeps billing while unattached.
        $nats = (aws ec2 describe-nat-gateways --region $region `
            --filter "Name=vpc-id,Values=$vpcId" $filter `
            --query "NatGateways[?State=='available' || State=='pending'].NatGatewayId" --output text 2>$null).Trim()
        foreach ($nat in $nats.Split()) {
            if (-not $nat -or $nat -eq 'None') { continue }
            & aws ec2 delete-nat-gateway --region $region --nat-gateway-id $nat *> $null
            if ($LASTEXITCODE -eq 0) { Write-Ok "Deleting NAT gateway $nat (waiting for it to drain…)" }
            else { Write-Warn "Could not delete NAT $nat" }
            & aws ec2 wait nat-gateway-deleted --region $region --nat-gateway-ids $nat *> $null
            if ($LASTEXITCODE -ne 0) { Write-Warn "NAT $nat still deleting — if subnet teardown fails, re-run rollback shortly" }
        }
        # Release sandbox-tagged Elastic IPs (detached once the NAT is gone).
        $eips = (aws ec2 describe-addresses --region $region --filters $filter `
            --query 'Addresses[].AllocationId' --output text 2>$null).Trim()
        foreach ($eip in $eips.Split()) {
            if (-not $eip -or $eip -eq 'None') { continue }
            & aws ec2 release-address --region $region --allocation-id $eip *> $null
            if ($LASTEXITCODE -eq 0) { Write-Ok "Released Elastic IP $eip" }
            else { Write-Warn "Could not release EIP $eip (may still be attached)" }
        }

        # Subnets.
        $subnets = (aws ec2 describe-subnets --region $region --filters $filter `
            --query 'Subnets[].SubnetId' --output text).Trim()
        foreach ($s in $subnets.Split()) {
            if (-not $s) { continue }
            aws ec2 delete-subnet --region $region --subnet-id $s | Out-Null
            Write-Ok "Deleted subnet $s"
        }

        # IGW (detach first).
        $igw = (aws ec2 describe-internet-gateways --region $region --filters $filter `
            --query 'InternetGateways[0].InternetGatewayId' --output text 2>$null).Trim()
        if ($igw -and $igw -ne 'None') {
            & aws ec2 detach-internet-gateway --region $region --internet-gateway-id $igw --vpc-id $vpcId *> $null
            aws ec2 delete-internet-gateway --region $region --internet-gateway-id $igw | Out-Null
            Write-Ok "Deleted IGW $igw"
        }

        # VPC last.
        aws ec2 delete-vpc --region $region --vpc-id $vpcId | Out-Null
        Write-Ok "Deleted VPC $vpcId"
    } else {
        Write-Info "No sandbox-tagged VPC found in $region"
    }

    # 3. IAM role: only delete if it carries our tag.
    $tagsJson = aws iam list-role-tags --role-name ecsTaskExecutionRole --output json 2>$null
    if ($LASTEXITCODE -eq 0 -and $tagsJson) {
        $tags = $tagsJson | ConvertFrom-Json
        $matching = $tags.Tags | Where-Object { $_.Key -eq $Script:SandboxTagKey -and $_.Value -eq $Script:SandboxTagValue }
        if ($matching) {
            & aws iam detach-role-policy --role-name ecsTaskExecutionRole `
                --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy *> $null
            & aws iam delete-role --role-name ecsTaskExecutionRole *> $null
            if ($LASTEXITCODE -eq 0) { Write-Ok 'Deleted IAM role ecsTaskExecutionRole' }
            else { Write-Warn 'Could not delete ecsTaskExecutionRole' }
        }
    }

    # 4. Promote-runner task role — same pattern, only delete if we created it.
    $promoteRole = "$($Script:SandboxNamePrefix)-promote-runner-task"
    $promoteTagsJson = aws iam list-role-tags --role-name $promoteRole --output json 2>$null
    if ($LASTEXITCODE -eq 0 -and $promoteTagsJson) {
        $tags = $promoteTagsJson | ConvertFrom-Json
        $matching = $tags.Tags | Where-Object { $_.Key -eq $Script:SandboxTagKey -and $_.Value -eq $Script:SandboxTagValue }
        if ($matching) {
            & aws iam delete-role-policy --role-name $promoteRole `
                --policy-name 'promote-runner-s3-write' *> $null
            & aws iam delete-role --role-name $promoteRole *> $null
            if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted IAM role $promoteRole" }
            else { Write-Warn "Could not delete $promoteRole" }
        }
    }

    # 5. vmimport — only delete if we tagged it. `vmimport` is a well-known AWS
    # name; an operator may have one pre-existing for unrelated reasons, so the
    # tag check is critical here.
    $vmiTagsJson = aws iam list-role-tags --role-name vmimport --output json 2>$null
    if ($LASTEXITCODE -eq 0 -and $vmiTagsJson) {
        $tags = $vmiTagsJson | ConvertFrom-Json
        $matching = $tags.Tags | Where-Object { $_.Key -eq $Script:SandboxTagKey -and $_.Value -eq $Script:SandboxTagValue }
        if ($matching) {
            & aws iam delete-role-policy --role-name vmimport `
                --policy-name 'vmimport-s3-and-ec2' *> $null
            & aws iam delete-role --role-name vmimport *> $null
            if ($LASTEXITCODE -eq 0) { Write-Ok 'Deleted IAM role vmimport' }
            else { Write-Warn 'Could not delete vmimport' }
        }
    }

    # 5b. Dashboard IAM user — sandbox-tagged only. AWS refuses delete-user
    # while access keys, managed-policy attachments, or inline policies
    # still exist, so unwind in order.
    $dashboardUser = "$($Script:SandboxNamePrefix)-app"
    $userTagsJson  = aws iam list-user-tags --user-name $dashboardUser --output json 2>$null
    if ($LASTEXITCODE -eq 0 -and $userTagsJson) {
        $uTags = $userTagsJson | ConvertFrom-Json
        $uMatch = $uTags.Tags | Where-Object { $_.Key -eq $Script:SandboxTagKey -and $_.Value -eq $Script:SandboxTagValue }
        if ($uMatch) {
            # Access keys first.
            $keys = @((aws iam list-access-keys --user-name $dashboardUser --output json 2>$null | ConvertFrom-Json).AccessKeyMetadata)
            foreach ($k in $keys) {
                & aws iam delete-access-key --user-name $dashboardUser --access-key-id $k.AccessKeyId *> $null
                if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted access key $($k.AccessKeyId)" }
                else { Write-Warn "Could not delete access key $($k.AccessKeyId)" }
            }
            # Detach managed policies.
            $attached = @((aws iam list-attached-user-policies --user-name $dashboardUser --output json 2>$null | ConvertFrom-Json).AttachedPolicies)
            foreach ($a in $attached) {
                & aws iam detach-user-policy --user-name $dashboardUser --policy-arn $a.PolicyArn *> $null
            }
            # Inline policies next (legacy / defensive).
            $policies = @((aws iam list-user-policies --user-name $dashboardUser --output json 2>$null | ConvertFrom-Json).PolicyNames)
            foreach ($p in $policies) {
                & aws iam delete-user-policy --user-name $dashboardUser --policy-name $p *> $null
            }
            & aws iam delete-user --user-name $dashboardUser *> $null
            if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted IAM user $dashboardUser" }
            else { Write-Warn "Could not delete IAM user $dashboardUser" }
        }
    }

    # 5c. Dashboard managed policy — only delete if we tagged it. Customer
    # managed policies need all non-default versions deleted first.
    $policiesJson = aws iam list-policies --scope Local --output json 2>$null
    if ($LASTEXITCODE -eq 0 -and $policiesJson) {
        $allPolicies = ($policiesJson | ConvertFrom-Json).Policies
        $dashPolicy  = $allPolicies | Where-Object { $_.PolicyName -eq 'dashboard-app-policy' } | Select-Object -First 1
        if ($dashPolicy) {
            $tagsJson = aws iam list-policy-tags --policy-arn $dashPolicy.Arn --output json 2>$null
            if ($LASTEXITCODE -eq 0 -and $tagsJson) {
                $pTags = ($tagsJson | ConvertFrom-Json).Tags
                $pMatch = $pTags | Where-Object { $_.Key -eq $Script:SandboxTagKey -and $_.Value -eq $Script:SandboxTagValue }
                if ($pMatch) {
                    $oldVids = @((aws iam list-policy-versions --policy-arn $dashPolicy.Arn --output json 2>$null | ConvertFrom-Json).Versions | Where-Object { -not $_.IsDefaultVersion })
                    foreach ($v in $oldVids) {
                        & aws iam delete-policy-version --policy-arn $dashPolicy.Arn --version-id $v.VersionId *> $null
                    }
                    & aws iam delete-policy --policy-arn $dashPolicy.Arn *> $null
                    if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted managed policy dashboard-app-policy" }
                    else { Write-Warn "Could not delete dashboard-app-policy" }
                }
            }
        }
    }

    # 6. Storage / promote-staging S3 bucket — empty then delete.
    $accountId = (aws sts get-caller-identity --query Account --output text 2>$null).Trim()
    $storageBucket = "$($Script:SandboxNamePrefix)-storage-$accountId"
    & aws s3api head-bucket --bucket $storageBucket --region $region *> $null
    if ($LASTEXITCODE -eq 0) {
        & aws s3 rm "s3://$storageBucket" --recursive --region $region *> $null
        & aws s3api delete-bucket --bucket $storageBucket --region $region *> $null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted S3 bucket $storageBucket" }
        else { Write-Warn "Could not delete S3 bucket $storageBucket (may have versioned objects)" }
    }

    Clear-StateDir aws
    Write-Ok 'AWS sandbox state cleared'
}

# ── Azure rollback ───────────────────────────────────────────────────────────
function Invoke-AzureRollback {
    Assert-Command az
    Assert-LoggedIn 'az' { az account show --output json } 'Run: az login'

    Write-Section 'Azure rollback'
    $rg = "$($Script:SandboxNamePrefix)-rg"

    & az group show -n $rg *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Info "Resource group $rg does not exist."
    } else {
        if (-not $Yes) {
            if (-not (Confirm-Action "Delete entire resource group $rg (cascades all sandbox resources)?")) { return }
        }
        Write-Info "Deleting resource group $rg (cascade)…"
        az group delete -n $rg --yes --no-wait | Out-Null
        Write-Ok 'Resource group deletion queued (no-wait)'
    }

    # Service principal — match by display name.
    $spName = "$($Script:SandboxNamePrefix)-sp"
    $spId   = (az ad sp list --display-name $spName --query '[0].appId' -o tsv 2>$null).Trim()
    if ($spId -and $spId -ne 'null') {
        & az ad sp delete --id $spId *> $null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted service principal $spName ($spId)" }
        else { Write-Warn 'Could not delete SP (insufficient perms?)' }
    }

    Clear-StateDir azure
    Write-Ok 'Azure sandbox state cleared'
}

# ── GCP rollback ─────────────────────────────────────────────────────────────
function Invoke-GcpRollback {
    Assert-Command gcloud
    Assert-LoggedIn 'gcloud' { gcloud auth print-access-token --quiet } 'Run: gcloud auth login'

    $projectId = if ($env:GCP_PROJECT_ID) { $env:GCP_PROJECT_ID } else {
        (gcloud config get-value project 2>$null).Trim()
    }
    if (-not $projectId -or $projectId -eq '(unset)') { Write-Die 'No GCP project set.' }

    Write-Section "GCP rollback in $projectId (all regions on the shared sandbox VPC)"
    if (-not $Yes) {
        if (-not (Confirm-Action "Delete the GCP sandbox VPC across ALL regions — subnets, routers/NAT, firewall rules (incl. orphaned rancher/GKE rules), serverless egress IPs, PSA range + peering, SA, secret in $projectId?")) { return }
    }

    $prefix = $Script:SandboxNamePrefix
    $vpc    = "$prefix-vpc"
    $router = "$prefix-router"
    $nat    = "$prefix-nat"

    # Guard: a LIVE Rancher management node (VM tagged `rancher` / named rancher-server)
    # owns firewall rules on this VPC. Refuse rather than force-delete under a running
    # node — tear it down via the dashboard first (AWS-parity active-owner guard).
    $rancherNodes = @(gcloud compute instances list --project $projectId `
        --filter="tags.items=rancher OR name=rancher-server" --format='value(name)' 2>$null) | Where-Object { $_ }
    if ($rancherNodes) {
        Write-Warn "Rancher management node(s) present: $($rancherNodes -join ', ')"
        Write-Warn 'Tear the Rancher node down via the dashboard (Kubernetes -> Rancher) first, then re-run rollback. Aborting.'
        return
    }

    # Guard: an ACTIVE GKE<->sandbox VPC peering (a non-co-located cluster lives in its
    # own VPC and only the peering touches this one). servicenetworking is ours (below).
    $gkePeers = @(gcloud compute networks peerings list --network $vpc --project $projectId `
        --format='value(name)' 2>$null) | Where-Object { $_ -and $_ -ne 'servicenetworking-googleapis-com' }
    if ($gkePeers) {
        Write-Warn "Active non-servicenetworking VPC peering(s) on $vpc : $($gkePeers -join ', ')"
        Write-Warn 'A GKE cluster is still peered to this VPC. Decommission it via the dashboard first, then re-run rollback. Aborting.'
        return
    }

    # Refuse if user VMs are still in the VPC.
    $instances = @(gcloud compute instances list --project $projectId `
        --filter "networkInterfaces.network:$vpc" --format='value(name)' 2>$null) | Where-Object { $_ }
    if ($instances) {
        Write-Warn "Instances still running in $vpc : $($instances -join ', ')"
        Write-Warn 'Terminate them via the dashboard first, then re-run rollback. Aborting.'
        return
    }

    # Every region that still has a sandbox subnet or router on the shared VPC. The VPC
    # is global but subnets/router/NAT are regional (same fixed names in each region), so
    # a multi-region sandbox must be torn down per region or the shared-VPC delete blocks.
    $regions = @(
        (gcloud compute networks subnets list --project $projectId --filter="network~/$vpc`$" --format='value(region.basename())' 2>$null)
        (gcloud compute routers list          --project $projectId --filter="network~/$vpc`$" --format='value(region.basename())' 2>$null)
    ) | Where-Object { $_ } | Sort-Object -Unique

    # 1. Release Cloud Run direct-VPC-egress serverless IPs (purpose=SERVERLESS). GCP
    # auto-reserves these in the jumpoint subnet on each ansible/k8s run and never frees
    # them, so they pin the subnet unless released before the subnet delete.
    $addrRows = @(gcloud compute addresses list --project $projectId `
        --filter="purpose=SERVERLESS AND subnetwork~/$prefix-" `
        --format='csv[no-heading](name,region.basename())' 2>$null) | Where-Object { $_ }
    foreach ($row in $addrRows) {
        $parts = $row.Split(','); $addr = $parts[0]; $areg = $parts[1]
        & gcloud compute addresses delete $addr --region $areg --project $projectId --quiet *> $null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Released serverless egress IP $addr ($areg)" }
        else { Write-Warn "Could not release serverless egress IP $addr ($areg)" }
    }

    # 2. NAT + Router per region (deleting the router also removes its child Cloud NAT).
    foreach ($reg in $regions) {
        & gcloud compute routers nats delete $nat --router $router --router-region $reg `
            --project $projectId --quiet *> $null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted NAT $nat ($reg)" }
        & gcloud compute routers delete $router --region $reg --project $projectId --quiet *> $null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted router $router ($reg)" }
    }

    # 3. Firewall rules: sandbox-owned (name prefix) then any rule still on the VPC —
    # the guards above already refused under a live owner, so what remains (e.g.
    # rancher-server-allow-mgmt, <cluster>-allow-ssh-from-k8s) is orphaned.
    $rulesText = @(gcloud compute firewall-rules list --project $projectId `
        --filter "name~^$prefix-" --format='value(name)' 2>$null) | Where-Object { $_ }
    foreach ($r in $rulesText) {
        & gcloud compute firewall-rules delete $r --project $projectId --quiet *> $null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted firewall rule $r" }
    }
    $vpcRules = @(gcloud compute firewall-rules list --project $projectId `
        --filter="network~/$vpc`$" --format='value(name)' 2>$null) | Where-Object { $_ }
    foreach ($r in $vpcRules) {
        & gcloud compute firewall-rules delete $r --project $projectId --quiet *> $null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted orphaned firewall rule $r" }
    }

    # 4. Subnets — every sandbox subnet (jumpoint/vm/k8s) across every region on the VPC.
    $subnetRows = @(gcloud compute networks subnets list --project $projectId `
        --filter="network~/$vpc`$" --format='csv[no-heading](name,region.basename())' 2>$null) | Where-Object { $_ }
    foreach ($row in $subnetRows) {
        $parts = $row.Split(','); $sn = $parts[0]; $sreg = $parts[1]
        & gcloud compute networks subnets delete $sn --region $sreg --project $projectId --quiet *> $null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted subnet $sn ($sreg)" }
        else { Write-Warn "Could not delete subnet $sn ($sreg)" }
    }

    # 5. servicenetworking peering + reserved PSA range (Cloud SQL private-IP path) —
    # both are VPC-scoped and pin the network delete.
    & gcloud compute networks peerings delete servicenetworking-googleapis-com `
        --network $vpc --project $projectId --quiet *> $null
    if ($LASTEXITCODE -eq 0) { Write-Ok "Removed servicenetworking peering on $vpc" }
    & gcloud compute addresses delete "$prefix-psa-range" --global --project $projectId --quiet *> $null
    if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted PSA range $prefix-psa-range" }

    # 6. VPC.
    & gcloud compute networks describe $vpc --project $projectId *> $null
    if ($LASTEXITCODE -eq 0) {
        & gcloud compute networks delete $vpc --project $projectId --quiet *> $null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted VPC $vpc" }
        else { Write-Warn "Could not delete VPC $vpc (check for lingering attachments)" }
    }

    # 7. Secret.
    & gcloud secrets delete "$prefix-ssh-keypair" --project $projectId --quiet *> $null
    if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted secret $prefix-ssh-keypair" }

    # 8. Storage / promote-staging GCS bucket — empty then delete. (Bucket-scoped
    # IAM bindings go with the bucket; no separate cleanup step needed.)
    $storageBucket = "$projectId-$prefix-storage"
    & gcloud storage buckets describe "gs://$storageBucket" --project $projectId *> $null
    if ($LASTEXITCODE -eq 0) {
        & gcloud storage rm "gs://$storageBucket" --recursive --project $projectId --quiet *> $null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted GCS bucket gs://$storageBucket" }
        else { Write-Warn "Could not delete bucket gs://$storageBucket (may have retained objects)" }
    }

    # 9. Service account.
    $saEmail = "$prefix-sa@$projectId.iam.gserviceaccount.com"
    & gcloud iam service-accounts describe $saEmail --project $projectId *> $null
    if ($LASTEXITCODE -eq 0) {
        foreach ($role in @('roles/compute.admin','roles/secretmanager.secretAccessor',
                            'roles/iam.serviceAccountUser','roles/run.admin','roles/run.developer',
                            'roles/run.invoker')) {
            & gcloud projects remove-iam-policy-binding $projectId `
                --member "serviceAccount:$saEmail" --role $role --condition=None --quiet *> $null
        }
        gcloud iam service-accounts delete $saEmail --project $projectId --quiet | Out-Null
        Write-Ok "Deleted service account $saEmail"
    }

    Clear-StateDir gcp
    Write-Ok 'GCP sandbox state cleared'
}

# ── OCI rollback ─────────────────────────────────────────────────────────────
function Invoke-OciRollback {
    Assert-Command oci
    $ociProfile    = if ($env:OCI_PROFILE) { $env:OCI_PROFILE } else { 'DEFAULT' }
    $configFile = if ($env:OCI_CLI_CONFIG_FILE) { $env:OCI_CLI_CONFIG_FILE } else { Join-Path $HOME '.oci/config' }
    Assert-LoggedIn 'oci' { oci iam region list --profile $ociProfile } 'Run: oci setup config'

    # Tenancy/region from state or the CLI config (to locate the compartment).
    function Get-OciCfg { param([string]$Key)
        if (-not (Test-Path $configFile)) { return '' }
        $inSection = $false
        foreach ($line in Get-Content $configFile) {
            if ($line -match '^\s*\[(.+)\]\s*$') { $inSection = ($Matches[1] -eq $ociProfile); continue }
            if ($inSection -and $line -match "^\s*$Key\s*=\s*(.+?)\s*$") { return $Matches[1] }
        }
        return ''
    }
    $tenancy = if ($env:OCI_TENANCY_OCID) { $env:OCI_TENANCY_OCID } else { Get-OciCfg 'tenancy' }
    $region  = if ($env:OCI_REGION) { $env:OCI_REGION } else { Get-OciCfg 'region' }
    $oci = @('--profile', $ociProfile); if ($region) { $oci += @('--region', $region) }

    function Get-Id { param([string[]]$OciArgs)
        $out = (& oci @oci @OciArgs 2>$null); if ($out) { $out = "$out".Trim() }
        if (-not $out -or $out -eq 'null') { return '' } else { return $out }
    }

    $prefix = $Script:SandboxNamePrefix
    $compartment = (Get-StateValue oci compartment).Trim()
    if (-not $compartment) {
        if (-not $tenancy) { Write-Die "No compartment in state and no tenancy in $configFile — cannot locate the OCI sandbox." }
        $compartment = Get-Id @('iam','compartment','list','--compartment-id',$tenancy,'--all','--query',"data[?name=='$prefix'].id | [0]",'--raw-output')
    }
    if (-not $compartment) { Write-Info 'No OCI sandbox compartment found — nothing to roll back.'; Clear-StateDir oci; return }

    Write-Section "OCI rollback in compartment $($compartment.Substring(0,[Math]::Min(20,$compartment.Length)))… (region $region)"
    if (-not $Yes) {
        if (-not (Confirm-Action 'Delete OCI sandbox VCN, subnets, gateways, security list in this compartment?')) { return }
    }

    $running = Get-Id @('compute','instance','list','--compartment-id',$compartment,'--all','--query',"data[?`"lifecycle-state`"=='RUNNING'].`"display-name`"",'--raw-output')
    if ($running -and $running -ne '[]') {
        Write-Warn "Instances still running: $running"
        Write-Warn 'Terminate them via the dashboard first, then re-run rollback. Aborting.'
        return
    }

    function Find-Id { param([string]$Sub, [string]$DisplayName)
        return Get-Id (($Sub -split ' ') + @('list','--compartment-id',$compartment,'--all','--query',"data[?`"display-name`"=='$DisplayName' && `"lifecycle-state`"!='TERMINATED'].id | [0]",'--raw-output'))
    }
    function Remove-Res { param([string]$Sub, [string]$IdFlag, [string]$Id, [string]$Label)
        if (-not $Id) { return }
        & oci @oci ($Sub -split ' ') delete $IdFlag $Id --force --wait-for-state TERMINATED *> $null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted $Label" } else { Write-Warn "Could not delete $Label (retry rollback if a dependency is still draining)" }
    }

    $vcn = Find-Id 'network vcn' "$prefix-vcn"
    if ($vcn) {
        foreach ($sn in @("$prefix-public-subnet","$prefix-vm-subnet","$prefix-db-subnet")) {
            Remove-Res 'network subnet' '--subnet-id' (Find-Id 'network subnet' $sn) "subnet $sn"
        }
        Remove-Res 'network route-table'   '--rt-id'            (Find-Id 'network route-table' "$prefix-public-rt")  'public route table'
        Remove-Res 'network route-table'   '--rt-id'            (Find-Id 'network route-table' "$prefix-private-rt") 'private route table'
        Remove-Res 'network security-list' '--security-list-id' (Find-Id 'network security-list' "$prefix-sl")       'security list'
        Remove-Res 'network nat-gateway'      '--nat-gateway-id' (Find-Id 'network nat-gateway' "$prefix-nat")        'NAT gateway'
        Remove-Res 'network internet-gateway' '--ig-id'          (Find-Id 'network internet-gateway' "$prefix-igw")   'Internet gateway'
        Remove-Res 'network vcn'           '--vcn-id' $vcn "VCN $prefix-vcn"
    } else {
        Write-Info 'No sandbox VCN found in the compartment.'
    }

    $vault = (Get-StateValue oci vault).Trim()
    if ($vault) { Write-Warn "Vault $vault + its SSH secret persist — schedule their deletion in the OCI console (KMS -> Vaults) if you want them gone." }

    # Delete the sandbox compartment we created (best-effort; async + slow).
    if (-not $env:OCI_COMPARTMENT_OCID) {
        $tag = Get-Id @('iam','compartment','get','--compartment-id',$compartment,'--query','data."freeform-tags"."managed-by"','--raw-output')
        if ($tag -eq 'dashboard-sandbox') {
            & oci @oci iam compartment delete --compartment-id $compartment --force *> $null
            if ($LASTEXITCODE -eq 0) { Write-Ok 'Compartment deletion submitted (async — can take several minutes)' }
            else { Write-Warn 'Could not delete compartment (must be empty first; re-run after resources drain)' }
        }
    }

    Clear-StateDir oci
    Write-Ok 'OCI sandbox state cleared'
}

# ── Dispatch ─────────────────────────────────────────────────────────────────
switch ($Cloud) {
    'aws'   { Invoke-AwsRollback }
    'azure' { Invoke-AzureRollback }
    'gcp'   { Invoke-GcpRollback }
    'oci'   { Invoke-OciRollback }
    'all'   { Invoke-AwsRollback; Invoke-AzureRollback; Invoke-GcpRollback; Invoke-OciRollback }
}

Write-Ok 'Rollback complete.'
