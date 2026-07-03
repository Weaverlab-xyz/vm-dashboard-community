# Secrets Management

This document explains how the dashboard stores and protects credentials,
the security philosophy behind the design, and how to migrate secrets to
an external vault once you are ready.

---

## Philosophy

The dashboard operates on a **tiered secrets model**. Every tier is more
secure than the one before it. You start at Tier 1 on first run and can
advance at your own pace.

| Tier | Where secrets live | Who can read them | Best for |
|---|---|---|---|
| **1 — Encrypted database** | Application DB (AES-256) | Anyone with DB access + JWT key | Getting started; local dev |
| **2 — External vault (migrated)** | AWS SM / Azure KV / GCP SM / BT Secrets Safe | Vault IAM policy | Shared or long-lived deployments |
| **3 — Vault-backed cloud credentials** | External vault; fetched at runtime by BeyondTrust | Password Safe audit log | Regulated environments; zero-standing-access |

You do not have to reach Tier 3. Tier 1 is secure enough for a single-user
local deployment. Tier 2 is the right target for any shared or persistent
environment. Tier 3 adds checkout audit trails and is worth the added
complexity when compliance or separation-of-duties is a requirement.

---

## What counts as a secret

| Category | Examples | Where stored |
|---|---|---|
| **Cloud provider credentials** | AWS access key + secret, Azure SP client secret, GCP service account JSON | Encrypted DB (Tier 1) or external vault (Tier 2+) |
| **Integration API tokens** | Portainer PAT, Entitle API token, BeyondTrust client secret | Encrypted DB (Tier 1) or external vault (Tier 2+) |
| **SSH private keys** | EC2 keypair, GCP SSH key | External vault only (AWS SM / GCP SM); never in the DB |
| **JWT root key** | `.jwt_secret_key` | Host filesystem (owner-read-only) or Docker secret — see [why this can't be migrated](#why-the-jwt-root-key-cannot-be-migrated) |
| **Database password** | `POSTGRES_PASSWORD` | `.env` (non-secret bootstrap; not an application credential) |

The database password in `.env` is intentionally left there — it is only
reachable from within the Docker Compose network and protects the DB service
process, not application data. It is not a credential the application uses to
authenticate to external services.

---

## Tier 1 — Encrypted database (default)

All credentials entered through the setup wizard or **Settings → Integrations**
are encrypted with AES-256 before being written to PostgreSQL. The encryption
key is derived from the JWT root key (`.jwt_secret_key`).

**What this means in practice:**

- A copy of the database without the JWT key is useless — the ciphertext cannot
  be decrypted.
- The JWT key on disk is protected by owner-only filesystem permissions
  (`chmod 600` on Linux/macOS, Windows ACL on Windows).
- Credentials never appear in `.env`, in Docker environment variables, or in
  container inspect output.
- The setup wizard stores credentials immediately on submission — they are not
  held in browser memory or logged.

**Limitations of Tier 1:**

- The JWT key is a plaintext file on the host. Physical access to the machine
  or root access to the filesystem can expose it.
- There is no audit log of which application component read which credential,
  or when.
- Rotating a credential requires updating it in the dashboard Settings panel —
  there is no automated rotation.

---

## Tier 2 — External vault (migration)

The **Settings → Secrets Backend** page (`/secrets`) lets you migrate any
stored secret from the encrypted database to an external vault. After migration,
the database stores only a reference string (e.g. `aws_sm://dashboard/my-key`).
When the application needs the value, `config_service` resolves the reference
by calling the vault API, caches the result for 5 minutes, and discards it.

### Supported backends

| Backend | Reference prefix | Auth method |
|---|---|---|
| **AWS Secrets Manager** | `aws_sm://` | IAM credentials already configured in the dashboard |
| **Azure Key Vault** | `azure_kv://` | Service principal already configured in the dashboard |
| **GCP Secret Manager** | `gcp_sm://` | Service account already configured in the dashboard |
| **BeyondTrust Secrets Safe** | `bt_secrets_safe://` | ps-cli credentials already configured in the dashboard |

Each backend reuses the cloud provider credentials you have already entered —
no additional IAM setup is needed beyond granting the existing SP / SA / IAM
user access to the vault.

### How to migrate a secret

1. Log in as admin and navigate to **Settings → Secrets Backend** (`/secrets`).
2. Select the backend you want to migrate to.
3. Choose the secret to migrate and enter the target secret name in the vault.
4. Click **Migrate**. The dashboard:
   - Reads the current plaintext value from the encrypted DB.
   - Writes it to the vault under the name you specified.
   - Replaces the DB value with the reference string.
   - Verifies the reference resolves correctly before committing.
5. The plaintext value is no longer stored in the database. Future reads go
   to the vault.

### Why the JWT root key cannot be migrated

The JWT root key derives the Fernet key that encrypts every value in the
application database — including the cloud credentials the dashboard would
need to call any external vault. There is no startup ordering in the
community edition that resolves this:

```
read JWT root key
  └─ derive Fernet DEK
       └─ decrypt DB row
            └─ get vault credentials
                 └─ call vault to fetch JWT root key  ← the loop
```

The community edition therefore reads the JWT root key from one of these
sources only, in order:

1. The path in `JWT_SECRET_KEY_FILE` (set by `docker-compose.yml` to
   `/run/secrets/jwt_key` when the host file exists).
2. `/run/secrets/jwt_key` directly (Docker / Compose secret mount).
3. The `JWT_SECRET_KEY` environment variable.
4. A freshly generated random key — **only suitable for first-run/dev**;
   it does not survive a restart and invalidates all existing sessions
   and DB-encrypted values.

> The hosted SaaS edition removes this limitation by fetching the root key
> from Azure Key Vault using a workload-managed identity and OIDC federation
> — no static credential is required to bootstrap. See
> [SaaS comparison](saas-comparison.md) for details.

To rotate the JWT root key in the community edition: stop the application,
write a new key to the file/secret, restart. **All existing sessions are
invalidated and every DB-encrypted value must be re-entered** through the
setup wizard / Settings panels. Plan rotation accordingly.

### IAM permissions required per backend

**AWS Secrets Manager:**
```
secretsmanager:GetSecretValue
secretsmanager:CreateSecret
secretsmanager:PutSecretValue
secretsmanager:DescribeSecret
```
Scope to `arn:aws:secretsmanager:<region>:<account>:secret:dashboard/*`
(or your configured prefix).

**Azure Key Vault:**
The service principal needs the **Key Vault Secrets Officer** role on the vault,
or a custom role with `Microsoft.KeyVault/vaults/secrets/read` and
`Microsoft.KeyVault/vaults/secrets/write`.

**GCP Secret Manager:**
```
roles/secretmanager.secretAccessor
roles/secretmanager.secretVersionAdder
```
Scoped to the specific secrets, or `roles/secretmanager.admin` on the project.

**BeyondTrust Secrets Safe:**
The API Registration needs **Secrets → Read** and **Secrets → Write** permissions
on the folder where dashboard secrets will be stored.

---

## Browse & Edit — full CRUD on individual secrets

Migration covers the "move everything across" path. For day-to-day work
the `/secrets` page also provides full CRUD on individual secrets in any
configured backend — no need to drop into the AWS console, Azure Portal,
GCP console, or `ps-cli` for one-off edits.

The **Browse & Edit** section lets you:

- Pick any backend (Database, AWS SM, Azure KV, GCP SM, BT Secrets Safe).
- List every secret in that backend.
- Create a new secret, edit an existing one, or delete one.
- For BeyondTrust Secrets Safe specifically: navigate the full
  `Safe → Folder → Secret` hierarchy with create / rename / delete
  actions on Safes and create / delete on Folders, all driven by the
  ps-cli subcommands (`create-safe`, `update-safe`, `delete-safe`,
  `create`, `delete`). If a secret only sits one level deep, that
  folder acts as both the Safe and the Folder (per the BeyondInsight
  convention).

### JSON-only values

All secret values are constrained to **valid JSON** — same format across
every backend. The editor enforces this client-side (live parse) and
server-side (`validate_json_value` raises before the backend write). The
default scaffold for new secrets is:

```json
{
  "username": "",
  "password": ""
}
```

Add whatever fields your consumers expect — multi-line bodies (private
keys, certificates) work fine inside a JSON string. The **Format JSON**
button in the editor pretty-prints the current value to canonical
multi-line layout. This uniformity means downstream code can `json.loads`
the value from any backend without backend-specific parsing.

### What the dashboard can and cannot do per backend

| Operation | DB | AWS SM | Azure KV | GCP SM | BT Secrets Safe |
|---|---|---|---|---|---|
| List secrets | ✅ | ✅ | ✅ | ✅ | ✅ (per folder) |
| Read secret value | ✅ | ✅ | ✅ | ✅ | ✅ |
| Create / update secret | ✅ | ✅ | ✅ | ✅ | ✅ |
| Delete secret | ✅ | ✅ | ✅ | ✅ | ✅ |
| List Safes | — | — | — | — | ✅ |
| Create / rename / delete Safe | — | — | — | — | ✅ |
| List Folders | — | — | — | — | ✅ |
| Create / delete Folder | — | — | — | — | ✅ |

BeyondTrust hierarchy management is driven through the
[ps-cli subcommands](https://docs.beyondtrust.com/bips/docs/ps-cli-application):
`list-safes` / `create-safe` / `update-safe` / `delete-safe` for Safes,
and `list` / `create` / `delete` (parent identified via `-pid`) for
Folders. Deletes require the container to be empty — ps-cli refuses to
remove a Safe that still has Folders or a Folder that still has
Folders or Secrets, and the dashboard surfaces that error verbatim.

---

### Non-goals — what Browse & Edit deliberately does *not* include

The Browse & Edit feature is intentionally lightweight day-to-day CRUD.
It is **not** a replacement for the audit-and-compliance capabilities
of an enterprise vault (BeyondTrust Password Safe, CyberArk PAM,
HashiCorp Vault Enterprise, AWS Audit Manager + CloudTrail, etc.):

- **No enterprise audit trail.** The dashboard does not record who
  viewed, changed, or deleted a secret beyond the standard request log.
  If you need an immutable, queryable, regulator-grade audit trail —
  with session recording, four-eyes approval, time-bounded checkout,
  break-glass replay, etc. — keep using your vault's native UI / API
  for those workflows. The dashboard's Browse & Edit is for operator
  convenience, not compliance.
- **No plans to add it to the community edition.** Building a credible
  audit-and-compliance layer is a large undertaking and is not on the
  community roadmap. The community build exists to let small teams
  operate a multi-cloud workstation lab, not to replace their
  procurement decision for a PAM/secret-vault platform.
- **No plans on the SaaS roadmap either.** The hosted SaaS edition's
  differentiation (see [SaaS comparison](saas-comparison.md)) is
  managed hosting and operator UX — not vault feature parity.
  Customers who need an enterprise vault should keep using one and
  point the dashboard at it via the existing migration / reference
  flow above.

If you spot a per-secret operation in this page that should land in
your vault's audit log but doesn't, treat that as a sign you should
do that operation in the vault's native UI instead — not as a gap to
file against the dashboard.

---

## Tier 3 — Vault-backed cloud credentials (BeyondTrust)

With the BeyondTrust integration enabled, the dashboard can retrieve AWS, Azure,
and SSH credentials directly from Password Safe at runtime rather than from the
application database. See [docs/integrations/beyondtrust.md](integrations/beyondtrust.md)
for setup instructions.

**How it differs from Tier 2:**

| | Tier 2 | Tier 3 (BeyondTrust) |
|---|---|---|
| Where credentials live | External vault | BeyondTrust Password Safe |
| Checkout record | No | Yes — every retrieval creates a Password Safe audit entry |
| Rotation | Manual vault update | Rotate in Password Safe; dashboard gets new value immediately |
| SSH key | Stored as vault secret | Managed Account checkout (key never written to disk) |
| Requires BeyondTrust licence | No | Yes (Secrets Safe) |

---

## Secret staleness — age alerting

The dashboard can flag stored secrets that haven't changed in a while, so a
long-forgotten credential doesn't sit un-rotated indefinitely. It's **read-only**
— it never rotates or touches the secret; it only surfaces an age.

- **Turn it on** on the `/secrets` page: *"Flag secrets older than N days"*
  (`secret_max_age_days`; **0 disables**, the default).
- **What's tracked** — the config-secret registry (cloud credentials, integration
  tokens/passwords). A secret is flagged once its age reaches the threshold.
- **How age is measured** — this is the important part:
  - **Database-stored secrets** use the dashboard's own last-saved time
    (`AppConfig.updated_at`, stamped whenever you save the value).
  - **External-vault references** (`aws_sm://` / `azure_kv://` / `gcp_sm://` /
    `bt_safe://`) use **the vault's own last-changed / last-rotated date** — so a
    secret you rotate in AWS Secrets Manager or BeyondTrust Password Safe reads as
    *fresh*, not stale-since-you-pasted-the-reference. If a backend can't report a
    date, it falls back to when the reference was configured here.
- **Where it shows** — `GET /api/secrets/staleness` (admin) returns the per-secret
  ages; the dashboard's **Needs attention** panel rolls up *"N secrets not rotated
  in X+ days."*

This is deliberately the safe half of secret lifecycle — staleness signalling
only. Automated rotation is a hosted-edition concern (see the *SaaS roadmap*).

---

## Security best practices

**Do immediately after first run:**
- [ ] Verify the JWT root key file (`.jwt_secret_key` or
  `/run/secrets/jwt_key`) has owner-only read permissions.
- [ ] Back up the JWT root key to a secure offline location — losing it
  renders the entire encrypted database unrecoverable.
- [ ] Change the auto-generated admin password (`Settings → Security → Change Password`).

**For any shared or long-lived deployment:**
- [ ] Migrate all cloud provider credentials (AWS, Azure, GCP) to your vault.
- [ ] Restrict vault IAM policies to the minimum permissions listed above.
- [ ] Enable MFA for the dashboard admin account (`Settings → Security → Security Keys`).
- [ ] Set `POSTGRES_PASSWORD` in `.env` to a strong unique value (the onboard
  script auto-generates one — do not replace it with something weaker).
- [ ] Do not expose the PostgreSQL port (5432) outside the Docker Compose
  network. The `docker-compose.yml` publishes it for convenience; remove
  the `ports:` block under `db:` for any internet-facing deployment.

**If BeyondTrust is available:**
- [ ] Configure Password Safe as the credential source for AWS and Azure.
- [ ] Store SSH private keys as Managed Accounts rather than Secrets, so
  checkout requires an active session and creates a full audit record.

---

## What is never stored as a secret

- The **database password** (`POSTGRES_PASSWORD`) — in `.env` only; only
  reachable inside the Compose network.
- **Feature flags** (`VMWARE_ENABLED`, etc.) — configuration, not credentials.
- **Public cloud region and zone settings** — not sensitive.
- **Webhook URLs** — not sensitive; only the signing secret that validates
  inbound webhooks is treated as a credential.
