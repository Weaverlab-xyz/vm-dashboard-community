# Runtime Metrics

> **Audience:** operator · **Profile:** `both` · **Read this when:** the dashboard is slow or hung and you want to know whether the event loop, a thread pool, the database pool or the job queue is the thing that is full.

Part of [Operations](../operations.md).

`GET /api/metrics` returns the four numbers that decide most "the dashboard is stuck"
incidents, as JSON, without exec-ing into a container. It is for **administrators only**
(`Authorization: Bearer <token>`, as for any API call; see [service
accounts](../admin/service-accounts.md)), because queue depth and pool sizes describe your
install. It is not part of the browser UI.

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8001/api/metrics
```

### Prometheus format

Add `?format=prometheus` for Prometheus exposition text instead of JSON (same admin-only
token, same numbers; JSON stays the default, and any other value is a `400`).

```bash
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8001/api/metrics?format=prometheus"
```

Every metric is a gauge named `dashboard_*` and carries a `pid` label, because the values
are the answering process's own (see below): a scrape that lands on a different process
reports a different `pid`, so a scrape job that wants both processes must scrape
repeatedly and keep `pid` as a label rather than dropping it. Lag is in seconds, as
`dashboard_event_loop_lag_last_seconds`, `dashboard_event_loop_lag_max_60s_seconds` and
`dashboard_event_loop_seconds_since_last_beat`. Pools are
`dashboard_thread_pool_{busy,size,queued}{pool="anyio_sync_routes|loop_default_executor|cloud_<provider>"}`
and `dashboard_db_pool_{size,checked_out,checked_in,overflow,max_overflow}`. Jobs are
`dashboard_jobs{status}` and `dashboard_jobs_oldest_age_seconds{status}`.

Anything that is unavailable is left out rather than reported as zero: `db_pool` on SQLite,
or `event_loop` in the first second. The exception is `dashboard_jobs_up`, which is `0`
when the database read failed or timed out, so an alert can tell "the queue is empty" from
"the queue could not be read". Prometheus needs the bearer token, so use `authorization`
in the scrape config:

```yaml
scrape_configs:
  - job_name: dashboard
    metrics_path: /api/metrics
    params: {format: [prometheus]}
    authorization: {credentials_file: /etc/prometheus/dashboard.token}
    static_configs: [{targets: ["dashboard.example.com"]}]
```

## What it reports

| Section | Fields | A problem looks like |
|---|---|---|
| `event_loop` | `last_ms`, `max_60s_ms`, `seconds_since_last_beat` | `max_60s_ms` in the hundreds or more: something ran synchronous work on the event loop (typically an `async` route doing database or SDK calls). A `seconds_since_last_beat` well above 1 means the loop is blocked **right now** |
| `threads.anyio_sync_routes` | `busy`, `size` | `busy` equal to `size`: every thread that runs a plain (`def`) route is taken, so the next such request waits |
| `threads.loop_default_executor` | `threads`, `size`, `queued` | `queued` above 0 for a sustained time: `to_thread` work is waiting for a thread |
| `threads.cloud_providers` | per cloud: `busy`, `size` | `busy` equal to `size`: that provider's calls are being refused as busy |
| `db_pool` | `size`, `checked_out`, `overflow`, `max_overflow` | `checked_out` at `size + max_overflow`: the next request waits up to `timeout_s` for a connection. On SQLite this is `{"pooled": false}` |
| `jobs` | per status (`queued`, `pending`, `running`): `count`, `oldest_age_s` | a `pending` job with a large `oldest_age_s` while the worker is up: the queue is not draining. See [Job Worker](job-worker.md) |

## Reading it correctly

- **Per process.** The web app runs two worker processes, and a request is answered by one of
  them. Everything except `jobs` is that process's own, and the reply carries its `pid`. Call
  it a few times to see both. `jobs` comes from the database and is the same in both.
- **The job worker is a separate process** and is not in this reply. Its own health endpoint
  is covered in [Job Worker](job-worker.md); it reports its own `event_loop` and `job_executor`
  numbers there, [as extra fields](job-worker/health-endpoint.md).
- **It keeps answering when the app is struggling.** The loop and thread numbers need no
  database and no thread. The one database read runs on its own thread with a 3 second
  deadline, so if the database or its pool is the problem, `jobs` reports an `error` and
  everything else is still returned. `previous read still in flight` means the database has
  not answered the last request either.
- `event_loop` says `no samples yet` for the first second after start.

Nothing here is written anywhere or sent anywhere; it is computed when you ask.

## Deep health check

`GET /api/health` is the container probe and stays exactly `{"status": "ok", "version": ...}`;
it never touches the database, so a probe cannot fail (and restart the app) just because the
database is briefly slow. For a monitor that wants to know the database answers too, add
`?deep=1`:

```bash
curl -s "http://localhost:8001/api/health?deep=1"
# {"status":"ok","version":"...","db":"ok"}
```

It runs `SELECT 1` on its own thread with a 2 second deadline, so a slow or exhausted
database cannot hold up the event loop. If the check fails or times out, the reply is
`503` with `"status": "degraded"` and `"db": "error"`. Like the plain call it needs no
login. Point external monitors at `?deep=1` and leave the container probe on the plain URL.
