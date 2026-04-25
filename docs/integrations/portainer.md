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
| BeyondTrust Password Safe (optional) | PAT can be stored there instead of in `.env` |

---

## Setup

### Step 1 — Create a Portainer Personal Access Token

1. Log in to Portainer → click your username (top right) → **My account**.
2. Scroll to **Access tokens** → **Add access token**.
3. Give it a name (e.g. `vm-dashboard`) and copy the token string.

### Step 2 — Enable and configure in the dashboard

**Option A — `.env` file**

```
PORTAINER_ENABLED=true
PORTAINER_URL=http://portainer.local:9000
PORTAINER_PAT_SECRET_TITLE=Portainer_PAT   # BeyondTrust secret title (if using BT)
PORTAINER_VERIFY_SSL=true                   # set false for self-signed certs
```

If you are **not** using BeyondTrust, store the token directly in Password Safe
and set `PORTAINER_PAT_SECRET_TITLE`. If you prefer plain `.env` storage,
extend the config to add a `PORTAINER_PAT` field (or keep it in Password Safe).

**Option B — Setup wizard / Settings → Integrations**

Toggle **Portainer** on in wizard Step 5 or **Settings → Integrations →
Portainer** and fill in the URL and PAT secret title fields.

### Step 3 — Verify

Open the dashboard — a **Containers** entry should appear in the navigation.
Click it to confirm the container list loads from your Portainer instance.

---

## Multiple Portainer instances (workgroups)

The dashboard supports two Portainer instances (workgroups) simultaneously.
Add a second instance with the `_HYDRA` suffix variables:

```
PORTAINER_URL_HYDRA=http://portainer-hydra.local:9000
PORTAINER_PAT_SECRET_TITLE_HYDRA=Portainer_PAT_Hydra
PORTAINER_VERIFY_SSL_HYDRA=true
```

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

The agent image and port are configurable:

```
PORTAINER_AGENT_IMAGE=portainer/agent:latest
PORTAINER_AGENT_PORT=9001
```

---

## Troubleshooting

**Containers tab is missing** — check that `PORTAINER_ENABLED=true` is set in
`.env` and that the stack was restarted after the change.

**"Connection refused" or timeout** — verify the Portainer URL is reachable from
inside the container: `docker compose exec app curl -Is "$PORTAINER_URL/api/status"`.

**"Unauthorized" error** — the PAT may have expired or been deleted. Regenerate
a new token in Portainer and update the `.env` value or Password Safe secret.

**SSL certificate errors** — for self-signed certificates, set
`PORTAINER_VERIFY_SSL=false`. For production, add your CA cert to the container's
trusted store via the Dockerfile.
