# Design — Entitle User-Based JIT Authorization (Entra Quickstart)

> **Status:** Design + execution plan, v1.
> **Audience:** enterprise operators wiring the dashboard into an
> existing Entra ID + Entitle deployment. Community/dev installs
> that don't run Entra can skip it; the feature is purely additive.
> **Companion to** [`integrations/entitle.md`](../integrations/entitle.md)
> (the human-approval-gate feature already shipping) and
> [`design/cloud-identity-jit.md`](cloud-identity-jit.md) (the
> machine-identity story). This doc is the third leg: user-level
> dashboard authorization granted just-in-time.
> **Applies to:** community + dev + prod + QA. Same shape
> everywhere; prod's multi-tenancy story adds the per-tenant variation
> described in §6.

---

## 1. Problem

The dashboard already has two pieces of Entitle integration:

1. **Approval gate** ([integrations/entitle.md](../integrations/entitle.md))
   — a human operator hits a privileged endpoint, the dashboard
   opens an Entitle request, and the endpoint executes once
   approved. Per-action.
2. **Machine identity JIT** ([design/cloud-identity-jit.md](cloud-identity-jit.md))
   — the dashboard's own service principals request cloud-side
   privileges from Entitle for the duration of a workflow.

What's missing is the **third leg**: a user-level authorization
JIT. Today the dashboard's permissions are statically assigned —
either via admin → Users → set permissions, or via OAuth group
mapping seeding `default_permissions` on auto-created users.
Either way, once a user has `aws:write`, they keep it indefinitely
until an admin removes it.

For enterprise environments, this is a recurring audit gap:
- Long-tail standing privilege ("Alice last used `aws:write` six
  months ago but still has it").
- No paper trail for why Alice has the privilege.
- Removal requires an admin to remember + act.

What enterprises want from Entitle is the *exact* same shape as
their cloud-IAM JIT story: "I need `aws:write` on the dashboard
for 4 hours" → Entitle workflow → time-bound grant → automatic
expiry. The dashboard's role is to recognise the grant when the
user presents their token.

## 2. Goal

A user requests dashboard permissions via Entitle's normal
workflow; on approval, Entitle adds them to a matching Entra
group with an expiry; the next dashboard request (or relog)
reflects the new permissions. When the group membership expires,
the permissions go with it. **No admin intervention required to
grant or revoke.**

Concretely:

- For every dashboard permission tuple (`<scope>:<level>` —
  `aws:read`, `vms:write`, `images:delete`, etc.) there is an
  Entra ID security group named `dashboard-<scope>-<level>`.
- For every workgroup, there's an Entra group
  `dashboard-workgroup-<name>` — workgroup membership is
  Entitle-grantable too.
- `dashboard-admin` exists as a separate, high-value group;
  membership grants the `is_admin` flag for the duration.
- An Entitle **virtual application** named "VM Dashboard"
  exposes each of these groups as a grantable resource. Entitle
  policy (auto-approve / human-approved / time-of-day) is
  configured per-resource by the operator.
- The dashboard's existing `oauth_group_mapping` infrastructure
  drives permission resolution from group membership on every
  login — no per-request Graph API call.

## 3. Non-goals

- **Not** replacing the static-admin-set permission path. An
  operator may still want to grant a long-lived role manually
  (e.g. for a service account). The Entitle JIT path is the
  *preferred* enterprise path but not the only one.
- **Not** introducing a new identity provider. The user is
  already on Entra OIDC; we're just adding more groups to their
  token.
- **Not** introducing per-request Graph API calls. Permission
  resolution stays at login time (or token refresh time);
  shorter JWT TTLs are the lever for tighter grant cycles.
- **Not** replacing the human-approval gate on secrets. That
  feature stays — it gates *actions*; this feature gates
  *authorizations*. Different layers.

## 4. Threat model & what this buys

| Threat | Before | After |
|---|---|---|
| Stale standing privileges | Admin must remember to remove | Group expiry handles it; "stale grant" is no longer a category |
| Unauthorised admin escalation | Admin promotion is a manual change with weak audit | Admin grant goes through Entitle's policy + approval log |
| Audit "why does Alice have this?" | "Check the user_audit_log + ask the admin who set it" | Entitle's request log has the requester, approver, justification, TTL |
| Compromised user credential | Attacker inherits all standing privileges immediately | Attacker inherits only currently-active grants; the next refresh window drops them |
| Insider abuse of admin | Admin can grant themselves anything indefinitely | Admin can still self-grant, but every grant has a TTL ceiling + audit entry |

The dashboard is **still a privileged box** — what changes is that
the *user privileges within it* are no longer the ceiling on
blast radius for a single compromised account.

## 5. How the pieces connect

### 5.1 The full grant flow

```
1. Alice opens Entitle → searches "VM Dashboard / aws:write".
2. Entitle workflow:
     - Auto-approve if policy matches (e.g. Alice is in the
       data-engineering group AND requested TTL ≤ 4h)
     - Otherwise routes to a human approver
3. On approval, Entitle calls Microsoft Graph:
       POST /groups/{dashboard-aws-write}/members
       /$ref { @odata.id: ".../users/{alice}" }
     with a roleAssignmentSchedule expiry of 4h from now.
4. Alice's next login (or her next token refresh) picks up the
   new group membership from the Entra ID token claim.
5. The dashboard's OAuth callback writes her permissions row
   from the group membership.
6. 4 hours later, Entitle auto-revokes the Entra group
   membership. Alice's next login sees the group gone; her
   permissions drop back to the union of remaining grants.
```

Steps 1-4 happen entirely outside the dashboard. Steps 5-6 are
the dashboard's only direct interaction.

### 5.2 Entra group inventory

The dashboard quickstart provisions:

| Group name pattern | Mapping in `oauth_group_mappings` | Notes |
|---|---|---|
| `dashboard-admin` | `default_permissions = {"is_admin": true}` | Highest-value grant; recommend non-auto-approve policy |
| `dashboard-<scope>-<level>` for each (scope, level) in `PERMISSION_SCOPES × PERMISSION_LEVELS` | `default_permissions = {"<scope>": ["<level>"]}` | ~27 groups today |
| `dashboard-workgroup-<wg>` for each workgroup the operator manages | `workgroup = "<wg>"` (uses the existing workgroup field) | Per-workgroup membership grant |
| `dashboard-baseline` (optional) | `default_permissions = {"vms": ["read"], "jobs": ["read"]}` | Default safe-read role for any authenticated user; auto-approve recommended |

The naming convention is the operator's, not the dashboard's —
the dashboard just reads `oauth_group_mappings.entra_group_id` to
ID-match the JWT's group claim. The quickstart's value is doing
the consistent naming + provisioning automatically.

### 5.3 Dashboard-side resolution

Today's OAuth callback (`api/auth.py` Azure login flow):
1. Receives the OIDC token from Entra.
2. Reads the `groups` claim (object IDs).
3. Looks up matching rows in `oauth_group_mappings`.
4. **For auto-created users**, applies `default_permissions` +
   `workgroup`.
5. **For existing users**, currently does *nothing* with the
   group claim — permissions are whatever the admin previously
   set.

The minimum change for JIT:

- **Always re-apply** the union of `default_permissions` from
  matched mappings on every login, not just the first one.
- The result becomes the user's `permissions_dict` for this
  session. The DB row reflects this directly (or we add a
  `session_permissions` field if we want to preserve the
  admin-set baseline; see §9 open question).

That's the load-bearing change. Everything else is provisioning
plumbing.

### 5.4 Entitle virtual application config

A "VM Dashboard" Entitle application with:

- One **resource** per Entra group above. Each resource's
  *granting mechanism* is "add to Entra group".
- One **workflow** per sensitivity tier. Recommend three tiers:
  - **Auto-approve, ≤2h TTL** for `dashboard-baseline` and
    `*-read` levels.
  - **Single-approver, ≤24h** for `*-write` levels.
  - **Two-approver, ≤8h** for `*-delete` and `admin`.
- One **policy** per workflow that filters requester (e.g. the
  requester must already be a member of `corp-employees`).

Operators with stricter postures can override per-resource —
e.g. require human approval even for `*-read`.

## 6. Multi-tenancy variation (prod)

Per the [multi-tenancy execution plan](../../docs/multi-tenancy-execution-plan.md),
prod runs N tenants. The Entitle integration needs per-tenant
groups + per-tenant virtual applications:

| Single-tenant (community / pre-MT) | Multi-tenant (prod, post-MT Phase 4) |
|---|---|
| `dashboard-aws-write` | `dashboard-acme-aws-write`, `dashboard-globex-aws-write`, etc. |
| One Entitle virtual application | One virtual application per tenant (or one app with a `tenant` selector — Entitle solutions team confirms which is cleaner) |
| One `oauth_group_mappings` row per group | Same rows, but with the `tenant_id` column populated (already added by MT Phase 0) |

The dashboard's OAuth callback reads the tenant from the resolved
context (MT Phase 4), filters `oauth_group_mappings` to that
tenant, and applies the union of matched mappings.

## 7. Phased execution

```
                  community/dev   QA          prod
Phase 0           ✓ scaffold      —           —
Phase 1           ✓               ✓ smoke     —
Phase 2           ✓               ✓ smoke     —
Phase 3           ✓               ✓ smoke     —
Phase 4           ✓               ✓ rehearsal ✓ rollout
Phase 5           —               ✓           ✓
Phase 6           ✓               ✓           ✓
```

Some phases require real Entra + real Entitle (the bootstrap
needs Microsoft Graph + the Entitle API). Community / dev can
exercise the dashboard-side resolver with a mock Graph response
or a small dev Entra tenant; the "real bootstrap" lives in QA
onwards.

### Phase 0 — Resolver behaviour update (community + prod)

**Goal:** make the OAuth callback re-apply group → permissions
on every login, not just on first user creation. Pure dashboard
code change; no Entra / Entitle dependency.

Adds:
- `api/auth.py` Azure-OAuth callback now reads the JWT's `groups`
  claim on every login.
- Builds the union of `default_permissions` JSON across all
  matched `oauth_group_mappings`.
- Writes the union to `User.permissions` (or a new
  `User.session_permissions` field — see open question §9).
- Existing admin-set users (whose group claim is empty or whose
  groups don't match any mapping) are unaffected.

Exit criteria:
1. A test user with two groups mapped to `{"aws": ["read"]}` and
   `{"vms": ["read"]}` ends up with the union as their permissions.
2. Removing one of those groups (simulate via DB row delete) and
   re-logging-in drops the matching permission.
3. Existing admin-set users keep working with no group claim
   present.

**Runbook stub:** `entitle-user-jit-phase-0-resolver.md`.

**Test environments:** community + dev. Mock Entra groups (just
populate `oauth_group_mappings` directly + use a dev OIDC mock
or a real dev Entra tenant). No Entitle required yet.

### Phase 1 — Bootstrap script: provision Entra groups

**Goal:** a one-shot operator-runnable script that creates the
~27 Entra security groups + populates the matching
`oauth_group_mappings` rows.

Adds:
- `web_dashboard/scripts/bootstrap_entitle_groups.py` —
  PowerShell-equivalent or Python (using `azure-identity` +
  `msgraph-sdk` or raw Graph REST).
- Idempotent: skip groups that already exist; skip
  oauth_group_mapping rows that already exist.
- `--dry-run` + `--yes` mirror the multi-tenancy migration
  script's flags.
- Three sub-modes:
  - `--scope=permissions` — the 27 scope/level groups only.
  - `--scope=workgroups` — the per-workgroup groups (reads from
    the workgroups table; needs to run AFTER workgroups exist).
  - `--scope=all` — everything (admin + permissions + workgroups
    + baseline).
- For prod (multi-tenant), accepts `--tenant=<slug>` and prefixes
  group names with the tenant.

Exit criteria:
1. Running with `--dry-run` against an empty Entra tenant lists
   exactly the groups it would create.
2. Running for real creates them; re-running is a no-op.
3. `oauth_group_mappings` table has one row per provisioned group.

**Runbook stub:** `entitle-user-jit-phase-1-bootstrap-entra.md`.

**Test environments:** QA (needs real Entra tenant + Graph API
access). Community ships the script but most community installs
won't run it.

### Phase 2 — Entitle virtual application provisioning

**Goal:** Terraform module that provisions the Entitle side
(virtual application + resources + workflows) using the
`entitle-terraform-provider`.

Adds:
- `terraform/entitle_user_jit/` Terraform module:
  - `entitle_application "vm_dashboard"` — the virtual app.
  - `entitle_resource "<group>"` for each Entra group from
    Phase 1 — each pointing at the group's Entra object id.
  - Three `entitle_workflow` definitions (auto-approve /
    single-approver / two-approver).
  - `entitle_policy` rules routing each resource to its tier.
- Auth via `ENTITLE_API_KEY` env var (per the Terraform provider
  docs).
- A wrapper script `bootstrap_entitle_app.py` that calls
  `terraform apply` with the right vars.

Exit criteria:
1. `terraform plan` against a clean Entitle tenant prints the
   expected resources.
2. `terraform apply` provisions them; `terraform apply` again is
   a no-op.
3. In the Entitle UI, the "VM Dashboard" app appears with the
   resource list.

**Runbook stub:** `entitle-user-jit-phase-2-bootstrap-entitle.md`.

**Test environments:** QA (needs real Entitle tenant).

### Phase 3 — End-to-end JIT request flow

**Goal:** prove the loop works against real Entra + real Entitle.

The runbook's narrative test:
1. Operator A logs into the dashboard via OIDC — currently has
   no permissions.
2. Operator A opens Entitle's portal, requests `dashboard-aws-read`
   for 1 hour.
3. Approver clicks Approve.
4. Operator A logs out + back into the dashboard.
5. Operator A's permissions now include `aws:read`; they can
   view the AWS Images tab.
6. After 1 hour, Entitle revokes the group membership.
7. Operator A re-logs-in; AWS Images tab returns to forbidden.

Exit criteria:
1. Steps 1-5 work end-to-end with sub-10-minute latency between
   approval and effective permission.
2. Step 6's revoke fires automatically without admin action.
3. Audit log records the grant + revoke (visible via dashboard
   audit log; Entitle has its own).

**Runbook stub:** `entitle-user-jit-phase-3-e2e.md`.

**Test environments:** QA + a dedicated test user.

### Phase 4 — UI affordances + operator polish

**Goal:** small UI touches that make the JIT experience
discoverable.

Adds:
- "Request access" link in the dashboard nav (admin-configurable
  URL — operator points it at their Entitle portal).
- On a 403 page, surface a "You don't have `<scope>:<level>` —
  request it" link that deep-links to the matching Entitle
  resource.
- `/settings → Integrations → Entitle` gains a "User JIT
  enabled" toggle + URL field for the request portal.
- Documentation note: shorter JWT TTL = tighter grant cycle
  (recommend ≤30min for high-sensitivity tenants; default is
  several hours today).

Exit criteria:
1. Nav link shows when the operator has configured the URL.
2. 403 page deep-links land on the right Entitle resource.
3. JWT TTL is documented as a tuning knob.

**Runbook stub:** `entitle-user-jit-phase-4-ui.md`.

**Test environments:** community + dev + QA + prod.

### Phase 5 — Multi-tenancy hooks (prod only)

**Goal:** per-tenant group naming, per-tenant Entitle apps,
tenant-scoped oauth_group_mappings.

Adds:
- Bootstrap script gains the `--tenant=<slug>` flag.
- Group naming becomes `dashboard-<tenant>-<scope>-<level>`.
- `oauth_group_mappings.tenant_id` populated (column already
  exists from MT Phase 0).
- OAuth callback resolves user's tenant via MT Phase 4's
  resolver, filters `oauth_group_mappings` to that tenant.
- One Entitle virtual application per tenant (decision to
  confirm with Entitle solutions: alternative is one app with a
  `tenant` request parameter).

Exit criteria:
1. A user in tenant A who is a member of `dashboard-acme-aws-write`
   gets `aws:write` only when they log in via tenant A's
   subdomain.
2. The same group membership in tenant B doesn't grant
   `aws:write` to a user logging in via tenant B (no
   cross-tenant leakage).

Depends on: multi-tenancy plan Phases 4 (auth + memberships) +
5 (tenant CRUD).

**Runbook stub:** `entitle-user-jit-phase-5-multi-tenancy.md`.

**Test environments:** QA + prod (after MT Phase 8 lands first
real second tenant).

### Phase 6 — Docs + adoption playbook

**Goal:** operator-facing playbook.

- Update `integrations/entitle.md` with a new "User JIT" section
  documenting the bootstrap + day-2 operations.
- Migration guide: how to convert a dashboard with static
  permissions into the JIT model.
- Recommend default Entitle policies for each sensitivity tier
  (auto-approve thresholds, approver group composition, TTL
  defaults).
- Test plan operators can run quarterly to confirm the JIT loop
  is still tight.

**Test environments:** all.

## 8. Interactions with in-flight parity plans

| Other plan / phase | This plan's phase that depends on it | Why |
|---|---|---|
| Multi-tenancy Phase 0 (`oauth_group_mappings.tenant_id`) | Phase 5 | Already shipped — the column exists |
| Multi-tenancy Phase 4 (auth + tenant resolver) | Phase 5 | The OAuth callback needs the resolved tenant to filter mappings |
| Multi-tenancy Phase 5 (tenant CRUD) | Phase 5 | Per-tenant bootstrap needs CRUD to know which tenants to provision |
| Secrets parity 5.7 (prod setup wizard) | Phase 1 | The wizard can optionally trigger `bootstrap_entitle_groups.py` as a setup step for enterprise installs |
| Cloud-identity JIT (machine-flow) | None directly | Both use Entitle but the surfaces don't overlap — user JIT grants Entra group membership; machine JIT grants cloud IAM. Documented as complementary, not coupled |
| Approval gate (existing) | None directly | Both layers stay — approval gate gates *actions*, this plan gates *authorizations* |

## 9. Open questions

- **`User.permissions` vs `User.session_permissions`.** Today
  `permissions` is the single source of truth. Phase 0's
  resolver could overwrite it on every login, which loses the
  admin-set baseline if an admin had previously hand-tuned the
  user. Alternative: add `session_permissions` as a separate
  column that's the union of group-derived + admin-set, refreshed
  per login. Decide before Phase 0 ships. Recommend
  `session_permissions` for safety.
- **Removing a group while a session is active.** A user with an
  active JWT keeps their permissions until the JWT expires (or
  they refresh). For sensitivity-tier-3 grants (delete / admin),
  this is acceptable up to JWT TTL. Below that, consider adding
  a per-request Graph check for admin-equivalent grants only
  (not all 27 grants — that's too chatty).
- **Group object IDs are tenant-specific.** The bootstrap script
  needs to know the Entra tenant ID at provisioning time, not
  just at runtime. Confirm the script reads it from the same
  `azure_tenant_id` config the rest of the dashboard uses.
- **Entitle policy DSL coverage.** The "auto-approve if TTL ≤ X
  AND requester ∈ group Y" shape is presumed; confirm with
  Entitle solutions during Phase 2 build.
- **JWT refresh frequency.** Today's tokens are several-hour TTL.
  Tighter tokens = tighter grant cycle but more login friction.
  Phase 4 surfaces this as a documented tuning knob; consider
  whether to ship a default change with Phase 0.
- **`dashboard-workgroup-<wg>` overlap with existing workgroup
  field.** The current `oauth_group_mappings.workgroup` column
  assigns a single workgroup per mapping. The JIT model wants
  workgroup *membership* granted per-group. May need a slight
  schema update — confirm in Phase 1.

## 10. Sizing

| Phase | Engineer-days | Wall-clock |
|---|---|---|
| 0 (resolver) | 0.5 | 2 days |
| 1 (Entra bootstrap script) | 1.5 | 1 week (waiting on Graph API access in QA) |
| 2 (Entitle app provisioning) | 1 | 1 week (Terraform module + Entitle tenant) |
| 3 (E2E test) | 0.5 | 1 week (QA observation) |
| 4 (UI polish) | 1 | 3 days |
| 5 (multi-tenancy hooks) | 1 | 2 weeks (waiting on MT Phase 4+5) |
| 6 (docs + playbook) | 1 | 3 days |
| **Total** | **~6.5 eng-days** | **~6 weeks wall-clock** |

Wall-clock dominated by waiting on QA Entra/Entitle access and
the multi-tenancy dependency.

## 11. Test environment matrix

| Capability | Community | Dev | QA | Prod |
|---|---|---|---|---|
| Resolver re-applies group → permissions on every login | ✓ | ✓ | ✓ | ✓ |
| Removing a group claim drops the matching permission | ✓ | ✓ | ✓ | ✓ |
| Bootstrap script provisions Entra groups | — | mock | ✓ load-bearing | ✓ |
| Bootstrap script provisions Entitle app via Terraform | — | mock | ✓ load-bearing | ✓ |
| End-to-end Entitle request → group → permission loop | — | — | ✓ load-bearing | ✓ |
| Group expiry revokes permission within JWT TTL | — | — | ✓ | ✓ |
| Per-tenant groups + Entitle apps | — | — | ✓ load-bearing | ✓ |
| "Request access" UI affordances | ✓ | ✓ | ✓ | ✓ |

The **load-bearing-in-QA** tests are the end-to-end grant loop
(Phase 3) and the per-tenant variation (Phase 5). Don't ship
Phase 4's prod rollout without Phase 3 green in QA against a
real Entitle tenant.

---

## Appendix — Why this complements (doesn't replace) the
approval gate

Today's [approval gate](../integrations/entitle.md) makes the
dashboard ask Entitle for permission on each privileged
*action*: "Operator A is trying to delete secret X — is this
approved?" Entitle says yes / no, the action proceeds or 403s.

This plan adds a layer below that: "Operator A wants the
*ability* to attempt delete-secret actions on this dashboard
in the first place." Both layers can be active simultaneously:

- User has `secrets:write` (granted JIT via this plan) → reaches
  the delete-secret endpoint → approval gate fires → Entitle
  asks for action approval → operator approves → delete runs.
- User does not have `secrets:write` → reaches the
  delete-secret endpoint → 403 from the dashboard's RBAC →
  never reaches the approval gate.

Defence in depth: authorization is gated; action is gated.
Compromised credential gets reduced blast radius from both
sides.
