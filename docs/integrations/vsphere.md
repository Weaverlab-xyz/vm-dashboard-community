# VMware vSphere / ESXi Integration

> **Audience:** operator · **Profile:** `demo` · **Read this when:** your VMs live on vSphere or a standalone ESXi host.

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

Turning it on sets the `vsphere_enabled` feature flag, and the **vSphere** page appears in
the navigation at `/vsphere`.

**Turn it on** in **Settings → Integrations → VMware vSphere / ESXi**.

**Then add the connection on the Connections tab** of the Remote Agents page
(`/connections`) → **Add connection**, kind **vSphere**. That is where connection details
have lived since a dashboard could hold more than one of each kind; see
[Multiple connections](#multiple-connections).

| Field | Description |
|---|---|
| Name | Your name for it, such as `dc1` |
| Host | Hostname or IP of the vCenter Server or ESXi host |
| Port | Default `443` |
| Username | e.g. `administrator@vsphere.local` or `root` |
| Datacenter | Optional — leave blank to show all VMs; set to filter |
| Password | The account password. Prefer a **Password Safe managed account** or a **secret reference** (`bt_safe://`, `aws_sm://`, `azure_kv://`, `gcp_sm://`): a password stored here is the weakest of the three |
| Verify the TLS certificate | Untick for self-signed certificates (common in home labs) |
| Site | Optional label |
| Make this the default | The connection every page and API call uses unless told otherwise. The first one of a kind becomes the default |

Click **Save**. No container restart is required. For a vSphere the dashboard cannot reach,
tick **Reached through a remote agent** instead; see [Over a remote agent](#over-a-remote-agent).

The old **Settings → Integrations → VMware vSphere / ESXi** fields (`vsphere_host`, `vsphere_port`, `vsphere_user`, `vsphere_password`, `vsphere_datacenter`, `vsphere_verify_ssl`) are read only as a fallback
while no vSphere connection exists, and were copied into the first connection on upgrade.
Editing them after that changes nothing.

### Step 3 — Verify

The **vSphere** link appears in the navigation menu (the ☰ button in the top bar). Click it — you should see
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
| **Bulk power** | Tick several rows and send Power On, Shutdown, Force Off or Reset to the whole selection from the toolbar — one job per VM, sharing a batch you watch on one page. VMs already in the target state are skipped and the dialog says how many; fifty per operation is the cap. See [Powering a selection](../remote-agents/hypervisors.md#powering-a-selection) |

Templates are automatically excluded from the VM list.

**Tags.** vSphere tags are not read yet, so VMs here show no tag chips and do not appear
under [Inventory](../inventory.md)'s **Tag** filter. Proxmox is the only hypervisor that
reports tags today; see [Cloud VMs — Tags and labels](../cloud/vms.md#tags-and-labels).

**Scheduling power.** On a connection bound to a
[remote agent](../remote-agents/hypervisors.md), the bulk power toolbar's **Schedule**
tick books the operation for a time or a change window. A directly dialled connection
cannot be booked, and each of its VMs is refused by name. See [Scheduling](../scheduling.md).

---

## VMware Tools and graceful shutdown

The **Graceful Shutdown** button is only enabled when the `tools_status` for the
VM is `toolsOk` (i.e., VMware Tools are installed and running inside the guest).

One exception, and it is the difference between *unknown* and *absent*: an
[agent-bound connection](#over-a-remote-agent) shows a synced inventory that carries no
`tools_status` at all, so the button is offered rather than hidden. If Tools turns out not
to be running, vCenter answers 503 and the job fails saying so.

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

**"no vsphere connection is configured — add one on the Connections page"** — add a
vSphere connection on the **Connections** tab of the Remote Agents page (`/connections`).

**"pyVmomi is not installed"** — run `pip install pyVmomi` inside the container,
or rebuild the image: `docker compose build app`.

**SSL certificate errors** — for self-signed certificates, untick **Verify the TLS
certificate** on the connection. For production with a valid CA-signed cert, leave it on.

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

## Multiple connections

Connection details used to live in Settings as a single set of fields, so there could
only ever be one vCenter. They now live on the **Connections** tab of the Remote Agents page (`/connections`),
which holds as many as you like — a second vCenter at another site, or the same one
under a read-only and a privileged service account.

* The **default** connection is what every page and API call uses when not told
  otherwise. The first connection of a kind becomes the default automatically.
* Pass `?connection_id=<id>` to any `/api/vsphere` endpoint to target a specific one.
* Job-backed operations (deploys, power verbs) record the connection at **enqueue**, so
  changing the default while one is queued cannot redirect it.

Your existing Settings values were copied into the first connection on upgrade. The old
panel is still there, read-only, with a banner pointing here — editing it no longer
changes what the dashboard connects to. It is kept so that rolling back to a previous
image still works.

## Over a remote agent

A vCenter the dashboard has no network route to can be reached through a
[remote agent](../remote-agents/hypervisors.md#hypervisor-connections) instead. Tick *Reached
through a remote agent* when adding the connection and give it the name that connection
has in the agent's own `connections.yaml`.

The dashboard then stores **no host and no credential** for it — only the name. The
agent uses the vSphere Automation REST API (7.0U2+), which needs no dependency the agent does not already have.

Three separate grants must line up: the dashboard grants the agent the
`agent_hypervisor` job type, your `policy.yaml` grants the individual verbs on that
connection, and your `connections.yaml` defines it. Withhold any one and nothing runs.

**vCenter only.** A bare ESXi host serves the SOAP API and not the
Automation REST API, so an ESXi connection has to stay dashboard-direct.

### When the connection is reached through an agent

The dashboard has no route to an agent-bound connection — that is the point of binding it
to an agent — so this page cannot query it live. It shows the **last synced inventory**
instead, with a banner saying so and how old it is. Live-only figures (CPU usage, uptime,
disk) are blank there rather than zero: they were never measured, and a fabricated 0 is
worse than an empty cell.

Power actions still work: they are dispatched to the agent as jobs and appear on `/jobs`
with Live Output, exactly like a discovery scan. Power On, Force Off, Reset and Shutdown
all map to a verb; **Suspend** does not, and is refused with a 501 naming what is
available rather than approximated onto a neighbouring operation — see
[the verbs](../remote-agents/hypervisors.md#the-verbs).

Shutdown is the one that needs the guest: it is not a power action but a call to
vCenter's separate `guest/power` endpoint, through VMware Tools. The synced inventory
carries no `tools_status`, so the button is offered rather than hidden, and vCenter
answers 503 if Tools is not running.
