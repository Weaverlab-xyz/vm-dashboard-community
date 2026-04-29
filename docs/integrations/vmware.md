# VMware Workstation Integration

## What is it?

The VMware integration lets you list, start, and stop VMware Workstation VMs
on your **local Windows machine** directly from the dashboard web UI. No
separate vSphere or ESXi licence is required — it works with the desktop
hypervisor (Workstation Pro) you probably already have.

The dashboard container SSHes from inside Docker back to the Windows host and
invokes a PowerShell wrapper script (`vm_cli_api_wrapper.ps1`) that drives
VMware's `vmrun` CLI. The result is a VM management panel alongside your cloud
resources.

---

## Use cases

- **Lab environment management** — start and stop test VMs from the same
  interface you use to spin up EC2 and Azure VMs, without switching tools.
- **On-prem + cloud unified view** — see your VMware VMs and cloud instances
  in one dashboard without logging into multiple consoles.
- **Developer workflows** — quickly power VMs on before a demo or testing
  session, and tear them back down after.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Windows host | The dashboard container SSHes to the **host machine**; VMware and PowerShell 7 must be on that machine |
| VMware Workstation Pro | Free Personal Use licence is fine; `vmrun.exe` must be on `PATH` |
| PowerShell 7+ | The wrapper script uses `??` (null-coalescing) — PS 5.1 will not work |
| OpenSSH server (Windows) | The container authenticates to the host via SSH; must be running |
| SSH key pair | The container needs an RSA/Ed25519 key whose public half is in the host's `authorized_keys` |

### Enable OpenSSH server on Windows

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
```

### Generate an SSH key for the container

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\dev_dashboard_key" -N ""
# Add the public key to authorized_keys
Add-Content "$env:USERPROFILE\.ssh\authorized_keys" `
    (Get-Content "$env:USERPROFILE\.ssh\dev_dashboard_key.pub")
```

---

## Setup

### 1. Copy the Windows Compose override

```powershell
Copy-Item docker-compose.override.windows.yml.example `
          docker-compose.override.windows.yml
```

### 2. Edit the override file

Open `docker-compose.override.windows.yml` and set `VM_CLI_WRAPPER_PATH` to the
full path of `vm_cli_api_wrapper.ps1` on your host:

```yaml
environment:
  - VM_CLI_WRAPPER_PATH=C:\Scripts\VM_CLI\VM_DEMO_CLI\vm_cli_api_wrapper.ps1
  - POWERSHELL_EXECUTION_MODE=ssh
```

The `volumes:` section in the override bind-mounts your SSH private key into
the container. Verify the path matches where you generated the key:

```yaml
volumes:
  - ${USERPROFILE}/.ssh/dev_dashboard_key:/root/.ssh/dev_dashboard_key:ro
```

### 3. Enable the integration

Enable **VMware** in the setup wizard (Step 5) or **Settings → Integrations →
VMware**. The dashboard also needs to know the SSH user and host to reach the
Windows host — configure these in the same Settings panel:

| Setting | Value |
|---|---|
| SSH User | your Windows username |
| SSH Host | `host.docker.internal` (Docker Desktop's built-in hostname for the Windows host) |

### 4. Start the stack with both Compose files

```powershell
docker compose `
  -f docker-compose.yml `
  -f docker-compose.override.windows.yml `
  up -d
```

> **Note:** every restart must include both `-f` flags. The override file adds
> the bind-mount and SSH env vars; without it the VMs tab is unavailable even
> if the flag is set.

### 5. Verify

The **VMs** link should appear in the navigation bar. Click it — you should see
any VMs in your configured workgroup paths within a few seconds.

---

## What it enables in the dashboard

| Feature | Description |
|---|---|
| **VMs tab** | Lists all VMs in your configured `WORKGROUPS` paths |
| **Start / Stop** | One-click power toggle per VM |
| **VM detail** | OS type, memory, CPU count, VMX file path |
| **Workgroup filter** | If you have multiple VM libraries (e.g. lab + dev), they appear as separate groups |

The feature is entirely **Windows host-only**. The flag is hidden and
non-functional on macOS, Linux, and WSL hosts.

---

## Troubleshooting

**VMs tab is missing** — verify VMware is toggled on in **Settings → Integrations
→ VMware** and that you started the stack with the Windows override file.

**"SSH connection refused"** — verify OpenSSH server is running:
```powershell
Get-Service sshd
```

**"vmrun not found"** — `vmrun.exe` must be on the Windows `PATH`. It ships
with VMware Workstation, typically at
`C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe`. Add its
directory to `PATH` via System Properties → Environment Variables.

**"Cannot dot-source"** — the wrapper script cuts content at the `# RUN MENU`
marker before dot-sourcing. If you customised the CLI script and removed that
marker, the wrapper will fail. Restore the marker or adjust
`vm_cli_api_wrapper.ps1` to match.

**Key permission errors on startup** — Docker Desktop mounts Windows files as
mode `0777`; the container entrypoint copies the key to a `0600` path
automatically. If you see `WARNING: UNPROTECTED PRIVATE KEY FILE`, check that
`/usr/local/bin/entrypoint.sh` ran and that the key copy succeeded (check
`docker compose logs app | head -20`).
