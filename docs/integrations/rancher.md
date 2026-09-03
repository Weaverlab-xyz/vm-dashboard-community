# Rancher Kubernetes Management

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you have more than a couple of clusters and want one place to see them.

## What is it?

The Rancher integration gives the dashboard a **central Kubernetes management
plane**. Instead of standing up a whole Kubernetes cluster just to host
[Rancher](https://www.rancher.com/), the dashboard runs the Rancher server as a
**single privileged container on one VM** — the same lightweight
container-on-a-VM pattern it already uses for the BeyondTrust Gateway. The node
gets a **public, source-restricted IP**, and every Kubernetes cluster you manage
is *imported* into it.

**The node runs on AWS, Azure or GCP** — you pick which when you deploy it. That
choice is about where the management plane *lives*; the clusters it manages can be
anywhere, on any cloud or on-prem (see [below](#does-this-work-for-private-clusters-in-another-cloud)).

You deploy, view, and tear down the node from a **Kubernetes (Rancher)** tab on
the **Containers** page — the same place you manage Portainer, the Gateways and
cloud containers.

> **Single-container Rancher is intended for lab / demo / small-scale use**, the
> same as Rancher's own single-node Docker install. It is not a highly-available
> production topology (that would be Rancher on an HA cluster). The dashboard
> treats the node as **ephemeral** — see [Ephemeral node](#ephemeral-node).

---

## How it works

```
Operator ──(source-restricted 443, or optional PRA Web Jump)──▶ Rancher node (public IP)
Dashboard app ──(direct HTTPS v3 API, httpx)──────────────────▶ same node
Downstream cluster (any cloud / on-prem, PRIVATE) ──(cattle-cluster-agent egress 443)──▶ same node
```

- **The node** is one `rancher/rancher` container on one VM — no Helm, no cluster
  to build. How the container gets started is the only thing that differs per
  cloud; see [the node, per cloud](#the-node-per-cloud).
- **The dashboard** talks to the Rancher v3 API directly over HTTPS with an API
  token minted at first boot.
- **Downstream clusters** are *imported*: Rancher hands back a registration
  manifest whose `cattle-cluster-agent` **dials outbound** to the node's public
  URL. Because the connection is outbound-only, **private clusters on any cloud
  or on-prem can be managed** as long as they have egress to the node — no
  inbound firewall opening, no VPC peering.

### The node, per cloud

The container is the same everywhere: one privileged `rancher/rancher` on ports
80/443, restarted with the host, holding its state in `/var/lib/rancher`. What
differs is the machinery around it.

| | AWS | Azure | GCP |
|---|---|---|---|
| Host | EC2, ECS-optimized AL2023 AMI (Docker preinstalled) | Ubuntu 22.04 VM | Container-Optimized OS VM |
| Container started by | `docker run` from EC2 user-data | `docker run` from cloud-init | `gce-container-declaration` (konlet) |
| Default size | `t3.medium` | `Standard_B2s` | `e2-medium` |
| Ingress gate | a dedicated security group | a dedicated NSG on the NIC | a firewall rule targeting a network tag |
| Fail-closed means | every ingress rule revoked | the allow rule deleted | the firewall rule deleted |
| Public IP | ephemeral | **static** — survives a recreate | ephemeral |
| Needs an instance identity? | no | no | no |

The node is given **no instance profile, managed identity or service account**: it
talks to Rancher and to the clusters that dial it, never to the cloud's own API.

### Does this work for private clusters in another cloud?

**Yes** — and it does not matter which cloud the node itself is on. The imported
agent initiates the connection *to* the Rancher node, so the downstream cluster
only needs outbound reachability to the node's public `server-url`. EKS, AKS, GKE
and on-prem clusters all work the same way.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Kubernetes management enabled** | Toggle **Kubernetes** on under **Settings → Integrations** (`k8s_management_enabled`). This surfaces the Containers → Kubernetes (Rancher) tab. |
| **At least one cloud configured** | AWS, Azure or GCP credentials on **Settings** (or the setup wizard). You pick which one hosts the node on the deploy form; the imported clusters can live anywhere regardless. |
| **Permissions for that cloud** | **GCP** — `compute.instances.create`, `compute.firewalls.{get,create,update,delete}` and instance delete; `setup-gcp.sh` grants `roles/compute.admin`, which covers these. **AWS** — EC2 run/terminate plus security-group create/authorize/revoke/delete; `setup-aws.sh`'s `dashboard-app-policy` already grants all of it. **Azure** — `Contributor` on the resource group, which `setup-azure.sh` grants. |
| **A region with a configured subnet** | The node needs a public subnet with egress, and a subnet is regional on every cloud — so only regions that have one are offered. This is the same per-region config the Gateways use. |
| **A bootstrap password** | Set a Rancher bootstrap (first-run admin) password — see Setup. |
| **Allowed source CIDRs** | The firewall **fails closed**, but dashboard-provisioned clusters' egress IPs (and, when the Web Jump is on, the dashboard-managed Gateway's egress IP) are **added automatically** — see [Automatic firewall whitelisting](#automatic-firewall-whitelisting). You only add extra operator IPs and pre-existing operator Gateways here. |
| **A ≥ 4 GB machine type** | Rancher OOMs on shared-core types; the default `e2-medium` (4 GB) is the minimum. |

---

## Setup

### Step 1 — Enable Kubernetes management

**Settings → Integrations → Kubernetes** → toggle on. A **Kubernetes (Rancher)**
tab appears on the **Containers** page.

### Step 2 — Configure the node

Open **Settings → Kubernetes** and fill in the **Rancher management node**
section. The first group applies whichever cloud the node runs on:

| Field | Notes |
|---|---|
| **Bootstrap password** | First-run admin password. The API token is minted from it and stored encrypted; you never re-enter it. |
| **Allowed source CIDRs** | *Optional / additive.* Comma-separated CIDRs for the node's ingress rule (tcp 80/443). Dashboard-provisioned clusters and the dashboard-managed Web-Jump Gateway are added automatically ([details](#automatic-firewall-whitelisting)); use this only for extra operator IPs and pre-existing operator Gateways. If nothing is set here **and** nothing is auto-discovered, the node stays closed unless *Allow open* is ticked. The panel shows the effective allow-list read-only. |
| **Readiness timeout (s)** | Default 360. How long the deploy waits for Rancher to serve after boot; raise it for a cold image pull or a slow disk. |
| **Verify TLS certificate** | Leave off for the node's self-signed cert; turn on only if you've put a real cert on it. |
| **PRA Web Jump to the Rancher UI** | Opt-in zero-trust access — see [PRA Web Jump](#pra-web-jump-optional). |

Below that, a **Settings for cloud** picker switches between one field group per
cloud — container image, machine size, VM name, disk size, and that cloud's
*Allow open* opt-in. Each cloud keeps its own, so trying the node somewhere else
doesn't mean re-entering everything.

> **That picker is a view control, not a setting.** Where the node actually runs
> is chosen on the deploy form and recorded from where it landed — so there is one
> source of truth for it, and it is the node itself.

Sizes must clear Rancher's ~4 GB floor: `e2-medium` on GCP, `t3.medium` on AWS,
`Standard_B2s` on Azure. Smaller types OOM, and the GCP path refuses the obvious
ones outright.

Settings apply immediately — no restart.

### Step 3 — Deploy the node

On **Containers → Kubernetes (Rancher)**, choose the **Cloud** and optionally a
**Region** (and a **Zone**, on GCP only) — see [placement](#placement-cloud-and-region)
— fill the **PRA access** fieldset (see below), then click **Deploy Rancher node**.
This enqueues a background job (follow it at `/jobs/{job_id}`) that:

1. Creates/updates the source-restricted ingress rule (`<node>-allow-mgmt`, tcp 80/443)
   — a firewall rule on GCP, a security group on AWS, an NSG rule on Azure.
2. Launches the VM with the privileged Rancher container and a public IP.
3. Pins the node's public IP as Rancher's `server-url`.
4. Waits for Rancher to come up, then bootstraps it and mints the API token.
5. **Completes Rancher's first-run wizard** (see below) so the UI is ready to log into.
6. If a PRA Web Jump was chosen, provisions it + vaults the admin credential.
7. If Entitle registration is enabled, registers the node in Entitle (best-effort).

When the job completes, the tab shows the node with a **RUNNING** status, a
clickable **URL**, and an **Access** hint. Open it (from an allowlisted IP, or via
the [PRA Web Jump](#pra-web-jump-optional)) and log in as `admin` with your
password (see below).

#### PRA access (chosen at deploy)

Like the database and cloud-VM deploys, the deploy form offers **PRA pickers**
(populated from your PRA appliance) so you choose access at deploy/redeploy time:

- **Broker the Rancher UI via a PRA Web Jump** — tick to create a `rancher-ui`
  Web Jump. Then pick:
  - **Jump Group** and **Gateway** — where/through-what the Web Jump routes
    (blank → the `bt_*` / `rancher_ui_*` config defaults).
  - **Vault Account Group** — the PRA Vault group the generated **admin
    credential is stored in**, so PRA **injects** it into the Rancher login (you
    click the Web Jump and you're already logged in — the password never appears
    in the dashboard). Leave blank to skip vaulting (the password is surfaced in
    the *Access* hint instead).

The picks are persisted to config, so they're reused by later console opens.

#### Placement (cloud and region)

The deploy form offers a **Cloud**, a **Region** and — on GCP only — a **Zone**:

- **Cloud** — `aws`, `azure` or `gcp`. Defaults to wherever the node currently is.
- **Region** — the dropdown lists only regions of the chosen cloud that have a
  configured subnet (the default region plus any region added via **Settings →
  Multi-region** or a per-region sandbox run). The node needs a public subnet and a
  subnet is regional everywhere, so a region without one can't host it. Blank keeps
  the node's **current** region.
- **Zone** — GCP only, and optional. Blank uses the region's **first available**
  zone (which correctly skips regions with no `-a` zone, e.g. `us-east1` starts at
  `-b`). If that zone is out of capacity (`ZONE_RESOURCE_POOL_EXHAUSTED`), the
  deploy **automatically retries a sibling zone in the same region**. The field is
  hidden on the other clouds because an EC2 subnet already pins its availability
  zone and Azure has no zone in this shape.

Rancher stays a **single** management plane: redeploying to a different region **or
a different cloud relocates** the node — the old VM is deleted first so no duplicate
`rancher-server` is stranded. The node is ephemeral, so state re-bootstraps and
imported clusters must re-import. Where it landed is recorded
(`rancher_node_cloud` plus that cloud's zone key), so teardown and later bare
redeploys stay put instead of drifting back to a default.

### Automatic first-run

A stock Rancher greets its first visitor with a **"Welcome to Rancher — enter your
bootstrap password"** wizard (set a new admin password, accept the EULA, answer
telemetry). On a **fresh** deploy the dashboard completes that for you
(`rancher_auto_first_run`, on by default) using the API token it just minted:

- **Sets the admin password.** Rancher **forbids reusing the bootstrap password**
  as the new admin password (*"must not be the same as the current password"*), so
  a distinct one is required. Set `rancher_admin_password` (≥12 chars) to choose it,
  **or leave it blank and the dashboard auto-generates a strong one**, stores it
  encrypted. When you chose a **Vault Account Group** at deploy, that credential is
  **stored in the PRA Vault** and injected by the Web Jump (never shown); otherwise
  it is **surfaced** in the Containers → Rancher panel's *Access* line + the job result.
- **Accepts the EULA and opts out of telemetry**, and clears the `first-login`
  flag — best-effort, version-tolerant.

The result: the operator opens the URL and lands straight on the cluster list.
**Log in as `admin`** with the password shown in the *Access* hint (change the
auto-generated one after first login). This runs **only on a fresh deploy** — a
reused/already-set-up node is left untouched; a generated password is cleared on
teardown so the next fresh deploy makes a new one. If it can't complete, the deploy
still succeeds and you finish the wizard by hand once (the job note says what
happened). Turn it off with `rancher_auto_first_run=false` for the manual wizard.

---

## Importing clusters

Two paths, depending on whether the dashboard already holds a kubeconfig for the
target cluster.

### Clusters the dashboard manages

For a cluster you provisioned or registered through the dashboard (it has a
stored kubeconfig), use the **Kubernetes** page's management action, or the
management-plane job. The dashboard creates the import in Rancher and **applies
the registration manifest for you** — the cluster shows up **Active** in Rancher
within a minute, and its dashboard row links straight to the Rancher cluster view.

### External / private clusters

For a cluster the dashboard doesn't have a kubeconfig for (e.g. a private cluster
on another cloud or on-prem), use **Import a cluster** on the Rancher tab:

1. Enter a name and click **Import**.
2. The dashboard creates the import in Rancher and returns a **`kubectl apply`
   command**.
3. Run that command against the target cluster's kubeconfig.

The applied `cattle-cluster-agent` dials out to the node and the cluster goes
Active. This is the standard Rancher import flow — the only requirement is
egress from the cluster to the node's public URL.

---

## Automatic firewall whitelisting

Private clusters egress through a NAT, so their public source IP isn't knowable
until the cluster exists — which made the "Allowed source CIDRs" field a chicken-
and-egg problem. The dashboard now manages the allow-list for you:

- **The dashboard itself** — the dashboard bootstraps the node and mints its API
  token over the node's **public IP**, so the dashboard's *own* egress IP must be
  allowed or the deploy can't reach the node it just launched (the readiness poll
  would time out). On deploy the dashboard **auto-detects its public egress IP**
  (best-effort, via a plain-HTTP IP-echo) and adds it as a `/32`. If detection can't
  reach an echo service — e.g. behind a TLS-inspecting corporate proxy — set
  `rancher_dashboard_egress_cidr` manually. If the firewall would end up **fully
  closed**, the deploy now **fails fast** with that instruction instead of burning
  the readiness timeout. **Corp proxy pools:** proxies like Cloudflare WARP egress
  from a **pool** of IPs (consecutive requests can leave from different addresses),
  so a single detected `/32` isn't reliable there — set the pool's CIDR (e.g.
  `104.28.182.0/24`) in `rancher_dashboard_egress_cidr`; detection keeps a stored
  CIDR that already contains the detected IP instead of clobbering it.
- **API runner** — when `rancher_api_transport=runner` (see
  [Corp TLS inspection](#corp-tls-inspection-api-transport)), the runner's own source
  range (`rancher_runner_source_cidr`) is auto-added so its internal traffic is
  admitted — ingress rules apply to internal traffic on all three clouds. Private
  RFC1918 range, so no public exposure.
- **Provisioned clusters** — each dashboard-provisioned cluster (EKS/AKS/GKE) is
  given a **stable, reserved egress IP** (an Elastic IP on AWS, a reserved Cloud
  NAT IP on GCP, a static NAT-gateway IP on Azure). The provision job captures it
  (module output `nat_public_ip` → `k8s_clusters.egress_ip`) and adds it to the
  node firewall as a `/32`. Decommissioning the cluster removes it again.
- **Web-Jump Gateways** — when the [PRA Web Jump](#pra-web-jump-optional) is
  enabled, a `/32` is added for **every gateway the dashboard deployed** in that cloud:
  the shared managed host *and* any you added on **Containers → Gateways**. A Web Jump
  reaches the node **through a Gateway**, so this — not the PRA appliance IP — is the
  source the firewall must allow, and since all of a cloud's gateway hosts join one PRA
  Gateway *cluster*, PRA may broker a given session through any node in it: allowing
  only the shared host's IP left a session brokered by another node blocked.
  `rancher_ui_jumpoint_cloud` (default `gcp`) picks which cloud's gateways broker the
  UI. It is independent of where the node runs — a Gateway in any cloud can reach a
  public node in any other — but keeping them on the same cloud is the simplest setup. A provisioned Web Jump also holds a reference on the
  shared gateway, so the idle teardown can't reclaim its broker.
- **Manual CIDRs** — `rancher_allowed_source_cidrs` is still honoured and **added
  on top**, for extra operator/human IPs and for **pre-existing operator Gateways**
  (a Gateway the dashboard didn't provision has an egress IP the dashboard can't
  learn — add it here).

The effective set is recomputed and re-applied idempotently on every relevant
event: node deploy, cluster provision, cluster import, cluster decommission, Web Jump
enable, and every **gateway deploy or teardown**. It stays **fail-closed** — if there
are no manual CIDRs, no provisioned clusters, and no captured Gateway IP, the node is
not opened (unless *Allow open* is ticked). The **Settings → Kubernetes** panel shows
the computed allow-list read-only.

**What "closed" means, per cloud.** The merged set is identical everywhere; only the
mechanism differs, because the three clouds do not offer the same primitive:

| Cloud | Open | Closed |
|---|---|---|
| GCP | a firewall rule targeting the node's network tag | the rule is **deleted** |
| AWS | a dedicated security group on the node's ENI | **every ingress permission is revoked** — a security group in use by a running instance cannot be deleted |
| Azure | one allow rule in a dedicated NSG on the node's NIC | the **rule** is deleted; the NSG stays (it is attached to a live NIC, and a Standard public IP denies all inbound without a rule anyway) |

In every case the node ends up unreachable, which is what the deploy checks before it
bothers polling for readiness.

All three dashboard-managed gateway hosts expose a knowable egress IP: GCP and AWS
via the host's public IP, and Azure via a **Standard, secure-by-default public IP**
on the gateway VM's NIC (Standard IPs block all inbound unless an NSG allows it, so
this is egress-only — no ingress path). The AWS/GCP gateway IPs are ephemeral and
re-captured on each ensure and recorded per-gateway (`gateways.egress_ip`); the Azure
one is static. A gateway that is torn down has its `/32` dropped from the rule.

**Limitations.** A **pre-existing operator Gateway** (one the dashboard didn't
provision) has an egress IP the dashboard can't learn — add it to
`rancher_allowed_source_cidrs` manually. Registered (not dashboard-provisioned)
clusters likewise have no captured egress IP and must be added manually.

---

## Corp TLS inspection (API transport)

Corporate networks that **TLS-inspect** outbound traffic (e.g. Cloudflare
Gateway/WARP) verify the *origin's* certificate at the proxy — and the Rancher
node ships a **self-signed cert**, so the proxy kills every HTTPS handshake to it
in transit. The dashboard's `verify=False` can't help: the block happens at the
proxy, not the client. The symptom is a deploy that fails with *"Rancher IS up …
but the HTTPS handshake is being terminated in transit"* (the readiness probe
falls back to plain-HTTP `/ping` to detect exactly this), while `curl -k` to the
node dies after ClientHello.

Two ways out:

1. **Proxy exception** — add a *Do Not Inspect* rule for the node's IP (or your
   GCP ranges) in the proxy policy. Zero dashboard changes, but the node's IP is
   ephemeral, and you may not control corp policy.
2. **`rancher_api_transport = runner`** — the dashboard executes every Rancher
   API call (readiness, bootstrap, server-url pin, cluster import/delete) as
   `curl` inside a **one-shot in-cloud job**, which egresses from the cloud with
   no inspecting proxy in the path — the same corp-CA-dodging pattern as the
   Ansible/k8s cloud runners. The job targets the node's **internal IP**
   (`rancher_internal_url`, captured at deploy), so it needs network reach to it.

   **The runner runs in the node's own cloud** — it is not configured separately.
   That is forced by what it is for: reaching a private address inside the node's
   own network. A GCP node gets a Cloud Run job, an AWS node an ECS Fargate task in
   the node's VPC, an Azure node an ACI container group on a VNet-delegated subnet.
   Each reuses the k8s runner's existing configuration for that cloud, so a runner
   install needs nothing new. Set `rancher_runner_source_cidr` to the runner's
   source range and it is auto-merged into the node's allow-list while the transport
   is `runner`.

   On **GCP** specifically, VPC reach needs **either** of:
   - **Direct VPC egress (recommended)** — `gcp_run_network` +
     `gcp_run_subnetwork`: the job's NIC lands straight in the subnet. No
     standing infrastructure or cost, and immune to the Serverless-VPC-Access
     connector's shared-core zonal stockouts (`ZONE_RESOURCE_POOL_EXHAUSTED`
     killed connector creation across three `us-central1` zones when this was
     validated live). Set `rancher_runner_source_cidr` to the subnet's CIDR.
   - **Serverless VPC Access connector** — `gcp_ansible_vpc_connector` (a
     standing `/28` connector, ~$10-15/mo). Set `rancher_runner_source_cidr` to
     the connector's `/28`.

   Plus the k8s runner's base GCP knobs: `gcp_project_id` and `gcp_region` (or
   `gcp_ansible_cloud_run_region`). The runner fails fast naming the exact keys when
   neither VPC option is configured.

   On **AWS** the task is pinned to the node's region and that region's runner
   subnet (`ansible_ecs_subnet_id`), because a task in another VPC has no route to
   the node's private IP — and the failure is a dropped SYN, not an error. On
   **Azure** the container group needs a VNet-**delegated** subnet in the node's VNet
   (`ansible_aci_subnet_id`, falling back to `azure_aci_subnet_id`); without one it
   runs with a public address and cannot route to the node at all.

   Request payloads (API token, bootstrap password) travel to the job as a curl
   config over stdin — never in the container's argv. Note each API call costs a
   Cloud Run job cold-start (~20-40 s), which is fine for the deploy/import flows
   this covers.

**Downstream clusters are unaffected** either way — cattle-cluster-agents dial out
from their cloud NAT, not through your corp proxy. The Rancher **UI** in your
browser rides the same inspected path though: if the proxy blocks the self-signed
UI too, use the [PRA Web Jump](#pra-web-jump-optional) (the Gateway egresses from
the cloud, cleanly) or a proxy exception.

---

## Entitle registration

If **Entitle resource registration** is enabled
(`entitle_registration_enabled`), the node auto-registers as an Entitle
**Rancher** integration at the end of the deploy job, so users can request
just-in-time Rancher RBAC through Entitle. You can also register/deregister
manually:

```
POST /api/k8s/rancher/entitle-register   {"action": "register"}   # or "deregister"
```

Because the node is publicly reachable, Entitle's cloud connects to it directly
(no agent token). For tenants who lock the node behind CIDRs that Entitle can't
traverse, set `entitle_rancher_private = true` to attach the shared Entitle agent
token instead. See the [Entitle guide](entitle.md) for enabling
resource registration.

---

## PRA Web Jump (optional)

The node is reachable directly at its source-restricted URL, so the BeyondTrust
PRA Web Jump is **off by default**. Enable **PRA Web Jump to the Rancher UI**
(`rancher_ui_web_jump_enabled`) to *also* broker the UI through the PRA
representative console — brokered, session-recorded access. It requires PRA to be
configured (Gateway + Jump Group). This is independent of the Entitle JIT RBAC
grant, which continues to work either way.

When enabled, the dashboard ensures its managed Gateway host is up, captures its
egress IP, and adds it to the node firewall automatically (see [Automatic firewall
whitelisting](#automatic-firewall-whitelisting)) — so you don't pre-configure that
address. `rancher_ui_jumpoint_cloud` (default `gcp`) selects which dashboard-managed
Gateway brokers the UI. If you instead point the Web Jump at a **pre-existing**
operator Gateway, add that Gateway host's egress IP to `rancher_allowed_source_cidrs`
manually (the dashboard can't discover an IP for a host it didn't provision).

---

## Ephemeral node

The node is deliberately **disposable**: its boot disk is deleted with the VM, so
`/var/lib/rancher` does not survive a recreate.

- While the VM is alive, redeploying reuses it (state is preserved).
- **Recreating the node wipes `/var/lib/rancher`.** Rancher must re-bootstrap, and
  every imported cluster must be re-imported.

Whether the *address* survives depends on the cloud, and that changes how much
re-importing you face:

| Cloud | Public IP across a recreate | Consequence |
|---|---|---|
| GCP | ephemeral — **changes** | the agents were dialing the old IP, so every cluster re-imports |
| AWS | auto-assigned — **changes** | same |
| Azure | Standard SKU, and a Standard IP must be Static — **kept** | the `server-url` still resolves, so re-import is only needed because the state is gone, not because the address moved |

This trade-off keeps the node cheap and simple for lab/demo use. On GCP and AWS you
could reserve a static address to close the gap; on Azure you already have one.

---

## Teardown

On the Rancher tab, click **Stop** on the node. This enqueues a teardown job that:

1. Refuses (unless forced) if clusters are still imported — it warns you they'll
   be orphaned. The tab's confirm dialog forces past this.
2. Deletes the VM and its ingress rule — plus, on AWS, reclaims the node's security
   group once the terminating instance releases it, and on Azure the NIC, public IP
   and NSG the VM owned.
3. Deregisters the node from Entitle and removes the PRA Web Jump (if either was
   configured).
4. Clears the node's runtime config so a fresh deploy re-bootstraps cleanly.

Decommissioning an individual imported cluster (from the Kubernetes page) removes
just that cluster's import from Rancher — it doesn't touch the node.

---

## Configuration reference

All keys are set via **Settings** (encrypted in the application database) and
apply immediately.

| Key | Default | Purpose |
|---|---|---|
| `k8s_management_enabled` | `false` | Master toggle; surfaces the Rancher tab |
| `rancher_bootstrap_password` | — | First-run bootstrap password (secret; transient — Rancher requires the admin password to differ). ≥12 chars |
| `rancher_admin_password` | `""` | Admin UI password for auto first-run (secret); blank = auto-generate a distinct one, surfaced in the panel + job result |
| `rancher_auto_first_run` | `true` | Auto-complete Rancher's first-run wizard on a fresh deploy (password + EULA + telemetry) |
| `rancher_node_cloud` | `gcp` | `aws` \| `azure` \| `gcp` — which cloud hosts the node. Picked on the deploy form and rewritten to where it actually landed, so teardown and bare redeploys stay put. Defaults to `gcp` because every node deployed before this key existed is a GCE VM |
| `rancher_allowed_source_cidrs` | `""` | *Additive* manual CIDRs (tcp 80/443); the dashboard's own egress, provisioned clusters + the Web-Jump Gateway are auto-added. Empty + nothing auto-discovered = closed |
| `rancher_dashboard_egress_cidr` | (runtime) | The dashboard's own public egress IP/CIDR, auto-detected + persisted on deploy so the worker can reach the node's public IP. Behind a corp proxy pool set the pool's CIDR — a stored CIDR containing the detected IP is kept, not clobbered. Bare IP → `/32` |
| `rancher_ready_timeout_s` | `360` | Seconds the deploy waits for Rancher to serve after boot; raise for slow disks / large images |
| `rancher_api_transport` | `direct` | `direct` \| `runner` — run the Rancher API calls as curl in a one-shot job **in the node's own cloud** when this network's TLS inspection blocks the node's self-signed cert ([details](#corp-tls-inspection-api-transport)) |
| `rancher_internal_url` | (runtime) | `https://<node internal IP>` captured at deploy — what the runner transport dials |
| `rancher_runner_source_cidr` | `""` | The runner's source range (the direct-egress subnet's CIDR, the connector's `/28`, the ECS runner subnet, or the ACI delegated subnet); auto-added to the node's ingress while the transport is `runner` |
| `gcp_run_network` / `gcp_run_subnetwork` | `""` | Direct VPC egress for Cloud Run runner jobs (preferred over `gcp_ansible_vpc_connector`; no standing infra) |
| `rancher_verify_tls` | `false` | Verify the node's cert on API calls |
| `rancher_server_url` | (runtime) | Set to `https://<node IP>` by the deploy job |
| `rancher_api_token` | (runtime) | Minted at bootstrap (secret) |
| `rancher_ui_web_jump_enabled` | `false` | Opt-in PRA Web Jump broker for the UI |
| `rancher_ui_verify_certificate` | `false` | Web Jump cert verification |
| `rancher_ui_jumpoint_cloud` | `gcp` | Which dashboard-managed Gateway host brokers the UI (`gcp`\|`aws`\|`azure`); its egress IP is auto-whitelisted |
| `rancher_ui_jumpoint_egress_ip` | (runtime) | Captured egress IP of the SHARED Web-Jump Gateway (auto-added to the firewall). Gateways you deploy yourself come from the gateway registry, so every cluster node is allowed |
| `rancher_ui_vault_account_group_id` | `""` | PRA Vault account group (numeric id) the admin credential is vaulted into for Web-Jump injection; usually chosen per-deploy |
| `rancher_ui_vault_account_id` | (runtime) | PRA Vault account id created for the admin credential; cleared on teardown |
| `entitle_rancher_private` | `false` | Attach the Entitle agent token (node not reachable from Entitle's cloud) |

### Per-cloud node keys

One group per cloud, all optional — the defaults are usable. Only the group for the
cloud the node runs on has any effect, which is why each cloud can keep its own.

| Key | Default | Purpose |
|---|---|---|
| `gcp_rancher_image` / `aws_rancher_image` / `azure_rancher_image` | `rancher/rancher:latest` | Rancher container image. Pin a version for reproducibility |
| `gcp_rancher_machine_type` | `e2-medium` | GCE size (≥ 4 GB enforced — the launcher refuses the obvious too-small types) |
| `aws_rancher_instance_type` | `t3.medium` | EC2 size. `t3.small` (2 GB) OOMs |
| `azure_rancher_vm_size` | `Standard_B2s` | Azure size. `Standard_B1s` (1 GB) OOMs |
| `gcp_rancher_name` / `aws_rancher_name` / `azure_rancher_name` | `rancher-server` | VM (or instance) name, and the base name of its ingress rule (`<name>-allow-mgmt`) |
| `gcp_rancher_boot_disk_gb` / `aws_rancher_boot_disk_gb` / `azure_rancher_boot_disk_gb` | `30` | Boot / root / OS disk size. Holds `/var/lib/rancher` and is deleted with the VM |
| `gcp_rancher_allow_open` / `aws_rancher_allow_open` / `azure_rancher_allow_open` | `false` | Open `0.0.0.0/0` on that cloud when no CIDRs are set (discouraged) |
| `gcp_rancher_network_tag` | `rancher` | GCE network tag = the firewall rule's target. No analogue on AWS/Azure, where a dedicated security group / NSG *is* the scope |
| `gcp_rancher_zone` | `""` | Zone the node runs in. Overwritten on deploy with the **actual** launched zone. Blank → the region's first available zone. Pick a region/zone per-deploy on the Rancher tab |
| `aws_rancher_zone` | (runtime) | **Recorded, not chosen** — the availability zone the node landed in. The subnet pins it, so there is nothing to pick |
| `azure_rancher_zone` | (runtime) | **Recorded, not chosen** — the location the node landed in, so a bare redeploy stays there |

---

## Troubleshooting

**Kubernetes (Rancher) tab is missing** — enable **Kubernetes** under **Settings
→ Integrations**. The flag applies immediately.

**"Rancher node isn't configured" card** — the node's cloud has no credentials, or
no Rancher bootstrap password is set. Add both under **Settings**.

**Deploy fails: "cannot be placed in \<region\>: … is not set for that region"** —
the chosen region has no configured subnet (and, on AWS, no VPC). That is deliberate:
falling back would put the node in the *default* region's network while the form, the
job and the row all said otherwise. Run that cloud's sandbox setup for the region, or
add a per-region config under **Settings → Multi-region**.

**Deploy job fails with a permission error** — the node's cloud is missing a
permission. **GCP**: `compute.instances.create` or `compute.firewalls.*` — re-run
`setup-gcp.sh` or grant `roles/compute.admin`. **AWS**: EC2 run/terminate or
security-group authorize/revoke — re-run `setup-aws.sh` to refresh
`dashboard-app-policy`. **Azure**: `Contributor` on the resource group — re-run
`setup-azure.sh`.

**Node is RUNNING but the URL won't load** — the node is closed. Set
`rancher_allowed_source_cidrs` to include your IP (it fails closed by design) and
redeploy to patch the rule. Check what is actually allowed with
`GET /api/containers/rancher/firewall`, or the read-only allow-list on
**Settings → Kubernetes**.

**Deploy job fails waiting for Rancher** — two common causes, and the error names
both. (1) **The node is up but the dashboard's egress IP isn't in the firewall** —
the dashboard talks to the node over its public IP, so its own egress must be
allowed. Auto-detection usually handles this; if it's blocked (e.g. a TLS-inspecting
proxy), set `rancher_dashboard_egress_cidr` and redeploy. Compare the node's firewall
`sourceRanges` (or `GET /api/containers/rancher/firewall`) against the dashboard host's
public egress IP. (2) **Rancher hasn't come up yet** — it needs 1–3 minutes (longer on
a cold image pull / slow disk); raise `rancher_ready_timeout_s` and check the VM's
serial console / the container's logs in GCP (`google-logging-enabled` is on).

**Deploy job fails immediately: "firewall is closed"** — no source CIDRs were set
and the dashboard couldn't auto-detect its own egress IP, so opening the firewall
would leave the node unreachable. Set `rancher_dashboard_egress_cidr` or
`rancher_allowed_source_cidrs` (or enable *Allow open*) and redeploy.

**"Rancher IS up … but the HTTPS handshake is being terminated in transit"** —
this network TLS-inspects and rejects the node's self-signed cert at the proxy
(plain-HTTP `/ping` answered, so the node itself is fine). Set
`rancher_api_transport=runner` and redeploy, or add a proxy *Do Not Inspect*
exception for the node — see [Corp TLS inspection](#corp-tls-inspection-api-transport).

**Readiness flip-flops / works one minute, times out the next** — a corp proxy
pool (e.g. Cloudflare WARP) egresses from multiple IPs while the firewall pins one
`/32`. Set the pool's CIDR in `rancher_dashboard_egress_cidr` (e.g.
`104.28.182.0/24`); detection keeps a containing CIDR intact.

**Machine type rejected** — GCE types under 4 GB (`e2-micro`, `e2-small`, …) are
refused outright; use `e2-medium` or larger. On AWS and Azure the API accepts a small
size and Rancher then OOMs instead, which looks like a readiness timeout — use at
least `t3.medium` or `Standard_B2s`.

**Azure: the node is RUNNING but nothing answers** — an Azure VM with a Standard
public IP and no NSG rule denies *every* inbound packet, so this looks identical to a
closed allow-list. Confirm the node's NSG (`<node>-allow-mgmt`) exists and carries an
`allow-mgmt` inbound rule; a deploy whose ingress step failed leaves the VM up and
unreachable.

**AWS: the VPC won't delete after a teardown** — the node's security group is
unreferenced but still present, and an orphaned group blocks a VPC delete with an
opaque `DependencyViolation`. Teardown reclaims it once the instance releases its
ENI, and the sandbox rollback sweeps any `*-allow-mgmt` group it finds; delete it by
hand if both were interrupted.

**Imported cluster stays "Pending" in Rancher** — the `cattle-cluster-agent`
can't reach the node. Confirm the downstream cluster has egress to the node's
public URL and that the node's firewall allows the cluster's egress IP.

**Everything broke after a stop/recreate** — expected for the ephemeral node: the
IP changed and state was wiped. Redeploy, then re-import the clusters. See
[Ephemeral node](#ephemeral-node).
