# Troubleshooting Gateway hosts

> **Audience:** operator · **Profile:** `both` · **Read this when:** a Gateway host will not join, or a jump through it connects and then drops.

Part of [BeyondTrust Gateway Hosts](../gateways.md).

**The Gateways tab isn't there** — it is gated on `pra_enabled`. Toggle
**BeyondTrust** on under **Settings → Integrations**; it applies immediately, no restart.

**"The `<cloud>` gateway could not be started"** — the ensure path returned nothing because
a prerequisite is missing: the deploy key, the project, or the gateway subnet. The job log
names the specific one. Check **Settings → Integrations → Privileged Remote Access**.

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

**A Web Jump times out after its node was recreated** — on GCP and AWS the Rancher and
Portainer nodes take an ephemeral address, so a recreate moves them, and a Web Jump carries
the URL it was made with. (An Azure-hosted node keeps its Standard, static IP, so it does not
hit this.) The registration now compares the two and re-points the jump item (destroy +
recreate) when they disagree; before that it reused the stored id unconditionally, so the
item kept dialling the dead address and no redeploy could converge it. If you are on an
older build, tear the node down (**Stop**, which removes the jump item) rather than
redeploying it.

**A Gateway shows in PRA but no node is connected — and the host was rebuilt** — PRA keeps
the **dead node**. A recreated host registers under the same Gateway *name* but from a fresh
address, so the appliance ends up listing the old node alongside the new one, and the
Gateway can read as disconnected while something is in fact connected. **Compare node
counts** before and after: that, not the name, is what tells the two apart. This is the
usual explanation for an agent-installed Gateway whose job went green while PRA looked
empty — see [A third kind](agent-host.md).

**I can't remove the managed gateway** — by design; it is created and reclaimed by the
reference-counted lifecycle. Remove whatever still
[holds a reference](../gateways.md#why-the-managed-one-is-reference-counted) and the idle teardown
reclaims it.

## A Web Jump connects, then drops — "the endpoint has disconnected"

The gateway ran out of memory. **A Web Jump renders the target UI in a headless Chromium
on the gateway itself** (`sra-web.bin`) — unlike a tunnel, which only forwards bytes — so
a host sized for tunnels is not sized for this. At 1 GB the kernel OOM-kills the renderer
and every session on that node dies at once.

It is a nasty one to read from symptoms, because *which* error you get depends on timing:

| When it dies | What PRA shows |
|---|---|
| Mid-session | "Waiting for Reconnect — the endpoint has disconnected" |
| Before the session is up | "internal timeout starting session" — identical to a blocked firewall |

The second is the trap: it sends you to the node firewall, which may be perfectly correct.
Distinguish them at the source — on a GCP gateway:

```bash
gcloud compute instances get-serial-port-output <gateway> --zone <zone>
```

A confirmed kill looks like `Out of memory: Killed process … (sra-web.bin)`, usually with
Chromium's own `ThreadPoolServi invoked oom-killer` just above it and `global_oom` (the
whole VM, not a container limit). The Containers page cannot tell you this — its `RUNNING`
badge is the *instance* status, so a VM whose gateway container is dead still reads healthy.

Defaults are now sized for a Web Jump (`gcp_jumpoint_machine_type` = `e2-medium`, Azure
`Standard_B2s`, AWS `bt_ecs_host_instance_type` = `t3.small`). A gateway built before that
keeps its old size — the launcher is idempotent on name and reuses the VM without touching
its shape, so raising the setting is not enough. Resize it in place, or delete it and let
the next ensure rebuild it. **Either way its ephemeral IP changes**, so the node firewalls
need the new `/32`: a gateway deploy/teardown re-applies them, or add it manually.

Concurrency is the usual trigger — one session may fit where two do not. Budget for the
number of Web Jumps you expect to have open at once, not for one.

## The AWS host never joins the cluster

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
