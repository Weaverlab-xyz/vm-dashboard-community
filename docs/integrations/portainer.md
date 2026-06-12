# Portainer CE Integration

## What is it?

The Portainer CE integration connects the dashboard to a single
[Portainer Community Edition](https://www.portainer.io/) instance running on
your on-premises Docker hosts. It adds a **Containers** tab to the dashboard
that lets you see running containers, start/stop them, and deploy the Portainer
Agent to new VMs — all from the same UI you use to manage cloud resources.
(One Portainer instance can manage many Docker hosts — each host appears as
its own environment/endpoint in the Containers tab.)

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
| External vault (optional) | PAT can live in BeyondTrust Secrets Safe, AWS Secrets Manager, Azure Key Vault, or GCP Secret Manager instead of the application database |

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

Navigate to **Settings → Integrations → Portainer CE**, toggle it on, and fill in:

| Field | Example |
|---|---|
| Portainer URL | `http://portainer.local:9000` |
| API Token (PAT) | the token string, or a vault reference (see below) |
| Verify SSL | disable for self-signed certificates |

The token is stored encrypted in the application database. If you'd rather keep
it in an external vault, enter a reference instead of the raw token —
`bt_safe://Portainer_PAT`, `aws_sm://dashboard/portainer-pat`,
`azure_kv://portainer-pat`, or `gcp_sm://portainer-pat` — and the dashboard
resolves it at runtime through the secrets backend configured on **/secrets**.

Settings changes apply immediately — no `.env` edit or restart required.
(Legacy installs that kept the PAT in BeyondTrust Password Safe under the
`PORTAINER_PAT_SECRET_TITLE` secret title continue to work as a fallback when
no token is set here.)

### Step 3 — Verify

Open the dashboard — a **Containers** entry should appear in the navigation.
Click it to confirm the container list loads from your Portainer instance.

---

## What it enables in the dashboard

| Feature | Description |
|---|---|
| **Containers tab** | Lists containers from every environment on your Portainer instance |
| **Start / Stop** | One-click container power toggle |
| **Deploy** | Launch a container from an image, or a stack from a compose file |
| **Agent deploy** | Install Portainer Agent on a new VM from the VM's detail page |

---

## Portainer Agent deployment

When you deploy a new VM via the dashboard (AWS EC2 or Azure VM), the post-deploy
workflow can automatically install the Portainer Agent on the VM:

1. The dashboard SSHes into the new VM using the BeyondTrust managed SSH key.
2. It installs Docker (if not present — runs `zypper install -y docker` on SLES,
   `apt-get install -y docker.io` on Debian/Ubuntu, etc.).
3. It pulls and runs `portainer/agent:latest` on port 9001.
4. The new agent appears in Portainer under **Environments** within ~30 seconds.

The agent image and port are configurable via the `PORTAINER_AGENT_IMAGE` and
`PORTAINER_AGENT_PORT` environment variables.

---

## Troubleshooting

**Containers tab is missing** — verify Portainer is toggled on in **Settings →
Integrations → Portainer CE**. The flag applies immediately; no restart needed.

**"Portainer is not configured" card on the Containers page** — the URL or API
token is missing. Fill both in under **Settings → Integrations → Portainer CE**.

**"Connection refused" or timeout** — verify the Portainer URL is reachable from
inside the container: `docker compose exec app curl -Is <portainer-url>/api/status`.

**"Unauthorized" error** — the PAT may have expired or been deleted. Regenerate
a new token in Portainer and update it in **Settings → Integrations → Portainer CE**
(or update the vault secret if you stored a reference).

**SSL certificate errors** — for self-signed certificates, turn off **Verify SSL
certificate** in the Portainer panel. For production, add your CA cert to the
container's trusted store via the Dockerfile.
