# Change Windows

> **Audience:** operator · **Profile:** `both` · **Read this when:** a job must not start the moment somebody presses the button — it has to wait for an approved maintenance period, or for a second person to sign it off.

Every job in this dashboard starts as soon as the worker has capacity. For a lab that is
the right default. For a host somebody depends on it is not: a playbook that patches,
reboots or reconfigures production may only run inside an agreed window, and "remember to
click Run at 02:00 on Saturday" is not an operating model.

A change window is a named, recurring period during which changes may **start**. Book a job
into one and it waits. If the window closes before it started, it is marked **missed** and
never runs — that is the difference between a change window and a delay timer, and it is
the whole point.

---

## The rule, in one paragraph

A window governs when work may **begin**, not how long it may take.

* A job booked into a window sits `pending` until the window opens, then queues normally.
* If the window **closes before the job started**, it is marked `cancelled` with a reason
  and never runs. It does not run late on Monday morning.
* If the job **already started** and is still running when the window closes, it is left
  alone to finish, and the overrun is recorded.

That last one is deliberate and is not a compromise. Interrupting a Terraform apply
halfway leaves orphaned cloud resources; interrupting a playbook leaves a host in a state
nobody can describe. Both are worse than a change running twenty minutes long. If you need
work to stop at the boundary, make the window long enough for the work, or split the work.

---

## Scheduling one job

**Where you can book one**

| Surface | How |
|---|---|
| Config Management runs | **When to run** on the run form (single and bulk) |
| Cloud VM deploys — AWS, Azure, GCP, OCI | **When to run** in the deploy dialog |
| Image promotion | the promote request |
| Packer image builds | the build request |
| Image export / capture / AMI copy | the request |
| Bulk power — AWS, Azure, GCP, OCI | **Schedule** on the selection toolbar |

Any job type can be *held* for a window — that lives in the job queue itself, not in a
form — so a surface without a picker yet can still be booked through the API by passing
`run_at` / `run_timezone` / `change_window_id`.

On each of those forms, **When to run** offers three modes:

| Mode | What it does |
|---|---|
| **Now** | Today's behaviour, unchanged. Nothing about this feature applies. |
| **At a time** | Runs at a specific date and time, in a timezone you pick. Gets an implicit grace period (default 60 minutes) after which it counts as missed. |
| **Change window** | Runs in the next occurrence of a named window. The window supplies both the start and the deadline. |

Times are entered in **your** timezone — the picker defaults to the one your browser
reports — and stored as UTC. Everything the dashboard displays afterwards is labelled UTC,
deliberately: a change window read an hour wrong is the exact failure this feature exists
to prevent, so the unit is never left for you to assume.

A booked job appears on **Jobs** with a second badge next to its status. It is still
`pending`, because that is what it is; the badge says which kind of waiting it is.

> The **engine** is not specific to any page. Scheduling lives in the job queue itself —
> two columns on the job row and one clause in the query that hands work out — so every
> job type in the dashboard can be held for a window. What is per-page is only the
> **picker**; a page gets it by rendering one shared partial and spreading one helper,
> and nothing in the queue changes when it does.

### Scheduling power operations

The **Schedule** tick on the cloud selection toolbar books the whole selection for a
time. It has its own time field, separate from the deploy dialog's — a mode left set by
a dialog you cancelled must not silently book the next power operation.

Two things to know:

* **Only the cloud pages offer it.** An on-premises power operation against a *direct*
  connection runs in the dashboard process rather than through the job queue, so a
  booking there would create a scheduled-looking job and power the machine off
  immediately. The toolbar does not offer the control on those pages, and the server
  refuses the combination outright if one is ever wired by mistake.
* **For a recurring power schedule, use Repeat instead** — open the resulting job and
  repeat it in a window. And for plain business hours, the
  [suspend schedule](cloud-vms.md) is the better tool: it understands per-VM
  eligibility, which a generic power booking does not.

### Bulk deploys book as a unit

A deploy of several VMs creates one parent job and one child per VM. The **parent**
carries the booking; the children are driven by it and never claimed on their own. So a
batch is never split across a window boundary — either the whole batch runs in the
window or none of it does.

---

## Defining a window

**Settings → Change Windows → Add window.**

| Field | Notes |
|---|---|
| **Name** | How people pick it on a run form. Must be unique. |
| **Starts** | 24-hour `HH:MM`, local to the timezone below. |
| **Length** | Minutes. A window may cross local midnight. |
| **Timezone** | An IANA name such as `America/New_York`. Blank means UTC — **not** the server's local timezone, which is an accident of the container image. |
| **Days** | Which days the window recurs on. |

Daylight saving is handled by walking local days, so a 02:00 window stays at 02:00 local
across a clock change rather than drifting by an hour twice a year.

### Editing and deleting

Editing a window **does not move changes already booked into it.** Each job carries its own
resolved start and end, copied when it was booked, so an edit affects only future bookings.
That is the only behaviour that can be explained to somebody looking at a job list.

For the same reason, deleting a window leaves those jobs running to their original times —
they just lose the name in the UI. Deleting is refused while changes are still booked in;
**disable** the window instead to retire it without rewriting history.

---

## When a change is missed

A sweep runs every few minutes and marks any job whose window closed before it started:

```
Change window closed at 2026-10-04 06:00 UTC before this job could start
(37 min ago). It was NOT run outside its window; reschedule it.
```

The job ends up `cancelled`, not `failed`, and that distinction is load-bearing. Nothing
went wrong — the job never ran — so `failed` would put a perfectly healthy change into the
failed-jobs panel and into the retry dead-letter tail, which means "used every retry and
failed anyway". Neither is true.

You will be told: `job.window_missed` is a notification event, on by default. Nothing else
reports this, because nothing failed — without the notification, a change you believe is
scheduled has simply not happened and you find out from the thing it was supposed to fix.

**To recover:** open the job and press **Reschedule**. The run is terminal; the intent is
not, so the row is revived rather than making you rebuild the job.

### Why a change gets missed

Two reasons, and the message says which:

* **Nobody approved it.** It was never eligible. Approve before the window closes.
* **It never got a turn.** The worker was saturated, the app was down, or an earlier
  change overran. Either widen the window or raise the worker's concurrency — see
  [Job Worker](job-worker.md).

---

## Requiring a window for a whole workgroup

Everything above is a **choice**: the operator picks Now, and Now is what happens. A
workgroup-level window is a **constraint** — it cannot be bypassed by clicking Run now.

**RBAC → Workgroups → Edit → Change window**, then tick **Require it**.

With that set, any *gated* change against a resource in that workgroup is refused outside
the window, with the reason and the next occurrence:

```
403  prod may only be changed during Prod Weekend
     (Sat 02:00–06:00 (UTC), 4h). The next window opens 2026-09-26 02:00 UTC.
```

and the form offers to book it instead. **Accepting that offer is admitted** — booking
into the named window, or picking any time that falls inside it, satisfies the
requirement. A booking *outside* it does not; a booking is not a bypass.

### Which actions it covers

The same list as the guardrails: **Settings → Action Guardrails → Gated actions**. One
list, not two — if an action is worth gating on policy, it is the kind of action a
change window is about. Two consequences worth knowing:

* **Nothing is constrained until that list has entries.** A workgroup can require a
  window and still allow everything, if no action is gated.
* **Power operations are not on it by default**, so a window does not block start/stop
  unless an administrator adds them. That is usually right — powering a VM off is
  reversible, and the [suspend schedule](cloud-vms.md) is the better tool for it.

You do **not** need to enable the policy engine. `admission_control_enabled` switches
*OPA* on; a maintenance window is enforced in the dashboard and needs no Rego.

### How it fails

* **Required, with a window that was deleted** → every gated change is refused, saying
  so. An administrator picks another. Failing closed here is deliberate: somebody asked
  for these changes to be gated, and a broken gate is not an open one.
* **Required, with no window picked** → refused at the point of saving the workgroup,
  not at deploy time. Storing that state would block every change with nothing to book
  into.
* **The dashboard cannot tell whether a window is required** (a schema problem) → the
  change is **admitted**, and a warning is logged. The opposite direction on purpose:
  the overwhelmingly likely cause is an install that never used the feature, and 403ing
  every deploy on that estate would be far worse than not enforcing a constraint nobody
  configured.

---

## Requiring approval

**Settings → Change Windows → Require approval for scheduled changes.**

With it on, a change booked into a window waits for somebody holding the
`change_windows:use` permission before it becomes eligible to run. The approver sees it on
the job page and presses **Approve**.

* **You cannot approve your own change.** A gate the requester can clear is not a gate.
  There is a setting to allow it, off by default, for a single-operator install where the
  alternative is not being able to use windows at all.
* **Rescheduling clears the approval.** A change approved for 02:00 Saturday is not thereby
  approved for 14:00 Tuesday — the approver signed off on a time.
* **The requirement is stored on each job when it is booked**, not read from policy at run
  time. Turning the setting on cannot freeze changes already queued; turning it off cannot
  release ones already waiting on an approver.
* **Immediate runs are unaffected.** This gates *booked* changes. Requiring approval for
  every action in the dashboard would be a different and much larger feature.

The permission is deliberately separate from `config_mgmt:write`: maintaining the
maintenance calendar (`change_windows:write`) and signing off a production change
(`change_windows:use`) are different authorities from being allowed to run a playbook. See
[Permissions](permissions.md).

---

## Repeating a change

**Schedules** lists jobs that run on every occurrence of a window.

A schedule is created **from a job**, not from a form of its own: open a job, choose
**Repeat in a change window**, pick the window. It reuses that job's saved parameters, so
what repeats is the run you already tested — there is no second place to describe the work
and therefore no way for the two to drift apart.

Things worth knowing:

* **It does not fire for the window it was created in.** You just ran the job; that is
  where the schedule came from. It starts at the next occurrence.
* **Exactly one job per occurrence**, however many times the sweep runs during it.
* **Not every job type can repeat**, and what a repeat stores is filtered.

  A schedule replays a job's saved parameters weeks later, so it may only carry keys
  that are *references* the runner reads — never a secret, never a one-shot handle, and
  never a result. The allowlist is per **key**, not per job type, because a job's stored
  metadata is the *post-run* one: every runner merges its output back into it. So a
  filter is what keeps an Ansible run's output, an export's registered image id, or an
  EPM-L sync's pre-signed package URLs out of the schedules table.

  Repeatable today: Config Management runs, power operations, image exports, image
  promotions, and the EPM for Linux package sync.

  Not repeatable, and each for a specific reason: **cloud VM deploys** (their saved
  state carries live teardown handles, so a replay would tear down the *first* VM's PRA
  jump), **destroys** (the target is gone after the first run), **Packer builds** (a
  provisioner environment variable may hold a literal value rather than a reference),
  and **cluster/database provisioning** (the job points at one specific row created
  alongside it). The Repeat control is hidden with the reason for these.
* **A schedule runs as the person who created it.** If that account is deleted or
  deactivated, the schedule disables itself and says so rather than running as nobody.
* **Three consecutive failures disable it**, with the reason recorded. A recurring change
  that fails every week forever trains people to ignore the alert.
* **Editing a schedule cannot change what it does** — only its name, window, approval
  requirement and on/off state. To change the work, run a new job and repeat that one.

---

## What it costs when you are not using it

Nothing measurable. A job with no schedule carries `NULL` in every one of these columns and
takes no scheduling code path at all; the sweep writes no job row on an install that has
never booked a change or defined a schedule. Turning the feature on cannot hold back a
single job that is already queued — every row that predates it reads as "run now" by
construction, not by a guard somebody has to remember.

---

## Troubleshooting

**A job shows "scheduled" but never ran.**
Check the window's next occurrence in Settings. A window whose days are all deselected, or
whose timezone was mistyped, is reported as misconfigured there.

**A change was missed even though the worker looked idle.**
The window has to be open when a worker polls, which it does every couple of seconds. If
the window is shorter than the time the queue takes to drain ahead of it, widen the window
or see [Job Worker](job-worker.md) for concurrency.

**Approval does not appear.**
The Approve button shows only for a job that requires approval and has not had it. If you
raised the change yourself, you will be refused — that is the gate working.

**A recurring schedule stopped.**
Open **Schedules**. A self-disabled schedule shows the reason: repeated failures, a deleted
window, or an owner who no longer has an account.

**Everything runs an hour early or late twice a year.**
Should not happen — windows are resolved by walking local days. If you see it, check
whether the window's timezone is blank (which means UTC, not local) rather than set to the
zone you meant.

---

## See also

* [Config Management](config-management.md) — what a scheduled run actually does, and how
  its credentials are handled.
* [Job Worker](job-worker.md) — why a job might not get a turn inside its window.
* [Permissions](permissions.md) — the `change_windows` scope and its two levels.
* [Notifications](notifications.md) — routing `job.window_missed` somewhere you will see it.
* [Auto-delete Timer](auto-delete-timer.md) — the other time-based feature, and the one
  that destroys things.
