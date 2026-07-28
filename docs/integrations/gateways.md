# BeyondTrust Gateway Hosts

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
enabled (`beyondtrust_enabled`), alongside the [Cloud](../cloud-containers.md),
[Portainer](portainer.md) and [Kubernetes (Rancher)](rancher.md) tabs.

The **Gateways** tile in the dashboard's *Containers* section links straight there
(`/containers#gateways`) and counts every gateway host across all three clouds, with the
running total underneath — so a host that failed to come up is visible from the landing
page. The tile is gated on the same `beyondtrust` flag as the tab.

> **This page is about the gateway *hosts*.** Which PRA Gateway a jump item routes
> through — by name, from the `bt_jumpoint_name` setting or a deploy form's picker — is
> covered in [BeyondTrust integration](beyondtrust.md). The two are related but not the
> same thing, and the next section is the reason why.

---

## Two kinds of gateway, one table

The tab lists both, distinguished by a **`managed`** badge on the name:

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

- **Cloud databases** in `available` or `provisioning` state.
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
tab, badged `Gateway` — see [Cloud Containers](../cloud-containers.md#monitoring-the-container-fleet).

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **BeyondTrust enabled** | `beyondtrust_enabled`, under **Settings → Integrations → BeyondTrust**. Gates the tab entirely. |
| **A Gateway deploy key for that cloud** | The key above. Without it the ensure has nothing to launch and the job fails with the specific missing value in its log. |
| **A gateway subnet for that cloud** | `bt_ecs_jumpoint_subnet_id` (AWS), the region's `jumpoint_subnet_id` / `azure_aci_subnet_id` (Azure), the zone's derived subnetwork (GCP). |
| **AWS only: an ECS-capable instance profile** | The host's profile (`bt_ecs_host_instance_profile`, default `ecsInstanceRole`) must carry the AWS-managed **`AmazonEC2ContainerServiceforEC2Role`** policy. Without `ecs:RegisterContainerInstance` the host's ECS agent is denied, treats it as terminal and exits — see [below](#the-aws-host-never-joins-the-cluster). |
| **A per-region config set, to deploy outside the default region** | **Settings → Multi-region**. This is not optional politeness — see [Placement](#placement-and-the-region-picker). |
| **`admin:write`** | To deploy. Removing needs `admin:delete`; *viewing* the list needs only a login. |

---

## Deploying a gateway

**Containers → Gateways** → the deploy row at the top of the panel:

| Field | Notes |
|---|---|
| **Cloud** | AWS / Azure / GCP. Changing it reloads the region list and re-suggests a name, because both are per-cloud. |
| **Region** | A picker, not free text. `(configured default)` leaves it blank — resolved at deploy time — which is what a single-region install wants. |
| **Zone** | **GCP only**, optional. Blank resolves from the region; the placeholder shows what that will be. |
| **Name** | Prefilled with a free, cloud-legal default (`gw-<region>-01`). Editable. |

**Deploy gateway** enqueues a `gateway_deploy` job and returns — the row appears
immediately as `provisioning` with a **Job** link. The work runs on the durable worker, so
a gunicorn recycle mid-deploy can't strand a half-built host:

| Step | What happens |
|---|---|
| Launching | Ensures the host under the name you gave. Passing a name is precisely what marks this gateway as *requested* — the ensure skips its managed-adoption write, because the row already exists and is owned by the job. |
| Recording placement | Writes back where the host **actually landed** (zone, egress IP), not what was asked for. |
| Updating node firewalls | Re-applies the Rancher + Portainer node allow-lists. Best-effort — a firewall hiccup logs and never fails the gateway job. |
| Done | Row flips to `running` with its host id. |

> **Why the row records where it landed rather than what you picked.** A blank GCP zone is
> resolved at launch, and a capacity-exhausted zone falls through to a sibling — so the
> requested zone is not reliably the real one. Teardown deletes by `(zone, name)`, so a row
> holding the requested zone would later fail to find its own host.

### Placement and the region picker

**The picker offers only the configured default region plus every region that has a
per-region config set of its own** (the same list the Rancher and Portainer node pickers
use — [`region_config.deployable_regions`](../../web_dashboard/services/region_config.py)).
That restriction is the feature, not a limitation.

The tab used to take the region as free text, and typing an unconfigured region was **worse
than a validation error**. Every regional id a gateway needs — subnet, security group, ECS
cluster — resolves through `region_config.resolve_region`, which falls each field back to
the flat config keys. So the host came up **on the default region's network** while the row,
the job, and you all said otherwise. Nothing failed; the gateway was simply somewhere else,
with no line of sight to the private targets in the region you meant.

Restricting the picker makes *"the region I picked"* and *"the region it lands in"* the same
thing. To deploy somewhere new, add that region under **Settings → Multi-region** first;
it then shows up in the picker.

GCP has a second route to the same silent relocation: a gateway's **subnet is derived from
its zone**, so a zone in another region would quietly override the region you chose. The
deploy endpoint refuses that pair (HTTP 400) rather than queueing a job that lands
elsewhere, and picking a new region clears any leftover zone.

### Names, and why they matter more than they look

The name is not cosmetic — it is what keeps the managed and requested hosts apart **in the
cloud itself**, because the managed idle teardown finds its host *by name tag*. A requested
gateway wearing the managed name is one the idle teardown would terminate out from under
you. So:

- **Names must be unique within a cloud**, case-insensitively, across both kinds — including
  the managed name, which is reserved even if the auto-ensure has never run and so has no
  row yet.
- **Cloud rules apply.** GCP names must be RFC1035 (lowercase, digits, hyphens, leading
  letter) and ≤ 63 characters; AWS allows ≤ 255. Rejections are explicit rather than being
  silently rewritten — you typed the name and then have to find it in a cloud console.
- **Azure is capped at 15 characters**, not the 64-character Azure resource limit, because
  the in-guest hostname is derived as `name[:15]`. Two gateways differing only past the 15th
  character would share a hostname.

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

Gateway hosts are configured under **Settings → Integrations → BeyondTrust** (and
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
| `bt_jumpoint_name` | Unrelated to host placement: the PRA Gateway a jump item routes through, **by name**. Adding a host to that Gateway's cluster does not change this value |

---

## Troubleshooting

**The Gateways tab isn't there** — it is gated on `beyondtrust_enabled`. Toggle
**BeyondTrust** on under **Settings → Integrations**; it applies immediately, no restart.

**"The `<cloud>` gateway could not be started"** — the ensure path returned nothing because
a prerequisite is missing: the deploy key, the project, or the gateway subnet. The job log
names the specific one. Check **Settings → Integrations → BeyondTrust**.

**The region I want isn't in the picker** — it has no per-region config set. Add one under
**Settings → Multi-region**. The picker is deliberately not the full region catalog; a
region without its own subnet would put the gateway on the *default* region's network.

**"Zone `<z>` is not in region `<r>`"** — a GCP gateway's subnet follows its zone, so that
pair would come up outside the region you chose. Clear the zone (blank resolves correctly
from the region) or pick one inside it.

**"A gateway named `<n>` already exists in `<cloud>`"** — names are unique per cloud,
case-insensitively, and the managed host's name is reserved even when it has no row yet.
That reservation is load-bearing: the managed idle teardown acts on that name, so a
requested host wearing it would be terminated automatically.

**Azure rejects a name that looks short enough** — the limit is **15** characters, not
Azure's 64, because the in-guest hostname is `name[:15]`.

**A new gateway brokers a Web Jump that can't reach the node** — the node firewall allows a
`/32` per gateway, refreshed on each gateway deploy/teardown. Confirm it appears under
*Web-Jump Gateways* in **Settings → Containers**; if not, its egress IP was never recorded —
redeploy the gateway, or add the IP to the node's `*_allowed_source_cidrs` manually.

**I can't remove the managed gateway** — by design; it is created and reclaimed by the
reference-counted lifecycle. Remove whatever still
[holds a reference](#why-the-managed-one-is-reference-counted) and the idle teardown
reclaims it.

### The AWS host never joins the cluster

**"Gateway host `i-…` did not register with the ECS cluster … within 180s"** — the EC2 host
booted, but its ECS agent never registered, so no Gateway task can be placed on it. The
usual cause is the host's instance profile (`bt_ecs_host_instance_profile`, default
`ecsInstanceRole`) missing **`ecs:RegisterContainerInstance`**. Attach the AWS-managed
**`AmazonEC2ContainerServiceforEC2Role`** policy to that role;
`/var/log/ecs/ecs-agent.log` on the host confirms it.

This one is worth knowing even if you never deploy a gateway by hand, for two reasons:

- **The agent treats the denial as terminal and exits**, so the host stays out of the
  cluster *permanently* — it is not a race that retrying wins. The deploy fails fast and
  names the permission rather than burning its timeout and then reporting AWS's
  `InvalidParameterException: No Container Instances were found in your cluster`, which
  names neither the host nor the missing permission.
- **Only a gateway deploy treats a missing Gateway as fatal.** An ordinary EC2 / database /
  Kubernetes deploy records `ecs_error` and reports *success* — with no tunnel. So this tab
  is where the failure is visible; a cloud whose gateways are all in `error` explains a
  fleet of resources that provisioned fine and can't be reached.

`ecsInstanceRole` is AWS's own default role name, so it is easy to already have from a
console wizard or an older `setup-aws.sh` run. Current `setup-aws.sh` attaches the policy on
both the create *and* the reuse path; a role left over from before only converges once that
script runs again.

**A row is stuck in `provisioning`** — the row is written before the job runs, so it reflects
the job's state. Open its **Job** link. A failed job leaves the row in `error` with the
reason, and records the host id it got as far as creating — so a host that came up but never
became usable is still removable from this tab rather than being an orphan you have to find
in a cloud console.

**A gateway I deployed by hand isn't listed** — the registry only holds hosts the dashboard
deployed. A gateway you run yourself is invisible to the tab, and to the node-firewall
automation that reads it.
