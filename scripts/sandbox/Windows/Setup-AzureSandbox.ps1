# Azure sandbox bootstrap for the VM Dashboard (Windows PowerShell variant).
# Functional twin of setup-azure.sh. See docs/CLOUD_SANDBOX.md for topology.

[CmdletBinding()] param()
$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir 'lib/Common.ps1')

Assert-Command az
Assert-Command jq
Assert-Command ssh-keygen

$Location  = if ($env:AZURE_LOCATION) { $env:AZURE_LOCATION } else { 'centralus' }
$Name      = $Script:SandboxNamePrefix
$Rg        = "$Name-rg"
$VnetName  = "$Name-vnet"
$AciSubnet = 'aci-subnet'
$VmSubnet  = 'vm-subnet'
$K8sSubnet = 'k8s-subnet'
$DesktopsSubnet = 'desktops-subnet'
$NsgName   = "$Name-vm-nsg"
$DesktopsNsg = "$Name-desktops-nsg"

Assert-LoggedIn 'az' { az account show --output json } 'Run: az login'

$SubscriptionId = (az account show --query id       -o tsv).Trim()
$TenantId       = (az account show --query tenantId -o tsv).Trim()
Write-Section "Azure sandbox in subscription $SubscriptionId, location $Location"

$Tags = "$($Script:SandboxTagKey)=$($Script:SandboxTagValue)"

# ── 1. Resource Group ─────────────────────────────────────────────────────────
Write-Section 'Resource group'
az group create -n $Rg -l $Location --tags $Tags | Out-Null
Write-Ok "Resource group $Rg"
Set-StateValue azure rg $Rg

# ── 2. VNet + subnets ─────────────────────────────────────────────────────────
Write-Section 'VNet + subnets'
az network vnet create -g $Rg -n $VnetName --address-prefix 10.99.0.0/16 --tags $Tags | Out-Null
Write-Ok "VNet $VnetName (10.99.0.0/16)"

az network vnet subnet create -g $Rg --vnet-name $VnetName -n $AciSubnet `
    --address-prefix 10.99.1.0/24 `
    --delegations Microsoft.ContainerInstance/containerGroups | Out-Null
Write-Ok "ACI subnet $AciSubnet (10.99.1.0/24, delegated)"

az network vnet subnet create -g $Rg --vnet-name $VnetName -n $VmSubnet `
    --address-prefix 10.99.2.0/24 | Out-Null
Write-Ok "VM subnet $VmSubnet (10.99.2.0/24)"

# Dedicated subnet for managed Kubernetes (AKS) — separate from the ACI and VM
# subnets above.
az network vnet subnet create -g $Rg --vnet-name $VnetName -n $K8sSubnet `
    --address-prefix 10.99.3.0/24 | Out-Null
Write-Ok "K8s subnet $K8sSubnet (10.99.3.0/24)"

$AciSubnetId = (az network vnet subnet show -g $Rg --vnet-name $VnetName -n $AciSubnet --query id -o tsv).Trim()
$VmSubnetId  = (az network vnet subnet show -g $Rg --vnet-name $VnetName -n $VmSubnet  --query id -o tsv).Trim()
$K8sSubnetId = (az network vnet subnet show -g $Rg --vnet-name $VnetName -n $K8sSubnet --query id -o tsv).Trim()
Set-StateValue azure aci_subnet_id $AciSubnetId
Set-StateValue azure vm_subnet_id  $VmSubnetId
Set-StateValue azure k8s_subnet_id $K8sSubnetId

# ── 2b. Managed-database subnets + private DNS zone (Flexible Server) ─────────
# Private VNet-integrated Flexible Server needs a subnet delegated to
# Microsoft.DBforPostgreSQL/flexibleServers + a private DNS zone linked to the
# VNet. The tunnel-capable jumpoint runs on a VM (ACI can't tunnel), so it gets
# its own subnet with internet egress.
Write-Section 'Managed-database subnets + private DNS zone'
$DbSubnet = 'db-subnet'
az network vnet subnet create -g $Rg --vnet-name $VnetName -n $DbSubnet `
    --address-prefix 10.99.4.0/24 `
    --delegations Microsoft.DBforPostgreSQL/flexibleServers | Out-Null
Write-Ok "DB subnet $DbSubnet (10.99.4.0/24, delegated to flexibleServers)"

$JpSubnet = 'jumpoint-subnet'
az network vnet subnet create -g $Rg --vnet-name $VnetName -n $JpSubnet `
    --address-prefix 10.99.5.0/24 | Out-Null
Write-Ok "Jumpoint subnet $JpSubnet (10.99.5.0/24, internet egress for the VM jumpoint)"

$DbSubnetId = (az network vnet subnet show -g $Rg --vnet-name $VnetName -n $DbSubnet --query id -o tsv).Trim()
$JpSubnetId = (az network vnet subnet show -g $Rg --vnet-name $VnetName -n $JpSubnet --query id -o tsv).Trim()

$DbDnsZone = "$Name.private.postgres.database.azure.com"
az network private-dns zone create -g $Rg -n $DbDnsZone 2>$null | Out-Null
az network private-dns link vnet create -g $Rg -n "$Name-db-dns-link" `
    --zone-name $DbDnsZone --virtual-network $VnetName --registration-enabled false 2>$null | Out-Null
$DbDnsZoneId = (az network private-dns zone show -g $Rg -n $DbDnsZone --query id -o tsv 2>$null)
if ($DbDnsZoneId) { $DbDnsZoneId = $DbDnsZoneId.Trim() }
Write-Ok "Private DNS zone $DbDnsZone linked to $VnetName"

Set-StateValue azure db_subnet_id           $DbSubnetId
Set-StateValue azure jumpoint_subnet_id     $JpSubnetId
Set-StateValue azure db_private_dns_zone_id $DbDnsZoneId

# MySQL Flexible Server needs its OWN delegated subnet (Microsoft.DBforMySQL/
# flexibleServers — a delegated subnet hosts only one flexible-server type) + its
# own private DNS zone (...mysql.database.azure.com).
$DbMysqlSubnet = 'db-mysql-subnet'
az network vnet subnet create -g $Rg --vnet-name $VnetName -n $DbMysqlSubnet `
    --address-prefix 10.99.7.0/24 `
    --delegations Microsoft.DBforMySQL/flexibleServers | Out-Null
Write-Ok "MySQL DB subnet $DbMysqlSubnet (10.99.7.0/24, delegated to DBforMySQL/flexibleServers)"
$DbMysqlSubnetId = (az network vnet subnet show -g $Rg --vnet-name $VnetName -n $DbMysqlSubnet --query id -o tsv).Trim()

$DbMysqlDnsZone = "$Name.private.mysql.database.azure.com"
az network private-dns zone create -g $Rg -n $DbMysqlDnsZone 2>$null | Out-Null
az network private-dns link vnet create -g $Rg -n "$Name-db-mysql-dns-link" `
    --zone-name $DbMysqlDnsZone --virtual-network $VnetName --registration-enabled false 2>$null | Out-Null
$DbMysqlDnsZoneId = (az network private-dns zone show -g $Rg -n $DbMysqlDnsZone --query id -o tsv 2>$null)
if ($DbMysqlDnsZoneId) { $DbMysqlDnsZoneId = $DbMysqlDnsZoneId.Trim() }
Write-Ok "Private DNS zone $DbMysqlDnsZone linked to $VnetName"

Set-StateValue azure db_mysql_subnet_id           $DbMysqlSubnetId
Set-StateValue azure db_mysql_private_dns_zone_id $DbMysqlDnsZoneId

# ── 3. NSG: deny VM internet egress, allow VNet ──────────────────────────────
Write-Section 'NSG (block VM internet egress)'
az network nsg create -g $Rg -n $NsgName --tags $Tags | Out-Null

az network nsg rule create -g $Rg --nsg-name $NsgName -n allow-vnet-out `
    --priority 100 --direction Outbound --access Allow --protocol "*" `
    --source-address-prefix VirtualNetwork --source-port-range "*" `
    --destination-address-prefix VirtualNetwork --destination-port-range "*" | Out-Null
az network nsg rule create -g $Rg --nsg-name $NsgName -n deny-internet-out `
    --priority 200 --direction Outbound --access Deny --protocol "*" `
    --source-address-prefix "*" --source-port-range "*" `
    --destination-address-prefix Internet --destination-port-range "*" | Out-Null
az network nsg rule create -g $Rg --nsg-name $NsgName -n allow-vnet-in `
    --priority 100 --direction Inbound --access Allow --protocol "*" `
    --source-address-prefix VirtualNetwork --source-port-range "*" `
    --destination-address-prefix VirtualNetwork --destination-port-range "*" | Out-Null
Write-Ok "NSG ${NsgName}: VM subnet egress restricted to VirtualNetwork"

az network vnet subnet update -g $Rg --vnet-name $VnetName -n $VmSubnet `
    --network-security-group $NsgName | Out-Null
Write-Ok "Attached NSG to $VmSubnet"
Set-StateValue azure vm_nsg $NsgName

# ── 3b. Desktops subnet + NSG (VDI pools) ────────────────────────────────────
# VDI desktop pools land here. Unlike vm-subnet (all Internet egress denied),
# desktops need outbound 443 so the BeyondTrust RS jump client can register with
# the appliance at FIRST BOOT (it phones home directly, not via the Jumpoint).
# NOT delegated — a delegated subnet (e.g. aci-subnet) can't host VM NICs.
Write-Section 'Desktops subnet + NSG (VDI)'
& az network vnet subnet show -g $Rg --vnet-name $VnetName -n $DesktopsSubnet *> $null
if ($LASTEXITCODE -ne 0) {
    az network vnet subnet create -g $Rg --vnet-name $VnetName -n $DesktopsSubnet `
        --address-prefix 10.99.6.0/24 | Out-Null
}
Write-Ok "Desktops subnet $DesktopsSubnet (10.99.6.0/24, no delegation)"

az network nsg create -g $Rg -n $DesktopsNsg --tags $Tags | Out-Null
# Outbound: allow HTTPS to Internet (jump-client registration + Windows update/
# activation) and VNet; deny other Internet egress.
az network nsg rule create -g $Rg --nsg-name $DesktopsNsg -n allow-https-out `
    --priority 100 --direction Outbound --access Allow --protocol Tcp `
    --source-address-prefix "*" --source-port-range "*" `
    --destination-address-prefix Internet --destination-port-range 443 | Out-Null
az network nsg rule create -g $Rg --nsg-name $DesktopsNsg -n allow-vnet-out `
    --priority 110 --direction Outbound --access Allow --protocol "*" `
    --source-address-prefix VirtualNetwork --source-port-range "*" `
    --destination-address-prefix VirtualNetwork --destination-port-range "*" | Out-Null
az network nsg rule create -g $Rg --nsg-name $DesktopsNsg -n deny-internet-out `
    --priority 200 --direction Outbound --access Deny --protocol "*" `
    --source-address-prefix "*" --source-port-range "*" `
    --destination-address-prefix Internet --destination-port-range "*" | Out-Null
# Inbound: RDP from the VNet so the PRA Jumpoint can broker in.
az network nsg rule create -g $Rg --nsg-name $DesktopsNsg -n allow-rdp-vnet-in `
    --priority 100 --direction Inbound --access Allow --protocol Tcp `
    --source-address-prefix VirtualNetwork --source-port-range "*" `
    --destination-address-prefix VirtualNetwork --destination-port-range 3389 | Out-Null
Write-Ok "NSG ${DesktopsNsg}: outbound 443 (jump client) + VNet; RDP in from VNet"

az network vnet subnet update -g $Rg --vnet-name $VnetName -n $DesktopsSubnet `
    --network-security-group $DesktopsNsg | Out-Null
Write-Ok "Attached NSG to $DesktopsSubnet"

$DesktopsSubnetId = (az network vnet subnet show -g $Rg --vnet-name $VnetName -n $DesktopsSubnet --query id -o tsv).Trim()
Set-StateValue azure desktops_subnet_id $DesktopsSubnetId
Set-StateValue azure desktops_nsg $DesktopsNsg

# ── 4. Storage account + file share for ACI /jpt persistence ─────────────────
Write-Section 'Storage account (ACI /jpt persistence)'
$saHash   = ($SubscriptionId -replace '-','').Substring(0, 8).ToLower()
$cleaned  = ($Name -replace '-','').ToLower()
$SaName   = ($cleaned + $saHash)
if ($SaName.Length -gt 24) { $SaName = $SaName.Substring(0, 24) }

& az storage account show -g $Rg -n $SaName *> $null
if ($LASTEXITCODE -ne 0) {
    az storage account create -g $Rg -n $SaName -l $Location --sku Standard_LRS --tags $Tags | Out-Null
}
az storage share-rm create -g $Rg --storage-account $SaName -n 'jpt' --quota 1 2>$null | Out-Null
Write-Ok "Storage account $SaName (file share: jpt)"
Set-StateValue azure sa_name $SaName

# ── 5. Key Vault + SSH keypair JSON ──────────────────────────────────────────
Write-Section 'Key Vault + SSH keypair'
$kvHash = ($SubscriptionId -replace '-','').Substring(0, 6).ToLower()
$KvName = "$Name-kv-$kvHash"
if ($KvName.Length -gt 24) { $KvName = $KvName.Substring(0, 24) }

& az keyvault show -g $Rg -n $KvName *> $null
if ($LASTEXITCODE -ne 0) {
    az keyvault create -g $Rg -n $KvName -l $Location `
        --enable-rbac-authorization false --tags $Tags | Out-Null
    Write-Ok "Created Key Vault $KvName"
} else {
    Write-Ok "Reusing Key Vault $KvName"
}
$KvUrl = "https://$KvName.vault.azure.net/"
Set-StateValue azure kv_name $KvName

$SshSecret = 'azureVM-ssh-keypair'
& az keyvault secret show --vault-name $KvName -n $SshSecret *> $null
if ($LASTEXITCODE -ne 0) {
    $kpJson = New-SshKeyPairJson
    $tmp    = [System.IO.Path]::GetTempFileName()
    try {
        Set-Content -Path $tmp -Value $kpJson -Encoding utf8 -NoNewline
        az keyvault secret set --vault-name $KvName -n $SshSecret --file $tmp | Out-Null
        Write-Ok "Stored keypair as KV secret $SshSecret"
    } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
} else {
    Write-Ok "Reusing existing keypair secret $SshSecret"
}

# ── 6. Service principal with Contributor on the RG ──────────────────────────
Write-Section 'Service principal'
$SpName    = "$Name-sp"
$StateDir  = Get-StateDir azure
$SpPath    = Join-Path $StateDir 'sp.json'
$reuse     = $false

if (Test-Path $SpPath) {
    try {
        $existing = Get-Content $SpPath -Raw | ConvertFrom-Json -ErrorAction Stop
        if ($existing.appId) { $reuse = $true }
    } catch { }
}

if ($reuse) {
    Write-Ok "Reusing service principal from $SpPath"
} else {
    $RgScope = "/subscriptions/$SubscriptionId/resourceGroups/$Rg"
    $spJson  = az ad sp create-for-rbac -n $SpName --role Contributor --scopes $RgScope --years 1 -o json
    Set-Content -Path $SpPath -Value $spJson -Encoding utf8

    # Mode 600 — best-effort on Windows; works on PowerShell on Linux/Mac.
    if ($IsLinux -or $IsMacOS) {
        & chmod 600 $SpPath 2>$null
    } else {
        # Windows ACL: remove inheritance, grant only the current user.
        $acl = Get-Acl $SpPath
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            ([System.Security.Principal.WindowsIdentity]::GetCurrent()).Name,
            'Read,Write','Allow')
        $acl.AddAccessRule($rule)
        Set-Acl $SpPath $acl
    }
    Write-Ok "Created SP $SpName (creds at $SpPath, owner-only)"

    $SpObjectId = (az ad sp list --display-name $SpName --query '[0].id' -o tsv).Trim()
    # read for runtime SSH-key fetches; set/delete so the azure_kv secrets backend
    # can vault per-VM Windows admin passwords and clean them up on teardown.
    az keyvault set-policy -n $KvName --object-id $SpObjectId --secret-permissions get list set delete | Out-Null
    Write-Ok "Granted SP get/list/set/delete on Key Vault $KvName"
}

$sp = Get-Content $SpPath -Raw | ConvertFrom-Json
$SpAppId    = $sp.appId
$SpPassword = $sp.password

# ── 6b. Image-hub container + promote-runner Azure plumbing ──────────────────
# Provisions the prerequisites the dashboard's automated cross-cloud image
# promote runner needs (see docs/image-management.md, runners/promote/README.md):
#
#   • A `hub` blob container on the storage account that doubles as both the
#     image-registry hub and the staging container the promote-runner ACI
#     writes converted VHDs to (under promote-staging/).
#   • Storage Blob Data Contributor on the storage account for the SP — the
#     SP already has Contributor on the RG (control plane), but the runner
#     does AAD-authenticated *data plane* blob writes which need this
#     dedicated role.
#   • Microsoft.ContainerInstance resource provider registered so ACI works
#     in this subscription without a first-use 5-minute provisioning wait.
Write-Section 'Image-hub container + promote-runner Azure plumbing'

az storage container-rm create -g $Rg --storage-account $SaName -n 'hub' 2>$null | Out-Null
Write-Ok "Blob container 'hub' on storage account $SaName"

$SpObjectId = (az ad sp list --display-name $SpName --query '[0].id' -o tsv).Trim()
$SaScope = "/subscriptions/$SubscriptionId/resourceGroups/$Rg/providers/Microsoft.Storage/storageAccounts/$SaName"
$existingBlobRole = (az role assignment list --assignee $SpObjectId --scope $SaScope `
    --role 'Storage Blob Data Contributor' --query '[0].id' -o tsv 2>$null).Trim()
if ($existingBlobRole) {
    Write-Ok "SP already has Storage Blob Data Contributor on $SaName"
} else {
    az role assignment create --assignee-object-id $SpObjectId `
        --assignee-principal-type ServicePrincipal `
        --role 'Storage Blob Data Contributor' --scope $SaScope | Out-Null
    Write-Ok "Granted SP Storage Blob Data Contributor on $SaName"
}

# Register the ACI provider if not already (no-op if registered). The
# promote runner launches as an ACI container group.
$AciState = (az provider show --namespace Microsoft.ContainerInstance `
    --query registrationState -o tsv 2>$null)
if (-not $AciState) { $AciState = 'NotRegistered' }
if ($AciState -ne 'Registered') {
    az provider register --namespace Microsoft.ContainerInstance --wait | Out-Null
    Write-Ok "Registered Microsoft.ContainerInstance provider"
} else {
    Write-Ok "Microsoft.ContainerInstance already registered"
}

# ── 6c. Container Registry (ACR) — mirror public images to dodge Docker Hub limits ──
# Azure rate-limits anonymous Docker Hub pulls, and every ACI runner (Shell-Jump
# Jumpoint, config-mgmt Ansible, cross-cloud promote) pulls a public image at deploy
# time. Stand up a small ACR, mirror the three images into it once (az acr import is
# server-side — no local Docker), and grant the SP pull access. The dashboard then pulls
# from ACR via the azure_acr_* / ansible_aci_acr_* keys emitted below. The ACI Jumpoint
# image stays bare (the runner prepends azure_acr_server itself) — so the VM jumpoint,
# which shares that key and docker-runs without a registry login, keeps working off
# Docker Hub. One ACR serves every region (globally pullable); re-runs reuse it. Opt out
# with SANDBOX_SKIP_ACR=1 (Basic SKU ~$5/mo).
$AcrLoginServer = ''
if (-not $env:SANDBOX_SKIP_ACR) {
    Write-Section 'Container Registry (ACR)'

    # az acr create needs the provider registered (mirrors the ContainerInstance
    # registration above; no-op if already registered).
    $AcrProvState = (az provider show --namespace Microsoft.ContainerRegistry `
        --query registrationState -o tsv 2>$null)
    if (-not $AcrProvState) { $AcrProvState = 'NotRegistered' }
    if ($AcrProvState -ne 'Registered') {
        az provider register --namespace Microsoft.ContainerRegistry --wait | Out-Null
        Write-Ok 'Registered Microsoft.ContainerRegistry provider'
    }

    # ACR names are globally unique and alphanumeric-only (5-50 chars) — mirror the storage
    # account scheme (strip hyphens + subscription hash), not the hyphenated KV name. The
    # name is region-independent, so a per-region re-run reuses the same ACR.
    $acrHash = ($SubscriptionId -replace '-','').Substring(0, 8).ToLower()
    $AcrName = (($Name -replace '-','').ToLower() + 'acr' + $acrHash)
    if ($AcrName.Length -gt 50) { $AcrName = $AcrName.Substring(0, 50) }

    & az acr show -g $Rg -n $AcrName *> $null
    if ($LASTEXITCODE -ne 0) {
        az acr create -g $Rg -n $AcrName -l $Location --sku Basic --tags $Tags | Out-Null
        Write-Ok "Created ACR $AcrName (Basic SKU)"
    } else {
        Write-Ok "Reusing ACR $AcrName"
    }
    $AcrLoginServer = (az acr show -g $Rg -n $AcrName --query loginServer -o tsv).Trim()
    $AcrId          = (az acr show -g $Rg -n $AcrName --query id -o tsv).Trim()

    # Grant the SP AcrPull so the dashboard's ACI runners can pull (idempotent).
    $existingAcrRole = (az role assignment list --assignee $SpObjectId --scope $AcrId `
        --role AcrPull --query '[0].id' -o tsv 2>$null).Trim()
    if ($existingAcrRole) {
        Write-Ok "SP already has AcrPull on $AcrName"
    } else {
        az role assignment create --assignee-object-id $SpObjectId `
            --assignee-principal-type ServicePrincipal `
            --role AcrPull --scope $AcrId | Out-Null
        Write-Ok "Granted SP AcrPull on $AcrName"
    }

    # Mirror the public images server-side. --force makes re-runs refresh :latest. Optional
    # Docker Hub creds (DOCKERHUB_USERNAME/DOCKERHUB_TOKEN) dodge the anonymous import limit.
    foreach ($img in @(
        'beyondtrust/sra-jumpoint:latest',
        'willhallonline/ansible:latest',
        'chrweav/dashboard-promote-runner:latest')) {
        if ($env:DOCKERHUB_USERNAME -and $env:DOCKERHUB_TOKEN) {
            az acr import -n $AcrName --source "docker.io/$img" --image $img `
                --username $env:DOCKERHUB_USERNAME --password $env:DOCKERHUB_TOKEN --force | Out-Null
        } else {
            az acr import -n $AcrName --source "docker.io/$img" --image $img --force | Out-Null
        }
        Write-Ok "Mirrored $img -> $AcrLoginServer/$img"
    }

    Set-StateValue azure acr_name         $AcrName
    Set-StateValue azure acr_id           $AcrId
    Set-StateValue azure acr_login_server $AcrLoginServer
} else {
    Write-Ok 'Skipping ACR (SANDBOX_SKIP_ACR set) — ACI runners will pull from Docker Hub'
}

# Promote-runner image: full ACR path when the registry exists, else the public image.
# (The promote runner uses the image verbatim — no server prepend — and authenticates via
# the azure_acr_* creds emitted below.)
$PromoteImage = if ($AcrLoginServer) { "$AcrLoginServer/chrweav/dashboard-promote-runner:latest" } else { 'chrweav/dashboard-promote-runner:latest' }

# ── 7. Print config to paste into /setup ─────────────────────────────────────
$cfg = @(
    "azure_subscription_id=$SubscriptionId",
    "azure_tenant_id=$TenantId",
    "azure_client_id=$SpAppId",
    "azure_client_secret=$SpPassword",
    "azure_resource_group=$Rg",
    "azure_location=$Location",
    "azure_vnet_resource_group=$Rg",
    "azure_aci_resource_group=$Rg",
    "azure_aci_subnet_id=$AciSubnetId                      # ACI lands here, has internet egress",
    "azure_default_subnet_id=$VmSubnetId                   # VMs land here, NSG-restricted to VNet",
    "azure_desktops_subnet_id=$DesktopsSubnetId            # VDI desktop pools (no delegation, 443 egress for the jump client)",
    "azure_db_subnet_id=$DbSubnetId                        # Flexible Server delegated subnet (private)",
    "azure_db_private_dns_zone_id=$DbDnsZoneId             # Private DNS zone for the DB FQDN",
    "azure_db_mysql_subnet_id=$DbMysqlSubnetId             # MySQL Flexible Server delegated subnet (private)",
    "azure_db_mysql_private_dns_zone_id=$DbMysqlDnsZoneId  # Private DNS zone for the MySQL DB FQDN",
    "azure_jumpoint_subnet_id=$JpSubnetId                  # Tunnel-capable VM jumpoint lands here (internet egress)",
    "azure_aci_storage_account=$SaName                      # /jpt persistent volume",
    "azure_aci_storage_account_rg=$Rg",
    "azure_aci_file_share=jpt",
    "azure_key_vault_url=$KvUrl",
    "azure_ssh_keypair_secret_name=$SshSecret               # JSON {public_key, private_key}",
    '',
    "# Per-region config set for $Location (multi-region — PR3). /api/setup/import",
    "# merges these into azure_region_configs[$Location] without clobbering other",
    "# regions, so re-running this script in a second region populates both. The",
    "# flat azure_* keys above stay as the default region for backward-compat.",
    "azure_region.${Location}.resource_group=$Rg",
    "azure_region.${Location}.vnet_resource_group=$Rg",
    "azure_region.${Location}.desktops_subnet_id=$DesktopsSubnetId",
    "azure_region.${Location}.db_subnet_id=$DbSubnetId",
    "azure_region.${Location}.db_mysql_subnet_id=$DbMysqlSubnetId",
    "azure_region.${Location}.db_private_dns_zone_id=$DbDnsZoneId",
    '',
    '# Image-registry hub + automated cross-cloud promote:',
    "storage_azure_account=$SaName                          # Image hub + promote staging",
    'storage_azure_container=hub                              # Container for hub artefacts',
    'storage_active_backend=azure_blob                        # Active asset backend',
    'storage_hub_backend=azure_blob                           # Image hub (defaults to active if unset)',
    "promote_runner_image=$PromoteImage   # ACR mirror when present (else public Docker Hub image)",
    "promote_runner_azure_resource_group=$Rg                  # ACI lands here",
    "promote_runner_azure_location=$Location",
    "promote_runner_azure_subnet_id=$AciSubnetId            # Reuses the Jumpoint ACI subnet",
    "promote_runner_azure_staging_account=$SaName            # Same account as hub by default",
    'promote_runner_azure_staging_container=hub',
    "promote_runner_azure_target_resource_group=$Rg           # Resulting managed image lands here",
    '',
    '# BeyondTrust deploy key — set in /setup or /secrets:',
    'azure_aci_docker_deploy_key=…'
)

# Azure Container Registry — point the ACI runners at the private mirror so they don't hit
# Docker Hub's anonymous pull limits. Jumpoint image stays bare (the runner prepends
# azure_acr_server); ansible takes the full ACR path (it does not prepend). Flat global
# keys (one ACR serves all regions). Skipped when SANDBOX_SKIP_ACR was set.
if ($AcrLoginServer) {
    $cfg += @(
        '',
        '# Azure Container Registry (mirrors 3 public images; dodges Docker Hub rate limits):',
        "azure_acr_server=$AcrLoginServer",
        "azure_acr_username=$SpAppId                            # SP appId (granted AcrPull above)",
        "azure_acr_password=$SpPassword",
        "ansible_aci_image=$AcrLoginServer/willhallonline/ansible:latest   # full path: the ansible runner does not prepend the server",
        "ansible_aci_acr_server=$AcrLoginServer",
        "ansible_aci_acr_username=$SpAppId",
        "ansible_aci_acr_password=$SpPassword"
    )
}
Write-DashboardConfig 'Azure sandbox configuration' $cfg
Export-ConfigJson -Cloud azure -Lines $cfg   # machine-readable twin for Onboard-Sandbox.ps1

@"
Sandbox topology summary

  VNet $VnetName (10.99.0.0/16)
    ├─ aci-subnet (10.99.1.0/24, delegated to ACI) → internet egress  [Jumpoint]
    └─ vm-subnet  (10.99.2.0/24, NSG-restricted)   → VirtualNetwork only  [user VMs]

Service principal credentials cached at:
  $SpPath  (owner-only)

To tear it down:
  .\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud azure

"@ | Write-Host
