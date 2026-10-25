# Idle suspend on AWS

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you want an AWS POV suspended when nobody has used it for a while.

Part of [Running POVs on a public cloud](../public-cloud.md).

On AWS the dashboard also runs an **idle timer**, the nearest thing to Skytap's
`suspend_on_idle`. Pick the window on the create form (*Suspend when idle*, one hour by
default) or change it later from the *Sleeps* column. Allowed values are 30 minutes to
24 hours, or off.

**Idle means every VM was quiet for the whole window.** On each ten-minute reconcile pass
the dashboard reads the free five-minute EC2 metrics from CloudWatch for every running POV
that has a window set. It stops the POV only when, for every VM, each period in the window
shows:

| Metric | Must stay under |
|---|---|
| `CPUUtilization`, average | 5% |
| `NetworkIn` + `NetworkOut` | 2 MB per five minutes |

Missing data never counts as idle. A VM that has not been reporting since the window
began was not running for all of it, so a POV you start by hand gets a full window before
the timer can stop it again. Metrics can arrive up to fifteen minutes late, so the newest
periods may be missing. Any period that *has* arrived still counts, though, so activity
keeps the POV up as soon as CloudWatch has it.

The stop is the same `pov_env_power` job the **Suspend** button creates, attributed to
`pov-idle`. A stop means the compute bill ends and the disks are kept; see the cost note
above.

**The blind spot.** A customer reading a static screen over PRA for the whole window
makes almost no traffic and looks idle. Skytap's timer has the same gap. If that is a risk
for a particular demo, turn idle suspend off for that POV and rely on the schedule.

The timer and the schedule can both be set on the same POV. The dashboard runs both, so
they don't conflict: the schedule can resume a POV at 08:00, and the idle timer stops it
again at 09:00 if nobody used it.

The dashboard's AWS credential needs `cloudwatch:GetMetricData`, the
`DashboardCloudWatchMetrics` statement in `setup-aws.sh`. Without it the idle check
fails, nothing is stopped, and the reconcile job log says why.

## The customer can wake it

A schedule without a customer-reachable resume is a broken demo: the POV sleeps at 19:00
and the prospect trying it at nine the next morning finds a dead environment and an SE to
email. So the accessor page carries a **Start it** button.

It is **start only**. There is no suspend and no destroy on that page, and the endpoint
takes no environment id — it resolves the POV from the accessor's own session, the same
way every other write on that page does, so "could they stop somebody else's POV?" is
unanswerable rather than merely guarded. The job it enqueues is the one the SE's Start
button creates, attributed to `pov-accessor` so the `/jobs` row says a prospect pressed it.

The button renders from state the API serves rather than probing, so it only appears when
pressing it will work. When it does not, the page says why in a sentence the prospect can
act on:

| State | What they see |
|---|---|
| Suspended, under its cap | **Start it** |
| Already starting | "Give it a few minutes" — a second job would race the first |
| Still provisioning, or being destroyed | The lifecycle reason |
| **Over its spend cap** | "Ask your contact to raise it" |

That last one matters. The spend cap **latches** once it has acted, so the sweep will not
re-suspend — which means a prospect who woke a capped POV would leave it running past its
cap indefinitely, and the account owner's one cost control would be the one anybody could
undo.

**Suspending a cloud POV is not free.** Stopping an instance halts its compute charge and
nothing else — the root volume, the public address and the network keep billing for the
whole evaluation. A schedule cuts the largest line on the bill, not the bill.
