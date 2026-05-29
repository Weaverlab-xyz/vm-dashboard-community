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

### Step 2 — Enable and configure in the dashboard

**Option A — Setup wizard (first run)**

Toggle **Entitle** on in wizard Step 5 and fill in the fields.

**Option B — Settings → Integrations (after first run)**

Navigate to **Settings → Integrations → Entitle**, toggle it on, and fill in:

| Field | Example |
|---|---|
| API URL | `https://api.entitle.io/v1` |
| API Token | (bearer token from Step 1) |
| Webhook Secret | (signing secret from Step 1) |
| Default TTL (minutes) | `15` |
| Approval gate enabled | toggle on |

`Default TTL` controls how long the dashboard waits for an approval before
auto-expiring the request. Set higher for async reviewer workflows.

`Approval gate enabled` is the master kill-switch — toggle it off to temporarily
disable all gates without removing the Entitle configuration.

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

## Machine identity — JIT cloud credentials via Entitle

The approval gate covers **who can ask** for a privileged action. There's
a separate, complementary track that covers **what credentials get used**
when the dashboard executes that action against AWS / Azure / GCP — the
Cloud-Identity JIT design.

Today the dashboard's three cloud identities (AWS IAM user, Azure Service
Principal, GCP Service Account) carry broad standing privilege all the
time. The machine-identity track replaces that with **per-request,
short-TTL elevations** issued through the same Entitle tenant you've
already set up for human approvals — so an `EC2 deploy` triggers a fresh
IAM role grant scoped to that one action, valid for ~15 minutes, audited
end-to-end in Entitle, and auto-revoked afterwards. No long-lived keys
in the dashboard.

### How it relates to the approval gate

| Axis | Approval gate (this doc, above) | Machine identity (Cloud-Identity JIT) |
|---|---|---|
| Concern | **Who** is allowed to trigger the action | **What credentials** the action runs with |
| Subject | The dashboard *user* | The dashboard *process* |
| Surface | A modal in the UI; webhook callback | A cloud SDK call wrapped in `async with elevate(...)` |
| Default state | Off (opt-in per environment) | Off — gated on `cloud_identity_gate_enabled` |
| Approval flow | Reviewer in Entitle approves the request | Entitle auto-approves machine requests against a dedicated bundle |
| Failure mode | Action blocked; user sees "denied" | Action raises `CloudIdentityError`; no silent fall-back to baseline creds |

The two layers are orthogonal — you can run the approval gate on its own,
the machine-identity track on its own, or both together (recommended for
production). They share **the same Entitle tenant, API token, and webhook
secret** — no additional credentials to manage.

### Setup additions (when enabling the machine-identity track)

| Field | Where | Notes |
|---|---|---|
| `cloud_identity_gate_enabled` | Settings → Integrations → Entitle → Machine identity | Master kill-switch. Off by default. |
| `cloud_identity_<cloud>_enabled` | Same panel (3 checkboxes) | Per-cloud opt-in — promote AWS → Azure → GCP one at a time |
| Operation matrix | Same panel (JSON textarea) | Maps dashboard operations (`aws:ec2:deploy`) → Entitle resource IDs + per-cloud IAM roles |
| Entitle bundle | Entitle console | A dedicated **machine-identity** bundle with auto-approve enabled; do NOT route to a human reviewer |

The Terraform module under
[`terraform/entitle_user_jit/`](../../terraform/entitle_user_jit) covers
the user-side workflows. A companion machine-identity module is shipped
under `terraform/entitle_machine_identity/` (Phase 2 of the
Cloud-Identity JIT execution plan).

### Status

| Cloud | Implementation | E2E verification |
|---|---|---|
| AWS  | EC2 deploy / bulk-deploy / terminate wrapped in `elevate(...)` | Pending real Entitle tenant + auto-approve bundle |
| Azure | Service Principal swap path implemented | Same |
| GCP  | Service Account impersonation path implemented | Same |
| Sweeper | AWS reconciliation, Azure trust-self-expiry, GCP agent-driven revoke | Phase 4a/b/c shipped |

### Further reading

- [`docs/design/cloud-identity-jit.md`](../design/cloud-identity-jit.md) — full design, threat model, per-cloud trade-offs.
- `docs/runbooks/cloud-identity-jit-phase-0-smoke-test.md` — scaffolding smoke test (no real Entitle calls).
- `docs/runbooks/cloud-identity-jit-phase-1-entitle-submit.md` — first end-to-end Entitle submit-and-poll loop; **requires a configured Entitle tenant**.
- `docs/runbooks/cloud-identity-jit-phase-4a-aws-sweeper.md` (and `4b`, `4c`) — orphan-row sweeper per cloud.

---

## User JIT — Entra-group-backed dashboard permissions via Entitle

The approval gate governs **who can ask** for a privileged action.
The machine-identity track above governs **what cloud credentials**
that action runs with. The Entitle User-JIT track is the third leg:
it governs **what permissions the user has at all** — granted
just-in-time through Entitle instead of statically assigned by an
admin.

Today the dashboard's user permissions (e.g. `aws:write`,
`images:delete`, `dashboard-admin`) are static — once granted, they
persist until an admin revokes them. For enterprise audit, that
leaves a long tail of standing privilege ("Alice last used
`aws:write` six months ago and still has it"). The Entitle
User-JIT track lets users **request** those permissions through
the same Entitle workflow they already use for cloud-side access,
and the dashboard recognises the time-bound grant on the next
login or token refresh.

### How it relates to the other two tracks

| Axis | Approval gate | Machine identity | **User JIT** |
|---|---|---|---|
| Concern | Who can trigger an action | What creds the action runs with | **What permissions the user has at all** |
| Subject | User performing an action | Dashboard process | **User session** |
| Mechanism | Per-action Entitle approval | Per-call `elevate(...)` | **Entra group membership read at login** |
| Grant duration | Single action | Single SDK call (~15 min) | **Time-bound; mirrors Entra group TTL** |
| Revocation | Action denied | Cloud-side IAM revoked | **Group falls off → next login lacks scope** |
| Default state | Off (`APPROVAL_GATE_ENABLED`) | Off (`cloud_identity_gate_enabled`) | **Off (`entitle_user_jit_enabled`)** |

The three layers are independent and can be enabled in any combination.
They share the same Entitle tenant + token + webhook — but the
User-JIT track additionally requires **Entra ID** for group resolution
(no Entra → fall back to static permissions only).

### Setup additions (when enabling the User-JIT track)

| Field | Where | Notes |
|---|---|---|
| `entitle_user_jit_enabled` | Settings → Integrations → Entitle → User JIT | Master toggle. Off by default. |
| Entra tenant ID + admin SP | Same panel | Used by the bootstrap script for one-shot group provisioning. |
| OAuth group mapping | Same panel + `/api/admin/oauth-group-mappings` | Maps `dashboard-aws-write` → scope `aws:write`. Bootstrap populates a default map for 24 scope×level + N workgroup groups. |
| Resource ID map | Same panel (JSON) | Maps each scope to the corresponding Entitle resource ID, so `require_permission` can attach a one-click request-access URL to its 403 response. |
| Entitle bundle | Entitle console | One **VM Dashboard** virtual application; per-resource policy (auto-approve vs human-reviewed) is per the operator's preference. |
| Entra groups | Bootstrap script | `dashboard-admin` + `dashboard-baseline` + 24 `dashboard-<scope>-<level>` + N `dashboard-workgroup-<name>`. Idempotent. |

The Terraform module under
[`terraform/entitle_user_jit/`](../../terraform/entitle_user_jit) covers
the Entitle side of provisioning (one application + workflows + resources +
policies in a single `terraform apply`). The Entra bootstrap is a
separate Python script — `python -m web_dashboard.scripts.bootstrap_entra_groups` —
because Graph operations don't fit the Terraform workflow as cleanly.

### Status

| Phase | Implementation | E2E verification |
|---|---|---|
| Resolver | OAuth callback computes UNION across all matched groups every login; baseline preserved via `effective_permissions = union(baseline, session)` | Shipped scaffolding |
| Bootstrap (Entra) | Idempotent Graph + DB provisioner for all 28 groups | Shipped scaffolding |
| Bootstrap (Entitle) | Terraform module + `bootstrap_entitle_app.py` wrapper | Pending real Entitle tenant |
| UI affordances | Settings panel + 403 deep links (toast → one-click request access in Entitle) | Shipped scaffolding |
| E2E request flow | "Request → group → login → scope" round-trip | Not started — requires real Entitle + Entra |
| Multi-tenancy hook | Per-tenant group resolution | Not started — depends on MT Phases 4 + 5 |

### Further reading

- [`docs/design/entitle-user-jit.md`](../design/entitle-user-jit.md) — full design, including the operation matrix and OAuth resolution flow.
- `docs/runbooks/entitle-user-jit-phase-0-resolver.md` — resolver UNION behaviour smoke test.
- `docs/runbooks/entitle-user-jit-phase-1-bootstrap-entra.md` — Entra group provisioner; **requires a configured Entra tenant**.
- `docs/runbooks/entitle-user-jit-phase-2-bootstrap-entitle.md` — Entitle virtual-application provisioner; **requires a configured Entitle tenant**.

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

**Approval modal does not appear** — check that both **Entitle enabled** and
**Approval gate enabled** are toggled on in **Settings → Integrations → Entitle**,
then restart the stack.

**"Webhook signature invalid"** — the webhook secret in **Settings → Integrations
→ Entitle** must match the signing secret shown in the Entitle console exactly.
Copy it fresh and save.

**Approval times out immediately** — verify `ENTITLE_DEFAULT_TTL_MINUTES` is
set to a value large enough for your reviewer response time. Also confirm the
Entitle webhook URL is publicly reachable. If you are running locally without
a public IP, use a tunnel (`ngrok http 8001`) as a temporary workaround, or
consider the SaaS hosted tier which provides a stable public endpoint.

**"Entitle API error"** — verify the API token in **Settings → Integrations →
Entitle** is valid and not expired. Test it from inside the container:
`docker compose exec app curl -H "Authorization: Bearer <token>" https://api.entitle.io/v1/me`.
