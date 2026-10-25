# The POV suspend schedule

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you want a POV's cloud resources off outside working hours.

Part of [Running POVs on a public cloud](../public-cloud.md).

Skytap suspends an environment on its own idle timer. No public cloud has one, so on a
cloud the dashboard supplies the timer — a **schedule**, set per POV from the *Sleeps*
column, or carried on a blueprint so every POV of a kind starts with it.

| Field | Meaning |
|---|---|
| Suspend at | Local time, 24-hour `HH:MM`. Blank removes the schedule |
| Resume at | Blank means it stays down until somebody starts it |
| Timezone | An IANA name. Blank means UTC — not the server's zone, which is an accident of its base image |
| Days | Seven characters of `0`/`1`, Monday first. `1111100` is Mon–Fri |

A schedule rather than an inactivity timer, because "idle" on a cloud has no honest
definition from outside the guest. A PRA session says nothing about a customer clicking
around a console; an agent heartbeat never stops. Every candidate signal has a blind spot
that either leaves a POV running all month or suspends one mid-demo. Business hours are
something you can state, predict, and explain on an invoice.

**The rule is "has a boundary been crossed since the last check", not "should this be
asleep right now".** That distinction is the whole design, and it shows up in three places
you would otherwise file as bugs:

- **A manual start outside hours survives.** Start a POV by hand at 20:00 for a call and it
  stays up until tomorrow's suspend time. A state check would put it back to sleep on the
  next sweep, four minutes later, and every sweep after that.
- **The first pass after you set a schedule does nothing.** An unevaluated row has crossed
  every boundary there has ever been, so the first pass records the time and acts on
  nothing — the same rule the auto-delete timer's arming clock follows.
- **An outage settles on the later boundary.** Down from 18:00 to 22:00 with a 19:00
  suspend and a 21:00 resume, the POV ends up running. It does not replay both. A latch
  older than 25 hours is re-armed rather than replayed at all.

The sweep rides the reconcile pass, which already runs every ten minutes and already knows
each POV's real runstate. It only ever **enqueues a `pov_env_power` job** — the same one
the Suspend and Start buttons create — so a scheduled action has a `/jobs` row, a Live
Output, a cancel, and a place in the failed-jobs panel. It is attributed to
`pov-schedule`, not to whoever set the schedule, so the row says why the POV went to sleep
at 19:00.

Setting a schedule is refused on a platform that has its own idle timer. The two answer
the same question, and a POV carrying both is one where neither is in charge.

For a single early start before a call, outside the schedule's hours or on a platform that
refuses one, book a [warm-up](../lifecycle.md#warming-a-pov-up-for-a-demo) instead.
