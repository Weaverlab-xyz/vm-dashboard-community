# Proxmox VE Integration

## What is it?

The Proxmox VE integration connects the dashboard to one or more
[Proxmox Virtual Environment](https://www.proxmox.com/en/proxmox-ve) nodes or
clusters via the Proxmox REST API. It adds a **Proxmox** tab to the dashboard
where you can see all your QEMU VMs and LXC containers, check resource usage,
and start, stop, or reboot them — without opening the Proxmox web UI.

---

## Use cases

- **Unified on-prem + cloud view** — see Proxmox VMs alongside AWS EC2, Azure
  VMs, and GCP instances in a single dashboard without switching between tools.
- **Lab and homelab management** — start and stop VMs before a demo or test
  session from the same interface used for cloud resources.
- **Multi-node visibility** — view all VMs and containers across every node in
  a Proxmox cluster, grouped by node, from one screen.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Proxmox VE 7.x or 8.x | The REST API is available on all recent Proxmox versions |
| API token or user account | API token is strongly recommended (see setup below) |
| Network access | The dashboard container must be able to reach the Proxmox host on port 8006 |

---

## Setup

### Step 1 — Create an API token (recommended)

API tokens are the preferred auth method: they are revokable, auditable, and do
not require session management.

1. Log in to the **Proxmox web UI** → **Datacenter** → **Permissions** →
   **API Tokens** → **Add**.
2. Select the user (e.g. `root@pam`), enter a Token ID (e.g. `dashboard`),
   and uncheck **Privilege Separation** if you want the token to inherit the
   user's full permissions.
3. Click **Add** and copy the **Token Secret** — it is shown only once.

The full token identifier shown in Proxmox is `USER@REALM!TOKENID`
(e.g. `root@pam!dashboard`). In the dashboard, enter:
- **Username**: `root@pam`
- **Token ID**: `dashboard`
- **Token Secret**: the UUID shown after creation

### Step 2 — Grant the API token read and power permissions

The minimum required privileges on the `/` path (or the relevant pool):

| Privilege | Purpose |
|---|---|
| `VM.Audit` | List VMs and containers, read status |
| `VM.PowerMgmt` | Start, stop, shutdown, reboot |
| `Sys.Audit` | List nodes and cluster resources |

Assign via **Datacenter → Permissions → Add → API Token Permission**:
- Path: `/`
- Token: `root@pam!dashboard`
- Role: **PVEVMAdmin** (includes all of the above) or a custom role

### Step 3 — Enable and configure in the dashboard

**Option A — Settings → Integrations → Proxmox VE**

Toggle **Proxmox VE** on. Fill in the connection fields:

| Field | Description |
|---|---|
| Proxmox Host | Hostname or IP of a Proxmox node (or the cluster VIP) |
| Port | Default `8006` |
| Username | e.g. `root@pam` |
| Token ID | The token name (e.g. `dashboard`) |
| Token Secret | The UUID token value |
| Verify SSL | Disable for self-signed certificates (common in home labs) |

Click **Save**. No container restart is required.

### Step 4 — Verify

The **Proxmox** link appears in the navigation bar. Click it — you should see
your nodes as tabs and all VMs and containers listed within a few seconds.

---

## What it enables in the dashboard

| Feature | Description |
|---|---|
| **Proxmox tab** | Lists all QEMU VMs and LXC containers across all nodes |
| **Node tabs** | Filter the resource list by node |
| **Start / Shutdown / Force Off** | One-click power controls per VM or container |
| **Reboot** | Graceful reboot for QEMU VMs |
| **Status and resource usage** | CPU %, memory, running/stopped state |
| **VM detail** | IP addresses (via QEMU guest agent), OS type, tags, description |

Templates are automatically hidden from the resource list.

---

## Password auth (alternative)

If you cannot create an API token, you can authenticate with a username and
password instead. Leave **Token ID** and **Token Secret** blank and set
**Password**. Note that password auth creates a ticket that expires; the
dashboard will re-authenticate on each request, which is slightly slower and
adds one API call per operation.

```
PROXMOX_USER=root@pam
PROXMOX_PASSWORD=<your-root-password>
```

---

## Cluster vs single-node setup

The integration works identically for a single-node Proxmox installation and a
full Proxmox cluster. Point `PROXMOX_HOST` at any node in the cluster — Proxmox
returns resources across all cluster members from any single node's API. The
node tabs in the UI reflect the actual cluster topology.

---

## IP address display (QEMU guest agent)

The VM detail panel shows IP addresses when the **QEMU Guest Agent** is
installed and running inside the VM. Without it, IP addresses cannot be read
from the API.

To install the agent inside a Debian/Ubuntu VM:
```bash
apt-get install -y qemu-guest-agent
systemctl enable --now qemu-guest-agent
```

Then enable it in Proxmox: **VM → Options → QEMU Guest Agent → Enabled**.

---

## Troubleshooting

**Proxmox tab is missing** — verify `PROXMOX_ENABLED=true` and that the stack
restarted after the change (or that you saved via Settings → Integrations).

**"PROXMOX_HOST is not configured"** — the host field is required. Set it in
**Settings → Integrations → Proxmox VE**.

**"Connection refused" or timeout** — confirm port 8006 is reachable from
inside the container:
```bash
docker compose exec app curl -sk https://proxmox.local:8006/api2/json/version
```
If the request times out, check firewall rules between the Docker host and the
Proxmox node.

**"401 Unauthorized"** — the token ID or secret is incorrect, or the token
has been deleted. Regenerate the token in Proxmox → Datacenter → API Tokens.

**"403 Forbidden" on power operations** — the API token lacks `VM.PowerMgmt`.
Reassign the token to a role that includes that privilege (e.g. `PVEVMAdmin`).

**SSL certificate errors** — for self-signed certificates, set
`PROXMOX_VERIFY_SSL=false`. For production with a valid cert, set it to `true`.

**IP addresses not showing** — install and enable the QEMU Guest Agent inside
the VM and ensure **QEMU Guest Agent** is checked under VM → Options in the
Proxmox UI.
