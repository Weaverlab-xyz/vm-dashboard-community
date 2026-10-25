# Retrying a job that failed transiently

> **Audience:** operator · **Profile:** `both` · **Read this when:** a job failed on something that should have cleared by itself, and you want to know when the worker retries it.

Part of [Job Worker](../job-worker.md).

Off by default (`job_retry_enabled`). Turned on, a job that failed for a reason another
attempt could get past goes back on the queue instead of stopping there — an API throttle,
a capacity shortfall, a 5xx, a token that expired mid-run.

**Two allowlists, and the first is the one that matters.**

**Which job types.** Only types whose runner is safe to re-enter, listed explicitly in
`retry_policy.RETRYABLE_TYPES` — today the five `*_power` and five `*_destroy` types.
Powering an already-powered VM is a no-op on all four clouds, and a half-finished destroy
is the case that *most* needs retrying because what it leaves behind goes on billing.

**A deploy is never retried.** `aws_vm_service` calls `set_failed` without cleaning up an
instance it may already have launched (Azure's deploy path does clean up; AWS's does not).
A second run would launch a second instance while the first is still running and billing,
and would report that the retry had helped. A type joins this list when somebody has read
its runner and shown it is re-entrant — not because its name looks safe.

**Which failures.** Recognised transient signals only, never "retry unless it looks
permanent". A denylist fails open on the next unfamiliar error message, which is how a
permanent misconfiguration gets retried three times and reaches you twenty minutes late.
The failure mode this errs toward is *no retry where one would have helped*, which is you
doing by hand what you already do by hand today.

Backoff is 30s, 2m, 10m, 30m — the same schedule the notification drain already uses.
`job_retry_max_attempts` (default 3, counting the first run) is clamped to that schedule.

**A retry inside a change window can push past the window's end**, and when it does the
job is marked missed rather than retried outside it. That is the correct outcome and not a
special case: the window is the boundary, a retry is just another attempt to start, and an
attempt that cannot start inside the window must not start at all. If a change is being
missed this way, the window is too short for the work plus its backoff. See
[Change Windows](../scheduling/change-windows.md), and [Scheduling](../scheduling.md) for which surfaces
can book a job at all.

## The dead-letter tail

A job that uses every attempt is `failed` with `attempts > 0`. **There is no fourth
status** — 108 places in the tree compare against `"failed"`, and the first one missed
would be a job that silently stops appearing on some page. So the tail is a query:

```
GET /api/jobs?dead_lettered=true
```

and exhaustion raises `job.dead_lettered` (severity `critical`), which is distinct from
`job.failed` — that one fires the first time anything fails, this only when the failure has
outlasted every retry.

## What it does to the claim query

`_claim_one` gains one condition: a job whose `retry_after` has not yet passed is not
claimable. `retry_after` is NULL for every row that has never failed, which is nearly all
of them, so the queue behaves exactly as before for everything else. That query is polled
every two seconds by every worker replica, which is the main reason this ships off.
