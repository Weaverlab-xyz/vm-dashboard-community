# Auto-delete Timer

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want lab resources to clean themselves up -- read it before enabling it, because it deletes infrastructure.

**This feature deletes infrastructure.** A timer that runs out ends in the same teardown the
Destroy button runs — terminated instances, deleted databases, destroyed clusters, none of it
recoverable. Everything below is arranged around making that safe to switch on.

The problem it solves: a lab fills up and nothing tells you. Someone deploys three VMs to
reproduce a bug, gets an answer, moves on, and the invoice explains it a month later. The
Destroy button has always been there; what was missing was anything that pressed it.

Off by default — and **off is not the only brake**. Three more stand behind it, described
under [the four gates](#the-four-gates).

---

## Architecture

```
 STAMP — at request time, in the deploy's own transaction
   job_service.create_job            (VMs)          ──┐
   cloud_database_service.provision                 ──┼──► expires_at
   k8s_service.provision                            ──┘

 SWEEP — app enqueues, worker acts
   main._expiry_sweeper_loop  ──►  enqueue_sweep_if_due()   [app, gunicorn -w 2]
                                       └─ creates ONE `expiry_sweep` job
   jobs_worker._claim_one     ──►  run() → sweep_once()     [worker, replicas: 3]
```

Three properties carry the design:

**`expires_at` NULL means "never" — never "inherit the default."** The predicate that
selects victims cannot return a row without a real timestamp. So enabling the feature on an
existing fleet, whose rows all backfill to NULL, selects **zero resources**. That is a
property of the query, not a guard someone can weaken later. NULL is also how "pinned" is
represented, which is why pinning and clearing a timer are the same write.

**The sweep never implements teardown.** For a VM it enqueues the identical job row the
cloud's own DELETE endpoint creates; for a database or cluster it calls the same
`start_decommission`. PRA tunnel teardown, vault cleanup and Password Safe deletion
therefore happen exactly as they do when a human presses Destroy, and this file stays small.

**The app loop only enqueues.** It creates one `expiry_sweep` job and stops. The app runs
`gunicorn -w 2`, so a loop that swept directly would run two concurrent sweeps forever.
Routing through the queue also buys a Job row on `/jobs`, per-resource Live Output, mid-pass
cancel and stale-job reconcile for free. Sweep-initiated work is attributed to
`expiry-reaper` — a name nobody can log in as, so `created_by` on a job and the audit log
both read unambiguously as machine action.

---

## One row per tick

Both `gunicorn -w 2` workers run the timer, so both reach the same tick within about a
second of each other. Three checks collapse that into a single sweep, and the third is not
redundant:

1. a Postgres advisory lock serializes the two callers;
2. the **active-pass** check refuses while a sweep is queued, pending or running;
3. the **recency** check refuses while a sweep merely *finished* moments ago.

The third exists because the second provably does not hold on its own. `ACTIVE_STATUSES` is
a liveness test, and a sweep with nothing to reap finishes in well under a second — so with
three worker replicas polling every two seconds, the first row is often already `completed`
by the time the second app worker looks. Before the recency check was added, 5 of 55 rows on
a live install were duplicate pairs 0.13–0.4 s apart. **A liveness-based dedupe cannot hold
when the work is instantaneous.**

The window is half the sweep interval: wide enough to swallow a duplicate tick, never wide
enough to suppress the next scheduled one. **Run sweep now** opts out of it, because a human
pressing the button means now.

### Why `/jobs` looks quiet

A sweep records a job whether or not it found anything — 48 rows a day at the default
interval, against a `jobs` table nothing else prunes. Two things keep that from burying real
work:

- **Job History hides completed sweeps.** Tick **Show routine sweeps** to see them.
- **Completed sweeps are deleted past *Keep sweep history*** (7 days by default).

Both rules stop at `completed`. A **failed or cancelled** sweep is always listed, always
kept, and still appears in the dashboard's failed-jobs panel — a pass that broke is the one
you need to see. Retention only ever touches `expiry_sweep` rows: a VM's deploy job row *is*
its inventory record, so pruning `jobs` by age alone would delete VMs from the inventory
while leaving them running in the cloud.

---

## What can carry a timer

| Kind | Reapable | Teardown |
|---|---|---|
| Cloud VM — AWS, Azure, GCP, OCI | yes | the matching `*_destroy` job |
| Cloud database | yes | `start_decommission` |
| Kubernetes cluster | yes | `start_decommission` |

A VM has no inventory table of its own: **its deploy job row *is* its record of existence**,
which is why its inventory id is already `job:<uuid>`.

Deliberately excluded, and not oversights:

- **Proxmox and Nutanix VMs.** Their deletes run in-request rather than as queued jobs, so
  there is no job for the sweep to enqueue and it must not pretend otherwise.
- **Bulk parent jobs.** The parent owns no VM; its children do, and they carry the real
  deploy type. So excluding parents needs no special case.
- **Virtual desktop seats.** A seat's teardown is a pool operation, so expiring one seat
  would silently shrink a live pool.
- **Gateways.** Not in the inventory at all, and the shared one is reference-counted.
- **Anything you *registered* rather than provisioned.** A database or cluster you already
  ran and merely told the dashboard about is never stamped and never swept. The dashboard
  did not create it and will not delete it.

Resources that predate the feature are never stamped either. To put a timer on one, set it
by hand from **Inventory**.

---

## The four gates

Between "overdue" and "destroyed" stand four independent gates. A sweep **reports its full
target list whichever of them is shut**, so you can always see what would happen before
anything does.

1. The feature is enabled.
2. The resource has a stamped `expires_at`. NULL means never — see above.
3. The feature has been enabled for at least an hour.
4. **Allow deletion** is on, **Observe only** is off, and *that* combination has held for at
   least an hour.

Gates 3 and 4 are two separate clocks, and neither can be shortened from Settings:

| Clock | Starts on | Reset by |
|---|---|---|
| Feature arming | the first sweep that sees the feature enabled | — |
| Enforcement arming | the first sweep that sees **Allow deletion** on *and* **Observe only** off | turning Observe only back on, at any point |

The second clock is the non-obvious one, and it exists for a specific scenario: you watch
observe-only for a week, forty resources pass their expiry in that time, and unchecking one
box would otherwise make all forty eligible in the same pass. Turning Observe only back on
clears the clock, so flipping it off again starts a fresh hour.

Both clocks are runtime state, not settings. They are deliberately absent from the Settings
panel and there is a test asserting no operator can reset them.

Beyond the four gates, each individual resource must also be in a healthy idle state — a VM
`active`, a database `available`, a cluster `registered`, `managed` or `awaiting_agent` —
not in an exempt workgroup, and past its grace window. Never a row mid-provision,
mid-decommission or failed. An unrecognised status is refused, so a new state added
elsewhere in the codebase can only ever make the sweep do *less*.

---

## Rolling it out

Four changes, and two waits that are not changes.

1. **Enable the feature** in **Settings → Auto-delete Timer**. This starts the feature
   arming clock and nothing else — every existing resource is NULL, so the first sweeps
   have nothing to report.
2. **Set a default lifetime** above `0`. Until you do, new deploys aren't stamped either and
   the feature is inert twice over. Note this applies to **new deployments only**; anything
   already running stays untimed unless you set one from Inventory.
3. **Wait, and read the sweep reports.** Leave **Observe only** on for a full cycle. The
   `Last sweep` box in the panel summarises the most recent pass, and **Run a sweep now**
   queues one immediately; every pass is a Job row on `/jobs` with full output.
4. **Turn Observe only off and Allow deletion on.** This starts the enforcement arming
   clock. An hour later, deletion is live.

### Settings

Panel labels, with the underlying key, since the keys are what the API and environment use.

| Setting | Key | Default | |
|---|---|---|---|
| Observe only | `resource_expiry_dry_run` | on | Log what would be deleted, delete nothing. |
| Allow deletion | `resource_expiry_enforce` | off | A second, separate gate on deletion. |
| Default lifetime for new deployments | `resource_expiry_default_hours` | `0` | Hours. `0` = don't stamp anything. **The clock starts when the deploy is queued, so it includes provisioning time.** |
| Maximum total lifetime | `resource_expiry_max_total_hours` | `720` | Hours from creation — not from the last extension — so repeated extending can't outrun it. `0` = no ceiling. Admins aren't capped. |
| Extend button adds | `resource_expiry_extend_hours` | `24` | Hours per click. |
| Warn this long before | `resource_expiry_warn_hours` | `24` | Entering this window shows a dashboard warning and fires `resource.expiring`. |
| Grace | `resource_expiry_grace_minutes` | `30` | Minutes past expiry before acting. Absorbs clock skew between app and worker. |
| Sweep every | `resource_expiry_sweep_interval_minutes` | `30` | Read live, so a change lands on the next pass without a restart. Also sets the cadence of everything below — at `30` the sweep records **48 job rows a day**. |
| Keep sweep history | `resource_expiry_sweep_retention_days` | `7` | Days a *completed* sweep row survives on `/jobs`. `0` = keep forever. A failed pass never expires. |
| Max per sweep | `resource_expiry_max_per_pass` | `10` | Bounds the damage rate. Targets go oldest-overdue-first, and the cap counts **deletions, not attempts** — otherwise a few unreapable rows at the front would starve everything behind them forever. |
| Allow admins to remove a timer | `resource_expiry_allow_never` | off | Off means the most anyone can do is extend. |
| Exempt workgroups | `resource_expiry_exempt_workgroups` | *blank* | CSV. **Only VMs carry a workgroup** — databases, clusters and desktop seats don't, so pin those individually instead. |

Settings win over environment variables, which win over the built-in defaults, and every
value is re-read each pass rather than captured at startup.

### Floors you cannot lower

Module constants, not settings, so a misconfiguration is bounded:

| | |
|---|---|
| Minimum lifetime | 60 min — a 5-minute timer on an EKS cluster would expire before the cluster finished provisioning |
| Minimum grace | 15 min |
| Arming delay | 60 min, for both clocks |
| Max per sweep ceiling | 50 |

---

## Extending and pinning

The **Expires** column on **Inventory** shows the timer; **Extend** changes it. The modal
offers your configured default, `+3 days`, `+7 days`, and — for admins, when **Allow admins
to remove a timer** is on — *Never expires*, which clears the timer outright.

Three semantics worth knowing:

- **An extend is relative to the resource's current expiry, not to now.** Twelve hours left
  plus twenty-four is thirty-six, not twenty-four. Extending something with no timer starts
  from now.
- **A request over the ceiling is clamped, not rejected.** You get the longest permitted
  expiry and a notice saying so, rather than a 400. The 60-minute floor applies to admins
  too; only the ceiling is admin-bypassable.
- **Extending resets the warning latch**, so the new deadline warns again.

Anyone who can see a resource may extend it. That is deliberate: extending only ever
*delays* a deletion, so there is no expiry permission scope, and adding one would silently
strip the ability from every user who has explicit permissions. Authorization is visibility
— an id you can't see returns 404 rather than 403, so the API doesn't confirm that something
exists. Running a sweep on demand is admin-only, and `?force=true` there bypasses only the
"a sweep is already running" check, never observe-only, arming or the per-pass cap.

---

## Warnings

A resource entering the warn window raises `resource.expiring`; a resource whose teardown
has been enqueued raises `resource.reaped`. Both go out over
[notifications.md](notifications.md), which is off by default and dry-run by default even
once on — so **on a fresh install the timer warns nobody**. Turn notifications on if you
want to hear about this anywhere other than the dashboard.

Two details specific to expiry:

- **Warning does not wait for either arming clock, or for Allow deletion.** An operator who
  hasn't armed deletion yet still wants to know a timer is running down.
- **`resource.reaped` fires when the teardown is *enqueued*,** not when it finishes. The
  destroy job carries the rest, and the message links to it.

Separately, the dashboard's own **Needs attention** banner lists expiring resources. It is
derived in the browser on each poll, dismissible per browser, and deliberately *not* latched
— it comes back on reload, because it is a glance, not a notification.

---

## Failure modes

**I enabled it and nothing has a timer.** Expected. Default lifetime is `0` until you change
it, and even then only *new* deployments are stamped — everything already running stays NULL
forever unless you set a timer from Inventory. This is the same property that makes enabling
the feature on a large estate a no-op.

**A resource is past its expiry and still there.** Walk the four gates in order. In practice
it is almost always Observe only still on, or one of the two arming hours not yet elapsed.
The sweep's Job output names the gate that stopped it.

**I turned Observe only off and it still won't delete.** The enforcement arming clock, which
starts at that moment — not when you enabled the feature. If you toggled Observe only back
on at any point since, the clock restarted then.

**Only some of the overdue resources went.** Max per sweep, default 10. Targets are ordered
oldest-overdue-first, so the rest go on subsequent passes.

**A registered database or cluster never expires.** By design — the dashboard didn't
provision it. Same for Proxmox/Nutanix VMs, desktop seats and gateways; see
[what can carry a timer](#what-can-carry-a-timer).

**One VM was skipped every pass while others were deleted.** Its deploy job is missing the
metadata the destroy job needs — instance id, resource group, zone, OCID. The sweep rebuilds
that from the deploy job's own record and **refuses rather than guess**, which is the
intended behaviour: a re-derived instance id is how you delete the wrong machine. Destroy it
by hand.

**Everything in one workgroup is exempt except the databases.** Only VMs carry a workgroup.
Pin the databases and clusters individually instead.

**A resource was deleted and nobody was told.** See *A resource was deleted and never warned*
in [notifications.md](notifications.md) — the short version is that the warn-once latch is
stamped only after the outbox accepts the row, but a warning that was queued and then failed
to deliver has still burned it.

**A timer I extended came back shorter than I asked for.** The maximum total lifetime,
counted from when the resource was created rather than from now.

**No sweeps in Job History.** Successful ones are hidden — tick **Show routine sweeps**. If
they are missing with the box ticked, either the feature is off, or they aged past *Keep
sweep history*. The Settings panel's last-sweep summary is read from config, not from `/jobs`,
so it survives retention and is the better place to confirm the sweep is running at all.

**Two sweeps at the same timestamp.** Fixed — see *One row per tick*. Pairs seconds apart in
history that predate the fix are harmless: while **Observe only** is on, a duplicate pass just
reported the same thing twice.

---

## Where things live

| | |
|---|---|
| `services/expiry_policy.py` | Pure: what timer a new resource gets, whether one may be reaped, what an extend resolves to. The floors live here as constants. Stdlib + `config_service` only. |
| `services/expiry_reaper.py` | The sweep, the two arming clocks, the write behind Extend, and `prune_sweep_history` — which may only ever delete `expiry_sweep` rows. |
| `api/expiry.py` | Set / status / run-a-sweep endpoints. |
| `main.py` | `_expiry_sweeper_loop` — enqueues, never sweeps. |
| `jobs_worker.py` | Claims `expiry_sweep` and runs the pass. |
| `services/job_service.py` | `ROUTINE_JOB_TYPES` + the `include_routine` filter that hides completed sweeps on `/jobs`. |
| `templates/inventory/list.html` | The Expires column and the Extend modal. |
| `templates/settings.html` | The panel. |
| `jobs`, `cloud_databases`, `k8s_clusters` | Each carries `expires_at` + `expiry_warned_at`. |

Deliberately not built: per-resource schedules, calendar-aware expiry, and any form of
"archive instead of delete". A timer either runs out or it doesn't.

One real gap: **the environment-variable names above appear in no `.env.example` and no
compose file.** They work — every setting falls back to its environment variable — but
Settings is the supported path, and the env names are documented here and nowhere else.
