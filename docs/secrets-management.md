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
| **JWT root key** | `.jwt_secret_key` | Host filesystem (owner-read-only) or external vault after migration |
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

### Migrating the JWT root key

The JWT key is the most sensitive secret because it protects all other
encrypted values. Migrating it to a vault breaks the dependency on the local
`.jwt_secret_key` file:

1. Ensure your chosen vault backend is configured and working.
2. Go to **Settings → Secrets Backend** → select your backend → choose
   **JWT Root Key** as the secret to migrate.
3. After a successful migration, delete `.jwt_secret_key` from the repo
   directory. The application reads it from the vault on every startup.

> **Before migrating the JWT key**, verify your vault credentials are correct
> and the vault is reachable from inside the container
> (`docker compose exec app curl <vault-endpoint>`). A failed migration rolls
> back cleanly, but a successful migration followed by vault unavailability
> will prevent the application from starting.

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

## Security best practices

**Do immediately after first run:**
- [ ] Migrate the JWT root key to your cloud vault (see above).
- [ ] Delete `.jwt_secret_key` from the host once migration is confirmed.
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
