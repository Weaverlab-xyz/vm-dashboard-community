# Design — Machine-Identity JIT Cloud Access via Entitle

> **Status:** Design draft. No code changes yet. Author: dashboard team.
> **Scope:** Community + prod/dev. Community is the reference target; prod inherits.
> **Depends on:** The Entitle integration (see [`integrations/entitle.md`](../integrations/entitle.md))
> already shipping for *human* approval gates. This doc extends the same Entitle
> tenant to also broker *machine-identity* privilege.

---

## 1. Problem

Today the dashboard holds **three long-lived cloud identities** with broad
standing privilege:

| Cloud | Identity                | Standing privilege today |
|-------|-------------------------|--------------------------|
| AWS   | IAM user (access key)   | `ec2:*`, `iam:PassRole`, `s3:*` on dashboard buckets, `ssm:*`, `tag:*` |
| Azure | Service Principal (SP)  | `Contributor` on the subscription (sometimes scoped to RGs) |
| GCP   | Service Account (SA)    | `Compute Admin`, `IAM Service Account User`, `Storage Admin` on dashboard project |

That is convenient but violates least-privilege: a stolen access key, leaked SP
secret, or compromised SA JSON gives an attacker the union of every privilege
the dashboard ever uses, indefinitely, without an approval trail.

The human-facing approval gate we shipped in
[`secrets-management.md`](../secrets-management.md) and
[`integrations/entitle.md`](../integrations/entitle.md) covers *human* reads /
updates / deletes. It does **not** cover the case where the dashboard's own
service principal performs a privileged cloud action on behalf of a workflow
(e.g. a deploy job, a workgroup tag rewrite, a Packer image capture).

## 2. Goal

Move the three cloud identities to a **baseline read-only** footing and make
every privileged operation an **on-demand, time-bound elevation** brokered by
Entitle, with the elevation request bound to the dashboard operation that
triggered it.

Concretely:

- The dashboard's three "primary" cloud identities lose all standing write
  privilege. They keep read-only privilege for inventory/list views (so the
  Dashboard tiles and table views still render with no friction).
- Every code path that *mutates* cloud state (`deploy`, `destroy`, `set-tags`,
  `capture-image`, `attach-volume`, `assume-role-for-Packer`, etc.) goes through
  a **credential resolver** that asks Entitle for a time-bound activation of a
  matching cloud role. **No human approver is in the loop** — Entitle policy
  auto-approves the request if it satisfies the policy (principal, action,
  TTL ≤ ceiling) and denies otherwise.
- Activations are short-lived. Policy default is **TTL ≤ 60 minutes** per
  request, no extension, no renewal — a new operation gets a new request and
  a new credential. Anything requesting a longer TTL is **denied, alerted,
  and audited** as anomalous.
- Activations are cached in-process for their natural TTL, so a deploy that
  fires three SDK calls doesn't trigger three Entitle round-trips.

## 3. Non-goals

- **Not** replacing the existing *human* approval gate on secret reads /
  updates / deletes. That stays — they target different threat models.
- **Not** introducing per-user cloud identities. The dashboard remains the
  principal; Entitle is the broker for *its* elevations. Per-user cloud
  identity is much larger scope and is out of band.
- **Not** building a homegrown JIT broker. Entitle is the policy engine; the
  dashboard just consumes its decisions.
- **Not** changing the secrets backend story. Entitle here is the *access*
  control for the *cloud action*; secrets-backend choice is orthogonal.

## 4. Threat model & what this buys

The point is **not** a human-in-the-loop second pair of eyes. The point is:

1. **Zero standing write privilege** — a stolen credential alone unlocks
   nothing destructive.
2. **Hard TTL ceiling per elevation** — even a successful elevation is
   bounded to ≤60min, no extension, no renewal.
3. **Anomaly signal** — any request asking for more than the policy ceiling
   is *denied and alerted*. Legitimate dashboard code never asks for more
   than the ceiling, so a denied request is high-fidelity signal that
   something is wrong (bug, or attacker probing).

| Threat                              | Before                       | After                                                                            |
|-------------------------------------|------------------------------|----------------------------------------------------------------------------------|
| Leaked AWS access key               | Full `ec2:*` until rotated   | Read-only inventory only; writes need a fresh Entitle activation each time       |
| Leaked Azure SP secret              | Subscription Contributor     | Reader only; same                                                                |
| Leaked GCP SA JSON                  | Project-wide Admin           | Project Viewer only; same                                                        |
| Attacker tries to extend TTL        | n/a                          | **Denied + alerted** — policy ceiling is fixed; no API path to raise it          |
| Attacker uses creds off-hours       | Indistinguishable from app   | Every activation is a discrete logged event with timestamp, action, and TTL      |
| Compromised dashboard host          | Same as standing creds       | Bounded — even with full host control, attacker is capped by ceiling per request |
| Insider misuse (rogue admin)        | No bound                     | Same ceiling applies; can't grant themselves longer creds via the dashboard      |
| Bug → runaway deploy loop           | Unbounded blast radius       | Each elevation is its own request; rate-of-requests visible in Entitle audit log |

The dashboard is **already a privileged box** — what changes is that the
*credentials it holds* are no longer the ceiling on what an attacker can do,
and **every escalation past the policy is a high-confidence detection event**.

## 5. Per-cloud activation mechanics

The three cloud providers each have a native JIT primitive Entitle can drive.
The shape of the resolver is the same across all three; only the activation
call differs.

### 5.1 AWS — STS AssumeRole with Entitle as trusted issuer

- **Baseline identity:** an IAM user `dashboard-readonly` with `ReadOnlyAccess`
  managed policy plus `tag:GetResources` for inventory.
- **Privileged roles:** one IAM role per *operation class* (see §6.3 for the
  matrix). E.g.:
  - `dashboard-ec2-deploy` — `ec2:RunInstances`, `ec2:CreateTags`,
    `iam:PassRole` on the dashboard instance profile, scoped to a tag
    condition `aws:RequestTag/ManagedBy=vm-dashboard`.
  - `dashboard-ec2-destroy` — `ec2:TerminateInstances` scoped to
    `aws:ResourceTag/ManagedBy=vm-dashboard`.
  - `dashboard-tag-rewrite` — `ec2:CreateTags`, `ec2:DeleteTags` scoped to
    `aws:ResourceTag/ManagedBy=vm-dashboard`.
  - `dashboard-image-builder` — Packer-required privileges (run instance,
    create AMI, deregister, deletesnapshot) scoped to dashboard-owned tags.
- **Trust policy:** each role's `AssumeRolePrincipal` trusts Entitle's identity
  provider (Entitle vends a SAML/OIDC issuer URL; this is configured once per
  AWS account). The `Condition` ties the assumption to a session tag
  `entitle:approval_id` matching the approval Entitle issued.
- **Activation call:** dashboard receives `{role_arn, external_id, ttl_seconds}`
  from Entitle, calls `sts:AssumeRoleWithWebIdentity` with the Entitle-signed
  JWT, gets temporary credentials. Boto3 client created from those credentials.
- **Notes:**
  - AWS `AssumeRole` max TTL is 12h with `MaxSessionDuration`; we cap at 1h.
  - `iam:PassRole` for the EC2 instance profile is itself a privileged action,
    so it lives in `dashboard-ec2-deploy` and is denied to the baseline.
  - Cross-account is supported — if a customer wants the dashboard to deploy
    into a separate workload account, the role lives there and trusts Entitle.

### 5.2 Azure — PIM-eligible role activations

- **Baseline identity:** SP `dashboard-baseline` with `Reader` on the
  subscription (or RG, if scoped).
- **Privileged roles:** PIM-eligible role assignments on the SP. Examples:
  - `Virtual Machine Contributor` (RG-scoped) — eligible, not active.
  - `Storage Blob Data Contributor` (storage account scope) — eligible.
  - `Network Contributor` — eligible.
- **Activation call:** Entitle holds an Azure AD app with PIM activation
  permission. On approval Entitle calls
  `POST /providers/Microsoft.Authorization/roleAssignmentScheduleRequests`
  with the SP as `principalId`, the role definition as `roleDefinitionId`,
  a `requestType=SelfActivate` (or `AdminAssign` if Entitle runs as a
  delegated admin), and `scheduleInfo.expiration.endDateTime` set to
  `now + ttl`.
- **Resolver:** the dashboard's resolver, on receiving an Entitle approval,
  does **not** itself need to call PIM — Entitle has already activated the
  role on the SP. The resolver just refreshes its `DefaultAzureCredential`
  token (forces a new MSAL token request, which now reflects the new role)
  and hands the resulting `TokenCredential` to the Azure SDK clients.
- **Notes:**
  - Azure caches access tokens for ~1h; we must force-refresh by clearing
    the in-process token cache after activation completes.
  - PIM eligibility must be configured *once* per role per environment;
    Entitle's "resource" for each role points at the PIM-eligible assignment.
  - PIM has a minimum activation duration of 5min; cap at 1h to match AWS.

### 5.3 GCP — time-bound IAM Conditions on the SA

- **Baseline identity:** SA `dashboard-baseline@project.iam.gserviceaccount.com`
  with `roles/viewer` and `roles/storage.objectViewer` on the dashboard project.
- **Privileged roles:** Entitle holds a GCP SA with `roles/iam.securityAdmin`
  (or `roles/resourcemanager.projectIamAdmin` scoped to the dashboard project).
  On approval Entitle calls `projects.setIamPolicy` to **add** an IAM binding
  granting the *dashboard* SA the requested role *with an IAM Condition*:
  ```
  request.time < timestamp("<now + ttl iso8601>")
  ```
  When the condition expires GCP automatically stops honouring the binding.
- **Activation call:** the resolver waits for Entitle to confirm the binding
  was written (Entitle returns the operation id; resolver polls until
  `done=true`, ≤5s typical), then hands the SDK the baseline SA credentials
  — the SDK now sees the new role automatically.
- **Periodic sweep:** because adding-then-leaving-bindings can leak if the
  resolver crashes mid-flight, a hourly sweeper (`gcp_pim_sweep.py`) compares
  active bindings against the dashboard's `EntitleActivation` table and
  removes any that are orphaned or past TTL. This is belt-and-braces — GCP
  honours the condition expiry on its own.
- **Notes:**
  - GCP doesn't have native "PIM" — IAM Conditions with `request.time` is
    the supported equivalent and is widely used by JIT vendors.
  - The Entitle activator SA needs `iam.serviceAccounts.actAs` on the
    dashboard SA to grant roles to it.
  - For cross-project deploys, the Condition-bound binding lives on the
    workload project, not the dashboard project.

## 6. Dashboard-side design

### 6.1 Credential resolver

A new module: `web_dashboard/services/cloud_identity_service.py`.

```python
async def get_credential(
    cloud: Literal["aws", "azure", "gcp"],
    operation: str,                 # e.g. "aws:ec2:deploy"
    *,
    payload_hash: str,              # binds the elevation to the request body
    requester_user_id: str,         # who triggered the dashboard operation
) -> CloudCredential
```

`CloudCredential` is a small wrapper holding:

- The SDK-ready object (`boto3.Session`, `TokenCredential`, GCP SA email + token)
- `expires_at` for cache eviction
- `activation_id` (Entitle's approval id, threaded into audit logs and tags)

The resolver:

1. Looks up the cached activation for `(cloud, operation)` — if present and
   `now < expires_at - safety_margin`, returns it.
2. Otherwise calls `entitle_service.create_machine_request(
       action=operation, payload_hash=payload_hash,
       requester_user_id=requester_user_id, principal=<cloud SP id>)`.
3. Awaits the approval via the existing webhook flow (re-using
   `api/approvals.py`'s `Approval` table; new column `principal_kind`
   in `{"user","machine"}` so policy can fork).
4. On approval, calls the per-cloud activation path (§5).
5. Constructs and caches the `CloudCredential`.

The cache is in-process (per worker), keyed on `(cloud, operation)`. Cross-
worker coordination uses the existing `cache_service` Redis layer (TTL
matches the activation TTL) so two workers don't request two activations.

### 6.2 Where the resolver is called

Every cloud SDK client construction goes through a factory that uses the
resolver. We do **not** sprinkle `get_credential()` calls at every call site.

```
# Today
ec2 = boto3.client("ec2", region_name=region)

# After
ec2 = await aws_clients.client(
    "ec2",
    region=region,
    operation="aws:ec2:deploy",
    payload_hash=hash_request_body(body),
    requester_user_id=current_user.id,
)
```

For read-only paths the factory passes `operation="aws:ec2:read"` which is
configured in the operation matrix (§6.3) to map to the baseline identity —
no Entitle round-trip.

Concretely the files that change (community repo):

| File                                  | Change |
|---------------------------------------|--------|
| `services/aws_service.py`             | All `boto3.client(...)` calls go through `aws_clients.client(...)` factory; factory injects `botocore.credentials` from resolver |
| `services/azure_service.py`           | Replace `DefaultAzureCredential()` direct construction with `azure_clients.credential(operation=...)`; pass to mgmt clients |
| `services/gcp_service.py`             | Replace `google.auth.default()` with `gcp_clients.credentials(operation=...)`; pass to `compute_v1` and `storage` clients |
| `services/cloud_identity_service.py`  | **new** — the resolver |
| `services/entitle_service.py`         | Add `create_machine_request()` alongside the existing `create_request()` (different Entitle "resource" id; same webhook plumbing) |
| `api/approvals.py`                    | Same table, no schema change beyond a `principal_kind` discriminator (`user` / `machine`) |
| `database.py`                         | Add `Approval.principal_kind`, plus a new `EntitleActivation` table (operation, role_arn/role_id, expires_at, revoked_at) |

### 6.3 Operation → role matrix

The mapping lives in `config_service` as a JSON blob editable from
**Settings → Integrations → Entitle (Machine Roles)**. Default seeded values:

| Operation key                  | AWS role / scope                            | Azure role (PIM-eligible)             | GCP role (condition-bound)        |
|--------------------------------|---------------------------------------------|---------------------------------------|-----------------------------------|
| `aws:ec2:read`                 | baseline (`ReadOnlyAccess`)                 | n/a                                   | n/a                               |
| `aws:ec2:deploy`               | `dashboard-ec2-deploy`                      | n/a                                   | n/a                               |
| `aws:ec2:destroy`              | `dashboard-ec2-destroy`                     | n/a                                   | n/a                               |
| `aws:ec2:tag-rewrite`          | `dashboard-tag-rewrite`                     | n/a                                   | n/a                               |
| `aws:image-builder`            | `dashboard-image-builder`                   | n/a                                   | n/a                               |
| `azure:vm:read`                | n/a                                         | baseline (`Reader`)                   | n/a                               |
| `azure:vm:deploy`              | n/a                                         | `Virtual Machine Contributor` (RG)    | n/a                               |
| `azure:vm:destroy`             | n/a                                         | `Virtual Machine Contributor` (RG)    | n/a                               |
| `azure:storage:write`          | n/a                                         | `Storage Blob Data Contributor`       | n/a                               |
| `gcp:compute:read`             | n/a                                         | n/a                                   | baseline (`Viewer`)               |
| `gcp:compute:deploy`           | n/a                                         | n/a                                   | `Compute Instance Admin (v1)`     |
| `gcp:compute:destroy`          | n/a                                         | n/a                                   | `Compute Instance Admin (v1)`     |
| `gcp:storage:write`            | n/a                                         | n/a                                   | `Storage Object Admin`            |

Anything not in the matrix is **denied** rather than defaulting to the
baseline — fail closed, force the operator to declare it.

### 6.4 Auto-approval policy (no human modal)

Machine activations are **not** routed through the human approval modal.
The Entitle policy auto-decides:

```
ALLOW  if  principal ∈ {dashboard-baseline-{aws,azure,gcp}}
       AND action ∈ {configured operation matrix}
       AND ttl_minutes ≤ machine_ttl_ceiling   (default 60)
       AND payload_hash matches the originating request

DENY + ALERT  otherwise.
```

The policy lives in Entitle (per the existing Entitle config flow); the
dashboard just submits the request and reads the decision. No reviewer
pool, no modal, no polling UI for the operator — the resolver `await`s
the decision in-process and the typical end-to-end latency is one
HTTP round-trip to Entitle plus the per-cloud activation call (sub-
second for AWS STS, 1–3s for Azure PIM, 1–5s for GCP IAM Condition
propagation).

`machine_ttl_ceiling` is configurable in **Settings → Integrations →
Entitle → Machine identity gate** but is intentionally a *ceiling*, not
a *default* — the resolver always asks for the minimum it needs for the
operation (e.g. 15min for a single EC2 deploy, 30min for a Packer image
capture). The ceiling is the hard upper bound that Entitle will refuse
to exceed.

**Denials are alerts.** Every denied request fires:

1. A row in `EntitleActivation` with `status="denied"` and the reason.
2. A webhook to the operator's configured alert sink (Slack / email /
   PagerDuty — wired through the existing notification framework).
3. A banner on the dashboard's admin home for the next 24h:
   "Cloud elevation denied — review Entitle audit log."

A legitimate dashboard never produces these alerts, so any occurrence is
worth investigating.

### 6.5 Cache invalidation

Three triggers invalidate a cached activation:

1. **TTL** — `now >= expires_at - 30s` (safety margin so an in-flight SDK
   call can't race the expiry).
2. **Webhook revocation** — Entitle supports a "revoke" webhook event
   (`POST /api/approvals/webhook` with `status="revoked"`); we treat it as
   immediate cache eviction + cloud-side teardown:
   - AWS: nothing to do; STS creds simply remain valid until natural expiry.
     We tolerate this — TTL ≤1h.
   - Azure: call PIM deactivate on the SP.
   - GCP: call `setIamPolicy` to remove the condition-bound binding.
3. **Manual** — `POST /api/cloud-identity/{cloud}/{operation}/revoke` (admin
   only) for the operator break-glass case.

### 6.6 Graceful failure

There are two failure modes — they're treated very differently.

**Entitle unreachable** (network failure, Entitle outage):

- Read paths never hit Entitle (baseline only) — unaffected.
- Write paths return `503 Service Unavailable`: `"Cloud elevation service
  is unreachable. Cloud write paths are gated; retry when service is
  restored."`
- We **do not** auto-fall-back to the baseline + a "best-effort" write.
  Silent privilege fall-back is the bug class this whole design exists to
  prevent.
- The kill-switch (`cloud_identity_gate_enabled=false` in config) exists
  for genuine emergency-break-glass; it is *not* in the UI as a one-click.
  Friction is the point.

**Entitle denies** (policy refusal — TTL over ceiling, unknown action,
payload-hash mismatch):

- Write path returns `403 Forbidden` with the Entitle denial reason.
- The denial is treated as a **security event**, not a routine error:
  - Alert sink fires (Slack/email/PagerDuty).
  - Admin-home banner appears.
  - The audit row stays in `EntitleActivation` with `status="denied"`.
- Operators should investigate every denial. They are not expected in
  normal operation.

### 6.7 Audit trail

Each `EntitleActivation` row records:

- `operation`, `cloud`, `role`, `requester_user_id` (who in the dashboard
  triggered the action), `entitle_policy_id` (which Entitle policy clause
  matched), `auto_approved` (always true for machine flows), `status`
  (`granted` | `denied` | `revoked` | `expired`), `denial_reason` (if
  denied), `payload_hash`, `entitle_request_id`, `granted_at`,
  `expires_at`, `revoked_at`.

Plus, on every cloud write call the resolver passes through, the resolver
appends a session tag (AWS) / `_x-ms-correlation-request-id` header (Azure)
/ user-agent suffix (GCP) of `entitle:{request_id}` so the cloud-side audit
log (CloudTrail / Activity Log / Audit Logs) can be joined to the Entitle
approval after the fact.

## 7. Setup wizard additions

Wizard Step 5 ("Entitle") gains a sub-section **Machine identity gate**:

| Field                              | Notes |
|------------------------------------|-------|
| `cloud_identity_gate_enabled`      | Master toggle |
| `machine_ttl_ceiling_minutes`      | Hard upper bound Entitle will honour per activation; default 60. Requests above this are denied + alerted |
| Alert sink (Slack/email/PagerDuty) | Where denial events are routed |
| AWS baseline access key ID/secret  | Replaces the existing single AWS credential pair; this one is RO |
| AWS Entitle trust issuer URL       | From Entitle's per-AWS-account config |
| AWS operation→role JSON            | The matrix from §6.3, AWS columns |
| Azure baseline SP credentials      | The existing SP, now Reader-only |
| Azure operation→role JSON          | Matrix, Azure columns |
| GCP baseline SA JSON               | Existing SA, now Viewer-only |
| GCP Entitle activator SA email     | The SA Entitle uses to flip bindings |
| GCP operation→role JSON            | Matrix, GCP columns |

These fields are admin-only in **Settings → Integrations → Entitle** post-
wizard. Existing operators who don't want this gate at all leave
`cloud_identity_gate_enabled=false`; behaviour is unchanged from today.

## 8. Migration path

Phased rollout per cloud — AWS first (easiest IAM model), Azure second, GCP
third (most operationally fiddly because of the Condition sweeper).

### Phase 0 — Pre-work (no behaviour change)

- Build the resolver + factory + matrix scaffolding behind
  `cloud_identity_gate_enabled=false`. Default off. Ship.
- Add `EntitleActivation` table + migration.
- Smoke test: with the gate off, every cloud client construction goes
  through the factory but the factory short-circuits to today's credential.
  Verify no regression in normal flows.

### Phase 1 — AWS, opt-in

- Document the IAM role definitions the operator must create (Terraform
  module in `terraform/entitle_iam/` ships with the dashboard).
- Operator runs the module, populates the matrix, flips
  `cloud_identity_gate_enabled=true` for AWS only (`aws.enabled=true`,
  others stay `false`).
- Resolver routes AWS through Entitle; Azure/GCP stay on legacy creds.
- Run for ≥2 weeks against dev/prod before community release.

### Phase 2 — Azure

- Operator configures PIM-eligible assignments on the SP.
- Flip `azure.enabled=true`.
- Watch for token-cache refresh issues (the known sharp edge of this design).

### Phase 3 — GCP

- Operator configures the Entitle activator SA.
- Deploy the orphaned-binding sweeper as a separate `gcp_pim_sweep` service
  in `docker-compose.yml`.
- Flip `gcp.enabled=true`.

### Phase 4 — Tighten baseline

Once all three are gated, *separately* tighten the baseline identity privileges
(strip write privileges off the baseline IAM user / SP / SA). This is its own
flip because if the resolver has any bug that falls back to the baseline,
write paths must fail rather than silently use over-privileged baseline creds.

## 9. Open questions

- **Entitle policy authoring for auto-approval.** Confirm the exact policy
  DSL needed to express `auto-approve if principal=X AND action=Y AND
  ttl≤60min; deny otherwise`. Entitle's product is human-centric so the
  no-reviewer policy path may need product confirmation. If they can't
  express it natively, fallback is a thin "policy decision endpoint"
  Entitle calls before showing a reviewer (effectively pre-empting human
  review). Worst case: we run a single bot reviewer account that
  auto-clicks approve on policy-matching requests — ugly but works.
- **Ceiling per cloud or global?** Default in the design is one global
  `machine_ttl_ceiling`. Some operations (e.g. Packer image capture) may
  legitimately want 30–45min while a tag rewrite needs 2min. Decide
  whether the ceiling is per-(cloud,operation) or global. Recommendation:
  per-operation, with global default of 60min.
- **AWS instance profile passrole.** `dashboard-ec2-deploy` needs `iam:PassRole`
  on the instance profile used by deployed EC2s. If customers want per-workgroup
  instance profiles, the role policy gets one `PassRole` per workgroup — or
  we templatize via a tag condition. Pick during Phase 1 build.
- **GCP cross-project deploys.** The IAM Condition has to be set on the
  workload project, not the dashboard project. The Entitle activator SA
  needs `iam.securityAdmin` on each workload project. Decide whether we
  ship Terraform for this or document it.
- **Terraform-backed deploys.** Terraform reads creds from the environment
  / boto session, so the resolver must *export* the temporary creds into
  the subprocess env vars (AWS) / set `ARM_*` env vars (Azure) / write a
  short-lived SA key file (GCP). Each is doable; sharp edges per cloud
  documented at build time.
- **What does the operator see when Entitle is down?** Beyond the 503,
  do we need a banner on every cloud page ("Cloud writes are gated and
  the approval service is unreachable")? Probably yes; cheap addition.

## 10. Trade-offs we're accepting

- **Latency.** Every cold write path adds an Entitle round-trip plus a
  per-cloud activation call. With auto-approval, this is sub-second for
  AWS, 1–3s for Azure (PIM), 1–5s for GCP (IAM Condition propagation).
  Cached for the activation TTL, so amortizes well across multi-call
  operations.
- **Webhook ingress.** Not required for machine flows specifically — the
  resolver waits synchronously for the policy decision rather than the
  async-webhook pattern used by human approvals. But the human gate
  (still optional, still useful for secret reads / updates / deletes)
  does need ingress. If the operator runs *only* the machine gate they
  can avoid the public-URL requirement.
- **Entitle request volume.** Each cold cache miss is one Entitle
  request. Worth pricing against expected deploy rate, but volume is
  bounded by the cache TTL so realistic upper bound is ~1 req/min/cloud
  on a busy dashboard.
- **Operational complexity.** Three cloud-native primitives (STS / PIM /
  IAM Conditions) each have their own failure modes. We're trading one
  failure mode (leaked credential) for several smaller ones (PIM lag,
  Condition propagation, STS clock skew). The sweeper + observability
  in §6.7 is how we keep that manageable.
- **Alert-fatigue risk.** Because *every* denial is treated as a security
  event, a bug in the operation matrix that requests an unconfigured
  action will fire alerts until fixed. This is a deliberate design choice
  (loud over quiet) but the operator needs a runbook for triaging the
  first few denials post-rollout.

## 11. What we're NOT doing in v1

- Per-workgroup role mapping. The matrix is global. If `team-alpha` and
  `team-bravo` need different IAM scopes, add it in v2.
- Human-in-the-loop approval for machine flows. Policy is auto-approve
  within ceiling, deny otherwise. Routing machine activations to a human
  reviewer is explicitly out of scope — the existing *user* gate already
  covers that use case for secret operations.
- TTL extension or renewal. A long-running operation must request a new
  activation when its current credential expires; we don't extend in
  place. Keeps the audit trail tidy and the ceiling meaningful.
- Replacing the IAM-user-with-keys baseline with IRSA / Workload Identity /
  Workload Identity Federation. Worth doing eventually; out of scope for
  this design which is about *write* paths.

---

## Appendix A — Sequence diagram (AWS deploy)

```
operator                dashboard            Entitle           AWS STS         AWS EC2
   |                       |                    |                |                |
   | POST /api/aws/deploy  |                    |                |                |
   |---------------------->|                    |                |                |
   |                       | POST /policy/decide (action=aws:ec2:deploy,         |
   |                       |   principal=dashboard-baseline-aws, ttl=15min,      |
   |                       |   payload_hash=...)                                 |
   |                       |------------------->|                |                |
   |                       |   ALLOW + signed JWT (TTL bounded)  |                |
   |                       |<-------------------|                |                |
   |                       | AssumeRoleWithWebIdentity (Entitle JWT)             |
   |                       |---------------------------------->  |                |
   |                       |     temp creds (15min)              |                |
   |                       |<----------------------------------  |                |
   |                       | RunInstances + CreateTags(ManagedBy, Workgroup, entitle:<id>) |
   |                       |--------------------------------------------------> |
   |                       |     instance-id                                    |
   |                       |<-------------------------------------------------- |
   |   200 + job id        |                                                    |
   |<----------------------|                                                    |

(No operator-visible modal. Total added latency ≈ 200-500ms vs current path.)

If Entitle DENIES (e.g. ttl request exceeded ceiling):

   |                       |   DENY (reason: ttl_exceeds_ceiling)|
   |                       |<-------------------|
   |   403 + reason        |
   |<----------------------|
   (Alert sink fires; admin-home banner; EntitleActivation row inserted with status=denied)
```

## Appendix B — Why not just shrink the three IAM users?

We could just write tighter IAM policies on the three existing identities
(no Entitle). That helps but doesn't change the leak-and-it's-permanent
property — a stolen key still gets the union of everything the dashboard
ever does, until rotation. The point of JIT is **time-boundedness** and
**approval-boundedness**, which static policies cannot give you.

## Appendix C — Why Entitle and not native cloud JIT (e.g. AWS IAM Identity
Center session policies)?

Each cloud has a JIT story — they just don't compose across providers and
they're hard to put a human-readable approval workflow on. Entitle's value
here is being the *one* policy engine that fronts all three and gives the
operator a single audit log. If a customer prefers to use cloud-native JIT
in only one cloud, the design lets them set `cloud_identity_gate_enabled`
per-cloud — but the resolver always goes through Entitle if it's enabled
for that cloud.
