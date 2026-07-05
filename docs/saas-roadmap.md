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
> (multi-tenancy), tenant-scoped runner networking, the durable cross-cloud
> promote, and the containerised remote worker are all now in flight or
> dev-verified behind feature flags. Status labels and the "Today's
> reality" section reflect that. Per-feature design/execution plans are
> tracked in the (non-public) engineering planning workspace; this doc
> stays the high-level honest-status view.
>
> **Maintenance note (2026-07-05):** three corrections. **(1)** Four items
> that were SaaS-distinct have **shipped into the community edition**: the
> **tamper-evident (hash-chained) audit trail**, **action-level policy
> guardrails** (OPA pre-action admission), **config drift-aware runs**, and
> the staleness-alerting + artefact secret-scanning half of **secret
> lifecycle** — see [policy-guardrails.md](policy-guardrails.md),
> [config-management.md](config-management.md),
> [secrets-management.md](secrets-management.md). **(2)** The **per-tenant
> isolation primitive is now Built (prod)** — the hosted deployment is
> multi-tenant today; only the root-key *store* is still shared (per-tenant
> store scoping via federated workload identity remains). **(3)** This doc
> now describes hosted features by their **mechanics**, not the specific
> products the deployment happens to use (external managed secret store /
> platform identity / remote worker / durable-workflow engine).

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

Features marked `Built (prod)` exist in the cloud-hosted, **multi-tenant**
deployment; community users running the open-source edition don't get them
automatically because they depend on hosting infrastructure (an external
managed secret store, a remote automation worker, etc.) the community
deployment doesn't assume. This doc describes those features by their
**mechanics**, not by the specific products the hosted deployment happens
to use. `Built (dev)` features live in the dev branch behind flags and are
the next things queued for QA.

---

## Security & identity

### Root-key bootstrap via a managed secret store

- **What community does:** stores the JWT root key on the host
  filesystem (or as a mounted secret).
- **What the hosted topology adds:** pulls the JWT root key (and all
  other dashboard secrets) from an **external managed secret store** at
  startup, using a **platform-issued identity** (short-lived tokens, no
  on-disk credential); exports each value into the process environment
  without ever writing it to disk. Dev mirrors the same flow against a
  developer identity.
- **Status:** Built (prod). See
  [saas-comparison.md](saas-comparison.md) for the mechanics.
- **Dev-testable?** Yes. Already exercised by the dev rig.

### Per-tenant workload identity (federated) for the root-key store

- **What the hosted topology does today:** a single **shared** secret
  store reached via one platform identity, common to the deployment. The
  app/data layer is multi-tenant (below), but the root-key store is not
  yet scoped per tenant.
- **What SaaS adds:** per-tenant **workload identity** — each tenant
  authenticates to **its own** secret store via a federated token rather
  than a shared identity.
- **Status:** In design.
- **Dev-testable?** Partial. The federated-token exchange can be
  approximated locally, which validates the code path but not the hosting
  model.

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
  Built (prod), below).

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

> **Shipped in community (was fully on this list):** **staleness/expiry
> alerting** (`secret_max_age_days`, `GET /api/secrets/staleness`, the
> Needs-attention rollup — vault refs use the backend's own last-changed
> date) and **artefact secret-scanning** (uploaded playbooks / scripts are
> scanned on upload, `secret_scan_enabled`) both run in the community
> edition — see [secrets-management.md](secrets-management.md) and
> [config-management.md](config-management.md#secret-scanning-advisory).
> A rotation *primitive* also shipped: a Password Safe managed-account
> checkout used on a cloud run can be flagged **rotate-on-check-in**
> ([ansible.md](integrations/ansible.md#managed-account-checkout-beyondtrust-password-safe)).

- **What community does:** stores + resolves secrets, **alerts on
  staleness**, and **scans executed artefacts** for embedded secrets;
  rotates a managed-account credential on release (best-effort). What it
  lacks is *scheduled* rotation of stored backend secrets.
- **What SaaS adds:** **scheduled rotation** for supported backends — the
  one lifecycle piece still missing — on top of the staleness + scanning
  that now ship in community.
- **Status:** In design (scheduled rotation only; staleness + scanning
  shipped in community).
- **Dev-testable?** Partial. Live rotation needs real backend credentials.

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
- **What SaaS adds:** a **durable-workflow engine** wraps both the runner
  task and the cloud-side image-import poll (each provider's import API).
  A dashboard restart resumes the workflow at the last activity boundary;
  orphan tasks become impossible.
- **Status:** In progress. A durable-workflow-engine dev rig + the
  cloud-agnostic base activities (preflight/registry/audit) have shipped
  as scaffolding. The load-bearing per-cloud import activities +
  restart-resume are next.
- **Dev-testable?** Yes. The workflow engine runs in docker-compose; the
  workflow + activities are plain Python wrapping the existing
  `promote_runner_service` + `image_registry_service.promote_to_*_automated`
  entry points.

### On-prem image promotion via a remote worker

- **What community does:** customers manage cloud-side images only —
  the build → export → register flow runs cloud-native APIs; on-prem
  OVA/VHD artefacts have no first-class place in the lifecycle.
- **What the hosted topology adds:** customers can host image artefacts
  (OVAs) on a **remote worker they enrol on their own infrastructure**.
  The cloud-hosted dashboard dispatches an **automation job** to that
  worker, which promotes the local OVA to the chosen cloud provider —
  uploading the artefact, kicking off the cloud-native image import, and
  reporting back.
- **What SaaS adds beyond that:** per-tenant worker scoping — each
  tenant's worker runs only jobs issued by that tenant's view; the
  `/images` surface shows per-tenant OVAs and promotion history.
- **Status:** **Built (prod)** for on-prem OVA storage + promote to
  AWS + promote to Azure. Promote-to-GCP is now **dev-verified** (the
  GCP Export VHD endpoint + Cloud Run promote runner). Per-tenant scoping
  rides on multi-tenancy (now Built (prod)).
- **Dev-testable?** Partial. Promotion to AWS/Azure/GCP can be
  exercised in dev today (the cloud side is unchanged); the remote-worker
  dispatch leg needs a real enrolled worker registered against the dev
  tenant.

### Containerised remote worker for zero-touch SaaS spokes

- **What community does:** none — community is single-host; there is
  no concept of a remote worker that performs cloud-side actions on
  the dashboard's behalf.
- **What the hosted topology does today:** the remote worker lives on a
  customer-owned machine the customer enrols by hand, installing every
  job prerequisite (the cloud provider CLIs/SDKs, `qemu-img`, etc.)
  manually.
- **What SaaS adds beyond that:** ship the worker as a **single container
  image** the customer pulls and runs. The image bundles every
  prerequisite pre-installed and pre-versioned; enrollment with the tenant
  happens via a **short-lived registration token** the dashboard mints, so
  the spoke comes online with a `docker run` and a paste-the-token step.
  This gives a true **hub-and-spoke** topology: the hub is the hosted
  dashboard; each customer runs N containerised spokes wherever their
  on-prem image artefacts live.
- **Status:** In progress. The dashboard side is being built — table +
  flag, a remote-compute SDK wrapper, token mint + script templates,
  the `/workers` UI, and the reconciliation polling loop have shipped as
  scaffolding. Row actions + the dev validation gate remain, and the
  container-image packaging itself is still to come.
- **Dev-testable?** Partial. The dashboard onboarding flow + polling
  run on the dev rig with a stubbed worker list; the real registration
  handshake + short token expiry need a QA tenant.
- **Why it matters for SaaS:** removes the highest-friction step in
  worker onboarding — packaging it as pull-and-run shortens onboarding
  from days to minutes.

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
  Built (prod) and the image registry exists; what remains is scoping the
  registry per tenant + the org-scope catalog.
- **Dev-testable?** Yes (DB schema partitioning + UI scoping).

### Per-tenant signed build manifests

- **What community does:** the build provenance is the local job
  row; nothing is cryptographically signed.
- **What SaaS adds:** each build's manifest is signed with a
  tenant-scoped, KMS-backed key. Artefacts are verifiable at promote
  time and at deploy time.
- **Status:** In design.
- **Dev-testable?** Yes (any KMS — cloud or self-hosted — can sign
  manifests in dev).

### Self-supply-chain for the platform's own privileged containers

- **What community does:** the ephemeral runner containers — and, in the
  hosted topology, the remote-worker prereqs — run as-built. The platform
  signs *customer* image manifests (above) but does not sign, SBOM, or pin
  its *own* privileged execution images.
- **What SaaS adds:** sign + SBOM + pin the runner and remote-worker
  container images (container-signing + SBOM tooling), and verify
  provenance before a privileged container runs. Matters because these
  containers hold cloud credentials in memory during a run.
- **Status:** In design. *(Backlog — sketched 2026-05-30; not yet
  specified.)* The containerised remote-worker entry already flags
  signed-image distribution as an open question; this promotes it to
  scoped work.
- **Dev-testable?** Yes. Container-signing + SBOM tooling run locally.

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

> **Shipped in community (was on this list):** the config-drift signal
> runs in the community edition — each successful apply records a
> per-target fingerprint (`config_apply_state`), and `GET
> /api/config-mgmt/drift` surfaces **unverified** (no apply within
> `config_drift_stale_days`) and **changed** (stored playbook now differs
> from what was applied) targets. See
> [config-management.md](config-management.md).

- **What community does:** records the per-target content/inputs hash of
  the last successful apply and surfaces unverified/changed targets in the
  Ansible stream — *"state of host X unverified since 2026-04-12"*.
- **What SaaS adds:** the remaining active half — a *scheduled reconciler*
  that re-checks targets between runs rather than only recording at
  apply-time.
- **Status:** Shipped in community (passive/apply-time); SaaS adds a
  scheduled reconciler.
- **Dev-testable?** Yes.

### Tenant-scoped runner networking

- **What community does:** ephemeral runners share the host's
  Docker network.
- **What SaaS adds:** each run scoped to the tenant's network
  namespace; tenant A's run can't reach tenant B's targets even
  under a shared cloud account.
- **Status:** **Built (prod).** Shipped as a `tenant_network_service` +
  runner-spawn injection and live in the multi-tenant deployment; the
  load-bearing cross-tenant reachability test passes by design (100%
  packet loss A→B).
- **Dev-testable?** Yes (per-tenant network).

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
- **What SaaS adds:** a remote state backend with locking (an object
  store + a lock table, per cloud) serialising concurrent operations.
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

> **Shipped in community (was on this list):** pre-action admission
> control runs in the community edition — an OPA-backed engine
> (`admission_service`) evaluates a deploy request against Rego policies
> (allowed regions, instance-size caps, prod-window) at the pre-action
> decision point and **blocks before the job is created**, failing closed.
> Config-driven limits are settable without writing Rego, and denials land
> in the (hash-chained) audit log. Off by default
> (`admission_control_enabled`). See
> [policy-guardrails.md](policy-guardrails.md).

- **What community does:** OPA pre-action guardrails over deploy
  operations (allowed regions / instance-size caps / prod-window), gated
  by a feature flag, blocking before job creation.
- **What SaaS adds:** shares the OPA engine with **compliance-as-code**
  (post-deploy, still SaaS-only) and extends admission to the broader
  multi-tenant action set and the async human **approval gate** below.
- **Status:** Shipped in community (deploy guardrails); SaaS extends to
  post-deploy compliance + approval routing.
- **Dev-testable?** Yes (OPA runs locally).

---

## Multi-tenancy & audit (cross-cutting)

Several features above (multi-tenant image catalog, tenant-scoped
asset libraries, tenant-scoped runner networking, per-tenant signed
manifests) depend on the same underlying primitive: **per-tenant
isolation across DB schemas, storage namespaces, credential stores,
and network scoping**.

### Per-tenant isolation primitive

- **Status:** **Built (prod).** This was the load-bearing change for
  most SaaS-distinct features, and it is **now live in the hosted
  deployment**, which serves multiple tenants from one stack:
  schema-per-tenant persistence wiring, the service-layer scoping sweep,
  per-tenant network isolation, auth + tenant memberships + a tenant claim
  in the auth token, and tenant CRUD admin endpoints. The one part of the
  root-key axis not yet per-tenant is the shared secret store (see
  *Per-tenant workload identity* above).
- **Dev-testable?** Yes. Exercised in dev with multiple synthetic tenants
  on the same docker-compose stack.

### Centralised audit pane

- **What community does:** each subsystem (jobs, audits, secrets)
  logs separately.
- **What SaaS adds:** single pane that aggregates secret-store access
  logs (root key), Password Safe checkout entries (BeyondTrust),
  signed build manifests (image promotion), durable-workflow history
  (promote jobs), and Terraform state-lock history
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

> **Shipped in community (was on this list):** the append-only,
> **hash-chained audit log** runs in the community edition
> (`audit_chain` — each record links to the prior record's hash, so
> alteration or truncation is detectable). This is the integrity
> foundation the centralised audit pane sits on.

- **What community does:** hash-chained, tamper-evident audit log
  (detects alteration/truncation) via `audit_chain`.
- **What SaaS adds:** continuous **export to a customer-owned WORM bucket
  / SIEM** so the trail is externally durable evidence, not only locally
  verifiable — plus the multi-tenant scoping and the centralised audit
  pane that aggregates it.
- **Status:** Shipped in community (hash-chained log); SaaS adds WORM/SIEM
  export + the aggregating pane.
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

**Built (prod)** — shipping in the cloud-hosted, **multi-tenant**
deployment today:

- **Per-tenant isolation** (multi-tenancy) — one deployment serves
  multiple isolated tenants (data, storage, credentials, network).
- **Root-key bootstrap via a managed secret store.** The bootstrap loop
  is solved; the remaining work on this axis is per-tenant store scoping
  via a federated workload identity (the store is shared today).
- **On-prem image promotion via a remote worker** for AWS and Azure
  targets. Promote-to-GCP is now dev-verified and queued behind it.
- **Tenant-scoped runner networking** — per-tenant network, cross-tenant
  traffic blocked by design.

**In progress** — actively under construction:

- **Durable cross-cloud promote** (durable-workflow engine) — dev rig +
  base activities scaffolded; per-cloud import + restart-resume next.
- **Containerised remote worker** — dashboard onboarding + polling
  scaffolded; container packaging + QA token-expiry test remain.

**Shipped into community since the last update (2026-07-05)** — four
items left the SaaS-only column: the **tamper-evident (hash-chained)
audit trail**, **action-level policy guardrails** (OPA pre-action
admission), **config drift-aware runs**, and the **staleness-alerting +
artefact secret-scanning** half of secret lifecycle. Each keeps a
residual SaaS-only delta (WORM/SIEM export, post-deploy compliance,
scheduled reconciler, scheduled rotation) noted in its entry above.

**In design** — CVE scanning + signed manifests, TF state-locking + drift
detection + compliance-as-code, the audit pane + cross-tenant catalog, and
per-tenant federated identity for the root-key store + per-tenant webhook.

**Researching (deferred — no plan yet)** — the three AI-assisted
features (image hardening, playbook generation, module refactoring).
Each is buildable but flagged above: value unproven and/or output runs
with privilege, so they wait behind their non-AI foundations.

**Backlog (In design — sketched 2026-05-30, not yet specified)** — of the
six governance/assurance items added to harden the "secure and auditable"
story, **two shipped into community** (action-level policy guardrails,
tamper-evident audit trail) and **secret lifecycle partly shipped**
(staleness + scanning done; scheduled rotation remains). The residual
backlog is: approval / change-control gate for destructive automation,
scheduled secret rotation, self-supply-chain for the platform's own
privileged containers, and compliance evidence reporting. Accepted into
scope but not yet specified.

When a feature flips status, this doc updates. When it flips into the
community open-source surface, the relevant lifecycle doc gets the
inline tease the way [image-management.md](image-management.md) already
teases SaaS cross-cloud promote.
