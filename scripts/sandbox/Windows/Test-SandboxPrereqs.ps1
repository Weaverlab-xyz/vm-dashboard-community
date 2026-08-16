# Sandbox bootstrappers prereq check (Windows PowerShell variant).
# Verifies docker, docker-compose-v2, aws, az, gcloud, oci, jq, ssh-keygen are on PATH.
# Prints install hints (winget) for anything missing.

[CmdletBinding()] param()
$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir 'lib/Common.ps1')

Write-Section 'Checking prerequisites'

# winget package IDs (or vendor URL) for each tool.
$WingetHints = [ordered]@{
    docker       = 'winget install -e --id Docker.DockerDesktop'
    aws          = 'winget install -e --id Amazon.AWSCLI'
    az           = 'winget install -e --id Microsoft.AzureCLI'
    gcloud       = 'winget install -e --id Google.CloudSDK'
    oci          = 'winget install -e --id Oracle.OCI-CLI'
    jq           = 'winget install -e --id jqlang.jq'
    'ssh-keygen' = 'Settings → Apps → Optional Features → Add OpenSSH Client'
}

$Checks  = @('docker', 'aws', 'az', 'gcloud', 'oci', 'jq', 'ssh-keygen')
$Missing = @()

foreach ($cmd in $Checks) {
    $exe = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($exe) {
        $version = ''
        try {
            switch ($cmd) {
                'aws'        { $version = (& aws --version 2>&1   | Select-Object -First 1) }
                'az'         { $version = (& az  --version 2>&1   | Select-Object -First 1) }
                'gcloud'     { $version = (& gcloud --version 2>&1| Select-Object -First 1) }
                'oci'        { $version = (& oci --version 2>&1    | Select-Object -First 1) }
                'docker'     { $version = (& docker --version 2>&1) }
                'jq'         { $version = (& jq --version 2>&1) }
                'ssh-keygen' { $version = (& ssh -V 2>&1) }
            }
        } catch { $version = '(version probe failed)' }
        Write-Ok ("{0,-12} {1}" -f $cmd, $version)
    } else {
        Write-Warn "$cmd is not installed."
        $Missing += $cmd
    }
}

# docker-compose v2 is a docker subcommand, not a separate binary.
$composeOk = $false
try {
    & docker compose version *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "docker compose — $(& docker compose version --short 2>$null)"
        $composeOk = $true
    }
} catch {}
if (-not $composeOk) {
    Write-Warn 'docker-compose v2 not available (run: docker compose version)'
    $Missing += 'docker-compose'
}

# Confirm Docker is reachable from the current shell.
if (Get-Command docker -ErrorAction SilentlyContinue) {
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Warn 'docker is installed but the daemon is not reachable. Start Docker Desktop and wait for the whale icon to settle.'
    }
}

if ($Missing.Count -gt 0) {
    Write-Section 'Install missing prereqs'
    foreach ($m in $Missing) {
        $hint = $WingetHints[$m]
        if (-not $hint) { $hint = '(no install hint; consult docs)' }
        Write-Host ("  {0,-12} → {1}" -f $m, $hint) -ForegroundColor Yellow
    }
    Write-Host ''
    exit 1
}

Write-Section 'All prerequisites satisfied'
Write-Ok 'Ready to run Setup-AwsSandbox.ps1 / Setup-AzureSandbox.ps1 / Setup-GcpSandbox.ps1 / Setup-OciSandbox.ps1'

@'

Next steps — authenticate each CLI you plan to use:

  AWS:    aws configure                       (or: aws sso login)
  Azure:  az login
  GCP:    gcloud auth login
          gcloud auth application-default login
  OCI:    oci setup config

Then:

  .\scripts\sandbox\Windows\Setup-AwsSandbox.ps1
  .\scripts\sandbox\Windows\Setup-AzureSandbox.ps1
  .\scripts\sandbox\Windows\Setup-GcpSandbox.ps1
  .\scripts\sandbox\Windows\Setup-OciSandbox.ps1

To tear it all down:

  .\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud all

'@ | Write-Host
