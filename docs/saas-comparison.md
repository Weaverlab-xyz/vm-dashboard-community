# Community vs. SaaS — what ships today

The dashboard codebase has two shipping deployment topologies today:

- **Community** — runs on a host you control, secrets in an on-disk
  `.env` or via the `/setup` wizard, JWT root key on the filesystem.
  This is the open-source path.
- **Prod (single-tenant)** — dashboard runs **cloud-hosted** on Azure.
  Secrets, including the JWT root key, are pulled from Azure Key Vault
  at startup via the dashboard's **managed identity** — no static
  credential exists on disk or in the image. Same dashboard codebase
  as community; different startup scaffolding
  (`Start-DevEnvironment.ps1` mirrors the same flow via `az login`
  for dev). The prod topology also supports an **Azure Arc-enrolled
  on-prem worker** that hosts customer-owned image artefacts (OVAs)
  locally and runs Azure Automation runbooks to promote them to cloud
  providers — see the on-prem image promotion item in
  [saas-roadmap.md](saas-roadmap.md).

This doc covers the one structural difference that's **fully shipping
today**: the root-key bootstrap. For the broader feature roadmap
(multi-tenancy, durable cross-cloud workflows, AI-assisted generation,
drift detection, compliance-as-code, etc.) and which items are built,
planned, or researching, see [saas-roadmap.md](saas-roadmap.md).

---

## The structural difference: JWT root-key bootstrap

The community edition stores the JWT root key on the host filesystem
(or mounted as a Docker secret) because every other secret in the
application database is encrypted with a key derived from it. That
introduces a hard limit: **the JWT root key itself cannot be migrated
to an external vault**, because the dashboard would need a vault
credential to fetch it — and that credential would live in the same
encrypted database the JWT root key unlocks.

See [secrets-management.md → Why the JWT root key cannot be migrated](secrets-management.md#why-the-jwt-root-key-cannot-be-migrated)
for the loop in detail.

## How the prod topology breaks the cycle (built today)

The prod deployment scaffolding pulls every dashboard secret — including
the JWT root key (`JWT_SECRET_KEY` in env, stored as
`dashboard-jwt-secret` in the `assetmgmtdashboard` Key Vault) — from
Azure Key Vault at startup. No static credential exists on the host:

- The cloud-hosted dashboard runs with a managed identity that has
  `Key Vault Secrets User` on the vault.
- At startup, `Start-DevEnvironment.ps1` (or its prod equivalent)
  authenticates to Key Vault using the managed identity (prod) or the
  developer's `az login` session (dev), fetches the secret bundle, and
  exports each value into the container's environment.
- The dashboard process reads the root key from `JWT_SECRET_KEY` env
  and derives the Fernet DEK exactly as in the community edition.
- Secrets are never written to disk — the script is explicit about
  this. Rotation is a Key Vault operation; the next container start
  picks up the new values.
- Key Vault diagnostic logs provide the audit trail for every secret
  read.

Everything *above* the root key is identical to community: the same
encrypted database, the same `_SECRET_REGISTRY`, the same `/secrets`
migration UI for moving individual application credentials to AWS SM,
Azure KV, GCP SM, or BeyondTrust Secrets Safe.

The remaining SaaS-shaped work on top of this prod topology — workload
identity (OIDC federation) instead of a system-assigned managed
identity, and per-tenant Key Vault scoping — is documented in
[saas-roadmap.md](saas-roadmap.md). (An earlier draft assumed a move to
per-tenant Container Apps / AKS as the runtime; that hosting model was
**rejected on cost** — the direction is to stay on the docker-compose
topology. See the feasibility flag in the roadmap.) The bootstrap loop
itself is already solved.

## Side-by-side

| | Community (shipping) | Prod topology (shipping) | SaaS multi-tenant (planned) |
|---|---|---|---|
| JWT root key location | Host filesystem / Docker secret | Azure Key Vault, hydrated to env at startup | Per-tenant Azure Key Vault |
| How the dashboard authenticates to the key store | n/a (local file) | Arc-host managed identity (prod) / `az login` (dev) | Per-tenant workload identity + OIDC federation (docker-compose topology) |
| Application secrets (cloud creds, integration tokens) | Encrypted DB → migratable to external vault | Same, with KV-hydrated env as primary | Same, with per-tenant KV |
| Rotating the root key | Stop app, replace key file, restart, **re-enter all DB-encrypted values** | Rotate in Key Vault, next container start picks it up | Same, with per-tenant rotation |
| Audit trail for root-key access | Filesystem ACL only | Key Vault diagnostic logs (single-tenant scope) | Key Vault diagnostic logs (per-tenant scope) |
| Static credentials on the host | JWT root key file | None | None |

## When to choose which

**Stay on community when:**
- You are running locally or on a single host you control.
- Filesystem-level secret protection is acceptable for your threat model.
- You want full control of the deployment topology and no Azure dependency.

**Use the prod topology when:**
- Your security model requires the JWT root key to live in a vault, not on disk.
- You already have Azure infrastructure and a managed-identity story for the host.
- Single-tenant is fine (multi-tenancy is on the SaaS roadmap, not in prod today).

**Wait for SaaS multi-tenant when:**
- You need per-tenant Key Vault scoping, workload identity (federated OIDC),
  and the broader multi-tenancy primitives in [saas-roadmap.md](saas-roadmap.md).

There is no community-edition workaround that keeps the JWT root key
out of the host filesystem without breaking the bootstrap. If that
requirement is firm, the prod topology (or a custom Azure deployment
following the same managed-identity pattern) is the supported path
today; the multi-tenant SaaS edition extends it.
