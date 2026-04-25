# VMware vSphere / ESXi Integration

## What is it?

The vSphere integration connects the dashboard to a VMware vCenter Server or a
standalone ESXi host via the **vSphere Web Services API** (pyVmomi). It adds a
**vSphere** tab to the dashboard where you can list all VMs across your VMware
estate, inspect their state and hardware configuration, and control power — all
without opening the vSphere Client.

---

## Use cases

- **Unified on-premises + cloud view** — see VMware VMs alongside AWS EC2, Azure
  VMs, and GCP instances from a single screen.
- **Datacenter and homelab management** — start, stop, and reboot VMs without
  opening the vSphere Client.
- **ESXi standalone hosts** — works with bare ESXi (no vCenter required);
  returns the single `ha-datacenter` datacenter.
- **vCenter multi-datacenter environments** — lists every datacenter, filters the
  VM list by datacenter when you choose one.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| VMware ESXi 6.7+ or vCenter Server 6.7+ | The Web Services API is available on all recent VMware releases |
| A read/power-control user account | See setup below — dedicated read-only account is recommended |
| Network access | The dashboard container must reach the host on port 443 (HTTPS) |
| `pyVmomi>=8.0.0` | Installed automatically from `requirements.txt` |

---

## Setup

### Step 1 — Create a dedicated service account (recommended)

For vCenter Server:

1. Log in to the **vSphere Client** → **Administration** → **Single Sign-On** →
   **Users and Groups** → **Add**.
2. Create a user (e.g. `dashboard@vsphere.local`) in the `vsphere.local` domain.
3. Go to **Administration** → **Access Control** → **Roles** → **Clone** the
   built-in **Read-Only** role and name it `Dashboard`.
4. Add these privileges to the cloned role:
   - **Virtual Machine** → **Interaction**: `Power Off`, `Power On`, `Reset`,
     `Suspend`, `Console Interaction` (optional)
   - **Virtual Machine** → **Guest Operations** (optional — needed to read IP
     addresses when using VMware Tools)
5. Go to **Administration** → **Access Control** → **Global Permissions** →
   **Add**, select `dashboard@vsphere.local` and the `Dashboard` role, check
   **Propagate to children**.

For a standalone ESXi host:

1. Go to the **ESXi host client** → **Manage** → **Security & Users** →
   **Users** → **Add user**.
2. Assign the `Administrator` role (ESXi has no custom role editor in the host
   client) or use the full `root` account if this is an isolated lab host.

### Step 2 — Enable and configure in the dashboard

**Option A — Settings → Integrations → VMware vSphere / ESXi**

Toggle **VMware vSphere / ESXi** on. Fill in the connection fields:

| Field | Description |
|---|---|
| vCenter / ESXi Host | Hostname or IP of the vCenter Server or ESXi host |
| Port | Default `443` |
| Username | e.g. `administrator@vsphere.local` or `root` |
| Password | The account password |
| Default Datacenter | Optional — leave blank to show all VMs; set to filter |
| Verify SSL | Disable for self-signed certificates (common in home labs) |

Click **Save**. No container restart is required.

**Option B — `.env` file**

```
VSPHERE_ENABLED=true
VSPHERE_HOST=vcenter.corp.local
VSPHERE_PORT=443
VSPHERE_USER=administrator@vsphere.local
VSPHERE_PASSWORD=ChangeMe
VSPHERE_VERIFY_SSL=false
VSPHERE_DATACENTER=
```

### Step 3 — Verify

The **vSphere** link appears in the navigation bar. Click it — you should see
host tabs and a table of VMs within a few seconds.

---

## What it enables in the dashboard

| Feature | Description |
|---|---|
| **vSphere tab** | Lists all non-template VMs across all hosts |
| **Host tabs** | Filter VMs by ESXi host |
| **Datacenter filter** | Dropdown filter (only shown when vCenter has multiple datacenters) |
| **Power On** | Start a powered-off or suspended VM |
| **Graceful Shutdown** | Guest OS shutdown via VMware Tools (only enabled when Tools are running) |
| **Force Off** | Hard power-off (equivalent to pulling the plug) |
| **Reset** | Hard reboot |
| **Suspend** | Suspend VM to memory |
| **VM detail** | Hardware config, guest OS, Tools status, all IP addresses, annotation, managed object reference |
| **Host summary cards** | CPU, memory, VM count, maintenance mode status per host |

Templates are automatically excluded from the VM list.

---

## VMware Tools and graceful shutdown

The **Graceful Shutdown** button is only enabled when the `tools_status` for the
VM is `toolsOk` (i.e., VMware Tools are installed and running inside the guest).

To install VMware Tools in a Linux VM:

```bash
# Debian / Ubuntu
apt-get install -y open-vm-tools

# RHEL / Rocky / AlmaLinux
dnf install -y open-vm-tools

# SUSE / openSUSE
zypper install -y open-vm-tools
```

Windows VMs: Install from **VM menu → Install VMware Tools** in the vSphere
Client, or use the ISO mount already in the CD-ROM drive.

---

## vCenter vs standalone ESXi

| Scenario | Datacenter name | Notes |
|---|---|---|
| Standalone ESXi | `ha-datacenter` | The host tab shows one host; datacenter filter hidden |
| vCenter (single DC) | Your DC name | Datacenter filter hidden (only one option) |
| vCenter (multiple DCs) | Multiple | Datacenter dropdown appears to filter the VM list |

In all cases, the same `VSPHERE_HOST` / `VSPHERE_USER` / `VSPHERE_PASSWORD`
configuration applies — the API is identical for ESXi and vCenter.

---

## Power operations reference

| Operation | API call | Requires Tools | Notes |
|---|---|---|---|
| **Power On** | `PowerOnVM_Task` | No | Works from powered-off or suspended |
| **Graceful Shutdown** | `ShutdownGuest` | **Yes** | Polls power state; UI button disabled without Tools |
| **Force Off** | `PowerOffVM_Task` | No | Hard power-off — data loss risk if guest has unsaved work |
| **Reset** | `ResetVM_Task` | No | Hard reset — equivalent to hardware reset button |
| **Suspend** | `SuspendVM_Task` | No | Saves VM state to disk/memory |

---

## Troubleshooting

**vSphere tab is missing** — verify `VSPHERE_ENABLED=true` and that the stack
restarted after the change (or that you saved via Settings → Integrations).

**"VSPHERE_HOST is not configured"** — the host field is required. Set it in
`.env` or Settings → Integrations → VMware vSphere / ESXi.

**"pyVmomi is not installed"** — run `pip install pyVmomi` inside the container,
or rebuild the image: `docker compose build app`.

**SSL certificate errors** — for self-signed certificates, set
`VSPHERE_VERIFY_SSL=false`. For production with a valid CA-signed cert, set it
to `true`.

**"Permission to perform this operation was denied"** — the account lacks the
required privileges. Check the role assignment in vCenter → Administration →
Access Control → Global Permissions.

**IP addresses not showing** — IP addresses are read from the VMware guest
agent (VMware Tools). Install and start `open-vm-tools` inside the guest. For
VMs without Tools, the IP address column will be empty.

**VMs loading slowly** — the service opens a fresh vSphere session for each
request (no persistent session pool). If the inventory is large (hundreds of
VMs), consider setting `VSPHERE_DATACENTER` to scope requests to one datacenter.

**"VM not found" on power operation** — the managed object reference (moref)
changed, which can happen after a vMotion or vCenter reconnect. Refresh the VM
list and retry.
