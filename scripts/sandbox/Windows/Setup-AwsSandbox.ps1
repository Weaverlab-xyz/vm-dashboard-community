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

function _MakeSubnet {
    param([string]$Cidr, [string]$Name)
    $existing = (aws ec2 describe-subnets --region $Region `
        --filters "Name=vpc-id,Values=$VpcId" "Name=tag:Name,Values=$Name" `
        --query 'Subnets[0].SubnetId' --output text 2>$null)
    if ($existing -and $existing -ne 'None') { return $existing.Trim() }
    return (aws ec2 create-subnet --region $Region `
        --vpc-id $VpcId --cidr-block $Cidr --availability-zone $Az `
        --tag-specifications (_TagSpec 'subnet' $Name) `
        --query 'Subnet.SubnetId' --output text).Trim()
}

$PublicSubnetId  = _MakeSubnet '10.99.1.0/24' "$Name-public"
$PrivateSubnetId = _MakeSubnet '10.99.2.0/24' "$Name-private"
Write-Ok "Public subnet (Jumpoint) $PublicSubnetId"
Write-Ok "Private subnet (VMs)    $PrivateSubnetId"
Set-StateValue aws public_subnet_id  $PublicSubnetId
Set-StateValue aws private_subnet_id $PrivateSubnetId

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
_AssociateRT $PublicRtId  $PublicSubnetId
_AssociateRT $PrivateRtId $PrivateSubnetId
Write-Ok "Public  RT $PublicRtId  → IGW (0.0.0.0/0)"
Write-Ok "Private RT $PrivateRtId → local VPC only"

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

# ── 8. Print config to paste into /setup ──────────────────────────────────────
Write-DashboardConfig 'AWS sandbox configuration' @(
    "aws_region=$Region",
    "aws_default_subnet_id=$PrivateSubnetId            # Deploy form's default subnet for new EC2 instances",
    "aws_default_security_group_id=$VmSg               # Deploy form's default SG (VM-tier, no internet egress)",
    "ec2_ssh_key_secret=$SshSecretName                 # JSON {public_key,private_key} for EC2 cloud-init + Ansible",
    "bt_ecs_cluster=$EcsCluster                          # ECS cluster the Jumpoint Fargate task runs in",
    "bt_ecs_task_family=bt-jumpoint",
    "bt_ecs_image=beyondtrust/sra-jumpoint                # or your ECR mirror",
    "bt_ecs_execution_role_arn=$RoleArn",
    "bt_ecs_jumpoint_subnet_id=$PublicSubnetId         # Jumpoint task lands here (public, IGW-routed)",
    "bt_ecs_jumpoint_security_group_id=$JumpointSg      # SG for the Jumpoint task",
    "",
    "# Plus your AWS credentials:",
    'aws_access_key_id=…',
    'aws_secret_access_key=…',
    'aws_ecs_docker_deploy_key=…   # BeyondTrust SRA Jumpoint deploy key'
)

@"
Sandbox topology summary

  VPC $VpcId (10.99.0.0/16)
    ├─ public  $PublicSubnetId  (10.99.1.0/24) → IGW → internet  [Jumpoint ECS]
    └─ private $PrivateSubnetId  (10.99.2.0/24) → no internet     [user EC2s]

To tear it down:
  .\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud aws

"@ | Write-Host
