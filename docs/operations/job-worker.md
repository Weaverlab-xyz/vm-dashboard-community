# Job Worker

> **Audience:** operator · **Profile:** `both` · **Read this when:** a long job is sitting queued, or you are sizing the worker for more of them.

Part of [Operations](../operations.md).

Long jobs — cluster and database provisions, Packer builds, image exports and promotes, VM
deploys — run in a separate `worker` process, not in the web app. This page is about **how
many of them run at once**, why the limits are split by kind, and why the database
connection pool usually decides the answer.

Tune it in **Settings → Job Worker**. Changes apply within about five seconds, with no
restart and no redeploy.

---

## Why there are limits at all, and why they are tiered

The worker used to run exactly **one** job at a time. Concurrency came from running more
worker containers (`WORKER_REPLICAS`), one job each.

That has a failure mode in both directions. A single image export polls a cloud API for up
to two hours doing essentially nothing — and held the only slot the whole time, so a
two-second auto-delete sweep queued behind it. Reversed: a forty-minute Packer build
blocked every cheap poll.

It also assumes replicas are a cheap knob. On a PaaS host they are not always available
per-worker, and they are never free: **each replica needs its own database connection
pool**, while N slots inside one process share one. On a small managed Postgres the
connection budget, not CPU, is what caps total concurrency.

So the concurrency moved inside the process — and it is tiered, because the jobs are not
alike:

| Tier | Default | What it is | What it costs |
|---|---|---|---|
| **light** | 3 | Image exports, promotes, copies, gateways, auto-delete sweeps, EPM-L sync | Starts something in the cloud and waits. Nearly free — raise this first |
| **medium** | 1 | VM deploys and destroys, PRA tunnels, Kubernetes add-ons, virtual desktops | Cloud SDK plus a short `terraform` or `kubectl`/`helm` |
| **heavy** | 2 | Cluster and database provisions, Packer builds, local Ansible | Streams a local subprocess and writes a `job_logs` row **per output line** |
| **total** | 3 | The aggregate ceiling | What actually binds |

The three tier caps are allowed to sum higher than `total`: they cap the **mix**, `total`
caps the **total**. So `heavy=2, medium=1, light=3, total=3` means "up to two provisions at
once, or one provision plus two polls, but never more than three jobs".

The tier of each job type lives in `web_dashboard/jobs_worker.py` next to `HANDLED_TYPES`,
because the partition is only reviewable beside the dispatch table.
`tests/test_worker_tiers.py` fails the build if a job type is in no tier or in two — an
untiered type would raise `KeyError` in the supervisor *after* claiming a job, taking the
loop down for every job, not just that one.

A few types are additionally limited to **one at a time regardless of tier**: the
Rancher/Portainer node jobs and the auto-delete sweep, because they rewrite
deployment-global config; and `epml_sync`, because it holds a whole agent package in memory
and multiplying that by the light cap is how you find a container's memory limit.

---

## The database connection pool is the real limit

A running job does not hold one connection. It holds the dispatcher's session for its whole
duration, several services open their own, and every heartbeat, cancel check and streamed
output line opens a transient one. The worker therefore **reduces** your configured total to
what its pool can serve, and reports that in Settings → Job Worker:

```
heavy 2   mixed 1   poll-only 3   total 3
reduced: total 12→3
why: DB pool of 10 serves 3 concurrent job(s); raise DB_POOL_SIZE / DB_MAX_OVERFLOW to use 12
```

Roughly, usable concurrency is `(DB_POOL_SIZE + DB_MAX_OVERFLOW - 4) / 2`.

Clamping rather than refusing to start is deliberate: a pool-exhausted worker fails jobs
with opaque `QueuePool limit ... timeout` errors, which is worse than running fewer at once
and saying so. The readout exists because a silently clamped cap is otherwise only
diagnosable from a container log.

**The pool cannot be set from the Settings page.** The engine is built at import time,
before the app can read anything out of the database — so `DB_POOL_SIZE` and
`DB_MAX_OVERFLOW` are environment variables, and the caps are clamped to them rather than
the other way round.

### Budgeting connections

Per **process**: `DB_POOL_SIZE + DB_MAX_OVERFLOW`. The app runs `gunicorn -w 2` (two pools)
and each worker one, so a single deployment holds `3 × (size + overflow)` — 30 at the
defaults. Multiply by your replica count and keep the result under
`max_connections - 20`, the 20 covering the server's own management and reserved sessions.

```sql
SHOW max_connections;
```

Azure Postgres Flexible Server **Burstable B1ms allows only 50**, which is why the shipped
defaults are a conservative `5 + 5`. B2s and every General Purpose tier allow 429 or more.

Burstable has a second ceiling worth knowing: it runs on CPU credits, so sustained load
throttles once they are spent, and a small volume provisions few IOPS. Two concurrent
streamed Terraform applies each write a `job_logs` row per output line, each with a WAL
flush. If Live Output starts lagging under `heavy=2`, look at the database tier before the
worker.

---

## Running more than one worker

Replicas still multiply the caps — two workers at `total=3` run up to six jobs — and no job
ever runs twice however many workers there are, because claiming a job is a single atomic
`UPDATE ... WHERE status='pending'` whose row count is the lock. There is no
`SKIP LOCKED`, deliberately, so the same mechanism works on SQLite for development.

Prefer raising the caps to adding replicas: another replica is another connection pool and
another copy of a large import footprint, for the same concurrency.

---

## The configuration surface

The worker is configured in three places, and which one a setting lives in is not arbitrary —
it follows from when the value has to be known.

| Setting | Where | Applies |
|---|---|---|
| `worker_heavy_concurrency` | **Settings → Job Worker** (or `WORKER_HEAVY_CONCURRENCY`) | next pass, ~5s |
| `worker_medium_concurrency` | same | next pass, ~5s |
| `worker_light_concurrency` | same | next pass, ~5s |
| `worker_max_concurrency` | same | next pass, ~5s |
| `worker_drain_timeout_s` | same | next shutdown |
| `worker_executor_threads` | same | **worker restart** |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | **environment only** | worker restart |
| `DB_POOL_TIMEOUT_S` / `DB_POOL_RECYCLE_S` | environment only | worker restart |
| CPU / memory / replicas | **the platform** (Container App or Compose) | new revision |
| `terminationGracePeriodSeconds` | the platform | new revision |

Three rules explain that table:

- **The caps are in the database** because they are the ones you actually want to tune, and
  tuning should not need a redeploy. `worker_policy` re-reads them every supervisor pass, and
  `config_service`'s 5-second cache is what carries a write in the app process across to the
  worker process — even in a different container.
- **The pool is in the environment** because `create_engine` runs at import, before the app
  can read anything out of the database. A pool sized from a database setting would have to
  connect in order to learn how many connections it may open. So the caps are clamped to the
  pool instead, and the clamp is reported in the UI.
- **`worker_executor_threads` needs a restart** because the default executor is installed once
  on the event loop at start. It is the one knob in the panel that is not live, and it is
  labelled as such.

Nothing about the worker is secret, so none of these need to be Container App secrets — but
`DATABASE_URL` and `JWT_SECRET_KEY` do, and the worker needs both.

---

## Pages

The larger parts have a page each:

| Page | Read it when |
|---|---|
| [Retrying a job that failed transiently](job-worker/retries.md) | a job failed on something that should have cleared by itself, and you want to know when the worker retries it |
| [Deploying the job worker](job-worker/deploying.md) | you are running the worker as its own process or container, or more than one of them |
| [The job worker health endpoint](job-worker/health-endpoint.md) | you want to monitor the worker, or a health check reports it unhealthy |

---

## Verifying it works

Open **Settings → Job Worker** and read the status card. It is written by the worker itself, so
it answers three questions at once — whether a worker is alive, what it resolved its limits to,
and whether anything was reduced:

```
heavy 2   mixed 1   poll-only 3   total 3
0 job(s) in flight when reported · 25 threads · pool of 10
reported by dash-worker--ci3ubcs-5f9c8d4b7-xk2mq at 2026-08-03T18:22:04Z
```

The worker also logs one line on start and one per claim:

```
job runner: caps heavy=2 medium=1 light=3 total=3 (threads=25)
job runner: claimed aws_export_image job 7f3a… (tier=light, in flight=2)
```

```bash
az containerapp logs show -n dash-worker -g RG-CWeaver --tail 50
```

---

## Shutdown

A revision change or scale-in sends `SIGTERM`. The worker stops claiming, gives what is
in flight `WORKER_DRAIN_TIMEOUT_S` to finish, and then abandons the rest.

Abandoned jobs are **not requeued** — the cloud side effect is already under way and
re-running would start it twice. Instead their heartbeat is backdated past the staleness
cutoff, so the next worker's startup reconcile fails those rows within seconds of coming
up, rather than leaving them looking alive for ten minutes.

Before this, a redeploy killed in-flight jobs outright and they sat `running` until the
ten-minute reconciler noticed. With several jobs in flight that multiplies, which is the one
way concurrency makes a redeploy worse — hence the drain.

---

## Threads

Every blocking cloud SDK call runs in a thread pool. The worker sizes that pool explicitly
from the caps, because the standard library's default is derived from `os.cpu_count()` —
which is not container-aware, and reports the host's CPU count rather than the CPU the
container was given.

It has to be explicit for a second reason: several cloud pollers are synchronous sleep loops
that occupy a thread for their entire wait, not per call. Exhaust the pool and the next
call queues behind a two-hour export — the job sits at 0% while its heartbeat, which runs on
the event loop, keeps reporting it healthy. A hang that reports healthy is the worst failure
mode available, so the pool is sized generously; threads parked in `sleep` cost little.

Leave `WORKER_EXECUTOR_THREADS` at `0` (derive from the caps) unless you see jobs stuck at
0%. Unlike the caps, it is applied at worker start, so changing it needs a restart.

---

## Troubleshooting

**Jobs sit `pending` and nothing happens.** The worker is not running. On Container Apps the
usual cause is `minReplicas: 0` (see above) — check `az containerapp show -n dash-worker -g
RG-CWeaver --query properties.template.scale`. The status card saying "No worker has reported
yet" points the same way.

**"No worker has reported yet" but jobs do run.** The worker is on an image that predates the
status readout. Redeploy it.

**A cap you raised did nothing.** Read the `reduced:` and `why:` lines on the status card. Almost
always the connection pool; raise `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` on the worker and restart it.

**Jobs stuck at 0% with a healthy-looking heartbeat.** The thread pool is exhausted — a cloud
call is queued behind a long synchronous poller. The heartbeat runs on the event loop, so it
keeps reporting the job alive. Set `worker_executor_threads` explicitly and restart.

**The container restarts and takes every running job with it.** Out of memory. Lower `heavy`
or raise the memory limit; see the sizing note above.

**Live Output lags during a provision.** Look at the database before the worker. Every streamed
output line is an INSERT plus a WAL flush, and a Burstable tier throttles once its CPU credits
are spent.
