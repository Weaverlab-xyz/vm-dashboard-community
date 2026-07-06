# AWS sandbox bootstrap for the VM Dashboard (Windows PowerShell variant).
# Functional twin of setup-aws.sh — same resources, same tags, same idempotency.
# See docs/CLOUD_SANDBOX.md for the topology walkthrough.

[CmdletBinding()] param()
$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir 'lib/Common.ps1')

Assert-Command aws
Assert-Command jq
Assert-Command ssh-keygen

$Region = if ($env:AWS_REGION) { $env:AWS_REGION }
          elseif ($env:AWS_DEFAULT_REGION) { $env:AWS_DEFAULT_REGION }
          else { 'us-east-2' }
$Name = $Script:SandboxNamePrefix

Assert-LoggedIn 'aws' { aws sts get-caller-identity --region $Region --output json } `
    'Run: aws configure  (or: aws sso login)'

$AccountId = (aws sts get-caller-identity --query Account --output text).Trim()
Write-Section "AWS sandbox in account $AccountId, region $Region"

# Tag spec helper — same shape as the bash version's tag_spec().
function _TagSpec {
    param([string]$ResourceType, [string]$ResourceName)
    "ResourceType=$ResourceType,Tags=[{Key=Name,Value=$ResourceName},{Key=$($Script:SandboxTagKey),Value=$($Script:SandboxTagValue)}]"
}

# ── 1. VPC ────────────────────────────────────────────────────────────────────
Write-Section 'VPC'
$VpcId = (aws ec2 describe-vpcs --region $Region `
    --filters "Name=tag:$($Script:SandboxTagKey),Values=$($Script:SandboxTagValue)" `
    --query 'Vpcs[0].VpcId' --output text 2>$null)
if (-not $VpcId -or $VpcId -eq 'None') {
    $VpcId = (aws ec2 create-vpc --region $Region `
        --cidr-block 10.99.0.0/16 `
        --tag-specifications (_TagSpec 'vpc' "$Name-vpc") `
        --query 'Vpc.VpcId' --output text).Trim()
    aws ec2 modify-vpc-attribute --region $Region --vpc-id $VpcId --enable-dns-hostnames | Out-Null
    Write-Ok "Created VPC $VpcId (10.99.0.0/16)"
} else {
    Write-Ok "Reusing VPC $VpcId"
}
Set-StateValue aws vpc_id $VpcId

# ── 2. Internet Gateway ──────────────────────────────────────────────────────
Write-Section 'Internet Gateway'
$IgwId = (aws ec2 describe-internet-gateways --region $Region `
    --filters "Name=tag:$($Script:SandboxTagKey),Values=$($Script:SandboxTagValue)" `
    --query 'InternetGateways[0].InternetGatewayId' --output text 2>$null)
if (-not $IgwId -or $IgwId -eq 'None') {
    $IgwId = (aws ec2 create-internet-gateway --region $Region `
        --tag-specifications (_TagSpec 'internet-gateway' "$Name-igw") `
        --query 'InternetGateway.InternetGatewayId' --output text).Trim()
    aws ec2 attach-internet-gateway --region $Region --vpc-id $VpcId --internet-gateway-id $IgwId | Out-Null
    Write-Ok "Created and attached IGW $IgwId"
} else {
    Write-Ok "Reusing IGW $IgwId"
}
Set-StateValue aws igw_id $IgwId

# ── 3. Subnets ────────────────────────────────────────────────────────────────
Write-Section 'Subnets'
$Az = (aws ec2 describe-availability-zones --region $Region `
    --query 'AvailabilityZones[0].ZoneName' --output text).Trim()
# A second AZ is required for the RDS DB subnet group (RDS spans >= 2 AZs).
$Az2 = (aws ec2 describe-availability-zones --region $Region `
    --query 'AvailabilityZones[1].ZoneName' --output text).Trim()

function _MakeSubnet {
    param([string]$Cidr, [string]$Name, [string]$SubnetAz = $Az)
    $existing = (aws ec2 describe-subnets --region $Region `
        --filters "Name=vpc-id,Values=$VpcId" "Name=tag:Name,Values=$Name" `
        --query 'Subnets[0].SubnetId' --output text 2>$null)
    if ($existing -and $existing -ne 'None') { return $existing.Trim() }
    return (aws ec2 create-subnet --region $Region `
        --vpc-id $VpcId --cidr-block $Cidr --availability-zone $SubnetAz `
        --tag-specifications (_TagSpec 'subnet' $Name) `
        --query 'Subnet.SubnetId' --output text).Trim()
}

$PublicSubnetId  = _MakeSubnet '10.99.1.0/24' "$Name-public"
$PrivateSubnetId = _MakeSubnet '10.99.2.0/24' "$Name-private"
# Two private DB subnets in distinct AZs — the RDS DB subnet group needs >= 2 AZs.
# Dedicated to managed databases; the VM subnets above are left untouched.
$DbSubnetAId = _MakeSubnet '10.99.3.0/24' "$Name-db-a" $Az
$DbSubnetBId = _MakeSubnet '10.99.4.0/24' "$Name-db-b" $Az2
# Managed Kubernetes (EKS) no longer needs sandbox subnets — the aws_eks module
# builds its OWN VPC + subnets + NAT-instance egress per cluster and peers back
# to this VPC (see aws_vpc_id / aws_private_route_table_id emitted at the end).
Write-Ok "Public subnet (Jumpoint) $PublicSubnetId"
Write-Ok "Private subnet (VMs)    $PrivateSubnetId"
Write-Ok "DB subnet A ($Az)        $DbSubnetAId"
Write-Ok "DB subnet B ($Az2)        $DbSubnetBId"
Set-StateValue aws public_subnet_id  $PublicSubnetId
Set-StateValue aws private_subnet_id $PrivateSubnetId
Set-StateValue aws db_subnet_a_id    $DbSubnetAId
Set-StateValue aws db_subnet_b_id    $DbSubnetBId

# ── 4. Route tables ───────────────────────────────────────────────────────────
Write-Section 'Route tables'
function _MakeRouteTable {
    param([string]$Name)
    $existing = (aws ec2 describe-route-tables --region $Region `
        --filters "Name=vpc-id,Values=$VpcId" "Name=tag:Name,Values=$Name" `
        --query 'RouteTables[0].RouteTableId' --output text 2>$null)
    if ($existing -and $existing -ne 'None') { return $existing.Trim() }
    return (aws ec2 create-route-table --region $Region --vpc-id $VpcId `
        --tag-specifications (_TagSpec 'route-table' $Name) `
        --query 'RouteTable.RouteTableId' --output text).Trim()
}

$PublicRtId  = _MakeRouteTable "$Name-public-rt"
$PrivateRtId = _MakeRouteTable "$Name-private-rt"

# Public RT: 0.0.0.0/0 → IGW. Idempotent — swallow errors if route exists.
aws ec2 create-route --region $Region --route-table-id $PublicRtId `
    --destination-cidr-block 0.0.0.0/0 --gateway-id $IgwId 2>$null | Out-Null

function _AssociateRT {
    param([string]$RtId, [string]$SubnetId)
    $existing = (aws ec2 describe-route-tables --region $Region --route-table-ids $RtId `
        --query "RouteTables[0].Associations[?SubnetId=='$SubnetId'].RouteTableAssociationId" --output text)
    if (-not $existing -or $existing -eq 'None') {
        aws ec2 associate-route-table --region $Region --route-table-id $RtId --subnet-id $SubnetId | Out-Null
    }
}

# Move a subnet onto a route table, REPLACING any existing association (a subnet
# can have only one). _AssociateRT can't — associate-route-table errors
# Resource.AlreadyAssociated when the subnet already belongs to another RT (e.g. an
# older sandbox put the k8s subnets on the private RT). Idempotent on the target RT.
function _MoveSubnetToRt {
    param([string]$RtId, [string]$SubnetId)
    $curRt = (aws ec2 describe-route-tables --region $Region `
        --filters "Name=association.subnet-id,Values=$SubnetId" `
        --query 'RouteTables[0].RouteTableId' --output text 2>$null)
    if (-not $curRt -or $curRt -eq 'None') {
        aws ec2 associate-route-table --region $Region --route-table-id $RtId --subnet-id $SubnetId | Out-Null
    } elseif ($curRt -ne $RtId) {
        $assoc = (aws ec2 describe-route-tables --region $Region `
            --filters "Name=association.subnet-id,Values=$SubnetId" `
            --query "RouteTables[0].Associations[?SubnetId=='$SubnetId'].RouteTableAssociationId | [0]" --output text)
        aws ec2 replace-route-table-association --region $Region --association-id $assoc --route-table-id $RtId | Out-Null
    }
}
_AssociateRT $PublicRtId  $PublicSubnetId
_AssociateRT $PrivateRtId $PrivateSubnetId
_AssociateRT $PrivateRtId $DbSubnetAId
_AssociateRT $PrivateRtId $DbSubnetBId
Write-Ok "Public  RT $PublicRtId  → IGW (0.0.0.0/0)"
Write-Ok "Private RT $PrivateRtId → local VPC only (VMs + DBs)"

# ── 4a. K8s node egress — now owned by the EKS Terraform build ────────────────
# The sandbox no longer stands up a NAT for k8s (no standing NAT cost). Managed
# Kubernetes (EKS) is self-contained like AKS/GKE: the aws_eks module builds its
# OWN VPC + subnets + NAT instance per cluster (torn down with the cluster) and
# VPC-peers back to this sandbox VPC for direct management-plane access. The
# dashboard passes the peering inputs emitted at the end.
Set-StateValue aws private_rt_id $PrivateRtId

# ── 4b. RDS DB subnet group ───────────────────────────────────────────────────
# The managed-database feature deploys private RDS instances (no public
# endpoint) into this group; access is brokered only through the PRA tunnel.
Write-Section 'RDS DB subnet group'
$DbSubnetGroupName = "$Name-db"
$dbgExists = (aws rds describe-db-subnet-groups --region $Region `
    --db-subnet-group-name $DbSubnetGroupName `
    --query 'DBSubnetGroups[0].DBSubnetGroupName' --output text 2>$null)
if ($dbgExists -and $dbgExists -ne 'None') {
    Write-Ok "Reusing DB subnet group $DbSubnetGroupName"
} else {
    aws rds create-db-subnet-group --region $Region `
        --db-subnet-group-name $DbSubnetGroupName `
        --db-subnet-group-description "Private subnet group for dashboard-managed databases ($Name)" `
        --subnet-ids $DbSubnetAId $DbSubnetBId `
        --tags "Key=Name,Value=$DbSubnetGroupName" "Key=$($Script:SandboxTagKey),Value=$($Script:SandboxTagValue)" | Out-Null
    Write-Ok "Created DB subnet group $DbSubnetGroupName ($Az, $Az2)"
}
Set-StateValue aws db_subnet_group_name $DbSubnetGroupName

# ── 5. Security groups ────────────────────────────────────────────────────────
Write-Section 'Security groups'
function _MakeSG {
    param([string]$Name, [string]$Description)
    $existing = (aws ec2 describe-security-groups --region $Region `
        --filters "Name=vpc-id,Values=$VpcId" "Name=group-name,Values=$Name" `
        --query 'SecurityGroups[0].GroupId' --output text 2>$null)
    if ($existing -and $existing -ne 'None') { return $existing.Trim() }
    return (aws ec2 create-security-group --region $Region `
        --vpc-id $VpcId --group-name $Name --description $Description `
        --tag-specifications (_TagSpec 'security-group' $Name) `
        --query 'GroupId' --output text).Trim()
}

$JumpointSg = _MakeSG "$Name-jumpoint-sg" 'Jumpoint ECS task — outbound to internet, ingress from VPC'
$VmSg       = _MakeSG "$Name-vm-sg" 'Sandbox VMs — egress within VPC only, ingress SSH from Jumpoint SG'

# Wipe default egress so we control rules explicitly.
aws ec2 revoke-security-group-egress --region $Region --group-id $VmSg `
    --ip-permissions '[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]' 2>$null | Out-Null

aws ec2 authorize-security-group-egress --region $Region --group-id $VmSg `
    --ip-permissions '[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"10.99.0.0/16"}]}]' 2>$null | Out-Null
$ingressJson = "[{`"IpProtocol`":`"tcp`",`"FromPort`":22,`"ToPort`":22,`"UserIdGroupPairs`":[{`"GroupId`":`"$JumpointSg`"}]}]"
aws ec2 authorize-security-group-ingress --region $Region --group-id $VmSg `
    --ip-permissions $ingressJson 2>$null | Out-Null

Write-Ok "Jumpoint SG $JumpointSg (default egress 0.0.0.0/0)"
Write-Ok "VM SG       $VmSg (egress: VPC only; ingress 22/tcp from Jumpoint SG)"
Set-StateValue aws jumpoint_sg $JumpointSg
Set-StateValue aws vm_sg       $VmSg

# ── 6. SSH keypair JSON in Secrets Manager ────────────────────────────────────
Write-Section 'SSH keypair (Secrets Manager)'
$SshSecretName = 'dashboard/sandbox/ssh-keypair'

$exists = $false
& aws secretsmanager describe-secret --region $Region --secret-id $SshSecretName *> $null
if ($LASTEXITCODE -eq 0) { $exists = $true }

if ($exists) {
    Write-Ok "Reusing existing secret $SshSecretName"
} else {
    $kpJson = New-SshKeyPairJson
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        Set-Content -Path $tmp -Value $kpJson -Encoding utf8 -NoNewline
        aws secretsmanager create-secret --region $Region `
            --name $SshSecretName `
            --description 'Dashboard sandbox SSH keypair (autogenerated)' `
            --secret-string "file://$tmp" `
            --tags "Key=$($Script:SandboxTagKey),Value=$($Script:SandboxTagValue)" | Out-Null
        Write-Ok "Created secret $SshSecretName"
    } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
}
Set-StateValue aws ssh_secret $SshSecretName

# ── 7. ECS cluster + task execution role ─────────────────────────────────────
Write-Section 'ECS cluster + execution role'
$EcsCluster = 'bt-jumpoint'
aws ecs create-cluster --region $Region --cluster-name $EcsCluster `
    --tags "key=$($Script:SandboxTagKey),value=$($Script:SandboxTagValue)" 2>$null | Out-Null
Write-Ok "ECS cluster: $EcsCluster"

$RoleName = 'ecsTaskExecutionRole'
& aws iam get-role --role-name $RoleName *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Ok "Reusing IAM role $RoleName"
} else {
    $assumePolicy = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
    aws iam create-role --role-name $RoleName `
        --assume-role-policy-document $assumePolicy `
        --tags "Key=$($Script:SandboxTagKey),Value=$($Script:SandboxTagValue)" | Out-Null
    aws iam attach-role-policy --role-name $RoleName `
        --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy | Out-Null
    Write-Ok "Created IAM role $RoleName"
}
$RoleArn = (aws iam get-role --role-name $RoleName --query 'Role.Arn' --output text).Trim()
Set-StateValue aws ecs_role_arn $RoleArn

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
Write-Section 'Image-hub S3 bucket + promote-runner IAM'

$StorageBucket = "$Name-storage-$AccountId"
$bucketExists = $false
& aws s3api head-bucket --bucket $StorageBucket --region $Region *> $null
if ($LASTEXITCODE -eq 0) { $bucketExists = $true }

if ($bucketExists) {
    Write-Ok "Reusing S3 bucket $StorageBucket"
} else {
    # us-east-1 has its own create-bucket dance (no LocationConstraint allowed).
    if ($Region -eq 'us-east-1') {
        aws s3api create-bucket --bucket $StorageBucket --region $Region | Out-Null
    } else {
        aws s3api create-bucket --bucket $StorageBucket --region $Region `
            --create-bucket-configuration "LocationConstraint=$Region" | Out-Null
    }
    aws s3api put-bucket-tagging --bucket $StorageBucket `
        --tagging "TagSet=[{Key=$($Script:SandboxTagKey),Value=$($Script:SandboxTagValue)}]" 2>$null | Out-Null
    # Lock down public access — this bucket holds VHDs the dashboard presigns
    # short-lived URLs for; it should never serve anonymous reads.
    aws s3api put-public-access-block --bucket $StorageBucket `
        --public-access-block-configuration `
          'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true' `
        2>$null | Out-Null
    Write-Ok "Created S3 bucket $StorageBucket"
}
Set-StateValue aws storage_bucket $StorageBucket

$PromoteTaskRoleName = "$Name-promote-runner-task"
& aws iam get-role --role-name $PromoteTaskRoleName *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Ok "Reusing IAM role $PromoteTaskRoleName"
} else {
    $assumePolicy = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
    aws iam create-role --role-name $PromoteTaskRoleName `
        --assume-role-policy-document $assumePolicy `
        --tags "Key=$($Script:SandboxTagKey),Value=$($Script:SandboxTagValue)" | Out-Null
    Write-Ok "Created IAM role $PromoteTaskRoleName"
}
$promoteInlinePolicy = @"
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
      "Resource": "arn:aws:s3:::$StorageBucket/promote-staging/*"
    }
  ]
}
"@
aws iam put-role-policy --role-name $PromoteTaskRoleName `
    --policy-name 'promote-runner-s3-write' `
    --policy-document $promoteInlinePolicy | Out-Null
$PromoteTaskRoleArn = (aws iam get-role --role-name $PromoteTaskRoleName --query 'Role.Arn' --output text).Trim()
Write-Ok "Granted promote-runner task role S3 write on s3://$StorageBucket/promote-staging/*"
Set-StateValue aws promote_task_role_arn $PromoteTaskRoleArn

# vmimport service role — well-known name AWS expects unless overridden via
# aws_vmimport_role_name in the dashboard config. ec2:ImportImage assumes
# this role server-side to read the staged VHD + write the resulting AMI.
$VmImportRoleName = 'vmimport'
& aws iam get-role --role-name $VmImportRoleName *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Ok "Reusing IAM role $VmImportRoleName"
} else {
    $vmiTrust = @"
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
"@
    aws iam create-role --role-name $VmImportRoleName `
        --assume-role-policy-document $vmiTrust `
        --tags "Key=$($Script:SandboxTagKey),Value=$($Script:SandboxTagValue)" | Out-Null
    Write-Ok "Created IAM role $VmImportRoleName"
}
$vmiPolicy = @"
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
        "arn:aws:s3:::$StorageBucket",
        "arn:aws:s3:::$StorageBucket/*"
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
"@
aws iam put-role-policy --role-name $VmImportRoleName `
    --policy-name 'vmimport-s3-and-ec2' `
    --policy-document $vmiPolicy | Out-Null
Write-Ok "Granted $VmImportRoleName read+write on s3://$StorageBucket/* + ec2 image-import perms"

# ── 7c. Dashboard IAM user — programmatic creds for the app ───────────────────
# Create a sandbox-tagged IAM user with a single inline policy covering every
# AWS API the dashboard calls. Re-runs reuse the cached secret from
# $HOME/.dashboard-sandbox/aws/ rather than rotating (AWS allows at most 2
# access keys per user; rotation churn is unhelpful for a developer sandbox).
Write-Section 'Dashboard IAM user'
$DashboardUserName   = "$Name-app"
$DashboardPolicyName = 'dashboard-app-policy'

& aws iam get-user --user-name $DashboardUserName *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Ok "Reusing IAM user $DashboardUserName"
} else {
    aws iam create-user --user-name $DashboardUserName `
        --tags "Key=$($Script:SandboxTagKey),Value=$($Script:SandboxTagValue)" | Out-Null
    Write-Ok "Created IAM user $DashboardUserName"
}

# Inline user policies are capped at 2048 bytes; this policy is well over,
# so it's a customer-managed policy (6144-byte quota + versioning). On
# re-runs we create-policy-version --set-as-default so edits propagate
# without rotating the access key.
$DashboardPolicyArn = "arn:aws:iam::${AccountId}:policy/${DashboardPolicyName}"
$DashboardPolicy = @"
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
        "arn:aws:iam::${AccountId}:role/${VmImportRoleName}",
        "arn:aws:iam::${AccountId}:role/ecsTaskExecutionRole",
        "arn:aws:iam::${AccountId}:role/${PromoteTaskRoleName}",
        "arn:aws:iam::${AccountId}:role/k8s-*"
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
      "Resource": "arn:aws:iam::${AccountId}:role/aws-service-role/ecs.amazonaws.com/AWSServiceRoleForECS*",
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
      "Resource": "arn:aws:secretsmanager:*:${AccountId}:secret:dashboard/*"
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
        "arn:aws:s3:::${Name}-storage-*",
        "arn:aws:s3:::${Name}-storage-*/*"
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
      "Resource": "arn:aws:iam::${AccountId}:role/k8s-*"
    },
    {
      "Sid": "DashboardEKSServiceLinkedRole",
      "Effect": "Allow",
      "Action": "iam:CreateServiceLinkedRole",
      "Resource": "arn:aws:iam::${AccountId}:role/aws-service-role/eks*.amazonaws.com/*",
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
"@
# Compact the policy JSON — AWS measures actual bytes, smaller is better.
$DashboardPolicyCompact = ($DashboardPolicy | ConvertFrom-Json | ConvertTo-Json -Depth 20 -Compress)

& aws iam get-policy --policy-arn $DashboardPolicyArn *> $null
if ($LASTEXITCODE -eq 0) {
    # Policy exists — push a new version. Managed policies cap at 5 versions
    # total; if we're already at 5, prune the oldest non-default first.
    $versionCount = [int](aws iam list-policy-versions --policy-arn $DashboardPolicyArn `
        --query 'length(Versions)' --output text)
    if ($versionCount -ge 5) {
        $oldestVid = (aws iam list-policy-versions --policy-arn $DashboardPolicyArn `
            --query 'Versions[?!IsDefaultVersion]|[-1].VersionId' --output text).Trim()
        aws iam delete-policy-version --policy-arn $DashboardPolicyArn `
            --version-id $oldestVid | Out-Null
    }
    aws iam create-policy-version --policy-arn $DashboardPolicyArn `
        --policy-document $DashboardPolicyCompact --set-as-default | Out-Null
    Write-Ok "Updated managed policy $DashboardPolicyName (new default version)"
} else {
    aws iam create-policy --policy-name $DashboardPolicyName `
        --policy-document $DashboardPolicyCompact `
        --tags "Key=$($Script:SandboxTagKey),Value=$($Script:SandboxTagValue)" | Out-Null
    Write-Ok "Created managed policy $DashboardPolicyName"
}

aws iam attach-user-policy --user-name $DashboardUserName `
    --policy-arn $DashboardPolicyArn | Out-Null
Write-Ok "Attached $DashboardPolicyName to $DashboardUserName"

# Access-key management. Cache the secret in the same state dir the rest of
# the script uses so re-runs can re-print it.
$CachedAccessKeyId     = Get-StateValue aws access_key_id
$CachedSecretAccessKey = Get-StateValue aws secret_access_key
$ExistingKeysJson      = (& aws iam list-access-keys --user-name $DashboardUserName --output json 2>$null)
if (-not $ExistingKeysJson) { $ExistingKeysJson = '{"AccessKeyMetadata":[]}' }
$ExistingKeys = @(($ExistingKeysJson | ConvertFrom-Json).AccessKeyMetadata)

$cachedStillValid = $false
if ($CachedAccessKeyId -and $CachedSecretAccessKey) {
    $cachedStillValid = [bool]($ExistingKeys | Where-Object { $_.AccessKeyId -eq $CachedAccessKeyId })
}

if ($cachedStillValid) {
    $AwsAccessKeyId     = $CachedAccessKeyId
    $AwsSecretAccessKey = $CachedSecretAccessKey
    Write-Ok "Reusing cached access key $AwsAccessKeyId (from $((Get-StateDir aws)))"
} elseif ($ExistingKeys.Count -eq 0) {
    $KeyJson = (& aws iam create-access-key --user-name $DashboardUserName --output json) | ConvertFrom-Json
    $AwsAccessKeyId     = $KeyJson.AccessKey.AccessKeyId
    $AwsSecretAccessKey = $KeyJson.AccessKey.SecretAccessKey
    Set-StateValue aws access_key_id     $AwsAccessKeyId
    Set-StateValue aws secret_access_key $AwsSecretAccessKey
    Write-Ok "Created access key $AwsAccessKeyId (secret cached at $((Get-StateDir aws))\secret_access_key)"
} else {
    Write-Warn "IAM user $DashboardUserName has $($ExistingKeys.Count) existing access key(s) but no cached secret on this host."
    Write-Warn 'AWS does not let us re-read the secret of an existing key. Two ways forward:'
    Write-Warn "  1. Recover: aws iam list-access-keys --user-name $DashboardUserName ; aws iam delete-access-key --user-name $DashboardUserName --access-key-id <id> ; then re-run this script to mint a fresh key."
    Write-Warn '  2. Clean restart: .\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud aws ; then re-run this script.'
    $AwsAccessKeyId     = '<existing-key-from-AWS-Console>'
    $AwsSecretAccessKey = '<rotate-or-rollback-see-warning-above>'
}

# ── 8. Print config to paste into /setup ──────────────────────────────────────
$cfg = @(
    "aws_region=$Region",
    "aws_default_subnet_id=$PrivateSubnetId            # Deploy form's default subnet for new EC2 instances",
    "aws_default_security_group_id=$VmSg               # Deploy form's default SG (VM-tier, no internet egress)",
    "aws_db_subnet_group_name=$DbSubnetGroupName       # Managed-DB deploys: private RDS subnet group (2 AZs)",
    "aws_db_security_group_id=$VmSg                     # Managed-DB deploys: reuse the VM-tier SG (no internet egress)",
    "aws_vpc_id=$VpcId                                  # Sandbox VPC the EKS module peers its own VPC back to",
    "aws_vpc_cidr=10.99.0.0/16                          # Sandbox VPC CIDR (EKS peering route target)",
    "aws_private_route_table_id=$PrivateRtId            # Sandbox private RT — gets the EKS peering return route",
    "ansible_ecs_subnet_id=$PublicSubnetId              # ECS Fargate ansible/k8s runners: public subnet (egress via IGW, no NAT)",
    "ansible_ecs_security_group_ids=$JumpointSg         # runner SG (egress 0.0.0.0/0)",
    "ec2_ssh_key_secret=$SshSecretName                 # JSON {public_key,private_key} for EC2 cloud-init + Ansible",
    "bt_ecs_cluster=$EcsCluster                          # ECS cluster the Jumpoint Fargate task runs in",
    "bt_ecs_task_family=bt-jumpoint",
    "bt_ecs_image=beyondtrust/sra-jumpoint                # or your ECR mirror",
    "bt_ecs_execution_role_arn=$RoleArn",
    "bt_ecs_jumpoint_subnet_id=$PublicSubnetId         # Jumpoint task lands here (public, IGW-routed)",
    "bt_ecs_jumpoint_security_group_id=$JumpointSg      # SG for the Jumpoint task",
    "",
    "# Image-registry hub + automated cross-cloud promote:",
    "storage_s3_bucket=$StorageBucket                                       # Image hub + promote staging",
    "storage_active_backend=s3                                                  # Active asset backend",
    "storage_hub_backend=s3                                                     # Image hub (defaults to active if unset)",
    "promote_runner_image=chrweav/dashboard-promote-runner:latest         # Public multi-arch image; override to your ECR for a private/air-gapped registry",
    "promote_runner_ecs_cluster=$EcsCluster                                    # Reuses Jumpoint cluster",
    "promote_runner_ecs_execution_role_arn=$RoleArn                            # Image pull + CloudWatch logs",
    "promote_runner_ecs_task_role_arn=$PromoteTaskRoleArn                    # S3 PutObject on the staging bucket",
    "promote_runner_ecs_subnet_id=$PublicSubnetId                             # Runner needs egress to the presigned source URL",
    "promote_runner_ecs_security_group_ids=$JumpointSg                         # Reuses Jumpoint SG (egress 443)",
    "aws_vmimport_role_name=$VmImportRoleName                                 # Service role ec2:ImportImage assumes",
    "",
    "# Sandbox-provisioned AWS credentials for the dashboard IAM user ($DashboardUserName):",
    "aws_access_key_id=$AwsAccessKeyId",
    "aws_secret_access_key=$AwsSecretAccessKey",
    'aws_ecs_docker_deploy_key=…   # BeyondTrust SRA Jumpoint deploy key (paste manually)'
)
Write-DashboardConfig 'AWS sandbox configuration' $cfg
Export-ConfigJson -Cloud aws -Lines $cfg   # machine-readable twin for Onboard-Sandbox.ps1

@"
Sandbox topology summary

  VPC $VpcId (10.99.0.0/16)
    ├─ public  $PublicSubnetId  (10.99.1.0/24) → IGW → internet  [Jumpoint ECS]
    └─ private $PrivateSubnetId  (10.99.2.0/24) → no internet     [user EC2s]

To tear it down:
  .\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud aws

"@ | Write-Host
