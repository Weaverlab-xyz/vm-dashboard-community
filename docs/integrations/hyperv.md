# Microsoft Hyper-V Integration

## What is it?

The Hyper-V integration connects the dashboard to a Windows host running
Microsoft Hyper-V via **WinRM** (Windows Remote Management). It executes
PowerShell `Hyper-V` module cmdlets remotely — no agent required on the host —
and adds a **Hyper-V** tab to the dashboard where you can list all VMs, inspect
their state and configuration, and control power.

---

## Use cases

- **On-premises Windows Server management** — control VMs on Windows Server
  Hyper-V hosts alongside cloud resources from a single dashboard.
- **Dev/test lab management** — start and stop VMs on a Windows 10/11
  developer workstation running Hyper-V.
- **Unified on-prem + cloud view** — see Hyper-V VMs next to AWS EC2, Azure
  VMs, Proxmox, and vSphere resources in one screen.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Windows Server 2016–2025 or Windows 10/11 Pro/Enterprise/Education | With Hyper-V role/feature enabled |
| WinRM enabled on the host | See Step 1 — one PowerShell command |
| A Windows account with Hyper-V management rights | Local admin or dedicated service account |
| Network access | Dashboard container → host on port 5985 (HTTP) or 5986 (HTTPS) |
| `pywinrm>=0.4.3` | Installed automatically from `requirements.txt` |

---

## Setup

### Step 1 — Enable WinRM on the Hyper-V host

Open PowerShell **as Administrator** on the Hyper-V host and run:

```powershell
Enable-PSRemoting -Force
```

This enables the WinRM service, sets it to start automatically, and creates a
default HTTP listener on port 5985.

For a workgroup (non-domain) host, also run:

```powershell
Set-Item WSMan:\localhost\Client\TrustedHosts -Value "<dashboard-ip>" -Force
```

Replace `<dashboard-ip>` with the IP or hostname of the machine running the
dashboard container.

### Step 2 — Create a service account (optional but recommended)

Using a dedicated local account avoids giving the dashboard full Administrator
access:

```powershell
# Create a local account
$pw = ConvertTo-SecureString "ChangeMe123!" -AsPlainText -Force
New-LocalUser -Name "dashboard-svc" -Password $pw -FullName "Dashboard Service" -PasswordNeverExpires

# Add to the Hyper-V Administrators group (read + power control)
Add-LocalGroupMember -Group "Hyper-V Administrators" -Member "dashboard-svc"

# Add to Remote Management Users so WinRM accepts the account
Add-LocalGroupMember -Group "Remote Management Users" -Member "dashboard-svc"
```

For a domain-joined host, use an Active Directory service account instead of a
local account and ensure it is a member of the **Hyper-V Administrators** built-in
group on each host.

### Step 3 — Enable and configure in the dashboard

**Option A — Settings → Integrations → Microsoft Hyper-V**

Toggle **Microsoft Hyper-V** on. Fill in the connection fields:

| Field | Description |
|---|---|
| Hyper-V Host | Hostname or IP of the Windows host |
| Port | `5985` for HTTP (default), `5986` for HTTPS |
| Username | Windows account — `DOMAIN\user`, `user@domain`, or `.\localuser` |
| Password | Account password |
| Auth Transport | `NTLM` (works for domain and local accounts without extra setup) |
| Use HTTPS | Enable to use WinRM over HTTPS (requires a certificate on the host) |
| Verify SSL | Disable for self-signed certificates |

Click **Save**. No container restart is required.

### Step 4 — Verify

The **Hyper-V** link appears in the navigation bar. Click it — you should see
your VMs listed within a few seconds.

---

## What it enables in the dashboard

| Feature | Description |
|---|---|
| **Hyper-V tab** | Lists all VMs on the configured host |
| **State display** | Running (green), Off (gray), Saved (amber), Paused (blue) |
| **Generation badge** | Gen 1 or Gen 2 per VM |
| **Power On** | Start an off, saved, or paused VM |
| **Graceful Shutdown** | Guest OS shutdown via Integration Services (button disabled without IS) |
| **Force Off** | Hard power-off (`Stop-VM -Force`) |
| **Restart** | Hard reset (`Restart-VM -Force`) |
| **Pause** | Suspend VM to memory (`Suspend-VM`) |
| **Resume** | Resume a paused or saved VM |
| **Save** | Save VM state to disk (`Save-VM`) |
| **VM detail modal** | vCPUs, memory, CPU usage, uptime, IP addresses, IS state, VM ID, path |

---

## Integration Services

**Integration Services** (IS) is the Hyper-V equivalent of VMware Tools. When
IS are installed and running inside the guest, the dashboard can:

- Show IP addresses for the VM
- Perform **graceful shutdown** (the `Shutdown` button is disabled when IS are absent)

To install IS in a Linux VM, ensure the `hyperv-daemons` package is installed:

```bash
# Debian / Ubuntu
apt-get install -y hyperv-daemons

# RHEL / Rocky / AlmaLinux
dnf install -y hyperv-daemons

# SUSE
zypper install -y hyper-v
```

Windows guest VMs include IS built-in on modern Windows versions. If IS appear
as "not installed" on a Windows VM, run Windows Update inside the guest.

The dashboard shows the IS state as:
- **Up to date** — IS running and current (graceful shutdown available)
- **Update available** — IS running but outdated (graceful shutdown still works)
- **Not installed** — IS absent (graceful shutdown button disabled)

---

## Power operations reference

| Operation | PowerShell cmdlet | Requires IS | Notes |
|---|---|---|---|
| **Start** | `Start-VM` | No | Starts off, saved, or paused VM |
| **Graceful Shutdown** | `Stop-VM` | **Yes** | Guest OS shutdown — button disabled without IS |
| **Force Off** | `Stop-VM -Force` | No | Hard power-off — data loss risk |
| **Restart** | `Restart-VM -Force` | No | Hard reset |
| **Pause** | `Suspend-VM` | No | Suspends VM to memory |
| **Resume** | `Resume-VM` | No | Resumes paused or saved state |
| **Save** | `Save-VM` | No | Saves VM state to disk (frees memory) |

---

## Auth transport options

| Transport | When to use |
|---|---|
| **NTLM** (default) | Domain accounts and local Windows accounts. No extra infrastructure needed. Works over HTTP (port 5985). |
| **Basic** | Simplest — username/password in plain text. **Requires HTTPS** (port 5986) to avoid credential exposure. |
| **Kerberos** | Domain accounts when the dashboard container is also domain-joined. Requires `krb5` packages in the container. |

For most setups, **NTLM over HTTP** (port 5985) is the right choice. Switch to
HTTPS if the network between the container and the Hyper-V host is untrusted.

---

## HTTPS / WinRM over SSL (optional)

To use WinRM over HTTPS (port 5986), the Hyper-V host needs a certificate.
For a lab, you can create a self-signed cert and bind it to the WinRM listener:

```powershell
# Create a self-signed cert valid for 3 years
$cert = New-SelfSignedCertificate -DnsName "hyperv.corp.local" `
    -CertStoreLocation Cert:\LocalMachine\My `
    -NotAfter (Get-Date).AddYears(3)

# Create an HTTPS listener
New-WSManInstance WinRM/Config/Listener `
    -SelectorSet @{Address="*"; Transport="HTTPS"} `
    -ValueSet @{CertificateThumbprint=$cert.Thumbprint}

# Open the HTTPS port in the firewall
New-NetFirewallRule -Name "WinRM HTTPS" -DisplayName "WinRM HTTPS" `
    -Protocol TCP -LocalPort 5986 -Action Allow
```

In the dashboard: set **Port** to `5986`, enable **Use HTTPS**, and disable
**Verify SSL** (for self-signed), or enable it if using a CA-signed cert.

---

## Troubleshooting

**Hyper-V tab is missing** — verify `HYPERV_ENABLED=true` and that the stack
restarted after the change (or that you saved via Settings → Integrations).

**"HYPERV_HOST is not configured"** — set the host field in **Settings → Integrations → Hyper-V**.

**"pywinrm is not installed"** — run `pip install pywinrm` inside the container
or rebuild the image: `docker compose build app`.

**"Connection refused" (port 5985)** — WinRM is not listening. Run
`Enable-PSRemoting -Force` on the host. Check the firewall:
```powershell
Get-NetFirewallRule -Name "WINRM-HTTP-In-TCP*" | Select Enabled
```

**"401 Unauthorized"** — username or password is wrong, or NTLM is blocked.
Verify credentials with: `Test-WSMan -ComputerName hyperv.corp.local -Credential (Get-Credential)`.

**"WinRM cannot process the request" (workgroup host)** — add the dashboard
server's IP to TrustedHosts on the Hyper-V host:
```powershell
Set-Item WSMan:\localhost\Client\TrustedHosts -Value "*" -Force
```
(Replace `*` with the specific dashboard IP for a more restrictive setting.)

**"Access is denied" on power operations** — the account is not in the
**Hyper-V Administrators** group. Add it:
```powershell
Add-LocalGroupMember -Group "Hyper-V Administrators" -Member "dashboard-svc"
```

**IP addresses not showing** — install `hyperv-daemons` (Linux) or update IS
(Windows) inside the guest. IP discovery requires Integration Services.

**Slow VM list** — each refresh opens a WinRM connection and runs a PowerShell
script. On hosts with many VMs (50+), the list script may take 5–10 seconds.
The dashboard shows a spinner during the load.
