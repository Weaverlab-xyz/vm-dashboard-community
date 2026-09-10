# Cloud VMs

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are deploying cloud VMs and want the full access and onboarding story.

The dashboard deploys **cloud virtual machines** across AWS, Azure, GCP, and OCI, then
layers the BeyondTrust PAM stack on top — the same **provisioning + three layers** model
as [Databases](databases.md) and [Kubernetes](kubernetes.md):

- **Provisioning** *(stand it up)* — launch an instance into a **private** subnet and inject
  an SSH key. Done directly through each cloud's **SDK** (not Terraform — see Architecture).
- **Layer 1 — PRA** *(reach it)* — broker a BeyondTrust **Shell Jump** so an operator can
  SSH the private VM through the PRA representative console.
- **Layer 2 — Password Safe** *(manage its secrets)* — *optional.* Onboard the VM as a
  Password Safe managed system + managed account so Password Safe rotates its credential.
- **Layer 3 — Entitle** *(grant time-boxed access)* — *optional.* Register the VM for
  **SSH ephemeral accounts** so users request just-in-time access.

| Cloud | Provisioning | L1 PRA (Shell Jump) | L2 Password Safe | L3 Entitle |
|---|---|---|---|---|
| **AWS** | EC2 (Linux + Windows) | ✅ | ✅ `ssm` plugin (or `ssh`) | ✅ SSH ephemeral |
| **Azure** | VM (Linux + Windows) | ✅ (Linux; Windows → RDP jump) | ✅ `azurevm` plugin (or `ssh`) | ✅ SSH ephemeral |
| **GCP** | GCE (Linux) | ✅ | ✅ `gcpvm` plugin (or `ssh`) | ✅ SSH ephemeral |
| **OCI** | Compute (Linux) | ✅ (shared gateway, or bring your own¹) | ⚠️ `ssh` method only | ✅ SSH ephemeral |

¹ OCI has no dashboard-provisioned gateway — you supply your own (see the OCI section).

Unlike the other features, **cloud VM deploy has no feature toggle** — it's core
functionality available whenever a cloud's credentials are configured, gated only by RBAC
(`require_permission("aws"|"azure"|"gcp"|"oci", …)`). **Windows** is supported on **AWS and
Azure** only.

---

## Architecture

A deploy is orchestrated by a per-cloud `_run_deploy` background job and runs directly
against the cloud **SDK** (`boto3` / `azure-sdk-for-python` / `google-cloud` / `oci`), not
Terraform. (Terraform VM modules exist under `terraform/ec2_instance`, `terraform/azure_vm`,
`terraform/gce_instance` for a separate CLI-oriented path, but `/api/*/deploy` does **not**
use them.)

There is exactly **one** `_run_deploy` per cloud, and it is the only place that cloud's
SDK is asked to create a VM. A batch does not repeat it: `_run_bulk_deploy` acquires
whatever is genuinely shared for the run — the gateway, and on AWS the NAT instance,
SSM endpoints and SSH key; on Azure the quota check, ACI container and Key Vault key —
then loops calling `_run_deploy` with those injected. Each instance still owns its own
job row, so one failure fails that row and the batch carries on.

That shape is load-bearing rather than tidy. When the batch path was a second copy of
the deploy body it drifted, and every divergence was invisible because a batch still
reported success: AWS batches stopped passing `os_type` and booted Linux VMs with no SSM
agent, and Azure batches ignored the per-deploy Gateway key. `tests/test_deploy_runner_parity.py`
asserts no `_run_bulk_deploy` calls its cloud's launch function directly.

Ordered steps (each Layer-1/2/3 step is **non-fatal** — a failure logs a warning and the
deploy still succeeds):

1. **Ensure the gateway host** (only when `pra_enabled`) — AWS uses a shared
   ref-counted ECS host, Azure the shared `clouddb-jumpoint` VM (see
   `azure_vm_jumpoint_mode`), GCP the shared COS host (see `gcp_vm_jumpoint_mode`), and
   OCI a shared Oracle Linux instance when `oci_vm_jumpoint_mode` is `shared` —
   **`none` by default**, which keeps OCI's historical bring-your-own behaviour. In a
   batch this happens once for the whole run.
2. **Ensure on-demand egress** — AWS: the shared **NAT instance** (`aws_nat_instance_enabled`)
   plus **SSM interface endpoints** (`aws_ssm_endpoints_enabled`). GCP: a **Cloud NAT**
   gateway for the VM subnet plus the egress allow rule (`gcp_vm_nat_enabled`). Both are
   ref-counted and reclaimed when the last VM goes; Azure and OCI do nothing here.
3. **Fetch the SSH public key** from the cloud's secret store and inject it (Linux via
   cloud-init / `admin_ssh_key` / `ssh-keys` metadata; Windows skips key injection).
4. **Launch the instance** (SDK).
5. **Layer 1** — broker the PRA **Shell Jump**.
6. **Layer 3** — Entitle SSH-ephemeral registration (opt-in).
7. **Layer 2** — Password Safe onboarding (opt-in).

VMs land in a **private** subnet with **no direct internet egress** and are reachable only
from the gateway (SSH/22); see [Cloud Sandbox](CLOUD_SANDBOX.md) for the per-cloud network
topology. Entry points: `/aws`, `/azure`, `/gcp`, `/oci` (per-cloud deploy + image browser)
and `/vms` (unified cross-cloud inventory).

---

## Power (start and suspend)

A cloud VM used to be deploy-or-destroy. Every on-prem hypervisor here has had power
control for its whole life; the four clouds had none, so an operator who wanted a VM off
overnight used the cloud console — which puts this dashboard's inventory out of step with
reality.

Each cloud page now has **Start** and **Suspend** on every instance row, and a bulk
toolbar for a whole selection — see [Powering a selection](#powering-a-selection).

`POST /api/{aws,azure,gcp,oci}/power/start` and `/power/stop`, with the instance
identifier in the **body**, matching every other `/power/*` route here. Each queues a job,
so the action gets an audit row, a `/jobs` entry and Live Output like any other.

Requires `write` on that cloud, not `delete` — stopping a VM changes its state, it does
not remove it — plus the same ownership check destroy uses: you can power what you can
see, and an untagged VM is admin-only.

**What "stop" means is not the same word on every cloud**, and the wrong choice is
expensive and silent:

| Cloud | What the dashboard calls | Why not the obvious one |
|---|---|---|
| AWS | `StopInstances` | Never Hibernate: it must be enabled at launch, is unsupported on most families, and silently degrades to a plain stop where it is not |
| Azure | `begin_deallocate` | `begin_power_off` leaves the VM "Stopped" and **still billing for compute** |
| GCP | `instances.stop` | `suspend` preserves RAM to disk and charges for that storage plus the reserved resources; `stop` reaches TERMINATED, where only disks bill |
| OCI | `SOFTSTOP` | A hard `STOP` pulls the cord and risks a dirty filesystem on resume |

**Stopping saves compute and nothing else.** Disks, public addresses and reserved capacity
keep billing. A stopped VM is cheaper, not free.

Power is deliberately **not** behind [Action Guardrails](policy-guardrails.md), where
destroy is. A reversible action earns a lighter brake than an irreversible one, and a
change-freeze that forbade *suspending* a VM would forbid the cheapest thing an operator
can do during one.

### Powering a selection

Tick the checkbox on any instance row — or select-all — and the toolbar above the list
offers **Start** and **Suspend** for the whole selection. It queues **one job per
instance**, all sharing a batch id, and takes you to `/jobs?batch_id=…`, which counts them
by status as they go.

The same two ops as the rows, and nothing more: no cloud path here has a shutdown, a
restart or a reset, so there is nothing to leave out. Every instance in a selection goes
through the same code as its own row button, which means the same `write` permission, the
same per-VM ownership check, and the same discovery-derived region or resource group. An
instance you cannot reach fails on its own without stopping the rest — the response names
each one and why.

Four things worth knowing before ticking fifty boxes:

- **They do not all run at once.** These are ordinary jobs, and the worker's light tier
  admits a few at a time (3 by default). Fifty instances is fifty jobs draining in waves,
  and on Azure and GCP each one waits for the cloud to finish — a deallocate is minutes.
  Bulk saves the clicking, not the waiting. OCI is the quick one: its `SOFTSTOP` is
  fire-and-forget.
- **Instances already in the target state are skipped, and the dialog says how many.**
  Suspending a selection of sixteen where five are already stopped sends eleven. An
  instance whose state the page does not know is *included*, not skipped: a job that turns
  out to be unnecessary says so, where a silent skip does not.
- **Azure is the exception worth reading.** A VM the Azure portal has *stopped* without
  deallocating is not running **and is still billing for compute** — so Suspend is still
  offered for it, because deallocating is the thing that stops the bill.
- **Fifty per operation**, then it refuses and asks you to narrow the selection. The same
  cap and the same wording as a bulk Config-Management run.

**Suspending can break a BeyondTrust wire-up, and bulk Suspend warns you.** A VM wired
into PRA, Password Safe or Entitle at a **public** address comes back on a different one —
none of the four clouds guarantees an auto-assigned public address across a stop, and
there is no repair short of destroy-and-recreate. That is the same rule
[suspend schedules](#suspend-schedules-all-four-clouds) refuse on, and the same reasons:
an Azure VM whose private address is not pinned, and a VM under Password Safe
auto-management, count too.

Bulk Suspend does **not** refuse those. The per-instance button never has — ownership is
the gate — and a toolbar that refused what a row allows would be worse than useless. It
tells you instead: the confirmation names how many of the selection are affected, and the
result names which ones, so you know what to go and check. Start never warns; nothing has
moved yet.

### VMs this dashboard did not deploy

Every cloud console here is **job-driven**: it starts from completed `*_deploy` jobs and
fetches live state for exactly those identifiers. That is what makes the console a record of
what the dashboard did — and it means a VM somebody launched in the cloud's own console, in
Terraform, or before this dashboard existed is invisible, and your only lever on it is the
provider's UI.

Turn on **`cloud_unmanaged_discovery_enabled`** (Settings → Integrations → *Discover unmanaged
cloud VMs*, off by default) and each cloud gains a second listing:

| | Managed | Discovered |
|---|---|---|
| Endpoint | `GET /api/{cloud}/instances` (`/vms` on Azure) | `GET /api/{cloud}/unmanaged` |
| Source | completed `*_deploy` jobs | every instance in the account / subscription / project / compartment |
| Power | ✅ | ✅ |
| Destroy | ✅ | ❌ **never** |
| Suspend schedule | ✅ | ❌ — a schedule lives on the deploy job row, and there isn't one |

**Off by default** because it lists everything rather than the identifiers the deploy jobs
name: more cloud calls, and on a real estate a great many more rows.

**A VM is "discovered" when the dashboard has no deploy job for it *and* it carries none of
the dashboard's own tags.** Both halves matter. The tag half is what keeps a VDI pool seat, or
a VM whose job row was pruned, in the managed list where it belongs — those are the
dashboard's, and they still have a Destroy button.

**Who can see one.** A discovered VM has no `Job.workgroup`, so its workgroup comes from a
`workgroup` tag (or GCP label) if it has one, and otherwise it has none — which makes it
**admin-only**. That is not a new rule; it is what every cloud module's `_assert_can_act`
already says about an untagged resource. Tag your own VMs for workgroups and non-admins see
them; don't, and only admins do.

**Destroy is absent, not hidden.** There is no destroy route on the discovery module and
nothing in a discovered row resolves into one. Separately, `api/azure`'s destroy fan-out — the
one path that can terminate a VM with no deploy job, which exists for VDI seats — now asks
whether the VM carries a dashboard tag before it acts. It did not before: any VM in a listed
resource group could be destroyed by name, which discovery would have made easy to find.

**How power reaches a discovered VM.** AWS needs a region and Azure a resource group, which
the deploy job used to supply. Both now take it **from the discovery listing** — never from
the request, because a caller-supplied resource group would turn `/power/stop` into "deallocate
any VM of this name anywhere the credentials reach". A VM discovery does not show is still a
404. (GCP and OCI already accepted a power call with no deploy job: a GCE instance is named by
zone, which the request carries, and an OCID is globally unique. That behaviour is unchanged
and is not gated by this flag.)

### OCI's shared gateway

OCI was the one cloud where the dashboard provisioned nothing inside the VCN — you brought
your own gateway. That gap reached further than it looked. With nothing in the VCN to broker
a session, the OCI deploy could not assume a private address was reachable, so it wired the
**public** one into every jump item, Password Safe system and Entitle registration; and
because an auto-assigned public address does not survive a stop, those instances could not
carry a suspend schedule either. One missing host, three consequences.

Set **`oci_vm_jumpoint_mode = shared`** and OCI behaves like the other three: a small Oracle
Linux instance runs the BeyondTrust gateway container privileged with `/dev/net/tun` (the
capabilities a protocol tunnel needs), reference-counted by `jumpoint_host_service` so it is
created on the first deploy that needs it and terminated when the last resource using it
goes.

**`none` is the default**, because turning this on creates a billable instance and an upgrade
must never do that on its own. Left at `none`, nothing changes: no host, public-first
wire-up, exactly as before.

| Key | Default | What it is |
|---|---|---|
| `oci_vm_jumpoint_mode` | `none` | `none` (bring your own) or `shared` (dashboard-managed) |
| `oci_jumpoint_host_name` | `oci-shared-jumpoint` | the gateway **instance's** display name |
| `oci_jumpoint_subnet_ocid` | — | gateway VNIC subnet; falls back to `oci_default_subnet_ocid` |
| `oci_jumpoint_docker_deploy_key` | — | BeyondTrust gateway deploy key; falls back to `bt_jumpoint_docker_deploy_key` |
| `oci_jumpoint_image_ocid` | — | blank resolves the newest Oracle Linux platform image |
| `oci_jumpoint_shape` / `_ocpus` / `_memory_gbs` | `VM.Standard.E4.Flex` / 1 / 6 | the gateway instance's size |

**Watch the name.** `oci_jumpoint_host_name` is the compute instance; **`oci_jumpoint_name`**
is the PRA Gateway a Shell Jump binds to. They are different things that share a word, and
GCP resolves the same collision the other way round (there `gcp_jumpoint_name` *is* the
instance), so do not reason about one from the other.

**What changes when you turn it on.** New OCI deploys are wired at their **private** address
instead of their public one — which is what makes them schedulable (below). Instances
deployed before you turned it on keep the address they were wired at; `terraform_pra_service`
has no update path, so there is nothing to migrate them with short of redeploying.

The gateway instance carries the dashboard's `managed-by` tag, so it appears in the managed
listing rather than in unmanaged discovery.

### Suspend schedules (all four clouds)

A business-hours power window: suspend at 19:00, resume at 07:00, weekdays only. Set per
VM; off until you set one, and the feature itself is behind
`vm_suspend_schedule_enabled` (default off).

`PUT /api/suspend/{deploy_job_id}` with `suspend_at`, optional `resume_at`, an IANA
`timezone` and a 7-character `days` mask (Monday first). `GET` reads it back along with
whether the VM may have one; `DELETE` clears it — clearing never changes the VM's current
power state, only stops it happening again.

**The rule is BOUNDARY CROSSED, not "should it be asleep now."** Those differ in exactly
the case that matters: start a VM by hand at 20:00 for a call, and a state check would
suspend it again on the next sweep four minutes later, forever. A boundary check leaves it
alone until tomorrow's suspend time — your action wins until the schedule next has
something new to say. After an outage that swallowed both a suspend and a resume, the
later crossing wins, so the VM ends where the schedule says it should be now.

A schedule that has never been evaluated acts on nothing. Setting one cannot suspend a VM
for boundaries crossed before it existed — the same arming rule the auto-delete timer uses.

**No cloud is schedulable unconditionally.** The rule is one question asked per VM: *does the
address this VM was wired at survive a stop?* Everything below is that question.

**That address is recorded, not inferred.** Each runner picks one and hands it to the PRA jump
item, the Password Safe managed system and the Entitle registration; it is stored as
`wired_address`. Three runners prefer the **private** address; **OCI prefers the public one**,
because OCI is the one cloud where the dashboard provisions no gateway in the VCN (see step 1
above — "bring your own"). So "does this VM have a private address?" answers the right question
on three clouds and the wrong one on the fourth. Rows written before `wired_address` existed
are *reconstructed* by replaying the runner's fixed preference, which gives the same answer
that runner gave rather than a guess.

| Cloud | What happens |
|---|---|
| **AWS, GCP** | Schedulable as deployed. The private address survives a stop, and it is the one the wire-up used. |
| **Azure** | ARM releases a `Dynamic` private address when a VM is deallocated, so it can return on a different one. The address is **pinned** before a schedule is allowed: `Dynamic` → `Static` at the address the NIC already has. New deploys pin themselves; an older VM is pinned the first time somebody schedules it, audited as `azure_address_pinned` and named in the response. **The address does not change** — only ARM's freedom to reclaim it does. A static *private* address is free on Azure. |
| **OCI** | Schedulable when the wire-up used the private address — which is either a deploy with `assign_public_ip=False`, or **any** deploy made while `oci_vm_jumpoint_mode = shared` (see above). Wired publicly, it is refused. |

Note what the Azure pin does **not** do: it never picks an address. `pov_cloud_azure` does pick
one — scan the resource group, take the lowest free — which is safe because each POV
environment owns its resource group. An estate shares one and deploys in bulk, so two deploys
in flight would choose the same address and the second would fail. Ratifying the allocation ARM
has already made cannot collide with anything.

Two cases where the pin cannot help, and an Azure VM is refused anyway: one currently
**deallocated** has no address to pin (start it, then set the schedule), and one deployed
before the NIC name was recorded has to be set to Static in the Azure portal by hand.

Two more refusals apply on all four clouds, each with the reason returned to the caller:

- **A VM wired into BeyondTrust at its public address.** None of the four guarantees an
  auto-assigned public address across a stop. This one has **no remedy short of redeploying**
  without a public address: the address is already inside a jump item, a Password Safe system
  and an Entitle registration, and none of the three can be updated in place. It applies only
  to a VM that was actually registered somewhere — an unregistered VM has told nothing its
  address, so nothing breaks when it moves.
- **A VM under Password Safe auto-management.** AWS onboards via the `ssm` plugin, GCP via
  `gcpvm`, Azure via `azurevm` and OCI via plain `ssh`; all four reach the guest through
  something that cannot reach a stopped instance. Password Safe rotates on its own clock, which this dashboard cannot
  pause, so a nightly suspend would mean a nightly rotation failure. Detach
  auto-management if you want the VM scheduled — that is a decision for you to make, not
  one for this to make quietly on your behalf.

Both are checked **before** the Azure pin, so a VM that is going to be refused for one of
them never has its NIC written to on the way to being told no.

The sweep runs every `vm_suspend_sweep_interval_minutes` (default 10) and never powers
anything itself: it enqueues the identical `*_power` job the Suspend button creates, so
the audit row, the `/jobs` entry and the workgroup all come out the same either way.


### Spend caps

The suspend schedule above answers *"when may this be off?"*. This answers the question an
operator on their own cloud account actually loses sleep over: **"how much may it cost?"** A
clock is a poor proxy — the same fortnight is twenty dollars or two thousand depending on
what was deployed, and the second only becomes visible on an invoice weeks later.

Behind **`vm_spend_cap_enabled`** (Settings → Integrations → *VM spend caps*, off by
default). `PUT /api/spend/{deploy_job_id}` with `cap_usd`; `GET` reads it back with what has
accrued; `DELETE` clears it — and keeps the accrued total, because that is a record of what
this VM has cost, and zeroing it would let a clear-and-re-add reset the meter by accident.

**The number is accrued, not read off a bill.** Every sweep adds *rate now × time since the
last sweep* to a running total on the deploy row. A bill lags a day on all four clouds, so a
cap that read one would report a runaway rather than stop one; and Cost Explorer bills per
request. Accrual reacts within one sweep, works identically on every cloud, and needs no new
API or permission.

**It is a list-price estimate.** No Savings Plans, reservations, credits, free tier, data
transfer or snapshots. It **errs high**, which is the only safe direction for a cap.

**`warn` is the default action.** Reaching the cap suspends only if you set it to — and
suspending is reversible, which is what lets this feature exist without the auto-delete
timer's arming clocks and dry-run mode. The worst outcome is a VM somebody starts again.

| Behaviour | Why |
|---|---|
| A cap is **refused** on a VM whose region has no price source | `accrue` treats a missing rate as *move the clock on, bill nothing*. Stored anyway, such a cap reads `$0.00 of $500.00` forever and the operator believes they are protected. The refusal names the cloud and region. |
| A cap that stops being priceable later is **reported** each sweep | Same lie, arriving after the fact. |
| A cap set to `suspend` on a VM that cannot be suspended is refused | An unpinned Azure address or a publicly-wired OCI instance — see the schedule refusals above. Under `warn` the same cap is accepted, because a warning does work. |
| The first sweep after a cap is set **accrues nothing** | A NULL "last measured" means never measured, and billing an unbounded interval would charge for every hour since deploy. The same arming rule the schedule latch uses. |
| A long outage accrues at most `MAX_ACCRUAL_HOURS` (24h) in one step | A dashboard that was down for days cannot know the VM ran the whole time, and a restart must not invent a bill big enough to trip every cap at once. |
| A VM the dashboard has suspended stops accruing compute | Read from the dashboard's own `*_power` jobs, not a live cloud call. A VM stopped in the cloud's own console still counts as running — wrong in the safe direction. |

**Known gap, inherited deliberately from the POV implementation:** the cap latches, so
restarting a VM that was suspended by its cap leaves it running past that cap. Raise the cap
to re-arm it.

The sweep runs every `vm_spend_sweep_interval_minutes` (default 10) and, like the schedule
sweep, powers nothing itself — it enqueues the identical `*_power` job the Suspend button
creates. Its query is scoped to rows that carry a cap, so an estate that sets none does no
writes at all.

## Provisioning — per cloud

Each cloud reads its credentials + a default subnet + an SSH-keypair secret from config
(emitted by the sandbox setup script). The **admin/SSH keypair** is stored in the cloud's
own secret store and retrievable per instance from the UI.

### Deploying more than one VM

Every deploy form has a **Count** (1–20). Leave it at 1 and nothing changes. Set it higher
and the base name is expanded into a numbered series — `web` × 3 becomes `web-01`,
`web-02`, `web-03` — with the form previewing the exact names before you submit.

A count above 1 creates one `*_bulk_deploy` **parent** job plus one `queued` child per VM,
all sharing a `batch_id`; the browser lands on `/jobs?batch_id=…`, which rolls the batch up
into total / running / failed. The children deploy **sequentially** inside a single worker
slot, so a batch of N takes roughly N × the single-deploy time and occupies one of the
`WORKER_REPLICAS` slots for the duration — that, plus default cloud vCPU quotas, is why the
ceiling is 20 (`MAX_DEPLOY_COUNT` in [`services/vm_naming.py`](../web_dashboard/services/vm_naming.py)).

Names are expanded by truncating the *base*, never the numeric suffix, so a series stays
unique at any provider's length limit. Two limits are worth knowing:

* **Azure batches are budgeted to 15 characters**, not the 64-char ARM limit, because the
  in-guest hostname is derived as `vm_name[:15]`. A longer base would give every VM in the
  batch the same hostname, which breaks Entitle and Password Safe onboarding — both key off
  hostname. Single deploys are unaffected.
* **GCP names must be RFC1035** (lowercase, leading letter, hyphens); a base that isn't is
  rejected with a 400 rather than silently rewritten.

Names are checked against VMs the dashboard has already deployed *or is deploying*, and a
clash returns **409** rather than creating anything. That matters because Azure, GCP and OCI
resolve a destroy by first match on name, so duplicates would make a later teardown
ambiguous.

### The two ways to deploy several VMs

All four clouds offer both, and they are different operations:

| | **Count** (on the deploy form) | **Bulk Deploy** (on the image list) |
|---|---|---|
| What it makes | N copies of **one** image | one VM **per selected image** |
| Names | auto-numbered from a base | typed per VM |
| Where | the deploy modal | tick images, then the *Bulk Deploy (n)* button |

Both produce the same job shape — one `*_bulk_deploy` parent plus one `queued` child per
VM, sharing a `batch_id` — so both land on the `/jobs?batch_id=` rollup and both share a
single Gateway for the run.

Use Count for "five identical lab boxes"; use Bulk Deploy for "one each of these three
images". GCP and OCI gained Bulk Deploy after AWS and Azure, so older screenshots may show
their image lists without checkboxes.

Policy guardrails ([Policy Guardrails](policy-guardrails.md)) are enforced **per VM** on every
path — count batches and multi-select bulk included — before any job row is created.

### AWS (EC2)

Sandbox: [`scripts/sandbox/Linux/setup-aws.sh`](../scripts/sandbox/Linux/setup-aws.sh).
Creates the VPC + a **private VM subnet** (`10.99.2.0/24`, local-only), the **VM security
group** (egress to the VPC only, ingress SSH/22 from the gateway SG), the **NAT** + SSM
endpoint SGs, a Secrets Manager **SSH keypair** secret, the ECS `bt-jumpoint` cluster, and
the scoped IAM user (`ec2:RunInstances/…`, `ec2:*KeyPair*`, `GetPasswordData`, `iam:PassRole`
for the SSM instance profile, and `ssm:SendCommand`/`GetCommandInvocation` for PS-SSM).

| Key | Default | Notes |
|---|---|---|
| `aws_region` | `us-east-2` | default region (Settings) |
| `ec2_ssh_key_secret` | — | Secrets Manager keypair secret (JSON) |
| `ec2_ssm_instance_profile` | — | instance profile attached at launch (SSM) |
| `aws_default_subnet_id` / `aws_default_security_group_id` | — | deploy-form default subnet + VM SG (import-only) |
| `aws_nat_instance_enabled` | `false` (sandbox `true`) | on-demand ref-counted NAT instance for VM egress |
| `aws_ssm_endpoints_enabled` | `false` (sandbox `true`) | on-demand SSM interface endpoints (private-subnet PS-SSM reach) |
| `aws_ecs_docker_deploy_key` + `bt_ecs_*` | — | shared gateway host (Layer 1) |

Deploy VMs into the **private** subnet. Enable the NAT instance if the VM needs outbound
internet (e.g. `apt`/`yum`). Windows AMIs are auto-detected (key injection skipped;
retrieve the password via `GET /api/aws/instances/{id}/ssh-key` / the console).

### Azure (VM)

Sandbox: [`scripts/sandbox/Linux/setup-azure.sh`](../scripts/sandbox/Linux/setup-azure.sh).
Creates the RG + VNet with a **vm-subnet** (`10.99.2.0/24`, NSG denies Internet egress,
allows VNet), an **aci-subnet** for the ACI gateway, a Key Vault **SSH keypair** secret,
and a service principal with **Contributor** on the RG.

| Key | Default | Notes |
|---|---|---|
| `azure_resource_group` / `azure_location` | `vm-cli-rg` / `centralus` | RG + default region |
| `azure_default_subnet_id` | — | deploy-form default VM subnet (import-only) |
| `azure_key_vault_url` / `azure_ssh_keypair_secret_name` | — / `azureVM-ssh-keypair` | SSH keypair secret |
| `azure_ssh_username` | `azureuser` | default Linux login |
| `azure_aci_subnet_id` / `azure_aci_docker_deploy_key` | — | ACI gateway (Layer 1) |
| `azure_jumpoint_subnet_id` | — | subnet for the shared VM gateway (falls back to `azure_aci_subnet_id`) |
| `azure_vm_jumpoint_mode` | `shared` | `shared` (the ref-counted `clouddb-jumpoint` VM) or `aci` (a container group per VM) |

Azure single deploys borrow the **shared, ref-counted gateway VM** that cloud databases,
k8s tunnels and VDI seats already use, following `azure_vm_jumpoint_mode` (editable under
**Settings → Integrations → Privileged Remote Access → Shell Jump provisioning**, so the choice is reversible without a
redeploy). Batches still share one ACI container group. Two things override the mode: a
deploy supplying its own **Gateway deploy key** always gets ACI (the shared host resolves
its key from config, so there is nowhere to honour a per-deploy override), and `aci` mode
restores the pre-2026-07 per-deploy container.

`shared` is the default because ACI has two limits a real VM does not:

* **No protocol tunneling.** ACI is serverless and cannot grant `NET_ADMIN` / `NET_RAW` /
  `IPC_LOCK` or `/dev/net/tun`, so an ACI-brokered VM gets a Shell Jump but never a
  Protocol Tunnel. The shared VM runs the container privileged (`azure_service.run_vm_jumpoint`).
* **One shared identity store.** Every ACI group gets a random name
  (`bt-jumpoint-azure-<uuid8>`) but they all mount the same `/jpt` Azure File share, which
  is where the Gateway persists its identity. Successive groups contend over that one
  install, and once the `.installed-<key-hash>` marker disagrees with what is on disk the
  container **crash-loops** (`ExitCode 1`, no log output) and never registers with PRA. The
  Containers page still shows it *Running*, because that is the ACI **group** state — check
  `containers[0].instanceView.currentState` for `CrashLoopBackOff`. Recovery: empty the
  `jpt` share so the next container reinstalls clean.

Which shape a VM used is recorded as `jumpoint_mode` on its deploy job; destroy releases a
shared reference and lets `jumpoint_host_service` decide, or stops the ACI group when no
sibling VM still references it.

Windows is supported: the dashboard generates + vaults a local-admin password, retrievable
via `GET /api/azure/vms/{name}/admin-password`. Windows VMs use an **RDP jump**, not the
SSH Shell Jump.

### GCP (GCE)

Sandbox: [`scripts/sandbox/Linux/setup-gcp.sh`](../scripts/sandbox/Linux/setup-gcp.sh).
Creates a **vm-subnet** (`10.99.2.0/24`, **no** Cloud NAT → no internet egress by default)
and a **jumpoint-subnet** (Cloud NAT), a firewall `…-allow-ssh-from-jumpoint`, a Secret Manager
SSH keypair, and a service account. The dashboard **auto-attaches** `gcp_default_network_tag`
(`dashboard-sandbox-vm`) to every VM so the firewall applies — and, when `gcp_vm_nat_enabled`
is on, opens on-demand egress for the vm-subnet at deploy time (see below).

| Key | Default | Notes |
|---|---|---|
| `gcp_project_id` / `gcp_region` / `gcp_zone` | — / `us-central1` / `us-central1-a` | project + default region/zone |
| `gcp_network` / `gcp_subnetwork` | `default` / — | VPC + VM subnet |
| `gcp_ssh_key_secret_name` | — | Secret Manager keypair secret |
| `gcp_ssh_username` | `gcp-user` | default Linux login |
| `gcp_jumpoint_subnetwork` / `gcp_cloud_run_docker_deploy_key` | — | COS gateway subnet + deploy key (Layer 1) |
| `gcp_vm_jumpoint_mode` | `shared` | `shared` (one ref-counted host) or `paired` (an `e2-micro` per VM) |
| `gcp_vm_nat_enabled` | `true` | on-demand ref-counted Cloud NAT + egress rule for VM internet |
| `gcp_vm_nat_name` / `gcp_vm_egress_rule_name` | `dashboard-sandbox-vm-nat` / `dashboard-sandbox-vm-egress-ondemand` | names of the two on-demand resources |
| `gcp_vm_egress_rule_priority` | `900` | must beat the sandbox's priority-1000 egress deny (lower wins) |

#### On-demand VM egress

The sandbox denies VM internet through **two independent gates**: the vm-subnet is left off
the shared Cloud NAT, *and* `…-deny-vm-egress` denies all egress at priority 1000 on the VM
network tag (a priority-999 rule allows the sandbox supernet back, which is why SSH still
works). Opening one gate alone changes nothing.

Rather than making the sandbox permanently open — which bills for egress infrastructure
serving VMs that don't exist — the dashboard opens both **by reference count**, the GCP
analog of `aws_nat_instance_enabled`. On the first VM deploy it adds a **second** Cloud NAT
gateway to the sandbox's existing Cloud Router (scoped to the vm-subnet's primary range) plus
a priority-900 egress ALLOW; when the last VM **in that region** is destroyed it removes both.
The count is region-scoped because each sandbox region has its own Cloud Router, so a VM in
one region never pins another region's gateway.

The sandbox's own NAT and deny rule are never modified — both halves are separately named,
additive resources. That matters more than it looks: `routers.patch` replaces the `nats` list
wholesale, so the existing gateways are read back and re-sent verbatim; dropping them would
cut the PRA Gateway's own egress, which is the SSH path to every VM. No-ops quietly when the
region has no Cloud Router, so non-sandbox projects are untouched.

GCP deploys borrow the **shared, ref-counted gateway host** that cloud databases, k8s
tunnels and VDI seats already use — one host, rather than an `e2-micro` per VM. Batches
always share. Single deploys follow `gcp_vm_jumpoint_mode` (`shared` by default,
`paired` for the pre-2026-07 behaviour of a dedicated `bt-jumpoint-<vmname>`), editable
under **Settings → Integrations → Privileged Remote Access → Shell Jump provisioning** so the choice is reversible without a
redeploy.

Two things override the mode. A deploy that supplies its own **Gateway deploy key** is
always paired — the shared host resolves its key from config, so there is nowhere to
honour a per-deploy override on it. And the shared host lands on the
`jumpoint_subnetwork` (the only sandbox subnet with Cloud NAT) rather than the VM
subnet; reachability is unaffected either way, because the sandbox SSH rule is
tag-based (`--source-tags bt-jumpoint`) and so applies VPC-wide.

Which shape a VM used is recorded as `jumpoint_mode` on its deploy job, and destroy
handles both: a paired gateway is deleted once no sibling VM references it, a shared
one only has its reference released. Rows predating the field are inferred as paired,
so no migration is needed.

### OCI (Compute) — read the caveats

Sandbox: [`scripts/sandbox/Linux/setup-oci.sh`](../scripts/sandbox/Linux/setup-oci.sh).
Creates a compartment + VCN (`10.98.0.0/16`) with a **public subnet** (IGW, for your
gateway), a **vm-subnet** (`10.98.2.0/24`, NAT Gateway egress, no public IP), a scoped IAM
user + API keypair, and (best-effort) a KMS vault SSH-keypair secret.

| Key | Default | Notes |
|---|---|---|
| `oci_tenancy_ocid` / `oci_user_ocid` / `oci_fingerprint` / `oci_private_key` (+`_passphrase`) | — | API-signing identity |
| `oci_region` | `us-ashburn-1` | **all OCI deploys land here** regardless of the form's region |
| `oci_compartment_ocid` / `oci_vcn_ocid` / `oci_default_subnet_ocid` | — | compartment + VCN + vm-subnet |
| `oci_ssh_key_secret` / `oci_ssh_username` | — / `opc` | keypair secret + default login |
| `oci_freetier_enforce` | `true` | warn-and-confirm gate (below) |

> ⚠️ **OCI caveats.** (1) **No dashboard-provisioned gateway** — the deploy never ensures
> one; you must pre-create a PRA Gateway in the OCI public subnet and point
> `oci_bt_jump_group_name` / `oci_jumpoint_name` (or `bt_*`) at it. (2) **Region is fixed to
> `oci_region`.** (3) **Free-tier gate** — the form defaults to Always-Free
> (`VM.Standard.E2.1.Micro` / `A1.Flex`); a larger shape is rejected (HTTP 400) unless the
> request sets `acknowledge_charges=true`. The gate is evaluated over the whole request,
> so a **Count** that would exceed the envelope trips it even when each VM is individually
> free — three free micros is one more than the tier allows. Changing the count clears any
> acknowledgment you had already ticked. (4) **SDK-only** (no Terraform VM module),
> Linux-only, no per-region config sets. (5) **The shape has to exist in your availability
> domain and the image has to support it** — OCI does not offer every shape in every AD of a
> region (the free AMD micro `VM.Standard.E2.1.Micro` exists only in the older ones;
> `us-chicago-1` offers no E2 shape at all), and an image boots only on the shapes it was
> built for (the other free shape, `A1.Flex`, is Ampere — it needs an `aarch64` image). So
> the shape list is **scoped to the AD and the image**, and changing either refetches the
> picker (deploy form and Packer build form both); a shape the new scope no longer offers is
> **cleared**, not substituted, and the form says why. The picker narrows through the same
> lookup as `oci_service.check_launch_placement`, and **every entry point gates on that
> lookup too** — the Packer build route and runner, plus `/api/oci/deploy` and
> `/api/oci/bulk-deploy` — so an unlaunchable pairing is refused with HTTP 400
> `shape_not_launchable`, naming the shape and listing what would work, instead of becoming
> a bare `404 NotAuthorizedOrNotFound` — or a `400 InvalidParameter` naming shape and image —
> a second into the job. That gate is what covers the
> paths the picker doesn't: an API client bypassing the form, and the bulk modal, whose list
> is not scoped at all (no AD picker; one shared shape across N images) — there **every**
> selected image is checked against the shared shape before anything is created.

---

## Layer 1 — PRA (Shell Jump)

When `pra_enabled` and PRA is configured (`bt_api_host`, `bt_client_id`,
`bt_client_secret`, `bt_jump_group_name`, `bt_jumpoint_name`), every Linux deploy brokers a
PRA **Shell Jump** via `terraform_pra_service.provision_jump(tag=<cloud>)` (the `beyondtrust/sra`
provider), routed through the cloud's gateway host. The jump is removed on destroy from its
stored state.

Jump Group / Gateway resolution: per-deploy form `jump_group` / `jumpoint_name` → the
per-cloud override (`azure_bt_jump_group_name`/`azure_jumpoint_name`,
`gcp_bt_jump_group_name`/`gcp_jumpoint_name`, `oci_bt_jump_group_name`/`oci_jumpoint_name`) →
the `bt_*` defaults. AWS + Azure also accept a per-deploy `pra_credential_ref` (overrides
`bt_client_secret`). **Windows Azure VMs** skip the SSH jump — use an RDP jump.

The shared gateway host, deploy keys, and PRA OAuth setup are described in the
[Privileged Remote Access](integrations/privileged-remote-access.md) doc.

---

## Layer 2 — Password Safe (VM onboarding)

*Optional* (`passwordsafe_registration_enabled` + a per-deploy **"Onboard into Password
Safe"** toggle). Onboards the built VM as a Password Safe **managed system + managed
account** (the baked-in `adminuser`), so Password Safe rotates its credential. Per-cloud
method: **AWS `ssm`** (AWS Systems Manager plugin, DNS `{instance-id}:{region}`), **Azure
`azurevm`** (Azure VM SSH Rotation, address `tenant/sub/rg/vm`), **GCP `gcpvm`** (GCP VM SSH
Rotation, `projectId/zone/instance`), each with an `ssh` fallback. **OCI uses the `ssh`
method only** (no cloud-native plugin) and therefore needs SSH line-of-sight from a Resource
Broker / Gateway.

This is documented in full — plugin uploads, per-cloud methods, the `adminuser` account, and
the config-key table — in the [Password Safe](integrations/password-safe.md) doc's
**"Password Safe VM onboarding"** section. Off-boarding is automatic on VM destroy.

---

## Layer 3 — Entitle (SSH ephemeral accounts)

*Optional* (`entitle_registration_enabled` + a per-deploy **"Register in Entitle"** toggle).
Registers the VM as an Entitle **SSH Ephemeral Accounts** integration so users request
just-in-time SSH access; Entitle mints a short-lived account per grant, using the VM's own
build keypair and `sudo` as the image's cloud-default user (`ubuntu`/`ec2-user`/`azureuser`/
`gcp-user`, override `entitle_ssh_sudo_user`).

- **Public VM** → registered with no agent.
- **Private VM** (the sandbox default) → attaches the **shared Entitle agent** (Kubernetes,
  one per VPC) via `entitle_agent_token_name`.

Requires `entitle_owner_id` + `entitle_workflow_id`. See the [Entitle integration](integrations/entitle.md)
doc. A separate **machine-identity JIT** track (the AWS `elevate()` wrapping of
`ec2_deploy`/`ec2_terminate`) is covered in [design/cloud-identity-jit.md](design/cloud-identity-jit.md).

---

## Images

Deploy from a stock marketplace/public image or one the dashboard's Packer flow built
(`/images/aws|azure|gcp`). The **BT-ready provisioners** under
[`provisioners/beyondtrust/`](../provisioners/beyondtrust/) harden sshd and create the
cloud-default `adminuser` login with passwordless sudo — the account both the Entitle
`sudo_user` and the Password Safe managed account rely on. Full build/promote/export flow is
in [image-management.md](image-management.md).

---

## Lifecycle & troubleshooting

- **Destroy** (`DELETE /api/{cloud}/instances|vms/{id}`) removes the instance, deregisters
  the PRA Shell Jump (from stored state), and off-boards Password Safe / Entitle if they were
  wired. AWS reclaims the shared NAT instance + SSM endpoints when the last VM is gone; GCP
  reclaims the on-demand Cloud NAT + egress rule when the last VM **in that region** is gone.
- **VM can't reach the internet** — by design (private subnet). On AWS enable
  `aws_nat_instance_enabled`; on OCI the vm-subnet already has a NAT Gateway; on GCP enable
  `gcp_vm_nat_enabled` (default on). Note GCP denies egress through **two** independent gates —
  the vm-subnet is left off the sandbox Cloud NAT *and* a priority-1000 rule denies egress on
  the VM network tag — so opening only one changes nothing. DNS still resolves either way
  (the metadata server is always reachable), so the symptom is a **connection timeout**, not a
  name-resolution failure. Egress is ensured at **deploy** time: a VM created before the
  feature was enabled needs a redeploy, or the gateway + rule created by hand.
- **Shell Jump shows Unavailable** — the gateway host didn't start; set the cloud's deploy
  key (`aws_ecs_docker_deploy_key` / `azure_aci_docker_deploy_key` /
  `gcp_cloud_run_docker_deploy_key`). On **OCI** you must supply your own gateway.
- **Can't SSH the VM** — the VM SG/NSG only allows SSH from the gateway; reach it through the
  PRA Shell Jump, not directly.
- **OCI deploy rejected (HTTP 400)** — a non-free-tier shape without `acknowledge_charges`;
  tick the acknowledge box or pick a free-tier shape. With a **Count**, the whole batch is
  measured against the envelope, so this can fire on a shape that is free on its own.
- **OCI deploy rejected (HTTP 400, `shape_not_launchable`)** — the shape can't launch that
  image in that availability domain: either the AD doesn't offer the shape, or the image
  doesn't support it (architecture, usually). The message lists the shapes that *do* work
  for that image there — pick one. `LaunchInstance` reports both cases, and a genuine IAM
  denial, as the same unattributed `404 NotAuthorizedOrNotFound` about a second into the
  job, which is why this is checked up front instead. It applies to `POST /api/oci/deploy`
  (including its `count` fan-out) and to `POST /api/oci/bulk-deploy`, where the shape is
  shared but **every** selected image is checked — one bad image rejects the whole
  selection and creates nothing, like a name collision does. A blank `availability_domain`
  is checked against the AD the deploy would actually use (the first one). Reaching this
  from the deploy form means the picker fell open (see the empty-picker entry below); from
  an API client it needs no picker at all. The check deliberately **fails open**: if the
  lookup can't reach OCI the deploy proceeds, so a bare 404 that survives it is most likely
  the IAM case — check the policies on the compartment holding the subnet and the image.
- **OCI Shape reset itself to “— pick a shape —”** — expected: the shape list is scoped to
  the selected availability domain and image, and the one you had isn't offered in the new
  scope. The note under the picker names it. Choose from the narrowed list; the form won't
  submit until you do. The same happens on load in a region without
  `VM.Standard.E2.1.Micro`, whose Always-Free default the form drops rather than POST.
- **OCI shape picker is empty** — two different causes, and the note under the picker says
  which, because they need opposite fixes:
  - *"No shape offered in … can launch this image"* — the AD does offer shapes, and this
    **base image** can boot none of them. Change the image, not the AD. The note lists what
    the AD offers; if those are all `A1`/`A2` (Ampere), you need an `aarch64` image. This is
    common in trial tenancies, where `ListShapes` is scoped by your **service limits** rather
    than by what hardware exists — a tenancy with quota only for Ampere sees three shapes in
    every AD of the region and cannot launch an x86 image anywhere in it.
  - *"OCI lists no shapes in …"* — OCI returned nothing for that AD at all (a failed
    `ListShapes` looks identical to an empty one). Try another AD, or **Refresh**.

  The narrowing still falls open when a lookup is *silent* — the tenancy's policy blocks
  `ListImageShapeCompatibilityEntries`, or the image publishes no entries — so the picker
  never offers less than `check_launch_placement` accepts. In that fallen-open state the
  advisory architecture warning and the `shape_not_launchable` gate above are your only
  hints, so mind them.
- **Deploy rejected (HTTP 409, `vm_name_collision`)** — the names this deploy would create
  are already taken by VMs the dashboard deployed or is deploying. Pick a different base
  name, or destroy the existing VMs first. The check is deliberately strict: Azure, GCP and
  OCI resolve a destroy by first match on name, so duplicates make teardown ambiguous.
- **A batch child is stuck `queued`** — children are created unclaimable on purpose and are
  driven by their `*_bulk_deploy` parent. Check the parent (same `batch_id`): if it failed
  or was reconciled away, its children have nothing to drive them.

For the sandbox network topology see [Cloud Sandbox](CLOUD_SANDBOX.md); for day-2 Ansible
against deployed VMs see [Config Management](config-management.md).
