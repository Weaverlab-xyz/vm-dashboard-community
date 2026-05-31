# SaaS Roadmap

The hosted SaaS edition is a target architecture for the dashboard.
This doc is the consolidated list of features it will add on top of
the shipping [community edition](../README.md), with honest status
labels.

For the one piece of SaaS architecture that's already specified in
detail (the JWT root-key bootstrap problem and its managed-identity
solution), see [saas-comparison.md](saas-comparison.md).

> **Maintenance note (2026-05-30):** several items below have moved off
> `Planned`/`Researching` — the per-tenant isolation primitive
> (multi-tenancy), tenant-scoped runner networking, the Temporal durable
> promote, and the containerised Arc worker are all now in flight or
> dev-verified behind feature flags. Status labels and the "Today's
> reality" section reflect that. Per-feature design/execution plans are
> tracked in the (non-public) engineering planning workspace; this doc
> stays the high-level honest-status view.

---

## Status legend

Each feature carries two labels:

- **Status** — one of:
  - `Built (prod)` — shipping in the prod deployment topology today.
  - `Built (dev)` — implemented and dev-verified on the docker-compose
    rig, behind a feature flag; not yet through QA/prod cutover.
  - `In progress` — actively under construction; some phases shipped as
    scaffolding, more remain.
  - `Planned` — specified, not built.
  - `In design` — sketched, not specified.
  - `Researching` — open question; feasibility or value not yet settled.
- **Dev-testable?** — `Yes` if the feature can be stood up and
  exercised on the existing docker-compose dev rig; `Partial` if it
  needs extra cloud topology a developer might not have; `No` if it
  needs real SaaS-hosting infrastructure.

Features marked `Built (prod)` exist in the single-tenant Azure-hosted
prod deployment; community users running the open-source edition don't
get them automatically because they depend on Azure infrastructure
(Key Vault, Arc, etc.) the community deployment doesn't assume.
`Built (dev)` features live in the dev branch behind flags and are the
next things queued for QA.

---

## Security & identity

### Root-key bootstrap via managed identity + Key Vault

- **What community does:** stores the JWT root key on the host
  filesystem (or as a Docker secret).
- **What prod adds:** pulls the JWT root key (and all other
  dashboard secrets) from Azure Key Vault `assetmgmtdashboard` at
  startup via a managed identity on the Arc-enrolled prod host;
  exports each value to the container env without ever writing them
  to disk. Dev mirrors the same flow using the developer's `az
  login` session.
- **Status:** Built (prod). `Start-DevEnvironment.ps1` is the
  shipping reference; `JWT_SECRET_KEY` is stored as
  `dashboard-jwt-secret` in the vault.
- **Dev-testable?** Yes. Already exercised by the dev rig.

### Workload identity (OIDC federation) for SaaS multi-tenant

- **What prod does:** system-assigned managed identity on a single
  Arc-enrolled host. Single-tenant.
- **What SaaS adds:** per-tenant **workload identity** — each tenant
  authenticates to its own Key Vault via a federated OIDC token rather
  than a shared system-assigned identity.
- **Status:** In design.
- **Dev-testable?** Partial. The OIDC exchange can be approximated
  locally with `DefaultAzureCredential` against an `az login` session,
  which validates the code path but not the production hosting model.

> **Feasibility flag (2026-05-30):** an earlier draft specified this as
> per-tenant **Container Apps revisions / AKS pods**. That hosting model
> was rejected on cost; the direction is to stay on docker-compose, not
> migrate to managed containers. The *idea* (per-tenant federated
> identity scoped to per-tenant vaults) is sound but has to be re-scoped
> to the docker-compose topology before it gets an execution plan — the
> AKS/Container-Apps framing is explicitly out.

### Public webhook endpoint per tenant

- **What community does:** none — operators behind NAT must stand up
  their own reverse proxy (ngrok, Cloudflare Tunnel, etc.) for
  inbound webhooks (Entitle approvals, EPM-L event callbacks).
- **What SaaS adds:** a stable public HTTPS endpoint per tenant out
  of the box, scoped to that tenant's routes.
- **Status:** In design.
- **Dev-testable?** Partial. The endpoint logic itself works in dev
  via ngrok; the per-tenant scoping rides on multi-tenancy (now
  Built (dev), below).

### Approval / change-control gate for destructive automation

- **What community does:** destructive operations (decommission VM,
  `terraform destroy`, image/registry deletes, tenant hard-delete)
  execute immediately on the actor's authority. RBAC gates *who* can
  run them, but nothing requires a second approver.
- **What SaaS adds:** a configurable approval gate (two-person rule)
  on a defined set of high-blast-radius actions — the action enters a
  pending-approval queue, a second authorised user signs off, and only
  then does it execute, with the approval recorded in the audit trail.
  Distinct from JIT elevation (which *grants permission*); this gates
  *the act itself*.
- **Status:** In design. *(Backlog — sketched 2026-05-30; not yet
  specified.)*
- **Dev-testable?** Yes. Pure app-layer queue + approval state machine.

### Secret lifecycle — rotation, expiry, and scanning

- **What community does:** secrets are stored and resolved, but nothing
  rotates them, alerts on staleness, or scans the playbooks / Terraform
  / manifests the platform runs for embedded secrets.
- **What SaaS adds:** scheduled rotation for supported backends,
  expiry/staleness alerting on credentials, and secret-scanning of the
  artefacts the platform executes — the lifecycle half that "store
  secrets" leaves out.
- **Status:** In design. *(Backlog — sketched 2026-05-30; not yet
  specified.)*
- **Dev-testable?** Partial. Staleness detection + secret-scanning run
  locally; live rotation needs real backend credentials.

---

## Image lifecycle

> **Shipped in community (was on this list):** one-click cross-cloud
> promote and live cloud-side checks. The runner-driven flow
> documented in [Image Management](image-management.md) runs entirely
> in the community edition for AWS/Azure/GCP targets. SaaS now layers
> only the durable-replay guarantee on top — a 45-minute import that
> survives a dashboard restart mid-poll without orphan cloud-side
> tasks. Same registry, same `/images` UI, same audit trail.

### Durable cross-cloud promote (SaaS replay-safety)

- **What community does:** runner-driven promote (ECS / ACI / Cloud
  Run) drives the full conversion + import end-to-end. A dashboard
  restart while a long-running import is in flight leaves the
  cloud-side task running but the dashboard-side polling Job is gone
  — the operator has to reconcile via the cloud console.
- **What SaaS adds:** Temporal-backed workflows wrap both the runner
  task and the cloud-side `ec2.ImportImage` / `images.create_or_update` /
  `images.insert` poll. A dashboard restart resumes the workflow at
  the last activity boundary; orphan tasks become impossible.
- **Status:** In progress. A Temporal-in-docker dev rig + the
  cloud-agnostic base activities (preflight/registry/audit) have shipped
  as scaffolding. The load-bearing per-cloud import activities +
  restart-resume are next.
- **Dev-testable?** Yes. Temporal runs in docker-compose; the
  workflow + activities are plain Python wrapping the existing
  `promote_runner_service` + `image_registry_service.promote_to_*_automated`
  entry points.

### On-prem image promotion via Azure Arc

- **What community does:** customers manage cloud-side images only —
  the build → export → register flow runs cloud-native APIs; on-prem
  OVA/VHD artefacts have no first-class place in the lifecycle.
- **What prod adds:** customers can host image artefacts (OVAs) on
  an **Azure Arc-enrolled machine** they own. The cloud-hosted
  dashboard triggers an **Azure Automation runbook** dispatched to
  that Arc worker, which promotes the local OVA to the chosen cloud
  provider — uploading the artefact, kicking off the cloud-native
  image import, and reporting back.
- **What SaaS adds beyond prod:** multi-tenant Arc-worker scoping —
  each tenant's Arc machine runs runbooks issued only by that
  tenant's dashboard view; the `/images` surface shows per-tenant
  OVAs and per-tenant promotion history.
- **Status:** **Built (prod)** for on-prem OVA storage + promote to
  AWS + promote to Azure. Promote-to-GCP is now **dev-verified** (the
  GCP Export VHD endpoint + Cloud Run promote runner). Tenant scoping
  rides on multi-tenancy (Built (dev)).
- **Dev-testable?** Partial. Promotion to AWS/Azure/GCP can be
  exercised in dev today (the cloud side is unchanged); the Arc-runbook
  dispatch leg needs a real Arc-enrolled worker registered against
  the dev tenant's Azure Automation account.

### Containerised Arc-worker for zero-touch SaaS spokes

- **What community does:** none — community is single-host; there is
  no concept of a remote worker that performs cloud-side actions on
  the dashboard's behalf.
- **What prod adds today:** the Arc-worker lives on a customer-owned
  machine the customer enrols in Azure Arc, installing the Hybrid
  Worker extension + every runbook prereq (Az.* modules, AWS Tools for
  PowerShell, GCP SDK, qemu-img, etc.) by hand.
- **What SaaS adds beyond prod:** ship the Arc worker as a **single
  container image** the customer pulls and runs. The image bundles
  every runbook prereq pre-installed and pre-versioned; enrollment
  with the SaaS tenant happens via a short-lived registration token
  the dashboard mints, so the spoke comes online with a `docker run`
  and a paste-the-token step. This gives SaaS a true
  **hub-and-spoke** topology: the hub is the hosted dashboard; each
  customer runs N containerised spokes wherever their on-prem image
  artefacts live.
- **Status:** In progress. The dashboard side is being built — table +
  flag, a Hybrid Compute SDK wrapper, token mint + script templates,
  the `/workers` UI, and the reconciliation polling loop have shipped as
  scaffolding. Row actions + the dev validation gate remain, and the
  container-image packaging itself is still to come.
- **Dev-testable?** Partial. The dashboard onboarding flow + polling
  run on the dev rig with a stubbed Arc list; the real registration
  handshake + 1-hour token expiry need a QA Arc tenant.
- **Why it matters for SaaS:** removes the highest-friction step in
  Arc-worker onboarding — packaging the worker as pull-and-run
  shortens onboarding from days to minutes.

### Continuous CVE scanning per image version

- **What community does:** build job log is the only manifest;
  image hygiene is operator discipline.
- **What SaaS adds:** retains the Packer template, provisioner
  output, and component bill-of-materials per build; runs scheduled
  vulnerability checks against published CVE feeds; surfaces
  affected images in the dashboard.
- **Status:** In design.
- **Dev-testable?** Yes. The manifest store + scanner can run in
  docker-compose; CVE feed sources are public.

### AI-assisted image hardening

- **What community does:** none.
- **What SaaS adds:** suggestions like *"this image is missing CIS
  benchmark §5.2.3 — apply this provisioner snippet to your next
  rebuild"* generated against the stored bill-of-materials.
- **Status:** Researching.
- **Dev-testable?** Yes (call an LLM with a stored manifest).

> **Feasibility flag (2026-05-30):** deferred — no execution plan yet.
> Output quality depends entirely on manifest fidelity from the CVE-
> scanning feature above, which isn't built. Building hardening advice
> on top of a manifest store that doesn't exist would be speculative.
> Revisit once a real bill-of-materials ships.

### Multi-tenant image catalog

- **What community does:** single-tenant; one image registry per
  dashboard deployment.
- **What SaaS adds:** one catalog per tenant *and* a cross-tenant
  catalog for organisation-wide blessed base images.
- **Status:** Planned — now unblocked. The multi-tenancy primitive is
  Built (dev) and the image registry exists; what remains is scoping the
  registry per tenant + the org-scope catalog.
- **Dev-testable?** Yes (DB schema partitioning + UI scoping).

### Per-tenant signed build manifests

- **What community does:** the build provenance is the local job
  row; nothing is cryptographically signed.
- **What SaaS adds:** each build's manifest is signed with a
  tenant-scoped key (sigstore / KMS-backed). Artefacts are
  verifiable at promote time and at deploy time.
- **Status:** In design.
- **Dev-testable?** Yes (any KMS — cloud or self-hosted — can sign
  manifests in dev).

### Self-supply-chain for the platform's own privileged containers

- **What community does:** the ephemeral runner containers — and, in
  prod, the Arc-worker prereqs — run as-built. The platform signs
  *customer* image manifests (above) but does not sign, SBOM, or pin
  its *own* privileged execution images.
- **What SaaS adds:** sign + SBOM + pin the runner and Arc-worker
  container images (cosign / syft), and verify provenance before a
  privileged container runs. Matters because these containers hold
  cloud credentials in memory during a run.
- **Status:** In design. *(Backlog — sketched 2026-05-30; not yet
  specified.)* The containerised Arc-worker entry already flags
  signed-image distribution as an open question; this promotes it to
  scoped work.
- **Dev-testable?** Yes. cosign / syft run locally.

---

## Config management

### AI-assisted playbook generation

- **What community does:** auto-wrap (shell script → playbook).
- **What SaaS adds:** generator that reads the dashboard's asset
  schema *and* the tenant's live inventory; produces opinionated
  YAML tuned to the tenant's environment.
- **Status:** Researching.
- **Dev-testable?** Yes.

> **Feasibility flag (2026-05-30):** deferred — no execution plan yet.
> Buildable (it's an LLM call over the asset schema), but the output is
> a playbook that runs with privilege on real hosts. Auto-applying
> generated config is a genuine safety risk; this needs a human-in-the-
> loop review gate designed up front, and its value over the existing
> auto-wrap is unproven. Keep researching before committing a plan.

### Drift-aware runs

- **What community does:** job log is the audit trail; nothing
  watches the target between runs.
- **What SaaS adds:** stores the per-target hash of the last
  successfully applied playbook; surfaces *"state of host X
  unverified since 2026-04-12"*.
- **Status:** In design.
- **Dev-testable?** Yes.

### Tenant-scoped runner networking

- **What community does:** ephemeral runners share the host's
  Docker network.
- **What SaaS adds:** each run scoped to the tenant's network
  namespace; tenant A's run can't reach tenant B's targets even
  under a shared cloud account.
- **Status:** **Built (dev).** Shipped as a `tenant_network_service` +
  runner-spawn injection; the load-bearing cross-tenant ping test passes
  by design (100% packet loss A→B).
- **Dev-testable?** Yes (per-tenant Docker network).

### Tenant-scoped asset libraries

- **What community does:** single-tenant storage.
- **What SaaS adds:** each tenant gets its own storage namespace,
  inventory, and credential set without per-instance deployment.
- **Status:** Partial. The multi-tenancy service-scoping sweep already
  scopes config, secrets, and JIT per tenant; the per-tenant **storage
  namespace** for assets/images is the remaining piece and lands with
  the multi-tenant image catalog work.
- **Dev-testable?** Yes.

---

## Infrastructure as code

### Centralised Terraform state with locking

- **What community does:** state lives on the dashboard filesystem
  under `terraform/deployments/{job_id}/`. No locking — two
  operators running `apply` concurrently can corrupt state.
- **What SaaS adds:** remote backend (S3 + DynamoDB lock, or the
  Azure / GCP equivalent) serialising concurrent operations.
- **Status:** In design.
- **Dev-testable?** Yes. Drop in any remote backend; locking is
  a backend-config change, not a code rewrite.

### Continuous drift detection

- **What community does:** deployment view is frozen at apply time.
  Cloud-console changes are invisible until the next `apply`.
- **What SaaS adds:** scheduled reconciler that runs `terraform
  plan` against live cloud state and flags differences in the
  dashboard.
- **Status:** In design.
- **Dev-testable?** Yes.

### AI-assisted module refactoring

- **What community does:** none.
- **What SaaS adds:** suggestions like *"you have twelve almost-
  identical deploy modules; here's a single parameterised module
  that replaces them"* generated against the tenant's accumulated
  history.
- **Status:** Researching.
- **Dev-testable?** Yes.

> **Feasibility flag (2026-05-30):** deferred — no execution plan yet.
> Lowest-value of the three AI items and the riskiest: auto-refactoring
> Terraform modules touches infrastructure-defining code, and a wrong
> suggestion silently applied could destroy resources. Needs the IaC
> hardening work (state + drift) as a foundation and a strict
> human-in-the-loop. Keep researching.

### Compliance-as-code

- **What community does:** inventory only.
- **What SaaS adds:** continuous policy evaluation (OPA / Sentinel
  / cloud-provider policy) against deployed infrastructure;
  non-compliant resources flagged in the dashboard.
- **Status:** In design.
- **Dev-testable?** Yes (OPA runs locally).

### Action-level policy guardrails (pre-action admission control)

- **What community does:** nothing pre-empts an operation. RBAC gates
  who can act; compliance-as-code (above) evaluates infrastructure
  *after* apply.
- **What SaaS adds:** pre-action admission control over dashboard
  operations — allowed regions, allowed base images, instance-size
  caps, prod-window restrictions — evaluated by the same OPA engine as
  compliance-as-code but at the *pre-action* decision point, so a
  disallowed deploy never starts.
- **Status:** In design. *(Backlog — sketched 2026-05-30; not yet
  specified.)* Sibling of compliance-as-code; shares the OPA engine,
  differs in decision point (pre-action vs post-deploy).
- **Dev-testable?** Yes (OPA runs locally).

---

## Multi-tenancy & audit (cross-cutting)

Several features above (multi-tenant image catalog, tenant-scoped
asset libraries, tenant-scoped runner networking, per-tenant signed
manifests) depend on the same underlying primitive: **per-tenant
isolation across DB schemas, storage namespaces, credential stores,
and network scoping**.

### Per-tenant isolation primitive

- **Status:** **Built (dev).** This was the load-bearing change for
  most SaaS-distinct features, and it's now implemented and dev-gated:
  schema-per-tenant SQLAlchemy wiring, the service-layer scoping sweep,
  per-tenant Docker network isolation, auth + tenant memberships + a
  JWT tenant claim, and tenant CRUD admin endpoints all shipped. The
  dev validation suite (10 tests) **passed the QA gate 2026-05-30**.
  Next is QA cutover with a single tenant, then onboarding a real
  second tenant.
- **Dev-testable?** Yes. Exercised in dev with two synthetic tenants
  on the same docker-compose stack.

### Centralised audit pane

- **What community does:** each subsystem (jobs, audits, secrets)
  logs separately.
- **What SaaS adds:** single pane that aggregates Key Vault access
  logs (root key), Password Safe checkout entries (BeyondTrust),
  signed build manifests (image promotion), Temporal workflow
  history (promote jobs), and Terraform state-lock history
  (concurrent apply serialisation).
- **Status:** In design. Aggregates several feeds that are themselves
  not built yet (signed manifests, TF state-lock history), so it lands
  *after* those, not before.
- **Dev-testable?** Yes, once the underlying feeds exist.

### Cross-tenant catalog

- **What SaaS adds:** image-registry and asset-library entries can
  be promoted from tenant-scope to org-scope.
- **Status:** Planned — unblocked by the multi-tenancy primitive.
- **Dev-testable?** Yes.

### Tamper-evident audit trail

- **What community does:** each subsystem logs to its own store;
  records are mutable like any other DB row, and nothing guarantees the
  trail wasn't altered or truncated.
- **What SaaS adds:** an append-only, hash-chained audit log (and/or
  continuous export to a customer-owned WORM bucket / SIEM) so the
  trail is *evidence*, not just convenience. This is the integrity
  foundation the centralised audit pane sits on — the pane aggregates;
  this guarantees the records can be trusted.
- **Status:** In design. *(Backlog — sketched 2026-05-30; not yet
  specified.)*
- **Dev-testable?** Yes. Hash-chaining is local; external WORM / SIEM
  export needs a target.

### Compliance evidence reporting

- **What community does:** nothing — operators assemble audit evidence
  by hand.
- **What SaaS adds:** auto-generated, auditor-ready evidence per tenant
  (SOC 2 / CIS / change-history) built from the tamper-evident audit
  log + compliance-as-code results.
- **Status:** In design. *(Backlog — sketched 2026-05-30; not yet
  specified.)* Depends on the tamper-evident audit trail +
  compliance-as-code, so it lands after both.
- **Dev-testable?** Yes, once the underlying feeds exist.

---

## Today's reality

**Built (prod)** — two SaaS-shaped features ship in the prod
deployment topology today:

- **Root-key bootstrap via managed identity + Key Vault.** The
  bootstrap loop is solved; remaining SaaS work on this axis is
  per-tenant scoping via federated identity.
- **On-prem image promotion via Azure Arc** for AWS and Azure
  targets. Promote-to-GCP is now dev-verified and queued behind it.

**Built (dev)** — implemented behind flags on the docker-compose rig,
queued for QA:

- **Per-tenant isolation primitive** (multi-tenancy) — dev validation
  gate passed 2026-05-30; QA cutover is the next step.
- **Tenant-scoped runner networking** — per-tenant Docker network,
  cross-tenant traffic blocked by design.

**In progress** — actively under construction:

- **Durable cross-cloud promote** (Temporal) — dev rig + base
  activities scaffolded; per-cloud import + restart-resume next.
- **Containerised Arc-worker** — dashboard onboarding + polling
  scaffolded; container packaging + QA token-expiry test remain.

**In design** — CVE scanning + signed manifests, config drift-aware
runs, TF state-locking + drift detection + compliance-as-code, the
audit pane + cross-tenant catalog, and re-scoped tenant identity (OIDC)
+ per-tenant webhook.

**Researching (deferred — no plan yet)** — the three AI-assisted
features (image hardening, playbook generation, module refactoring).
Each is buildable but flagged above: value unproven and/or output runs
with privilege, so they wait behind their non-AI foundations.

**Backlog (In design — sketched 2026-05-30, not yet specified)** — six
governance/assurance items added to harden the "secure and auditable"
story: approval / change-control gate for destructive automation,
secret lifecycle (rotation + expiry + scanning), self-supply-chain for
the platform's own privileged containers, action-level policy
guardrails (pre-action admission control), tamper-evident audit trail,
and compliance evidence reporting. Accepted into scope but not yet
specified.

When a feature flips status, this doc updates. When it flips into the
community open-source surface, the relevant lifecycle doc gets the
inline tease the way [image-management.md](image-management.md) already
teases SaaS cross-cloud promote.
