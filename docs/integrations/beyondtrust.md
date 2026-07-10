# BeyondTrust Integration

## What is it?

The BeyondTrust integration connects the dashboard to two BeyondTrust products:

- **Password Safe (ps-cli)** — on-demand checkout of SSH keys and passwords
  stored in BeyondTrust Secrets Safe. Target credentials (AWS keys, Azure
  service principal secrets, SSH private keys) are fetched from Password Safe
  at the moment the dashboard needs them and discarded after use, rather than
  being stored in the dashboard's encrypted database.
- **Privileged Remote Access (btapi)** — optional session-metadata callbacks
  to BeyondTrust PRA during remote access operations (Shell Jump / session
  recording context).

Both are controlled by the single `BEYONDTRUST_ENABLED` flag. You can
configure only Password Safe (ps-cli) and leave the btapi block blank if you
do not have a PRA deployment.

---

## Use cases

- **Vault-backed cloud credentials** — instead of entering AWS access keys,
  Azure service principal secrets, or SSH private keys into the dashboard
  (where they would be stored encrypted in the application database), the
  dashboard fetches them from Password Safe at runtime. Rotate credentials in
  one place; the dashboard always gets the current value.
- **Audit trail** — every secret checkout creates a Password Safe audit record.
  You know who (the dashboard service account) requested what credential and
  when.
- **SSH key checkout for cloud VMs** — the Ansible config-management runner and
  BeyondTrust Jumpoint container retrieve SSH keys from Password Safe managed
  accounts, so the private key never touches the host filesystem.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| BeyondTrust Password Safe | Secrets Safe licence; hosted or on-prem |
| `ps-cli` binary inside the container | BeyondTrust BIPS CLI; baked into the `app` Docker image |
| BeyondTrust PRA (optional) | Required only if using session recording via btapi |
| `btapi` binary inside the container | BeyondTrust API CLI; baked into the `app` Docker image |

---

## Setup

### Part 1 — Password Safe OAuth application (ps-cli)

ps-cli authenticates to Password Safe with an OAuth 2.0 client-credentials
grant.

1. In **Password Safe** → **Configuration** → **API Registration** →
   **Add API Registration**:
   - Authentication type: **Client Credentials**
   - Copy the **Client ID** and **Client Secret** displayed after creation.

2. Assign the registration the following permissions (minimum):
   - **Secrets** → Read
   - **Requests** → Create
   - **Credentials** → Read

   Add Managed System / Managed Account scope for any accounts the dashboard
   will check out SSH keys from.

### Part 2 — btapi credentials (PRA session recording — optional)

btapi authenticates to the BeyondTrust PRA API with its own client credentials.

1. In **BeyondTrust PRA** → **Configuration** → **API Configuration** →
   **Add API Account**. Copy the **Client ID** and **Client Secret**.
2. The API host is the hostname of your PRA appliance, e.g.
   `https://pra.company.com`.

> If your PRA appliance and Password Safe are the same host, the credentials
> from Part 1 and Part 2 may be identical.

### Part 3 — Enable and configure in the dashboard

**Option A — Setup wizard (first run)**

The wizard Step 5 lists optional integrations. Toggle **BeyondTrust** on and
fill in the fields.

**Option B — Settings → Integrations (after first run)**

1. Open **Settings** → **Integrations** → **BeyondTrust** → toggle on.
2. Fill in the **Password Safe** section:

   | Field | Example |
   |---|---|
   | Password Safe URL | `https://ps.company.com` |
   | OAuth Client ID | (from API Registration) |
   | OAuth Client Secret | (from API Registration) |

3. Fill in the **btapi** section (leave blank if not using PRA):

   | Field | Example |
   |---|---|
   | API Host | `https://pra.company.com` |
   | Client ID | (from API Account) |
   | Client Secret | (from API Account) |

4. Click **Save**. No container restart is required.

---

## What it enables in the dashboard

| Feature | Description |
|---|---|
| **Vault-backed cloud credentials** | AWS, Azure, and SSH credentials resolved from Password Safe at runtime rather than stored in the application database |
| **SSH key checkout** | Ansible and BT Jumpoint tasks retrieve SSH private keys from Managed Accounts on demand |
| **PRA session context** | Shell Jump sessions opened by the dashboard are tagged with job metadata in BeyondTrust PRA |
| **Secret audit log** | Every checkout creates an immutable record in Password Safe |

---

## Password Safe VM onboarding (managed systems)

When enabled, each freshly built **Linux** VM can be onboarded into Password Safe as a
**managed system + managed account** via a per-deploy **"Onboard into Password Safe"**
checkbox on the AWS / Azure / GCP deploy forms. Turn the capability on under **Settings →
Integrations → BeyondTrust → Resource registration (VMs)** (`passwordsafe_registration_enabled`).
The functional account + workgroup must already exist in Password Safe; the dashboard
resolves them over the public API and creates the managed system/account with Terraform.

Two onboarding methods, chosen per cloud:

### AWS — AWS Systems Manager custom plugin (cloud-native, default)

The recommended path. Password Safe manages the Linux EC2 instance over **AWS SSM
`SendCommand`** instead of SSH, so you need **no per-VPC Resource Broker and no SSH
line-of-sight** — one Password Safe node (or a single Cloud Resource Broker on EC2) can
manage Linux instances across many accounts/VPCs.

The dashboard creates the managed system with **DNS name `{instance-id}:{region}`** (e.g.
`i-0eaa6a10886717ed:us-east-1`, the field the plugin parses) on the custom-plugin platform,
and a managed account named **`{managed_account_name};{suffix}`**. The account's credential
is an SSH private key that **Password Safe mints over SSM on a credential change** — it is
not set at creation. Auto-management rotates it on schedule; optionally the dashboard can
trigger an immediate **Change Password** right after onboarding
(`passwordsafe_ssm_change_password_on_register`, off by default).

**Prerequisites (one-time, admin):**

- Upload the **AWS Systems Manager** `.PSPLUGIN` in BeyondInsight → **Configuration →
  Privileged Access Management → Platform Plugins**.
- Create a **functional account on the *AWS Systems Manager Custom Plugin* platform** and
  point the dashboard's **Functional account — AWS** at it. Its platform is what binds the
  managed system to the plugin.
  - **IAM-user mode** (suffix `local`): the functional account password is
    `{AccessKeyID}:{AccessKeySecret}` for an IAM user with `ssm:SendCommand`,
    `ssm:ListCommandInvocations`, `ssm:GetCommandInvocation`.
  - **EC2 mode** (cross-account Resource Broker on EC2): set **SSM account suffix** to the
    remote-account **AssumeRole ARN** (`{name};arn:aws:iam::…:role/…`); auth is the broker
    EC2 instance's IAM role, so the functional account holds only placeholder credentials.
- The instance must already be **SSM-managed** — the deploy attaches
  `ec2_ssm_instance_profile`, which must grant `AmazonSSMManagedInstanceCore`. Confirm the
  instance appears in **Fleet Manager** before onboarding.

### Azure — Azure VM SSH Rotation custom plugin (cloud-native, default)

The recommended path for Azure. Password Safe writes the key onto the VM over **Azure VM
Run Command** (through the Azure control plane) instead of SSH, so you need **no Resource
Broker and no SSH line-of-sight** — one Password Safe node can manage Linux VMs across many
resource groups and regions. This is the Azure counterpart of the AWS Systems Manager path;
plugin internals are documented in **`Beekeeper-AzureVmSshRotation.docx`**.

The dashboard creates the managed system with **address
`tenantId/subscriptionId/resourceGroup/vmName`** (tenant + subscription from the dashboard's
Azure config, resource group + VM name from the deploy — the field the plugin parses) on the
custom-plugin platform, and a managed account named after the baked-in **`adminuser`** Linux
user (no `;suffix`). The account's credential is an SSH key the plugin **generates and writes
onto `adminuser`'s `~/.ssh/authorized_keys` via Run Command** on a credential change. Because
`adminuser` has no key baked in, the dashboard triggers an initial **Change Password** right
after onboarding by default (`passwordsafe_azure_change_password_on_register`, on) so the
account is immediately usable.

**Prerequisites (one-time, admin):**

- Upload the **Azure VM SSH Rotation** `.PSPLUGIN` in BeyondInsight → **Configuration →
  Privileged Access Management → Platform Plugins**.
- Create a **functional account on the *Azure VM SSH Rotation Custom Plugin* platform** and
  point the dashboard's **Functional account — Azure** at it. Its platform is what binds the
  managed system to the plugin. The credentials are the Azure service principal:
  **Username = Application (client) ID**, **Password = client secret**.
- Grant that service principal **Virtual Machine Contributor** on the target resource group
  (covers `Microsoft.Compute/virtualMachines/read` + `runCommand/action`). You may reuse the
  service principal the dashboard already uses to deploy Azure VMs — it qualifies.
- The image must be built with the **bt-ready** provisioner so the `adminuser` account exists
  on the VM (the plugin `chown`s the key to it; it does not create the account).

### GCP (and AWS / Azure when set to SSH) — traditional managed system

A managed system keyed by hostname/IP on an SSH platform; the dashboard pushes the VM's own
SSH private key into the managed account and `passwordsafe_ssh_key_enforcement_mode` enforces
key-only auth. This requires SSH line-of-sight from a Resource Broker / Jumpoint.

### Configuration keys

| Key | Default | Notes |
|---|---|---|
| `passwordsafe_registration_enabled` | `false` | Global capability flag (also per-deploy opt-in) |
| `passwordsafe_workgroup` | — | Workgroup name or id the managed system lands in |
| `passwordsafe_vm_functional_account_aws` / `_azure` / `_gcp` | — | Functional account per cloud (for AWS+SSM, the custom-plugin account) |
| `passwordsafe_managed_account_name` | `adminuser` | The onboarded account (the `{name}` part for SSM) |
| `passwordsafe_aws_registration_method` | `ssm` | AWS method: `ssm` (AWS Systems Manager plugin) or `ssh` |
| `passwordsafe_ssm_account_suffix` | `local` | SSM account-name suffix; an AssumeRole ARN for EC2 cross-account mode |
| `passwordsafe_ssm_change_password_on_register` | `false` | Trigger an initial Change Password after onboarding (mints the key now) |
| `passwordsafe_azure_registration_method` | `azurevm` | Azure method: `azurevm` (Azure VM SSH Rotation plugin) or `ssh` |
| `passwordsafe_azure_change_password_on_register` | `true` | Mint `adminuser`'s first key over Run Command right after onboarding |
| `passwordsafe_ssh_key_enforcement_mode` | `2` | SSH method only — 0 none / 1 auto / 2 strict |
| `passwordsafe_application_host_id` | `0` | SSH method only — >0 routes via a broker/application host |

Off-boarding is automatic: destroying the VM removes the managed system + account
(Terraform destroy from the stored state). Onboarding failures are **non-fatal** — they are
recorded on the job (`ps_error`) but never fail the deploy.

---

## Password Safe cloud-database onboarding (AWS)

An optional extension of the cloud databases feature (AWS RDS only
for now — Postgres / MySQL / SQL Server). When enabled, provisioning an AWS database also
hands rotation of a database credential to Password Safe and keeps the PRA-vaulted credential
in sync. Turn it on under **Settings → Integrations → BeyondTrust → Cloud Database Onboarding**
(`clouddb_ps_onboarding_enabled`).

**What the dashboard does, per provisioned AWS DB:**

1. Creates a **dedicated managed DB user** (the rotation target) from the minted admin
   credential, by running the DB client (`psql` / `mysql` / `sqlcmd`) on the shared **PRA
   Jumpoint host over AWS SSM `SendCommand`** — the only dashboard component with line-of-sight
   to the private DB. No DB drivers are added to the dashboard; the client runs as a
   `docker run` on the jump host.
2. Points the **PRA tunnel's injected Vault credential** at this managed user (not the master
   admin), so day-to-day access uses the rotated account.
3. Onboards the DB as a **managed system + managed account** on the **`{engine} SSM Custom
   Plugin`** platform. The managed system's DNS name is
   `{instanceArn};{region};{dbEndpoint};{dbName};{publicKeyPath};{suffix}`. The **Password
   Safe functional account is the AWS IAM user** used for SSM — there is **no privileged DB
   login**; the managed account changes *its own* password on rotation (self-rotation needs no
   elevated DB privilege).
4. Onboards the PRA Vault account as a managed account on the **`PRA Vault Username Password`**
   plugin (managed system address = the PRA appliance URL; functional account = the PRA
   Configuration-API OAuth client). When Password Safe rotates the DB managed account, this
   propagates the new value into the PRA Vault credential the tunnel injects.

**Prerequisites (one-time, admin — the dashboard cannot automate these):**

- Upload the **`{engine} SSM Custom Plugin`** and **`PRA Vault Username Password`**
  `.PSPLUGIN`s in BeyondInsight → **Configuration → Privileged Access Management → Platform
  Plugins**. Plugin internals are documented in the Beekeeper articles
  (`Beekeeper-UsernamePasswordPRAVault.docx` and the per-engine SSM plugin guides). Set the
  platform-name fields in Settings to match what you uploaded.
- Prepare the **jump host** for the SSM DB plugin: the DB client binary at the path the plugin
  invokes, plus the RSA key pair (`private.pem` + `passphrase.txt`) in the `ssm-user` home for
  credential decryption (see the plugin guide). The dashboard's own managed-user creation uses
  the `docker run` client image instead and does not need this.
- Create a **PRA Configuration-API account** (OAuth client) with **Vault Account Management**
  permission; put its client id/secret in the PRA Config API fields (or leave blank to reuse
  the PRA credentials the SRA provider already uses).
- Run the updated `scripts/sandbox/Linux/setup-aws.sh` so the jumpoint host's `ecsInstanceRole`
  has `AmazonSSMManagedInstanceCore` and the dashboard IAM user has `ssm:SendCommand` /
  `ssm:GetCommandInvocation`.

> **Password sync note.** The dashboard registers both managed systems; making Password Safe
> *propagate* a DB rotation into the PRA Vault managed account may require a Password Safe
> **SmartRule / linked-account** configuration that the Terraform provider cannot express — set
> that up in Password Safe if your rotation policy requires the two to move together.

### Configuration keys (cloud-DB onboarding)

| Key | Default | Notes |
|---|---|---|
| `clouddb_ps_onboarding_enabled` | `false` | Master toggle (AWS only) |
| `clouddb_ps_platform_postgres` / `_mysql` / `_sqlserver` | `psql/mysql/mssql SSM Custom Plugin` | Custom-plugin platform names (as uploaded) |
| `clouddb_ps_pravault_platform` | `PRA Vault Username Password` | PRA Vault plugin platform name |
| `clouddb_ps_workgroup` | — | Workgroup; blank falls back to `passwordsafe_workgroup` |
| `clouddb_db_client_image_postgres` / `_mysql` / `_sqlserver` | `postgres:16` / `mysql:8.4` / `mcr.microsoft.com/mssql-tools18` | DB-client images run on the jump host |
| `clouddb_ps_ssm_iam_username` | — | IAM user (functional account); blank → EC2 role mode |
| `clouddb_ps_ssm_access_key_id` / `_secret_access_key` | — | IAM-mode credentials for the functional account |
| `clouddb_ps_ssm_account_suffix` | `local` | DNS-name suffix; an AssumeRole ARN for cross-account mode |
| `clouddb_ps_ssm_public_key_path` | — | Public-key path on the PS node/broker (plugin cred encryption) |
| `pra_config_api_client_id` / `_secret` | — | PRA Config API OAuth account; blank → reuse `bt_client_id` / `bt_client_secret` |

Off-boarding is automatic: decommissioning the DB deregisters both managed systems and deletes
both functional accounts (before the RDS instance is destroyed — the managed DB user goes with
it). Every onboarding step is **non-fatal**: any failure logs a warning and falls back to the
legacy admin-credential staging, leaving the database up.

---

## Preparing images for BT management

Images built by the dashboard's Packer flow (`/images/aws`, `/images/azure`, `/images/gcp`) can be pre-conditioned for BeyondTrust pickup using the provisioner scripts under [`provisioners/beyondtrust/`](../../provisioners/beyondtrust/):

- [`bt-ready-debian.sh`](../../provisioners/beyondtrust/bt-ready-debian.sh) — Debian, Ubuntu.
- [`bt-ready-rpm.sh`](../../provisioners/beyondtrust/bt-ready-rpm.sh) — RHEL, Rocky, CentOS Stream, AlmaLinux, Amazon Linux 2 / 2023.

What they prepare:

- **PRA Shell Jump connectivity** — sshd hardened (key-only, no root password, sensible client-alive), passwordless sudo wired to the cloud-default user via a `/etc/sudoers.d/90-bt-ready` drop-in, host clock synced. The sshd drop-in is written as `00-bt-ready.conf` so it loads lex-first and wins against any later compliance drop-ins (sshd is first-occurrence-wins).
- **Conservative baseline hygiene** — security updates applied, persistent journald, opt-in unattended security updates (`BT_AUTOPATCH=1`), image cleaned for re-launch (host keys + machine-id + cloud-init state stripped).
- **Optional CIS / STIG remediation** (`BT_APPLY_CIS=1`) — installs OpenSCAP + SCAP Security Guide and applies a per-distro profile (default CIS L1 Server). Override via `BT_CIS_PROFILE=stig` or `cis_level2_server` (short names auto-expand to the SSG namespace). Report HTML lands at `/var/log/bt-ready/cis-report.html` on the built image. Debian-proper has no SSG CIS profile shipped, and Amazon Linux 2023's SSG coverage is incomplete — both warn and skip.

What they deliberately don't do (covered in the script README):

- No new local accounts (sudoers wired to the existing cloud-default user).
- No Password Safe Managed Account creation (those are admin-managed in PS).
- No EPM-L agent install (registration tokens expire 8h after issue; needs a first-boot hook, separate effort).
- No host firewall (cloud security groups are the source of truth).

**Cross-cloud constraint**: Azure's Packer builder invokes scripts as `sudo -E sh '{{ .Path }}'`, forcing `/bin/sh` regardless of shebang. Both scripts are strict POSIX `sh` (verified with `dash -n`) so they behave identically on AWS, Azure, and GCP.

**Using them**: upload the appropriate script to your active storage backend via `/storage`, then on the AWS / Azure / GCP build page pick it from the **Load from storage** dropdown above the Provisioner Script textarea. Full smoke-test recipe and the operator-overridable env vars (`BT_TARGET_USER`, `BT_AUTOPATCH`, `BT_SKIP_UPDATES`, `BT_SKIP_CLEANUP`, `BT_APPLY_CIS`, `BT_CIS_PROFILE`) are in [`provisioners/beyondtrust/README.md`](../../provisioners/beyondtrust/README.md).

---

## Advanced configuration

### Jump group and policy (PRA Shell Jump routing)

If your PRA deployment uses multiple jump groups, set:

```
BT_JUMP_GROUP_NAME=us-east-2
BT_GROUP_POLICY_NAME=BeyondTrust IT User
BT_JUMPOINT_ID=7
```

`BT_JUMPOINT_ID` is the numeric ID of the Jumpoint the dashboard will use for
cloud instances (visible in PRA → **Jump** → **Jumpoints** → edit a jumpoint).

### Azure-specific jump group override

```
AZURE_BT_JUMP_GROUP_NAME=azure-east
AZURE_BT_GROUP_POLICY_NAME=BeyondTrust IT User
AZURE_JUMPOINT_ID=9
```

Leave blank to fall back to the global `BT_JUMP_GROUP_NAME` / `BT_GROUP_POLICY_NAME`.

### Password Safe secret titles

The dashboard looks up secrets by **title** in Password Safe. The defaults work
for a standard deployment; override in **Settings → Integrations → BeyondTrust**
if your titles differ:

```
BT_PS_DEPLOY_KEY_TITLE=Docker Deploy Key
```

---

## Troubleshooting

**"ps-cli not found"** — the `ps-cli` binary must be on `PATH` inside the
container. Verify the Dockerfile includes the BIPS CLI installation step and
rebuild the image.

**"Authentication failed" from ps-cli** — verify the Client ID and Client Secret
in **Settings → Integrations → BeyondTrust** match the API Registration in
Password Safe and that the registration has not expired. Run
`docker compose exec app ps-cli --version` to confirm the binary is present.

**"btapi command failed"** — confirm `BT_API_HOST` is reachable from inside the
container: `docker compose exec app curl -Is "$BT_API_HOST"`. If the host uses
a self-signed certificate you may need to add it to the container's CA store.

**Secrets retrieved are empty** — check that the API Registration has **Secrets →
Read** and **Credentials → Read** permissions, and that the specific secret is
in scope for the registration.
