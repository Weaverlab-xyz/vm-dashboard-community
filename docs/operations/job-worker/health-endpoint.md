# The job worker health endpoint

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want to monitor the worker, or a health check reports it unhealthy.

Part of [Job Worker](../job-worker.md).

The worker serves `GET /api/health` on `WORKER_HEALTH_PORT` (8080; `0` disables it). It is
the only thing it serves. Nothing publishes that port — the Container App has no ingress
and compose adds no `ports:` mapping — so it is reachable from the pod or host network,
which is where platform probes come from.

It exists because **a worker with no port cannot be probed at all.** ACA probes need an
`httpGet` or `tcpSocket` target and there is no exec probe, so before this the platform
had no way to check the runner. On 2026-08-28 the app and the worker cold-started together,
deadlocked inside `init_db()`, and sat at 0% CPU for ~50 minutes while ACA reported the
revision `Healthy` with `restartCount: 0` — because with no probe there is nothing to fail.

**It reports unhealthy until the run loop is actually turning**, and that is the part that
matters. `jobs_worker.main()` starts the listener *before* `init_db()`, so during a wedge
there it answers `503` with a reason rather than refusing the connection. A listener that
answered `200` as soon as it bound would have returned `200` straight through that outage:
the process was alive the whole time — what was not true is that it was working.

Two independent signals, catching different faults:

| Response | Meaning | Which probe reads it |
|---|---|---|
| `503`, `phase` = `starting` / `db_ready` | startup never reached the run loop | Startup |
| `503`, `seconds_since_last_pass` over the limit | loop died *after* a clean start | Liveness |
| `200`, `phase` = `running` | claiming work normally | both |

The loop announces a pass each time round; 120s without one is treated as stalled. That is
two `RECONCILE_INTERVAL`s, sized so a slow sweep on a large `jobs` table cannot manufacture
a restart — a false restart during a 30-minute terraform apply costs more than noticing a
genuine stall a minute late. Note this is *supervisor* liveness: the loop never awaits a
job, so a long-running job does not make it look stalled, and a thread-pool exhaustion that
leaves jobs at 0% is a different fault (see [Threads](../job-worker.md#threads)) that this will not catch.

```bash
curl -s localhost:8080/api/health
```

To probe it on Container Apps — and the same caveat as any probe change applies, it mints a
new revision, so not during an incident:

| Probe | Type | Period | Failures |
|---|---|---|---|
| Startup | `httpGet /api/health:8080` | 30s | 10 |
| Liveness | `httpGet /api/health:8080` | 30s | 3 |

**Do not add that probe until the deployed image actually contains the listener.** The image
must be at least the release carrying `web_dashboard/worker_health.py`; point a probe at
8080 on an older image and nothing answers, the startup probe fails its whole budget, and
ACA restart-loops a worker that was fine. Check with the `curl` above first. Readiness is
pointless here — there is no ingress to be removed from — so Startup and Liveness are the
whole set. See [cloud-hosting.md](../../platform/cloud-hosting.md#probes) for the app's own probes and for
why the Caddy sidecar needs a TCP probe instead.
