# GCP sandbox bootstrap for the VM Dashboard (Windows PowerShell variant).
# Functional twin of setup-gcp.sh. See docs/CLOUD_SANDBOX.md for topology.

[CmdletBinding()] param()
$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir 'lib/Common.ps1')

Assert-Command gcloud
Assert-Command jq
Assert-Command ssh-keygen

$Name      = $Script:SandboxNamePrefix
$ProjectId = if ($env:GCP_PROJECT_ID) { $env:GCP_PROJECT_ID } else {
    (gcloud config get-value project 2>$null).Trim()
}
$Region    = if ($env:GCP_REGION) { $env:GCP_REGION } else { 'us-central1' }
$Zone      = if ($env:GCP_ZONE)   { $env:GCP_ZONE }   else { "$Region-a" }

if (-not $ProjectId -or $ProjectId -eq '(unset)') {
    Write-Die 'No GCP project set. Run: gcloud config set project YOUR-PROJECT  (or set $env:GCP_PROJECT_ID)'
}

Assert-LoggedIn 'gcloud' { gcloud auth print-access-token --quiet } `
    'Run: gcloud auth login && gcloud auth application-default login'

Write-Section "GCP sandbox in project $ProjectId, region $Region ($Zone)"

# GCP labels can't contain hyphens-as-keys — substitute underscore.
$Labels = "$($Script:SandboxTagKey -replace '-','_')=$($Script:SandboxTagValue -replace '-','_')"

$Vpc       = "$Name-vpc"
$JpSubnet  = "$Name-jumpoint-subnet"
$VmSubnet  = "$Name-vm-subnet"
$K8sSubnet = "$Name-k8s-subnet"
$Router    = "$Name-router"
$Nat       = "$Name-nat"

$NetTagJp  = 'bt-jumpoint'      # the dashboard's Jumpoint COS VM gets this tag
$NetTagVm  = "$Name-vm"         # the dashboard auto-attaches this to user VMs

# ── 1. Enable required APIs ───────────────────────────────────────────────────
Write-Section 'Enable APIs'
# run.googleapis.com is needed for the dashboard's automated image promote
# (the runner launches as a Cloud Run Job in the target project).
foreach ($api in @('compute.googleapis.com','secretmanager.googleapis.com','iam.googleapis.com','run.googleapis.com')) {
    gcloud services enable $api --project $ProjectId --quiet | Out-Null
}
Write-Ok 'Enabled compute, secretmanager, iam, run'

# ── 2. VPC + subnets ─────────────────────────────────────────────────────────
Write-Section 'VPC + subnets'
& gcloud compute networks describe $Vpc --project $ProjectId *> $null
if ($LASTEXITCODE -ne 0) {
    gcloud compute networks create $Vpc --project $ProjectId `
        --subnet-mode=custom --bgp-routing-mode=regional --quiet | Out-Null
    Write-Ok "Created VPC $Vpc (custom mode)"
} else {
    Write-Ok "Reusing VPC $Vpc"
}

& gcloud compute networks subnets describe $JpSubnet --project $ProjectId --region $Region *> $null
if ($LASTEXITCODE -ne 0) {
    gcloud compute networks subnets create $JpSubnet `
        --project $ProjectId --network $Vpc --region $Region `
        --range 10.99.1.0/24 --quiet | Out-Null
    Write-Ok "Created jumpoint subnet $JpSubnet (10.99.1.0/24)"
} else {
    Write-Ok "Reusing jumpoint subnet $JpSubnet"
}

& gcloud compute networks subnets describe $VmSubnet --project $ProjectId --region $Region *> $null
if ($LASTEXITCODE -ne 0) {
    gcloud compute networks subnets create $VmSubnet `
        --project $ProjectId --network $Vpc --region $Region `
        --range 10.99.2.0/24 --quiet | Out-Null
    Write-Ok "Created VM subnet $VmSubnet (10.99.2.0/24)"
} else {
    Write-Ok "Reusing VM subnet $VmSubnet"
}

# Dedicated subnet for managed Kubernetes (GKE) — separate from the jumpoint and
# VM subnets above.
& gcloud compute networks subnets describe $K8sSubnet --project $ProjectId --region $Region *> $null
if ($LASTEXITCODE -ne 0) {
    gcloud compute networks subnets create $K8sSubnet `
        --project $ProjectId --network $Vpc --region $Region `
        --range 10.99.3.0/24 --quiet | Out-Null
    Write-Ok "Created K8s subnet $K8sSubnet (10.99.3.0/24)"
} else {
    Write-Ok "Reusing K8s subnet $K8sSubnet"
}

Set-StateValue gcp vpc        $Vpc
Set-StateValue gcp jp_subnet  $JpSubnet
Set-StateValue gcp vm_subnet  $VmSubnet
Set-StateValue gcp k8s_subnet $K8sSubnet

# ── 3. Cloud Router + Cloud NAT (only the jumpoint subnet) ───────────────────
Write-Section 'Cloud Router + Cloud NAT (jumpoint subnet only)'
& gcloud compute routers describe $Router --project $ProjectId --region $Region *> $null
if ($LASTEXITCODE -ne 0) {
    gcloud compute routers create $Router `
        --project $ProjectId --network $Vpc --region $Region --quiet | Out-Null
    Write-Ok "Created router $Router"
} else {
    Write-Ok "Reusing router $Router"
}

& gcloud compute routers nats describe $Nat --project $ProjectId --router $Router --router-region $Region *> $null
if ($LASTEXITCODE -ne 0) {
    gcloud compute routers nats create $Nat `
        --project $ProjectId --router $Router --router-region $Region `
        --nat-custom-subnet-ip-ranges $JpSubnet `
        --auto-allocate-nat-external-ips --quiet | Out-Null
    Write-Ok "Created NAT $Nat (NAT'd subnets: $JpSubnet only)"
} else {
    Write-Ok "Reusing NAT $Nat"
}
Set-StateValue gcp router $Router
Set-StateValue gcp nat    $Nat

# ── 4. Firewall rules ────────────────────────────────────────────────────────
Write-Section 'Firewall rules'

# Idempotent — gcloud returns non-zero if the rule already exists; swallow.
gcloud compute firewall-rules create "$Name-allow-internal" `
    --project $ProjectId --network $Vpc --direction INGRESS --priority 65534 `
    --allow all --source-ranges 10.99.0.0/16 --quiet 2>$null | Out-Null

gcloud compute firewall-rules create "$Name-allow-ssh-from-jumpoint" `
    --project $ProjectId --network $Vpc --direction INGRESS --priority 1000 `
    --action ALLOW --rules tcp:22 `
    --source-tags $NetTagJp --target-tags $NetTagVm --quiet 2>$null | Out-Null

gcloud compute firewall-rules create "$Name-deny-vm-egress" `
    --project $ProjectId --network $Vpc --direction EGRESS --priority 1000 `
    --action DENY --rules all `
    --target-tags $NetTagVm --destination-ranges 0.0.0.0/0 --quiet 2>$null | Out-Null

gcloud compute firewall-rules create "$Name-allow-vm-egress-vpc" `
    --project $ProjectId --network $Vpc --direction EGRESS --priority 999 `
    --action ALLOW --rules all `
    --target-tags $NetTagVm --destination-ranges 10.99.0.0/16 --quiet 2>$null | Out-Null

Write-Ok 'Firewall rules: allow-internal, allow-ssh-from-jumpoint, deny-vm-egress, allow-vm-egress-vpc'

# ── 4b. Private Services Access + Cloud SQL reachability (managed databases) ──
# Private-IP Cloud SQL needs a reserved IP range + a servicenetworking VPC
# peering (the GCP analog of the AWS private DB subnet group). The instance's
# private IP lands in this peered range — outside 10.99.0.0/16 — so deny-vm-egress
# already blocks user VMs from it; only the jumpoint can reach it. An explicit
# egress ALLOW makes that auditable.
Write-Section 'Private Services Access + Cloud SQL (managed databases)'
gcloud services enable servicenetworking.googleapis.com sqladmin.googleapis.com `
    --project $ProjectId --quiet 2>$null | Out-Null

$PsaRange = "$Name-psa-range"
& gcloud compute addresses describe $PsaRange --global --project $ProjectId *> $null
if ($LASTEXITCODE -ne 0) {
    gcloud compute addresses create $PsaRange `
        --global --purpose VPC_PEERING --prefix-length 20 `
        --network $Vpc --project $ProjectId --quiet 2>$null | Out-Null
    Write-Ok "Allocated private-services-access range $PsaRange (/20)"
} else {
    Write-Ok "Reusing private-services-access range $PsaRange"
}

# Connect (or update) the servicenetworking peering. 'connect' fails if it
# already exists, so fall back to 'update --force'.
gcloud services vpc-peerings connect `
    --service servicenetworking.googleapis.com `
    --ranges $PsaRange --network $Vpc --project $ProjectId --quiet 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    gcloud services vpc-peerings update `
        --service servicenetworking.googleapis.com `
        --ranges $PsaRange --network $Vpc --project $ProjectId --force --quiet 2>$null | Out-Null
}
Write-Ok "servicenetworking peering on $Vpc (Cloud SQL private IP path)"

# Explicit, auditable egress ALLOW: jumpoint → the peered PSA range on 5432
# (postgres), 3306 (mysql), 1433 (sqlserver) — every managed-DB engine reaches via the tunnel.
$PsaCidr = (gcloud compute addresses describe $PsaRange --global --project $ProjectId `
    --format 'value(address,prefixLength)' 2>$null) -replace '\s+', '/'
if ($PsaCidr -match '^\d') {
    gcloud compute firewall-rules create "$Name-allow-db-from-jumpoint" `
        --project $ProjectId --network $Vpc --direction EGRESS --priority 998 `
        --action ALLOW --rules tcp:5432,tcp:3306,tcp:1433 `
        --target-tags $NetTagJp --destination-ranges $PsaCidr --quiet 2>$null | Out-Null
    Write-Ok "Firewall: allow-db-from-jumpoint (tcp:5432,tcp:3306,tcp:1433 -> $PsaCidr)"
} else {
    Write-Ok 'PSA CIDR not resolvable yet — skipping explicit DB egress rule (jumpoint default egress still reaches the DB)'
}

# ── 5. Service account ──────────────────────────────────────────────────────
Write-Section 'Service account'
$SaId    = "$Name-sa"
$SaEmail = "$SaId@$ProjectId.iam.gserviceaccount.com"

& gcloud iam service-accounts describe $SaEmail --project $ProjectId *> $null
if ($LASTEXITCODE -ne 0) {
    gcloud iam service-accounts create $SaId --project $ProjectId `
        --display-name 'Dashboard sandbox SA' --quiet | Out-Null
    Write-Ok "Created SA $SaEmail"
} else {
    Write-Ok "Reusing SA $SaEmail"
}

foreach ($role in @('roles/compute.admin','roles/secretmanager.secretAccessor',
                    'roles/iam.serviceAccountUser','roles/run.admin','roles/run.developer',
                    'roles/run.invoker','roles/cloudsql.admin','roles/servicenetworking.networksAdmin')) {
    gcloud projects add-iam-policy-binding $ProjectId `
        --member "serviceAccount:$SaEmail" --role $role --condition=None --quiet | Out-Null
}
Write-Ok 'Granted compute.admin, secretmanager.secretAccessor, iam.serviceAccountUser, run.{admin,developer,invoker}, cloudsql.admin, servicenetworking.networksAdmin'

$SaKeyPath = Join-Path (Get-StateDir gcp) 'sa-key.json'
if (-not (Test-Path $SaKeyPath) -or (Get-Item $SaKeyPath).Length -eq 0) {
    gcloud iam service-accounts keys create $SaKeyPath `
        --iam-account $SaEmail --project $ProjectId --quiet | Out-Null
    if ($IsLinux -or $IsMacOS) {
        & chmod 600 $SaKeyPath 2>$null
    } else {
        $acl = Get-Acl $SaKeyPath
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            ([System.Security.Principal.WindowsIdentity]::GetCurrent()).Name,
            'Read,Write','Allow')
        $acl.AddAccessRule($rule)
        Set-Acl $SaKeyPath $acl
    }
    Write-Ok "Created SA key at $SaKeyPath (owner-only)"
} else {
    Write-Ok "Reusing SA key at $SaKeyPath"
}

# ── 5b. Image-hub GCS bucket + promote-runner plumbing ───────────────────────
# Provisions the prerequisites the dashboard's automated cross-cloud image
# promote runner needs (see docs/image-management.md, runners/promote/README.md):
#
#   • A GCS bucket that doubles as the image-registry hub and the staging
#     bucket the promote-runner Cloud Run Job writes converted tar.gz disks
#     to (under promote-staging/) before compute.images.insert consumes them.
#   • storage.objectAdmin on that bucket for the dashboard SA — the runner
#     uploads as this SA via workload identity.
Write-Section 'Image-hub GCS bucket + promote-runner IAM'

# GCS bucket names are globally unique; prefix with project ID to avoid
# collisions in shared organisations.
$StorageBucket = "$ProjectId-$Name-storage"
& gcloud storage buckets describe "gs://$StorageBucket" --project $ProjectId *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Ok "Reusing GCS bucket gs://$StorageBucket"
} else {
    gcloud storage buckets create "gs://$StorageBucket" `
        --project $ProjectId --location $Region `
        --uniform-bucket-level-access `
        --public-access-prevention --quiet | Out-Null
    gcloud storage buckets update "gs://$StorageBucket" `
        --project $ProjectId --update-labels $Labels --quiet 2>$null | Out-Null
    Write-Ok "Created GCS bucket gs://$StorageBucket (uniform access, public prevention)"
}
Set-StateValue gcp storage_bucket $StorageBucket

# Grant the dashboard SA objectAdmin on the bucket — covers upload (runner) +
# read (dashboard mints signed URLs) + delete (cleanup after promote).
gcloud storage buckets add-iam-policy-binding "gs://$StorageBucket" `
    --member "serviceAccount:$SaEmail" --role 'roles/storage.objectAdmin' --quiet | Out-Null
Write-Ok "Granted $SaEmail storage.objectAdmin on gs://$StorageBucket"

# ── 6. Secret Manager: SSH keypair JSON ─────────────────────────────────────
Write-Section 'Secret Manager — SSH keypair'
$SshSecret = "$Name-ssh-keypair"
& gcloud secrets describe $SshSecret --project $ProjectId *> $null
if ($LASTEXITCODE -ne 0) {
    $kpJson = New-SshKeyPairJson
    $tmp    = [System.IO.Path]::GetTempFileName()
    try {
        Set-Content -Path $tmp -Value $kpJson -Encoding utf8 -NoNewline
        gcloud secrets create $SshSecret `
            --project $ProjectId --replication-policy=automatic `
            --labels=$Labels --data-file $tmp --quiet | Out-Null
        Write-Ok "Created secret $SshSecret"
    } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
} else {
    Write-Ok "Reusing secret $SshSecret"
}

# ── 7. Print config to paste into /setup ────────────────────────────────────
$cfg = @(
    "gcp_project_id=$ProjectId",
    "gcp_region=$Region",
    "gcp_zone=$Zone",
    "gcp_network=$Vpc",
    "gcp_subnetwork=$VmSubnet                                # User VMs land here (no NAT, no internet)",
    "gcp_jumpoint_subnetwork=$JpSubnet                       # Jumpoint COS lands here (Cloud NAT)",
    "gcp_ssh_key_secret_name=$SshSecret                      # JSON {public_key, private_key}",
    'gcp_jumpoint_image=beyondtrust/sra-jumpoint:latest',
    'gcp_jumpoint_machine_type=e2-micro',
    "gcp_default_network_tag=$NetTagVm                       # Auto-attached to every dashboard-deployed VM so the sandbox firewall rules apply",
    "gcp_service_account_json=`$(Get-Content $SaKeyPath -Raw)",
    '',
    '# Managed databases (Cloud SQL private IP via the PRA tunnel):',
    "gcp_db_network=projects/$ProjectId/global/networks/$Vpc   # Cloud SQL private_network (private-services-access peered on it)",
    '',
    '# Image-registry hub + automated cross-cloud promote:',
    "storage_gcs_bucket=$StorageBucket                       # Image hub + promote staging",
    'storage_active_backend=gcs                                # Active asset backend',
    'storage_hub_backend=gcs                                   # Image hub (defaults to active if unset)',
    'promote_runner_image=chrweav/dashboard-promote-runner:latest   # Public multi-arch image; override to Artifact Registry for a private/air-gapped registry',
    "promote_runner_gcp_region=$Region                         # Cloud Run Job lands here",
    "promote_runner_gcp_service_account=$SaEmail              # Workload-identity SA for the runner",
    "promote_runner_gcp_staging_bucket=$StorageBucket",
    '',
    '# BeyondTrust deploy key — set in /setup or /secrets:',
    'gcp_cloud_run_docker_deploy_key=…'
)
Write-DashboardConfig 'GCP sandbox configuration' $cfg
Export-ConfigJson -Cloud gcp -Lines $cfg   # machine-readable twin for Onboard-Sandbox.ps1
# The printed block shows a "$(Get-Content …)" placeholder so the SA private key
# never hits the terminal; the machine-readable config.json needs real contents.
if (Test-Path $SaKeyPath) {
    $gcpCfg = Join-Path (Get-StateDir gcp) 'config.json'
    $obj = Get-Content $gcpCfg -Raw | ConvertFrom-Json
    $obj.gcp_service_account_json = (Get-Content $SaKeyPath -Raw).Trim()
    ($obj | ConvertTo-Json -Compress -Depth 10) | Set-Content -LiteralPath $gcpCfg -Encoding utf8 -NoNewline
}

@"
Sandbox topology summary

  VPC $Vpc
    ├─ $JpSubnet (10.99.1.0/24) → Cloud NAT → internet  [Jumpoint COS]
    └─ $VmSubnet (10.99.2.0/24) → no NAT → no internet  [user VMs]

  Firewall:
    • allow-internal      : within 10.99.0.0/16
    • allow-ssh-from-jumpoint : tag $NetTagJp → tag $NetTagVm, tcp/22
    • deny-vm-egress      : tag $NetTagVm → 0.0.0.0/0 (any proto)
    • allow-vm-egress-vpc : tag $NetTagVm → 10.99.0.0/16

Service-account JSON cached at $SaKeyPath (owner-only).

The dashboard auto-applies the bt-jumpoint network tag to its Jumpoint COS
GCE instance and reads gcp_default_network_tag from config to attach
$NetTagVm to every user VM it deploys, so the sandbox firewall rules take
effect automatically — no per-deploy manual tagging needed.

To tear it down:
  .\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud gcp

"@ | Write-Host
