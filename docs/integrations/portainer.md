# Portainer CE Integration

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you already run Portainer and want the dashboard to drive it.

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

## The pages

This page covers connecting a Portainer you run, Docker hosts, and what the integration
gives you. Three more are under [`portainer/`](portainer/managed-server.md):

| Page | Read it when |
|---|---|
| [A managed server](portainer/managed-server.md) | you want the dashboard to deploy Portainer CE for you on AWS, Azure or GCP: what it creates, its firewall, Web Jump and teardown |
| [Just-in-time access through Entitle](portainer/entitle.md) | you want people to request Portainer access in Entitle and get an account for the duration |
| [Importing from another Portainer](portainer/import.md) | you are moving users, teams and registries from an existing Portainer into this one |

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

The dashboard deploys and bootstraps Portainer CE on a VM in AWS, Azure or GCP: see [Portainer: a managed server](portainer/managed-server.md). For just-in-time access to it, see [Portainer through Entitle](portainer/entitle.md).

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

Step 2 can be a job instead of a paste:
[`portainer-edge-env-ensure.yml`](../../examples/playbooks/portainer/portainer-edge-env-ensure.yml)
runs both halves against a **VM** target — it creates the environment over the API
(minting a fresh key, so a stale one can't be the problem) and then installs and starts
the agent on that host. It is idempotent: a host that already has the agent is left
alone unless you pass `force_rejoin: true`, and the agent id is derived from the node
URL and the environment name, so a re-run rejoins as the same agent rather than
registering a second environment for one host. Docker has to be on the host already —
[`linux/install-docker.yml`](../../examples/playbooks/linux/install-docker.yml) if it
isn't.

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

Merge users, teams, team memberships and registries from another Portainer into this one: see [Importing from another Portainer](portainer/import.md).

## Option B — connect your own Portainer server

### Step 1 — Create a Personal Access Token

1. Log in to Portainer → click your username (top right) → **My account**.
2. Scroll to **Access tokens** → **Add access token**.
3. Give it a name (e.g. `vm-dashboard`) and copy the token string.

### Step 2 — Configure in the dashboard

**Setup wizard (first run)** — toggle **Portainer** on the wizard's **Feature Flags**
step. That step carries toggles only; fill in the fields afterwards, in the same place
as **after first run** — **Settings → Integrations → Portainer CE**:

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
| `portainer_dashboard_egress_recent` | (runtime) | Bounded CSV of recently-detected egress `/32`s, also admitted — covers a host with no stable outbound address |
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
| `portainer_adapter_source_cidr` | `""` | Subnet range(s) of the `portainer_access` Entitle adapter function, CSV (runtime-set when you deploy it; auto-added to the firewall, cleared when you remove it). A VPC firewall applies to intra-VPC traffic too, so without this the adapter reaches the node's internal IP and is dropped |

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

**"Cannot reach Portainer: ConnectTimeout"** — the TCP connect got no answer, so the
packets are being *dropped*: an ingress rule, not a closed port (a closed port
answers with a reset). On the managed node, click **Re-apply the node firewall** (see
[Re-applying it](portainer/managed-server.md#re-applying-it)); **Mint an API token** does the same repair on its
own way past, so the message you are left with there already says what the rule now
allows. Compare that with where the dashboard actually egresses from: if it has no
stable outbound address, set `portainer_dashboard_egress_cidr` to the whole range
rather than a single address (a corporate proxy pool). On Container Apps the dashboard
reads its own pool instead, so check *Hosting platform outbound pool* in **Settings →
Containers**: an error there names the missing grant
([Outbound addresses](../operations/cloud-hosting.md#outbound-addresses-and-the-managed-node-firewalls)).
If the message says the URL is **not a node this dashboard deployed**, the firewall
in front of that Portainer is yours to open.

**Deploy fails with "the Portainer node's firewall is closed"** — no allowed source
CIDRs, and the dashboard couldn't auto-detect its own egress IP. Set
`portainer_dashboard_egress_cidr` or `portainer_allowed_source_cidrs` in **Settings →
Containers** — or enable that cloud's `*_portainer_allow_open` — then redeploy.

**Deploy fails: "cannot be placed in \<region\>: … is not set for that region"** — the
chosen region has no configured subnet (and, on AWS, no VPC). That is deliberate:
falling back would put the node in the *default* region's network while the form, the
job and the row all said otherwise. Run that cloud's sandbox setup for the region, or
add a per-region config under **Settings → Multi-region**.

**Azure: deploy fails at 30% with "(InvalidRequestContent) … Could not find member
'hardware_profile' on object of type 'ResourceDefinition'"** — Azure rejected the VM
create request itself, so nothing was provisioned (no VM, no cost) and the job has no
output. It is a dashboard-side bug, not a permission, quota or region problem: the
`azure-mgmt-compute` SDK ≥ 38 sends a raw request body straight through to Azure
instead of translating it to the REST shape. Fixed in this version — the Azure node
launcher builds the request from SDK model objects — so **rebuild/redeploy the
dashboard image** and deploy again. The same request kills the Rancher node deploy,
since both land in the same launcher.

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
is running and usable; click **Mint an API token** on the Containers page (or add one
by hand in **Settings → Containers**) once you can sign in to it.

**Deploy times out waiting for Portainer to start** — the VM is up but nothing
answered. Usually the firewall doesn't admit the dashboard's egress IP; check the
merged allow-list in **Settings → Containers**, then redeploy. Raise
`portainer_ready_timeout_s` if the image pull is simply slow.

**"Connection refused" or timeout** — verify the Portainer URL is reachable from
inside the container: `docker compose exec app curl -Isk <portainer-url>/api/system/status`.

**"Unauthorized" error** — the stored token is not one this Portainer knows: it was
deleted, or its Portainer DB was. For a managed node, click **Mint a new API token**
on the Containers page. For your own Portainer, regenerate a token there and update
**Settings → Integrations → Portainer CE** (or the vault secret, if you stored a
reference).

Note that Portainer answers **`{"message": "Invalid JWT token"}`** here whatever kind
of credential you sent — that is its generic message for a rejected one, not a request
for a JWT. This dashboard and the adapter both authenticate with `X-API-Key`.

**The Entitle adapter reports a Portainer 401 while the Containers tab works fine** —
they read different copies of the token. The Containers tab reads `portainer_pat`; the
adapter reads the copy staged in the cloud's secret store when it was paired. Click
**Re-send the token to the adapter**.

**Every Entitle grant times out, and the adapter card says `available`** — the node was
relocated and the adapter was not. Entitle reports it from its own side, as a connect
timeout against the adapter's endpoint, because the adapter itself never answers: it is
VPC-attached in the region the node *used* to be in. Look for the red **STRANDED**
badge on the adapter card, or for `adapter_stranded` in the node deploy's job result.
Re-pair it — **Remove adapter**, then **Deploy adapter** — as described under
[Moving the node strands the adapter](portainer/entitle.md#moving-the-node-strands-the-adapter). Re-sending
the token will not help; the token was never the problem.

**The adapter's `/check_config` names an unresolved `@Microsoft.KeyVault(...)`
reference** — on Azure the token arrives as a Key Vault reference that the *platform*
resolves, and an identity that cannot read the secret leaves the setting as written
instead of failing. Grant the Function App's Key Vault reference identity `get` on the
vault. (The adapter refuses to send a reference as a credential, which is why you get
this instead of Portainer's 401.)

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
