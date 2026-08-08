<#
.SYNOPSIS
    Move dashboard Settings configuration from one instance to another — every
    key in the encrypted app_config store plus the notification endpoints edited
    in Settings → Notifications.

.DESCRIPTION
    A pg_dump of app_config does NOT work: values are Fernet-encrypted with a key
    derived from JWT_SECRET_KEY, so a restore into a second instance yields
    ciphertext it cannot read — and _decrypt swallows the failure, so the app
    serves the ciphertext with no error and no log line. This moves plaintext and
    lets the target re-encrypt, through POST /api/setup/import.

    Windows twin of scripts/migrate-config.sh. Both are thin launchers over
    `python -m web_dashboard.scripts.config_migrate`, so there is one
    implementation and no behaviour to drift between them.

.PARAMETER Command       export | diff | import | export-local
.PARAMETER Arguments     Remaining arguments, passed through verbatim.

.EXAMPLE
    .\scripts\Migrate-Config.ps1 export -Source http://localhost:8001

.EXAMPLE
    .\scripts\Migrate-Config.ps1 diff --bundle $env:LOCALAPPDATA\dashboard-migrate\bundle.json --target https://dash.example.com

.NOTES
    `--via docker` needs a Docker CLI. Docker Desktop's WSL backend often leaves
    `docker` off the Windows PATH; when that is the case this script re-runs the
    command inside WSL rather than failing. Everything else runs natively.

    The bundle holds live credentials and Windows has no POSIX file mode, so on
    this platform it defaults under $env:LOCALAPPDATA rather than the profile
    root. Keep it out of OneDrive-synced folders and delete it after cutover.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('export', 'diff', 'import', 'export-local')]
    [string]$Command,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments = @()
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

$RepoRoot = Split-Path -Parent $PSScriptRoot

function Write-Die { param([string]$Message) Write-Host "$([char]0x2717) $Message" -ForegroundColor Red; exit 1 }

# ── Locate Python ────────────────────────────────────────────────────────────
$Py = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) { $Py = $cmd.Source; break }
}
if (-not $Py) { Write-Die "Python not found on PATH. Install Python 3, or run scripts/migrate-config.sh from WSL." }

# ── --via docker needs a Docker CLI; fall back to WSL when there isn't one ────
$viaDocker = ($Arguments -contains '--via') -and
             ($Arguments[[array]::IndexOf($Arguments, '--via') + 1] -eq 'docker')

if ($viaDocker -and -not (Get-Command docker -ErrorAction SilentlyContinue)) {
    if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
        Write-Die "--via docker needs the Docker CLI, and neither docker nor wsl is on PATH. Use --via http instead."
    }
    Write-Host "  docker is not on the Windows PATH - running this command inside WSL." -ForegroundColor Cyan
    $wslRoot = (& wsl wslpath -a "$RepoRoot").Trim()
    $inner = @('./scripts/migrate-config.sh', $Command) + $Arguments
    & wsl --cd $wslRoot -- bash -lc ($inner -join ' ')
    exit $LASTEXITCODE
}

# The tool is stdlib-only, so no virtualenv is needed - but it imports
# web_dashboard.services.region_config for the region field names, so the repo
# root has to be importable.
Push-Location $RepoRoot
try {
    & $Py -m web_dashboard.scripts.config_migrate $Command @Arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
