# Notifications

**Outbound only.** The dashboard POSTs to endpoints you configure. It never opens an
inbound webhook, and nothing here listens for anything. (A per-tenant *inbound* endpoint
is reserved for the hosted edition — see [saas-roadmap.md](saas-roadmap.md) — and is a
different feature entirely.)

The problem it solves: everything the dashboard knows is on a page you have to open.
That was tolerable until the [auto-delete timer](auto-delete-timer.md) started **destroying
resources on a schedule**, whose only warning was a banner in a browser nobody had open.
Three more signals had the same shape — a cloud budget, a stale secret, a drifted host —
each with an evaluator and no way to tell anybody.

Off by default, and **dry-run by default even once on**.

---

## Architecture

```
 EMIT — INSERT only. No network call, ever, at any of these sites.
   job_service.set_failed / reconcile_stale_jobs   ──┐
   expiry_reaper  (warn, reaped)                    ──┼──► notification_deliveries
   notify_scanner (cost, secrets, drift)            ──┘        one row per endpoint
                                                                       │
 DELIVER — worker process, alongside the job runner                    ▼
   notification_service.drain_loop()  ── claim → POST → sent | retry | failed
```

Two properties carry the design:

**A notification can never fail the thing it reports on.** Emit sites only write a row.
The HTTP call happens later, in a different process, in a different transaction. A dead
webhook cannot slow down, block, or fail a deploy — and this is enforced statically by
`tests/test_notify_wiring.py`, which fails the build if `job_service` so much as imports
an HTTP client.

**`UNIQUE(dedupe_key)` is the coordination.** The app runs `gunicorn -w 2` and the worker
at `replicas: 3` — five processes that all reach the same tick. An in-process dedupe set
would be worthless across them; a database constraint is not. That is also why no
advisory lock is taken.

Delivery is **at-least-once**. A worker killed after the POST but before the `sent` write
re-sends when the row is reclaimed 10 minutes later. A duplicate alert beats silence.

---

## Events

| Event | Fires when | Severity |
|---|---|---|
| `resource.expiring` | A VM/database/cluster enters the auto-delete warning window. **Once per resource**, latched on `expiry_warned_at`; extending the expiry clears the latch so the new deadline re-warns. | warning |
| `resource.reaped` | The auto-delete sweep started a teardown. Fires on *enqueue* of the destroy job — the job itself carries the rest. | critical |
| `job.failed` | Any job reaches `failed`, including "the worker died mid-provision" (which writes `failed` inline and used to notify nobody). | warning |
| `cost.budget_exceeded` | Month-to-date spend is over `cost_monthly_budget` or a per-cloud budget. Once per day per budget. | warning |
| `secret.stale` | Stored secrets have gone past `secret_max_age_days`. Once per day. | warning |
| `config.drift` | Ansible targets are changed or unverified. Once per day, or again if the counts change. | warning |

Selection is the **Events to send** CSV. `notify_min_severity` is a second filter on top.

Not wired on purpose: `job.completed` and `job.cancelled`. `set_completed` fires for
every `expiry_sweep` every 30 minutes, and a 30-VM bulk deploy would be 30 messages.

---

## Endpoints

Each endpoint is a URL plus a **format**. Transport is always HTTP POST; the format picks
the body, because Slack and Teams reject anything that isn't their own shape.

Add them under **Settings → Notifications → Endpoints**. URLs and signing secrets are
Fernet-encrypted at rest with the same key as every other stored secret, and are never
returned by the API — the UI shows a scheme+host hint.

### `slack`

An **Incoming Webhook** URL from the Slack app that owns the channel. Body is
`{"text": …, "blocks": […]}`.

> Slack reports `invalid_payload` / `no_service` as **HTTP 200 with an error body**. The
> dashboard treats any 200 whose body isn't `ok` as a failure, so a misconfigured webhook
> shows up in the delivery log instead of silently doing nothing.

### `teams`

A **Power Automate Workflows** webhook. Create a flow from the *"Post to a channel when a
webhook request is received"* template and paste its URL (it will be on
`logic.azure.com`).

Three things that will otherwise cost you an afternoon:

- **The old connector webhooks are gone.** Office 365 Connectors inside Teams were
  permanently switched off in May 2026, not deprecated. A URL on `outlook.office.com`
  will not work and cannot be made to work — recreate it as a Workflow.
- **Workflows answers `202 Accepted`, not `200`.** The dashboard accepts any 2xx. If you
  are writing your own receiver, don't accept only 200.
- **The card needs the envelope.** The body is
  `{"type":"message","attachments":[{"contentType":"application/vnd.microsoft.card.adaptive","content":{…}}]}`.
  Posting a bare Adaptive Card is the single most common reason a Workflows webhook
  "succeeds" and renders an empty post. If your Flow reads the card explicitly, the path
  is `triggerBody()?['attachments']?[0]?['content']`.

Messages post as the **Flow bot**; the name and icon can't be customised. That's a
Microsoft limitation, not a setting we've omitted.

### `custom` — and how to send email

A stable, versioned JSON envelope. **There is no SMTP client in this codebase**, and adding
one would mean a blocking I/O path, TLS modes and its own CA handling for one more
delivery target. Instead: point a `custom` endpoint at a Power Automate Flow, a Zapier
hook, or anything else that can send mail — and get a mailbox, a channel, and a ticket
queue out of one endpoint.

```json
{
  "version": 1,
  "event": "resource.expiring",
  "severity": "warning",
  "subject": "[WARNING] web-01 auto-deletes in about 23h",
  "body": "…",
  "resource": {"id": "job:3f21…", "kind": "vm", "name": "web-01",
               "cloud": "aws", "region": "us-east-1", "workgroup": "lab"},
  "fields": {"Expires": "2026-07-30T14:00:00", "State": "active"},
  "url": "https://dashboard.example.com/inventory",
  "occurred_at": "2026-07-29T15:04:05Z"
}
```

`version` is the contract. Receivers can rely on these keys.

Headers:

| Header | |
|---|---|
| `X-Dashboard-Event` | the event type |
| `X-Dashboard-Delivery` | the delivery row id, for your own dedupe |
| `X-Dashboard-Timestamp` | unix seconds |
| `X-Dashboard-Signature` | `sha256=<hex>`, present only when a secret is set |

The signature is `HMAC-SHA256(secret, "<timestamp>." + raw_body)` over the **exact bytes
posted** (compact separators, sorted keys). Verify against the raw request body, not a
re-serialisation of the parsed JSON. The timestamp gives you replay protection.

The signing secret may be an `aws_sm://` / `azure_kv://` / `gcp_sm://` / `bt_safe://`
reference; it is resolved at send time.

---

## Rolling it out

1. **Add an endpoint** and press **Test**. This ignores dry-run and sends immediately,
   returning the verbatim error — which is the whole point of the button.
2. **Enable the feature**, leave **Dry run** on. Set the **Dashboard URL**.
3. Watch **Recent deliveries** for a day. Everything appears with status `dry-run`,
   rendered exactly as it would be sent.
4. Turn **Dry run** off.

### Settings

| Setting | Default | |
|---|---|---|
| Dry run | on | Record, don't send. |
| Dashboard URL | *blank* | **Set this.** The worker has no request context, so blank means every message ships with no link — which looks like a bug and is this setting. |
| Events to send | all six | CSV. An endpoint can narrow this, never widen it. |
| Minimum severity | `warning` | |
| Send every | 30s | Drain cadence. |
| Condition scan every | 3600s | Budget / secret / drift. Reads the **cached** cost summary and skips when cold — those API calls are billable. |
| Attempts | 4 | Then terminal. Backoff 30s → 2m → 10m → 30m, honouring `Retry-After` on a 429. Retry state is a column, so it survives a worker restart. |
| Max per pass | 50 | |
| Queue ceiling | 500 | Past this, new messages are recorded `suppressed` rather than queued, with one audit entry per pass. This is the brake that keeps a first enable against a large estate from becoming an incident. |
| Keep delivery history | 30 days | 0 = forever. Failed rows are never pruned — they're the evidence. |

---

## Failure modes

**Everything fails with `CERTIFICATE_VERIFY_FAILED`, but terraform and boto3 work.**
You're behind a TLS-inspecting proxy. `httpx` verifies against `certifi` and ignores
`SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` — which this image sets and
`docker-compose.corp-ca.yml` mounts over. The dashboard resolves the bundle explicitly
from those variables, so the fix is to make sure your CA actually reaches the container:
use the corp-CA overlay, or `--corp-ca` at onboard time. There is deliberately no
"disable TLS verification" switch.

**A Teams post arrives blank.** The Flow isn't reading the Adaptive Card from the
attachment. See the envelope note above.

**Everything fails right after a `JWT_SECRET_KEY` rotation.** Endpoint URLs and secrets
are encrypted with a key derived from it. After a rotation they can't be decrypted, and
the delivery log will say so. Re-enter the URL and secret on each endpoint. (Every stored
secret in the dashboard has this property — it isn't specific to notifications.)

**A resource was deleted and never warned.** Check
`/api/notifications/deliveries?status=failed`. `expiry_warned_at` is stamped only *after*
the outbox accepts the row, so a warning that was never queued — feature off, no
endpoints, storm-suppressed — is re-offered next sweep. But a warning that was queued and
then failed to deliver **has** burned the latch for that resource.

**Duplicate messages.** Expected, occasionally: delivery is at-least-once. If it's
constant, look for a worker being killed mid-send (rows reclaimed from `sending`).

**Nothing at all is queued.** In order: is the feature on; is there an enabled endpoint;
is the event type in the CSV; is its severity at or above the floor; is the queue over its
ceiling.

---

## Where things live

| | |
|---|---|
| `services/notify_policy.py` | Pure: the event shape, gating, dedupe keys, rendering. Stdlib + `config_service` only. |
| `services/notify_transports.py` | The three payload builders and the POST. |
| `services/notification_service.py` | Outbox, drain loop, retry, endpoint storage. |
| `services/notify_scanner.py` | The periodic cost / secret / drift scan. |
| `api/notifications.py` | Endpoint CRUD + the delivery log. Admin-only. |
| `notification_endpoints`, `notification_deliveries` | The two tables. |

Deliberately not built: per-user subscriptions, digests, on-call routing, and
owner-address lookup. Routing is one function (`recipients_for` semantics live in
`emit`), so those are additive when someone actually needs them.
