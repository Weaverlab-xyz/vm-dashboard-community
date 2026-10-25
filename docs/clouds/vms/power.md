# Powering cloud VMs

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want VMs started, stopped or suspended, on a schedule or under a spend cap, including VMs this dashboard did not deploy.

Part of [Cloud VMs](../vms.md).

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

Power is deliberately **not** behind [Action Guardrails](../../operations/scheduling/policy-guardrails.md), where
destroy is. A reversible action earns a lighter brake than an irreversible one, and a
change-freeze that forbade *suspending* a VM would forbid the cheapest thing an operator
can do during one.

## Powering a selection

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

To run the selection later, or in the next change window, tick **Schedule** on the same
toolbar. See [Scheduling power operations](../../operations/scheduling/change-windows.md#scheduling-power-operations).

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

## VMs this dashboard did not deploy

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

**Who can see one.** A discovered VM has no `Job.workgroup`, so its workstream comes from a
`workgroup` tag (or GCP label) if it has one, and otherwise it has none — which makes it
**admin-only**. That is not a new rule; it is what every cloud module's `_assert_can_act`
already says about an untagged resource. Tag your own VMs for workstreams and non-admins see
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

## OCI's shared gateway

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

## Suspend schedules (all four clouds)

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
the audit row, the `/jobs` entry and the workstream all come out the same either way.

## Spend caps

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
