# SaaS Roadmap

The hosted SaaS edition is a target architecture for the dashboard.
This doc is the consolidated list of features it will add on top of
the shipping [community edition](../README.md), with honest status
labels.

For the one piece of SaaS architecture that's already specified in
detail (the JWT root-key bootstrap problem and its managed-identity
solution), see [saas-comparison.md](saas-comparison.md). Everything
in *this* doc is either planned, in design, or under research.

---

## Status legend

Each feature carries two labels:

- **Status** — `Planned` (specified, not built), `In design` (sketched,
  not specified), or `Researching` (open question).
- **Dev-testable?** — `Yes` if the feature can be stood up and
  exercised on the existing docker-compose dev rig; `Partial` if it
  needs extra cloud topology a developer might not have; `No` if it
  needs real SaaS-hosting infrastructure (multi-region replay,
  Azure Container Apps with managed identity, an Arc-enrolled host,
  etc.).

Nothing in this doc is in the running code today beyond what the
community edition already ships. If you read a feature here and want
to know what *would* be involved in building it, the per-theme
sections link to the relevant planning notes.

---

## Security & identity

### Root-key bootstrap via managed identity + Key Vault

- **What community does:** stores the JWT root key on the host
  filesystem (or as a Docker secret).
- **What SaaS adds:** workload-identity-backed bootstrap. Each tenant
  pod uses a system-assigned managed identity + OIDC federation to
  read the root key from Azure Key Vault. No static credential
  exists on disk or in the image.
- **Status:** Planned. Design specified in [saas-comparison.md](saas-comparison.md).
- **Dev-testable?** Partial. Faithful to spec requires Azure Container
  Apps or AKS; the OIDC exchange can be approximated locally with
  `DefaultAzureCredential` against an `az login` session, which
  validates the code path but not the production hosting model.

### Public webhook endpoint per tenant

- **What community does:** none — operators behind NAT must stand up
  their own reverse proxy (ngrok, Cloudflare Tunnel, etc.) for
  inbound webhooks (Entitle approvals, EPM-L event callbacks).
- **What SaaS adds:** a stable public HTTPS endpoint per tenant out
  of the box, scoped to that tenant's routes.
- **Status:** Planned.
- **Dev-testable?** Partial. The endpoint logic itself works in dev
  via ngrok; the per-tenant scoping needs multi-tenancy (below) to
  be meaningful.

---

## Image lifecycle

### One-click cross-cloud promote (Steps 5–6)

- **What community does:** automates **build → export → register**
  (Phase 2 Steps 1–4) and returns operator-readable manual steps for
  the cross-storage copy + native VM-import.
- **What SaaS adds:** durable Temporal-backed workflows for both
  cross-storage copy (S3 ↔ Azure Blob ↔ GCS) and native VM-import
  (`aws ec2 import-image`, Azure `images` create, GCP
  `images.insert` with Daisy). Survives dashboard restart mid-poll
  without orphan cloud-side tasks.
- **Status:** Planned. Full plan in the private repo at
  `docs/image-promote-saas-plan.md`.
- **Dev-testable?** Yes. Temporal runs in docker-compose; the
  workflow + activities are plain Python. The plan's 8-test
  validation list explicitly includes kill-mid-poll recovery.

### Live cloud-side pre-flight

- **What community does:** pure-Python pre-flight (artefact recorded,
  format compat, cross-storage required, target creds configured).
- **What SaaS adds:** live cloud-side probes — `vmimport` role
  exists, VM-import quota available, source blob HEAD-reachable.
- **Status:** Planned.
- **Dev-testable?** Yes. The probes are direct cloud API calls;
  community already exercises the same credential paths for builds.

### On-prem image builds via Azure Arc

- **What community does:** runs Packer builds in the dashboard's
  local Docker, against cloud target accounts.
- **What SaaS adds:** registers an Azure Arc runbook worker on the
  operator's on-prem build host; Packer runs against VMware /
  Hyper-V / vSphere there, without sending build traffic through
  public internet egress.
- **Status:** In design.
- **Dev-testable?** No. Requires an Arc-enrolled host + a real
  Azure subscription with Arc runbook support.

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
- **Status:** Researching. Useful output quality depends on
  manifest fidelity from the CVE-scanning feature above.
- **Dev-testable?** Yes (call an LLM with a stored manifest).

### Multi-tenant image catalog

- **What community does:** single-tenant; one image registry per
  dashboard deployment.
- **What SaaS adds:** one catalog per tenant *and* a cross-tenant
  catalog for organisation-wide blessed base images.
- **Status:** Planned. Hinges on the multi-tenancy primitive
  (cross-cutting section below).
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

---

## Config management

### AI-assisted playbook generation

- **What community does:** auto-wrap (shell script → playbook).
- **What SaaS adds:** generator that reads the dashboard's asset
  schema *and* the tenant's live inventory; produces opinionated
  YAML tuned to the tenant's environment. *"Install Docker on my
  SUSE assets in AWS"* yields a playbook that already knows which
  assets those are.
- **Status:** In design.
- **Dev-testable?** Yes.

### Drift-aware runs

- **What community does:** job log is the audit trail; nothing
  watches the target between runs.
- **What SaaS adds:** stores the per-target hash of the last
  successfully applied playbook; surfaces *"state of host X
  unverified since 2026-04-12"*.
- **Status:** Planned.
- **Dev-testable?** Yes.

### Tenant-scoped runner networking

- **What community does:** ephemeral runners share the host's
  Docker network.
- **What SaaS adds:** each run scoped to the tenant's network
  namespace; tenant A's run can't reach tenant B's targets even
  under a shared cloud account.
- **Status:** Planned. Hinges on multi-tenancy.
- **Dev-testable?** Yes (per-tenant Docker network).

### Tenant-scoped asset libraries

- **What community does:** single-tenant storage.
- **What SaaS adds:** each tenant gets its own storage namespace,
  inventory, and credential set without per-instance deployment.
- **Status:** Planned. Hinges on multi-tenancy.
- **Dev-testable?** Yes.

---

## Infrastructure as code

### Centralised Terraform state with locking

- **What community does:** state lives on the dashboard filesystem
  under `terraform/deployments/{job_id}/`. No locking — two
  operators running `apply` concurrently can corrupt state.
- **What SaaS adds:** remote backend (S3 + DynamoDB lock, or the
  Azure / GCP equivalent) serialising concurrent operations.
- **Status:** Planned.
- **Dev-testable?** Yes. Drop in any remote backend; locking is
  a backend-config change, not a code rewrite.

### Continuous drift detection

- **What community does:** deployment view is frozen at apply time.
  Cloud-console changes are invisible until the next `apply`.
- **What SaaS adds:** scheduled reconciler that runs `terraform
  plan` against live cloud state and flags differences in the
  dashboard.
- **Status:** Planned.
- **Dev-testable?** Yes.

### AI-assisted module refactoring

- **What community does:** none.
- **What SaaS adds:** suggestions like *"you have twelve almost-
  identical deploy modules; here's a single parameterised module
  that replaces them"* generated against the tenant's accumulated
  history.
- **Status:** Researching.
- **Dev-testable?** Yes.

### Compliance-as-code

- **What community does:** inventory only.
- **What SaaS adds:** continuous policy evaluation (OPA / Sentinel
  / cloud-provider policy) against deployed infrastructure;
  non-compliant resources flagged in the dashboard.
- **Status:** In design.
- **Dev-testable?** Yes (OPA runs locally).

---

## Multi-tenancy & audit (cross-cutting)

Several features above (multi-tenant image catalog, tenant-scoped
asset libraries, tenant-scoped runner networking, per-tenant signed
manifests) depend on the same underlying primitive: **per-tenant
isolation across DB schemas, storage namespaces, credential stores,
and network scoping**.

### Per-tenant isolation primitive

- **Status:** Planned. This is the load-bearing change for most of
  the SaaS-distinct features; until it's built, those features are
  blocked.
- **Dev-testable?** Yes. Multi-tenancy can be exercised in dev with
  two synthetic tenants on the same docker-compose stack.

### Centralised audit pane

- **What community does:** each subsystem (jobs, audits, secrets)
  logs separately.
- **What SaaS adds:** single pane that aggregates Key Vault access
  logs (root key), Password Safe checkout entries (BeyondTrust),
  signed build manifests (image promotion), Temporal workflow
  history (promote jobs), and Terraform state-lock history
  (concurrent apply serialisation).
- **Status:** In design. Depends on the underlying features being
  built first (most of which are themselves not built).
- **Dev-testable?** Yes, once the underlying features exist.

### Cross-tenant catalog

- **Status:** Planned. Image-registry and asset-library entries can
  be promoted from tenant-scope to org-scope.
- **Dev-testable?** Yes.

---

## Today's reality

Nothing in this roadmap is in the running code today. The community
edition is the only shipping edition; what it does is documented in
the four lifecycle docs ([images](image-management.md),
[config](config-management.md), [storage](storage-management.md),
[infrastructure-as-code](infrastructure-as-code.md)) and exercised by
the test plan in each.

The closest thing to a SaaS prototype is the managed-identity
bootstrap design in [saas-comparison.md](saas-comparison.md) and the
cross-cloud promote plan referenced above. Both are paper
specifications, not running code. When a feature here flips to
**Built (dev)**, this doc will be updated; when it flips to **Built
(prod)**, the relevant lifecycle doc will inline-tease it the way
[image-management.md](image-management.md) already teases SaaS
cross-cloud promote.
