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

On the Config Management run form, **When to run** offers three modes:

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

> The **engine** is not specific to Config Management. Scheduling lives in the job queue
> itself — one column on the job row and one clause in the query that hands work out — so
> every job type in the dashboard can be held for a window. What is specific to Config
> Management today is the **picker on the run form**; other pages get it as they are wired
> up, and nothing needs to change in the queue when they are.

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
* **Not every job type can repeat.** Only those whose saved parameters are *references*
  rather than values — Config Management runs qualify, because their credentials are
  resolved at run time and never stored on the job. The Repeat control is hidden with a
  reason for the rest.
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
