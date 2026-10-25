# Powering hypervisor VMs through an agent

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want VMs on an agent-run hypervisor started, stopped or suspended, one at a time or as a selection.

Part of [Hypervisor connections through an agent](../hypervisors.md).

Every hypervisor page, and the Workstation page, has a checkbox on each row. Tick some and
the toolbar above the table offers **Start**, **Shutdown**, **Force Off** and that
product's restart — the same four operations as the row buttons, over the whole selection.
It queues **one job per VM**, all sharing a batch id, and takes you to
`/jobs?batch_id=…`, which counts them by status as they go.

Nothing about the verbs changes. Each VM in a selection goes through the same code as its
own row button, so the same `policy.yaml` grant is required, an op with no verb for that
product is greyed out in the toolbar exactly as it is on the row, and a VM you cannot
reach fails on its own without stopping the rest — the response names each one that could
not be queued, and the reason.

Four things worth knowing before you tick fifty boxes:

- **They run one at a time, not all at once.** An agent leases a single job, finishes it,
  then asks for the next; a connection the dashboard dials directly is walked the same way
  on purpose. So twenty VMs is twenty sequential jobs and the last one starts last — bulk
  saves you the clicking and the twenty trips back to the page, not the waiting. The batch
  page is where you watch the queue drain.
- **One inventory sync, not one per VM.** The automatic sync above collapses across the
  whole batch: the last job to finish queues it, and it sees every VM the batch moved.
- **VMs already in the target state are skipped, and the dialog says how many.** Start over
  a selection of sixteen where five are running sends seven — the confirm names both
  numbers before anything is queued. A VM whose power state the page does not know is
  *included*, not skipped: an agent-synced row may carry no state at all, and a job that
  turns out to be unnecessary says so out loud, where a silent skip does not.
- **Fifty VMs per operation.** Above that it refuses and asks you to narrow the selection,
  the same cap and the same wording as a bulk Config-Management run.

Only **Start** goes without a confirmation, matching the row button. Force Off and the
restarts name the count and say plainly that the guests are not being asked.

Not every button on those pages has one of them behind it, and the ones that do not are
**refused rather than approximated**. `restart` is why the mapping is per product rather
than shared: each kind resolves it differently — Proxmox `/status/shutdown` (graceful),
vSphere `?action=reset` (a hard reset), XCP-ng `VM.clean_reboot` (a reboot), Hyper-V
nothing at all. `shutdown` and `reboot` exist because that made every Shutdown button
unmappable, and one Reboot button too:

| Button | Proxmox | vCenter | XCP-ng | Hyper-V |
|---|---|---|---|---|
| Power On / Force Off | yes | yes | yes | yes |
| Shutdown (graceful) | yes — `/status/shutdown` | yes — `guest/power?action=shutdown`, **needs VMware Tools** | yes — `VM.clean_shutdown` | yes — `Stop-VM`, **needs Integration Services** |
| Reboot / Restart | yes — `/status/reboot` | not offered | yes — `VM.clean_reboot` | yes — `Restart-VM -Force`, a **hard** restart |
| Reset / Hard Reboot | not offered | yes | yes | not offered |
| Suspend / Resume / Pause / Unpause / Save | not offered | **refused** | **refused** | **refused** |

Two of those are graceful in the strict sense — they ask software *inside* the guest. A
vCenter Shutdown is not a power action at all but a call to `/api/vcenter/vm/{vm}/guest/
power`, and it answers 503 when VMware Tools is not running; the agent turns that into a
message saying so, because "answered 503" on a plainly-running VM points nowhere. Hyper-V
Shutdown is bare `Stop-VM`, which Microsoft documents as shutting down "through the guest
operating system" — it carries neither `-TurnOff` (the power cut) nor `-Force`, which on
`Stop-VM` means "regardless of any unsaved application data" and would quietly make it a
different promise. Use Force Off when the guest cannot answer.

Hyper-V is the one product with **no graceful reboot at all**: `Restart-VM` is documented
as a "hard" restart, "like powering the computer down, then back up again", so it is what
the Restart button's `power_reset` runs and there is nothing left for `reboot` to be. The
agent refuses that combination by name rather than letting the runner answer "unknown
verb", which would read as an agent too old for the dashboard.

The mapping is one table, [`agent_hypervisor_meta.PAGE_OPS`][page-ops], keyed by kind —
it was once a copy per router, and three identical copies is how `shutdown` came to hard
-reset a vCenter VM. A refused button is greyed out on the page with the reason in its
tooltip, and the endpoint answers 501 naming the substitution that would have been wrong
and the buttons that do work. The refusal happens before a job row exists, so nothing
appears on /jobs.

**Adding a verb is a three-file change, and a partial one is worse than none.** The
dashboard normalizes an unrecognised verb to `inventory_sync`, so a verb granted here but
missing from an agent would run a discovery scan and report success. The three are this
allowlist, the agent's per-kind maps, and the sibling runner.

Version skew across them is safe in the direction it actually happens — the agent is
deployed separately and lags. An old agent given a new verb refuses it out loud: it reads
the verb raw and never normalizes, so `policy.check_verb` rejects it first, naming
`policy.yaml` and the line to add. **This is why `shutdown` and `reboot` are new verbs
rather than a redefinition of `restart`:** redefining a verb an old agent already
implements, and an old `policy.yaml` already grants, would silently change what that
agent does with a button — the exact failure the whole table exists to prevent.

The corollary is operational: after upgrading the dashboard, **every deployed agent must
be re-pulled** before its Shutdown button works, and until then those agents refuse the
verb in Live Output. Proxmox Shutdown is the one button this affects that worked before —
it used to ride `restart`, which really is `/status/shutdown` on Proxmox alone. It was
moved anyway, because leaving it would have kept "restart means shutdown here" alive as a
per-kind special case, and that reading is what made Reboot unmappable in the first place.

[page-ops]: ../../../web_dashboard/services/agent_hypervisor_meta.py

**`snapshot`** creates a snapshot named `dash-<job id>`. The name is *generated*, never
supplied — which is exactly why it was held back at first: a created thing needs a name,
and a name is a free-form string. There is still no field in the protocol through which
operator text could reach a hypervisor, and the job id makes the snapshot traceable back to
the row that made it, which a typed name would not be.

Deliberately absent: **deploy / clone / delete / console** — they need sizes, networks and
cloud-init, a payload shape indistinguishable from a config file, and a config file is one
step from a script. Those stay dashboard-direct.

`power_off` and `power_reset` are separate verbs rather than one verb with a `force`
flag, because a boolean on a destructive verb gets defaulted wrong exactly once.
