# BeyondTrust Gateway Hosts

> **Audience:** operator · **Profile:** `both` · **Read this when:** a resource sits in a private network and something has to broker a session into it.

## What is it?

A **BeyondTrust Gateway** is the host that brokers PRA sessions into a private cloud
network. Every tunnel the dashboard builds — the cloud-database protocol
tunnel, the Kubernetes API tunnel, a VM Shell Jump, a Rancher or Portainer Web Jump —
reaches its target *through* one, because the target has no public address to reach
directly.

The dashboard has always run one of these per cloud and managed it invisibly: ensure a
host before something needs a tunnel, reap it when nothing does. That answers *"is there
a gateway?"* but not *"what gateways do we have?"* — which is the question you actually
have once sessions start queueing behind a single host.

**Containers → Gateways** answers it. The tab is one inventory of every gateway host the
dashboard put in a cloud, plus a form to add more. It appears when **BeyondTrust** is
enabled (`pra_enabled`), alongside the [Cloud](../cloud-services/containers.md),
[Portainer](../cloud-services/portainer.md) and [Kubernetes (Rancher)](../cloud-services/rancher.md) tabs.

The **Gateways** tile in the dashboard's *Containers* section links straight there
(`/containers#gateways`) and counts every gateway host across all three clouds, with the
running total underneath — so a host that failed to come up is visible from the landing
page. The tile is gated on the same `beyondtrust` flag as the tab.

> **This page is about the gateway *hosts*.** Which PRA Gateway a jump item routes
> through — by name, from the `bt_jumpoint_name` setting or a deploy form's picker — is
> covered in [Privileged Remote Access](privileged-remote-access.md). The two are related but not the
> same thing, and the next section is the reason why.

---

## Two kinds of gateway, one table

The tab lists both, distinguished by a **`managed`** badge on the name. Both are hosts the
dashboard put in a *cloud*; there is a
[third kind that is deliberately not in this table](gateways/agent-host.md).

| | **managed** | **requested** |
|---|---|---|
| Created by | the auto-ensure, when something needs a tunnel | you, on this tab |
| How many | exactly one per cloud | as many as you like — **no cap** |
| Reference counted | **yes** — see below | no |
| Torn down | automatically, once nothing is using it | only when you remove it |
| Deletable from the UI | **no** | yes |
| `created_by` | `system` | your username |

They are in one table on purpose: the page's job is to show everything the dashboard put
in the cloud, and splitting the list by who owns the lifecycle would hide the shared host
that most sessions actually run through.

### Why the managed one is reference-counted

Its lifecycle is a ref-counted ensure/idle pair, so a shared host can be reclaimed when
the last thing using it goes away without a teardown yanking it out from under something
else. These hold a reference on it — while any of them is live, the idle teardown leaves
the host alone:

- **Provisioned cloud databases** in `available` or `provisioning` state. A *registered*
  database holds nothing — it has no tunnel, so it was never using the host.
- **VMs that borrowed the shared host** — every live EC2 deploy; and for GCE / Azure, the
  deploys that actually recorded a `jumpoint_host_id` (a VM given its own paired gateway
  container isn't using the shared one, and shouldn't pin it).
- **Kubernetes clusters** with a live PRA k8s tunnel *or* a live API TCP tunnel.
- **Virtual desktop seats** with a live PRA Remote RDP jump.
- **Rancher / Portainer Web Jumps** that have actually been provisioned — an
  enabled-but-never-provisioned one correctly holds nothing.

### Why the managed one can't be deleted

**Remove** doesn't render on a managed row, and the API refuses it (HTTP 400) if you call
it anyway. Deleting the row would not delete the gateway for long: the next thing that
needs a tunnel re-ensures the host, and you would be left with a live gateway the registry
said was gone — and, because node firewalls are computed from these rows, a broker whose
`/32` nothing allows.

If you want the managed host gone, remove what is holding it: the idle teardown reclaims it
on the next teardown pass once the count reaches zero.

### Why there is no cap on the requested ones

"Three in `us-central1` and two in `us-east-2`" is the stated use case, and the right
number is a function of session load — which the dashboard cannot see. Cloud quotas are the
real ceiling, so the dashboard doesn't invent a smaller one.

---

## Pages

The larger parts have a page each:

| Page | Read it when |
|---|---|
| [A Gateway on an agent host](gateways/agent-host.md) | you want a PRA Gateway running on a remote agent's host rather than in a cloud |
| [Deploying a Gateway host](gateways/deploying.md) | you are deploying a Gateway host, choosing its region and naming it |
| [The network tunnel address pool](gateways/address-pool.md) | you are using PRA network tunnels and need to set or check the address pool they hand out |
| [Troubleshooting Gateway hosts](gateways/troubleshooting.md) | a Gateway host will not join, or a jump through it connects and then drops |

---

## How it works

### One PRA Gateway, many nodes

This is the part worth internalising, because it is what makes adding a gateway safe:

**Every gateway host in a cloud is launched with that cloud's configured Gateway deploy
key.** A PRA Gateway's identity comes from its deploy key, so hosts sharing one don't
become separate Gateways — they join the **same PRA Gateway as additional cluster nodes**.

Two consequences:

- **Nothing needs repointing.** A jump item targets the Gateway *by name*, exactly as
  before, and PRA distributes sessions across its nodes. Existing databases, clusters, VMs
  and Web Jumps pick up the new capacity with no edit, no redeploy, no config change.
- **Any node may broker any session.** Which one PRA picks is not yours to choose, so
  anything that allow-lists a broker by address has to allow *all* of them — see
  [Node firewalls](#node-firewalls-the-consequence-that-bites).

| Cloud | Resolved from |
|---|---|
| AWS | `aws_ecs_docker_deploy_key`, else the Password Safe secret titled `bt_ps_deploy_key_title` |
| GCP | `gcp_cloud_run_docker_deploy_key` → `gcp_jumpoint_docker_deploy_key` → `gcp_jumpoint_deploy_key` |
| Azure | `azure_aci_deploy_key` → `azure_aci_docker_deploy_key` |

### What a gateway host actually is

The shape is per-cloud, and the same for both kinds:

| Cloud | Host | Managed host's name |
|---|---|---|
| **AWS** | the gateway container as an **ECS task on an EC2 container instance** in the configured cluster | `bt_ecs_host_name`, default `dashboard-sandbox-jumpoint-host` |
| **GCP** | a **privileged container on a Container-Optimized-OS GCE VM** (the tunnel needs `NET_ADMIN`/`NET_RAW`/`IPC_LOCK` + `/dev/net/tun`, which serverless can't grant) | `gcp_jumpoint_name`, default `clouddb-shared-jumpoint` |
| **Azure** | a **VM** running the gateway container | `clouddb-jumpoint` |

On GCP the tab's hosts also appear in the **GCE Container Instances** table on the Cloud
tab, badged `Gateway` — see [Cloud Containers](../cloud-services/containers.md#monitoring-the-container-fleet).

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **PRA enabled** | `pra_enabled`, under **Settings → Integrations → Privileged Remote Access**. Gates the tab entirely. |
| **A Gateway deploy key for that cloud** | The key above. Without it the ensure has nothing to launch and the job fails with the specific missing value in its log. |
| **A gateway subnet for that cloud** | `bt_ecs_jumpoint_subnet_id` (AWS), the region's `jumpoint_subnet_id` / `azure_aci_subnet_id` (Azure), the zone's derived subnetwork (GCP). |
| **AWS only: an ECS-capable instance profile** | The host's profile (`bt_ecs_host_instance_profile`, default `ecsInstanceRole`) must carry the AWS-managed **`AmazonEC2ContainerServiceforEC2Role`** policy. Without `ecs:RegisterContainerInstance` the host's ECS agent is denied, treats it as terminal and exits — see [below](gateways/troubleshooting.md#the-aws-host-never-joins-the-cluster). |
| **A per-region config set, to deploy outside the default region** | **Settings → Multi-region**. This is not optional politeness — see [Placement](gateways/deploying.md#placement-and-the-region-picker). |
| **`admin:write`** | To deploy. Removing needs `admin:delete`; *viewing* the list needs only a login. |

---

## Removing a gateway

**Remove** on a requested row confirms (*sessions routed through it will drop* — they will;
PRA may be brokering through that node right now), then enqueues a `gateway_teardown` job:
it deletes the host, marks the row `deleted`, clears the recorded egress IP, and re-applies
the node firewalls so the departed gateway's `/32` leaves the allow-lists.

The managed row has no Remove button, and the service layer refuses it independently of the
API — see [above](#why-the-managed-one-cant-be-deleted).

---

## Reading the list

| Column | Notes |
|---|---|
| **Status** | `provisioning` → `running`, or `error`; `deleting` while a teardown runs. Deleted rows are hidden. |
| **Name** | Plus the `managed` badge where it applies. |
| **Cloud / Region** | `–` region means "the configured default", resolved at deploy time. |
| **Host** | EC2 instance id, GCE instance, or Azure VM name. |
| **Created by** | `system` for the managed gateway; a username for requested ones. |
| **Actions** | **Job** (the deploy job) and **Remove** (requested only). |

**An empty list is normal on a fresh install.** The managed gateway is *adopted* into a row
on its first ensure, not on install — so it appears the first time anything needs a tunnel.
A deployment that has been running the shared gateway for months gets it registered rather
than duplicated the next time anything ensures it.

**A failed gateway keeps its row**, in `error`, with the reason attached, so the failure
stays visible instead of vanishing; open its job for the detail.

---

## Node firewalls: the consequence that bites

The Rancher and Portainer management nodes have public, **source-restricted** IPs and are
reached over a PRA Web Jump — so the source hitting them is *a gateway's egress IP*. Since
every gateway in a cloud is a node of one PRA Gateway cluster and PRA may broker through any
of them, allowing one remembered IP is a coin flip.

So the dashboard allows a **`/32` for every live gateway** it deployed in that cloud —
managed and requested — and re-applies the set on **every gateway deploy and teardown**, and
on every node deploy. AWS and GCP egress IPs are ephemeral (a reclaim/recreate changes them),
which is why the IP is re-recorded on each ensure rather than trusted once.

A gateway that is *not* in the allow list can reach nothing, which looks exactly like a
broken Web Jump. Check **Settings → Containers → Effective firewall sources**; the gateway
should be listed under *Web-Jump Gateways*.

A **pre-existing Gateway you run yourself** can't be auto-detected — the dashboard only
knows about hosts it deployed. Add its egress IP to `rancher_allowed_source_cidrs` /
`portainer_allowed_source_cidrs` by hand.

---

## Permissions

| Action | Requires |
|---|---|
| See the tab and the list | any authenticated user |
| Deploy a gateway | `admin:write` |
| Remove a gateway | `admin:delete` |

The name-suggestion endpoint also requires `admin:write`, so a non-admin sees the inventory
but gets no prefilled name — the deploy they can't perform anyway.

## API

| Endpoint | Notes |
|---|---|
| `GET /api/gateways` | Every gateway, managed and requested. `?cloud=aws\|azure\|gcp` filters. |
| `GET /api/gateways/suggest-name?cloud=&region=` | A free, cloud-legal default. |
| `POST /api/gateways/deploy` | `{cloud, region, zone, name}` → `202` with a `job_id`. |
| `DELETE /api/gateways/{id}` | → `202` with a `job_id`. `400` on the managed gateway. |

Both mutating endpoints are **enqueue-only** and write an audit record
(`gateway_deploy` / `gateway_teardown`).

## Configuration reference

Gateway hosts are configured under **Settings → Integrations → Privileged Remote Access** (and
**Settings → Multi-region** for per-region overrides). Nothing here is specific to the
requested gateways — they reuse the managed host's configuration, which is why a new one
needs no setup of its own.

| Key | Purpose |
|---|---|
| `bt_ecs_launch_type` / `bt_ecs_cluster` / `bt_ecs_jumpoint_subnet_id` / `bt_ecs_image` | AWS gateway host: how and where the ECS task runs |
| `bt_ecs_host_name` | Name of the **managed** AWS host (default `dashboard-sandbox-jumpoint-host`) — reserved against requested names |
| `aws_ecs_docker_deploy_key` / `bt_ps_deploy_key_title` | The AWS Gateway deploy key, direct or by Password Safe secret title |
| `gcp_jumpoint_name` | Name of the **managed** GCP gateway VM (default `clouddb-shared-jumpoint`); sanitized to RFC1035 |
| `gcp_jumpoint_zone` | Zone override for a GCP gateway — **applied only when it is inside the requested region** |
| `gcp_cloud_run_docker_deploy_key` | The GCP Gateway deploy key (with two legacy fallbacks) |
| `azure_aci_subnet_id` / `azure_aci_deploy_key` | Azure gateway subnet and deploy key |
| `azure_vm_jumpoint_mode` | `shared` (default) or `aci` — which shape a *VM deploy* borrows; the managed VM is `clouddb-jumpoint` |
| `azure_jumpoint_tunnel_pool` / `bt_ecs_jumpoint_tunnel_pool` / `gcp_jumpoint_tunnel_pool` | Network-tunnel lease pool, per cloud. Blank **derives** it from the gateway subnet; per-region `jumpoint_tunnel_pool` wins; `off` disables it. See [Network tunnels need an address pool](gateways/address-pool.md) |
| `bt_jumpoint_name` | Unrelated to host placement: the PRA Gateway a jump item routes through, **by name**. Adding a host to that Gateway's cluster does not change this value |

---

