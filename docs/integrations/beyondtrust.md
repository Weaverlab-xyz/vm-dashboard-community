# BeyondTrust Integration

## What is it?

The dashboard integrates with four BeyondTrust products. **This page covers the first
two**; the others have their own pages and are listed here so the whole surface is
discoverable from one place.

- **Password Safe / Secrets Safe** — on-demand checkout of SSH keys and passwords.
  Target credentials (AWS keys, Azure service principal secrets, SSH private keys) are
  fetched at the moment the dashboard needs them and discarded after use, rather than
  stored in the dashboard's encrypted database. It also **onboards** resources the
  dashboard builds — VMs, cloud databases — as managed systems + accounts. Driven by
  `ps-cli`.
- **Privileged Remote Access (PRA/SRA)** — brokered access to everything the dashboard
  builds. The dashboard creates and tears down **Shell Jump**, **Web Jump**, **Remote
  RDP** and **Protocol Tunnel** jump items, plus PRA Vault accounts, so operators reach
  VMs, container UIs, databases and Kubernetes API servers through PRA rather than
  direct network exposure. Driven by the `sra` Terraform provider plus a small REST
  client for the few calls the provider can't make.
- **Endpoint Privilege Management for Linux (EPM-L)** — agent package builds, sync to
  asset storage, and installation tokens. Gated by the **same** `beyondtrust_enabled`
  flag as the two above. See [EPM-L](epml.md).
- **Entitle** — just-in-time cloud identity and access requests. A **separate**
  `entitle_enabled` flag, so it can be turned on and off independently. See
  [Entitle](entitle.md).

Password Safe and PRA are controlled by the single `beyondtrust_enabled` flag. You can
configure only Password Safe and leave the PRA fields blank if you do not have a PRA
deployment.

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
- **In-playbook secret lookup (Ansible)** — a config-management playbook can fetch
  its own secrets/managed-account passwords from Password Safe at runtime via the
  `beyondtrust.secrets_safe` Galaxy collection. The dashboard reuses this same OAuth
  client (`pscli_*`) — auto-injecting it into the runner as `PASSWORD_SAFE_*` — so no
  separate credential is needed. See
  [integrations/ansible.md](ansible.md#in-playbook-password-safe-lookup-beyondtrustsecrets_safe)
  and [examples/playbooks/password-safe/](../../examples/playbooks/password-safe/).

---

## Prerequisites

| Requirement | Notes |
|---|---|
| BeyondTrust Password Safe | Secrets Safe licence; hosted or on-prem |
| `ps-cli` | Ships as the `beyondtrust-bips-cli` **pip dependency** (`web_dashboard/requirements.txt`), so it is present in any image built from this repo — there is no separate binary install step |
| BeyondTrust PRA (optional) | Required for jump items, protocol tunnels and PRA Vault accounts |
| Terraform + the `sra` provider | How PRA jump items are created and destroyed; fetched by the dashboard's Terraform flow. No CLI to install |

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

### Part 2 — PRA Config API credentials (optional)

The `sra` Terraform provider and the REST client both authenticate to the BeyondTrust
PRA Configuration API with an OAuth 2.0 client-credentials pair.

1. In **BeyondTrust PRA** → **Configuration** → **API Configuration** →
   **Add API Account**. Copy the **Client ID** and **Client Secret**.
2. The API host is the hostname of your PRA appliance, e.g.
   `https://pra.company.com`.

Grant the account permission to manage **Jump Items** and **Jump Groups**, and — if you
use the cloud-database or Kubernetes tunnels — read access to **Vault Accounts** (the
dashboard enumerates account groups to populate the provision form).

> If your PRA appliance and Password Safe are the same host, the credentials
> from Part 1 and Part 2 may be identical.

**Optional second credential.** Onboarding a database's PRA Vault account into Password
Safe creates a functional account whose username/password *is* a PRA Config API client
pair. Set `pra_config_api_client_id` / `pra_config_api_client_secret` to use a separate,
narrower client for that; left blank, the dashboard falls back to the main
`bt_client_id` / `bt_client_secret`.

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

3. Fill in the **Privileged Remote Access** section (leave blank if not using PRA):

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
| **Managed-account checkout for playbook runs** | A Config-Management run can use a Password Safe managed account as its login identity — the operator picks an account from a live list and the credential is checked out **just-in-time** at run time, never shown and scrubbed from job output. See [below](#managed-account-checkout-for-config-management-runs) |
| **Resource onboarding** | VMs and cloud databases the dashboard builds are registered as Password Safe managed systems + accounts, and removed again on destroy |
| **PRA jump items** | Shell Jump (VMs), Web Jump (Portainer / Rancher UIs), Remote RDP (virtual desktops) and Protocol Tunnel (databases, Kubernetes API) — created and torn down with the resource |
| **PRA Vault accounts** | Tunnel credentials are minted as PRA Vault accounts, which can themselves be onboarded into Password Safe for rotation |
| **EPM-L agent lifecycle** | Package builds, sync to asset storage, and installation tokens — same feature flag ([EPM-L](epml.md)) |
| **Secret audit log** | Every checkout creates an immutable record in Password Safe |

### Managed-account checkout for Config-Management runs

Instead of referencing a *stored* secret, an operator running a playbook can pick a
Password Safe **managed account** and have its credential checked out just-in-time. Two
details are worth knowing because they are not obvious:

- **Across many hosts, the account is matched by name.** A managed account reference
  pins a system id *and* an account id, both specific to one managed system — reusing
  one across a fleet would check out a single machine's credential and connect to every
  host with it. A [bulk run](../config-management.md#bulk-runs-from-the-inventory)
  therefore sends the account **name**, and each job resolves it against the host it is
  configuring, so every host checks out its own credential.
- **On the ECS / Cloud Run runners it needs an opt-in.** Those runners *reference* a
  store secret rather than taking a value inline, which a just-in-time credential has
  no place in — so they are rejected unless **Ephemeral cloud secrets** is enabled, at
  which point the credential is briefly written to that cloud's store as a short-lived,
  RBAC-locked secret and force-deleted after the run.

Full walkthrough in
[Ansible → Managed-account checkout](ansible.md#managed-account-checkout-beyondtrust-password-safe).

---

## Password Safe VM onboarding (managed systems)

When enabled, each freshly built **Linux** VM can be onboarded into Password Safe as a
**managed system + managed account** via a per-deploy **"Onboard into Password Safe"**
checkbox on the AWS / Azure / GCP deploy forms. Turn the capability on under **Settings →
Integrations → BeyondTrust → Resource registration (VMs)** (`passwordsafe_registration_enabled`).
The functional account + workgroup must already exist in Password Safe; the dashboard
resolves them over the public API and creates the managed system/account with Terraform.

> This section is the authoritative reference for **VM** onboarding methods. For the full
> cloud-VM deploy story (provisioning, PRA Shell Jump, Entitle) see [Cloud VMs](../cloud-vms.md).

Three onboarding methods, chosen per cloud:

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

### GCP — GCP VM SSH Rotation custom plugin (cloud-native, default)

The recommended path for GCP. Password Safe writes the public key into the GCE instance's
**`ssh-keys` metadata** (through the Compute Engine API); the in-guest Google guest agent then
propagates it to the user's `~/.ssh/authorized_keys`, so you need **no Resource Broker and no
SSH line-of-sight** — one Password Safe node can manage instances across many projects and
zones. This is the GCP counterpart of the AWS Systems Manager / Azure paths; plugin internals
are documented in **`Beekeeper-GcpVmSshRotation.docx`**.

The dashboard creates the managed system with **DNS name `projectId/zone/instanceName`**
(project from the dashboard's GCP config, zone + instance name from the deploy — the field the
plugin parses) on the custom-plugin platform, and a managed account named after the baked-in
**`adminuser`** Linux user (no `;suffix`). The account's credential is an SSH key the plugin
**generates and writes into the instance's `ssh-keys` metadata** on a credential change. Because
`adminuser` has no key baked in, the dashboard triggers an initial **Change Password** right
after onboarding by default (`passwordsafe_gcp_change_password_on_register`, on) so the account
is immediately usable.

**Prerequisites (one-time, admin):**

- Upload the **GCP VM SSH Rotation** `.PSPLUGIN` in BeyondInsight → **Configuration →
  Privileged Access Management → Platform Plugins**.
- Create a **functional account on the *GCP VM SSH Rotation Custom Plugin* platform** and point
  the dashboard's **Functional account — GCP** at it. Its platform is what binds the managed
  system to the plugin. The credentials are a Google **service account**:
  **Username = service-account email**, **Password = the full service-account JSON key**.
- Grant that service account **`roles/compute.instanceAdmin.v1`** on the target project (covers
  `compute.instances.get` / `setMetadata` / `list` and `compute.zoneOperations.get`).
- **OS Login must be disabled** on the target instances/project — GCE ignores instance
  `ssh-keys` metadata when OS Login is enabled, so the plugin's updates would have no effect.
  (Dashboard-built VMs have OS Login off by default.)
- The image must be built with the **bt-ready** provisioner so the `adminuser` account exists
  on the VM (the guest agent syncs metadata to that existing user; it does not create it).

### AWS / Azure / GCP when set to SSH — traditional managed system

A managed system keyed by hostname/IP on an SSH platform; the dashboard pushes the VM's own
SSH private key into the managed account and `passwordsafe_ssh_key_enforcement_mode` enforces
key-only auth. This requires SSH line-of-sight from a Resource Broker / Jumpoint. Select it per
cloud via the `*_registration_method` key (set to `ssh`).

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
| `passwordsafe_gcp_registration_method` | `gcpvm` | GCP method: `gcpvm` (GCP VM SSH Rotation plugin) or `ssh` |
| `passwordsafe_gcp_change_password_on_register` | `true` | Mint `adminuser`'s first key into GCE `ssh-keys` metadata right after onboarding |
| `passwordsafe_ssh_key_enforcement_mode` | `2` | SSH method only — 0 none / 1 auto / 2 strict |
| `passwordsafe_application_host_id` | `0` | SSH method only — >0 routes via a broker/application host |

Off-boarding is automatic: destroying the VM removes the managed system + account
(Terraform destroy from the stored state). Onboarding failures are **non-fatal** — they are
recorded on the job (`ps_error`) but never fail the deploy.

---

## Cloud databases

The dashboard also provisions **managed cloud databases** (AWS / Azure / GCP / OCI), reaches
them through a PRA protocol tunnel, and can optionally **onboard AWS and Azure databases into
Password Safe** for credential rotation (via the `{engine} SSM Custom Plugin` /
`{engine} Azure Run Command Plugin` and the shared `PRA Vault Username Password` plugin). That
whole feature — base provisioning, per-cloud prerequisites, and the Password Safe onboarding —
is documented separately in **[Cloud Databases](../cloud-databases.md)**.

---

## Preparing images for BT management

Images built by the dashboard's Packer flow (`/images/aws`, `/images/azure`, `/images/gcp`) can be pre-conditioned for BeyondTrust pickup using the provisioner scripts under [`provisioners/beyondtrust/`](../../provisioners/beyondtrust/):

| Script | Targets | Packer provisioner |
|---|---|---|
| [`bt-ready-debian.sh`](../../provisioners/beyondtrust/bt-ready-debian.sh) | Debian, Ubuntu | `shell` |
| [`bt-ready-rpm.sh`](../../provisioners/beyondtrust/bt-ready-rpm.sh) | RHEL, Rocky, CentOS Stream, AlmaLinux, Amazon Linux 2 / 2023 | `shell` |
| [`bt-ready-windows.ps1`](../../provisioners/beyondtrust/bt-ready-windows.ps1) | Windows Server, incl. Server Core | `powershell` |
| [`bt-ready-windows11-vdi.ps1`](../../provisioners/beyondtrust/bt-ready-windows11-vdi.ps1) | Windows 11 multi-session (AVD SKU), for PRA VDI desktops | `powershell` |

### What the Linux scripts prepare

- **PRA Shell Jump connectivity** — sshd hardened (key-only, no root password, sensible client-alive), passwordless sudo wired to the cloud-default user via a `/etc/sudoers.d/90-bt-ready` drop-in, host clock synced. The sshd drop-in is written as `00-bt-ready.conf` so it loads lex-first and wins against any later compliance drop-ins (sshd is first-occurrence-wins). On OpenSSH &lt; 8.2 — no `Include` for `sshd_config.d` — the directives are written into `/etc/ssh/sshd_config` directly instead.
- **A Password Safe / Entitle SSH bootstrap account** — `adminuser` by default (`BT_ADMIN_USER`). This is the account the Azure and GCP onboarding paths above expect to exist: their plugins write a key *to* it, they don't create it.
- **Optional EPM-L package install** — set `BT_EPML_URL` to a presigned URL for the `.deb` / `.rpm`.
- **Optional PRA SSH certificate login** — see below.
- **Conservative baseline hygiene** — security updates applied, persistent journald, opt-in unattended security updates (`BT_AUTOPATCH=1`), image cleaned for re-launch (host keys + machine-id + cloud-init state stripped).
- **Optional CIS / STIG remediation** (`BT_APPLY_CIS=1`) — installs OpenSCAP + SCAP Security Guide and applies a per-distro profile (default CIS L1 Server). Override via `BT_CIS_PROFILE=stig` or `cis_level2_server` (short names auto-expand to the SSG namespace). Report HTML lands at `/var/log/bt-ready/cis-report.html` on the built image. Debian-proper has no SSG CIS profile shipped, and Amazon Linux 2023's SSG coverage is incomplete — both warn and skip.

### PRA SSH certificate authority (optional)

Set `BT_PRA_CA_PUBKEY` to your PRA Vault SSH CA **public** key and the script pins it as
a `cert-authority` line in each PRA account's `authorized_keys`, so those accounts trust
certificates PRA issues instead of needing a static key. Unset ⇒ the feature is entirely
off.

`BT_PRA_USERS` is the comma-separated list of accounts to create for it — and these must
match the **usernames of the PRA Vault SSH-CA accounts** targeting this host, because a
certificate is scoped to its vault account. It is not a list of people.
`BT_PRA_PRINCIPAL` overrides the required principal; `BT_PRA_SUDO=1` grants those
accounts NOPASSWD sudo (default: none).

### What the scripts deliberately don't do

- **No Password Safe onboarding.** They *create* `adminuser`; registering it as a Managed
  Account (Smart Rule / rotation) is out-of-band — or done by the dashboard's own
  [VM onboarding](#password-safe-vm-onboarding-managed-systems).
- **No EPM-L activation.** Package install only. `pbactivate` runs post-deploy with a
  short-lived token from the EPM-L integration, because registration tokens expire 8h
  after issue and can't be baked into an image. See [EPM-L](epml.md).
- **No host firewall.** Cloud security groups / NSGs / GCP firewall rules are the source
  of truth; layering `ufw` / `firewalld` on top risks lockouts.

### Operator-overridable env vars

Set these as Packer build env on the build page. Full detail and a smoke-test recipe in
[`provisioners/beyondtrust/README.md`](../../provisioners/beyondtrust/README.md).

| Var | Notes |
|---|---|
| `BT_TARGET_USER` | Force the sudoers-target user; default autodetects the cloud-default (`ubuntu`/`debian`/`admin`, `ec2-user`/`rocky`/`centos`/`almalinux`/`cloud-user`) |
| `BT_ADMIN_USER` | The Password-Safe-managed bootstrap account (default `adminuser`) |
| `BT_SEED_ADMIN_KEY=1` | Seed `adminuser`'s `authorized_keys` with a throwaway key **so the AWS Systems Manager plugin has one to rotate** — the private half is discarded. Relevant to the [AWS SSM path](#aws--aws-systems-manager-custom-plugin-cloud-native-default), where the credential is minted on first change |
| `BT_ADMIN_NOPASSWD_ALL=1` | Full `NOPASSWD: ALL` sudo for `adminuser` instead of the scoped set — **required for Ansible Config-Management `become`**, which runs sudo's `/bin/sh` |
| `BT_PRA_CA_PUBKEY` | PRA Vault SSH CA public key (enables certificate login) |
| `BT_PRA_USERS` | Accounts to create for certificate login; names must match the PRA vault accounts |
| `BT_PRA_PRINCIPAL` | Require a specific principal instead of the default (principal == username) |
| `BT_PRA_SUDO=1` | NOPASSWD sudo for those accounts |
| `BT_EPML_URL` | Presigned URL to the EPM-L package; set to install it |
| `BT_AUTOPATCH=1` | Enable `unattended-upgrades` / `dnf-automatic` on the built image |
| `BT_SKIP_UPDATES=1` | Skip the upgrade step (faster iteration builds) |
| `BT_SKIP_CLEANUP=1` | Keep host keys / machine-id / logs while debugging |
| `BT_APPLY_CIS=1` | Run OpenSCAP remediation |
| `BT_CIS_PROFILE` | Override the profile id (default CIS L1 Server) |

**Cross-cloud constraint**: Azure's Packer builder invokes shell scripts as `sudo -E sh '{{ .Path }}'`, forcing `/bin/sh` regardless of shebang. Both `.sh` scripts are strict POSIX `sh` (verified with `dash -n`) so they behave identically on AWS, Azure, and GCP. They also self-elevate via `sudo -E`, since the AWS and GCP templates invoke the provisioner as the cloud-default user rather than root.

### Using them

**Linux** — upload the script to your active storage backend via `/storage`, then on the AWS / Azure / GCP build page pick it from the **Load from storage** dropdown above the Provisioner Script textarea.

**Windows** — `bt-ready-windows.ps1` runs on the Azure Windows build (`os_type=Windows`) before the template's windows-restart + Sysprep finisher. It bakes OpenSSH + RDP into the *output* image so VMs deployed from it are reachable by SSH like Linux ones, plus agentless RDP through the Jumpoint.

> **Azure cannot inject SSH public keys into Windows VMs** — that is a Linux-only deploy feature. So the key is authorized *in the image*. Use the public half of the keypair the dashboard holds in Key Vault (`azure_ssh_keypair_secret_name`) so the private half stays retrievable from the VMs tab exactly as it is for Linux. With no key set, password SSH still works using the admin password the deploy generates and vaults.

`bt-ready-windows11-vdi.ps1` is the VDI counterpart: multi-session RDP, NLA + firewall for agentless PRA Remote RDP, conservative VDI optimizations, and optional **first-boot** staging of the Remote Support jump client — never baked installed, so each clone registers a distinct client (cloning an installed one produces a confused rep-console entry, KB0017470).

---

## Advanced configuration

These are **Settings keys**, not environment variables — set them under **Settings →
Integrations → BeyondTrust**. The dashboard's config store reads from the database
only; exporting an equivalently-named env var has no effect.

| Key | Notes |
|---|---|
| `bt_api_host` | PRA appliance hostname, e.g. `tenant.beyondtrustcloud.com` |
| `bt_client_id` / `bt_client_secret` | PRA Config API OAuth client-credentials pair |
| `pra_config_api_client_id` / `pra_config_api_client_secret` | Optional narrower client used only for the PRA Vault functional account; falls back to `bt_client_id` / `bt_client_secret` |
| `bt_jump_group_name` | Jump group new jump items land in |
| `bt_jumpoint_name` | The Jumpoint to route through, **by name** — the pickers in Settings enumerate both from the PRA Config API |
| `azure_bt_jump_group_name` | Azure-specific jump-group override; blank falls back to `bt_jump_group_name` |
| `bt_vault_account_group_id` | Vault account group new tunnel credentials are created in |
| `bt_ps_deploy_key_title` | Title of the Password Safe secret holding the Docker deploy key — the dashboard looks secrets up by title |
| `bt_shell_jump_id` | Recorded per-VM so the jump item can be torn down with the VM; not operator-set |

**Self-hosted Jumpoint (AWS).** A separate `bt_ecs_*` block provisions a Jumpoint host
as an ECS task or EC2 instance (`bt_ecs_launch_type`, `bt_ecs_cluster`,
`bt_ecs_jumpoint_subnet_id`, `bt_ecs_image`, and the CPU/memory/role keys). Configure it
in the same Settings panel; it exists so a cloud VPC can have a Jumpoint with
line-of-sight to private instances without one being run by hand.

---

## Troubleshooting

**"ps-cli not found"** — `ps-cli` comes from the `beyondtrust-bips-cli` pip package in
`web_dashboard/requirements.txt`, so this means the image was built without installing
requirements, or something is shadowing `PATH`. Confirm with
`docker compose exec app ps-cli --version`; rebuild the image if it is genuinely absent.

**"Authentication failed" from ps-cli** — verify the Client ID and Client Secret
in **Settings → Integrations → BeyondTrust** match the API Registration in
Password Safe and that the registration has not expired.

**PRA jump item wasn't created** — the jump item is created by Terraform with the `sra`
provider as part of the resource's own job, so the failure is in that job's log, not a
separate one. Check that `bt_api_host` is reachable from the container
(`docker compose exec app curl -Is "https://<bt_api_host>"`), that the PRA Config API
account may manage Jump Items, and that `bt_jump_group_name` / `bt_jumpoint_name` match
entries that exist in PRA — they are matched by **name**, so a rename in PRA breaks
them silently. A self-signed appliance certificate may need adding to the container's
CA store.

**Secrets retrieved are empty** — check that the API Registration has **Secrets →
Read** and **Credentials → Read** permissions, and that the specific secret is
in scope for the registration.
