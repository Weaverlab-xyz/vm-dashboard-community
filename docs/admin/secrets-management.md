# Secrets Management

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are deciding where to store cloud credentials, and how to evolve that over time.

Part of [Identity and access](../admin.md).

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
| **4 — Dynamic cloud credentials** | Nowhere — minted per lease by BeyondTrust Workload Credentials | Workload Credentials audit log | Removing the standing cloud credential entirely |

**The recommended destination is to store no cloud secret at all.** Every tier above
is about where a secret *lives*; the dashboard's own
[workload identity](../platform/workload-identity.md) removes the AWS, Azure and GCP keys outright, and
pairing it with Tier 4 or an Entitle grant for writes removes standing privilege as well.
Use this ladder for what still has to be stored (an OCI key, a Terraform provider's
credential, a hypervisor password), and for a cloud you have not moved over yet.

Every tier stays supported. Tier 1 is secure enough for a
single-user local deployment. Tier 2 is the right target for any shared or
persistent environment. Tier 3 adds checkout audit trails and is worth the added
complexity when compliance or separation-of-duties is a requirement. Tier 4 goes
further and removes the standing cloud credential altogether.

Each tier is selected **per cloud**, so a mixed deployment is normal rather than
a half-finished migration — AWS on Tier 4 alongside GCP on Tier 1 is a supported
configuration, not a gap. Nothing here changes on upgrade: a deployment stays on
whichever tier it is on until an operator moves it.

---

## What counts as a secret

| Category | Examples | Where stored |
|---|---|---|
| **Cloud provider credentials** | AWS access key + secret, Azure SP client secret, GCP service account JSON | Encrypted DB (Tier 1) or external vault (Tier 2+) |
| **Integration API tokens** | Portainer PAT, Entitle API token, BeyondTrust client secret | Encrypted DB (Tier 1) or external vault (Tier 2+) |
| **SSH private keys** | EC2 keypair, GCP SSH key | External vault only (AWS SM / GCP SM); never in the DB |
| **JWT root key** | `.jwt_secret_key` | Host filesystem (owner-read-only) or Docker secret — see [why this can't be migrated](secrets-management/external-vault.md#why-the-jwt-root-key-cannot-be-migrated) |
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

**One exception: Windows VM administrator passwords never use Tier 1.** They are
credentials for machines the dashboard built, not credentials the dashboard itself uses,
so a Windows build writes its administrator password to Password Safe, an external
backend or the cloud's own vault, and refuses to build when none is configured. The
**Windows VM administrator passwords** setting on the Secrets page picks the store
explicitly. See [Windows servers](../clouds/windows-servers.md).

---

## Pages

The larger parts have a page each:

| Page | Read it when |
|---|---|
| [Moving secrets to an external vault](secrets-management/external-vault.md) | you are moving secrets out of the dashboard's database into AWS, Azure, GCP or BeyondTrust, and need each backend's permissions |
| [Browsing and editing secrets](secrets-management/browse-and-edit.md) | you want to see, add, change or delete individual secrets from the Secrets page, and what each backend allows |
| [Using a secret in an Ansible run](secrets-management/ansible-runs.md) | a playbook needs a secret and nobody should have to read it to run the job |

---

## Vaults, and a workstream's own vault

Besides the one configured store, an administrator can register more vaults on **Secrets →
Backend → Vaults**: an id, a backend (Azure Key Vault, AWS Secrets Manager, GCP Secret
Manager or Secrets Safe) and an endpoint (the Key Vault URL, the AWS region or the GCP
project). A reference names one by putting its id first, like
`azure_kv://team-a-kv/pra-client`; a reference whose first segment is not a registered
vault reads from the configured store as before.

A vault can be **limited to one workstream**. Such a vault resolves only for that
workstream's cloud VM deploys and their **Add to PRA** actions — the PRA credential and the
Gateway deploy key. Resolved for anyone else, including a global Settings key, a
Config-Management secret variable or another workstream's deploy, it resolves to nothing,
the same as a secret that can't be read, and a warning is logged. So a team's own vault holds
that team's secrets and no one else's deploys can read them.

- A workstream's **bound PRA credential**
  ([Workstream secret bindings](../clouds/vms/pra.md#workstream-secret-bindings)) can name a
  reference into its own vault or an unlimited one; one into another workstream's vault is
  refused when it's saved. Type the reference in the editor's **or a reference** field, since
  the picker lists only the configured store.
- **Changing a vault's workstream** applies immediately; resolved values are not cached
  past it.
- **A vault still bound** as a workstream's PRA credential can't be deleted.
- A vault's **backend** can't change after it is registered, because references to it name
  that backend.

Managing vaults is administrator-only and audited (`secret_vault_update`,
`secret_vault_delete`). The API is `GET /api/secrets/vaults`, `PUT
/api/secrets/vaults/{id}` with `{"backend", "endpoint", "workgroup"}` (`null` for all
workstreams), and `DELETE /api/secrets/vaults/{id}`.

## Tier 3 — Vault-backed cloud credentials (BeyondTrust)

With the BeyondTrust integration enabled, the dashboard can retrieve AWS, Azure,
and SSH credentials directly from Password Safe at runtime rather than from the
application database. See [docs/beyondtrust/password-safe.md](../beyondtrust/password-safe.md)
for setup instructions.

**How it differs from Tier 2:**

| | Tier 2 | Tier 3 (BeyondTrust) |
|---|---|---|
| Where credentials live | External vault | BeyondTrust Password Safe |
| Checkout record | No | Yes — every retrieval creates a Password Safe audit entry |
| Rotation | Manual vault update | Rotate in Password Safe; dashboard gets new value immediately |
| SSH key | Stored as vault secret | Managed Account checkout (key never written to disk) |
| Requires BeyondTrust licence | No | Yes (Secrets Safe) |

### Tier 4 — dynamic cloud credentials (Workload Credentials)

Tier 3 still keeps a credential somewhere and fetches it when needed. **Tier 4
has nothing to fetch**: BeyondTrust Workload Credentials mints a short-lived AWS
or Azure credential on demand, and it expires on its own.

It is off by default. It is enabled under Settings → Integrations → BeyondTrust →
**Workload Credentials**, per cloud, and turning it on is
also what unlocks retiring that deployment's own static keys. Workload
Credentials additionally works as a Tier 2 backend for ordinary static secrets
(`wlc://`), which is the cheapest way to verify the connection before anything
dynamic is switched on.

One caveat, and the one way out of it. On the default auth mode the dashboard
still holds a Workload Credentials personal access token, so this collapses three
standing cloud credentials into one platform token rather than eliminating
secrets outright. On the **Azure workload-identity** auth mode it holds nothing:
the container mints a short-lived Entra token from its own managed identity, and
`wlc_pat` is read by no code path. That is the one posture in which migrating
*every* database secret — the PAT included — into `wlc://` leaves the database
with references only. The migration refuses to move the PAT on the token mode,
and reports it as a skipped bootstrap credential.

See [workload-lab/workload-credentials.md](../beyondtrust/workload-credentials.md).

### Hypervisor credentials for a remote agent

An agent-bound hypervisor connection can take its credential from any of the tiers above,
which is what lets an on-prem agent host store no hypervisor password at all. The
connection's `secret_ref` accepts the usual backend prefixes, plus one that is specific to
this path:

`ps_account://<managed-account-id>` — the dashboard checks a credential out of Password Safe
for the duration of one agent job, seals it to that agent, checks it back in when the job
ends, and **rotates the password on release** so the value the agent held is dead
immediately. Combined with `dashboard_secret: true` in the agent's own `connections.yaml`,
neither the agent host nor the application database holds a standing hypervisor credential.

Unlike the four backend prefixes, `ps_account://` is deliberately **not** resolvable through
`config_service.get()`. The others are stateless reads; this one opens a request that
something has to close, so it is handled only on the agent-credential path where the release
lifecycle exists. See
[Remote Agents → credentials](remote-agents/credentials.md#the-credential-the-dashboard-holds).

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
