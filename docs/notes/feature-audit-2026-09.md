# Feature audit: what is built, and what should come next

> **Audience:** contributor · **Profile:** `both` · **Read this when:** you are deciding what to build next, or you want a map of the current feature surface before you add to it.

An audit of the shipping feature set, and four recommendations for work that is not in the
codebase but sits naturally on top of what is.

The hard part was not finding gaps. It was finding gaps that are not already written down.
[saas-roadmap.md](../saas-roadmap.md) carries about twenty-five capabilities with honest
status labels, and [design/](../design/README.md) records the alternatives that were
rejected and why. Continuous Terraform drift detection, compliance-as-code, a two-person
approval gate, scheduled secret rotation, CVE scanning per image, signed build manifests and
a centralised audit pane are **already scoped** — so none of them is a recommendation here.

The other thing worth saying up front: the two highest-ranked items below are not features.
They are authorization gaps in shipped code. They rank first because they are cheap, because
they are load-bearing for anything built on top of them, and because a feature audit that
finds them and files them behind a nice-to-have would be the wrong document.

Dated 2026-09-06. Line references are to that commit.

## Summary

1. **The MCP server does not apply RBAC.** `_mcp_user` is declared at
   `api/mcp_server.py:36` and set at `:391`, and **no tool reads it**. Any valid Personal
   Access Token, from any active user, reads every job row in the estate — including the raw
   `extra_data` of a deploy, which carries `bt_tf_state`, `ps_registration_tf_state` and
   `ssh_secret_name`. `/mcp` is also mounted unconditionally (`main.py:1006`) while every
   router beside it carries a feature or profile gate.
   See [Recommendation 1](#1-make-the-mcp-server-obey-the-permission-model).
2. **Destroy is the least-governed write in the product.** `api/aws.py:860` requires
   `require_permission("aws","delete")` and performs **no workgroup check**, though deploy
   (`:587`, `:659`) and reassign (`:818`) both do. Meanwhile `admission_service.enforce()` is
   called from eleven sites and **every one is a deploy**. The auto-delete reaper needs four
   gates and two arming clocks to delete a VM; a human pressing Destroy passes through none.
   See [Recommendation 2](#2-govern-the-destroy-seams-with-the-engine-that-already-ships).
3. **The audit log has 75 write sites and one boolean read** — and the chain does not cover
   `ip_address`. `audit_chain._canonical` hashes seven fields, and `ip_address` is not among
   them; no call site populates the column today, which is exactly why fixing it now is
   cheap. There is no page, no list endpoint, no export, and nothing verifies the chain on a
   schedule. See [Recommendation 3](#3-make-the-audit-log-readable-and-close-the-chain).
4. **A cloud VM is deploy-or-destroy.** All six other power surfaces — five hypervisors plus
   VMware Workstation — expose `/power/*`. The four cloud consoles expose nothing, though
   the code exists POV-side for all four clouds. It is a smaller port than it looks, for
   reasons worth knowing.
   See [Recommendation 4](#4-cloud-vm-power-control-and-what-it-would-really-cost).
5. **The lab profile out-governs the estate profile.** `pov` has a spend cap that suspends
   and a business-hours suspend schedule. `demo` — the profile running infrastructure
   somebody depends on — has neither, and its only lifecycle control is the destructive one.

## What is built

Scale, so the rest has a denominator: FastAPI + SQLAlchemy + Jinja2, **143,726 lines** of
Python under `web_dashboard/`. **182 service modules**, **57 API modules**, 458 router
routes, **43 tables**, 60 job types, 27 user-facing feature flags, **321 test files**, 31 nav
pages.

| Area | What ships |
|---|---|
| **Clouds** | AWS, Azure, GCP, OCI — VM deploy/destroy/bulk-deploy, image capture and export, image browsing, per-cloud network options |
| **On-prem** | Proxmox, vSphere/ESXi, Hyper-V, Nutanix AHV, XCP-ng, VMware Workstation — inventory and full power control |
| **IaC** | Terraform per deploy, per-job **remote and locked** state in the configured storage backend, idempotent destroy |
| **Images** | Packer build per cloud, an image registry, one-click cross-cloud promote via one-shot ECS/ACI/Cloud Run runners |
| **Config mgmt** | Ansible runner (local or one-shot in-cloud), asset storage on S3/Blob/GCS/local-UNC, upload-time secret scanning, apply-time drift fingerprinting |
| **Databases** | Postgres/MySQL/SQL Server on AWS/Azure/GCP, Oracle on OCI; or register one you already run |
| **Kubernetes** | EKS/AKS/GKE/OKE provision or import, Rancher management plane, ESO secret delivery, PRA tunnels, Entra→RBAC federation |
| **Containers / functions** | ECS/ACI/GCE-COS from a stored Compose file, Portainer CE; one Python handler deployed unchanged to Lambda/Function App/Cloud Run *(preview)* |
| **Remote agents** | Outbound-dialing container, self-generated Ed25519 key, per-job credential sealing, hypervisor discovery, agent-executed config runs |
| **Identity** | Local auth, WebAuthn/FIDO2, Entra OAuth, generic OIDC, PATs, workgroup RBAC, Entitle-granted time-boxed dashboard permissions |
| **BeyondTrust** | PRA (jump items, protocol tunnels, Vault, Gateways), Password Safe (managed systems/accounts, checkout, rotate-on-check-in), EPM-L, Entitle |
| **Platform** | Job queue with live WebSocket output, hash-chained audit log, OPA pre-action admission control, outbound notifications, cost reporting with budgets and scope attribution, secret staleness, auto-delete timer, MCP server, in-app docs viewer, setup wizard |
| **POV profile** | Tenant registry, POV Gateway and Resource Broker, accessor login, share links, use-case checklists, spend caps, suspend schedules, Skytap and cloud lab platforms |

The architecture has habits worth naming, because the recommendations lean on them:

- **Pure policy split from I/O.** `expiry_policy`, `audit_chain`, `config_drift.evaluate`,
  `secret_hygiene.score`, `pov_schedule`, `pov_spend`, `cloud_stats` are importable and
  testable without a database, a clock or a cloud.
- **A sweep enqueues one job; the worker claims it.** `main._expiry_sweeper_loop` creates a
  single `expiry_sweep` row and stops; `jobs_worker._claim_one`'s `UPDATE … WHERE
  status='pending'` rowcount is the lock, which buys a Job row, Live Output, mid-pass cancel
  and stale reconcile for free.
- **Conditions are scanned; events are emitted.** `notify_scanner` walks the three things
  that are a *state* rather than a moment on a timer, deduped by day bucket.
- **Sweepers touch only what the dashboard tagged.** Every one of them. See
  [the reaper's guard](#the-reapers-refuse-to-touch-what-they-did-not-create) — it is a
  stated constraint, not an oversight.
- **A master switch alone changes nothing.** The auto-delete timer needs four gates and two
  separate arming clocks before it deletes anything.

## Where the seams are

### The MCP server does not apply the permission model

*(Fixed — see [Recommendation 1](#1-make-the-mcp-server-obey-the-permission-model).
Kept in the present tense because it is the finding, not the fix.)*

`api/mcp_server.py` authenticates properly. `_validate_pat` (`:328`) hashes the bearer token,
looks up the `PersonalAccessToken` row, checks `is_active` and `expires_at`, and resolves a
`User`. The middleware sets that user into a ContextVar at `:391`.

Then nothing reads it. `grep -n _mcp_user` returns exactly two lines: the declaration and the
set. Every one of the seven tools queries the database directly, with no reference to the
caller.

The contrast with the HTTP surface is stark, and the HTTP surface is right:

| | HTTP | MCP |
|---|---|---|
| List jobs | `api/jobs.py:66` — `owner_filter = None if can_audit_jobs(current_user) else current_user.username` | `db.query(Job)`, plus an **optional caller-supplied** `workgroup` filter (`:136-141`) |
| Job detail | permission check plus owner scoping | `get_job(job_id)` returns the row for any id, **plus raw `extra_data`** (`:157-163`) |
| EC2 instances | `api/aws.py:367` filters by `_accessible_workgroups` | `list_ec2_instances()` — all of them |

A `workgroup` argument the caller chooses is a convenience filter, not an authorization
boundary. So any active user's PAT reads every job in the estate.

`extra_data` is where this stops being abstract. It is the deploy result dict, and for a
cloud VM it carries `bt_tf_state` (`aws_vm_service.py:459`, `azure_vm_service.py:541`) — the
Terraform state of that VM's PRA Shell Jump — along with `ps_registration_tf_state`
(`:724`, `:824`) and `ssh_secret_name` (`:130`). There is no redaction anywhere in that path.

Two aggravating factors:

- **The mount is ungated.** `main.py:1006` is `app.mount("/mcp", get_mcp_asgi_app())`. Every
  router in the surrounding lines carries `_feature_gate(...)` or `_profile_page_gate(...)`.
  MCP carries neither, so it is reachable on every install whether or not anyone wanted it.
- **A PAT has no scopes.** `database.py:265` is `user_id`, `name`, `token_hash`,
  `created_at`, `expires_at`, `last_used_at`, `is_active`. There is no permission subset and
  no read-only flag. A PAT is unbounded impersonation of its owner — which is survivable
  while the surface is genuinely read-only and correctly scoped, and is not survivable
  otherwise.

### Destroy is the least-governed write

`api/aws.py:860`:

```python
@router.delete("/instances/{instance_id}", response_model=DestroyResponse)
async def destroy_instance(
    instance_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "delete")),
):
```

That is the whole authorization. It then queries **every** completed `ec2_deploy` job with no
workgroup filter, matches on instance id, and terminates. The same file validates workgroup
on deploy (`:587`, `:659`) and on the reassign PATCH (`:818`), and filters the list endpoint
by `_accessible_workgroups` (`:367`). Destroy is the one write that skips it. The parallel
endpoints on Azure, GCP and OCI are worth checking on the same axis.

Separately, and compounding it: `admission_service.enforce()` is called from eleven sites —
`api/aws.py:597`, `api/azure.py:813`, `api/gcp.py:737`, `api/oci.py:717`, `api/k8s.py:117`,
`api/cloud_databases.py:340`, three in `api/ot.py`, and `services/deploy_batch.py:127` — and
**every one is a deploy**. The engine has never seen a teardown.

This is not an accident of layering. `services/admission_service.py` explains why the gate
is an inline service-layer call rather than a FastAPI dependency: deploy parameters live in
the request body, which a dependency cannot see. That reasoning is about *where* the call
goes, not about which actions deserve one — a destroy has an identifiable target, a region
and an actor, which is everything a Rego rule needs.

The asymmetry is easiest to see against the auto-delete timer, which exists to delete VMs
carefully. It needs `resource_expiry_enabled`, a stamped `expires_at`, the feature armed for
a delay, and enforcement separately armed — four gates and two clocks. A human pressing
Destroy on the same VM passes through none of them, and no change-freeze window applies.
**The reaper is more constrained than the operator.**

### The audit log is write-only, and the chain has a hole

`services/audit_chain.py` gives every row a `seq`, a `prev_hash` and an `entry_hash`, so any
edit, delete or reorder is detectable. **75 `log_audit` call sites** feed it — agent
enrolment and revocation, hypervisor connection changes, image deletes, OT tunnel teardown,
cloud destroys, admission denials.

The read surface is `api/audit.py`: 29 lines, one admin-only route, `GET /api/audit/verify`,
returning `{ok, count, first_broken_seq}`. `AuditLog` appears in exactly two non-test files.
There is no `/audit` page and no nav entry, no list or filter endpoint, and **no tabular
export anywhere in the product** — the only `Content-Disposition` responses are kubeconfig
downloads at `api/k8s.py:400` and `:569`, and the only client-side `Blob` downloads are the
same kubeconfigs. Nothing calls verify on a schedule.

Three things follow that are worth stating separately:

- **The chain does not cover `ip_address`.** `_canonical` hashes `seq`, `timestamp`,
  `username`, `action`, `target_vm`, `details`, `prev_hash`. The `ip_address` column is
  outside the hash, so anyone who can write the database can rewrite the source address of
  any audit row and `/api/audit/verify` still returns `ok: true`. This is currently moot —
  **zero of the 75 call sites pass `ip_address`**, so the column is uniformly NULL — which is
  precisely why it is cheap to fix today. Changing `_canonical` recomputes every existing
  `entry_hash`, so it is a chain-breaking migration that gets more expensive every month.
- **Verification is O(N) over the whole table.** `job_service.verify_audit_chain` does
  `.all()`. That is fine for a manual admin click and not fine on a timer, especially because
  **`audit_log` has no retention and cannot have one** — pruning any row breaks the chain by
  construction. Compare `jobs`, which prunes completed sweeps by
  `resource_expiry_sweep_retention_days`. The audit table grows monotonically forever.
- **Audit writes are globally serialised.** `job_service.log_audit` takes a
  `pg_advisory_xact_lock` and reads the current chain tip before every append. Anything that
  adds write volume — new gated actions, a sweep that audits per resource — queues behind
  that one lock.

### A cloud VM is deploy-or-destroy

Six routers expose power operations through a `_power_endpoint` route factory:
`api/proxmox.py:412`, `api/xcpng.py:124`, `api/nutanix.py:377`, `api/vsphere.py:169`,
`api/hyperv.py:131`, and `api/vms.py:257` (VMware Workstation, agent-brokered).

The four cloud consoles expose none. Checked three ways rather than assumed: no `/power/*`
route in any of the four; no power job type among the 17 cloud entries in `jobs_worker.py`
(they are `*_deploy`, `*_bulk_deploy`, `*_destroy`, `*_create_image` / `*_capture_image`,
`*_export_image`); and `grep -E 'stop_instance|start_instance|deallocate|power_off'` across
`aws_vm_service.py`, `azure_vm_service.py`, `gcp_vm_service.py` and `oci_vm_service.py`
returns nothing.

One near-miss worth naming so nobody reports it as the feature: `azure_service.py:2079` calls
`begin_deallocate`, inside `_create_image_from_vm_sync` when `generalize=True`. Nothing ever
starts that VM again, and nothing should — generalizing makes it unusable by design. There is
no `begin_start` anywhere in the module.

### The lab profile out-governs the estate profile

| | estate (`demo`) | POV (`pov`) |
|---|---|---|
| Lifecycle control | auto-delete timer — **destroy only**, four gates, two arming clocks | `pov_spend.py` — accrual spend cap that **suspends** |
| Time-based control | none | `pov_schedule.py` — business-hours suspend/resume windows |
| Cloud VM stop/start | **none** | `pov_cloud_{aws,azure,gcp,oci}.py::power` |
| Spend visibility | `/costs` — cross-cloud MTD, split dashboard / sandbox / unattributed | `pov_cloud_cost.py` — per-environment footprint and estimate |

`services/pov_spend.py` argues the case in its own docstring:

> *Reaching the cap suspends; it never destroys. Suspending is reversible in one click,
> which is what lets this feature exist without the auto-delete timer's two arming clocks
> and dry-run mode.*

That reasoning is not POV-specific, and the estate instance — where the resources are ones
somebody depends on — is offered only the irreversible control. The auto-delete page opens by
telling you to read it *before* enabling it, because it deletes infrastructure. A feature
that frightening is a feature that stays off.

### The reapers refuse to touch what they did not create

Stated here because the obvious cost recommendation runs straight into it.
`gcp_service._cloud_run_reap_target` lists three guards, the first of which is
`labels.managed-by == vm-dashboard`, with the comment:

> *the project holds Cloud Run Jobs we did not create, and the reaper must never delete one
> of them*

Every sweeper in the codebase holds that line. There is exactly one deliberate exception, and
its cost is instructive: `ssm_endpoint_service.RECLAIM_ONLY_SERVICES` reclaims a
`secretsmanager` interface endpoint the **sandbox script** created, never the dashboard. To
do that safely it needs a live count of VPC-attached Lambdas, and on any error it leaves the
endpoint standing — paying about $7/month rather than risk breaking secret reads at runtime.
One resource type, one guard, and the guard costs a cloud call the sweep would not otherwise
make.

### Platform-level absences

Facts, listed because each is load-bearing for something above:

- **No scheduler.** Every recurring task is a hand-rolled `while True: await asyncio.sleep(…)`
  in `main.py` or `jobs_worker.py`, with the interval re-read each pass so Settings changes
  apply without a restart. `web_dashboard/tasks/` is a dead stub containing one docstring
  reading `"""Celery background tasks"""`. Nothing user-facing can be scheduled;
  `pov_schedule` is the only exception and it is POV-only.
- **No job retry and no dead-letter queue.** A failed job is terminal. (Notification
  *deliveries* do retry, with backoff.)
- **The rate limiter is inert.** `main.py:590` explains it: `default_limits` only takes effect
  through `SlowAPIMiddleware`, which is deliberately not added because a blanket per-address
  limit would break a UI that fires many calls per page load. Brute-force protection is
  `services/login_guard.py`, keyed on username.
- **No SMTP transport.** Email is delegated to a Power Automate flow behind the `custom`
  webhook format.
- **Test-connection exists seven times, ad hoc** — secrets backend, storage backend,
  hypervisor connection, OIDC discovery, Skytap, POV cloud, notification endpoint. No
  aggregate view, and none for PRA, Password Safe, Entitle, EPM-L, Portainer, the cloud
  credentials themselves, or OPA availability.
- **Partially built surfaces**, flagged so nobody reads them as finished: virtual desktops
  wire Azure only (*"AWS / GCP create seat records only"*); Certificate Lab is preview and
  *"none of its four submission paths has been proven against a live authority"*, with only
  `terraform/cert_ca/gcp_cas` built though AWS Private CA is a named path; Cloud Functions is
  preview.

## Recommendations

Four, ranked by value over cost. Three rules from [CONTRIBUTING.md](../../CONTRIBUTING.md)
§*What to avoid* bound all of them: no multi-tenant assumptions (*"those belong in the SaaS
codebase"*), nothing that only works with an enterprise integration enabled, and no new
dependency without a clear reason. All four work with zero BeyondTrust products configured
and add no packages.

### 1. Make the MCP server obey the permission model

> **Shipped.** Every tool now resolves the calling user and applies its HTTP twin's
> rule; `get_job`'s payload goes through an allowlist; `/mcp` sits behind
> `mcp_server_enabled`, default off, gated inside `_MCPAuth` because a mount takes
> no dependencies. The tool surface went from 7 to 19. `tests/test_mcp_rbac.py`
> pins the behaviour, including that a tool with no caller returns nothing —
> discovered by reflection, so a tool added later cannot skip the check. The
> paragraphs below are left as written; they are why the change happened.

**The finding:** `_mcp_user` is set and never read; the mount is ungated; `get_job` returns
raw `extra_data`.

**What to do**, in order, none of it large:

1. **Read the ContextVar.** Every tool resolves `_mcp_user` and applies the same scoping its
   HTTP twin already applies — `can_audit_jobs` for jobs, `_accessible_workgroups` for
   instances. The helpers exist; the tools just have to call them.
2. **Redact `extra_data`.** An allowlist of keys, not a denylist, because the deploy result
   grows as integrations are added and a denylist silently fails open on the next one.
3. **Gate the mount** behind a feature flag like every other router, default off.
4. **Then** add tools — inventory first, because it subsumes several per-cloud tools at once,
   followed by GCP, OCI, databases, Kubernetes, containers, functions, agents, costs, drift,
   secret staleness and expiry.

**Note the trap in step 4.** `services/inventory_service.py` says in its own docstring that
*"RBAC filtering is the API layer's job (see `visible_to`), not the collector's."* Exposing
the collector through MCP without calling `visible_to` reproduces the existing bug at larger
scale. The service is well factored; the MCP layer just has to be the API layer.

**What it reuses:** `can_audit_jobs`, `_accessible_workgroups`, `inventory_service.visible_to`,
the existing `_feature_gate` helper, and the PAT validation that already works.

**On writes: not yet, and say why.** A write tier is the obvious next thought and it does not
survive contact with the code. Admission control is not ambient — it is eleven inline calls,
all on deploys — so a write tool calling a service directly gets no policy gate at all, and
one re-entering an HTTP route gets one only if that route is among the eleven. Admission is
also off by default and per-action opt-in. Meanwhile a PAT carries no scope, and the real
permission model consults session-scoped Entitle JIT grants that a token has no way to hold.
And the roadmap's own reason for deferring three AI features — *"output runs with
privilege"*, needing a human-in-the-loop gate *"designed up front"* — applies to an LLM
issuing infrastructure actions at least as strongly. The gate it would need is the
asynchronous approval queue the roadmap already reserves. Fix the read tier; revisit writes
when that gate exists.

### 2. Govern the destroy seams with the engine that already ships

**The finding:** `destroy_instance` has no workgroup check, and admission control has never
seen a teardown.

**What to do:**

1. **Add the workgroup check** to `api/aws.py::destroy_instance` and its Azure, GCP and OCI
   counterparts, matching what deploy and reassign already do in the same files. This is a
   bug fix, not a feature.
2. **Add `admission_service.enforce()` at the destroy seams**, in the same position it
   occupies on the deploy paths — after params are resolved, before the job is created.
3. **Ship the Rego to match.** A change-freeze window on destroy is the obvious first rule,
   and `terraform/policy/admission/prod_window.rego` already exists to model it.

**Why this ranks so high:** the engine ships. It is OPA-backed, fails closed on evaluation
error, supports config-driven limits without anyone writing Rego, and its denials already
land in the hash-chained audit log. There are no new columns, no new sweep, no new write path
on a hot table, and no new cloud permission. It is the same shipped mechanism at a new seam.

**Roadmap boundary.** `saas-roadmap.md` reserves *"extends admission to the broader
multi-tenant action set and the async human approval gate"* for the hosted edition. That
clause is about the **multi-tenant** action set and about **asynchronous** approval. A
synchronous deny on a single-tenant estate destroy during a change-freeze window is
community-edition admission control doing exactly what it already does, one seam over.

### 3. Make the audit log readable, and close the chain

**The finding:** 75 write sites, one boolean read, no page, no export, nothing scheduled —
and `ip_address` sits outside the hash.

**What to do**, cheapest first:

1. **Populate `ip_address` and add it to `_canonical`.** Do this *first* and do it now. It is
   chain-breaking, so it recomputes every `entry_hash` — which costs nothing while the column
   is uniformly NULL and the table is young, and costs progressively more forever after. It
   is also the change that makes the rest of this worth building: an auditor-facing page whose
   "where did this come from" column sits outside the integrity guarantee is worse than no
   column.
2. **Scheduled verification**, emitting a new `audit.chain_broken` event. Make it
   **incremental** — verify forward from a persisted last-known-good `seq` — because
   `verify_audit_chain` currently materialises the whole table and the table can never be
   pruned. This is also the one condition that should *not* use `notify_scanner`'s day-bucket
   dedupe: a broken chain is not a state to report once a day and then forget.
3. **A list endpoint** with filters on actor, action, target and date range. Pagination is a
   copy of `api/jobs.py:44` — `page` / `page_size` with a `(rows, total)` service return.
4. **An `/audit` page**, admin-only, reusing the jobs page's filter furniture.
5. **Export**, CSV and JSON, streamed rather than assembled in memory.

**One decision to make explicitly:** `api/audit.py` is `require_admin` today, which sidesteps
the question of what a non-admin may see. `api/jobs.py` answers the same question with
`can_audit_jobs` and deliberately returns zeros rather than 404 from the batch summary so it
cannot be used as an existence oracle. Keeping the audit browse admin-only is a fine answer;
it should be a stated one.

**Roadmap boundary.** The reserved *centralised audit pane* aggregates feeds that mostly do
not exist yet — signed manifests, durable-workflow history, Terraform state-lock history —
and the roadmap says it lands after them. This is not that pane; it is making the one feed
that does exist legible. On *WORM/SIEM export*, I will argue part of it should come down:
CONTRIBUTING bars multi-tenant assumptions from this codebase anyway, so the per-tenant half
was never landing here, and what remains — a single-tenant instance shipping its own trail
somewhere durable — is community-shaped. The hash chain is what makes it worth shipping,
because the receiver can verify it. The narrow version is an audit-event notification
transport, which the existing HMAC-signed `custom` webhook format nearly is already.

### 4. Cloud VM power control, and what it would really cost

**The finding:** six power surfaces have `/power/*`; the four clouds have none; the estate
profile's only lifecycle lever is destructive.

This is the most *appealing* recommendation here and the most expensive, which is why it
ranks fourth rather than first. The appeal is real: `pov_schedule.due_action(row, now_utc)`
and `pov_spend.accrue(prev, at, rate, now)` are pure, clock-free and duck-typed on `row`, so
the policy genuinely ports. The cost is everywhere else.

**Phase 0 — the primitive.** `/power/start` and `/power/stop` on the four cloud routers using
the `_power_endpoint` shape, backed by new `*_power` job types, with the workgroup check
Recommendation 2 adds to destroy. `api/vms.py:257` is the best template, because it
carries the invariant a cloud power endpoint needs. `_workstation_workgroup` (`:233`) reads
the workgroup that authorizes an action from *the same table* the listing reads to decide
who may see the row, so — in its own words — *"what you can act on and what you can see
cannot disagree."* A power endpoint that resolved its workgroup any other way than the
list endpoint does is how you get buttons the page renders and the API refuses.

Note this is **not** a pure lift. `pov_cloud_aws._power_sync` (`:456`) takes no instance id —
it describes by environment tag and powers everything in the environment. There is no
per-VM power primitive in the POV code to reuse; Phase 0 writes one.

Phase 0 has standalone value and should be judged on its own. An operator who wants a VM off
overnight currently uses the cloud console, which puts the dashboard's inventory out of step
with reality.

**Phases 1 and 2 — schedules, then spend caps — have a prerequisite POV did not.** This is
the part to know before starting:

- **Estate Azure VMs get dynamic addresses; POV Azure VMs do not.** `azure_service.py:1246`
  is `private_ip_address_allocation="Dynamic"` and `:1235` is
  `public_ip_allocation_method="Dynamic"`. `pov_cloud_azure.py:381` and `:366` are both
  `"Static"`. `docs/profiles/pov/public-cloud.md:146` says why, in a sentence that reads like
  it was written to pre-empt this proposal: a deallocated VM with a dynamic private address
  can return on a different one, and by then the wire-up has written the old address into a
  PRA jump item, a Password Safe managed system and an Entitle integration — *"every
  scheduled suspend would silently invalidate all three."*
- **There is no repair path.** `terraform_pra_service` exposes `provision_jump` (`:433`) and
  `remove_jump` (`:459`) and no update — and the same provision/remove-only pair
  repeats for every other jump type in the module. A changed address can only be fixed by destroy-and-recreate,
  which mints a new Shell Jump and drops the association.
- **OCI is worse, and it is a security break rather than a usability one.**
  `oci_vm_service.py:119` prefers the **public** address. An OCI ephemeral public IP is
  released on stop, so after one suspend/resume cycle a PRA jump item can point at an address
  that now belongs to somebody else's instance.
- **Password Safe rotation runs on its own clock.** AWS onboarding defaults to the SSM
  method, whose `dns_name` is stop/start-stable — but `SendCommand` against a stopped
  instance fails. A nightly schedule produces a nightly rotation failure in the customer's
  Password Safe. POV never had to think about this because POV VMs are short-lived.
- **The spend cap's price source only half-ports.** `pov_cloud_cost.hourly_for_vm` is
  reusable, but `_priceable` gates AWS on a 13-entry hardcoded region-name map (`_LOCATIONS`,
  `:56`). An estate VM in a region outside that map gets no price, therefore no accrual,
  therefore a cap that silently never fires. On a curated POV region set that is fine; on an
  estate it is a cap that lies.

So the honest ordering is: fix addressing first (and migrate already-deployed VMs, whose NICs
would need reconfiguring), or scope Phase 1 to clouds where the address survives a stop, and
say which in the UI.

**Two structural notes.** First, `install_profile` is exclusive and *"the mask only ever
subtracts"* — every `pov_*` module is `_POV_ONLY`-masked on an estate instance. This is not a
port so much as **promoting a POV-owned module to profile-neutral**, which
`docs/profiles/README.md` treats as a deliberate category change; the plan needs to say which
modules move and who owns them afterwards. Second, a VM's deploy Job row *is* its inventory
record, so the schedule and accrual columns land on `jobs` — a table `_claim_one` polls every
two seconds. `expires_at` set the precedent with two columns written once at creation; spend
accrual writes two columns per VM per sweep, which is a different write profile on a hot
table, and it queues behind the audit advisory lock.

**On gating:** do not re-argue whether suspend needs the timer's four gates. `pov_spend`
already concluded it does not, in writing, because the action is reversible. Do port what it
did *not* skip: the default action is `warn` regardless, the NULL-latch on first evaluation
in both `due_action` and `accrue`, and `MAX_CATCHUP`. And inherit its known open gap
knowingly — the cap latches, so restarting a capped resource leaves it running past its cap.

### Also worth doing

- **Show what carries no `managed-by` tag.** `cost_service` already splits every cloud into
  `dashboard` / `sandbox` / `unattributed` and exposes `unattributed_total`, honestly
  reporting `None` where the tag filter structurally cannot reach. What is missing is the
  *list* behind the number. Ship it read-only and tag-scoped — no reclaim action — because a
  reclaim button reverses the reapers' stated guard, and because two of the waste shapes in
  [cloud-cost-guardrails.md](cloud-cost-guardrails.md) are, by that note's own finding,
  things a human built by hand. The note also treats endpoint deletion as a per-shape *proof
  obligation* rather than a heuristic, which is precisely what a generic sweep cannot encode
  — and is why that document is CLI-with-commentary rather than a feature.
- **Push budgets to the provider.** Create a real AWS Budget / Azure consumption budget / GCP
  billing budget from `cost_monthly_budget`, so the cloud alerts even when the dashboard is
  down. The audited GCP billing account had no budget at all and the API was not enabled.
- **Split gross from net on the cost tile.** One column, and it forecloses the specific wrong
  conclusion that opened the GCP audit — where net rose while gross usage fell 56%.
- **An aggregate integration preflight**, generalising the seven ad-hoc test-connection
  endpoints into one surface that probes what is actually reachable. The capability model
  exists in `pov_use_cases`, which answers *"can I run this here?"* from configuration rather
  than from a live probe.
- **A scheduler primitive**, if Recommendation 4 lands. Nine hand-rolled loops share a shape
  and no code. Extracting it is a refactor, not a dependency, and should stay that way.
- **Job retry with a dead-letter tail.** Sixty job types, no retry, and a class of failures —
  rate limits, capacity, a token that expired mid-run — that are transient by nature.
- **Finish what is started:** virtual-desktop AWS/GCP provisioning, and a
  `terraform/cert_ca/aws_pca` module to match the AWS Private CA path the certificate plugin
  already names.

## What this audit did not check

- **Runtime behaviour.** Nothing was executed. No container started, no test run, no cloud
  called. Every claim is from reading the tree at this commit.
- **Whether the tests assert what their names suggest.** 321 files were counted, not read.
- **Terraform module correctness.** The 39 files under `terraform/` were inventoried, not
  reviewed.
- **The security posture beyond the three authorization gaps named above.** Those surfaced
  while mapping features; this was not a security review, and the absence of further findings
  here is not evidence of their absence. `SECURITY.md` is the right channel for anything
  found deliberately.
- **The POV profile's own completeness.** POV is treated here as a source of primitives the
  estate lacks. Whether POV itself has gaps is a different audit.
- **Anything about the hosted edition** beyond what `saas-roadmap.md` claims; its status
  labels are taken at face value.

Line references go stale. Re-run the greps rather than trusting any number here — including
these.
