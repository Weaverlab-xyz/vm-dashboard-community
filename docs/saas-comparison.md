# Community vs. SaaS — what ships today

The community edition is the only shipping edition of the dashboard
right now. The hosted SaaS edition is a target architecture, not a
released product. This doc is narrowly about **the structural
difference that exists today**: where the JWT root key lives, and why
that pins community to a host-filesystem secret while SaaS will use a
managed-identity-backed vault.

For the full list of planned SaaS features (durable cross-cloud
workflows, multi-tenancy, AI-assisted generation, drift detection,
compliance-as-code, etc.) — and which ones can already be tested in a
dev environment — see [saas-roadmap.md](saas-roadmap.md).

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

## How the SaaS edition breaks the cycle (designed, not yet built)

The hosted edition's design replaces the on-disk root key with a
vault-backed one fetched at startup via **workload identity** — no
static credential is required to bootstrap:

- Each dashboard tenant runs as a workload (Azure Container Apps
  revision / AKS pod) with a system-assigned **Azure managed
  identity**.
- At process start, the dashboard exchanges its **OIDC federated
  token** for an Azure AD access token. There is no client secret
  on disk, in the image, or in the environment.
- The access token reads the root key from a tenant-scoped **Azure
  Key Vault**. The dashboard then derives the Fernet DEK exactly as
  in the community edition.
- Key Vault diagnostics provide the audit trail; rotation is a Key
  Vault operation that the next pod start picks up automatically.

Everything *above* the root key is identical to community: the same
encrypted database, the same `_SECRET_REGISTRY`, the same `/secrets`
migration UI for moving individual application credentials to AWS SM,
Azure KV, GCP SM, or BeyondTrust Secrets Safe.

## Side-by-side

| | Community (shipping) | SaaS (designed) |
|---|---|---|
| JWT root key location | Host filesystem / Docker secret | Azure Key Vault |
| How the dashboard authenticates to the key store | n/a (local file) | Managed identity + OIDC federation (no static credential) |
| Application secrets (cloud creds, integration tokens) | Encrypted DB → migratable to external vault | Same |
| Rotating the root key | Stop app, replace key file, restart, **re-enter all DB-encrypted values** | Rotate in Key Vault; next pod start picks up the new key |
| Audit trail for root-key access | Filesystem ACL only | Key Vault diagnostic logs |
| Static credentials on the host | JWT root key file | None |

## When to choose which

**Stay on community when:**
- You are running locally or on a single host you control.
- Filesystem-level secret protection is acceptable for your threat model.
- You want full control of the deployment topology.

**Move to SaaS when it ships, if:**
- Your security model requires the root key to live in a vault rather
  than on disk.
- You need audit logging for every root-key access.
- You want to avoid managing static credentials anywhere in the system.

There is no community-edition workaround that keeps the JWT root key
out of the host filesystem without breaking the bootstrap. If that
requirement is firm, the SaaS edition (or a custom deployment that
implements the managed-identity bootstrap design above) is the
supported path.

The broader feature roadmap — what else SaaS will offer beyond this
bootstrap problem — lives in [saas-roadmap.md](saas-roadmap.md).
