<#
.SYNOPSIS
  Create the Password Safe functional accounts the cloud-DB onboarding needs in
  "reference" mode (clouddb_ps_functional_account_mode = reference).

.DESCRIPTION
  Reference mode resolves an operator-created functional account by name (or id) and
  never creates or deletes it. This script creates the underlying cloud identity and
  then the functional accounts that carry it, for Azure, AWS, or both.

  Secrets never touch disk and are never printed. The Azure client secret and the AWS
  secret access key go straight from the CLI into a PowerShell variable and then into
  ps-cli. Nothing is written to a file and nothing is echoed.

  ONE CAVEAT: ps-cli takes the password as a command-line argument (-pwd), so it is
  briefly visible in the local process list while each account is created. That is
  ps-cli's interface, not a choice this script makes.

  NAMING, AND WHY IT MATTERS: ps_api_service.get_functional_account returns the FIRST
  account whose name matches, across EVERY platform, and the platform guard tokens
  ("ssm" for AWS, "azure"+"run command" for Azure, "pra vault") cannot discriminate
  within a plugin family. A name that resolves to a sibling platform therefore passes
  the guard, and the managed system silently inherits the WRONG platform. So:
    - Azure gets distinct names per engine (SP:psfa_pg / _mysql / _mssql), which the
      plugin allows because the suffix is just the DB login name.
    - AWS cannot: the plugin requires the account name to BE the IAM username, so all
      three are identical. Those are referenced by NUMERIC ID, which the resolver
      accepts and which cannot be ambiguous.
    - PRA Vault is referenced by id too, because the Username Password / Token /
      Private Key platforms routinely share one client id as the account name.

.PARAMETER JumpVmResourceGroup
  Azure only. The resource group the ref-counted `clouddb-jumpoint` VM lands in; it
  MUST match the Resource Group on the dashboard's Azure settings panel (config
  default is vm-cli-rg). Scope the role assignment to the RESOURCE GROUP, not the VM:
  the jump VM is deleted when idle and recreated on demand, so a VM-scoped assignment
  stops working the first time that happens.

.EXAMPLE
  .\new-clouddb-ps-functional-accounts.ps1 -Cloud both -JumpVmResourceGroup vm-cli-rg -WhatIf

.EXAMPLE
  .\new-clouddb-ps-functional-accounts.ps1 -Cloud aws
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('azure', 'aws', 'both')][string]$Cloud = 'both',
    [string]$JumpVmResourceGroup,
    [string]$AppName = 'psafe-clouddb-runcommand',
    [string]$AwsIamUserName = 'psafe-clouddb-ssm',
    [switch]$SkipPraVault,
    # Platform names as uploaded to BeyondInsight. Defaults match config.py.
    [string]$AzurePlatformNamePostgres  = 'PostgreSQL Azure Run Command Plugin',
    [string]$AzurePlatformNameMysql     = 'MySQL Azure Run Command Plugin',
    [string]$AzurePlatformNameSqlserver = 'MSSQL Azure Run Command Plugin',
    [string]$AwsPlatformNamePostgres    = 'psql SSM Custom Plugin',
    [string]$AwsPlatformNameMysql       = 'mysql SSM Custom Plugin',
    [string]$AwsPlatformNameSqlserver   = 'mssql SSM Custom Plugin',
    [string]$PraVaultPlatformName       = 'PRA Vault Username Password'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$doAzure = $Cloud -in @('azure', 'both')
$doAws   = $Cloud -in @('aws', 'both')
if ($doAzure -and -not $JumpVmResourceGroup) {
    throw '-JumpVmResourceGroup is required for Azure. Check the Resource Group on the dashboard Azure panel (config default: vm-cli-rg).'
}

# Platforms are identified by NAME and resolved to ids at runtime. Platform ids are
# per-tenant: hardcoding the ones from any single Password Safe instance would point
# this script at unrelated platforms elsewhere, and because the managed system inherits
# the functional account's platform, that fails silently rather than loudly. The names
# below are the config.py defaults (clouddb_ps_platform_* / _azure_*); override them
# with -AzurePlatformName* / -AwsPlatformName* if yours were uploaded under other names.
$AZURE_PLATFORMS = [ordered]@{
    postgres  = @{ Name = $AzurePlatformNamePostgres;  Account = 'SP:psfa_pg' }
    mysql     = @{ Name = $AzurePlatformNameMysql;     Account = 'SP:psfa_mysql' }
    sqlserver = @{ Name = $AzurePlatformNameSqlserver; Account = 'SP:psfa_mssql' }
}
$AWS_PLATFORMS = [ordered]@{
    postgres  = @{ Name = $AwsPlatformNamePostgres }
    mysql     = @{ Name = $AwsPlatformNameMysql }
    sqlserver = @{ Name = $AwsPlatformNameSqlserver }
}

# ── helpers ───────────────────────────────────────────────────────────────────

function ConvertFrom-CliJson {
    # Both CLIs write WARNING/ERROR lines to stderr, and capturing with 2>&1 folds
    # them into the same stream ahead of the payload -- `az ad app credential reset`
    # always emits a "protect these credentials" warning, so the JSON does not start
    # at position 0. Drop log-prefixed lines, then parse from the first JSON opener.
    # Keeping stderr is worth this: it is the only place a CLI failure explains itself.
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $body = ($Text -split "`r?`n" |
             Where-Object { $_ -notmatch '^\s*(WARNING|ERROR|INFO|DEBUG|CRITICAL)\b' -and
                            $_ -notmatch '^\s*\w+:\s*\[(ERROR|WARNING)\]' }) -join "`n"
    $i = $body.IndexOfAny([char[]]@('{', '['))
    if ($i -lt 0) { return $null }
    return $body.Substring($i) | ConvertFrom-Json
}

function Invoke-Az {
    # az lives in WSL on this host. Returns parsed JSON, or throws with az's stderr.
    param([Parameter(Mandatory)][string]$Arguments)
    $out = wsl bash -c "az $Arguments -o json" 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "az $Arguments failed: $out" }
    return ConvertFrom-CliJson $out
}

$script:AwsCaBundle = $null
function Invoke-Aws {
    # aws also lives in WSL, and needs the corp CA bundle: Cloudflare TLS inspection
    # otherwise fails every call with CERTIFICATE_VERIFY_FAILED, which reads as an
    # auth or network problem rather than a trust-store one.
    param([Parameter(Mandatory)][string]$Arguments)
    if (-not $script:AwsCaBundle) {
        # NOT $home: that is a read-only PowerShell automatic variable and assigning
        # to it throws "Cannot overwrite variable HOME because it is read-only".
        $wslHome = (wsl bash -c 'echo $HOME').Trim()
        $candidate = "$wslHome/.aws/corp-ca-bundle.pem"
        $exists = (wsl bash -c "test -f '$candidate' && echo yes || echo no").Trim()
        $script:AwsCaBundle = if ($exists -eq 'yes') { $candidate } else { '/etc/ssl/certs/ca-certificates.crt' }
        Write-Verbose "AWS_CA_BUNDLE=$($script:AwsCaBundle)"
    }
    $out = wsl bash -c "AWS_CA_BUNDLE='$($script:AwsCaBundle)' aws $Arguments --output json" 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "aws $Arguments failed: $out" }
    return ConvertFrom-CliJson $out
}

function New-Passphrase {
    # 32 chars from a set every engine accepts in a password literal, avoiding
    # ' " \ ` and : — the last because the account password is a colon-delimited triple.
    $alphabet = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789-_.'
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    -join ($bytes | ForEach-Object { $alphabet[$_ % $alphabet.Length] })
}

function Get-FunctionalAccounts {
    ConvertFrom-CliJson (& ps-cli --format json functional-accounts list 2>&1 | Out-String)
}

function New-FunctionalAccount {
    # Returns the new account's numeric id, so the caller can print an unambiguous
    # reference for the settings panel.
    param(
        [Parameter(Mandatory)][string]$AccountName,
        [Parameter(Mandatory)][int]$PlatformId,
        [Parameter(Mandatory)][string]$Password,
        [Parameter(Mandatory)][string]$DisplayName,
        [Parameter(Mandatory)][string]$Description
    )
    if (-not $PSCmdlet.ShouldProcess("$AccountName (platform $PlatformId)", 'create functional account')) { return $null }
    $raw = & ps-cli -y --format json functional-accounts create `
        -n $AccountName -p $PlatformId -pwd $Password `
        -d $DisplayName -desc $Description 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "ps-cli create '$AccountName' on platform $PlatformId failed: $raw" }
    $id = $null
    try { $id = (ConvertFrom-CliJson $raw).FunctionalAccountID } catch { }
    if (-not $id) {
        # Fall back to a lookup rather than guessing, so the panel value is real.
        $id = (Get-FunctionalAccounts | Where-Object { $_.PlatformID -eq $PlatformId -and $_.AccountName -eq $AccountName } |
               Select-Object -Last 1).FunctionalAccountID
    }
    Write-Host ("  created '{0}' on platform {1} -> id {2}" -f $AccountName, $PlatformId, $id) -ForegroundColor Green
    return $id
}

function Resolve-PlatformId {
    # Name -> id, matched case-insensitively. Throws with the closest candidates rather
    # than a bare "not found", because the usual cause is a plugin uploaded under a
    # slightly different name.
    param([Parameter(Mandatory)]$All, [Parameter(Mandatory)][string]$Name)
    $exact = @($All | Where-Object { $_.Name -and $_.Name.Trim() -ieq $Name.Trim() })
    if ($exact.Count -eq 1) { return [int]$exact[0].PlatformID }
    if ($exact.Count -gt 1) {
        throw ("{0} platforms are named '{1}' (ids {2}). Rename one in BeyondInsight — the managed system inherits the functional account's platform, so this cannot be resolved safely." -f
               $exact.Count, $Name, ($exact.PlatformID -join ', '))
    }
    $tokens = @($Name -split '\s+' | Where-Object { $_.Length -gt 3 })
    $near = @($All | Where-Object { $n = $_.Name; $n -and ($tokens | Where-Object { $n -imatch [regex]::Escape($_) }) })
    $hint = if ($near.Count -gt 0) { " Closest names present: " + (($near | Select-Object -First 5 | ForEach-Object { "'$($_.Name)' (id $($_.PlatformID))" }) -join ', ') + '.' } else { '' }
    throw ("no platform named '{0}' in Password Safe — upload the .psplugin first, or pass the actual name.{1}" -f $Name, $hint)
}

# ── 0. Preflight — fail before creating any cloud identity ────────────────────
Write-Host '== Preflight ==' -ForegroundColor Cyan
if (-not (Get-Command ps-cli -ErrorAction SilentlyContinue)) { throw 'ps-cli is not on PATH.' }

$allPlatforms = ConvertFrom-CliJson (& ps-cli --format json platforms list 2>&1 | Out-String)
$existingFas  = Get-FunctionalAccounts

if ($doAzure) {
    foreach ($engine in @($AZURE_PLATFORMS.Keys)) {
        $w = $AZURE_PLATFORMS[$engine]
        $w.Id = Resolve-PlatformId -All $allPlatforms -Name $w.Name
        Write-Host ("  {0,-9} -> platform {1} '{2}'" -f $engine, $w.Id, $w.Name)
        # Re-running must be safe. An account already on the RIGHT platform is reused;
        # one on a different platform is refused, because reference mode resolves by
        # name and the managed system would inherit that wrong platform silently.
        $dupes = @($existingFas | Where-Object { $_.AccountName -eq $w.Account })
        if ($dupes.Count -gt 1) {
            throw ("{0} functional accounts are named '{1}' (ids {2}). Reference mode resolves by name and would pick one arbitrarily — delete the extras." -f
                   $dupes.Count, $w.Account, ($dupes.FunctionalAccountID -join ', '))
        }
        if ($dupes.Count -eq 1) {
            if ([int]$dupes[0].PlatformID -ne $w.Id) {
                throw ("functional account '{0}' already exists on platform {1}, but this engine needs {2} '{3}'. The managed system inherits the account's platform, so reusing it would onboard databases onto the wrong plugin." -f
                       $w.Account, $dupes[0].PlatformID, $w.Id, $w.Name)
            }
            $w.Existing = [int]$dupes[0].FunctionalAccountID
            Write-Host ("             reusing existing account id {0}" -f $w.Existing) -ForegroundColor Green
        }
    }
}
if ($doAws) {
    foreach ($engine in @($AWS_PLATFORMS.Keys)) {
        $w = $AWS_PLATFORMS[$engine]
        $w.Id = Resolve-PlatformId -All $allPlatforms -Name $w.Name
        Write-Host ("  {0,-9} -> platform {1} '{2}'" -f $engine, $w.Id, $w.Name)
    }
}
$PRAVAULT_PLATFORM_ID = if ($SkipPraVault) { 0 } else { Resolve-PlatformId -All $allPlatforms -Name $PraVaultPlatformName }
Write-Host '  platforms OK' -ForegroundColor Green

$panel = [ordered]@{}

# ── 1. Azure ──────────────────────────────────────────────────────────────────
if ($doAzure) {
    Write-Host "`n== Azure service principal ==" -ForegroundColor Cyan
    $acct = Invoke-Az 'account show'
    Write-Host ("  subscription : {0} ({1})" -f $acct.name, $acct.id)
    Write-Host ("  tenant       : {0}" -f $acct.tenantId)
    # The resource-group lookup below is the real guard: if az is pointed at the wrong
    # subscription the RG will not be found, which is exactly the failure worth having
    # here rather than a role assignment that silently lands somewhere unhelpful.
    try {
        $rg = Invoke-Az "group show --name $JumpVmResourceGroup"
    } catch {
        throw ("resource group '{0}' not found in subscription '{1}'. If that is the wrong subscription: wsl az account set -s '<name>'" -f $JumpVmResourceGroup, $acct.name)
    }
    Write-Host ("  jump-VM RG   : {0} ({1})" -f $rg.name, $rg.location)

    # $appReal distinguishes "we have a usable appId" from "-WhatIf, so carry a
    # placeholder forward". Without the placeholder the dry run stops here and never
    # previews the functional accounts, which are the part worth reviewing.
    $appId = $null; $appReal = $false
    $existingApp = Invoke-Az "ad app list --display-name $AppName"
    if ($existingApp -and $existingApp.Count -gt 0) {
        $appId = $existingApp[0].appId; $appReal = $true
        Write-Host "  reusing existing app registration $AppName ($appId)"
    } elseif ($PSCmdlet.ShouldProcess($AppName, 'create Entra app registration')) {
        $appId = (Invoke-Az "ad app create --display-name $AppName").appId; $appReal = $true
        Write-Host "  created app registration $AppName ($appId)" -ForegroundColor Green
        Invoke-Az "ad sp create --id $appId" | Out-Null
        Start-Sleep -Seconds 10   # Entra propagation, so the role assignment resolves
    } else {
        $appId = '<new-app-id>'
    }

    $scope = "/subscriptions/$($acct.id)/resourceGroups/$($rg.name)"
    $clientSecret = '<client-secret>'
    if ($appReal) {
        $spObjectId = (Invoke-Az "ad sp show --id $appId").id
        if ($PSCmdlet.ShouldProcess($scope, 'assign Virtual Machine Contributor')) {
            $assigned = $false
            foreach ($attempt in 1..5) {
                try {
                    Invoke-Az ("role assignment create --assignee-object-id $spObjectId " +
                               "--assignee-principal-type ServicePrincipal " +
                               "--role 'Virtual Machine Contributor' --scope '$scope'") | Out-Null
                    $assigned = $true; break
                } catch {
                    if ($_.Exception.Message -match 'RoleAssignmentExists') { $assigned = $true; break }
                    Write-Host "  role assignment attempt $attempt failed (Entra propagation); retrying in 15s"
                    Start-Sleep -Seconds 15
                }
            }
            if (-not $assigned) { throw "could not assign Virtual Machine Contributor on $scope" }
            Write-Host "  Virtual Machine Contributor on $($rg.name)" -ForegroundColor Green
        }
        # Only issue a secret if an account will actually carry it. Otherwise a re-run
        # rotates the SP credential and silently invalidates the accounts already
        # holding the previous one — they store the secret, they do not look it up.
        $needSecret = @(@($AZURE_PLATFORMS.Keys) | Where-Object { -not $AZURE_PLATFORMS[$_].Contains('Existing') }).Count -gt 0
        if (-not $needSecret) {
            Write-Host '  all three accounts already exist — leaving the SP client secret alone'
            Write-Host '  (reissuing it would invalidate the secret they already hold).'
        }
        if ($needSecret -and $PSCmdlet.ShouldProcess($AppName, 'issue a client secret')) {
            # No --append: this app registration exists only for this purpose, so a
            # replace is self-healing. An earlier run that created a secret but failed
            # before capturing it leaves an unusable credential on the app; --append
            # would accumulate those, a plain reset invalidates them.
            $clientSecret = (Invoke-Az "ad app credential reset --id $appId --years 2").password
            if ([string]::IsNullOrWhiteSpace($clientSecret)) { throw 'az returned no client secret.' }
            Write-Host '  client secret issued (not displayed)'
        }
    } else {
        Write-Host "What if: Performing the operation `"assign Virtual Machine Contributor`" on target `"$scope`"."
        Write-Host "What if: Performing the operation `"issue a client secret`" on target `"$AppName`"."
    }

    Write-Host "`n== Functional accounts (Azure Run Command) ==" -ForegroundColor Cyan
    Write-Host '  The DB password segment is a generated throwaway: validated non-empty, never'
    Write-Host '  used by a self-rotate change, and deliberately not persisted. When you create'
    Write-Host '  the psfa_* logins, set the account password at the same time — Verify'
    Write-Host '  Functional Account is what uses it.'
    foreach ($engine in @($AZURE_PLATFORMS.Keys)) {
        $w = $AZURE_PLATFORMS[$engine]
        if ($w.Contains('Existing')) {
            Write-Host ("  '{0}' already on platform {1} (id {2}) — left alone" -f $w.Account, $w.Id, $w.Existing)
        } else {
            $dbUser = $w.Account -replace '^SP:', ''
            New-FunctionalAccount -AccountName $w.Account -PlatformId $w.Id `
                -Password ('{0}:{1}:{2}' -f $appId, $clientSecret, (New-Passphrase)) `
                -DisplayName "clouddb $engine Azure Run Command" `
                -Description "Cloud-DB onboarding (reference mode). Azure SP $AppName; DB login $dbUser." | Out-Null
        }
        $panel["Azure $engine Functional Account"] = $w.Account
    }
}

# ── 2. AWS ────────────────────────────────────────────────────────────────────
if ($doAws) {
    Write-Host "`n== AWS IAM user ==" -ForegroundColor Cyan
    $who = Invoke-Aws 'sts get-caller-identity'
    Write-Host ("  account : {0}" -f $who.Account)
    Write-Host ("  caller  : {0}" -f $who.Arn)
    if ($who.Arn -match ':root$') {
        Write-Warning '  the CLI is authenticated as the account ROOT. That works here, but replacing it with a scoped IAM principal is worth doing separately.'
    }

    $userExists = $true
    try { Invoke-Aws "iam get-user --user-name $AwsIamUserName" | Out-Null }
    catch { $userExists = $false }

    if (-not $userExists -and $PSCmdlet.ShouldProcess($AwsIamUserName, 'create IAM user')) {
        Invoke-Aws "iam create-user --user-name $AwsIamUserName" | Out-Null
        Write-Host "  created IAM user $AwsIamUserName" -ForegroundColor Green
    } elseif ($userExists) {
        Write-Host "  reusing existing IAM user $AwsIamUserName"
    }

    if ($PSCmdlet.ShouldProcess($AwsIamUserName, 'attach the clouddb-ssm inline policy')) {
        # Resource "*" deliberately: the SSM target is the ref-counted shared gateway
        # host, whose instance id changes every time it is reaped and rebuilt, so an
        # instance-scoped policy would silently stop working.
        $doc = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["ssm:SendCommand","ssm:GetCommandInvocation","ssm:ListCommandInvocations"],"Resource":"*"}]}'
        wsl bash -c "AWS_CA_BUNDLE='$($script:AwsCaBundle)' aws iam put-user-policy --user-name $AwsIamUserName --policy-name clouddb-ssm --policy-document '$doc'" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'put-user-policy failed' }
        Write-Host '  inline policy clouddb-ssm: ssm:SendCommand / GetCommandInvocation / ListCommandInvocations' -ForegroundColor Green
    }

    # Placeholders keep the dry run previewing the accounts, as on the Azure side.
    $akid = '<access-key-id>'; $awsSecret = '<secret-access-key>'
    if ($userExists) {
        $keys = (Invoke-Aws "iam list-access-keys --user-name $AwsIamUserName").AccessKeyMetadata
        if ($keys -and $keys.Count -ge 2) {
            throw "$AwsIamUserName already has 2 access keys (the AWS limit). Delete one first: aws iam delete-access-key --user-name $AwsIamUserName --access-key-id <id>"
        }
        if ($keys -and $keys.Count -eq 1) {
            Write-Warning ("  $AwsIamUserName already has access key {0}. Its secret is unrecoverable, so a new one is needed for the account password." -f $keys[0].AccessKeyId)
        }
    }
    if ($PSCmdlet.ShouldProcess($AwsIamUserName, 'create an access key')) {
        $k = (Invoke-Aws "iam create-access-key --user-name $AwsIamUserName").AccessKey
        $akid = $k.AccessKeyId; $awsSecret = $k.SecretAccessKey
        Write-Host "  access key $akid issued (secret not displayed)" -ForegroundColor Green
    }

    Write-Host "`n== Functional accounts (AWS SSM) ==" -ForegroundColor Cyan
    Write-Host '  All three share the IAM username as their account name, because the plugin'
    Write-Host '  requires that. Reference them by ID in the panel, not by name.'
    foreach ($engine in $AWS_PLATFORMS.Keys) {
        $w = $AWS_PLATFORMS[$engine]
        $id = New-FunctionalAccount -AccountName $AwsIamUserName -PlatformId $w.Id `
            -Password ('{0}:{1}' -f $akid, $awsSecret) `
            -DisplayName "clouddb $engine SSM" `
            -Description "Cloud-DB onboarding (reference mode). IAM user $AwsIamUserName for SSM SendCommand."
        $panel["AWS $engine Functional Account"] = if ($id) { "$id   (id, not the name — all three share '$AwsIamUserName')" } else { "<id assigned on create> (id, not the name)" }
    }
}

# ── 3. PRA Vault — reuse before create ────────────────────────────────────────
if (-not $SkipPraVault) {
    Write-Host "`n== Functional account (PRA Vault) ==" -ForegroundColor Cyan
    $allFas = Get-FunctionalAccounts
    $existingPv = @($allFas | Where-Object { $_.PlatformID -eq $PRAVAULT_PLATFORM_ID })
    if ($existingPv.Count -gt 0) {
        $pv = $existingPv[0]
        $dupes = @($allFas | Where-Object { $_.AccountName -eq $pv.AccountName })
        Write-Host ("  reusing existing account id {0} on platform {1}" -f $pv.FunctionalAccountID, $pv.PlatformID) -ForegroundColor Green
        if ($dupes.Count -gt 1) {
            Write-Host ("  {0} accounts share the name '{1}' (ids {2}) — hence the id." -f `
                $dupes.Count, $pv.AccountName, ($dupes.FunctionalAccountID -join ', ')) -ForegroundColor Yellow
        }
        $panel['PRA Vault Functional Account'] = "$($pv.FunctionalAccountID)   (id, not the name)"
    } else {
        Write-Host '  none found. This is your PRA Configuration-API OAuth client (Vault Account Management).'
        $praClientId = Read-Host '  PRA Config-API client ID'
        $praSecure   = Read-Host '  PRA Config-API client secret' -AsSecureString
        $praSecret   = [System.Net.NetworkCredential]::new('', $praSecure).Password
        if ([string]::IsNullOrWhiteSpace($praClientId) -or [string]::IsNullOrWhiteSpace($praSecret)) {
            Write-Warning '  blank input — skipped'
        } else {
            $id = New-FunctionalAccount -AccountName $praClientId -PlatformId $PRAVAULT_PLATFORM_ID `
                -Password $praSecret -DisplayName 'clouddb PRA Vault' `
                -Description 'Cloud-DB onboarding (reference mode): PRA Configuration-API client.'
            $panel['PRA Vault Functional Account'] = "$id"
        }
    }
}

# ── 4. What to put in the panel ───────────────────────────────────────────────
Write-Host "`n== Settings -> Integrations -> Password Safe -> Configure ==" -ForegroundColor Cyan
Write-Host '  Enable Password Safe onboarding ............ on'
Write-Host '  Functional account source .................. Reference an existing account'
Write-Host "  Rotate with the account's own credentials .. on   <-- without this, Password Safe"
Write-Host '                                                    calls the via-functional-account'
Write-Host '                                                    action and every rotation fails'
foreach ($k in $panel.Keys) { Write-Host ("  {0,-42} {1}" -f $k, $panel[$k]) }
Write-Host '  IAM / SP Client ID / Secret / Auth Mode .... leave BLANK (now in the accounts)'
Write-Host '  Platform fields ............................ leave blank (advisory in this mode)'
Write-Host '  Account Suffix ............................. local'
Write-Host ''
Write-Host '  Still required, and NOT covered by this script:' -ForegroundColor Yellow
Write-Host '   - Azure: Plugin Private Key (PEM) + passphrase. Blank SILENTLY skips the key'
Write-Host '     drop, so provisioning stays green and the first rotation fails to decrypt.'
Write-Host '   - Azure: Broker Cert Path must be where public_cert.cer really is on the broker.'
Write-Host '   - AWS: the jump-host RSA prep is manual — put private.pem and passphrase.txt in'
Write-Host '     the ssm-user home on the shared ECS gateway host yourself, and set Public Key'
Write-Host '     Path to the public key on the PS node (address field 5).'
Write-Host '   - azure_subscription_id / azure_tenant_id on the Azure panel (address fields 3'
Write-Host '     and 4, parsed positionally — a blank one yields an unresolvable empty field).'
Write-Host ''
Write-Host '  Then reopen the panel and confirm the values read back: config is Fernet-keyed off'
Write-Host '  JWT_SECRET_KEY, so a key mismatch reads as BLANKS rather than an error.'
