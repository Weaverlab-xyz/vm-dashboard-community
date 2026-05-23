# Design — Machine-Identity JIT Cloud Access via Entitle

> **Status:** Design draft v2 — revised after validation against Entitle's
> public API and Terraform-provider docs. No code changes yet.
> **Scope:** Community + prod/dev. Community is the reference target.
> **Depends on:** the existing human-facing Entitle approval gate already
> shipping for secret read / update / delete (see
> [`integrations/entitle.md`](../integrations/entitle.md)). This doc
> extends the same Entitle tenant to cover the dashboard's *machine*
> identity elevations.
> **Validation notes:** see Appendix D for the per-claim check against
> Entitle's docs.

---

## 1. Problem

Today the dashboard holds **three long-lived cloud identities** with broad
standing privilege:

| Cloud | Identity                | Standing privilege today |
|-------|-------------------------|--------------------------|
| AWS   | IAM user (access key)   | `ec2:*`, `iam:PassRole`, `s3:*` on dashboard buckets, `ssm:*`, `tag:*` |
| Azure | Service Principal       | `Contributor` on the subscription (sometimes scoped to RGs)            |
| GCP   | Service Account         | `Compute Admin`, `IAM Service Account User`, `Storage Admin` on the dashboard project |

That is convenient but violates least-privilege: a stolen access key,
leaked SP secret, or compromised SA JSON gives an attacker the union of
every privilege the dashboard ever uses, indefinitely, without an
approval trail.

The human-facing approval gate we already shipped (see
[`integrations/entitle.md`](../integrations/entitle.md)) covers the
case where a human reads / updates / deletes a secret. It does **not**
cover the case where the dashboard's own service principal performs a
privileged cloud action on behalf of a workflow (deploy, destroy,
tag-rewrite, Packer capture, etc.).

## 2. Goal

Move the three cloud identities to a **baseline read-only** footing and
make every privileged operation an **on-demand, time-bound elevation**
orchestrated by Entitle, with the elevation request **auto-approved by
policy** when it satisfies (principal, action, TTL ≤ ceiling).

Concretely:

- The dashboard's three primary cloud identities lose all standing write
  privilege. They keep read-only privilege for inventory/list views
  (Dashboard tiles still render without friction).
- Every write code path (`deploy`, `destroy`, `set-tags`, `capture-image`,
  `attach-volume`, etc.) **submits an access request to Entitle** before
  calling the cloud SDK / Terraform.
- Entitle's workflow auto-approves if the request matches policy. Its
  agent then **attaches the matching IAM policy / role assignment / GCP
  IAM binding to the dashboard's baseline identity** for the requested
  duration (≤ 60min ceiling).
- The dashboard's existing SDK client / Terraform invocation now sees the
  elevated permission and proceeds. When the duration expires, Entitle's
  agent revokes the elevation; the dashboard's identity drops back to
  read-only.
- Anything that doesn't match policy — wrong principal, unknown action,
  TTL over ceiling — is **denied, alerted, and audited** as anomalous.

## 3. Non-goals

- **Not** replacing the existing *human* approval gate on secret reads /
  updates / deletes. They target different threat models and stay.
- **Not** introducing per-user cloud identities. The dashboard remains
  the principal; Entitle is the broker for *its* elevations.
- **Not** building a homegrown JIT broker. Entitle is the policy and
  grant engine; the dashboard just submits requests and proceeds when
  permission is in place.
- **Not** changing the secrets backend story. The cloud-action gate and
  the secret-read gate are independent.

## 4. Threat model & what this buys

The value of this design is **not** a human-in-the-loop second pair of
eyes. It is:

1. **Zero standing write privilege** — a stolen credential alone unlocks
   nothing destructive.
2. **Hard TTL ceiling per elevation** — even a successful elevation is
   bounded to ≤ 60min, no extension, no renewal. A new operation gets a
   new request.
3. **Anomaly signal** — every request asking for more than the policy
   ceiling, or for an action not in the operation matrix, is *denied
   and alerted*. Legitimate dashboard code never produces these, so any
   occurrence is high-confidence signal that something is wrong.

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
*credentials it holds* are no longer the ceiling on what an attacker can
do, and **every escalation past the policy is a high-confidence
detection event**.

## 5. How Entitle actually grants the elevation

Validated against Entitle's public docs (Appendix D). This section
replaces the v1 draft's "AssumeRoleWithWebIdentity + PIM activation +
IAM Conditions" mechanics, which assumed a synchronous policy-decision
API Entitle does not provide.

### 5.0 The Entitle grant model — common shape

Entitle is a **grant orchestrator**, not a policy decision point that
issues JWTs. The flow is:

1. **Dashboard submits an access request** to Entitle via REST
   (`POST /v1/access-requests` — see Appendix D for the exact path).
   Body identifies the requesting principal (a synthetic Entitle "user"
   per machine identity — see §6.8), the resource/role/bundle, and the
   requested duration.
2. **Entitle runs the configured workflow.** Our workflow has a single
   "Automatic Approval" stage with `max_duration ≤ machine_ttl_ceiling`
   (default 60min). If the request matches policy, the workflow approves
   in milliseconds; if not, it denies.
3. **On approval, the Entitle agent (running in the customer's
   environment) calls the cloud's IAM API** to attach the role /
   permission to the dashboard's baseline identity for the requested
   duration. The exact API call depends on the cloud — see §5.1–§5.3.
4. **The dashboard polls the request status** (or subscribes to the
   workflow webhook), and once status is `approved` AND the agent has
   confirmed the grant, calls the cloud SDK / Terraform as normal.
   The baseline credential it already holds now has the elevated
   permission.
5. **At TTL expiry**, Entitle's agent revokes the grant. The dashboard's
   identity drops back to read-only.

Two consequences worth calling out:

- **No new credential is issued to the dashboard.** The dashboard keeps
  using its same baseline access key / SP secret / SA JSON. What
  changes is the privilege those credentials carry, for a bounded
  window. This is different from the v1 draft (which assumed STS would
  hand back fresh temporary keys) but it's how Entitle actually works.
- **There is an agent in the customer's environment.** For AWS
  pod-based-identity and most native integrations, Entitle runs an
  agent pod that holds an IAM role allowed to flip permissions. We
  document the agent requirement in setup; community installs that
  cannot host the agent are limited to AWS Identity Center / SSO
  flavored integrations.

### 5.1 AWS — Entitle attaches an IAM policy to the dashboard's IAM user

- **Baseline identity:** IAM user `dashboard-baseline-aws` with
  `ReadOnlyAccess` managed policy plus `tag:GetResources`.
- **Operation roles:** one *Entitle resource* per operation class
  (mapped to a managed IAM policy created at setup). E.g.:
  - `dashboard-ec2-deploy` → managed policy with `ec2:RunInstances`,
    `ec2:CreateTags`, `iam:PassRole` (scoped to the dashboard instance
    profile), tag-condition `aws:RequestTag/ManagedBy=vm-dashboard`.
  - `dashboard-ec2-destroy` → managed policy with `ec2:TerminateInstances`
    scoped to `aws:ResourceTag/ManagedBy=vm-dashboard`.
  - `dashboard-tag-rewrite`, `dashboard-image-builder` similarly.
- **Grant mechanism:** Entitle's agent (running with an IAM role that
  allows `iam:AttachUserPolicy` / `iam:DetachUserPolicy` scoped to the
  baseline IAM user) attaches the managed policy on approval and
  detaches it at TTL expiry.
- **Cross-account / Identity Center flavor:** if the customer uses AWS
  SSO / Identity Center, Entitle has a "Temporary permission set"
  integration that grants a permission set to the user for the duration
  rather than attaching policies. We support both flavors; the operation
  matrix specifies which.
- **TTL semantics:** the grant is bounded by the workflow's
  `max_duration` and Entitle's agent revocation. **AWS itself does not
  expire the policy attachment** — Entitle's agent does. If the agent
  pod dies between attach and revoke, the policy stays attached. See
  §6.7 (sweeper) and §10 (trade-offs).

### 5.2 Azure — Entitle assigns a role with `endDateTime`

- **Baseline identity:** SP `dashboard-baseline-azure` with `Reader` on
  the subscription (or RG).
- **Operation roles:** standard Azure built-in or custom roles. E.g.
  `Virtual Machine Contributor` (RG-scoped), `Storage Blob Data
  Contributor` (storage-account-scoped), `Network Contributor`.
- **Grant mechanism:** Entitle calls
  `Microsoft.Authorization/roleAssignments/write` with the SP as
  `principalId`, the role definition, and an expiry (Azure role
  assignments natively support `endDateTime` via the
  `roleAssignmentScheduleRequests` API; Entitle's docs reference
  time-bound role assignments — example: "Alice received an hour of
  access to an SQL database").
- **Native auto-expiry:** unlike AWS, Azure expires the role assignment
  on its own at `endDateTime`. Entitle's agent doesn't *have* to
  participate in revocation, though it does revoke on workflow
  termination too. This is the cleanest of the three clouds.
- **No PIM dependency (revised from v1):** the v1 draft assumed PIM
  eligible-role activation; Entitle's docs describe direct role
  assignment with expiry rather than PIM activation. PIM-eligible
  assignments are an option if the customer prefers, but not required.
- **Token-cache caveat:** Azure SDK clients cache access tokens for
  ~1h. After Entitle grants the new role, the dashboard must invalidate
  its in-process MSAL cache so the next token reflects the new
  assignment. Handled in the SDK factory (§6.2).

### 5.3 GCP — Entitle adds/removes an IAM binding via `setIamPolicy`

- **Baseline identity:** SA `dashboard-baseline@project.iam.gserviceaccount.com`
  with `roles/viewer` and `roles/storage.objectViewer`.
- **Operation roles:** `roles/compute.instanceAdmin.v1`,
  `roles/storage.objectAdmin`, etc.
- **Grant mechanism (validated):** Entitle calls
  `projects.setIamPolicy` (also `storage.buckets.setIamPolicy`,
  `iam.serviceAccounts.setIamPolicy` for narrower scopes) to add a
  binding granting the dashboard SA the operation role. At TTL expiry,
  Entitle calls `setIamPolicy` again to remove the binding. **This is
  agent-scheduled revocation, not IAM-Condition-based native expiry**
  — confirmed by the integration's `iam_policy_auditing: true` option
  which embeds grant time and request number into the IAM policy
  itself (a marker that wouldn't be needed if Conditions enforced
  expiry server-side).
- **Deployment model is flexible.** Unlike AWS pod-based-identity, GCP
  supports four ways to host Entitle:
  - **Cloud-hosted** (Entitle calls GCP APIs from its own cloud; known
    egress IPs the customer allow-lists) — no in-customer footprint.
  - **Self-hosted agent**
  - **GKE pod-based identity**
  - **EKS-hosted agent**

  Community installs without GKE can use the cloud-hosted model and
  avoid running an agent. Document the egress IP allow-list step.
- **Audit option to enable:** set `iam_policy_auditing: true` in the
  Entitle GCP integration config — this writes the grant timestamp and
  Entitle request ID into the IAM policy itself, joining cleanly with
  the `entitle:{request_id}` user-agent tag the dashboard already
  appends (§6.7).
- **Drift risk same as AWS.** Because revocation is Entitle-driven
  (cloud-hosted or agent), an Entitle outage between attach and revoke
  can leave bindings active past TTL. The hourly sweeper (§6.7)
  reconciles this; checks active bindings against `EntitleActivation`
  rows and reports orphans.
- **Cross-project:** the binding lives on the workload project, not
  the dashboard project. The Entitle integration must have
  `resourcemanager.projects.setIamPolicy` on each workload project.

## 6. Dashboard-side design

### 6.1 Request submitter (replaces "credential resolver" from v1)

The v1 draft had a `cloud_identity_service.get_credential()` that
returned a new SDK-ready credential. That was wrong — there is no new
credential; the baseline credential's privilege simply changes. The
new shape is a **request submitter**:

```python
async def elevate(
    cloud: Literal["aws", "azure", "gcp"],
    operation: str,                 # e.g. "aws:ec2:deploy"
    *,
    duration_minutes: int = 15,
    payload_hash: str,              # binds the elevation to the request body
    requester_user_id: str,         # who triggered the dashboard operation
) -> ElevationHandle
```

`ElevationHandle` is a context-manager-style object the caller `async
with`s; it represents an active Entitle activation. On enter:

1. Cache check: if an `EntitleActivation` for `(cloud, operation)` is
   already active and `now < expires_at - safety_margin`, return it.
2. Else `POST /v1/access-requests` to Entitle.
3. Poll the request status (or subscribe to the workflow webhook). Auto-
   approve typically completes within 1–3s for AWS/GCP, 1–5s for Azure.
4. Once status is `granted` AND the agent has confirmed the cloud-side
   grant, return the handle.

On exit: nothing to clean up locally; Entitle revokes server-side at
TTL. If the caller explicitly wants to release early, the handle has a
`release()` method that posts to Entitle's revoke endpoint.

The handle does NOT carry the SDK client. The dashboard's existing
SDK factory yields the same client as before — it just has more
privilege while the handle is active.

### 6.2 Where it's called

Every code path that *writes* to a cloud wraps its SDK calls in an
`elevate()`:

```python
async with cloud_identity.elevate(
    "aws", "aws:ec2:deploy",
    duration_minutes=15,
    payload_hash=hash_request_body(body),
    requester_user_id=current_user.id,
):
    ec2 = boto3.client("ec2", region_name=region)
    ec2.run_instances(...)
    ec2.create_tags(...)
```

For read-only paths, there is no `elevate()` — the baseline credential
already has read access. This is a deliberate API shape: if you forget
to wrap a write, it will fail with `AccessDenied` against the
read-only baseline, which is loud rather than silent.

Files that change (community repo):

| File                                  | Change |
|---------------------------------------|--------|
| `services/cloud_identity_service.py`  | **new** — request submitter + `ElevationHandle` + cache + audit-log writer |
| `services/entitle_service.py`         | Add `submit_machine_request()`, `poll_request_status()`, `release_request()` alongside the existing human-flow helpers. Reuse HMAC webhook handler |
| `services/aws_service.py`             | Wrap all `RunInstances`/`TerminateInstances`/`CreateTags` paths in `elevate(...)`. **No client-factory refactor needed** — clients are unchanged |
| `services/azure_service.py`           | Same; plus invalidate MSAL in-process cache on elevate-enter |
| `services/gcp_service.py`             | Same |
| `services/terraform.py`               | Wrap `terraform apply` / `destroy` calls in `elevate(...)`. Terraform reads creds from env vars / SDK chain — no changes needed there because the credential is unchanged |
| `api/approvals.py`                    | Add `principal_kind` discriminator (`user` / `machine`); webhook flow is shared |
| `database.py`                         | New `EntitleActivation` table (operation, role, requester_user_id, status, expires_at, denial_reason, payload_hash, entitle_request_id) |

### 6.3 Operation → Entitle resource matrix

The mapping lives in `config_service` as a JSON blob editable from
**Settings → Integrations → Entitle (Machine Roles)**. Each entry maps
a dashboard operation to an Entitle *resource* (which in turn bundles
the cloud role / policy / permission set). Default seeded values:

| Operation key                  | AWS (Entitle resource → IAM policy)          | Azure (Entitle resource → role)               | GCP (Entitle resource → role)        |
|--------------------------------|----------------------------------------------|-----------------------------------------------|--------------------------------------|
| `aws:ec2:read`                 | *(baseline — no elevation)*                  | n/a                                           | n/a                                  |
| `aws:ec2:deploy`               | `dashboard-ec2-deploy`                       | n/a                                           | n/a                                  |
| `aws:ec2:destroy`              | `dashboard-ec2-destroy`                      | n/a                                           | n/a                                  |
| `aws:ec2:tag-rewrite`          | `dashboard-tag-rewrite`                      | n/a                                           | n/a                                  |
| `aws:image-builder`            | `dashboard-image-builder`                    | n/a                                           | n/a                                  |
| `azure:vm:read`                | n/a                                          | *(baseline — Reader)*                         | n/a                                  |
| `azure:vm:deploy`              | n/a                                          | `Virtual Machine Contributor` (RG)            | n/a                                  |
| `azure:vm:destroy`             | n/a                                          | `Virtual Machine Contributor` (RG)            | n/a                                  |
| `azure:storage:write`          | n/a                                          | `Storage Blob Data Contributor`               | n/a                                  |
| `gcp:compute:read`             | n/a                                          | n/a                                           | *(baseline — Viewer)*                |
| `gcp:compute:deploy`           | n/a                                          | n/a                                           | `Compute Instance Admin (v1)`        |
| `gcp:compute:destroy`          | n/a                                          | n/a                                           | `Compute Instance Admin (v1)`        |
| `gcp:storage:write`            | n/a                                          | n/a                                           | `Storage Object Admin`               |

Anything not in the matrix is **denied** rather than defaulting to the
baseline — fail closed.

### 6.4 Auto-approval policy (no human modal)

Validated: Entitle natively supports "Automatic Approval" as an approver
type ("The step is approved automatically, without human intervention")
and workflow rules with `if/then` semantics ("A condition (if) defines
when the rule applies. An approval process (then) defines how the
request is approved"), evaluated top-to-bottom. Workflows can enforce
a maximum duration ("Less than: Select a maximum duration for
requests").

Our workflow:

```
Rule 1 (auto-approve machine identity requests):
  IF requester ∈ {dashboard-baseline-aws, dashboard-baseline-azure, dashboard-baseline-gcp}
     AND resource ∈ {configured operation matrix}
     AND requested_duration ≤ machine_ttl_ceiling   (default 60min)
  THEN
     approver: Automatic Approval
     max_duration: machine_ttl_ceiling

Rule 2 (catch-all deny):
  IF requester ∈ {dashboard-baseline-*}
  THEN
     deny + fire webhook
```

This is ordinary Entitle configuration — no custom code in Entitle, no
hosting requirement beyond what the existing human gate already has.

**Denials are alerts.** Every denial fires:

1. A row in `EntitleActivation` with `status="denied"` and the reason.
2. A webhook to the operator's configured alert sink (Slack / email /
   PagerDuty — wired through the existing notification framework).
3. A banner on the dashboard's admin home for the next 24h.

A legitimate dashboard never produces these. Any occurrence is worth
investigating.

### 6.5 Cache invalidation

Cached `ElevationHandle`s are released by:

1. **TTL** — `now ≥ expires_at - 30s` safety margin.
2. **Webhook revocation** — Entitle posts `status="revoked"` to the
   webhook; cache evicted; cloud-side teardown is Entitle's
   responsibility but we wait for confirmation before declaring
   release.
3. **Manual** — `POST /api/cloud-identity/{cloud}/{operation}/revoke`
   (admin only) for the break-glass case.

### 6.6 Graceful failure

Two distinct failure modes.

**Entitle unreachable** (network / outage):

- Read paths are unaffected (baseline only).
- Write paths return `503 Service Unavailable`: *"Cloud elevation
  service is unreachable. Cloud writes are gated; retry when service
  is restored."*
- No silent fall-back to baseline. The kill-switch
  (`cloud_identity_gate_enabled=false`) is config-only, not a UI
  one-click. Friction is the point.

**Entitle denies** (policy refusal — TTL exceeds ceiling, unknown
action, payload-hash mismatch):

- Write path returns `403 Forbidden` with the denial reason.
- Treated as a security event: alert sink fires, admin banner appears,
  audit row remains in `EntitleActivation` with `status="denied"`.

### 6.7 Audit trail + agent-revoke sweeper

Each `EntitleActivation` row records:

- `operation`, `cloud`, `role`, `requester_user_id`, `auto_approved`
  (always true for machine flows), `status` (`granted` | `denied` |
  `revoked` | `expired`), `denial_reason`, `payload_hash`,
  `entitle_request_id`, `granted_at`, `expires_at`, `revoked_at`.

In addition, on every cloud write the resolver tags the call with
`entitle:{request_id}` (AWS session tag / Azure correlation-request-id
header / GCP user-agent suffix) so CloudTrail / Activity Log / Audit
Logs can be joined to the Entitle request post-hoc.

**Sweeper.** Because Entitle's AWS revocation is agent-driven (and GCP
might be, depending on §5.3 resolution), a once-hourly sweeper compares
active cloud-side grants against the dashboard's `EntitleActivation`
view of "what should be active." Orphans get reported as warnings
(`/admin/cloud-identity/orphans`). Azure usually self-cleans via
`endDateTime`, so its sweeper output is informational only.

### 6.8 Machine-identity encoding

Entitle's UI/workflows are user-centric and have **no native "service
account" identity type**. To use Entitle for machine flows we encode
each cloud principal as a synthetic Entitle "user":

| Cloud | Dashboard principal                       | Entitle synthetic user (`directory`)        |
|-------|-------------------------------------------|---------------------------------------------|
| AWS   | IAM user `dashboard-baseline-aws`         | `dashboard-baseline-aws@machine.internal`   |
| Azure | SP `dashboard-baseline-azure`             | `dashboard-baseline-azure@machine.internal` |
| GCP   | SA `dashboard-baseline@project.iam.*`     | `dashboard-baseline-gcp@machine.internal`   |

These three synthetic users are members of an Entitle directory group
`dashboard-machine-identities`. The workflow rule in §6.4 filters on
membership of this group. This is the supported pattern per Entitle's
docs (Rule conditions filter on requester identity) and confirmed
viable with their solutions team to confirm during Phase 1 build.

A consequence: the *audit trail visible inside Entitle* shows these
synthetic users as the requestors. The *original human user* who
triggered the dashboard operation is recorded inside the dashboard's
`EntitleActivation.requester_user_id` and surfaced when joining the two
audit logs. Documented in the runbook.

## 7. Setup wizard additions

Wizard Step 5 ("Entitle") gains a sub-section **Machine identity gate**:

| Field                              | Notes |
|------------------------------------|-------|
| `cloud_identity_gate_enabled`      | Master toggle |
| `machine_ttl_ceiling_minutes`      | Hard upper bound Entitle will honour per activation; default 60. Requests above this are denied + alerted |
| AWS baseline access key ID/secret  | RO-only |
| AWS Entitle agent role ARN         | Role Entitle's agent assumes to attach policies |
| AWS operation→role matrix          | JSON (§6.3 AWS columns) |
| Azure baseline SP credentials      | The existing SP, now Reader-only |
| Azure operation→role matrix        | JSON (§6.3 Azure columns) |
| GCP baseline SA JSON               | Existing SA, now Viewer-only |
| GCP Entitle activator SA email     | The SA Entitle uses to flip bindings |
| GCP operation→role matrix          | JSON (§6.3 GCP columns) |
| Alert sink (Slack/email/PagerDuty) | Where denial events are routed |

These fields are admin-only post-wizard at **Settings → Integrations →
Entitle**. Most operators won't fill in the JSON matrices by hand —
they'll run the Terraform module described in §8.0 below.

## 8. Migration path

### 8.0 Setup IaC — Entitle config + cloud IAM in one Terraform module

Entitle ships a Terraform provider
([`entitle-terraform-provider`](https://docs.beyondtrust.com/entitle/docs/entitle-terraform-provider))
that exposes the configuration entities we need:
`entitle_integration`, `entitle_workflow`, `entitle_policy`,
`entitle_resource`, `entitle_role`, `entitle_bundle`,
`entitle_permission`. Auth is a bearer token (`ENTITLE_API_KEY`).

We ship a Terraform module `terraform/entitle_setup/` that creates, in
one `terraform apply`:

- Cloud-side: the IAM roles / managed policies / Azure custom roles /
  GCP roles the operation matrix references.
- Entitle-side: the synthetic-user directory group
  `dashboard-machine-identities`, one `entitle_resource` per operation,
  the auto-approve workflow with `max_duration =
  machine_ttl_ceiling_minutes`, and the policy rules from §6.4.
- An `entitle_agent_token` for the customer's Entitle agent pod.

Operators run:

```bash
terraform -chdir=terraform/entitle_setup apply \
  -var "entitle_api_key=$ENTITLE_API_KEY" \
  -var "machine_ttl_ceiling_minutes=60" \
  -var "aws_account_id=..." \
  -var "azure_subscription_id=..." \
  -var "gcp_project_id=..."
```

…and get a fully-configured Entitle tenant + matching cloud IAM in a
single command. No clicking through Entitle's UI to define a dozen
resources/workflows/policies.

What the provider does **not** do: there is no Terraform-native way to
*submit* an access request and pause for approval inside a `terraform
apply` for a *deploy* run. The dashboard still submits requests via
the Entitle REST API at runtime. The provider is setup-time only.

### 8.1 Phase 0 — pre-work (no behaviour change)

- Build the `cloud_identity_service` submitter + `EntitleActivation`
  table + the `terraform/entitle_setup/` module behind
  `cloud_identity_gate_enabled=false`. Ship.
- Smoke test: with the gate off, every cloud write still uses today's
  credential. The submitter is dormant.

### 8.2 Phase 1 — AWS, opt-in

- Operator runs `terraform/entitle_setup/` with `aws.enabled=true`.
  Deploys the Entitle agent pod into the AWS account (per Entitle's
  pod-based-identity setup).
- Flip `cloud_identity_gate_enabled=true` for AWS only.
- Submitter routes AWS writes through Entitle; Azure/GCP stay on legacy
  creds.
- Run for ≥2 weeks against dev/prod before community release.

### 8.3 Phase 2 — Azure

- Operator runs `terraform/entitle_setup/` with `azure.enabled=true`.
- Flip `azure.enabled=true` in the gate config.
- Watch for MSAL token-cache invalidation issues (the known sharp edge
  per §5.2).

### 8.4 Phase 3 — GCP

- Operator runs `terraform/entitle_setup/` with `gcp.enabled=true`.
- Enable `iam_policy_auditing: true` on the Entitle GCP integration so
  grant_time / request_number are embedded in the IAM policy for
  audit joins.
- Choose deployment model (cloud-hosted with IP allow-list, vs
  self-hosted agent / GKE / EKS). Cloud-hosted is the recommended
  default for community installs.
- Deploy the GCP sweeper service (same component pattern as AWS).
- Flip `gcp.enabled=true`.

### 8.5 Phase 4 — tighten baseline

Once all three are gated and verified, *separately* tighten the baseline
identity permissions (strip writes off the baseline IAM user / SP / SA).
This is its own flip because a bug that falls back to baseline must fail,
not silently use over-privileged baseline creds.

## 9. Open questions

- ~~**GCP grant mechanism.**~~ **Resolved (v2.1):** Entitle uses
  `setIamPolicy` calls to add and later remove bindings. Not
  Condition-based. Sweeper required, matching AWS.
- **Entitle access-request REST shape.** The intro docs confirm
  `Access Requests` is a manageable entity. We need the exact POST
  body shape (resource ID? bundle ID? role ID?) for the synthetic-user
  pattern in §6.8. Confirm with Entitle solutions or by inspecting the
  Entitle reference page for `accessrequests_create-1` (URL slug
  varies).
- **Polling vs webhook for grant completion.** Entitle has "Workflow
  webhooks" and "Audit Logs Webhooks." Confirm whether there's a
  webhook fired *when the agent has finished granting* (vs just *when
  the workflow approved*). The dashboard cannot proceed to call the
  cloud SDK until the agent has actually attached the policy — there
  may be a small gap between "approved" and "granted-server-side."
  Plan: poll for `granted` state explicitly, treat `approved` as
  insufficient.
- **Per-operation ceiling vs global.** Some operations (Packer image
  capture) want ~30min; tag rewrite wants ~2min. Default to a global
  60min ceiling; allow per-operation overrides in the JSON matrix.
- **Entitle pricing on machine request volume.** Each cold cache miss
  is one request. Upper bound is ~1/min/cloud on a busy dashboard.
  Confirm pricing model with BeyondTrust before committing.

## 10. Trade-offs we're accepting

- **Latency.** Every cold write adds a request POST + poll + agent-
  side grant ≈ 1–3s for AWS/GCP, 1–5s for Azure (MSAL token refresh).
  Amortizes well across multi-call operations via caching.
- **Agent dependency.** AWS pod-based-identity requires an Entitle
  agent pod in the customer's environment. SaaS hosted tier
  preinstalls it; community single-host setups can fall back to AWS
  Identity Center–style integrations.
- **No native machine-identity model.** Encoding the dashboard's three
  SPs as synthetic Entitle "users" works (§6.8) but is an idiom, not
  a first-class feature. Audit trail joins require care.
- **Agent-driven revocation drift.** AWS (and possibly GCP) rely on
  Entitle's agent to revoke. If the agent dies between attach and
  revoke, the elevation can outlive the TTL until the next sweeper
  pass. The hourly sweeper bounds the drift to ~1h on top of the
  configured TTL. For higher assurance, use Azure-style native
  expiry where available.
- **Webhook ingress.** Not required for machine flows specifically (we
  can poll Entitle for status). The human gate (still optional, still
  useful for secret reads/updates/deletes) does need ingress. Running
  only the machine gate can skip the public-URL requirement.
- **Alert-fatigue risk.** Every denial is treated as a security event.
  A bug in the operation matrix that requests an unconfigured action
  fires alerts until fixed. Deliberate (loud > quiet), but the
  operator needs a runbook for triaging the first few denials
  post-rollout.

## 11. What we're NOT doing in v1

- Per-workgroup role mapping. The matrix is global. Add in v2 if needed.
- Human-in-the-loop approval for machine flows. Auto-approve within
  ceiling, deny otherwise. The human gate already covers secret ops.
- TTL extension or renewal. A long-running operation must request a new
  activation when its current one expires.
- Submitting Entitle access requests from inside `terraform apply`
  (no native Terraform provider support for this; the dashboard
  submits via REST and then invokes Terraform once the grant is in
  place).
- Replacing IAM-user-with-keys baseline with IRSA / Workload Identity /
  Workload Identity Federation. Worth doing eventually; out of scope.

---

## Appendix A — Sequence diagram (AWS deploy, revised)

```
operator                dashboard                Entitle           Entitle agent   AWS IAM         AWS EC2
   |                       |                       |                  |               |               |
   | POST /api/aws/deploy  |                       |                  |               |               |
   |---------------------->|                       |                  |               |               |
   |                       | elevate(aws:ec2:deploy, 15min, hash)     |               |               |
   |                       |                                                                          |
   |                       | POST /v1/access-requests                                                 |
   |                       |---------------------->|                  |               |               |
   |                       |   request_id, status=pending             |               |               |
   |                       |<----------------------|                  |               |               |
   |                       |   (workflow runs: auto-approval matches policy)                          |
   |                       |                       | trigger agent    |               |               |
   |                       |                       |----------------->|               |               |
   |                       |                       |                  | AttachUserPolicy              |
   |                       |                       |                  |-------------->|               |
   |                       |                       |                  |  done         |               |
   |                       |                       |                  |<--------------|               |
   |                       |                       |   status=granted |               |               |
   |                       |                       |<-----------------|               |               |
   |                       | poll GET /v1/access-requests/{id} → granted             |               |
   |                       |<----------------------|                                  |               |
   |                       |   (now elevation handle is active; baseline IAM user has the policy)    |
   |                       | RunInstances + CreateTags(ManagedBy, Workgroup, entitle:<id>)            |
   |                       |--------------------------------------------------------------------> |
   |                       |   instance-id                                                          |
   |                       |<-------------------------------------------------------------------- |
   |   200 + job id        |                                                                          |
   |<----------------------|                                                                          |
   |                       |                                                                          |
   |                       |   (15 min later — TTL expiry)                                            |
   |                       |                       | trigger agent    |               |               |
   |                       |                       |----------------->|               |               |
   |                       |                       |                  | DetachUserPolicy              |
   |                       |                       |                  |-------------->|               |
   |                       |                       |   webhook: revoked                               |
   |                       |<----------------------|                                                  |

If Entitle DENIES (e.g. ttl request exceeded ceiling):

   |                       |   status=denied, reason=ttl_exceeds_ceiling                              |
   |                       |<----------------------|                                                  |
   |   403 + reason        |                                                                          |
   |<----------------------|                                                                          |
   (Alert sink fires; admin-home banner; EntitleActivation row inserted with status=denied)
```

## Appendix B — Why not just shrink the three IAM users?

We could write tighter IAM policies on the three existing identities and
skip Entitle. That helps but doesn't change the leak-and-it's-permanent
property — a stolen key still gets the union of everything the
dashboard ever does, until rotation. JIT's value is **time-boundedness**
and **request-boundedness**, which static policies cannot give.

## Appendix C — Why Entitle and not native cloud JIT?

Each cloud has a JIT story — AWS Identity Center session policies,
Azure PIM, GCP IAM Conditions — but they don't compose across providers
and they're each hard to put a uniform approval/audit workflow on.
Entitle's value is being the *one* policy + audit engine fronting all
three. If a customer prefers cloud-native JIT in only one cloud, the
design lets them flip `cloud_identity_gate_enabled` per-cloud — but
when the gate is on, the path always goes through Entitle.

## Appendix D — Validation against Entitle's public docs

This section records each design claim and the doc evidence backing it.
The v1 draft made several wrong architectural assumptions which v2
corrects.

| Claim (v2)                                                              | Evidence                                                                 |
|-------------------------------------------------------------------------|--------------------------------------------------------------------------|
| Entitle supports auto-approve workflows                                 | "Automatic Approval: The step is approved automatically, without human intervention." — [approval-workflows](https://docs.beyondtrust.com/entitle/docs/approval-workflows) |
| Workflows support if/then conditional rules                              | "A condition (if) defines when the rule applies. An approval process (then) defines how the request is approved." — same                                                  |
| Workflows enforce a max duration                                         | "Less than: Select a maximum duration for requests." — same                                                                                                                |
| AWS grant mechanism is policy-attach, not JWT issuance (corrects v1)     | "Entitle automatically creates a policy with the required permissions and attaches it to the corresponding IAM user." — [aws-iam-pod-based-identity](https://docs.beyondtrust.com/entitle/docs/aws-identity-and-access-management-iam-pod-based-identity) |
| AWS integration runs an agent pod in the customer's env                  | "the role is assumed by the agent pod's IAM role" — same                                                                                                                   |
| Azure grant mechanism is direct role assignment with expiry, not PIM (corrects v1) | "permissions are granted, the employee who requested them gets temporary access" — [entitle-integration-azure](https://docs.beyondtrust.com/entitle/docs/entitle-integration-azure) |
| Azure role assignments are time-bound                                    | "Alice received an hour of access to an SQL database in subscription 1" — same                                                                                             |
| Entitle has Workflow webhooks                                            | Webhook payload includes `stageNumber` / `stageAmount` — [approval-workflows](https://docs.beyondtrust.com/entitle/docs/approval-workflows)                                |
| No synchronous policy-decision API (corrects v1)                         | REST integration is *outbound from Entitle to target system* via `Give Access` / `Revoke Access` POSTs — [entitle-integration-rest](https://docs.beyondtrust.com/entitle/docs/entitle-integration-rest) |
| No native machine-identity type                                          | All workflow examples reference "employee" / "Alice" — same; no service-account predicate documented                                                                       |
| Terraform provider is setup-time only, no runtime access-request resource | Provider resources: integrations, workflows, policies, resources, roles, bundles, permissions, agent_token, access_request_forward (forward, not create). No `entitle_access_request` resource — [entitle-terraform-provider](https://docs.beyondtrust.com/entitle/docs/entitle-terraform-provider) |
| Terraform provider auth is bearer-token                                  | "API bearer authorizations via an Entitle API token… `api_key` or `ENTITLE_API_KEY`" — same                                                                                |
| GCP grant mechanism is `setIamPolicy` add/remove, not IAM Conditions     | "resourcemanager.projects.setIamPolicy / storage.buckets.setIamPolicy / iam.serviceAccounts.setIamPolicy" permissions; `iam_policy_auditing: true` option embeds grant_time + request_number in the policy (only meaningful if Entitle is the one editing it) — [entitle-integration-gcp](https://docs.beyondtrust.com/entitle/docs/entitle-integration-gcp) |
| GCP supports cloud-hosted (no agent) deployment                          | Cloud-hosted mode with known egress IPs documented for allow-listing — same                                                                                                |

**v1 assumptions that were wrong and are now removed:**

- ❌ "Entitle issues a JWT that AWS STS trusts for AssumeRoleWithWebIdentity"
  — Entitle does not issue JWTs the dashboard consumes. The agent attaches
  policies; the dashboard keeps its baseline credential.
- ❌ "Azure activation is PIM eligible-role activation" — Entitle's docs
  describe direct role assignment with expiry, not PIM activation.
- ❌ "Synchronous policy decision via `POST /policy/decide`" — Entitle's
  access-request flow is async (workflow runs after acceptance);
  dashboard must poll or subscribe to webhook.
- ❌ "Modal-less approval polling" — there is still polling, just no
  *user-visible* modal because the auto-approve workflow runs in
  milliseconds. Polling is the dashboard's internal mechanism.
