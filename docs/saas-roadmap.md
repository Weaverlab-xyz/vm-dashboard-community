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

- **Status** — `Built (prod)` (shipping in the prod deployment
  topology today), `Planned` (specified, not built), `In design`
  (sketched, not specified), or `Researching` (open question).
- **Dev-testable?** — `Yes` if the feature can be stood up and
  exercised on the existing docker-compose dev rig; `Partial` if it
  needs extra cloud topology a developer might not have; `No` if it
  needs real SaaS-hosting infrastructure.

Features marked `Built (prod)` exist in the single-tenant Azure-hosted
prod deployment; community users running the open-source edition don't
get them automatically because they depend on Azure infrastructure
(Key Vault, Arc, etc.) the community deployment doesn't assume.

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
- **What SaaS adds:** per-tenant **workload identity** — each
  Container Apps revision (or AKS pod) authenticates via an OIDC
  federated token, scoped to that tenant's Key Vault. No
  system-assigned identity is shared across tenants.
- **Status:** Planned. Builds directly on the prod bootstrap above.
- **Dev-testable?** Partial. Faithful to spec requires Container Apps
  or AKS; the OIDC exchange can be approximated locally with
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
- **Status:** Planned.
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
  image import, and reporting back. Promote-to-AWS and
  promote-to-Azure are shipping today; promote-to-GCP is the next
  prod increment.
- **What SaaS adds beyond prod:** multi-tenant Arc-worker scoping —
  each tenant's Arc machine runs runbooks issued only by that
  tenant's dashboard view; the `/images` surface shows per-tenant
  OVAs and per-tenant promotion history.
- **Status:** **Built (prod)** for on-prem OVA storage + promote to
  AWS + promote to Azure. **Planned (next prod increment)** for
  promote to GCP. **In design** for tenant scoping.
- **Dev-testable?** Partial. Promotion to AWS/Azure can be exercised
  in dev today (the cloud side is unchanged); the Arc-runbook
  dispatch leg needs a real Arc-enrolled worker registered against
  the dev tenant's Azure Automation account.

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

Two SaaS-shaped features ship in the prod deployment topology today:

- **Root-key bootstrap via managed identity + Key Vault.** The
  bootstrap loop is solved; remaining SaaS work on this axis is
  per-tenant scoping via workload identity (OIDC federation per
  Container Apps revision).
- **On-prem image promotion via Azure Arc** for AWS and Azure
  targets. Customers store OVAs locally on an Arc-enrolled machine;
  the cloud-hosted dashboard triggers Azure Automation runbooks
  there to promote the OVA to the chosen cloud provider. Promote-
  to-GCP is the named next prod increment; multi-tenant scoping is
  the SaaS-side increment after that.

Everything else here is paper specification, not running code. The
community edition is the only edition the open-source repository
ships; community users don't get the prod-topology features above
automatically because those depend on Azure infrastructure (Key
Vault, Automation, Arc) the community deployment doesn't assume.

When a feature flips to **Built (prod)**, this doc updates. When it
flips into the community open-source surface, the relevant lifecycle
doc gets the inline tease the way [image-management.md](image-management.md)
already teases SaaS cross-cloud promote.
