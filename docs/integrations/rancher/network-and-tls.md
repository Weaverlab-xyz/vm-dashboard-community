# Rancher: network access and TLS

> **Audience:** operator · **Profile:** `demo` · **Read this when:** a cluster, your browser or Entitle cannot reach the Rancher node, your network TLS-inspects outbound traffic, or you want a publicly trusted certificate on the node.

Part of [Rancher](../rancher.md).

How traffic reaches the Rancher node: the source allow-list the dashboard keeps for you, getting through a TLS-inspecting corporate proxy, and replacing the node's self-signed certificate.

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
  CIDR that already contains the detected IP instead of clobbering it. The last few
  detected `/32`s also stay admitted (`rancher_dashboard_egress_recent`, bounded), so
  a host whose *own* outbound address is not fixed — an Azure Container Apps
  environment with no NAT Gateway, for instance — does not lock the deploy out of the
  node it just launched. That failure is distinctive: the readiness poll needs only
  **one** attempt to land on the admitted address and passes, then the bootstrap needs
  several **consecutive** calls and is dropped, so the job reports "serving" and
  "cannot reach it" seconds apart. On Azure Container Apps the dashboard now reads
  the environment's published outbound pool and admits all of it, given one Reader
  grant. See
  [Outbound addresses](../../operations/cloud-hosting.md#outbound-addresses-and-the-managed-node-firewalls).
  Elsewhere, the durable fix is a stable egress (a NAT Gateway) or a manual pool CIDR.
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
- **Web-Jump Gateways** — when the [PRA Web Jump](../rancher.md#pra-web-jump-optional) is
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
enable, and every **gateway deploy or teardown** — plus **Re-apply the node firewall**
on the Rancher tab, on demand. It stays **fail-closed** — if there are no manual
CIDRs, no provisioned clusters, and no captured Gateway IP, the node is not opened
(unless *Allow open* is ticked). The **Settings → Kubernetes** panel shows the
computed allow-list read-only.

### Letting your own browser in

Editing the allow-list and **applying** it are two different actions, and this is the
one thing that catches everybody: **saving `rancher_allowed_source_cidrs` in Settings
writes config — it does not touch the cloud.** The Settings readout then shows your
address, because it renders the set that *would* be applied, so the IP looks allowed
while the rule on the node is still the one the last deploy wrote. Nothing recomputes
it until one of the events above.

1. **Find the address the node will see.** Any IP-echo works:

   ```bash
   curl -s https://api.ipify.org
   ```

   In PowerShell, `curl` is an alias for `Invoke-WebRequest` — use `curl.exe -s https://api.ipify.org`,
   or `(Invoke-RestMethod https://api.ipify.org)`.

   Your **browser** may not egress from that address. A corporate proxy or VPN
   (Cloudflare WARP, Zscaler) can route the browser and the shell differently, and WARP
   egresses from a **pool**, so consecutive requests leave from different IPs. The
   reliable check is to ask the browser itself — open <https://api.ipify.org> in the
   browser you'll use for Rancher. If it disagrees with the shell, or if the answer
   changes on reload, allow the **pool's CIDR** (e.g. `104.28.182.0/24`) rather than a
   single `/32`.
2. **Add it** in Settings → Kubernetes → *Allowed source CIDRs*, as `<ip>/32`, comma-separated
   with anything already there, and save.
3. **Apply it**: Containers → Kubernetes (Rancher) → **Re-apply the node firewall**.
   It re-detects the dashboard's own egress, re-merges the whole set and rewrites the
   rule, then tells you what it is now allowing. Safe to click on a healthy node, and
   safe to click twice.

> **"Allow open" only fires when the CSV is empty.** `<cloud>_rancher_allow_open`
> substitutes `0.0.0.0/0` for an *empty* `rancher_allowed_source_cidrs` — it is not an
> override. With one address in the CSV the tick does nothing. To open the node to
> everyone while keeping specific entries visible, put `0.0.0.0/0` in the CSV itself.

### Direct access for Entitle grantees (no PRA)

The [PRA Web Jump](../rancher.md#pra-web-jump-optional) is optional, and an Entitle grant doesn't
need it: the grantee gets an ephemeral Rancher account (username = their Entitle
email) and can sign in at the node's URL directly. But *their browser* then hits the
same source-restricted rule you do, and Entitle's own egress ranges don't help —
those admit **Entitle's cloud**, which is what creates the account, not the human who
uses it.

So for direct access, every grantee's egress has to be in the allow-list:

- **Everyone behind one corporate egress** (office, VPN, SASE) — add that **pool
  CIDR** once. This is the common case and the tidiest.
- **Grantees anywhere** (home, mobile, a customer site) — you cannot enumerate those.
  Either put `0.0.0.0/0` in `rancher_allowed_source_cidrs` and accept that the node is
  internet-reachable (it is a lab node behind Rancher's own login, and the grant is
  time-boxed), or keep it closed and broker access through the Web Jump after all.

Either way, finish with **Re-apply the node firewall** — and remember the node is
[ephemeral](../rancher.md#ephemeral-node): on GCP and AWS a recreate moves the node's address, not
the allow-list, so the CIDRs you added stay valid and the URL you shared does not.

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

## Corp TLS inspection (API transport)

Corporate networks that **TLS-inspect** outbound traffic (e.g. Cloudflare
Gateway/WARP) verify the *origin's* certificate at the proxy — and the Rancher
node ships a **self-signed cert**, so the proxy kills every HTTPS handshake to it
in transit. The dashboard's `verify=False` can't help: the block happens at the
proxy, not the client. The symptom is a deploy that fails with *"Rancher IS up …
but the HTTPS handshake is being terminated in transit"* (the readiness probe
falls back to plain-HTTP `/ping` to detect exactly this), while `curl -k` to the
node dies after ClientHello.

**`runner` fixes the dashboard, not your browser.** The two failures look
identical but are not the same problem. The runner transport moves the
*dashboard's* API calls off the inspected path; a human opening the Rancher UI is
still on it, and still gets a dead handshake. Only a certificate the proxy will
accept — or a proxy exception — fixes browser access. Pick accordingly:

| Route | Fixes the deploy | Fixes your browser | Cost |
|---|---|---|---|
| `rancher_acme_domain` | yes | **yes** | a DNS record; port 80 public |
| `rancher_api_transport = runner` | yes | no | an in-cloud runner per API call |
| Proxy *Do Not Inspect* rule | yes | yes | you must control corp policy |

Three ways out:

1. **`rancher_acme_domain` — a publicly trusted certificate.** Rancher's built-in
   ACME client gets a Let's Encrypt certificate for a name you own and renews it
   itself, so the proxy verifies the origin successfully and inspects normally.
   This is the only option that also fixes the UI in a browser. See
   [Public certificate](#public-certificate-lets-encrypt) below.
2. **Proxy exception** — add a *Do Not Inspect* rule for the node's IP in the
   proxy policy. Zero dashboard changes, but you may not control corp policy, and
   on GCP/AWS the node's IP is ephemeral so the rule rots on the next recreate.
   (On Azure the node's Standard public IP is Static and survives a recreate.)
3. **`rancher_api_transport = runner`** — the dashboard executes every Rancher
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
   runs with a public address and cannot route to the node at all. It is pinned to
   the node's location the same way AWS is: a delegated subnet is regional, so a node
   outside the default `azure_location` needs that region's own `aci_subnet_id` and
   `resource_group` (Settings → Multi-region; the Azure sandbox emits both). Without
   them the group lands in the default region's VNet, where `AllowVnetInBound` does
   not apply and nothing is peered — so the probe just times out. The runner now
   fails fast naming `aci_subnet_id` instead.

   Request payloads (API token, bootstrap password) travel to the job as a curl
   config over stdin — never in the container's argv.

   **Runner jobs are serialised — one at a time.** There is one Rancher node, so
   two runner jobs in flight against it are always two overlapping sequences (a
   deploy's first-run alongside a cluster import, say) rather than parallel work,
   and each costs a cold start and its own cloud resource. Every launch — including
   the readiness probe — queues behind the one in flight. The wait is capped at 30
   minutes and then **fails open**: the caller launches anyway rather than wedging,
   which is safe because each job's cloud resource is named per invocation. This is
   per process: it covers everything the job worker runs, which is where the long
   chains are, but not a job overlapping the inline **Import cluster** request.

   **Every API call is a whole container cold start, and it is not always quick.**
   A typical call is ~20-60 s, but they are not bounded by that: on Azure ACI a
   single first-run call was measured at **15 m 12 s** (2026-09-21) while its three
   siblings each took ~60 s. The launcher's own ceiling is ~20 minutes per call
   (it polls 120 × 10 s), so a deploy can legitimately sit in one step for that
   long. The deploy reports which of the four first-run steps it is on, and the
   worker log has a `Rancher API (runner): <METHOD> <path>` line on each side of
   every call with its elapsed time — check those before concluding a deploy is
   hung — see [Troubleshooting](../rancher.md#troubleshooting).

**Downstream clusters are unaffected** either way — cattle-cluster-agents dial out
from their cloud NAT, not through your corp proxy. The Rancher **UI** in your
browser rides the same inspected path though: if the proxy blocks the self-signed
UI too, use the [PRA Web Jump](../rancher.md#pra-web-jump-optional) (the Gateway egresses from
the cloud, cleanly) or a proxy exception.

## Public certificate (Let's Encrypt)

Set **`rancher_acme_domain`** (Settings → Kubernetes → *Public certificate
domain*) to an FQDN you control and the node serves a publicly trusted,
auto-renewing Let's Encrypt certificate instead of its self-signed one, using
Rancher's own built-in ACME client. Leave it blank for the self-signed default.

This is the fix for a TLS-inspecting proxy, and unlike the runner transport it
fixes **browser** access too.

### Preconditions

Both are enforced, not assumed:

1. **An A record for the name must already point at the node's external IP.**
   Nothing here can create it — it lives at your registrar. The deploy resolves
   the name and **fails fast** if it is missing or points elsewhere, naming the
   record to create. Without that check the container's ACME order fails silently
   inside the node and the deploy reports a readiness timeout, which reads like a
   slow image pull.
2. **Port 80 is opened to `0.0.0.0/0`** for the HTTP-01 challenge. Let's Encrypt
   validates from addresses it does not publish, so this cannot be
   source-restricted. It is a **separate rule** (`<node>-acme` on GCP,
   `allow-acme-http01` in the NSG on Azure, an extra permission on the AWS
   security group) — **443 stays source-restricted** to the merged allow-list.
   The opening is permanent, because renewal re-validates roughly every 60 days.
   Clearing `rancher_acme_domain` revokes it on every cloud.

### Consequences

* **The node is addressed by name, not by IP.** A certificate cannot cover a bare
  IP, and connecting to an IP literal sends no SNI for a proxy to match on. So
  `rancher_server_url` becomes `https://<your-domain>` and the readiness probe
  follows it. Re-pointing server-url means **agents on already-imported clusters
  keep dialling the old address** until re-imported — do this before importing
  clusters, or plan the re-import.
* **Let's Encrypt allows 5 certificates per exact name per week,** and the node
  does not persist ACME state (`/var/lib/rancher` is not on a durable disk), so
  **every redeploy issues a fresh certificate**. Five redeploys in a week and
  issuance is refused until the window rolls. Avoid redeploy loops once ACME is on.
* **Changing the domain on a LIVE node requires replacing it.** A deploy reuses a
  running node, and a container's arguments are fixed when it is created, so the
  change cannot otherwise take effect. The deploy detects this and **fails** rather
  than reporting a success that changed nothing; tick *Replace the node if its
  container arguments changed* to apply it. Replacing wipes Rancher's state --
  users, settings and imported clusters, which must be re-imported -- because that
  state lives inside the container with no volume behind it.
* **On Azure the node keeps its address while a certificate domain is set.** The
  Standard/Static public IP is normally deleted with the VM; with ACME configured
  the teardown leaves it, so a replace, a teardown or a sweep does not move the
  address your A record points at. It costs a few dollars a month while nothing is
  attached. Clearing `rancher_acme_domain` restores the old behaviour, so the next
  teardown releases the address -- and the record goes stale. **GCP and AWS keep
  their ephemeral addresses**, so there the record must be re-pointed after any
  recreate.
* A `.app`, `.dev` or other HSTS-preloaded domain is fine. Browsers force HTTPS on
  those names, but Let's Encrypt's validator is not a browser and ignores preload,
  so HTTP-01 still works.

### Setup

1. Create the A record: `rancher.example.com` → the node's external IP (shown on
   Containers → Kubernetes). On Azure that address is Static and survives a
   recreate; on GCP and AWS it is ephemeral, so re-check it after one.
2. Set *Public certificate domain* to `rancher.example.com` and save.
3. Redeploy the node. The firewall step opens port 80, the DNS pre-flight runs,
   and Rancher obtains the certificate during startup — first boot takes somewhat
   longer than a self-signed one, so raise `rancher_ready_timeout_s` if it is tight.
4. Open `https://rancher.example.com`. Reaching it **by name** is the point;
   the IP will still fail behind an inspecting proxy.
