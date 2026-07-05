# Community vs. hosted — what ships today

The dashboard codebase has two shipping deployment topologies:

- **Community (self-hosted)** — runs on a host you control, secrets in an
  on-disk `.env` or via the `/setup` wizard, JWT root key on the
  filesystem. This is the open-source path.
- **Hosted (multi-tenant)** — the dashboard runs cloud-hosted and serves
  **multiple tenants from one deployment**, each isolated at the data,
  storage, credential, and network layers. It runs the **same dashboard
  codebase** as community; what differs is the startup scaffolding that
  keeps secrets off disk and the per-tenant isolation layered on top.

This doc covers the one structural difference that changes the security
posture: the **root-key bootstrap**. For the broader feature roadmap
(durable cross-cloud workflows, drift detection, compliance-as-code, etc.)
and which items are built, planned, or researching, see
[saas-roadmap.md](saas-roadmap.md).

---

## The structural difference: JWT root-key bootstrap

The community edition stores the JWT root key on the host filesystem (or
mounted as a secret) because every other secret in the application
database is encrypted with a key derived from it. That introduces a hard
limit: **the JWT root key itself cannot live in the application's own
encrypted store**, because the dashboard would need a credential to fetch
it — and that credential would live in the same encrypted database the
root key unlocks.

See [secrets-management.md → Why the JWT root key cannot be migrated](secrets-management.md#why-the-jwt-root-key-cannot-be-migrated)
for the loop in detail.

## How the hosted topology breaks the cycle (shipping today)

The hosted deployment fetches every dashboard secret — including the JWT
root key — from an **external managed secret store** at startup, so no
static credential ever exists on the host. The mechanics, independent of
any particular provider:

- The deployment runs under a **platform-issued identity** authorized to
  read the secret store. The dashboard holds no long-lived credential for
  it — the platform hands it **short-lived tokens** instead.
- At startup the scaffolding authenticates with that identity, fetches the
  secret bundle, and exports each value into the **process environment**.
  Secrets are never written to disk.
- The dashboard reads the root key from the environment and derives the
  encryption key exactly as the community edition does.
- **Rotation** is a secret-store operation; the next process start picks
  up the new value.
- The secret store's **access logs** are the audit trail for every read.

Everything *above* the root key is identical to community: the same
encrypted database, the same secret registry, and the same `/secrets`
migration UI for moving individual application credentials to an external
vault (AWS SM, Azure KV, GCP SM, or BeyondTrust Secrets Safe).

## Multi-tenancy

The hosted topology is **multi-tenant today**: one deployment serves many
tenants, isolated at the data (schema-per-tenant), storage, credential,
and network layers, with tenant identity carried in the auth token.

One piece of the *root-key axis* is **not yet per-tenant**: the root key is
still fetched from a **single shared secret store under one platform
identity**, common to the deployment rather than scoped per tenant. Giving
each tenant its own store, reached via a **federated (per-tenant) workload
identity**, is the remaining enhancement on this axis — see
[saas-roadmap.md](saas-roadmap.md).

## Side-by-side

| | Community (self-hosted) | Hosted (multi-tenant) |
|---|---|---|
| JWT root key location | Host filesystem / mounted secret | External managed secret store, hydrated to env at startup |
| How the dashboard authenticates to the store | n/a (local file) | Platform-issued identity, short-lived tokens — no on-disk credential |
| Application secrets (cloud creds, integration tokens) | Encrypted DB → migratable to an external vault | Same, with store-hydrated env as primary |
| Rotating the root key | Stop app, replace key file, restart, **re-enter all DB-encrypted values** | Rotate in the store; next start picks it up |
| Audit trail for root-key access | Filesystem ACL only | Secret-store access logs |
| Static credentials on the host | JWT root key file | None |
| Tenancy | One deployment = one tenant | One deployment, many isolated tenants |
| Per-tenant root-key store | n/a | Shared store today; per-tenant scoping via federated workload identity is on the roadmap |

## When to choose which

**Stay on community (self-hosted) when:**
- You are running locally or on a single host you control.
- Filesystem-level secret protection is acceptable for your threat model.
- You want full control of the deployment topology and no external hosting
  dependency.

**Use the hosted topology when:**
- Your security model requires the JWT root key to live in a managed
  secret store rather than on disk.
- You want multi-tenant isolation from a single deployment.
- You'd rather not operate the secret-store bootstrap yourself.

There is no community-edition workaround that keeps the JWT root key out of
the host filesystem without breaking the bootstrap. If that requirement is
firm, the hosted topology — or a custom deployment following the same
managed-identity pattern — is the supported path today; per-tenant root-key
store scoping via federated workload identity is the remaining enhancement
(roadmap).
