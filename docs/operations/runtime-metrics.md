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
  is covered in [Job Worker](job-worker.md).
- **It keeps answering when the app is struggling.** The loop and thread numbers need no
  database and no thread. The one database read runs on its own thread with a 3 second
  deadline, so if the database or its pool is the problem, `jobs` reports an `error` and
  everything else is still returned. `previous read still in flight` means the database has
  not answered the last request either.
- `event_loop` says `no samples yet` for the first second after start.

Nothing here is written anywhere or sent anywhere; it is computed when you ask.
