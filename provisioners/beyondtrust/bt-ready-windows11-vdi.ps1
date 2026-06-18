<#
  bt-ready-windows11-vdi.ps1 — prepare a Windows 11 multi-session (AVD SKU) image
  for BeyondTrust PRA VDI desktops. The VDI analogue of bt-ready-windows.ps1
  (Server Core).

  Runs as the Packer powershell provisioner on the dashboard's Azure Windows 11
  build (Build Image tab -> "Windows 11 multi-session (24H2 AVD)", which publishes
  a Trusted Launch Compute Gallery image), BEFORE the template's windows-restart
  + Sysprep /generalize finisher.

  What it does: enables multi-session RDP, turns on RDP + NLA + firewall for
  agentless PRA Remote RDP, applies a conservative set of VDI optimizations, and
  (optionally) stages the BeyondTrust Remote Support jump client to install at
  FIRST BOOT on each clone — never baked installed, so every VDI VM registers a
  distinct jump client (cloning one installed client produces a "confused entry
  in the rep console"; see KB0017470).

  Access model: PRA agentless Remote RDP jump items reach 3389 from the Jumpoint
  subnet; pool VMs are private + brokered. (OpenSSH is optional — RDP is primary
  for a desktop.)

  Operator-overridable. Edit the inline defaults below, OR set the matching
  $env:BT_* before the build. NOTE: the dashboard's powershell provisioner does
  not forward environment_vars yet, so when driving this through the GUI the
  INLINE values are the working path today.
    $JumpClientUrl  / $env:BT_JUMP_CLIENT_URL   URL to the RS mass-deployment installer (.msi). Blank = skip jump-client staging.
    $JumpGroup      / $env:BT_JUMP_GROUP        jc_jump_group code name; blank = installer default
    $JumpTag        / $env:BT_JUMP_TAG          jc_tag; blank = none
    $InstallOpenSsh / $env:BT_INSTALL_OPENSSH=1 also install OpenSSH Server (admin SSH; default off)
    $AuthorizedKey  / $env:BT_AUTHORIZED_KEY    SSH public key (only when OpenSSH on) -> administrators_authorized_keys

  See provisioners/beyondtrust/README.md.
#>

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Log([string] $m) { Write-Output "[bt-ready] $m" }

# -- Config (inline defaults; $env:BT_* overrides win when present) -----------
$JumpClientUrl  = if ($env:BT_JUMP_CLIENT_URL) { $env:BT_JUMP_CLIENT_URL } else { '' }
$JumpGroup      = if ($env:BT_JUMP_GROUP)      { $env:BT_JUMP_GROUP }      else { '' }
$JumpTag        = if ($env:BT_JUMP_TAG)        { $env:BT_JUMP_TAG }        else { '' }
$InstallOpenSsh = ($env:BT_INSTALL_OPENSSH -eq '1')
$AuthorizedKey  = if ($env:BT_AUTHORIZED_KEY)  { $env:BT_AUTHORIZED_KEY }  else { '' }

$TS = 'HKLM:\System\CurrentControlSet\Control\Terminal Server'

Log "starting Windows 11 multi-session VDI prep on $([System.Environment]::OSVersion.VersionString)"

# -- 1. Multi-session: allow concurrent RDP sessions --------------------------
# fSingleSessionPerUser=0 lets a user hold concurrent sessions; the Win 11
# multi-session (AVD) SKU itself permits multiple distinct users (no RDSH role).
Log 'enabling multi-session (fSingleSessionPerUser=0)'
Set-ItemProperty -Path $TS -Name fSingleSessionPerUser -Value 0 -Type DWord

# -- 2. RDP + NLA + firewall (primary VDI access via PRA agentless Remote RDP) -
Log 'enabling RDP + NLA + firewall + time-zone redirection'
Set-ItemProperty -Path $TS -Name fDenyTSConnections -Value 0 -Type DWord
Set-ItemProperty -Path "$TS\WinStations\RDP-Tcp" -Name UserAuthentication -Value 1 -Type DWord
Set-ItemProperty -Path $TS -Name fEnableTimeZoneRedirection -Value 1 -Type DWord
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'

# -- 3. Conservative VDI optimizations ----------------------------------------
# A light, safe subset. For heavier tuning use Microsoft's Virtual Desktop
# Optimization Tool (VDOT):
#   https://github.com/The-Virtual-Desktop-Team/Virtual-Desktop-Optimization-Tool
Log 'applying conservative VDI optimizations'
powercfg /h off 2>$null   # no hibernation on a cloud VDI VM
$dc = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection'
New-Item -Path $dc -Force | Out-Null
Set-ItemProperty -Path $dc -Name AllowTelemetry -Value 0 -Type DWord
$cc = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent'
New-Item -Path $cc -Force | Out-Null
Set-ItemProperty -Path $cc -Name DisableWindowsConsumerFeatures -Value 1 -Type DWord
$ws = 'HKLM:\SOFTWARE\Policies\Microsoft\WindowsStore'
New-Item -Path $ws -Force | Out-Null
Set-ItemProperty -Path $ws -Name AutoDownload -Value 2 -Type DWord

# -- 4. RS jump client — stage + install at FIRST BOOT (per clone) ------------
# Never bake an *installed* jump client into the image — clones would all phone
# home with the same identity. Stage the mass-deployment installer now, and run
# it once per clone via SetupComplete.cmd (runs as SYSTEM after Sysprep OOBE,
# before logon), so each VDI VM registers a distinct jump client.
if ($JumpClientUrl.Trim()) {
    $btDir = Join-Path $env:ProgramData 'bt'
    $msi   = Join-Path $btDir 'sra-scc.msi'
    Log "staging RS jump-client installer -> $msi"
    New-Item -ItemType Directory -Path $btDir -Force | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest $JumpClientUrl.Trim() -OutFile $msi -UseBasicParsing

    $msiArgs = "/i `"$msi`" /quiet"
    if ($JumpGroup.Trim()) { $msiArgs += " jc_jump_group=jumpgroup:$($JumpGroup.Trim())" }
    if ($JumpTag.Trim())   { $msiArgs += " jc_tag=$($JumpTag.Trim())" }

    $scriptsDir = Join-Path $env:SystemRoot 'Setup\Scripts'
    New-Item -ItemType Directory -Path $scriptsDir -Force | Out-Null
    # SetupComplete.cmd runs once, as SYSTEM, after specialize/OOBE on each clone.
    # ASCII-only, CRLF — it's a cmd batch file.
    $setupComplete = @(
        '@echo off',
        'rem Managed by bt-ready - installs the RS jump client once per clone on first boot.',
        "msiexec $msiArgs /log `"%ProgramData%\bt\jc-install.log`""
    ) -join "`r`n"
    Set-Content -Path (Join-Path $scriptsDir 'SetupComplete.cmd') -Value $setupComplete -Encoding ascii
    Log 'RS jump client will install at first boot (SetupComplete.cmd). NOTE: mass-deploy installers EXPIRE and are invalidated by appliance upgrades — rebuild the image after appliance updates; the deployed (private) VM needs outbound 443 to the appliance to register.'
} else {
    Log 'no $JumpClientUrl set — skipping jump-client staging (deploy it post-provision via the Mass Deployment Wizard, or set BT_JUMP_CLIENT_URL).'
}

# -- 5. Optional OpenSSH (admin access; RDP is primary for a desktop) ---------
if ($InstallOpenSsh) {
    Log 'installing OpenSSH Server (optional admin access)'
    try {
        $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
        if ($null -ne $cap -and $cap.State -ne 'Installed') { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
    } catch { Log "warn: OpenSSH install failed: $($_.Exception.Message)" }
    Set-Service -Name sshd -StartupType Automatic
    Set-Service -Name ssh-agent -StartupType Automatic
    Start-Service sshd
    if (-not (Get-NetFirewallRule -Name 'sshd' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' -Enabled True `
            -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    }
    New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
    New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
        -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -PropertyType String -Force | Out-Null
    if ($AuthorizedKey.Trim()) {
        $akFile = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
        [IO.File]::WriteAllText($akFile, $AuthorizedKey.Trim() + "`n", (New-Object Text.ASCIIEncoding))
        icacls $akFile /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "icacls failed on $akFile (exit $LASTEXITCODE)" }
        Log 'SSH key authorized'
    }
}

# -- 6. Done ------------------------------------------------------------------
# No host-key / SID / log cleanup: the build template runs windows-restart then
# Sysprep /generalize next, which owns Windows image generalization.
Log 'bt-ready-windows11-vdi complete — windows-restart + Sysprep generalize run next'
