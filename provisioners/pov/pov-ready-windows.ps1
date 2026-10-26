<#
  pov-ready-windows.ps1 — the POV half of the Standard Windows image (Server 2022).

  Runs as a Packer PowerShell provisioner AFTER bt-ready-windows.ps1 (OpenSSH, RDP+NLA)
  and before the template's windows-restart and sysprep finisher (EC2Launch's on AWS,
  Sysprep.exe /generalize on Azure).

  What it adds, and why each one is in the IMAGE:
    * WinRM on 5985 reachable from the POV network. The Resource Broker install, the
      functional account and every guest step arrive as an Ansible run over WinRM
      (NTLM, HTTP 5985) from the control node — and AWS POV targets get no user-data,
      so nothing could turn it on after launch. Stock Server 2022 has the listener but
      its firewall rule admits only the local subnet on the Public profile, which a
      control node in another subnet (a Purdue-zoned POV) is not.
    * LocalAccountTokenFilterPolicy = 1. Without it a LOCAL administrator other than the
      built-in one (the POV's functional account) gets a filtered token over WinRM and
      every admin task fails "Access is denied".
    * A boot task that re-asserts all of that and removes what the BUILD needed: Packer's
      HTTPS listener on its self-signed cert ("CN=packer" on AWS; on Azure the builder
      names it after its temporary VM, "CN=pkrvm..."), Basic auth, and its firewall rule. It only
      acts once C:\BT\image-sealed exists (written by the finisher), so it cannot pull
      the build's own connection out from under it across the windows-restart.
    * C:\BT\staged\, and optionally one installer downloaded into it (POV_STAGE_URL) —
      staged, never run. The hook for EPM for Windows and anything like it.

  Env (set by services/pov_image_recipes):
    POV_STAGE_URL   presigned URL of an installer to stage (optional)
    POV_STAGE_NAME  its file name
#>

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Log([string] $m) { Write-Output "[pov-ready] $m" }

$BtDir     = 'C:\BT'
$StagedDir = Join-Path $BtDir 'staged'
New-Item -ItemType Directory -Force -Path $StagedDir | Out-Null

# -- 1. Remote admin for local accounts over WinRM ---------------------------
Log 'setting LocalAccountTokenFilterPolicy=1'
New-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' `
    -Name LocalAccountTokenFilterPolicy -Value 1 -PropertyType DWord -Force | Out-Null

# -- 2. The boot-time WinRM task ---------------------------------------------
$BootScript = Join-Path $BtDir 'pov-winrm.ps1'
@'
# Written by pov-ready-windows.ps1. Runs at every boot as SYSTEM; idempotent.
$ErrorActionPreference = 'Continue'
if (-not (Test-Path 'C:\BT\image-sealed')) { return }   # still the build instance
Set-Service WinRM -StartupType Automatic
Start-Service WinRM
# The HTTP listener (5985). quickconfig creates it if it is missing and leaves it alone if not.
& winrm quickconfig -quiet | Out-Null
# What the build added: its HTTPS listener and cert, Basic auth, its firewall rule.
Get-ChildItem WSMan:\Localhost\Listener | Where-Object { $_.Keys -contains 'Transport=HTTPS' } | ForEach-Object {
    $thumb = (Get-ChildItem $_.PSPath | Where-Object Name -eq 'CertificateThumbprint').Value
    $cert = Get-ChildItem Cert:\LocalMachine\My | Where-Object { $_.Thumbprint -eq $thumb }
    if ($cert -and ($cert.Subject -eq 'CN=packer' -or $cert.Subject -like 'CN=pkrvm*')) {
        Remove-Item $_.PSPath -Recurse -Force
        Remove-Item $cert.PSPath -Force
    }
}
Set-Item WSMan:\localhost\Service\Auth\Basic -Value $false
Set-Item WSMan:\localhost\Service\AllowUnencrypted -Value $false
Remove-NetFirewallRule -Name 'packer-winrm-https' -ErrorAction SilentlyContinue
# WinRM from the POV network on every profile. Private ranges only: a POV's security
# group already confines it to the POV network, and this keeps a mis-scoped SG from
# exposing WinRM to the internet.
if (-not (Get-NetFirewallRule -Name 'pov-winrm' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name 'pov-winrm' -DisplayName 'POV WinRM (5985/5986) from private ranges' `
        -Direction Inbound -Protocol TCP -LocalPort 5985,5986 -Action Allow -Profile Any `
        -RemoteAddress 10.0.0.0/8,172.16.0.0/12,192.168.0.0/16 | Out-Null
}
'@ | Set-Content -Path $BootScript -Encoding UTF8

Log "registering the boot task for $BootScript"
$action    = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$BootScript`""
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName 'BT-POV-WinRM' -Action $action -Trigger $trigger `
    -Principal $principal -Force | Out-Null

# -- 3. Optional: stage one installer (never run) -----------------------------
if ($env:POV_STAGE_URL) {
    $name = if ($env:POV_STAGE_NAME) { Split-Path -Leaf $env:POV_STAGE_NAME } else { 'installer' }
    $dest = Join-Path $StagedDir $name
    Log "staging $name into $StagedDir"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $env:POV_STAGE_URL -OutFile $dest -UseBasicParsing
    Log "staged $dest ($((Get-Item $dest).Length) bytes) — not run"
} else {
    Log 'no installer to stage'
}

Log 'pov-ready-windows complete — windows-restart + the sysprep finisher run next'
