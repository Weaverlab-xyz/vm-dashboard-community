# Portainer CE Integration

## What is it?

The Portainer CE integration connects the dashboard to a single
[Portainer Community Edition](https://www.portainer.io/) instance managing your
on-premises Docker hosts. It adds a **Containers** tab that lists running
containers, starts/stops them, and deploys containers or compose stacks — from the
same UI you use for cloud resources. (One Portainer instance can manage many Docker
hosts; each appears as its own environment/endpoint.)

There are two ways to get one:

- **Deploy a managed server** — the dashboard stands up Portainer CE for you on a
  GCE Container-Optimized-OS VM, bootstraps it, and wires up the connection. This is
  the same managed-service shape as the [Rancher node](rancher.md).
- **Connect your own** — point the dashboard at a Portainer server you already run,
  using a Personal Access Token.

Either way the dashboard talks to Portainer over its REST API. No special network
topology is required beyond the dashboard being able to reach the Portainer URL.

> **Kubernetes note.** Portainer manages **Docker hosts** here. The dashboard's
> Kubernetes management plane is **Rancher** — see [Kubernetes](../kubernetes.md).
> Earlier versions could install a Portainer Agent into clusters and onto new VMs;
> that path was removed when the management plane moved to Rancher.

---

## Use cases

- **On-prem + cloud unified view** — see what's running on your local Docker hosts
  alongside AWS EC2 and Azure VMs, without switching tools.
- **Lab container management** — start and stop containers on lab servers without
  SSHing in.
- **Disposable Portainer** — stand one up for a demo or a lab exercise, then tear it
  down when you're done.

---

## Option A — deploy a managed Portainer server

### What gets created

A single `portainer/portainer-ce` container on a **Container-Optimized-OS GCE VM**
with a public, source-restricted IP (the konlet mechanism, same as the Rancher node):

| Aspect | Detail |
|---|---|
| VM label | `purpose=portainer` (how the dashboard finds it) |
| Ports | **9443** HTTPS UI/API, **8000** Edge agent tunnel |
| Privileges | **Unprivileged**, no Docker socket — the server administers *remote* Docker hosts over the API, so it needs neither |
| State | `/data` bind-mounted from the VM's boot disk |
| TLS | self-signed certificate on 9443 |

> **The node is ephemeral.** The boot disk auto-deletes, so tearing the node down —
> or recreating it — wipes `/data`: users, environments and settings are all lost, and
> the external IP changes. It is a disposable-lab tool, not a durable Portainer.

### Prerequisites

| Requirement | Notes |
|---|---|
| A configured GCP project | The node is a GCE VM; set it under **Settings → GCP** |
| A region with a configured subnet | The node's subnet is regional — only configured regions are offered |
| Allowed source CIDRs | Who may reach 9443/8000. Fail-closed: see [Firewall](#firewall) |

### Deploy

1. Open **Containers → Portainer**. The **Managed Portainer server** panel is at
   the top.
2. Optionally pick a **Region** and **Zone**. Blank region keeps the node's current
   region (or the configured default); blank zone auto-picks the region's first
   available zone, falling back to a sibling zone if that one is capacity-exhausted.
3. Click **Deploy Portainer server**. You land on the job page.

The job runs on the durable worker (VM boot plus bootstrap outlasts a web timeout):

| Step | What happens |
|---|---|
| Configuring firewall | Auto-detects the dashboard's public egress IP, merges it with your CIDRs, applies the rule. **Fails fast** if the merged set is empty |
| Launching COS VM | Creates (or reuses/starts) the VM; relocates it if you picked a different region |
| Waiting for Portainer | Polls `GET /api/system/status` until it serves |
| Creating the admin user | `POST /api/users/admin/init` as `admin` |
| Minting an API token | Logs in and creates a personal access token |

On success the job **writes the connection settings for you** — `portainer_url`,
`portainer_pat`, and `portainer_verify_ssl` (off, because of the self-signed cert) —
so the Containers tab starts working with no Settings round-trip.

If you left **Admin password** blank the dashboard generates a 24-character one and
shows it once, on the Containers page (`Log in as admin / …`). Change it in Portainer
after first login.

### PRA Web Jump (optional)

By default the node's UI is reachable only from the source CIDRs you allow, and an
auto-generated admin password has to be shown on the Containers page so you can use
it. Ticking **Broker the Portainer UI via a PRA Web Jump** on the deploy form fixes
both:

- A `portainer-ui` **Web Jump** is created, so the UI opens from the PRA
  representative console — brokered and recorded — with no CIDR change for your own
  workstation.
- A Web Jump connects *through* a **Gateway**, so the source hitting the node is that
  host's egress IP. The dashboard ensures its managed Gateway host is up and
  **auto-allows that IP** as a `/32`. It re-checks on every deploy, because AWS/GCP
  gateway IPs are ephemeral. A *pre-existing* Gateway you run yourself can't be
  auto-detected — add its IP to `portainer_allowed_source_cidrs` manually.
- Pick a **Vault Account Group** and the admin credential is stored as a PRA Vault
  account and **injected at login** — the password is never displayed, and the job
  result says so instead of echoing it. Leave it blank for a plain (non-injected) Web
  Jump; the password is then shown as usual.

Provisioning runs after first-run bootstrap (the password has to exist to be vaulted)
and is **best-effort** — a PRA hiccup logs a warning and leaves the node deployed and
usable over its public IP. Jump Group, Gateway and Vault group default to the
`bt_*` settings when not chosen on the form.

Requires PRA to be configured (`bt_api_host`, `bt_client_id`, `bt_jumpoint_name`);
the fieldset stays hidden otherwise.

### Teardown

**Stop** on the node row removes the PRA Web Jump (when one exists), deletes the VM
and its firewall rule, then clears `portainer_url`, `portainer_pat` and the node's
other runtime config. Because the node is ephemeral, all Portainer state goes with it.

### Firewall

The node's ingress rule opens **tcp 9443 and 8000** to a merged source set:

- `portainer_allowed_source_cidrs` — your manual CSV.
- The dashboard's own public egress CIDR — auto-detected and saved on every deploy,
  because the worker bootstraps and polls the node over its public IP. If you egress
  from a proxy *pool*, set `portainer_dashboard_egress_cidr` to the pool's range by
  hand; detection will not clobber a broader range that already contains the detected IP.

It is **fail-closed**: an empty merged set opens nothing, and deletes any existing
rule. Set `gcp_portainer_allow_open` to open `0.0.0.0/0` when no CIDRs are set — a
deliberate opt-in. **Settings → Containers** shows the live merged allow-list.

---

## Option B — connect your own Portainer server

### Step 1 — Create a Personal Access Token

1. Log in to Portainer → click your username (top right) → **My account**.
2. Scroll to **Access tokens** → **Add access token**.
3. Give it a name (e.g. `vm-dashboard`) and copy the token string.

### Step 2 — Configure in the dashboard

**Setup wizard (first run)** — toggle **Portainer** on in wizard Step 5 and fill in
the fields. **After first run** — **Settings → Integrations → Portainer CE**:

| Field | Example |
|---|---|
| Portainer URL | `http://portainer.local:9000` |
| API Token (PAT) | the token string, or a vault reference (below) |
| Verify SSL | disable for self-signed certificates |

The token is stored encrypted in the application database. To keep it in an external
vault instead, enter a reference — `bt_safe://Portainer_PAT`,
`aws_sm://dashboard/portainer-pat`, `azure_kv://portainer-pat`, or
`gcp_sm://portainer-pat` — and the dashboard resolves it at runtime through the
secrets backend configured on **/secrets**.

Settings changes apply immediately — no `.env` edit or restart required. (Legacy
installs that kept the PAT in BeyondTrust Password Safe under the
`PORTAINER_PAT_SECRET_TITLE` secret title continue to work as a fallback when no
token is set here.)

### Step 3 — Verify

A **Containers** entry appears in the navigation; open it and confirm the container
list loads.

---

## What it enables in the dashboard

| Feature | Description |
|---|---|
| **Containers tab** | Lists containers from every environment on your Portainer instance |
| **Start / Stop** | One-click container power toggle |
| **Deploy** | Launch a container from an image, or a stack from a compose file |
| **Managed server** | Deploy / tear down a dashboard-run Portainer CE node (Option A) |

---

## Automating Portainer with Ansible

Config Management ships localhost playbooks that drive the Portainer API — see
[`examples/playbooks/portainer/`](../../examples/playbooks/portainer/): list
environments, create-or-update a compose stack, remove one, and prune a Docker host.

They need no per-run setup: whenever a Portainer server is configured (typed in below,
or written by a managed-node deploy) the dashboard injects `PORTAINER_URL`,
`PORTAINER_PAT` and `PORTAINER_VERIFY_SSL` into the Ansible runner as environment
variables — the same channel that carries the `PASSWORD_SAFE_*` credentials. The token
is added to the run's scrub set, so it can't leak into job output.

Because they're `hosts: localhost` plays that reach out over HTTPS, the target you pick
on the run form is irrelevant — nothing is installed on it.

## Configuration reference

| Key | Default | Purpose |
|---|---|---|
| `portainer_enabled` | `true` | Containers router + `/containers` page + the container warmer |
| `portainer_url` | `""` | Server URL; written by a managed deploy |
| `portainer_pat` | `""` | API token (encrypted, or a vault reference); written by a managed deploy |
| `portainer_verify_ssl` | `true` | Verify the server's TLS certificate; a managed deploy turns this off |
| `portainer_allowed_source_cidrs` | `""` | CSV of manual firewall sources; empty is fail-closed |
| `portainer_dashboard_egress_cidr` | `""` | The dashboard's own egress CIDR; auto-detected on deploy |
| `portainer_admin_password` | `""` | First-run admin password; blank auto-generates one |
| `portainer_ready_timeout_s` | `300` | How long the deploy waits for Portainer to serve |
| `gcp_portainer_image` | `portainer/portainer-ce:latest` | Server container image |
| `gcp_portainer_machine_type` | `e2-small` | VM size — Portainer is light |
| `gcp_portainer_name` | `portainer-server` | VM name |
| `gcp_portainer_zone` | `""` | Blank auto-picks a zone in the region |
| `gcp_portainer_boot_disk_gb` | `20` | COS boot disk (holds `/data`; auto-deletes) |
| `gcp_portainer_network_tag` | `portainer` | VM network tag = firewall target tag |
| `gcp_portainer_allow_open` | `false` | Open `0.0.0.0/0` when no CIDRs are set |
| `portainer_ui_web_jump_enabled` | `false` | Broker the UI via a PRA Web Jump (opt-in) |
| `portainer_ui_verify_certificate` | `false` | Web Jump TLS verification — off for the node's self-signed cert |
| `portainer_ui_jump_group` | `""` | Jump Group for the Web Jump; blank = `bt_jump_group_name` |
| `portainer_ui_jumpoint_name` | `""` | Gateway for the Web Jump; blank = `bt_jumpoint_name` |
| `portainer_ui_vault_account_group_id` | `""` | Vault account group the admin credential is stored in; blank = `bt_vault_account_group_id`, else the password is shown |
| `portainer_ui_jumpoint_cloud` | `gcp` | Which managed Gateway host brokers the UI; its egress IP is auto-allowed |
| `portainer_ui_jumpoint_egress_ip` | `""` | Captured Gateway egress IP (runtime-set; auto-added as a `/32`) |

---

## Troubleshooting

**Containers tab is missing** — verify Portainer is toggled on in **Settings →
Integrations → Portainer CE**. The flag applies immediately; no restart needed.

**"Portainer is not configured" card on the Containers page** — the URL or API token
is missing. Deploy a managed server, or fill both in under **Settings → Integrations
→ Portainer CE**.

**Deploy fails with "the Portainer node's firewall is closed"** — no allowed source
CIDRs, and the dashboard couldn't auto-detect its own egress IP. Set
`portainer_dashboard_egress_cidr` or `portainer_allowed_source_cidrs` in **Settings →
Containers** — or enable `gcp_portainer_allow_open` — then redeploy.

**Deploy finishes but reports "the node already had an admin user"** — Portainer only
allows first-run initialization while no admin exists, and closes that window shortly
after the container starts. This happens when redeploying onto a reused VM. The node
is running and usable; add an API token by hand in **Settings → Containers**.

**Deploy times out waiting for Portainer to start** — the VM is up but nothing
answered. Usually the firewall doesn't admit the dashboard's egress IP; check the
merged allow-list in **Settings → Containers**, then redeploy. Raise
`portainer_ready_timeout_s` if the image pull is simply slow.

**"Connection refused" or timeout** — verify the Portainer URL is reachable from
inside the container: `docker compose exec app curl -Isk <portainer-url>/api/system/status`.

**"Unauthorized" error** — the PAT may have expired or been deleted. Regenerate a
token in Portainer and update it in **Settings → Integrations → Portainer CE** (or
update the vault secret if you stored a reference).

**SSL certificate errors** — for self-signed certificates (including a managed node's)
turn off **Verify SSL certificate** in the Portainer panel. For production, add your
CA cert to the container's trusted store via the Dockerfile.
