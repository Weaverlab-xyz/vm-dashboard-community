# Portainer CE Integration

## What is it?

The Portainer CE integration connects the dashboard to a single
[Portainer Community Edition](https://www.portainer.io/) instance managing your
on-premises Docker hosts. It adds a **Containers** tab that lists running
containers, starts/stops them, and deploys containers or compose stacks — from the
same UI you use for cloud resources. (One Portainer instance can manage many Docker
hosts; each appears as its own environment/endpoint.)

There are two ways to get one:

- **Deploy a managed server** — the dashboard stands up Portainer CE for you on a VM
  in **AWS, Azure or GCP** (you pick which), bootstraps it, and wires up the
  connection. This is the same managed-service shape as the
  [Rancher node](rancher.md).
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

A single `portainer/portainer-ce` container on one VM with a public,
source-restricted IP — the same shape as the [Rancher node](rancher.md):

| Aspect | Detail |
|---|---|
| VM tag / label | `purpose=portainer` (how the dashboard finds it again) |
| Ports | **9443** HTTPS UI/API, **8000** Edge agent tunnel |
| Privileges | **Unprivileged**, no Docker socket — the server administers *remote* Docker hosts over the API, so it needs neither |
| State | `/data` on the VM's own disk, or on a separate volume — see [Durable state](#durable-state) |
| TLS | self-signed certificate on 9443 |

### The node, per cloud

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

### Durable state

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
  [the warning below](#connect-a-docker-host-edge-agent). **On Azure it does not**: the
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

### Prerequisites

| Requirement | Notes |
|---|---|
| One configured cloud | AWS, Azure or GCP credentials under **Settings**. You pick which hosts the node on the deploy form |
| Permissions for it | **GCP** `roles/compute.admin` (from `setup-gcp.sh`); **AWS** EC2 + security-group + EBS volume actions (all in `setup-aws.sh`'s `dashboard-app-policy`); **Azure** `Contributor` on the resource group (from `setup-azure.sh`) |
| A region with a configured subnet | The node needs a public subnet, and a subnet is regional on every cloud — only configured regions are offered |
| Allowed source CIDRs | Who may reach 9443/8000. Fail-closed: see [Firewall](#firewall) |

### Deploy

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

### PRA Web Jump (optional)

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

### Teardown

**Stop** on the node row removes the PRA Web Jump (when one exists), deletes the VM
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

### Firewall

The node's ingress opens **tcp 9443 and 8000** to a merged source set:

- `portainer_allowed_source_cidrs` — your manual CSV.
- The dashboard's own public egress CIDR — auto-detected and saved on every deploy,
  because the worker bootstraps and polls the node over its public IP. If you egress
  from a proxy *pool*, set `portainer_dashboard_egress_cidr` to the pool's range by
  hand; detection will not clobber a broader range that already contains the detected IP.
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

---

## Connect a Docker host (Edge agent)

A managed node **cannot reach into your network.** It runs unprivileged with no Docker
socket of its own, and it sits on a public IP with no route to a LAN address. So it can
manage a Docker host only if that host comes to *it*.

That is what an Edge agent does: the agent runs on the Docker host and polls **outbound**
to the node's tunnel port 8000 — which the node's firewall already opens — so nothing
inbound to your network is required, and no VPN.

1. Open **Containers → Portainer**. Under **Connect a Docker host**, type an environment
   name and click **Generate join command**.
2. Run the generated `docker run` on the Docker host you want managed.
3. The environment appears within a few seconds; **Refresh** lists it.

The command sets `EDGE_INSECURE_POLL=1`, because the node serves a self-signed
certificate. Without it the agent's first poll fails certificate verification and the
environment simply never appears — with no error in a place you would think to look.

> **The Edge key is shown once and is tied to the node's URL.** Portainer derives the
> key from the node URL, its tunnel host and the new environment's id — so whether a key
> survives a recreate depends on whether the address does. On **GCP and AWS** the node
> takes an ephemeral address, so recreating it changes the URL and every agent joined
> beforehand stops being able to check in — which shows up as environments quietly going
> offline, not as an error. On **Azure** the Standard public IP is static, so joined
> agents keep working. The Containers page warns when the stored URL no longer matches
> the running node; when it does, re-run **Generate join command** and re-join each host.

---

## Import from another Portainer

Merges **users, teams, team memberships and registries** from another Portainer into the
configured one. Existing names are matched rather than duplicated, so re-importing the
same bundle is a no-op.

### What a Portainer backup can and cannot do

Portainer's own **Backup** produces a `tar.gz` of its `/data` volume. Two limits matter:

- It **only restores into a pristine instance** with an empty data volume, during first
  run. A managed node initializes its admin at container start (to dodge the
  init-timeout lockout above), so it is *never* pristine — a `.tar.gz` cannot be
  restored into one, and the dashboard will not pretend otherwise.
- It never covered what is *deployed* on your environments — containers, volumes,
  images. Portainer's docs say so explicitly.

So the archive is opened where it already lives, and the dashboard imports a small
reviewable JSON bundle instead.

### Step 1 — turn a backup into a bundle (on your own machine)

Skip to step 2 if the source Portainer is still running — point the exporter straight at
it.

```bash
docker run -d --name portainer-scratch -p 9443:9443 portainer/portainer-ce:latest
curl -k -X POST https://localhost:9443/api/restore \
     -F "file=@portainer_backup.tar.gz" -F "password=<only if encrypted>"
python -m web_dashboard.scripts.portainer_migrate export \
     --url https://localhost:9443 --username admin --insecure --out bundle.json
docker rm -f portainer-scratch
```

The restore must be the **first** thing that scratch instance is asked to do — Portainer
closes its first-run window a short time after the container starts.

The exporter is stdlib-only, so it needs no virtualenv. `inspect` reviews a bundle
offline:

```bash
python -m web_dashboard.scripts.portainer_migrate inspect --bundle bundle.json
```

### Step 2 — import it

**Containers → Portainer → Import from another Portainer** → choose the `.json`. This
enqueues a `portainer_import` job; follow it at `/jobs/{job_id}`.

Optionally pick an environment under **Deploy stacks onto**. Be deliberate: Portainer has
no "save a stack without running it" API, so this **deploys** the bundle's stacks and
starts containers.

### What does not come across

| Not migrated | Why |
|---|---|
| **Environment connections** | They address a local Docker socket or a LAN host this node cannot route to. The bundle records them under `reference` so you can see what existed; re-establish them as [Edge agents](#connect-a-docker-host-edge-agent). |
| **User passwords** | Portainer's API never returns them, and the bundle scrubs credential-shaped fields regardless. Imported users get fresh generated passwords, reported **once** in the job result. |
| **Registry credentials** | Same reason. A registry is recreated with its name and URL but **unauthenticated**, and the job names each one that needs its password re-entered. |
| **Administrator role** | An imported user is always created as *standard*, even if the source had it as an administrator — a bundle is a hand-editable file from another server. The job says which users were downgraded; promote them in Portainer deliberately. |
| **The node's own `admin`** | Skipped outright. The dashboard holds that credential; colliding with it is how you lose access to the node. |
| **Anything deployed on the environments** | Portainer's backup never covered this either. |

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
| **Edge agent join** | Register an Edge environment and get the `docker run` for a host the dashboard can't reach |
| **Bundle import** | Merge users, teams, memberships and registries from another Portainer |

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
| `portainer_node_cloud` | `gcp` | `aws` \| `azure` \| `gcp` — which cloud hosts the node. Picked on the deploy form and rewritten to where it actually landed, so teardown and bare redeploys stay put. Defaults to `gcp` because every node deployed before this key existed is a GCE VM |
| `portainer_data_disk_enabled` | `false` | Put `/data` on a separate volume that survives a teardown |
| `portainer_ui_web_jump_enabled` | `false` | Broker the UI via a PRA Web Jump (opt-in) |
| `portainer_ui_verify_certificate` | `false` | Web Jump TLS verification — off for the node's self-signed cert |
| `portainer_ui_jump_group` | `""` | Jump Group for the Web Jump; blank = `bt_jump_group_name` |
| `portainer_ui_jumpoint_name` | `""` | Gateway for the Web Jump; blank = `bt_jumpoint_name` |
| `portainer_ui_vault_account_group_id` | `""` | Vault account group the admin credential is stored in; blank = `bt_vault_account_group_id`, else the password is shown |
| `portainer_ui_jumpoint_cloud` | `gcp` | Which managed Gateway host brokers the UI; its egress IP is auto-allowed |
| `portainer_ui_jumpoint_egress_ip` | `""` | Captured egress IP of the SHARED Gateway (runtime-set; auto-added as a `/32`). Gateways you deploy yourself are read from the gateway registry instead, so every cluster node is allowed |

### Per-cloud node keys

One group per cloud, all optional — the defaults are usable. Only the group for the
cloud the node runs on has any effect, which is why each cloud can keep its own.

| Key | Default | Purpose |
|---|---|---|
| `gcp_portainer_image` / `aws_portainer_image` / `azure_portainer_image` | `portainer/portainer-ce:latest` | Server container image |
| `gcp_portainer_machine_type` | `e2-small` | GCE size — Portainer is light |
| `aws_portainer_instance_type` | `t3.small` | EC2 size |
| `azure_portainer_vm_size` | `Standard_B1s` | Azure size |
| `gcp_portainer_name` / `aws_portainer_name` / `azure_portainer_name` | `portainer-server` | VM (or instance) name, and the base name of its ingress rule (`<name>-allow-mgmt`) and data volume (`<name>-data`) |
| `gcp_portainer_boot_disk_gb` (20) / `aws_portainer_boot_disk_gb` (20) / `azure_portainer_boot_disk_gb` (30) | — | Boot / root / OS disk. Holds `/data` when no data volume is enabled, and is deleted with the VM |
| `gcp_portainer_data_disk_gb` / `aws_portainer_data_disk_gb` / `azure_portainer_data_disk_gb` | `10` | Size of the durable data volume (a persistent disk, an EBS volume, a managed disk) |
| `gcp_portainer_allow_open` / `aws_portainer_allow_open` / `azure_portainer_allow_open` | `false` | Open `0.0.0.0/0` on that cloud when no CIDRs are set |
| `gcp_portainer_network_tag` | `portainer` | GCE network tag = the firewall rule's target. No analogue on AWS/Azure, where a dedicated security group / NSG *is* the scope |
| `gcp_portainer_zone` | `""` | Blank auto-picks a zone in the region; overwritten on deploy with the actual one |
| `aws_portainer_zone` | (runtime) | **Recorded, not chosen** — the availability zone the node (and so its data volume) landed in. The subnet pins it |
| `azure_portainer_zone` | (runtime) | **Recorded, not chosen** — the location the node landed in, so a bare redeploy stays there |

---

## Troubleshooting

**Containers tab is missing** — verify Portainer is toggled on in **Settings →
Integrations → Portainer CE**. The flag applies immediately; no restart needed.

**"Portainer is not configured" card on the Containers page** — the URL or API token
is missing. Deploy a managed server, or fill both in under **Settings → Integrations
→ Portainer CE**.

**Portainer shows "Your Portainer instance timed out for security purposes"** — the
node's admin-initialization window closed before an admin was created, so the whole API
is fenced off. A redeploy can't fix it on any cloud (the running VM is reused, and
whatever carries `--admin-password` is only read at boot): **delete the node on the
Containers page and deploy again**. Nodes deployed by this version initialize their
admin at startup and can't reach this state.

**A Web Jump through a gateway you deployed can't reach the node** — the node firewall
allows a `/32` per gateway, refreshed on every gateway deploy/teardown. Check
**Settings → Containers → Effective firewall sources**: the gateway should be listed
under *Web-Jump Gateways*. If it isn't, its egress IP was never recorded — redeploy the
gateway, or add the IP to `portainer_allowed_source_cidrs`.

**Deploy fails with "the Portainer node's firewall is closed"** — no allowed source
CIDRs, and the dashboard couldn't auto-detect its own egress IP. Set
`portainer_dashboard_egress_cidr` or `portainer_allowed_source_cidrs` in **Settings →
Containers** — or enable that cloud's `*_portainer_allow_open` — then redeploy.

**Deploy fails: "cannot be placed in \<region\>: … is not set for that region"** — the
chosen region has no configured subnet (and, on AWS, no VPC). That is deliberate:
falling back would put the node in the *default* region's network while the form, the
job and the row all said otherwise. Run that cloud's sandbox setup for the region, or
add a per-region config under **Settings → Multi-region**.

**Azure: the node is RUNNING but nothing answers on 9443** — an Azure VM with a
Standard public IP and no NSG rule denies *every* inbound packet, which looks identical
to a closed allow-list. Confirm the node's NSG (`<node>-allow-mgmt`) exists and carries
an `allow-mgmt` inbound rule; a deploy whose ingress step failed leaves the VM up and
unreachable.

**AWS: the node came up but `/data` is empty on a durable redeploy** — the data volume
never attached, so user-data refused to start the container rather than letting
Portainer write to the root volume. Check the instance's console output for
`node-data volume never appeared`, and that the volume is in the same availability zone
as the subnet.

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

**An Edge environment never comes online** — the agent polls outbound to the node's
port 8000, so check that egress is allowed from the Docker host. If the node was
recreated since the key was minted, the key is dead: Edge keys encode the node URL and
the node's external IP is ephemeral. Generate a new join command and re-join the host.

**An imported user can't log in** — imported users get a freshly generated password,
shown once in the `portainer_import` job result. If that job output is gone, reset the
password in Portainer.

**"That file is not a JSON migration bundle"** — a Portainer `.tar.gz` backup was
uploaded. It cannot be imported directly (it only restores into a pristine Portainer);
open it with a throwaway Portainer and export a bundle first.

**A deploy fails with "the Portainer data disk … already exists"** — durable state is on
and the volume holds an admin whose password isn't in Settings. Portainer ignores
`--admin-password` on an initialized database, so the node would come up with a password
nobody knows. Set `portainer_admin_password` to the one the volume was created with, or
delete the volume to start clean. Same on all three clouds.

**SSL certificate errors** — for self-signed certificates (including a managed node's)
turn off **Verify SSL certificate** in the Portainer panel. For production, add your
CA cert to the container's trusted store via the Dockerfile.
