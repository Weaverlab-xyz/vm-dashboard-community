# Troubleshooting Rancher

> **Audience:** operator · **Profile:** `demo` · **Read this when:** Rancher will not come up, a cluster will not import, or its Web Jump will not connect.

Part of [Rancher Kubernetes Management](../rancher.md).

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

**Azure: deploy fails at 30% with "(InvalidRequestContent) … Could not find member
'hardware_profile' on object of type 'ResourceDefinition'"** — Azure rejected the VM
create request itself, so nothing was provisioned (no VM, no cost) and the job has no
output. It is a dashboard-side bug, not a permission, quota or region problem: the
`azure-mgmt-compute` SDK ≥ 38 sends a raw request body straight through to Azure
instead of translating it to the REST shape. Fixed in this version — the Azure node
launcher builds the request from SDK model objects — so **rebuild/redeploy the
dashboard image** and deploy again. The same request kills the Portainer node deploy,
since both land in the same launcher.

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
exception for the node — see [Corp TLS inspection](network-and-tls.md#corp-tls-inspection-api-transport).

**Deploy sits at 85% "Completing Rancher first-run" for a long time** — on
`rancher_api_transport=runner` this stage is **four sequential API calls**, and each
one is a whole container cold start. Usually ~60 s apiece, but not bounded by that:
one was measured at **15 m 12 s** on Azure ACI (2026-09-21) while the other three
took ~60 s. The launcher's own ceiling is ~20 minutes per call, so up to ~80 minutes
in this stage is slow, not hung. The progress message names the step currently
running (`1/4: changing the admin password` … `4/4: clearing the first-login
prompt`), and the worker log brackets each call with
`Rancher API (runner): <METHOD> <path>` lines carrying its elapsed time. If a call
does hit the ceiling the stage still finishes — first-run is best-effort by
contract, so the deploy completes and the job note says what was skipped; you finish
that wizard step by hand once. On `direct` transport this stage takes seconds.

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
[Ephemeral node](../rancher.md#ephemeral-node).
