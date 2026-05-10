# Community vs. SaaS

The community edition is the same codebase as the hosted SaaS edition.
What you get with SaaS is the operational scaffolding around the core
dashboard: managed identity for secrets bootstrap, durable workflows
for long-running cross-cloud work, multi-tenancy, AI-assisted
generation, and audit trails that hold up to compliance review.

This doc is the canonical list. Other docs in the
[`docs/`](../docs/) tree link here rather than re-describing each
SaaS-side feature in place.

---

## What's the same

Community and SaaS share:

- The dashboard codebase, the per-cloud service modules, the
  Terraform-per-deploy state model, the Packer build orchestration,
  the storage backends (S3 / Azure Blob / GCS / Local-or-UNC), the
  FIDO2 + Azure-OAuth authentication, the encrypted secrets store,
  the migration flows to external vaults (AWS SM / Azure KV / GCP SM /
  BeyondTrust), and every per-cloud feature that's already shipped.

If a feature is in the community edition's `/api/...` surface, it's
in SaaS too. SaaS doesn't take features away.

---

## Security & identity

### Root key bootstrap (the original problem)

Community stores the JWT root key on the host filesystem (or as a
Docker secret) because every other secret in the database is encrypted
with a key derived from it. The root key itself cannot be migrated to
a vault: the dashboard would need a vault credential to fetch it, and
that credential would live in the same encrypted database the root key
unlocks. See [secrets-management.md → Why the JWT root key cannot be
migrated](secrets-management.md#why-the-jwt-root-key-cannot-be-migrated)
for the full loop.

SaaS breaks the cycle with **workload identity**:

- Each tenant runs as an Azure Container Apps revision (or AKS pod)
  with a system-assigned managed identity.
- At process start, the dashboard exchanges its OIDC federated token
  for an Azure AD access token. No static credential exists on disk,
  in the image, or in the environment.
- The access token reads the root key from a tenant-scoped Azure Key
  Vault. The dashboard derives the Fernet DEK exactly as in community.
- Key Vault diagnostics provide the audit trail; rotation is a Key
  Vault operation that the next pod start picks up automatically.

| | Community | SaaS |
|---|---|---|
| JWT root key location | Host filesystem / Docker secret | Azure Key Vault |
| How the dashboard authenticates to the key store | n/a (local file) | Managed identity + OIDC federation, no static credential |
| Rotating the root key | Stop app, replace key file, restart, re-enter all DB-encrypted values | Rotate in Key Vault, next pod start picks it up |
| Root-key access audit | Filesystem ACL only | Key Vault diagnostic logs per access |
| Static credentials on the host | Root key file | None |

Application secrets above the root key (cloud creds, integration
tokens) behave identically on both editions — same encrypted database,
same migration UI for moving individual credentials to AWS SM / Azure
KV / GCP SM / BeyondTrust Secrets Safe.

### Public webhook endpoint

Community deployments often sit behind NAT, which makes inbound
integrations (Entitle approval webhooks, EPM-L event callbacks, GitOps
push notifications) impractical without operator-supplied tunneling.
SaaS exposes a stable public HTTPS endpoint per tenant out of the box,
so third-party callbacks land without the operator standing up a
reverse proxy.

---

## Image lifecycle

The build-once-promote-many lifecycle ([image-management.md](image-management.md))
is shared. SaaS automates the cross-cloud half of the lifecycle that
community can't safely host on a single PostgreSQL container.

### Cross-cloud promote (Steps 5 & 6)

Community automates **build → export → register**. The Promote button
returns operator-readable manual steps for cross-storage copy and
native VM-import. SaaS runs both as **Temporal-backed durable
workflows**:

- Cross-storage copy (S3 ↔ Azure Blob ↔ GCS) issued server-side, with
  workflow checkpoints that survive worker restart mid-transfer.
- Native VM-import (`aws ec2 import-image`, Azure `images` create,
  GCP `images.insert` with Daisy conversion) polled by activity
  heartbeats. A 45-minute import survives dashboard restarts without
  orphan tasks.

Same `/images` UI, same registry, same audit trail — the `automated`
flag on the promote response flips to `true` and the manual-steps
pane is replaced with a live job stream.

### Live cloud-side pre-flight

Community's pre-flight checks read local state only (artefact
recorded, format compat, cross-storage required, target creds
configured). SaaS adds live probes — `vmimport` role exists, VM-import
quota available, source blob HEAD-reachable — that need replay-safe
state to be useful. Community surfaces those failures *after* the
import fails; SaaS surfaces them *before*.

### On-prem builds via Azure Arc

Community runs Packer builds in the dashboard's local Docker, against
cloud target accounts. SaaS can register an Azure Arc runbook worker
on your on-prem build host and run image builds *there*, against your
VMware / Hyper-V / vSphere hypervisor, without sending build traffic
through public internet egress. The resulting VHD is pushed to the
SaaS-tenant storage backend and promotes to cloud targets exactly the
same way as cloud-built images.

### Continuous CVE scanning per image version

Community's build job log is the manifest — image hygiene is the
operator's discipline. SaaS keeps every build's Packer template,
provisioner output, and component bill-of-materials, runs scheduled
vulnerability checks against published CVE feeds, and surfaces
affected images in the dashboard.

### AI-assisted image hardening

SaaS surfaces suggestions like *"this image is missing CIS benchmark
§5.2.3 — apply this provisioner snippet to your next rebuild"* against
your bill-of-materials. Community has no equivalent.

### Multi-tenant image catalog

Community is single-tenant: one image registry per dashboard
deployment. SaaS keeps one catalog per tenant *and* a cross-tenant
catalog for organisation-wide blessed base images — every team uses
the org's hardened Ubuntu without duplicating storage or rebuild
effort.

### Per-tenant signed build manifests

Community's build provenance is the local job row. SaaS signs each
build's manifest with a tenant-scoped key so the image artefact can
be verified against the manifest at promote time and at every deploy.

---

## Config management

The config-management lifecycle ([config-management.md](config-management.md))
shares ephemeral runners and the Ansible playbook layer.

### AI-assisted playbook generation

Community supports auto-wrap (shell script → playbook). SaaS adds a
generator that reads the dashboard's asset schema *and* the tenant's
live inventory, then produces opinionated YAML tuned to *your*
environment — not the generic best-effort wrap. "Install Docker on
all my SUSE assets in AWS" becomes a playbook that already knows
which assets those are.

### Drift-aware runs

Community's job log is the audit trail; nothing watches the target
between runs. SaaS stores the per-target hash of the last successfully
applied playbook and surfaces *"state of host X unverified since
2026-04-12"* without an operator polling manually.

### Tenant-scoped runner networking

Community ephemeral runners share the host's Docker network — fine
for single-tenant use, not for multi-tenant isolation. SaaS scopes
each run to the tenant's network namespace, so tenant A's run cannot
reach tenant B's targets even if they share an underlying cloud
account.

### Tenant-scoped asset libraries

Community storage is single-tenant. SaaS gives each tenant its own
storage namespace, inventory, and credential set without per-instance
deployment overhead.

---

## Infrastructure as code

The Terraform-per-deploy model ([infrastructure-as-code.md](infrastructure-as-code.md))
ships identically on both editions. SaaS adds the orchestration that
multi-operator and multi-tenant environments need.

### Centralised state with locking

Community Terraform state lives on the dashboard's filesystem under
`terraform/deployments/{job_id}/`. No locking — two operators running
`apply` concurrently against the same deployment can corrupt state.
SaaS uses a remote backend (S3 + DynamoDB locking, or the Azure /
GCP equivalent) so concurrent operations serialise safely.

### Continuous drift detection

Community's deployment view is frozen at apply time. Changes made
through the cloud console (or by other tools) are invisible until the
next `apply`. SaaS reconciles Terraform state against live cloud state
on a schedule and flags *"this instance type no longer matches the
module"* in the dashboard.

### AI-assisted module refactoring

SaaS surfaces opportunities like *"you have twelve almost-identical
deploy modules; here's a single parameterised module that replaces
them"* against the tenant's accumulated history. Community has no
equivalent.

### Compliance-as-code

Community has an inventory; SaaS continuously evaluates deployed
infrastructure against policy (OPA / Sentinel / cloud-provider
policy) and marks non-compliant resources in the dashboard. The
policy bundle is tenant-configurable.

---

## Multi-tenancy & audit (cross-cutting)

Several promises above hinge on the same underlying SaaS feature:

- **Per-tenant isolation** — separate database schemas, separate
  storage namespaces, separate credential stores, separate network
  scoping for ephemeral runners. The community edition is
  single-tenant by design and won't be retrofit.
- **Centralised audit** — Key Vault access logs (root key), Password
  Safe checkout entries (when BeyondTrust is wired in), signed build
  manifests (image promotion), Temporal workflow history (promote
  jobs), and Terraform state-lock history (concurrent apply
  serialisation) are all retained per tenant and exposed through one
  audit pane.
- **Cross-tenant catalog** — image registries and asset libraries
  can be promoted from tenant-scope to org-scope for blessed base
  images and golden playbooks.

---

## When to choose which

**Stay on community when:**

- You're running on a single host you control, behind your own
  network policy.
- Filesystem-level secret protection is acceptable for your threat
  model.
- You're single-tenant by nature (one team, one environment).
- You're willing to run cross-cloud promote manually for the rare
  case it comes up.
- You want full control of the deployment topology, including
  which dependencies upgrade and when.

**Move to SaaS when:**

- Your security model requires the JWT root key to live in a vault
  rather than on disk, with per-access audit logging.
- You need durable cross-cloud workflows that survive dashboard
  restarts (promote, scheduled drift reconciliation, long-running
  image imports).
- You need multi-tenant isolation between teams, environments, or
  customer accounts under one umbrella.
- Compliance review requires signed build manifests, OPA/Sentinel
  policy evaluation, or continuous CVE scanning.
- You want the AI-assisted layers (playbook generation, module
  refactoring, image hardening suggestions) that need tenant-scoped
  history to be useful.
- You want a public webhook endpoint without standing up your own
  reverse proxy.

There is no community-edition workaround for the JWT root key
bootstrap or the cross-cloud promote durability problem. If either
requirement is firm, SaaS is the supported path.
