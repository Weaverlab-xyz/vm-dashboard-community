# Keeping a POV true, and reaping it

> **Audience:** operator · **Profile:** `pov` · **Read this when:** a POV's view has drifted from the platform, or you want POVs to clean themselves up.

Part of [A POV Instance](README.md). Reconciling against the platform, and the auto-delete timer.

## Keeping the view true

A POV's `runstate` is a **remembered** value, and for most of this feature's life it was
only ever written when this dashboard changed it. Two things change it that this dashboard
does not: the platform's own `suspend_on_idle` timer, and anybody with a Skytap login.

So a reconcile sweep runs every ten minutes — one collection read per configured platform —
and writes back what the platform says: runstate, the rate-limit flag, and the idle timer's
current value. **Re-check platform** on the POV page runs the same pass on demand.

Three properties are worth knowing, because each is a way this could do more harm than the
staleness it fixes:

**The page prefers a live read when it has one.** Opening `/pov` already reads every
environment on the platform for the read-only table, so the managed table shows that and
labels it `live`. The sweep is what keeps the *rows* honest for everything that reads them
when no page is open. Each row says which it is showing — `live`, `confirmed 8m ago`, or
`not confirmed` — because a remembered value presented as a current one is the whole bug.

**Both power buttons show while a reading is stale.** Gating Start on a remembered runstate
is what hid it from every POV the platform had suspended: the cost feature working exactly
as designed removed the control needed to undo it.

**A missing environment is flagged, never reaped.** Absence from the listing is not proof of
deletion — the listing is project-scoped, so an environment outside the configured project
is invisible and perfectly alive — so the sweep confirms with a direct read and only a 404
sets the flag. Even then it only flags: the row holds the only record of which PRA, Password
Safe and Entitle tenants that POV was wired into, and the manifest for reaping them. Use
**Destroy** to close one out; it is idempotent on a 404. The flag clears by itself if the
environment becomes visible again.

---


## The auto-delete timer

A POV is the resource the auto-delete feature was most obviously missing. It bills for
every VM inside it, it sits on `suspend_on_idle` for months after the evaluation ends, and
until this release nothing in the codebase ever noticed it was finished.

This is **not** a second sweeper. A POV is a new *kind* in the one that already exists, so
every gate — the arming delay, report-only, the enforcement gate, the per-pass cap, the
exempt workgroups — is the same code that guards a cloud VM. The only thing POV-specific is
the default lifetime and the shape of the warnings.

### Turning it on

Two settings, both defaulting to off, and both needed:

| | |
|---|---|
| `resource_expiry_enabled` | the master switch, shared with every other kind |
| `pov_expiry_default_hours` | `0` (the default) means new POVs are not stamped at all |

`pov_expiry_default_hours` is deliberately separate from `resource_expiry_default_hours`.
The two describe different things: a cloud VM's default is a working-day sort of number,
and a POV is an evaluation that runs for weeks with a customer inside it. Sharing one
number would force a wrong default on one of them, and reaping a customer's lab is much the
worse half of that trade.

Note `resource_expiry_max_total_hours` (30 days, counted from creation) still caps the
total lifetime. If your evaluations run longer than a month, raise it — otherwise an
Extend will be clamped.

### Nothing that already exists is ever selected

`expires_at` NULL means **never**, and every POV that predates this release is NULL. So
turning the feature on acts on nothing; only POVs created afterwards, with a default
configured, carry a timer. This is the same property the rest of the feature has and it is
worth restating, because the failure it prevents is the loud one.

Past that, the same four gates stand between overdue and destroyed: the feature enabled, a
stamped expiry, the feature armed for an hour with a sweep report on disk, and enforcement
armed with report-only off.

### The warning ladder

Everything else in the feature warns **once**, `resource_expiry_warn_hours` before expiry.
For a VM that is right: one message, one owner, one decision.

A POV is not that. It has been running for weeks, whoever created it has moved on to the
next evaluation, and a single mail the day before a customer's lab disappears is a mail
that gets read afterwards. So a POV warns on a ladder:

**7 days → 3 days → 24 hours → 4 hours → 1 hour**

Each rung fires at most once. That is what `warned_stage_minutes` is for: `expiry_warned_at`
alone is a boolean latch, so the first rung would burn it and the other four would never
fire. The column records the *tightest rung already sent*.

Two consequences worth knowing:

- **A missed rung does not fire late.** The sweep runs every 30 minutes and a deadline can
  cross two rungs between passes. You get the tightest one crossed — "expires in about 4h",
  not a stale "expires in about 3 days" followed by a burst of four more.
- **Extending re-opens the whole ladder.** Both latches are cleared, so the new deadline
  gets its own full set of warnings rather than being permanently silenced by a rung
  crossed against a deadline that no longer exists.

### Extending

The **Expires** column on the POV page has an `edit` control: a number of hours to add, or
`never` to clear the timer outright. `never` needs `resource_expiry_allow_never`, exactly
as it does on `/inventory`, and a clamp against `max_total_hours` is reported rather than
applied silently.

It routes through the same `expiry_reaper.set_expiry` the inventory page uses, so a POV can
never end up with looser rules than a cloud VM.

### What reaping actually does

It enqueues the identical `pov_env_destroy` job the Destroy button creates. The reaper
never learns the teardown order — the share link, then the Entitle integrations, then the
Password Safe managed systems, then the PRA jump items, then the Gateway, then the broker
agent, then the environment. That ordering lives in one place and this feature cannot get
it wrong.

A POV is only ever reaped from `active` or `failed` — the same pair
`pov_env_service._ACTIONABLE` allows a human to destroy. `failed` is in that set
deliberately: a POV that died halfway through provisioning is the one that most needs
reaping, because whatever it did create is still billing. Anything mid-provision or
mid-destroy is refused, and an unrecognised status is refused too, so a state added later
can only ever make the reaper do less.

Clearing `expires_at` in the same commit is what makes it at-most-once.

---


## Failure modes

**No POV ever gets an auto-delete timer.** Both switches are needed:
`resource_expiry_enabled` AND `pov_expiry_default_hours` (0, the default, means don't
stamp). Environments created before both were set stay NULL forever — that is deliberate,
and the fix is the Expires column's `edit`, not a backfill.

**A POV expired but was not destroyed.** Read the sweep's job log on /jobs; it reports the
full target list whichever gate is shut. Most often the feature or enforcement has not been
armed for its hour yet, or `resource_expiry_dry_run` is still on.

**An Extend was shorter than I asked for.** `resource_expiry_max_total_hours` caps the
total lifetime from creation, and it defaults to 30 days. The response says `clamped`.
