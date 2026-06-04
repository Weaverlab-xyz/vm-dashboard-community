# Tear down sandbox infra created by Setup-AwsSandbox.ps1 / Setup-AzureSandbox.ps1
# / Setup-GcpSandbox.ps1. Tag/prefix-driven enumeration; refuses to delete if
# user VMs are still running in the sandbox network.
#
# Usage:
#   .\Rollback-Sandbox.ps1 -Cloud aws
#   .\Rollback-Sandbox.ps1 -Cloud azure
#   .\Rollback-Sandbox.ps1 -Cloud gcp
#   .\Rollback-Sandbox.ps1 -Cloud all
#   .\Rollback-Sandbox.ps1 -Cloud aws -Yes      # skip the confirmation prompt

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('aws','azure','gcp','all')]
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
    $region = if ($env:GCP_REGION) { $env:GCP_REGION } else { 'us-central1' }
    if (-not $projectId -or $projectId -eq '(unset)') { Write-Die 'No GCP project set.' }

    Write-Section "GCP rollback in $projectId, region $region"
    if (-not $Yes) {
        if (-not (Confirm-Action "Delete GCP sandbox network, NAT, firewall rules, SA, secret in $projectId?")) { return }
    }

    $prefix   = $Script:SandboxNamePrefix
    $vpc      = "$prefix-vpc"
    $jpSubnet = "$prefix-jumpoint-subnet"
    $vmSubnet = "$prefix-vm-subnet"
    $router   = "$prefix-router"
    $nat      = "$prefix-nat"

    # Refuse if user VMs are still in the VPC.
    $instances = (gcloud compute instances list --project $projectId `
        --filter "networkInterfaces.network:$vpc" --format='value(name)' 2>$null).Trim()
    if ($instances) {
        Write-Warn "Instances still running in $vpc : $instances"
        Write-Warn 'Terminate them via the dashboard first, then re-run rollback. Aborting.'
        return
    }

    # 1. NAT + Router.
    & gcloud compute routers nats delete $nat --router $router --router-region $region `
        --project $projectId --quiet *> $null
    if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted NAT $nat" }
    & gcloud compute routers delete $router --region $region --project $projectId --quiet *> $null
    if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted router $router" }

    # 2. Firewall rules.
    $rulesText = (gcloud compute firewall-rules list --project $projectId `
        --filter "name~^$prefix-" --format='value(name)').Trim()
    foreach ($r in $rulesText.Split([Environment]::NewLine, [StringSplitOptions]::RemoveEmptyEntries)) {
        gcloud compute firewall-rules delete $r --project $projectId --quiet | Out-Null
        Write-Ok "Deleted firewall rule $r"
    }

    # 3. Subnets, then VPC.
    foreach ($sn in @($jpSubnet, $vmSubnet)) {
        & gcloud compute networks subnets describe $sn --region $region --project $projectId *> $null
        if ($LASTEXITCODE -eq 0) {
            gcloud compute networks subnets delete $sn --region $region --project $projectId --quiet | Out-Null
            Write-Ok "Deleted subnet $sn"
        }
    }
    & gcloud compute networks describe $vpc --project $projectId *> $null
    if ($LASTEXITCODE -eq 0) {
        gcloud compute networks delete $vpc --project $projectId --quiet | Out-Null
        Write-Ok "Deleted VPC $vpc"
    }

    # 4. Secret.
    & gcloud secrets delete "$prefix-ssh-keypair" --project $projectId --quiet *> $null
    if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted secret $prefix-ssh-keypair" }

    # 5. Storage / promote-staging GCS bucket — empty then delete. (Bucket-scoped
    # IAM bindings go with the bucket; no separate cleanup step needed.)
    $storageBucket = "$projectId-$prefix-storage"
    & gcloud storage buckets describe "gs://$storageBucket" --project $projectId *> $null
    if ($LASTEXITCODE -eq 0) {
        & gcloud storage rm "gs://$storageBucket" --recursive --project $projectId --quiet *> $null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted GCS bucket gs://$storageBucket" }
        else { Write-Warn "Could not delete bucket gs://$storageBucket (may have retained objects)" }
    }

    # 6. Service account.
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

# ── Dispatch ─────────────────────────────────────────────────────────────────
switch ($Cloud) {
    'aws'   { Invoke-AwsRollback }
    'azure' { Invoke-AzureRollback }
    'gcp'   { Invoke-GcpRollback }
    'all'   { Invoke-AwsRollback; Invoke-AzureRollback; Invoke-GcpRollback }
}

Write-Ok 'Rollback complete.'
