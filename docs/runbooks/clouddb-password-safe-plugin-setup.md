# Cloud-DB Password Safe plugin setup + test (Azure Run Command / AWS SSM)

Operator runbook for standing up **database credential rotation** through the custom
Password Safe plugins: the three **`{engine} Azure Run Command Plugin`**s and the three
**`{engine} SSM Custom Plugin`**s. Covers what must exist before the settings panel is
useful, exactly what to put in each field, and how to prove the plugin ran.

Feature reference: [Databases → Layer 2](../databases.md#layer-2--password-safe-aws--azure).
Panel: **Settings → Integrations → Password Safe → Configure → Database Onboarding**.

**Scope:** AWS and Azure only. GCP and OCI databases provision and get a tunnel, but have
no Password Safe onboarding path at all.

---

## 0. You do not create the DB functional accounts by hand

This is the most common wrong turn, because the **VM** onboarding path works the opposite
way. Keep the two apart:

| Path | Functional account |
|---|---|
| **VM** onboarding (`passwordsafe_vm_functional_account_*`) | **You create it** in BeyondInsight and point config at it by name |
| **Database** onboarding (this runbook) | **The dashboard creates one per database**, at provision time, and deletes it on decommission |

For databases the dashboard calls `create_functional_account_on_platform` and packs your
configured credentials into it. What you supply in the panel is the *credential material*,
not the account:

| Cloud | Functional account username | Functional account password |
|---|---|---|
| **Azure** (`SP` mode) | `SP:<db-admin-login>` | `<clientId>:<clientSecret>:<adminPassword>` |
| **Azure** (`MSI` mode) | `MSI:<db-admin-login>` | `-:-:<adminPassword>` |
| **AWS** (IAM-user mode) | your IAM username | `<AccessKeyId>:<SecretAccessKey>` |
| **AWS** (EC2-role mode) | literal `EC2` | a random placeholder |

So the accounts you *do* have to create by hand are the **underlying cloud identities** —
an Azure service principal and an AWS IAM user — plus a PRA Configuration-API OAuth
client. Those are section 1.

Naming, so you can find them in BeyondInsight after a test run (`<id>` is the provisioning
job's `identifier`):

- functional accounts: `<id>-azure-fa` / `<id>-ssm-fa`, and `<id>-pravault-fa`
- managed systems: `<id>-db` and `<id>-pravault`
- the rotation target managed account: `psafe_<first 12 hex of the database id>`

---

## 1. Prerequisites, before the panel is worth opening

### 1.1 Upload the plugins

BeyondInsight → **Configuration → Privileged Access Management → Platform Plugins**. Upload
the `.PSPLUGIN` for each engine you intend to test, plus the shared PRA Vault plugin:

| Plugin | Platform name the dashboard expects by default |
|---|---|
| Azure PostgreSQL Run Command | `PostgreSQL Azure Run Command Plugin` |
| Azure MySQL Run Command | `MySQL Azure Run Command Plugin` |
| Azure SQL Server Run Command | `MSSQL Azure Run Command Plugin` |
| AWS psql SSM | `psql SSM Custom Plugin` |
| AWS mysql SSM | `mysql SSM Custom Plugin` |
| AWS mssql SSM | `mssql SSM Custom Plugin` |
| PRA Vault (shared) | `PRA Vault Username Password` |

The platform name is looked up by name at registration time. A mismatch fails the lookup
with the name quoted in the job error, so it is self-diagnosing — but it fails *after* the
database is already built.

### 1.2 Azure — the RSA key pair

The Azure plugin hands the new password to the jump VM **RSA-wrapped**. Generate an
RSA-4096 pair (`scripts/make-plugin-cert.sh` in the plugin repo — it is not in this repo),
then:

- copy **`public_cert.cer`** to **every** Password Safe Resource Broker, at the path you
  will enter as *Broker Cert Path*;
- paste **`private.pem`** and its passphrase into the panel. The dashboard base64-encodes
  both and drops them on the jump VM at `/root/psplugin/private.pem` and
  `/root/psplugin/passphrase.txt` (dir `700`, files `600`) over Run Command.

> **The single worst trap in this feature.** If *Plugin Private Key (PEM)* is blank, the
> key drop is **skipped silently** — `_azure_jump_prep_commands()` simply omits those
> commands. Jump-VM prep still reports success, the DB still provisions, the managed system
> still registers. You only find out at the first rotation, when the plugin cannot decrypt,
> and nothing in the provisioning job hints at the cause.

### 1.3 Azure — the service principal

Grant the SP behind the functional account **Virtual Machine Contributor** on the
**jump-VM resource group** (or just `Microsoft.Compute/virtualMachines/read` +
`.../runCommand/action`). Reusing the SP the dashboard already deploys Azure VMs with is
supported and is the default — leave *SP Client ID* / *Secret* blank to reuse
`azure_client_id` / `azure_client_secret`.

Also confirm `azure_subscription_id` and `azure_tenant_id` are set on the Azure panel:
they become **address fields 3 and 4**, and the plugin parses the address positionally, so
a blank one shifts nothing — it just yields an empty field the plugin cannot resolve.

### 1.4 AWS — the IAM user (IAM-user mode)

Because the plugin runs on a Password Safe node / Resource Broker that is **not** an EC2
instance, EC2-role mode is not available to you: leaving the IAM fields blank writes a
functional account whose username is the literal `EC2` and whose password is a throwaway
token, and rotation fails. Create an IAM user with:

- `ssm:SendCommand`
- `ssm:GetCommandInvocation`
- `ssm:ListCommandInvocations`

and an access key. Keep *Account Suffix* at `local` (it is only an AssumeRole ARN in the
cross-account EC2-broker mode).

### 1.5 AWS — the jump-host RSA prep is manual

Unlike Azure, **the dashboard does not stage the AWS key material.** Put `private.pem` and
`passphrase.txt` in the **`ssm-user` home** on the shared ECS gateway host yourself, and
make sure the DB client binary exists at the path the plugin invokes. The dashboard's own
managed-user creation uses a `docker run` client image and does *not* need any of this —
so a green provisioning job tells you nothing about whether the plugin's ongoing rotation
will work.

Set *Public Key Path (on PS node)* to where the matching public key lives on the PS
node/broker; it is **address field 5**.

### 1.6 Both clouds

- Run the current `scripts/sandbox/Linux/setup-aws.sh`, which attaches
  `AmazonSSMManagedInstanceCore` to `ecsInstanceRole` and grants the dashboard IAM user
  `ssm:SendCommand` / `GetCommandInvocation` / `ListCommandInvocations`.
- Create a **PRA Configuration-API** OAuth client with **Vault Account Management**, or
  leave `pra_config_api_client_id` / `_secret` blank to reuse `bt_client_id` /
  `bt_client_secret`.
- Give the **`pscli` API identity the Requestor role** (Smart Rule → Access Policy) over
  the new managed account. Databases need their **own** Smart Rule grant — a working VM
  grant does not extend to them, and the grant can take a couple of minutes to take
  effect. Without it, registration succeeds and the *checkout* fails later with `4031`/403.

---

## 2. Azure panel fill-in

**Settings → Integrations → Password Safe → Configure**, then the
**Azure (Run Command plugins)** subsection.

| Field | Value | Key |
|---|---|---|
| **Enable Password Safe DB onboarding** | on — drives **both** clouds | `clouddb_ps_onboarding_enabled` |
| PostgreSQL / MySQL / SQL Server Platform | leave **blank** (falls through to the defaults) | `clouddb_ps_platform_azure_*` |
| **Azure onboarding method** | `Run Command plugins` (the default) | `passwordsafe_azure_db_registration_method` |
| **Auth Mode** | `SP (service principal)` | `clouddb_ps_azure_auth_mode` |
| **Broker Cert Path** | where you copied `public_cert.cer`, e.g. `C:\BeyondTrust\certs\public_cert.cer` — **must match the broker's real path** | `clouddb_ps_azure_cert_path` |
| **Require TLS (sslTRUE)** | on — Azure flexible servers reject plaintext | `clouddb_ps_azure_ssl` |
| SP Client ID / Secret | blank to reuse the dashboard's Azure SP | `clouddb_ps_azure_sp_client_id` / `_client_secret` |
| **Plugin Private Key (PEM)** | the **full** `private.pem`, BEGIN/END lines included | `clouddb_ps_azure_plugin_private_key` |
| **Plugin Key Passphrase** | the passphrase for that key | `clouddb_ps_azure_plugin_passphrase` |

The client-image fields (`postgres:16`, `mysql:8.4`, `mcr.microsoft.com/mssql-tools18`) are
shared with AWS and only used by the dashboard's own managed-user creation. Leave them blank.

**Address the plugin receives** — eight `;`-separated fields:

```
vmName;resourceGroup;subscriptionId;tenantId;dbHost;dbName;certPath;sslTRUE
```

> **Leave *Azure onboarding method* on `Run Command plugins`.** Set to `Off` it skips Azure
> entirely and falls back to legacy admin-credential staging while the AWS SSM path stays
> on — it is the only way to disable one cloud, because the enable checkbox is shared by
> both. An unset key still reads as enabled at runtime, so an older install that never
> touched this control behaves the same.

---

## 3. AWS panel fill-in

Same panel, the fields above the Azure subsection.

| Field | Value | Key |
|---|---|---|
| PostgreSQL / MySQL / SQL Server Platform | leave **blank** (defaults) | `clouddb_ps_platform_*` |
| **PRA Vault Platform** | blank → `PRA Vault Username Password` | `clouddb_ps_pravault_platform` |
| **Workgroup** | blank → reuses `passwordsafe_workgroup` | `clouddb_ps_workgroup` |
| **IAM Username** | the IAM user from §1.4 — **required in your topology** | `clouddb_ps_ssm_iam_username` |
| **IAM Access Key ID** | `AKIA…` | `clouddb_ps_ssm_access_key_id` |
| **IAM Secret Access Key** | the secret (encrypted at rest) | `clouddb_ps_ssm_secret_access_key` |
| **Account Suffix** | `local` | `clouddb_ps_ssm_account_suffix` |
| **Public Key Path (on PS node)** | public-key path on the PS node/broker, e.g. `C:\Utils\public_ssm.pem` | `clouddb_ps_ssm_public_key_path` |

All three IAM fields are load-bearing together: the code only takes IAM-user mode when
username **and** key id **and** secret are all non-empty. Two out of three silently falls
back to EC2 mode.

**Address the plugin receives** — six `;`-separated fields:

```
instanceId;region;dbEndpoint;dbName;publicKeyPath;suffix
```

---

## 4. Test procedure

1. **Save the panel**, then confirm the values came back — reopen it. Config is encrypted
   with a key derived from `JWT_SECRET_KEY`, so a mismatched key reads back as blanks
   rather than as an error.
2. **Provision one small database** per engine you want to prove, on the cloud under test.
   Azure sends Run Commands to the shared `clouddb-jumpoint` VM (`Standard_B2s`); the DB
   clients get baked in on a fresh VM and ensured idempotently on a reused one.
3. **Read the provisioning job.** Look for these log lines — each is the checkpoint for one
   step: `managed DB user %r created`, `onboarded DB managed system … system_id=… account_id=…`,
   and either `onboarded PRA Vault managed system` or
   `no PRA Vault account … skipping PRA Vault onboarding`.
4. **In BeyondInsight**, confirm `<id>-db` exists with the right platform, and that its
   managed account is `psafe_<…>`.
5. **This is the actual plugin test:** run **Change Password** on that managed account, then
   **Verify Functional Account**. Everything before this point exercises *dashboard* code
   paths; only a credential change exercises your plugin.
6. **Prove the rotation propagated** by opening the PRA tunnel and connecting. If the
   vaulted credential did not move with the DB credential, you need the Password Safe
   SmartRule / linked-account configuration — the Terraform provider cannot express it, so
   it is out-of-band by design, not a bug.

---

## 5. Reading a failure

**Every onboarding step is best-effort.** A failure logs a warning, falls back to legacy
admin-credential staging, and **leaves the database up and the job green**. So "the deploy
worked" is not evidence the onboarding worked — always read the job log, not just its status.

| Symptom | Cause |
|---|---|
| Job green, no `onboarded DB managed system` line | onboarding was gated off — `clouddb_ps_onboarding_enabled`, or `pscli` not configured |
| Platform name quoted in the error | uploaded platform name ≠ the configured name |
| Rotation fails, decrypt error, provisioning was clean | *Plugin Private Key* was blank → no `/root/psplugin` on the jump VM (§1.2) |
| Rotation fails, functional account is `EC2` | two-of-three IAM fields — fell back to EC2 mode (§3) |
| Azure Run Command permission denied | SP lacks Virtual Machine Contributor on the **jump-VM** resource group |
| Address field 3 or 4 empty | `azure_subscription_id` / `azure_tenant_id` unset on the Azure panel |
| `4031` / 403 on checkout | `pscli` identity lacks Requestor on the **database** Smart Rule (§1.6) — also returned when the account is not API-enabled, or when the `SystemID` does not own the `AccountID` |
| Decommission left artifacts | managed systems are deregistered before their functional accounts are deleted; a partial failure logs each id it could not remove |
