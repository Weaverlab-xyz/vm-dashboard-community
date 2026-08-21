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

## 0. Decide where the functional account comes from

`clouddb_ps_functional_account_mode` — **Functional account source** at the top of the
panel — decides this, and it changes what half the panel means. Set it before anything else.

| Mode | Who owns the functional account | What you put in the panel |
|---|---|---|
| **`reference`** — *Reference an existing account* | **You do.** Create it in BeyondInsight and name it here. The dashboard resolves it by name, never creates it, and **never deletes it on decommission**. | the account **name** |
| `create` — *Create one per database* (legacy default) | **The dashboard.** One account per database at provision time, deleted on decommission. | the **credential material** it packs into that account |

`reference` is the same contract the **VM** onboarding
(`passwordsafe_vm_functional_account_*`) and the **k8s** token rotation
(`k8s_ps_functional_account_*`) have always used, and it keeps the static cloud credential —
the IAM access key, the Azure client secret — out of the dashboard's config store entirely.

Three consequences worth knowing before you pick it:

- **One account per engine per cloud.** A functional account belongs to a platform, and each
  engine is its own plugin platform. Postgres-only on one cloud is two accounts (the DB
  plugin and PRA Vault); all three engines on both clouds is seven.
- **The managed system inherits its platform from the functional account**, exactly as on the
  VM path. So the *Platform* fields become advisory — they are not even looked up, which also
  means a stale platform name cannot block onboarding.
- **A blank name is an error**, not a quiet fall-through to `create`. Deliberate: this feature
  already has two silent fallbacks too many (§3 and §1.2).

### What `create` mode packs into the account it mints

Only relevant if you stay on the legacy default. The dashboard calls
`create_functional_account_on_platform` per database with:

| Cloud | Functional account username | Functional account password |
|---|---|---|
| **Azure** (`SP` mode) | `SP:<db-admin-login>` | `<clientId>:<clientSecret>:<adminPassword>` |
| **Azure** (`MSI` mode) | `MSI:<db-admin-login>` | `-:-:<adminPassword>` |
| **AWS** (IAM-user mode) | your IAM username | `<AccessKeyId>:<SecretAccessKey>` |
| **AWS** (EC2-role mode) | literal `EC2` | a random placeholder |

Note the Azure rows embed the **per-database** minted admin password. That is why a single
pre-created Azure account only works with self-rotation — see the next section.

### Self-rotation is the other half of `reference` mode

The Azure Run Command plugins (v1.0.0.1) expose **two** change actions, and Password Safe
picks between them from **Change Password Using Own Credentials** on the *managed account*:

| Action | What the plugin does | What the functional account needs on the DB |
|---|---|---|
| Change Managed Account (**using self**) | `ALTER USER` on itself / `ALTER USER CURRENT_USER()` / `ALTER LOGIN … OLD_PASSWORD` | **nothing** — a login that can authenticate, for *Verify Functional Account* only |
| Change Managed Account (using Functional Account) | `ALTER USER`/`ALTER LOGIN` on the target | `CREATEROLE` (PG ≤ 15) or admin on the role (PG 16+), `CREATE USER` (MySQL), `ALTER ANY LOGIN` (SQL Server) |

**A dashboard-provisioned server has no such privileged login**, so `reference` mode requires
the self-rotate action. Turn on **Rotate with the account's own credentials**
(`clouddb_ps_self_rotation`) — it emits `use_own_credentials = true` on the managed account.
Leave it **off** in `create` mode, where the functional account *is* the minted DB admin and
therefore already privileged.

Two consequences that are easy to miss:

- **`SP:` mode needs nothing broker-side; `MSI:` mode does.** All three plugins resolve the
  Azure control-plane credential from the first available of three tiers:

  | Tier | Used when | Where the identity lives |
  |---|---|---|
  | 1 | FA name is `SP:<dbUser>` | the functional account's own service principal |
  | 2 | FA name is `MSI:…` **and** `ControlPlane:ClientId` is set on the broker | `AppSettings:Azure{Postgres,MySql,Mssql}:ControlPlane` — `ClientSecret`, or a PFX via `CertificatePath` (preferred) |
  | 3 | FA name is `MSI:…` and nothing is configured | `DefaultAzureCredential` — an ambient managed identity |

  So with `SP:` functional accounts an **off-Azure broker needs no extra setup**. Tier 2 is
  the escape hatch for `MSI:` mode on a broker with no managed identity; it is broker-side
  config the dashboard cannot set. Prefer the certificate, or set the secret as a machine
  environment variable (`AppSettings__AzureMssql__ControlPlane__ClientSecret`) — the shipped
  `appsettings.json` lives **inside the `.psplugin`**, so a secret in the file is baked into
  the package.

  **Half-configured is a silent downgrade:** `ControlPlane:ClientId` with neither a secret nor
  a certificate logs a warning and falls through to `DefaultAzureCredential` rather than
  failing — the same shape as the two-of-three IAM trap in §3.

- **The functional account is required for every action, self-rotation included.** The plugin
  parses its name and **all three** password segments before it builds the Azure credential,
  so `MSI:` mode still needs the `-:-:<dbPassword>` form with a **non-empty** DB password.
  Whichever identity the plugin ends up resolving is the one that needs **Virtual Machine
  Contributor** on the jump VM.
- **The functional account's DB login must still exist on each managed server.** Its password
  segment is unused for a self-rotate change, but it is still *validated* as non-empty, and
  *Verify Functional Account* logs in with it. The dashboard creates only the managed user
  `psafe_<…>`, so Verify FA will fail per managed system unless you create that login
  (e.g. `psfa`) on each server yourself.

Self-rotation also needs Password Safe to hold the account's **correct current password**.
It does — the dashboard seeds it at onboarding — but if it ever drifts, the self-login fails
and you fix it out of band or temporarily turn this off.

### Naming, so you can find things in BeyondInsight after a test run

`<id>` is the provisioning job's `identifier`:

- managed systems: `<id>-db` and `<id>-pravault` (both modes)
- the rotation target managed account: `psafe_<first 12 hex of the database id>` (both modes)
- functional accounts, **`create` mode only**: `<id>-azure-fa` / `<id>-ssm-fa`, and
  `<id>-pravault-fa`. In `reference` mode nothing new appears — that is the point.

Either way, the accounts you create by hand include the **underlying cloud identities** (an
Azure service principal or an AWS IAM user) plus a PRA Configuration-API OAuth client. Those
are section 1.

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

In `reference` mode the SP's client id and secret go **into the functional account you
create**, not into the panel — the *SP Client ID* / *Secret* fields are not read at all.
Either way the identity needs the same permission.

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
instance, EC2-role mode is not available to you. In `reference` mode the access key goes
**into the functional account you create** and the panel's IAM fields are not read; in
`create` mode leaving them blank writes a functional account whose username is the literal
`EC2` and whose password is a throwaway token, and rotation fails. Either way, create an IAM
user with:

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
| **Functional account source** | `Reference an existing account` (recommended) or `Create one per database` — see §0 | `clouddb_ps_functional_account_mode` |
| **Rotate with the account's own credentials** | **on** with `reference` mode; off with `create` — see §0 | `clouddb_ps_self_rotation` |
| PostgreSQL / MySQL / SQL Server Platform | leave **blank** (falls through to the defaults; advisory in `reference` mode) | `clouddb_ps_platform_azure_*` |
| **PostgreSQL / MySQL / SQL Server Functional Account** | `reference` mode only — the account you created on each Run Command platform | `clouddb_ps_functional_account_azure_*` |
| **Azure onboarding method** | `Run Command plugins` (the default) | `passwordsafe_azure_db_registration_method` |
| **Auth Mode** | `create` mode only — `SP (service principal)`. Not read in `reference` mode | `clouddb_ps_azure_auth_mode` |
| **Broker Cert Path** | where you copied `public_cert.cer`, e.g. `C:\BeyondTrust\certs\public_cert.cer` — **must match the broker's real path** | `clouddb_ps_azure_cert_path` |
| **Require TLS (sslTRUE)** | on — Azure flexible servers reject plaintext | `clouddb_ps_azure_ssl` |
| SP Client ID / Secret | `create` mode only — blank to reuse the dashboard's Azure SP. In `reference` mode leave both **blank**; the credential lives in your functional account | `clouddb_ps_azure_sp_client_id` / `_client_secret` |
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
| PostgreSQL / MySQL / SQL Server Platform | leave **blank** (defaults; advisory in `reference` mode) | `clouddb_ps_platform_*` |
| **PostgreSQL / MySQL / SQL Server Functional Account** | `reference` mode only — the account you created on each SSM platform | `clouddb_ps_functional_account_*` |
| **PRA Vault Platform** | blank → `PRA Vault Username Password` | `clouddb_ps_pravault_platform` |
| **PRA Vault Functional Account** | `reference` mode only — your PRA Config-API account on the PRA Vault platform | `clouddb_ps_pravault_functional_account` |
| **Workgroup** | blank → reuses `passwordsafe_workgroup` | `clouddb_ps_workgroup` |
| **IAM Username** | `create` mode only — the IAM user from §1.4, **required in your topology**. In `reference` mode leave **blank** | `clouddb_ps_ssm_iam_username` |
| **IAM Access Key ID** | `create` mode only — `AKIA…` | `clouddb_ps_ssm_access_key_id` |
| **IAM Secret Access Key** | `create` mode only — the secret (encrypted at rest) | `clouddb_ps_ssm_secret_access_key` |
| **Account Suffix** | `local` | `clouddb_ps_ssm_account_suffix` |
| **Public Key Path (on PS node)** | public-key path on the PS node/broker, e.g. `C:\Utils\public_ssm.pem` | `clouddb_ps_ssm_public_key_path` |

In `create` mode all three IAM fields are load-bearing together: the code only takes
IAM-user mode when username **and** key id **and** secret are all non-empty. Two out of
three silently falls back to EC2 mode. `reference` mode sidesteps the whole trap — the
credential is in the functional account and these three are never read.

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
3. **The plugin writes its own log on the Resource Broker** — `Logs\log-Azure<Engine>RunCommand-<date>.txt`
   beside the plugin assembly, rolling daily, last 3 files. Start there for any plugin-side
   failure; the dashboard's job log only covers what the dashboard did.
4. **Read the provisioning job.** Look for these log lines — each is the checkpoint for one
   step: `managed DB user %r created`, `onboarded DB managed system … system_id=… account_id=…`,
   and either `onboarded PRA Vault managed system` or
   `no PRA Vault account … skipping PRA Vault onboarding`.
4. **In BeyondInsight**, confirm `<id>-db` exists with the right platform, and that its
   managed account is `psafe_<…>`. In `reference` mode also confirm **no `<id>-ssm-fa` /
   `<id>-azure-fa` was created** — if one was, the mode did not take effect.
5. **This is the actual plugin test:** run **Change Password** on that managed account, then
   **Verify Functional Account**. Everything before this point exercises *dashboard* code
   paths; only a credential change exercises your plugin.
6. In `reference` mode, **decommission the database and confirm your functional account
   still exists.** Teardown deletes only accounts the dashboard minted, and this is the one
   check that proves it.
7. **Prove the rotation propagated** by opening the PRA tunnel and connecting. If the
   vaulted credential did not move with the DB credential, you need the Password Safe
   SmartRule / linked-account configuration — the Terraform provider cannot express it, so
   it is out-of-band by design, not a bug.

---

## 5. Reading a failure

**Every onboarding step is best-effort.** A failure leaves the database up and the job
**green**. Note the two different fallbacks: if the *managed-user creation* fails the run
falls back to legacy admin-credential staging, but if *managed-system onboarding* fails there
is no fallback at all — you get no Password Safe artifacts and a green job. That branch now
appends `Password Safe onboarding skipped (non-fatal): …` to the job log, so the job page
carries the reason. Either way, "the deploy worked" is not evidence the onboarding worked —
always read the job log, not just its status.

| Symptom | Cause |
|---|---|
| Job green, no `onboarded DB managed system` line | onboarding was gated off — `clouddb_ps_onboarding_enabled`, or `pscli` not configured |
| Platform name quoted in the error | uploaded platform name ≠ the configured name |
| Rotation fails, decrypt error, provisioning was clean | *Plugin Private Key* was blank → no `/root/psplugin` on the jump VM (§1.2) |
| Rotation fails, functional account is `EC2` | two-of-three IAM fields in `create` mode — fell back to EC2 mode (§3) |
| `PSPLUGIN_CHANGE_FAILED: … must have admin option on role` / `CREATE USER denied` / `ALTER ANY LOGIN` | self-rotation is **off**, so Password Safe called the via-functional-account action against a server with no privileged login — turn on `clouddb_ps_self_rotation` (§0) |
| `PSPLUGIN_CHANGE_FAILED: … password authentication failed for user psafe_…` (self-rotation) | Password Safe's stored current password drifted from the server |
| `CredentialUnavailableException` / `DefaultAzureCredential failed to retrieve a token` | an `MSI:` functional account reached tier 3 on a broker with no managed identity — switch the FA to `SP:`, assign an MI, or configure `ControlPlane` (§0) |
| Log warns `ControlPlane:ClientId is configured but neither ClientSecret nor CertificatePath is set` | tier 2 half-configured, silently fell through to tier 3 (§0) |
| `Control-plane certificate not found at '…'` / `CryptographicException` | `ControlPlane:CertificatePath` is wrong on **that** broker, or the password does not match — it is read on the broker, not the jump VM |
| Managed-system address rejected or truncated | Password Safe's address column is **255 characters**; the eight-field Azure address gets close with two GUIDs, a flexible-server FQDN and a long cert path. The dashboard now refuses over-long addresses up front and names the number |
| `Verify Functional Account` fails on every new managed system | the functional account's DB login does not exist on the provisioned server (§0) |
| `Run Command in progress (409)` after retries | every database shares `clouddb-jumpoint` and Azure allows one action-style Run Command per VM; the plugin retries 5 times at 15s (6 attempts) — stagger rotations or add jump VMs |
| Job log says `Password Safe onboarding skipped (non-fatal): … no … functional account is configured` | `reference` mode with a blank name — set the `clouddb_ps_functional_account_*` key the message quotes |
| Job log says `functional account … is on platform …, not a …` | the named account is on the wrong plugin platform; the managed system would inherit it (§0) |
| `functional account 'x' not found in Password Safe` | typo, or the account exists on a platform the API identity cannot see |
| `create`-mode functional accounts still appearing | the mode never took effect — reopen the panel and confirm it reads `Reference an existing account` |
| Azure Run Command permission denied | SP lacks Virtual Machine Contributor on the **jump-VM** resource group |
| Address field 3 or 4 empty | `azure_subscription_id` / `azure_tenant_id` unset on the Azure panel |
| `4031` / 403 on checkout | `pscli` identity lacks Requestor on the **database** Smart Rule (§1.6) — also returned when the account is not API-enabled, or when the `SystemID` does not own the `AccountID` |
| Decommission left artifacts | managed systems are deregistered before their functional accounts are deleted; a partial failure logs each id it could not remove |
