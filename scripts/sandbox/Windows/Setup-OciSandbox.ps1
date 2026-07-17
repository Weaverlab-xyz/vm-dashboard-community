# OCI (Oracle Cloud Infrastructure) sandbox bootstrap for the VM Dashboard
# (Windows PowerShell variant). Functional twin of setup-oci.sh.
#
# Creates a dedicated compartment + VCN (10.98.0.0/16) with public/vm/db subnets,
# Internet + NAT gateways, route tables, and a security list; plus a best-effort
# Vault + AES key + SSH-keypair secret. Reads the dashboard's API-key credentials
# from your OCI CLI config (~/.oci/config, DEFAULT profile) and emits an oci_*
# config block + config.json twin.
#
# Env overrides: OCI_PROFILE (default DEFAULT), OCI_COMPARTMENT_OCID (reuse an
# existing compartment), OCI_REGION, OCI_SKIP_VAULT=1.

[CmdletBinding()] param()
$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir 'lib/Common.ps1')

Assert-Command oci
Assert-Command ssh-keygen

$Name       = $Script:SandboxNamePrefix
$OciProfile = if ($env:OCI_PROFILE) { $env:OCI_PROFILE } else { 'DEFAULT' }
$ConfigFile = if ($env:OCI_CLI_CONFIG_FILE) { $env:OCI_CLI_CONFIG_FILE } else { Join-Path $HOME '.oci/config' }
$Freeform   = '{"managed-by":"dashboard-sandbox"}'

if (-not (Test-Path $ConfigFile)) {
    Write-Die "No OCI CLI config at $ConfigFile. Run: oci setup config"
}

# ── Read the API-key credentials from the CLI config (requested profile) ──────
function Get-OciConfigValue {
    param([string]$Key)
    $inSection = $false
    foreach ($line in Get-Content $ConfigFile) {
        if ($line -match '^\s*\[(.+)\]\s*$') { $inSection = ($Matches[1] -eq $OciProfile); continue }
        if ($inSection -and $line -match "^\s*$Key\s*=\s*(.+?)\s*$") { return $Matches[1] }
    }
    return ''
}

$Tenancy     = if ($env:OCI_TENANCY_OCID) { $env:OCI_TENANCY_OCID } else { Get-OciConfigValue 'tenancy' }
$UserOcid    = Get-OciConfigValue 'user'
$Fingerprint = Get-OciConfigValue 'fingerprint'
$Region      = if ($env:OCI_REGION) { $env:OCI_REGION } else { Get-OciConfigValue 'region' }
$KeyFile     = Get-OciConfigValue 'key_file'
$Passphrase  = Get-OciConfigValue 'pass_phrase'
if ($KeyFile -like '~*') { $KeyFile = Join-Path $HOME $KeyFile.Substring(1).TrimStart('/','\') }

if (-not $Tenancy)     { Write-Die "Could not read 'tenancy' from $ConfigFile [$Profile]." }
if (-not $UserOcid)    { Write-Die "Could not read 'user' from $ConfigFile [$Profile]." }
if (-not $Fingerprint) { Write-Die "Could not read 'fingerprint' from $ConfigFile [$Profile]." }
if (-not $Region)      { Write-Die "Could not read 'region' from $ConfigFile [$OciProfile]." }
if (-not (Test-Path $KeyFile)) { Write-Die "API signing key file '$KeyFile' (key_file in [$Profile]) not found." }

Assert-LoggedIn 'oci' { oci iam region list --profile $OciProfile } `
    'Run: oci setup config  (and add the public key to your user under Identity -> Users -> API Keys).'

$Oci = @('--profile', $OciProfile, '--region', $Region)

# JSON-valued OCI params are passed via file:// temp files to sidestep Windows
# native-CLI quote mangling. Tracked + cleaned up at the end.
$Script:OciTmpFiles = @()
function New-OciJsonArg {
    param([string]$Json)
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("oci-" + [guid]::NewGuid().ToString() + ".json")
    Set-Content -LiteralPath $tmp -Value $Json -Encoding ascii -NoNewline
    $Script:OciTmpFiles += $tmp
    return ("file://" + ($tmp -replace '\\','/'))
}
function Get-OciId {
    # Run an oci query returning a single id; normalise 'null'/'' to ''.
    param([string[]]$OciArgs)
    $out = (& oci @Oci @OciArgs 2>$null)
    if ($out) { $out = "$out".Trim() }
    if (-not $out -or $out -eq 'null') { return '' }
    return $out
}

Write-Section "OCI sandbox in tenancy $($Tenancy.Substring(0,[Math]::Min(20,$Tenancy.Length)))…, region $Region"

try {
    # ── 1. Compartment ────────────────────────────────────────────────────────
    Write-Section 'Compartment'
    if ($env:OCI_COMPARTMENT_OCID) {
        $Compartment = $env:OCI_COMPARTMENT_OCID
        Write-Ok "Using existing compartment $Compartment"
    } else {
        $Compartment = Get-OciId @('iam','compartment','list','--compartment-id',$Tenancy,'--all',
            '--query',"data[?name=='$Name'].id | [0]",'--raw-output')
        if (-not $Compartment) {
            $Compartment = Get-OciId @('iam','compartment','create','--compartment-id',$Tenancy,
                '--name',$Name,'--description','VM Dashboard sandbox',
                '--freeform-tags',(New-OciJsonArg $Freeform),
                '--wait-for-state','ACTIVE','--query','data.id','--raw-output')
            Write-Ok "Created compartment $Name"
        } else {
            Write-Ok "Reusing compartment $Name"
        }
    }
    Set-StateValue oci compartment $Compartment

    function Find-OciResource {
        param([string]$Sub, [string]$DisplayName)
        return Get-OciId (($Sub -split ' ') + @('list','--compartment-id',$Compartment,'--all',
            '--query',"data[?`"display-name`"=='$DisplayName' && `"lifecycle-state`"!='TERMINATED'].id | [0]",'--raw-output'))
    }

    # ── 2. VCN + gateways ──────────────────────────────────────────────────────
    Write-Section 'VCN + gateways'
    $VcnName = "$Name-vcn"
    $Vcn = Find-OciResource 'network vcn' $VcnName
    if (-not $Vcn) {
        $Vcn = Get-OciId @('network','vcn','create','--compartment-id',$Compartment,
            '--cidr-blocks',(New-OciJsonArg '["10.98.0.0/16"]'),'--display-name',$VcnName,
            '--dns-label','dashsandbox','--freeform-tags',(New-OciJsonArg $Freeform),
            '--wait-for-state','AVAILABLE','--query','data.id','--raw-output')
        Write-Ok "Created VCN $VcnName (10.98.0.0/16)"
    } else { Write-Ok "Reusing VCN $VcnName" }
    Set-StateValue oci vcn $Vcn

    $IgwName = "$Name-igw"
    $Igw = Find-OciResource 'network internet-gateway' $IgwName
    if (-not $Igw) {
        $Igw = Get-OciId @('network','internet-gateway','create','--compartment-id',$Compartment,
            '--vcn-id',$Vcn,'--is-enabled','true','--display-name',$IgwName,
            '--freeform-tags',(New-OciJsonArg $Freeform),'--wait-for-state','AVAILABLE','--query','data.id','--raw-output')
        Write-Ok "Created Internet Gateway $IgwName"
    } else { Write-Ok "Reusing Internet Gateway $IgwName" }

    $NatName = "$Name-nat"
    $Nat = Find-OciResource 'network nat-gateway' $NatName
    if (-not $Nat) {
        $Nat = Get-OciId @('network','nat-gateway','create','--compartment-id',$Compartment,
            '--vcn-id',$Vcn,'--display-name',$NatName,
            '--freeform-tags',(New-OciJsonArg $Freeform),'--wait-for-state','AVAILABLE','--query','data.id','--raw-output')
        Write-Ok "Created NAT Gateway $NatName"
    } else { Write-Ok "Reusing NAT Gateway $NatName" }

    # ── 3. Route tables ──────────────────────────────────────────────────────────
    Write-Section 'Route tables'
    $PubRtName = "$Name-public-rt"
    $PubRt = Find-OciResource 'network route-table' $PubRtName
    if (-not $PubRt) {
        $PubRt = Get-OciId @('network','route-table','create','--compartment-id',$Compartment,
            '--vcn-id',$Vcn,'--display-name',$PubRtName,'--freeform-tags',(New-OciJsonArg $Freeform),
            '--route-rules',(New-OciJsonArg "[{`"destination`":`"0.0.0.0/0`",`"destinationType`":`"CIDR_BLOCK`",`"networkEntityId`":`"$Igw`"}]"),
            '--wait-for-state','AVAILABLE','--query','data.id','--raw-output')
        Write-Ok 'Created public route table (-> IGW)'
    } else { Write-Ok 'Reusing public route table' }

    $PrivRtName = "$Name-private-rt"
    $PrivRt = Find-OciResource 'network route-table' $PrivRtName
    if (-not $PrivRt) {
        $PrivRt = Get-OciId @('network','route-table','create','--compartment-id',$Compartment,
            '--vcn-id',$Vcn,'--display-name',$PrivRtName,'--freeform-tags',(New-OciJsonArg $Freeform),
            '--route-rules',(New-OciJsonArg "[{`"destination`":`"0.0.0.0/0`",`"destinationType`":`"CIDR_BLOCK`",`"networkEntityId`":`"$Nat`"}]"),
            '--wait-for-state','AVAILABLE','--query','data.id','--raw-output')
        Write-Ok 'Created private route table (-> NAT)'
    } else { Write-Ok 'Reusing private route table' }

    # ── 4. Security list ─────────────────────────────────────────────────────────
    Write-Section 'Security list'
    $SlName = "$Name-sl"
    $Sl = Find-OciResource 'network security-list' $SlName
    if (-not $Sl) {
        $ingress = '[{"source":"10.98.0.0/16","protocol":"all","isStateless":false},{"source":"10.98.1.0/24","protocol":"6","isStateless":false,"tcpOptions":{"destinationPortRange":{"min":22,"max":22}}}]'
        $egress  = '[{"destination":"0.0.0.0/0","protocol":"all","isStateless":false}]'
        $Sl = Get-OciId @('network','security-list','create','--compartment-id',$Compartment,
            '--vcn-id',$Vcn,'--display-name',$SlName,'--freeform-tags',(New-OciJsonArg $Freeform),
            '--ingress-security-rules',(New-OciJsonArg $ingress),
            '--egress-security-rules',(New-OciJsonArg $egress),
            '--wait-for-state','AVAILABLE','--query','data.id','--raw-output')
        Write-Ok 'Created security list (intra-VCN + SSH from public subnet)'
    } else { Write-Ok 'Reusing security list' }

    # ── 5. Subnets ───────────────────────────────────────────────────────────────
    Write-Section 'Subnets'
    function New-OciSubnet {
        param([string]$SubName, [string]$Cidr, [string]$Rt, [string]$Prohibit, [string]$Dns)
        $id = Find-OciResource 'network subnet' $SubName
        if (-not $id) {
            $id = Get-OciId @('network','subnet','create','--compartment-id',$Compartment,
                '--vcn-id',$Vcn,'--cidr-block',$Cidr,'--display-name',$SubName,'--dns-label',$Dns,
                '--route-table-id',$Rt,'--security-list-ids',(New-OciJsonArg "[`"$Sl`"]"),
                '--prohibit-public-ip-on-vnic',$Prohibit,'--freeform-tags',(New-OciJsonArg $Freeform),
                '--wait-for-state','AVAILABLE','--query','data.id','--raw-output')
            Write-Ok "Created subnet $SubName ($Cidr)"
        } else { Write-Ok "Reusing subnet $SubName" }
        return $id
    }
    $PubSubnet = New-OciSubnet "$Name-public-subnet" '10.98.1.0/24' $PubRt  'false' 'pub'
    $VmSubnet  = New-OciSubnet "$Name-vm-subnet"     '10.98.2.0/24' $PrivRt 'true'  'vm'
    $null      = New-OciSubnet "$Name-db-subnet"     '10.98.3.0/24' $PrivRt 'true'  'db'
    Set-StateValue oci vm_subnet $VmSubnet

    # ── 6. Vault + key + SSH-keypair secret (best-effort) ────────────────────────
    $SshSecret = ''
    $Vault = ''
    if ($env:OCI_SKIP_VAULT -ne '1') {
        Write-Section 'Vault + SSH keypair secret'
        $VaultName = "$Name-vault"
        $Vault = Get-OciId @('kms','management','vault','list','--compartment-id',$Compartment,'--all',
            '--query',"data[?`"display-name`"=='$VaultName' && `"lifecycle-state`"=='ACTIVE'].id | [0]",'--raw-output')
        if (-not $Vault) {
            Write-Info "Creating Vault $VaultName (this can take a minute or two)…"
            $Vault = Get-OciId @('kms','management','vault','create','--compartment-id',$Compartment,
                '--display-name',$VaultName,'--vault-type','DEFAULT','--freeform-tags',(New-OciJsonArg $Freeform),
                '--wait-for-state','ACTIVE','--query','data.id','--raw-output')
        }
        if ($Vault) {
            Write-Ok "Vault $VaultName ready"
            $MgmtEp = Get-OciId @('kms','management','vault','get','--vault-id',$Vault,'--query','data."management-endpoint"','--raw-output')
            $KeyOcid = Get-OciId @('kms','management','key','list','--compartment-id',$Compartment,'--endpoint',$MgmtEp,'--all',
                '--query',"data[?`"display-name`"=='$Name-key' && `"lifecycle-state`"=='ENABLED'].id | [0]",'--raw-output')
            if (-not $KeyOcid) {
                $KeyOcid = Get-OciId @('kms','management','key','create','--compartment-id',$Compartment,'--endpoint',$MgmtEp,
                    '--display-name',"$Name-key",'--key-shape',(New-OciJsonArg '{"algorithm":"AES","length":32}'),
                    '--freeform-tags',(New-OciJsonArg $Freeform),'--wait-for-state','ENABLED','--query','data.id','--raw-output')
            }
            Write-Ok 'KMS key ready'
            $SshSecretName = 'dashboard-sandbox-ssh-keypair'
            $SshSecret = Get-OciId @('vault','secret','list','--compartment-id',$Compartment,'--all',
                '--query',"data[?`"secret-name`"=='$SshSecretName' && `"lifecycle-state`"=='ACTIVE'].id | [0]",'--raw-output')
            if (-not $SshSecret) {
                $kpJson = New-SshKeyPairJson
                $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($kpJson))
                $SshSecret = Get-OciId @('vault','secret','create-base64','--compartment-id',$Compartment,
                    '--secret-name',$SshSecretName,'--vault-id',$Vault,'--key-id',$KeyOcid,
                    '--secret-content-content',$b64,'--freeform-tags',(New-OciJsonArg $Freeform),
                    '--query','data.id','--raw-output')
                Write-Ok "Created SSH keypair secret $SshSecretName"
            } else { Write-Ok "Reusing SSH keypair secret $SshSecretName" }
            Set-StateValue oci vault $Vault
            Set-StateValue oci ssh_secret $SshSecret
        } else {
            Write-Warn 'Vault creation failed/unavailable — skipping the SSH secret (set oci_ssh_key_secret manually, or re-run).'
        }
    } else {
        Write-Warn 'OCI_SKIP_VAULT=1 — no Vault/secret created. Deployed VMs will be keyless unless you set oci_ssh_key_secret.'
    }

    # ── 7. Print config + write config.json twin ────────────────────────────────
    $PrivateKeyPem = (Get-Content $KeyFile -Raw)
    $cfg = @(
        "oci_tenancy_ocid=$Tenancy",
        "oci_user_ocid=$UserOcid",
        "oci_fingerprint=$Fingerprint",
        "oci_region=$Region",
        "oci_compartment_ocid=$Compartment",
        "oci_vcn_ocid=$Vcn",
        "oci_default_subnet_ocid=$VmSubnet                       # User VMs land here (NAT egress, no public IP)",
        "oci_private_key=…                                         # PEM injected into config.json below"
    )
    if ($Passphrase) { $cfg += "oci_private_key_passphrase=$Passphrase" }
    if ($SshSecret) {
        $cfg += "oci_ssh_key_secret=$SshSecret                # Vault secret: JSON {public_key, private_key}"
        $cfg += "oci_vault_ocid=$Vault"
    } else {
        $cfg += "oci_ssh_key_secret=…   # Create a Vault secret (JSON {public_key,private_key}) and paste its OCID"
    }
    Write-DashboardConfig 'OCI sandbox configuration' $cfg
    Export-ConfigJson -Cloud oci -Lines $cfg

    # Inject the real private-key PEM into config.json (kept off the printed block).
    $ociCfg = Join-Path (Get-StateDir oci) 'config.json'
    $obj = Get-Content $ociCfg -Raw | ConvertFrom-Json
    $obj | Add-Member -NotePropertyName 'oci_private_key' -NotePropertyValue ($PrivateKeyPem.TrimEnd("`r`n")) -Force
    ($obj | ConvertTo-Json -Compress -Depth 10) | Set-Content -LiteralPath $ociCfg -Encoding utf8 -NoNewline

    @"
Sandbox topology summary

  Compartment $Name
  VCN $Name-vcn (10.98.0.0/16)
    ├─ $Name-public-subnet (10.98.1.0/24) -> Internet Gateway  [Jumpoint]
    ├─ $Name-vm-subnet      (10.98.2.0/24) -> NAT Gateway       [user VMs]
    └─ $Name-db-subnet      (10.98.3.0/24) -> NAT (private)     [managed DBs]

The dashboard signs API calls with your ~/.oci/config [$OciProfile] API key.
Deploy VMs into the vm-subnet; the free tier defaults to VM.Standard.E2.1.Micro.

To tear it down:
  .\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud oci

"@ | Write-Host
} finally {
    foreach ($f in $Script:OciTmpFiles) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }
}
