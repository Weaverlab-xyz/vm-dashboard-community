# Moving secrets to an external vault

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are moving secrets out of the dashboard's database into AWS, Azure, GCP or BeyondTrust, and need each backend's permissions.

Part of [Secrets Management](../secrets-management.md).

The **Settings → Secrets Backend** page (`/secrets`) lets you migrate any
stored secret from the encrypted database to an external vault. After migration,
the database stores only a reference string (e.g. `aws_sm://dashboard/my-key`).
When the application needs the value, `config_service` resolves the reference
by calling the vault API, caches the result for 5 minutes, and discards it.

## Supported backends

| Backend | Reference prefix | Auth method |
|---|---|---|
| **AWS Secrets Manager** | `aws_sm://` | The dashboard's AWS credential: a Workload Credentials lease, the stored key, or its SPIFFE identity |
| **Azure Key Vault** | `azure_kv://` | The dashboard's Azure app: the stored secret or its SPIFFE identity |
| **GCP Secret Manager** | `gcp_sm://` | The dashboard's service account: the stored key or its SPIFFE identity |
| **BeyondTrust Secrets Safe** | `bt_safe://` | ps-cli credentials already configured in the dashboard |
| **BeyondTrust Workload Credentials** | `wlc://` | Site ID, plus either a personal access token or the container's own Azure workload identity (see below) |

Each backend reuses the cloud provider credentials you have already entered —
no additional IAM setup is needed beyond granting the existing SP / SA / IAM
user access to the vault. On a key-less install, where the dashboard reaches a
cloud with [its own SPIFFE identity](../../platform/oidc/dashboard-identity.md), grant the
federated role, app or service account instead. With no dashboard credential at all,
AWS Secrets Manager falls back to boto3's default chain (environment, instance role).

The OCI API signing key and its passphrase are on the list too. OCI has no backend
here, so they move to one of the vaults above, which keeps the PEM out of the
dashboard's database. Every OCI path (the console pages, Packer, Terraform, image
promotion) reads it through the same resolver.

## How to migrate a secret

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

**Bootstrap credentials are held back.** Each backend authenticates with one of
the dashboard's own secrets — the AWS secret key for Secrets Manager, the Azure
client secret for Key Vault, the service-account JSON for GCP SM, the ps-cli
client secret for Secrets Safe — and a migration that moved one into the backend
it unlocks would leave the dashboard unable to read its own credentials on the
next restart. Those appear in the results as skipped bootstrap credentials and
stay in the encrypted database. Workload Credentials is the exception: on
workload-identity auth it authenticates with no stored secret at all, so it holds
nothing back.

## Why the JWT root key cannot be migrated

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
> [SaaS comparison](../../editions/comparison.md) for details.

To rotate the JWT root key in the community edition: stop the application,
write a new key to the file/secret, restart. **All existing sessions are
invalidated and every DB-encrypted value must be re-entered** through the
setup wizard / Settings panels. Plan rotation accordingly.

## IAM permissions required per backend

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

Add `Microsoft.KeyVault/vaults/secrets/recover/action` if you use a custom role.
Soft delete is mandatory on every vault created since 2020 and cannot be turned
off, so deleting a secret does not free its name — the name stays reserved by a
deleted-but-recoverable object for the vault's retention window (7–90 days), and
writing it again fails with `Conflict … ObjectIsDeletedButRecoverable`. That
matters for the fixed-name secrets the dashboard stages for its adapters (for
example `portainer-adapter-pat`), where a retire followed by a re-pair reuses the
same name. The dashboard recovers such a secret and writes the new value over it;
without the `recover` action it can only tell you to run
`az keyvault secret recover --vault-name <vault> --name <secret>` yourself.
Key Vault Secrets Officer already includes the action.

**GCP Secret Manager:**
```
roles/secretmanager.secretAccessor
roles/secretmanager.secretVersionAdder
```
Scoped to the specific secrets, or `roles/secretmanager.admin` on the project.

**BeyondTrust Secrets Safe:**
The API Registration needs **Secrets → Read** and **Secrets → Write** permissions
on the folder where dashboard secrets will be stored.
