# Portainer CE Integration

## What is it?

The Portainer CE integration connects the dashboard to one or more
[Portainer Community Edition](https://www.portainer.io/) instances running on
your on-premises Docker hosts. It adds a **Containers** tab to the dashboard
that lets you see running containers, start/stop them, and deploy the Portainer
Agent to new VMs — all from the same UI you use to manage cloud resources.

The dashboard connects to Portainer over its REST API using a Personal Access
Token (PAT). No special network topology is required beyond the dashboard
container being able to reach the Portainer URL.

---

## Use cases

- **On-prem + cloud unified view** — see what's running on your local Docker
  hosts alongside AWS EC2 and Azure VMs, without switching tools.
- **Lab container management** — start and stop containers on lab servers
  without SSHing in.
- **Agent deployment** — when you spin up a new VM via the dashboard, the
  Portainer Agent install job appears alongside the VM creation job so you
  can onboard new hosts in one workflow.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Portainer CE 2.x or later | Self-hosted; community edition is free |
| Portainer API reachable from the dashboard container | Port 9000 (HTTP) or 9443 (HTTPS) |
| BeyondTrust Password Safe (optional) | PAT can be stored in Password Safe instead of the application database |

---

## Setup

### Step 1 — Create a Portainer Personal Access Token

1. Log in to Portainer → click your username (top right) → **My account**.
2. Scroll to **Access tokens** → **Add access token**.
3. Give it a name (e.g. `vm-dashboard`) and copy the token string.

### Step 2 — Enable and configure in the dashboard

**Option A — Setup wizard (first run)**

Toggle **Portainer** on in wizard Step 5 and fill in the fields.

**Option B — Settings → Integrations (after first run)**

Navigate to **Settings → Integrations → Portainer**, toggle it on, and fill in:

| Field | Example |
|---|---|
| Portainer URL | `http://portainer.local:9000` |
| PAT (or BeyondTrust secret title) | token string, or `Portainer_PAT` if using BT |
| Verify SSL | disable for self-signed certificates |

If you have BeyondTrust configured, enter the Password Safe secret title and the
dashboard will check out the PAT at runtime rather than storing it in the database.

### Step 3 — Verify

Open the dashboard — a **Containers** entry should appear in the navigation.
Click it to confirm the container list loads from your Portainer instance.

---

## Multiple Portainer instances (workgroups)

The dashboard supports two Portainer instances (workgroups) simultaneously.
Configure the second instance under **Settings → Integrations → Portainer →
Second instance (Hydra)** with its own URL, PAT, and SSL setting.

Both instances appear as separate workgroup sections in the Containers tab.

---

## What it enables in the dashboard

| Feature | Description |
|---|---|
| **Containers tab** | Lists all running containers from configured Portainer instances |
| **Start / Stop** | One-click container power toggle |
| **Agent deploy** | Install Portainer Agent on a new VM from the VM's detail page |
| **Workgroup filter** | Multiple Portainer instances shown as separate sections |

---

## Portainer Agent deployment

When you deploy a new VM via the dashboard (AWS EC2 or Azure VM), the post-deploy
workflow can automatically install the Portainer Agent on the VM:

1. The dashboard SSHes into the new VM using the BeyondTrust managed SSH key.
2. It installs Docker (if not present — runs `zypper install -y docker` on SLES,
   `apt-get install -y docker.io` on Debian/Ubuntu, etc.).
3. It pulls and runs `portainer/agent:latest` on port 9001.
4. The new agent appears in Portainer under **Environments** within ~30 seconds.

The agent image and port are configurable in **Settings → Integrations →
Portainer → Advanced**.

---

## Troubleshooting

**Containers tab is missing** — verify Portainer is toggled on in **Settings →
Integrations → Portainer** and that the stack was restarted after enabling it.

**"Connection refused" or timeout** — verify the Portainer URL is reachable from
inside the container: `docker compose exec app curl -Is <portainer-url>/api/status`.

**"Unauthorized" error** — the PAT may have expired or been deleted. Regenerate
a new token in Portainer and update it in **Settings → Integrations → Portainer**
(or update the Password Safe secret if you are using BeyondTrust).

**SSL certificate errors** — for self-signed certificates, set
`PORTAINER_VERIFY_SSL=false`. For production, add your CA cert to the container's
trusted store via the Dockerfile.
