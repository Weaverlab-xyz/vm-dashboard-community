<#
  bt-ready-windows.ps1 — prepare a Windows Server (incl. Server Core) cloud image
  for BeyondTrust PRA access. The Windows analogue of bt-ready-debian.sh /
  bt-ready-rpm.sh.

  Runs as the Packer `powershell` provisioner on the dashboard's Azure Windows
  build (Build Image tab, os_type=Windows), BEFORE the template's
  windows-restart + Sysprep /generalize finisher. The build itself connects over
  WinRM (marketplace base images have no SSH at first boot); this script bakes
  OpenSSH + RDP into the OUTPUT image so VMs deployed from it are reachable the
  same way Linux cloud VMs are — SSH — plus agentless RDP through the PRA
  Jumpoint.

  WHY THE KEY IS BAKED HERE: Azure cannot inject SSH public keys into Windows
  VMs at deploy time (that is Linux-only — WindowsConfiguration has no SSH
  field). So authorize the key in the image. Use the PUBLIC half of the keypair
  the dashboard stores in Key Vault (azure_ssh_keypair_secret_name) so the
  matching private key is retrievable from the VMs tab / ssh-key endpoint exactly
  like Linux. With no key set, password-auth SSH still works using the admin
  password the deploy generates + vaults (Azure -> VMs -> Password).

  Operator-overridable. Edit the inline defaults below, OR set the matching
  $env:BT_* before the build. NOTE: the dashboard's PowerShell provisioner does
  not yet forward environment_vars, so when driving this through the GUI the
  INLINE values are the working path today (the $env:* reads are for CLI/Packer
  use and a future env-var wiring).
    $AuthorizedKey / $env:BT_AUTHORIZED_KEY   SSH public key -> administrators_authorized_keys
    $AdminUser     / $env:BT_ADMIN_USER       admin account label (default: azureuser; key applies to all admins)
    $DisablePasswordAuth via $env:BT_SSH_KEY_ONLY=1   harden sshd to key-only (default: keep password auth)
    $EnableRdp     via $env:BT_ENABLE_RDP=0   skip RDP enablement (default: enable)

  See provisioners/beyondtrust/README.md.
#>

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Log([string] $m) { Write-Output "[bt-ready] $m" }

# -- Config (inline defaults; $env:BT_* overrides win when present) -----------
# Paste your SSH PUBLIC key between the quotes to enable key-based access, e.g.
#   $AuthorizedKey = 'ssh-ed25519 AAAA... you@host'
# Leave blank to ship password-auth SSH only.
$AuthorizedKey = if ($env:BT_AUTHORIZED_KEY) { $env:BT_AUTHORIZED_KEY } else { '' }
$AdminUser     = if ($env:BT_ADMIN_USER)     { $env:BT_ADMIN_USER }     else { 'azureuser' }
$DisablePasswordAuth = ($env:BT_SSH_KEY_ONLY -eq '1')
$EnableRdp           = ($env:BT_ENABLE_RDP -ne '0')

Log "starting on $([System.Environment]::OSVersion.VersionString) (admin: $AdminUser)"

# -- 1. Install OpenSSH Server ------------------------------------------------
# Prefer the in-box Feature-on-Demand; fall back to the official Win32-OpenSSH
# MSI if the FoD source is unreachable. (The build VM has normal Azure internet
# egress — the corp TLS proxy only sits in front of the dashboard container's
# WinRM connection, not the build VM's own outbound.)
Log 'installing OpenSSH Server'
$installed = $false
try {
    $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
    if ($null -ne $cap) {
        if ($cap.State -ne 'Installed') { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
        $installed = $true
    }
} catch {
    Log "warn: Add-WindowsCapability failed ($($_.Exception.Message)) - falling back to Win32-OpenSSH MSI"
}
if (-not $installed) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $rel = Invoke-RestMethod 'https://api.github.com/repos/PowerShell/Win32-OpenSSH/releases/latest' `
        -Headers @{ 'User-Agent' = 'bt-ready' }
    $asset = $rel.assets | Where-Object { $_.name -like 'OpenSSH-Win64-*.msi' } | Select-Object -First 1
    if ($null -eq $asset) { throw 'could not locate an OpenSSH-Win64 MSI in the latest Win32-OpenSSH release' }
    $msi = Join-Path $env:TEMP 'openssh.msi'
    Invoke-WebRequest $asset.browser_download_url -OutFile $msi -UseBasicParsing
    Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /qn" -Wait -NoNewWindow
    Remove-Item $msi -Force
}

# -- 2. Enable + start services ----------------------------------------------
Log 'enabling sshd + ssh-agent (Automatic)'
Set-Service -Name sshd      -StartupType Automatic
Set-Service -Name ssh-agent -StartupType Automatic
Start-Service sshd

# -- 3. Firewall: SSH 22/tcp --------------------------------------------------
if (-not (Get-NetFirewallRule -Name 'sshd' -ErrorAction SilentlyContinue)) {
    Log 'opening firewall for TCP 22 (sshd)'
    New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' -Enabled True `
        -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}

# -- 4. Default shell = PowerShell (so `ssh user@host` lands in a familiar shell)
Log 'setting OpenSSH default shell to PowerShell'
New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
    -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -PropertyType String -Force | Out-Null

# -- 5. Authorize SSH public key (administrators) -----------------------------
# For admin-group accounts, sshd reads C:\ProgramData\ssh\administrators_authorized_keys
# (NOT the user's ~/.ssh), and REFUSES it unless it is owned by Administrators/
# SYSTEM with no other write access. This ACL dance is the #1 Windows OpenSSH
# gotcha. The file must be ASCII without a BOM.
if ($AuthorizedKey.Trim()) {
    $akFile = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
    Log "authorizing SSH key in $akFile"
    [IO.File]::WriteAllText($akFile, $AuthorizedKey.Trim() + "`n", (New-Object Text.ASCIIEncoding))
    icacls $akFile /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed to set ACLs on $akFile (exit $LASTEXITCODE)" }
    Log 'SSH key authorized — key-based access ready'
} else {
    Log 'no $AuthorizedKey set — skipping key authorization (password-auth SSH still works with the deploy-time admin password)'
}

# -- 6. sshd auth policy ------------------------------------------------------
$sshdConfig = Join-Path $env:ProgramData 'ssh\sshd_config'
if ($DisablePasswordAuth) {
    Log 'BT_SSH_KEY_ONLY=1 — hardening sshd to key-only (PasswordAuthentication no)'
    if (Test-Path $sshdConfig) {
        (Get-Content $sshdConfig) -replace '^#?\s*PasswordAuthentication.*', 'PasswordAuthentication no' |
            Set-Content $sshdConfig -Encoding ascii
    }
} else {
    Log 'leaving password auth enabled — ssh in with the deploy-time admin password'
}
Restart-Service sshd

# -- 7. RDP + NLA (PRA agentless RDP via the Jumpoint) ------------------------
if ($EnableRdp) {
    Log 'enabling RDP + NLA + firewall group'
    Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server' `
        -Name fDenyTSConnections -Value 0
    Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' `
        -Name UserAuthentication -Value 1
    Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'
} else {
    Log 'BT_ENABLE_RDP=0 — skipping RDP enablement'
}

# -- 8. Done ------------------------------------------------------------------
# No host-key / SID / log cleanup here: the build template runs windows-restart
# then Sysprep /generalize next, which owns Windows image generalization.
Log 'bt-ready-windows complete — windows-restart + Sysprep generalize run next'
