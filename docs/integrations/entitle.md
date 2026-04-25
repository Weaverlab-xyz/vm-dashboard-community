# Entitle Integration

> **Community edition limitation — read this first**
>
> The Entitle integration requires Entitle's servers to deliver a webhook
> callback to your running dashboard. This means the dashboard must be
> reachable at a **public HTTPS URL** — a laptop behind NAT or a container on
> a home network will not work without extra tunnelling (e.g. ngrok).
>
> This is a structural constraint of the self-hosted model, not an Entitle
> limitation. If you are evaluating the approval-gate workflow but cannot
> expose a public endpoint, the **SaaS hosted tier** (coming soon) is the
> right fit — we run the dashboard on a stable public URL with TLS, so webhook
> delivery works out of the box without any networking changes on your side.
>
> The integration is fully implemented here and will work correctly for any
> community deployment that already has a public-facing URL (e.g. a cloud VM,
> a VPS, or a corporate server with an inbound HTTPS rule).

## What is it?

The Entitle integration adds an **approval gate** in front of privileged
dashboard actions. When enabled, operations like deploying a new cloud VM or
starting a BeyondTrust PRA session require explicit approval from an authorised
reviewer in [Entitle](https://www.entitle.io/) before they proceed.

The gate is implemented as a FastAPI dependency (`require_approval(...)`) that
the dashboard injects into selected endpoints. When a request hits a gated
endpoint, the dashboard creates an Entitle approval request and waits (up to a
configurable TTL) for the reviewer to approve or deny it. The UI shows a
pending-approval modal; on approval the original action completes automatically.

---

## Use cases

- **Four-eyes control over cloud deployments** — every EC2 or Azure VM
  creation requires a second person to approve, creating an audit trail in
  Entitle.
- **Just-in-time privileged access** — combine with BeyondTrust PRA so that
  a session cannot start until a reviewer grants time-limited access.
- **Compliance workflows** — satisfy change-management requirements without
  a heavyweight ITSM by routing approvals through Entitle's lightweight
  request/review flow.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Entitle tenant | [Entitle](https://www.entitle.io/) account with at least one configured resource and reviewer group |
| API token | Created in the Entitle admin console |
| Webhook secret | HMAC-SHA256 shared secret for inbound webhook callbacks |
| **Public HTTPS URL for the dashboard** | Entitle's servers must be able to POST to `https://your-dashboard/api/approvals/webhook` — a private/NAT'd host will not work without a tunnel. See note above. |

---

## Setup

### Step 1 — Obtain API credentials from Entitle

1. Log in to the **Entitle admin console**.
2. Navigate to **Settings → API → API Tokens**.
3. Create a new token and copy it.
4. Under **Settings → Webhooks**, create a new webhook pointing to
   `https://your-dashboard-url/api/approvals/webhook`.
5. Copy the **signing secret** Entitle generates for the webhook.

### Step 2 — Configure in `.env`

```
ENTITLE_ENABLED=true
ENTITLE_API_URL=https://api.entitle.io/v1
ENTITLE_API_TOKEN=<your-bearer-token>
ENTITLE_WEBHOOK_SECRET=<hmac-sha256-shared-secret>
ENTITLE_DEFAULT_TTL_MINUTES=15
APPROVAL_GATE_ENABLED=true
```

`ENTITLE_DEFAULT_TTL_MINUTES` controls how long the dashboard waits for an
approval before auto-expiring the request and returning an error. Set higher for
async reviewer workflows.

`APPROVAL_GATE_ENABLED` is the master kill-switch. Set it to `false` to
temporarily disable all gates without removing the Entitle configuration.

### Step 3 — Verify webhook delivery

After restarting the stack, trigger a gated action from the UI (e.g. attempt to
deploy an EC2 instance). You should see:

1. An approval modal appear in the browser.
2. An approval request appear in the Entitle console for a reviewer to action.
3. After approval, the deployment job starts automatically.

Check `docker compose logs app | grep entitle` if the modal does not appear.

---

## What it enables in the dashboard

| Feature | Description |
|---|---|
| **Approval modal** | Appears in the UI while the dashboard awaits Entitle approval |
| **Gated deploy endpoints** | EC2, Azure VM, and other privileged endpoints require approval |
| **Webhook callback** | Entitle POSTs the approval result back to the dashboard; the waiting request continues |
| **TTL expiry** | Requests auto-deny after `ENTITLE_DEFAULT_TTL_MINUTES` if not actioned |

---

## Which endpoints are gated?

The `APPROVAL_GATE_ENABLED` flag and `require_approval(...)` dependency control
which endpoints are gated. In the default configuration, gated endpoints include:

- `POST /api/aws/deploy` — EC2 instance creation
- `POST /api/azure/deploy` — Azure VM creation
- `POST /api/azure/bulk-deploy` — Azure bulk VM creation
- `POST /api/gcp/deploy` — GCP Compute Engine instance creation

Endpoints that are **not** gated by default: start/stop VM, list resources,
read-only operations, and job status queries.

---

## Advanced: per-resource approval routing

Entitle supports routing approval requests to different reviewer groups based on
resource type and sensitivity. Configure this in the **Entitle admin console**
under **Resources** and **Policies** — the dashboard passes the action type
(`aws:deploy`, `azure:deploy`, etc.) as the resource identifier, so you can
route cloud deploys to a cloud-team reviewer group while other actions go to a
general reviewer pool.

---

## Troubleshooting

**Approval modal does not appear** — check that `APPROVAL_GATE_ENABLED=true`
and `ENTITLE_ENABLED=true` are both set, then restart the stack.

**"Webhook signature invalid"** — the `ENTITLE_WEBHOOK_SECRET` must match
the signing secret shown in the Entitle console exactly. Copy it fresh and
update `.env`.

**Approval times out immediately** — verify `ENTITLE_DEFAULT_TTL_MINUTES` is
set to a value large enough for your reviewer response time. Also confirm the
Entitle webhook URL is publicly reachable. If you are running locally without
a public IP, use a tunnel (`ngrok http 8000`) as a temporary workaround, or
consider the SaaS hosted tier which provides a stable public endpoint.

**"Entitle API error"** — check that `ENTITLE_API_TOKEN` is valid and not
expired: `docker compose exec app curl -H "Authorization: Bearer $ENTITLE_API_TOKEN" "$ENTITLE_API_URL/me"`.
