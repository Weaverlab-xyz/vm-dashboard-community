# OCI (Oracle Cloud Infrastructure) sandbox bootstrap for the VM Dashboard
# (Windows PowerShell variant). Functional twin of setup-oci.sh.
#
# Creates a dedicated compartment + VCN (10.98.0.0/16) with public/vm/db subnets,
# Internet + NAT gateways, route tables, and a security list; plus a best-effort
# Vault + AES key + SSH-keypair secret.
#
# Operator auth: run this under whichever OCI CLI login you already use — an
# API-key profile (oci setup config) OR a browser/SSO session token
# (oci session authenticate). The script auto-detects the profile type via a
# security_token_file key and adds --auth security_token for session-token
# logins, so you never need a dedicated API user just to run it (parity with the
# AWS/Azure/GCP scripts).
#
# The dashboard does NOT reuse your operator identity. This script mints a
# dedicated IAM user (dashboard-sandbox-app) in a group with a compartment-scoped
# policy, generates an API key for it, and emits THAT key so the dashboard signs
# API calls as its own service identity. Set OCI_SKIP_DASHBOARD_USER=1 to instead
# reuse your operator API key (API-key operator login only — a session token has
# no long-lived key to hand off).
#
# Env overrides: OCI_PROFILE (default DEFAULT), OCI_COMPARTMENT_OCID (reuse an
# existing compartment), OCI_REGION, OCI_SKIP_VAULT=1, OCI_SKIP_DASHBOARD_USER=1.

[CmdletBinding()] param()
$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir 'lib/Common.ps1')

Assert-Command oci
Assert-Command ssh-keygen
Assert-Command openssl

$Name       = $Script:SandboxNamePrefix
$OciProfile = if ($env:OCI_PROFILE) { $env:OCI_PROFILE } else { 'DEFAULT' }
$ConfigFile = if ($env:OCI_CLI_CONFIG_FILE) { $env:OCI_CLI_CONFIG_FILE } else { Join-Path $HOME '.oci/config' }
$Freeform   = '{"managed-by":"dashboard-sandbox"}'

if (-not (Test-Path $ConfigFile)) {
    Write-Die "No OCI CLI config at $ConfigFile. Run: oci session authenticate  (browser/SSO) or oci setup config  (API key)."
}

# ── Read a single key from the [$OciProfile] INI section ──────────────────────
# Works for both an API-key profile (tenancy/user/fingerprint/region/key_file) and
# a session-token profile (tenancy/region/security_token_file/key_file, no
# user/fingerprint).
function Get-OciConfigValue {
    param([string]$Key)
    $inSection = $false
    foreach ($line in Get-Content $ConfigFile) {
        if ($line -match '^\s*\[(.+)\]\s*$') { $inSection = ($Matches[1] -eq $OciProfile); continue }
        if ($inSection -and $line -match "^\s*$Key\s*=\s*(.+?)\s*$") { return $Matches[1] }
    }
    return ''
}

# ── Detect operator auth mode ─────────────────────────────────────────────────
# A browser/SSO login (oci session authenticate) writes a security_token_file and
# needs --auth security_token on every call; an API-key profile (oci setup config)
# has user/fingerprint/key_file and needs no auth flag.
$AuthMode = if (Get-OciConfigValue 'security_token_file') { 'session' } else { 'apikey' }
$AuthArgs = @(); if ($AuthMode -eq 'session') { $AuthArgs = @('--auth', 'security_token') }

Assert-LoggedIn 'oci' { oci @AuthArgs iam region list --profile $OciProfile } `
    'Run: oci session authenticate  (browser/SSO) or oci setup config  (API key).'

# ── Read the operator credentials from the CLI config ─────────────────────────
$Tenancy     = if ($env:OCI_TENANCY_OCID) { $env:OCI_TENANCY_OCID } else { Get-OciConfigValue 'tenancy' }
$UserOcid    = Get-OciConfigValue 'user'
$Fingerprint = Get-OciConfigValue 'fingerprint'
$Region      = if ($env:OCI_REGION) { $env:OCI_REGION } else { Get-OciConfigValue 'region' }
$KeyFile     = Get-OciConfigValue 'key_file'
$Passphrase  = Get-OciConfigValue 'pass_phrase'
if ($KeyFile -like '~*') { $KeyFile = Join-Path $HOME $KeyFile.Substring(1).TrimStart('/','\') }

# Tenancy + region are required in both modes.
if (-not $Tenancy) { Write-Die "Could not read 'tenancy' from $ConfigFile [$OciProfile]." }
if (-not $Region)  { Write-Die "Could not read 'region' from $ConfigFile [$OciProfile]." }
# user/fingerprint/key_file exist only in an API-key profile — a session-token
# profile omits them (the dashboard user minted below supplies the real signer).
if ($AuthMode -eq 'apikey') {
    if (-not $UserOcid)    { Write-Die "Could not read 'user' from $ConfigFile [$OciProfile]." }
    if (-not $Fingerprint) { Write-Die "Could not read 'fingerprint' from $ConfigFile [$OciProfile]." }
    if (-not (Test-Path $KeyFile)) { Write-Die "API signing key file '$KeyFile' (key_file in [$OciProfile]) not found." }
}

$Oci = @('--profile', $OciProfile, '--region', $Region)
if ($AuthMode -eq 'session') { $Oci += @('--auth', 'security_token') }

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
    # Run an oci query returning a single id; normalise 'null'/'' to ''. -Base
    # overrides the default region/auth arg set (used for home-region IAM calls).
    param([string[]]$OciArgs, [string[]]$Base)
    if (-not $Base) { $Base = $Oci }
    $out = (& oci @Base @OciArgs 2>$null)
    if ($out) { $out = "$out".Trim() }
    if (-not $out -or $out -eq 'null') { return '' }
    return $out
}

# Retry a scriptblock a few times — fresh IAM users take a moment to propagate
# before api-key upload / group add-user succeed. Returns the block's output.
function Invoke-OciRetry {
    param([scriptblock]$Action, [int]$Tries = 6, [int]$DelaySec = 5)
    for ($i = 1; $i -le $Tries; $i++) {
        try { return (& $Action) } catch {
            if ($i -eq $Tries) { throw }
            Start-Sleep -Seconds $DelaySec
        }
    }
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

    # ── 1b. Dedicated dashboard IAM user + group + policy + API key ─────────────
    # Mint a service identity for the dashboard instead of reusing the operator's
    # credentials (parity with the AWS IAM user / Azure SP / GCP SA). The operator
    # login (API key OR session token) is used only to create it here; the
    # dashboard then signs its own API calls with the API key we generate for this
    # user. Skippable with OCI_SKIP_DASHBOARD_USER=1 (API-key operator login only).
    $DashboardUserName      = ''
    $DashboardUserOcid      = ''
    $DashboardFingerprint   = ''
    $DashboardPrivateKeyPem = ''
    if ($env:OCI_SKIP_DASHBOARD_USER -ne '1') {
        Write-Section 'Dashboard IAM user'

        # IAM control-plane writes must target the tenancy home region.
        $HomeRegion = Get-OciId @('iam','region-subscription','list',
            '--query',"data[?`"is-home-region`"]|[0].`"region-name`"",'--raw-output')
        if (-not $HomeRegion) { $HomeRegion = $Region }
        $OciIam = @('--profile', $OciProfile, '--region', $HomeRegion)
        if ($AuthMode -eq 'session') { $OciIam += @('--auth', 'security_token') }

        # User (tenancy-root; reuse by name).
        $DashboardUserName = "$Name-app"
        $DashboardUserOcid = Get-OciId @('iam','user','list','--compartment-id',$Tenancy,'--all',
            '--query',"data[?name=='$DashboardUserName']|[0].id",'--raw-output') -Base $OciIam
        if (-not $DashboardUserOcid) {
            $DashboardUserOcid = Get-OciId @('iam','user','create','--compartment-id',$Tenancy,
                '--name',$DashboardUserName,'--description','VM Dashboard service identity',
                '--freeform-tags',(New-OciJsonArg $Freeform),'--query','data.id','--raw-output') -Base $OciIam
            Write-Ok "Created IAM user $DashboardUserName"
        } else { Write-Ok "Reusing IAM user $DashboardUserName" }

        # Group (tenancy-root; reuse by name) + idempotent membership.
        $DashboardGroupName = "$Name-app-group"
        $DashboardGroupOcid = Get-OciId @('iam','group','list','--compartment-id',$Tenancy,'--all',
            '--query',"data[?name=='$DashboardGroupName']|[0].id",'--raw-output') -Base $OciIam
        if (-not $DashboardGroupOcid) {
            $DashboardGroupOcid = Get-OciId @('iam','group','create','--compartment-id',$Tenancy,
                '--name',$DashboardGroupName,'--description','VM Dashboard service group',
                '--freeform-tags',(New-OciJsonArg $Freeform),'--query','data.id','--raw-output') -Base $OciIam
            Write-Ok "Created IAM group $DashboardGroupName"
        } else { Write-Ok "Reusing IAM group $DashboardGroupName" }
        $member = Get-OciId @('iam','group','list-users','--group-id',$DashboardGroupOcid,'--all',
            '--query',"data[?id=='$DashboardUserOcid']|[0].id",'--raw-output') -Base $OciIam
        if (-not $member) {
            Invoke-OciRetry {
                & oci @OciIam iam group add-user --user-id $DashboardUserOcid --group-id $DashboardGroupOcid *> $null
                if ($LASTEXITCODE -ne 0) { throw 'group add-user failed' }
            }
            Write-Ok "Added $DashboardUserName to $DashboardGroupName"
        } else { Write-Ok "$DashboardUserName already in $DashboardGroupName" }

        # Policy at the tenancy root so it can reference the sandbox compartment by
        # name. Compartment-admin scope, confined to that one compartment. (The name
        # is a direct child of root; a nested compartment would need a parent:child
        # path here.)
        $DashboardPolicyName = "$Name-app-policy"
        $CompName = Get-OciId @('iam','compartment','get','--compartment-id',$Compartment,
            '--query','data.name','--raw-output') -Base $OciIam
        if (-not $CompName) { $CompName = $Name }
        $DashboardPolicyOcid = Get-OciId @('iam','policy','list','--compartment-id',$Tenancy,'--all',
            '--query',"data[?name=='$DashboardPolicyName']|[0].id",'--raw-output') -Base $OciIam
        if (-not $DashboardPolicyOcid) {
            $stmt     = "Allow group $DashboardGroupName to manage all-resources in compartment $CompName"
            $stmtJson = ConvertTo-Json -InputObject $stmt -AsArray -Compress   # ["Allow ..."]
            $DashboardPolicyOcid = Get-OciId @('iam','policy','create','--compartment-id',$Tenancy,
                '--name',$DashboardPolicyName,'--description','VM Dashboard sandbox access',
                '--statements',(New-OciJsonArg $stmtJson),'--freeform-tags',(New-OciJsonArg $Freeform),
                '--query','data.id','--raw-output') -Base $OciIam
            Write-Ok "Created IAM policy $DashboardPolicyName (manage all-resources in compartment $CompName)"
        } else { Write-Ok "Reusing IAM policy $DashboardPolicyName" }

        # API key: we generate the keypair locally, so we always hold the private
        # half (unlike AWS's server-minted secret). Reuse the cached key if it still
        # matches a live fingerprint; otherwise mint a fresh one (pruning the oldest
        # if the per-user 3-key cap is hit).
        $DashboardFingerprint   = (Get-StateValue oci dashboard_fingerprint).Trim()
        $DashboardPrivateKeyPem = Get-StateValue oci dashboard_private_key
        $keysJson = & oci @OciIam iam user api-key list --user-id $DashboardUserOcid 2>$null
        $keys = @()
        if ($keysJson) { try { $keys = @(($keysJson | ConvertFrom-Json).data) } catch { $keys = @() } }
        $haveFp = $false
        if ($DashboardFingerprint) { $haveFp = [bool]($keys | Where-Object { $_.fingerprint -eq $DashboardFingerprint }) }
        if ($DashboardFingerprint -and $DashboardPrivateKeyPem -and $haveFp) {
            Write-Ok "Reusing cached API key for $DashboardUserName (fingerprint $($DashboardFingerprint.Substring(0,[Math]::Min(11,$DashboardFingerprint.Length)))…)"
        } else {
            if ($keys.Count -ge 3) {
                $oldest = $keys | Sort-Object { $_.'time-created' } | Select-Object -First 1
                if ($oldest -and $oldest.fingerprint) {
                    & oci @OciIam iam user api-key delete --user-id $DashboardUserOcid `
                        --fingerprint $oldest.fingerprint --force *> $null
                }
            }
            $keyDir = New-Item -ItemType Directory -Path (Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid())) -Force
            try {
                $privPath = Join-Path $keyDir.FullName 'api_key.pem'
                $pubPath  = Join-Path $keyDir.FullName 'api_key_public.pem'
                & openssl genrsa -out $privPath 2048 *> $null
                & openssl rsa -pubout -in $privPath -out $pubPath *> $null
                $DashboardPrivateKeyPem = Get-Content $privPath -Raw
                $DashboardFingerprint = Invoke-OciRetry {
                    $fp = & oci @OciIam iam user api-key upload --user-id $DashboardUserOcid `
                        --key-file $pubPath --query 'data.fingerprint' --raw-output 2>$null
                    if ($LASTEXITCODE -ne 0 -or -not $fp) { throw 'api-key upload failed' }
                    "$fp".Trim()
                }
            } finally {
                Remove-Item -Recurse -Force $keyDir.FullName -ErrorAction SilentlyContinue
            }
            Set-StateValue oci dashboard_private_key $DashboardPrivateKeyPem
            Set-StateValue oci dashboard_fingerprint $DashboardFingerprint
            Write-Ok "Minted API key for $DashboardUserName (fingerprint $($DashboardFingerprint.Substring(0,[Math]::Min(11,$DashboardFingerprint.Length)))…)"
        }
        Set-StateValue oci dashboard_user   $DashboardUserOcid
        Set-StateValue oci dashboard_group  $DashboardGroupOcid
        Set-StateValue oci dashboard_policy $DashboardPolicyOcid
    } elseif ($AuthMode -eq 'session') {
        Write-Die "OCI_SKIP_DASHBOARD_USER=1 needs an API-key operator login — a session token has no long-lived key to hand the dashboard. Re-run without the flag, or use 'oci setup config'."
    } else {
        Write-Warn "OCI_SKIP_DASHBOARD_USER=1 — the dashboard will reuse your operator API key ($UserOcid)."
    }

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
            # A vault reports ACTIVE before its dedicated management endpoint (a
            # per-vault hostname, distinct from the regional control plane) starts
            # answering. The first call against it otherwise fails with "connection
            # to endpoint timed out" for a minute or two while the endpoint
            # provisions (the CLI's own retry logic makes one call hang ~90s before
            # giving up). Poll a cheap, fast-failing read (retries off, short
            # timeouts) until it responds before the key ops below.
            $MgmtReady = $false
            if ($MgmtEp) {
                Write-Info 'Waiting for the Vault management endpoint to come online…'
                try {
                    Invoke-OciRetry -Tries 20 -DelaySec 10 -Action {
                        & oci @Oci --no-retry --connection-timeout 10 --read-timeout 20 `
                            kms management key list --compartment-id $Compartment `
                            --endpoint $MgmtEp --limit 1 2>$null | Out-Null
                        if ($LASTEXITCODE -ne 0) { throw "management endpoint not ready (exit $LASTEXITCODE)" }
                    }
                    $MgmtReady = $true
                } catch { $MgmtReady = $false }
            }
            if (-not $MgmtReady) {
                Write-Warn 'Vault management endpoint not reachable yet — skipping the SSH secret. Re-run in a few minutes (the vault is reused) to finish it, or set oci_ssh_key_secret manually.'
                Set-StateValue oci vault $Vault
            } else {
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
            }
        } else {
            Write-Warn 'Vault creation failed/unavailable — skipping the SSH secret (set oci_ssh_key_secret manually, or re-run).'
        }
    } else {
        Write-Warn 'OCI_SKIP_VAULT=1 — no Vault/secret created. Deployed VMs will be keyless unless you set oci_ssh_key_secret.'
    }

    # ── 7. Print config + write config.json twin ────────────────────────────────
    # By default the dashboard uses the dedicated IAM user minted above; with
    # OCI_SKIP_DASHBOARD_USER=1 it falls back to the operator's own API key.
    if ($env:OCI_SKIP_DASHBOARD_USER -ne '1') {
        $CfgUserOcid    = $DashboardUserOcid
        $CfgFingerprint = $DashboardFingerprint
        $CfgPrivateKey  = $DashboardPrivateKeyPem
        $CfgPassphrase  = ''                        # generated key has no passphrase
        $CfgIdentity    = "dedicated IAM user $DashboardUserName (its own API key)"
    } else {
        $CfgUserOcid    = $UserOcid
        $CfgFingerprint = $Fingerprint
        $CfgPrivateKey  = (Get-Content $KeyFile -Raw)
        $CfgPassphrase  = $Passphrase
        $CfgIdentity    = "your operator API key ($UserOcid)"
    }
    $cfg = @(
        "oci_tenancy_ocid=$Tenancy",
        "oci_user_ocid=$CfgUserOcid",
        "oci_fingerprint=$CfgFingerprint",
        "oci_region=$Region",
        "oci_compartment_ocid=$Compartment",
        "oci_vcn_ocid=$Vcn",
        "oci_default_subnet_ocid=$VmSubnet                       # User VMs land here (NAT egress, no public IP)",
        "oci_private_key=…                                         # PEM injected into config.json below"
    )
    if ($CfgPassphrase) { $cfg += "oci_private_key_passphrase=$CfgPassphrase" }
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
    $obj | Add-Member -NotePropertyName 'oci_private_key' -NotePropertyValue ($CfgPrivateKey.TrimEnd("`r`n")) -Force
    ($obj | ConvertTo-Json -Compress -Depth 10) | Set-Content -LiteralPath $ociCfg -Encoding utf8 -NoNewline

    @"
Sandbox topology summary

  Compartment $Name
  VCN $Name-vcn (10.98.0.0/16)
    ├─ $Name-public-subnet (10.98.1.0/24) -> Internet Gateway  [Jumpoint]
    ├─ $Name-vm-subnet      (10.98.2.0/24) -> NAT Gateway       [user VMs]
    └─ $Name-db-subnet      (10.98.3.0/24) -> NAT (private)     [managed DBs]

The dashboard signs API calls as $CfgIdentity.
Deploy VMs into the vm-subnet; the free tier defaults to VM.Standard.E2.1.Micro.

To tear it down:
  .\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud oci

"@ | Write-Host
} finally {
    foreach ($f in $Script:OciTmpFiles) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }
}
