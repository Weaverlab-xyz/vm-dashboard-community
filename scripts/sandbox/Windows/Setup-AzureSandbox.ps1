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
$NsgName   = "$Name-vm-nsg"

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

$AciSubnetId = (az network vnet subnet show -g $Rg --vnet-name $VnetName -n $AciSubnet --query id -o tsv).Trim()
$VmSubnetId  = (az network vnet subnet show -g $Rg --vnet-name $VnetName -n $VmSubnet  --query id -o tsv).Trim()
Set-StateValue azure aci_subnet_id $AciSubnetId
Set-StateValue azure vm_subnet_id  $VmSubnetId

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
    az keyvault set-policy -n $KvName --object-id $SpObjectId --secret-permissions get list | Out-Null
    Write-Ok "Granted SP read on Key Vault $KvName"
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

# ── 7. Print config to paste into /setup ─────────────────────────────────────
Write-DashboardConfig 'Azure sandbox configuration' @(
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
    "azure_aci_storage_account=$SaName                      # /jpt persistent volume",
    "azure_aci_storage_account_rg=$Rg",
    "azure_aci_file_share=jpt",
    "azure_key_vault_url=$KvUrl",
    "azure_ssh_keypair_secret_name=$SshSecret               # JSON {public_key, private_key}",
    '',
    '# Image-registry hub + automated cross-cloud promote:',
    "storage_azure_account=$SaName                          # Image hub + promote staging",
    'storage_azure_container=hub                              # Container for hub artefacts',
    'storage_active_backend=azure_blob                        # Active asset backend',
    'storage_hub_backend=azure_blob                           # Image hub (defaults to active if unset)',
    'promote_runner_image=weaverlab-xyz/dashboard-promote-runner:latest   # Build + push to your ACR until public tag exists',
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
