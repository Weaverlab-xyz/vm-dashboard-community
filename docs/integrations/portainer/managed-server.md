# Portainer: a managed server

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want the dashboard to deploy Portainer CE for you on AWS, Azure or GCP, and want to know what it creates, how to reach it, and how it is torn down.

Part of [Portainer](../portainer.md).

## What gets created

A single `portainer/portainer-ce` container on one VM with a public,
source-restricted IP — the same shape as the [Rancher node](../rancher.md):

| Aspect | Detail |
|---|---|
| VM tag / label | `purpose=portainer` (how the dashboard finds it again) |
| Ports | **9443** HTTPS UI/API, **8000** Edge agent tunnel |
| Privileges | **Unprivileged**, no Docker socket — the server administers *remote* Docker hosts over the API, so it needs neither |
| State | `/data` on the VM's own disk, or on a separate volume — see [Durable state](#durable-state) |
| TLS | self-signed certificate on 9443 |

## The node, per cloud

The container is identical everywhere. What differs is the machinery around it.

| | AWS | Azure | GCP |
|---|---|---|---|
| Host | EC2, ECS-optimized AL2023 AMI (Docker preinstalled) | Ubuntu 22.04 VM | Container-Optimized OS VM |
| Container started by | `docker run` from EC2 user-data | `docker run` from cloud-init | `gce-container-declaration` (konlet) |
| Default size | `t3.small` | `Standard_B1s` | `e2-small` |
| Ingress gate | a dedicated security group | a dedicated NSG on the NIC | a firewall rule targeting a network tag |
| Durable `/data` | a gp3 EBS volume (`DeleteOnTermination=false`) | a managed disk (`delete_option=Detach`) | a persistent disk (`auto_delete=false`) |
| Public IP | ephemeral | **static** — survives a recreate | ephemeral |

> **By default the node is ephemeral.** Its disk is deleted with the VM, so tearing
> the node down — or recreating it — wipes `/data`: users, environments and settings
> are all lost. On GCP and AWS the external IP changes too, which additionally
> invalidates every Edge key; **on Azure the address is static and survives**. Turn on
> [durable state](#durable-state) to keep the data either way.

## Durable state

Tick **Keep Portainer's data on a persistent disk** in **Settings → Containers**
(`portainer_data_disk_enabled`) and `/data` moves to a separate volume named
`<node-name>-data`, created so it outlives the VM. A teardown then keeps the users,
environments and settings, and the next deploy reattaches them.

**The container must not start before that volume is mounted.** If it does, Portainer
writes its database to the VM's own disk and loses it on the next recreate — silently,
with nothing in any log to say so. Each cloud reaches that guarantee differently:

| Cloud | How the ordering is guaranteed |
|---|---|
| GCP | a konlet `gcePersistentDisk` volume: konlet formats a blank disk (`mkfs.ext4`), `fsck`s a used one, mounts it, and only *then* starts the container |
| Azure | the disk is attached at VM **create** time and its device path is deterministic, so cloud-init mounts it before the `docker run` |
| AWS | an existing EBS volume **cannot** be attached by `run_instances`, so the attach lands after boot — user-data therefore **waits** for the device (up to 5 minutes, resolved via `/dev/disk/by-id` because Nitro renames it), mounts it, and refuses to start the container at all if it never arrives |

On every cloud the format step is conditional on the volume having no filesystem, so a
redeploy can never reformat the disk holding your only copy of the node's state.

Four things follow from durable state, and they are the whole reason it is opt-in:

- **The volume pins the node's zone.** A persistent disk, an EBS volume and a managed
  disk are all zonal and cannot attach outside their own zone, so an existing one
  overrides the region/zone pick and the same-region capacity fallback is disabled.
  Deploying into a *different region* is refused outright rather than silently landing
  back in the old one — move it with a snapshot, or tear down with the volume deleted
  and rebuild.
- **On GCP and AWS the external IP still changes.** Only `/data` is durable; the node
  takes a fresh address on every recreate, so `portainer_url` is rewritten each
  deploy. That matters for Edge agents — see
  [the warning below](../portainer.md#connect-a-docker-host-edge-agent). **On Azure it does not**: the
  Standard public IP is static, so joined agents keep checking in.
- **The admin password must be the one the volume already knows.** Portainer ignores
  `--admin-password` once its database holds an admin, so a deploy onto an existing
  volume keeps the *old* credential. Teardown therefore preserves
  `portainer_admin_password` and `portainer_pat` whenever the volume is preserved. If
  the volume exists but no password is stored, the deploy **fails up front** rather
  than launching a node nobody can sign into.
- **The volume keeps billing** until something deletes it. Teardown asks separately;
  see [Teardown](#teardown). Moving the node to a **different cloud** keeps the old
  cloud's volume too — it is zonal, so it cannot follow, and deleting your only copy of
  the node's state as a side effect of a move would be indefensible. The job logs a
  warning naming it; delete it by hand once you are sure.

## Prerequisites

| Requirement | Notes |
|---|---|
| One configured cloud | AWS, Azure or GCP credentials under **Settings**. You pick which hosts the node on the deploy form |
| Permissions for it | **GCP** `roles/compute.admin` (from `setup-gcp.sh`); **AWS** EC2 + security-group + EBS volume actions (all in `setup-aws.sh`'s `dashboard-app-policy`); **Azure** `Contributor` on the resource group (from `setup-azure.sh`) |
| A region with a configured subnet | The node needs a public subnet, and a subnet is regional on every cloud — only configured regions are offered |
| Allowed source CIDRs | Who may reach 9443/8000. Fail-closed: see [Firewall](#firewall) |

## Deploy

1. Open **Containers → Portainer**. The **Managed Portainer server** panel is at
   the top.
2. Pick a **Cloud**, and optionally a **Region** (and a **Zone**, on GCP only). Blank
   region keeps the node's current region; blank zone auto-picks the region's first
   available one, falling back to a sibling if that is capacity-exhausted. The Zone
   field is hidden on AWS and Azure because an EC2 subnet already pins its
   availability zone and Azure has no zone in this shape.
3. Click **Deploy Portainer server**. You land on the job page.

The job runs on the durable worker (VM boot plus bootstrap outlasts a web timeout):

| Step | What happens |
|---|---|
| Configuring firewall | Auto-detects the dashboard's public egress IP, merges it with your CIDRs, applies the ingress rule. **Fails fast** if the merged set is empty |
| Launching the node VM | Creates (or reuses/starts) the VM; relocates it if you picked a different region **or cloud** |
| Waiting for Portainer | Polls `GET /api/system/status` until it serves |
| Signing in as the admin user | The VM was launched with `--admin-password`, so the admin already exists |
| Minting an API token | Logs in and creates a personal access token |

On success the job **writes the connection settings for you** — `portainer_url`,
`portainer_pat`, and `portainer_verify_ssl` (off, because of the self-signed cert) —
so the Containers tab starts working with no Settings round-trip.

## Minting a token later

The deploy is not the only way to get one. **Mint a new API token**, under the node
table on the Containers page, does the same two calls on demand: the dashboard signs
in as the Portainer admin (using `portainer_admin_password`) and mints a token through
`POST /api/users/{id}/tokens`. Use it when the stored token was revoked, when a node
was redeployed without durable state so the DB that issued the old one is gone, or
when a bootstrap reported that it could not mint one.

Each mint gets a distinct description (`vm-dashboard-<unix>`) because Portainer
refuses two tokens with the same description for one user. It **adds** a token rather
than replacing one — revoke the old ones in Portainer if you want them gone.

On the **managed node**, minting manages the node's ingress as well. A deploy writes
the allow-list from one egress detection and never revisits it, so by the time you
mint, the dashboard's own outbound address may have moved — a worker rescheduled
behind a different SNAT address, a proxy pool picking a different one — and the
node's rule still names the old one. The mint then fails with a `ConnectTimeout`
(dropped packets, not a closed port). Rather than report it, the dashboard
re-detects its egress address, re-applies the ingress rule and mints again; if the
rule had been deleted outright, re-applying puts it back. Only for a node this
dashboard deployed — a Portainer you merely point it at has a firewall that is
yours, and it says so instead of touching anything.

Minting also re-stages the token to the `portainer_access` adapter, if one is deployed
(see [Portainer through Entitle](entitle.md)). **Re-send the token to the adapter** does only that half, for when the
dashboard's own token is fine and only the function's copy is stale.

If `portainer_pat` holds a **vault reference** rather than a literal, minting is
refused: storing a token over the reference would leave the vault holding a stale
value that nothing reads. Rotate the token in Portainer, update the vault secret, then
re-send it to the adapter.

A short-lived Portainer **JWT** is the means here, never the product. Portainer's
session token expires in hours and nothing can refresh one on an integration's behalf,
so a JWT stored as the credential would work this afternoon and start answering 401
tomorrow. An API token does not expire, and is revocable from Portainer's own UI.

If you left **Admin password** blank the dashboard generates a 24-character one and
shows it once, on the Containers page (`Log in as admin / …`). Change it in Portainer
after first login.

The password is settled **before** the VM is created and passed to the container as a
bcrypt hash (`--admin-password`), so Portainer initializes its admin at startup. That
is deliberate: Portainer only accepts `POST /api/users/admin/init` for a short window
after the container starts, and once that window closes it answers *every* request with
`administrator initialization timeout` — a node with no admin that nobody can log into
until the container restarts. Initializing at boot means there is no window to lose.

This holds on every cloud, and so does the way out of it: a node already in that state
can't be repaired by redeploying, because the launcher reuses a running VM and the
thing that carries `--admin-password` (the container declaration on GCP, user-data on
AWS, cloud-init on Azure) is only read at boot. **Delete the node and deploy again**;
the job says so instead of reporting a misleading "already had an admin user".

> The hash travels in instance metadata / user-data, which is readable from inside the
> VM. It is a bcrypt hash rather than the password, and it is the same exposure the GCE
> path has always had — but it is why the node is given no other secret.

## PRA Web Jump (optional)

By default the node's UI is reachable only from the source CIDRs you allow, and an
auto-generated admin password has to be shown on the Containers page so you can use
it. Ticking **Broker the Portainer UI via a PRA Web Jump** on the deploy form fixes
both:

- A `portainer-ui` **Web Jump** is created, so the UI opens from the PRA
  representative console — brokered and recorded — with no CIDR change for your own
  workstation.
- A Web Jump connects *through* a **Gateway**, so the source hitting the node is that
  host's egress IP. The dashboard **auto-allows a `/32` for every gateway it deployed**
  in that cloud — the managed one *and* any you added on **Containers → Gateways** —
  because they all join the same PRA Gateway cluster and PRA may broker the session
  through any node in it. The set is re-applied on every node deploy and on every
  gateway deploy/teardown, since AWS/GCP gateway IPs are ephemeral. A *pre-existing*
  Gateway you run yourself can't be auto-detected — add its IP to
  `portainer_allowed_source_cidrs` manually.
- A provisioned Web Jump also **holds a reference on the shared gateway**, so the idle
  teardown won't reclaim the host that brokers it.
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

## Teardown

**Stop** on the node row first retires the Entitle adapter — deregistering the
integration, destroying the function and retiring its staged API token — because an
adapter that outlives its Portainer is a billable function that can only fail and a
grantable integration pointed at nothing. It is best-effort: an unreachable Entitle
tenant leaves a warning in the job result rather than blocking the teardown. **Remove
adapter** on the card does the same thing on its own.

Stop then removes the PRA Web Jump (when one exists), deletes the VM
and its ingress rule, then clears `portainer_url` and the node's other runtime config.
On AWS it also reclaims the node's security group once the terminating instance
releases it; on Azure it removes the NIC, the public IP and the NSG the VM owned.

- **Without a data volume** all Portainer state goes with the VM, and the admin
  password and API token are cleared too.
- **With a data volume** the volume is *kept* by default and the credential keys are
  preserved with it, so the next deploy comes back with the same users, environments
  and settings. A second confirmation offers to delete the volume as well — that is the
  one part of a teardown that cannot be undone.

> **The volume can outlive the sandbox.** Because it is meant to survive a teardown, it
> can end up the only thing left in a region. The sandbox rollback scripts therefore
> **refuse** to run while a managed node or an orphaned node volume is present, rather
> than cascading over your only copy of the node's state.

## Firewall

The node's ingress opens **tcp 9443 and 8000** to a merged source set:

- `portainer_allowed_source_cidrs` — your manual CSV.
- The dashboard's own public egress CIDR — auto-detected and saved on every deploy,
  because the worker bootstraps and polls the node over its public IP. If you egress
  from a proxy *pool*, set `portainer_dashboard_egress_cidr` to the pool's range by
  hand; detection will not clobber a broader range that already contains the detected IP.
  The last few detected `/32`s stay admitted too (`portainer_dashboard_egress_recent`,
  bounded), because a host whose own outbound address is not fixed — an Azure Container
  Apps environment with no NAT Gateway, say — would otherwise lock the deploy out of the
  node it just launched. The tell is a job that reports the node "serving" and then
  "cannot reach" it seconds later: the readiness poll needs one lucky attempt, the
  bootstrap that follows needs several consecutive ones. On a dropped connect the deploy
  re-detects, re-applies the ingress and retries the bootstrap once.
- The hosting platform's **published outbound pool**, when the dashboard runs on Azure
  Container Apps. The recent-`/32` heuristic can't cover a pool of several hundred
  addresses that picks one per destination, so the dashboard reads the pool from its
  own Container App and admits all of it. That needs one Reader grant: see
  [Outbound addresses](../../operations/cloud-hosting/azure-container-apps.md#outbound-addresses-and-the-managed-node-firewalls).
  Settings shows the pool as a count, or shows the reason it couldn't be read.
- A `/32` per dashboard-deployed Gateway, when the
  [PRA Web Jump](#pra-web-jump-optional) is on.

It is **fail-closed** on every cloud — an empty merged set leaves the node unreachable
— but the mechanism differs, because the three clouds do not offer the same primitive:

| Cloud | Open | Closed |
|---|---|---|
| GCP | a firewall rule targeting the node's network tag | the rule is **deleted** |
| AWS | a dedicated security group on the node's ENI | **every ingress permission is revoked** — a security group in use by a running instance cannot be deleted |
| Azure | one allow rule in a dedicated NSG on the node's NIC | the **rule** is deleted; the NSG stays (it is attached to a live NIC, and a Standard public IP denies all inbound without a rule anyway) |

Set that cloud's `*_portainer_allow_open` to open `0.0.0.0/0` when no CIDRs are set — a
deliberate opt-in, per cloud. **Settings → Containers** shows the live merged
allow-list.

### Re-applying it

The deploy configures the firewall from **one** egress detection and never revisits
it, so the rule ages out from under a node that is running perfectly well: the worker
is rescheduled behind a different SNAT address, a proxy pool picks another one, a
gateway comes back with a new IP, or someone deletes the rule in the cloud console.
Every caller then reports the same unhelpful thing — *unreachable* — and the only cure
used to be a redeploy.

**Re-apply the node firewall** (`POST /api/containers/portainer/node/firewall`) is
that step on its own: re-detect the dashboard's egress address, recompute the merged
set, re-apply the rule. It reports what it added and removed, and says so plainly when
the result is **closed** — an empty merged set is a real outcome here, not an error.
Safe to click on a healthy node: the per-cloud apply is idempotent, and a rule that had
been deleted is simply put back.

It is in two places, because the two halves of this are in two places: under the node
table on the **Containers** page, and next to the allow-list readout in **Settings →
Containers** — where the button is labelled **Re-apply** and the breakdown re-renders
from the result, so what you are reading afterwards is the rule that now exists.

Minting a token does this for itself on a dropped connect, so reach for the button
when what is failing is something else — listing environments, an Edge registration,
a Web Jump.
