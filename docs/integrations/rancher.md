# Rancher Kubernetes Management

## What is it?

The Rancher integration gives the dashboard a **central Kubernetes management
plane**. Instead of standing up a whole Kubernetes cluster just to host
[Rancher](https://www.rancher.com/), the dashboard runs the Rancher server as a
**single privileged container on a Google Compute Engine (GCE) VM using
Container-Optimized OS (COS)** — the same lightweight container-on-a-VM pattern
the dashboard already uses for the BeyondTrust Jumpoint. The node gets a
**public, source-restricted IP**, and every Kubernetes cluster you manage is
*imported* into it.

You deploy, view, and tear down the node from a **Kubernetes (Rancher)** tab on
the **Containers** page — the same place you manage Portainer and cloud
containers.

> **Single-container Rancher is intended for lab / demo / small-scale use**, the
> same as Rancher's own single-node Docker install. It is not a highly-available
> production topology (that would be Rancher on an HA cluster). The dashboard
> treats the node as **ephemeral** — see [Ephemeral node](#ephemeral-node).

---

## How it works

```
Operator ──(source-restricted 443, or optional PRA Web Jump)──▶ GCE COS Rancher node (public IP)
Dashboard app ──(direct HTTPS v3 API, httpx)──────────────────▶ same node
Downstream cluster (any cloud / on-prem, PRIVATE) ──(cattle-cluster-agent egress 443)──▶ same node
```

- **The node** is one `rancher/rancher` container on a COS VM, launched via the
  GCE container-declaration (konlet) metadata — no Helm, no cluster to build.
- **The dashboard** talks to the Rancher v3 API directly over HTTPS with an API
  token minted at first boot.
- **Downstream clusters** are *imported*: Rancher hands back a registration
  manifest whose `cattle-cluster-agent` **dials outbound** to the node's public
  URL. Because the connection is outbound-only, **private clusters on any cloud
  or on-prem can be managed** as long as they have egress to the node — no
  inbound firewall opening, no VPC peering.

### Does this work for private clusters that aren't in GCP?

**Yes.** The imported agent initiates the connection *to* the Rancher node, so
the downstream cluster only needs outbound reachability to the node's public
`server-url`. EKS, AKS, GKE, and on-prem clusters all work the same way.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Kubernetes management enabled** | Toggle **Kubernetes** on under **Settings → Integrations** (`k8s_management_enabled`). This surfaces the Containers → Kubernetes (Rancher) tab. |
| **GCP configured** | A GCP project + service-account JSON on **Settings → GCP** (or the setup wizard). The node always runs in GCP, regardless of where the imported clusters live. |
| **Service-account IAM** | The dashboard SA needs `compute.instances.create`, `compute.firewalls.{get,create,update,delete}`, and instance delete. `scripts/sandbox/Linux/setup-gcp.sh` grants `roles/compute.admin`, which covers these. |
| **A bootstrap password** | Set a Rancher bootstrap (first-run admin) password — see Setup. |
| **Allowed source CIDRs** | The firewall **fails closed**, but dashboard-provisioned clusters' egress IPs (and, when the Web Jump is on, the dashboard-managed Jumpoint's egress IP) are **added automatically** — see [Automatic firewall whitelisting](#automatic-firewall-whitelisting). You only add extra operator IPs and pre-existing operator Jumpoints here. |
| **A ≥ 4 GB machine type** | Rancher OOMs on shared-core types; the default `e2-medium` (4 GB) is the minimum. |

---

## Setup

### Step 1 — Enable Kubernetes management

**Settings → Integrations → Kubernetes** → toggle on. A **Kubernetes (Rancher)**
tab appears on the **Containers** page.

### Step 2 — Configure the node

Open **Settings → Kubernetes** and fill in the **Rancher management node (GCE
COS)** section:

| Field | Notes |
|---|---|
| **Bootstrap password** | First-run admin password. The API token is minted from it and stored encrypted; you never re-enter it. |
| **Allowed source CIDRs** | *Optional / additive.* Comma-separated CIDRs for the GCE firewall (tcp 80/443). Dashboard-provisioned clusters and the dashboard-managed Web-Jump Jumpoint are added automatically ([details](#automatic-firewall-whitelisting)); use this only for extra operator IPs and pre-existing operator Jumpoints. If nothing is set here **and** nothing is auto-discovered, the firewall stays closed unless *Allow open* is ticked. The panel shows the effective allow-list read-only. |
| **Machine type** | Default `e2-medium` (4 GB). Bump to `e2-standard-2` if you'll import several clusters. |
| **Zone** | Blank → the configured GCP zone. |
| **Container image** | Default `rancher/rancher:latest`. Pin a version for reproducibility. |
| **Boot disk (GB)** | Default 30. Holds `/var/lib/rancher`. |
| **Allow open** | Opt-in to open `0.0.0.0/0` when no CIDRs are set — **not recommended** for a public privileged container. |
| **Verify TLS certificate** | Leave off for the node's self-signed cert; turn on only if you've put a real cert on it. |
| **PRA Web Jump to the Rancher UI** | Opt-in zero-trust access — see [PRA Web Jump](#pra-web-jump-optional). |

Settings apply immediately — no restart.

### Step 3 — Deploy the node

On **Containers → Kubernetes (Rancher)**, click **Deploy Rancher node**. This
enqueues a background job (follow it at `/jobs/{job_id}`) that:

1. Creates/updates the source-restricted firewall rule (`<node>-allow-mgmt`, tcp 80/443).
2. Launches the COS VM with the privileged Rancher container and an external IP.
3. Pins the node's public IP as Rancher's `server-url`.
4. Waits for Rancher to come up, then bootstraps it and mints the API token.
5. If Entitle registration is enabled, registers the node in Entitle (best-effort).

When the job completes, the tab shows the node with a **RUNNING** status and a
clickable **URL**. Open it (from an allowlisted IP) and log in as `admin` with
your bootstrap password.

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
- **API runner (VPC connector)** — when `rancher_api_transport=runner` (see
  [Corp TLS inspection](#corp-tls-inspection-api-transport)), the Cloud Run
  runner's VPC-connector range (`rancher_runner_source_cidr`) is auto-added so the
  runner's internal-IP traffic is admitted (GCE ingress rules apply to internal
  traffic too). Private RFC1918 range — no public exposure.
- **Provisioned clusters** — each dashboard-provisioned cluster (EKS/AKS/GKE) is
  given a **stable, reserved egress IP** (an Elastic IP on AWS, a reserved Cloud
  NAT IP on GCP, a static NAT-gateway IP on Azure). The provision job captures it
  (module output `nat_public_ip` → `k8s_clusters.egress_ip`) and adds it to the
  node firewall as a `/32`. Decommissioning the cluster removes it again.
- **Web-Jump Jumpoint** — when the [PRA Web Jump](#pra-web-jump-optional) is
  enabled, the dashboard-managed Jumpoint host's egress IP is captured and added
  as a `/32`. A Web Jump reaches the node **through a Jumpoint**, so this — not the
  PRA appliance IP — is the source the firewall must allow. `rancher_ui_jumpoint_cloud`
  (default `gcp`, same cloud as the node) picks which dashboard-managed Jumpoint
  brokers the UI.
- **Manual CIDRs** — `rancher_allowed_source_cidrs` is still honoured and **added
  on top**, for extra operator/human IPs and for **pre-existing operator Jumpoints**
  (a Jumpoint the dashboard didn't provision has an egress IP the dashboard can't
  learn — add it here).

The effective set is recomputed and re-applied idempotently on every relevant
event: node deploy, cluster provision, cluster import, cluster decommission, and
Web Jump enable. It stays **fail-closed** — if there are no manual CIDRs, no
provisioned clusters, and no captured Jumpoint IP, the firewall is not opened
(unless *Allow open* is ticked). The **Settings → Kubernetes** panel shows the
computed allow-list read-only.

All three dashboard-managed jumpoint hosts expose a knowable egress IP: GCP and AWS
via the host's public IP, and Azure via a **Standard, secure-by-default public IP**
on the jumpoint VM's NIC (Standard IPs block all inbound unless an NSG allows it, so
this is egress-only — no ingress path). The AWS/GCP jumpoint IPs are ephemeral and
re-captured on each ensure; the Azure one is static.

**Limitations.** A **pre-existing operator Jumpoint** (one the dashboard didn't
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
   `curl` inside a **one-shot GCP Cloud Run job**, which egresses from GCP with
   no inspecting proxy in the path — the same corp-CA-dodging pattern as the
   Ansible/k8s cloud runners. The job targets the node's **internal IP**
   (`rancher_internal_url`, captured at deploy) through the Cloud Run **VPC
   connector** (its egress is private-ranges-only). Requirements:
   - the k8s runner's GCP knobs: `gcp_project_id`, `gcp_region` (or
     `gcp_ansible_cloud_run_region`), and `gcp_ansible_vpc_connector`;
   - `rancher_runner_source_cidr` = the connector's `/28`, so the firewall admits
     the runner (auto-merged into the allow-list while the transport is `runner`).

   Request payloads (API token, bootstrap password) travel to the job as a curl
   config over stdin — never in the container's argv. Note each API call costs a
   Cloud Run job cold-start (~20-40 s), which is fine for the deploy/import flows
   this covers.

**Downstream clusters are unaffected** either way — cattle-cluster-agents dial out
from their cloud NAT, not through your corp proxy. The Rancher **UI** in your
browser rides the same inspected path though: if the proxy blocks the self-signed
UI too, use the [PRA Web Jump](#pra-web-jump-optional) (the Jumpoint egresses from
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
token instead. See the [Entitle guide](/docs/integrations/entitle) for enabling
resource registration.

---

## PRA Web Jump (optional)

The node is reachable directly at its source-restricted URL, so the BeyondTrust
PRA Web Jump is **off by default**. Enable **PRA Web Jump to the Rancher UI**
(`rancher_ui_web_jump_enabled`) to *also* broker the UI through the PRA
representative console — brokered, session-recorded access. It requires PRA to be
configured (Jumpoint + Jump Group). This is independent of the Entitle JIT RBAC
grant, which continues to work either way.

When enabled, the dashboard ensures its managed Jumpoint host is up, captures its
egress IP, and adds it to the node firewall automatically (see [Automatic firewall
whitelisting](#automatic-firewall-whitelisting)) — so you don't pre-configure that
address. `rancher_ui_jumpoint_cloud` (default `gcp`) selects which dashboard-managed
Jumpoint brokers the UI. If you instead point the Web Jump at a **pre-existing**
operator Jumpoint, add that Jumpoint host's egress IP to `rancher_allowed_source_cidrs`
manually (the dashboard can't discover an IP for a host it didn't provision).

---

## Ephemeral node

The node is deliberately **disposable**: it uses an **ephemeral external IP** and
an **auto-delete boot disk**.

- While the VM is alive, redeploying reuses it (state is preserved).
- **Stopping/recreating the node changes its IP and wipes `/var/lib/rancher`.**
  Rancher must re-bootstrap, and every imported cluster must be re-imported
  (their agents were dialing the old IP).

This trade-off keeps the node cheap and simple for lab/demo use. If you need the
node to survive restarts, that's a future enhancement (reserve a static IP +
mount a persistent disk).

---

## Teardown

On the Rancher tab, click **Stop** on the node. This enqueues a teardown job that:

1. Refuses (unless forced) if clusters are still imported — it warns you they'll
   be orphaned. The tab's confirm dialog forces past this.
2. Deletes the VM and its firewall rule.
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
| `rancher_bootstrap_password` | — | First-run admin password (secret) |
| `rancher_allowed_source_cidrs` | `""` | *Additive* manual CIDRs (tcp 80/443); the dashboard's own egress, provisioned clusters + the Web-Jump Jumpoint are auto-added. Empty + nothing auto-discovered = closed |
| `rancher_dashboard_egress_cidr` | (runtime) | The dashboard's own public egress IP/CIDR, auto-detected + persisted on deploy so the worker can reach the node's public IP. Behind a corp proxy pool set the pool's CIDR — a stored CIDR containing the detected IP is kept, not clobbered. Bare IP → `/32` |
| `rancher_ready_timeout_s` | `360` | Seconds the deploy waits for Rancher to serve after boot; raise for slow disks / large images |
| `rancher_api_transport` | `direct` | `direct` \| `runner` — run the Rancher API calls as curl in a GCP Cloud Run job when this network's TLS inspection blocks the node's self-signed cert ([details](#corp-tls-inspection-api-transport)) |
| `rancher_internal_url` | (runtime) | `https://<node internal IP>` captured at deploy — what the runner transport dials |
| `rancher_runner_source_cidr` | `""` | The Cloud Run VPC connector's `/28`; auto-added to the firewall while the transport is `runner` |
| `gcp_rancher_allow_open` | `false` | Open `0.0.0.0/0` when no CIDRs set (discouraged) |
| `gcp_rancher_image` | `rancher/rancher:latest` | Rancher container image |
| `gcp_rancher_machine_type` | `e2-medium` | VM size (≥ 4 GB enforced) |
| `gcp_rancher_zone` | `""` | Blank → GCP default zone |
| `gcp_rancher_name` | `rancher-server` | VM + firewall base name |
| `gcp_rancher_boot_disk_gb` | `30` | Boot disk size |
| `gcp_rancher_network_tag` | `rancher` | Network tag = firewall target |
| `rancher_verify_tls` | `false` | Verify the node's cert on API calls |
| `rancher_server_url` | (runtime) | Set to `https://<node IP>` by the deploy job |
| `rancher_api_token` | (runtime) | Minted at bootstrap (secret) |
| `rancher_ui_web_jump_enabled` | `false` | Opt-in PRA Web Jump broker for the UI |
| `rancher_ui_verify_certificate` | `false` | Web Jump cert verification |
| `rancher_ui_jumpoint_cloud` | `gcp` | Which dashboard-managed Jumpoint host brokers the UI (`gcp`\|`aws`\|`azure`); its egress IP is auto-whitelisted |
| `rancher_ui_jumpoint_egress_ip` | (runtime) | Captured egress IP of the dashboard-managed Web-Jump Jumpoint (auto-added to the firewall) |
| `entitle_rancher_private` | `false` | Attach the Entitle agent token (node not reachable from Entitle's cloud) |

---

## Troubleshooting

**Kubernetes (Rancher) tab is missing** — enable **Kubernetes** under **Settings
→ Integrations**. The flag applies immediately.

**"Rancher node isn't configured" card** — set a GCP project and a Rancher
bootstrap password under **Settings → Kubernetes**.

**Deploy job fails with a `403` / permission error** — the GCP service account is
missing `compute.instances.create` or `compute.firewalls.*`. Re-run
`setup-gcp.sh` or grant `roles/compute.admin`.

**Node is RUNNING but the URL won't load** — the firewall is closed. Set
`rancher_allowed_source_cidrs` to include your IP (the node fails closed by
design) and redeploy to patch the rule.

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

**Machine type rejected** — types under 4 GB (`e2-micro`, `e2-small`, …) are
refused; use `e2-medium` or larger.

**Imported cluster stays "Pending" in Rancher** — the `cattle-cluster-agent`
can't reach the node. Confirm the downstream cluster has egress to the node's
public URL and that the node's firewall allows the cluster's egress IP.

**Everything broke after a stop/recreate** — expected for the ephemeral node: the
IP changed and state was wiped. Redeploy, then re-import the clusters. See
[Ephemeral node](#ephemeral-node).
