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
| **Allowed source CIDRs** | The firewall **fails closed**: with no CIDRs set the node boots but is unreachable. Set the operator IP(s) + downstream clusters' egress IPs. |
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
| **Allowed source CIDRs** | Comma-separated CIDRs for the GCE firewall (tcp 80/443). **Empty = the firewall is not opened** (node unreachable) unless *Allow open* is ticked. Include your operator IP and each downstream cluster's egress IP. |
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
representative console — useful when an operator's IP isn't in the CIDR allowlist
and you want brokered, session-recorded access without editing the firewall. It
requires PRA to be configured (Jumpoint + Jump Group). This is independent of the
Entitle JIT RBAC grant, which continues to work either way.

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
| `rancher_allowed_source_cidrs` | `""` | GCE firewall source ranges (tcp 80/443); empty = closed |
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

**Deploy job fails waiting for Rancher** — Rancher needs 1–3 minutes to serve
after the VM boots. If it never comes up, check the VM's serial console / the
container's logs in GCP (`google-logging-enabled` is on).

**Machine type rejected** — types under 4 GB (`e2-micro`, `e2-small`, …) are
refused; use `e2-medium` or larger.

**Imported cluster stays "Pending" in Rancher** — the `cattle-cluster-agent`
can't reach the node. Confirm the downstream cluster has egress to the node's
public URL and that the node's firewall allows the cluster's egress IP.

**Everything broke after a stop/recreate** — expected for the ephemeral node: the
IP changed and state was wiped. Redeploy, then re-import the clusters. See
[Ephemeral node](#ephemeral-node).
