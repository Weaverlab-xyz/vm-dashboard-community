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

## The pages

This page covers deploying the node, importing clusters and keeping it running. Two more
are under [`rancher/`](rancher/network-and-tls.md):

| Page | Read it when |
|---|---|
| [Network access and TLS](rancher/network-and-tls.md) | a cluster, your browser or Entitle cannot reach the node, your network TLS-inspects outbound traffic, or you want a publicly trusted certificate |
| [Just-in-time access through Entitle](rancher/entitle.md) | you want time-boxed Rancher access through Entitle, a grant fails, or you are weighing a real auth provider on the node |

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
| **Allowed source CIDRs** | The firewall **fails closed**, but dashboard-provisioned clusters' egress IPs (and, when the Web Jump is on, the dashboard-managed Gateway's egress IP) are **added automatically** — see [Automatic firewall whitelisting](rancher/network-and-tls.md#automatic-firewall-whitelisting). You only add extra operator IPs and pre-existing operator Gateways here. |
| **A ≥ 4 GB machine type** | Rancher OOMs on shared-core types; the default `e2-medium` (4 GB) is the minimum. |

---

## Pages

The larger parts have a page each:

| Page | Read it when |
|---|---|
| [Setting up Rancher](rancher/setup.md) | you are standing up the Rancher management plane, or connecting one you already run |
| [Rancher configuration reference](rancher/configuration.md) | you need what a Rancher setting does, or which one to change |
| [Troubleshooting Rancher](rancher/troubleshooting.md) | Rancher will not come up, a cluster will not import, or its Web Jump will not connect |

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

## PRA Web Jump (optional)

The node is reachable directly at its source-restricted URL, so the BeyondTrust
PRA Web Jump is **off by default**. Enable **PRA Web Jump to the Rancher UI**
(`rancher_ui_web_jump_enabled`) to *also* broker the UI through the PRA
representative console — brokered, session-recorded access. It requires PRA to be
configured (Gateway + Asset Group). This is independent of the Entitle JIT RBAC
grant, which continues to work either way.

When enabled, the dashboard ensures its managed Gateway host is up, captures its
egress IP, and adds it to the node firewall automatically (see [Automatic firewall
whitelisting](rancher/network-and-tls.md#automatic-firewall-whitelisting)) — so you don't pre-configure that
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

