# Shared helpers for the dashboard sandbox bootstrappers (PowerShell variant).
# Dot-source from each Setup-*.ps1 and Rollback-Sandbox.ps1.

$ErrorActionPreference = 'Stop'

# ── Tagging convention ─────────────────────────────────────────────────────────
$Script:SandboxTagKey     = 'managed-by'
$Script:SandboxTagValue   = 'dashboard-sandbox'
$Script:SandboxNamePrefix = 'dashboard-sandbox'

# ── Logging ────────────────────────────────────────────────────────────────────
function _Now { (Get-Date).ToUniversalTime().ToString('HH:mm:ss') }

function Write-Info    { param([string]$Message) Write-Host "[$(_Now)] $Message"      -ForegroundColor Cyan }
function Write-Ok      { param([string]$Message) Write-Host "[$(_Now)] $([char]0x2713) $Message" -ForegroundColor Green }
function Write-Warn    { param([string]$Message) Write-Host "[$(_Now)] ! $Message"    -ForegroundColor Yellow }
function Write-Err     { param([string]$Message) Write-Host "[$(_Now)] $([char]0x2717) $Message" -ForegroundColor Red }
function Write-Section { param([string]$Title)   Write-Host ""; Write-Host "── $Title" -ForegroundColor Magenta }

function Write-Die {
    param([string]$Message)
    Write-Err $Message
    exit 1
}

# ── Prereq checks ──────────────────────────────────────────────────────────────
function Assert-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Write-Die "$Name not found on PATH. Run scripts/sandbox/Test-SandboxPrereqs.ps1 first."
    }
}

# Confirms a CLI is authenticated. Pass a probe scriptblock and a hint string.
function Assert-LoggedIn {
    param(
        [string]      $CliName,
        [scriptblock] $Probe,
        [string]      $Hint
    )
    try { & $Probe *> $null } catch {
        Write-Die "$CliName is installed but not authenticated. $Hint"
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Die "$CliName is installed but not authenticated. $Hint"
    }
}

# ── Output: dashboard config block ─────────────────────────────────────────────
# At the end of each Setup-*.ps1 script, print a block of key=value pairs for
# the user to paste into the /setup wizard or Settings → Integrations panels.
function Write-DashboardConfig {
    param(
        [string]   $Title,
        [string[]] $Lines
    )
    $bar = '═' * 63
    Write-Host ""
    Write-Host $bar                    -ForegroundColor Green
    Write-Host "  $Title — paste into /setup or Settings → Integrations" -ForegroundColor Green
    Write-Host $bar                    -ForegroundColor Green
    Write-Host ""
    foreach ($line in $Lines) { Write-Host $line }
    Write-Host ""
}

# Machine-readable twin of Write-DashboardConfig: write the same key=value pairs
# to (Get-StateDir <cloud>)/config.json so Onboard-Sandbox.ps1 can merge them
# and POST to /api/setup/import. Splits each line on the FIRST '='.
function Export-ConfigJson {
    param(
        [string]   $Cloud,
        [string[]] $Lines
    )
    $obj = [ordered]@{}
    foreach ($line in $Lines) {
        $idx = $line.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $line.Substring(0, $idx).Trim()
        $val = $line.Substring($idx + 1)
        $val = [regex]::Replace($val, '\s+#.*$', '').Trim()   # strip trailing "  # comment"
        if (-not $key) { continue }
        if ($val -eq [char]0x2026) { continue }                # skip "…" paste-manually placeholders
        $obj[$key] = $val
    }
    $path = Join-Path (Get-StateDir $Cloud) 'config.json'
    ($obj | ConvertTo-Json -Compress -Depth 5) | Set-Content -LiteralPath $path -Encoding utf8 -NoNewline
    Write-Info "Wrote $path ($($obj.Count) keys)"
}

# ── State directory (optional cache) ───────────────────────────────────────────
# Tag-based rollback is the source of truth, but we also drop a state directory
# as a hint to users who want to know what was created.
function Get-StateDir {
    param([string]$Cloud)
    $base = if ($env:SANDBOX_STATE_DIR) { $env:SANDBOX_STATE_DIR } else { Join-Path $HOME '.dashboard-sandbox' }
    $dir  = Join-Path $base $Cloud
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    return $dir
}

function Set-StateValue {
    param([string]$Cloud, [string]$Key, [string]$Value)
    $path = Join-Path (Get-StateDir $Cloud) $Key
    Set-Content -Path $path -Value $Value -Encoding utf8 -NoNewline
}

function Get-StateValue {
    param([string]$Cloud, [string]$Key)
    $path = Join-Path (Get-StateDir $Cloud) $Key
    if (Test-Path $path) { Get-Content $path -Raw } else { '' }
}

function Clear-StateDir {
    param([string]$Cloud)
    $base = if ($env:SANDBOX_STATE_DIR) { $env:SANDBOX_STATE_DIR } else { Join-Path $HOME '.dashboard-sandbox' }
    $dir  = Join-Path $base $Cloud
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
}

# ── Confirm prompt (for destructive ops) ───────────────────────────────────────
function Confirm-Action {
    param([string]$Prompt)
    $reply = Read-Host "$Prompt [y/N]"
    return $reply -match '^[Yy]$'
}

# ── ssh-keygen wrapper that produces a JSON keypair envelope ──────────────────
# Both PowerShell on Windows and Linux ship with ssh-keygen; we use the OpenSSH
# binary that's already on PATH after `Add-WindowsCapability -Name OpenSSH.Client`.
function New-SshKeyPairJson {
    param([string]$Comment = 'dashboard-sandbox')
    $tmp = New-Item -ItemType Directory -Path (Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid())) -Force
    try {
        $keyPath = Join-Path $tmp.FullName 'key'
        & ssh-keygen -t rsa -b 4096 -N '""' -C $Comment -f $keyPath *> $null
        if ($LASTEXITCODE -ne 0) { throw "ssh-keygen failed (exit $LASTEXITCODE). Install OpenSSH client." }
        $pub  = Get-Content "$keyPath.pub" -Raw
        $priv = Get-Content $keyPath       -Raw
        # PowerShell's ConvertTo-Json escapes newlines for us; the resulting
        # string is what cloud secret stores want.
        return [pscustomobject]@{
            public_key  = $pub.TrimEnd("`r`n")
            private_key = $priv -replace "`r`n","`n"
        } | ConvertTo-Json -Compress
    } finally {
        Remove-Item -Recurse -Force $tmp.FullName -ErrorAction SilentlyContinue
    }
}
