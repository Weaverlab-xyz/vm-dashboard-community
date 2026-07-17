<#
.SYNOPSIS
    Consolidated onboarding: provision the chosen cloud sandbox(es) and push the
    resulting config straight into the dashboard's setup API — skipping the
    /setup wizard entirely.

.DESCRIPTION
    Runs the existing per-cloud bootstrappers (which use your local aws/az/gcloud
    SSO), reads the config.json each one writes, merges them, and POSTs to
    /api/setup/import — creating the admin + marking setup complete on a fresh
    stack, or merging with admin auth if setup is already done.

.PARAMETER Cloud         aws,azure,gcp,oci or "all" (prompted if omitted).
.PARAMETER DashboardUrl  Dashboard base URL (default http://localhost:8001).
.PARAMETER AdminUser     Admin username to create/login (prompted if needed).
.PARAMETER AdminPass     Admin password (prompted, hidden, if needed).
.PARAMETER Token         Admin JWT for re-runs when setup is already complete.
.PARAMETER PushOnly      Skip provisioning; just push cached config.json files.
.PARAMETER NoPush        Provision + write config.json, but don't call the API.

.EXAMPLE
    .\scripts\sandbox\Windows\Onboard-Sandbox.ps1 -Cloud all
#>
[CmdletBinding()]
param(
    [string]$Cloud = "",
    [string]$DashboardUrl = "http://localhost:8001",
    [string]$AdminUser = "",
    [string]$AdminPass = "",
    [string]$Token = "",
    [switch]$PushOnly,
    [switch]$NoPush
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/lib/Common.ps1"
$DashboardUrl = $DashboardUrl.TrimEnd('/')

# ── Resolve cloud list ──────────────────────────────────────────────────────
if (-not $Cloud) {
    $Cloud = Read-Host "Which clouds to provision? [all] (comma list of aws,azure,gcp,oci)"
    if (-not $Cloud) { $Cloud = 'all' }
}
if ($Cloud -eq 'all') { $Cloud = 'aws,azure,gcp,oci' }
$clouds = $Cloud.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }
foreach ($c in $clouds) { if ($c -notin @('aws','azure','gcp','oci')) { Write-Die "unknown cloud: '$c' (expected aws|azure|gcp|oci|all)" } }

# ── 1. Provision (unless -PushOnly) ─────────────────────────────────────────
if (-not $PushOnly) {
    foreach ($c in $clouds) {
        Write-Section "Provisioning $c sandbox"
        $title  = (Get-Culture).TextInfo.ToTitleCase($c)
        $script = Join-Path $PSScriptRoot ("Setup-{0}Sandbox.ps1" -f $title)
        & $script
        if ($LASTEXITCODE -ne 0) { Write-Die "Setup for $c failed." }
    }
}

# ── 2. Merge each cloud's config.json ───────────────────────────────────────
$merged = [ordered]@{}
$found  = 0
foreach ($c in $clouds) {
    $f = Join-Path (Get-StateDir $c) 'config.json'
    if (Test-Path $f) {
        $found++
        $obj = Get-Content $f -Raw | ConvertFrom-Json
        foreach ($p in $obj.PSObject.Properties) { $merged[$p.Name] = $p.Value }
    } else {
        Write-Warn "no config.json for $c (skipped)"
    }
}
if ($found -eq 0) { Write-Die "No config.json found. Provision first (drop -PushOnly)." }
Write-Ok "Merged $($merged.Count) config keys from $found cloud(s)."

if ($NoPush) {
    Write-Ok "Skipping API push (-NoPush). Cached config: ~/.dashboard-sandbox/<cloud>/config.json"
    return
}

# ── 3. Push to the dashboard setup API ──────────────────────────────────────
Write-Section "Pushing config to $DashboardUrl"
try {
    $status = Invoke-RestMethod -Method Get -Uri "$DashboardUrl/api/setup/status"
} catch {
    Write-Die "Cannot reach $DashboardUrl/api/setup/status — is the dashboard running and reachable?"
}

$headers = @{}
if ($status.complete) {
    Write-Info "Dashboard is already set up — merging config (admin auth required)."
    if (-not $Token) {
        if (-not $AdminUser) { $AdminUser = Read-Host "Admin username" }
        if (-not $AdminPass) { $AdminPass = [System.Net.NetworkCredential]::new('', (Read-Host "Admin password" -AsSecureString)).Password }
        $form = "username=$([uri]::EscapeDataString($AdminUser))&password=$([uri]::EscapeDataString($AdminPass))"
        try {
            $login = Invoke-RestMethod -Method Post -Uri "$DashboardUrl/api/auth/login" -Body $form -ContentType 'application/x-www-form-urlencoded'
        } catch { Write-Die "Login failed. Check the admin credentials or pass -Token." }
        $Token = $login.access_token
        if (-not $Token) { Write-Die "Login returned no access_token." }
    }
    $headers['Authorization'] = "Bearer $Token"
    $body = @{ config = $merged } | ConvertTo-Json -Depth 10 -Compress
} else {
    Write-Info "First-run setup — creating the admin and applying config."
    if (-not $AdminUser) { $AdminUser = Read-Host "New admin username [admin]"; if (-not $AdminUser) { $AdminUser = 'admin' } }
    if (-not $AdminPass) { $AdminPass = [System.Net.NetworkCredential]::new('', (Read-Host "New admin password" -AsSecureString)).Password }
    if (-not $AdminPass) { Write-Die "Admin password is required for first-run setup." }
    $body = @{ admin_username = $AdminUser; admin_password = $AdminPass; config = $merged } | ConvertTo-Json -Depth 10 -Compress
}

try {
    $resp = Invoke-RestMethod -Method Post -Uri "$DashboardUrl/api/setup/import" -Headers $headers -ContentType 'application/json' -Body $body
} catch {
    Write-Die "Import failed: $($_.Exception.Message)"
}
Write-Ok "Config imported ($($resp.keys_written) keys written)."
Write-Ok "Done — open $DashboardUrl and log in. No wizard needed."
