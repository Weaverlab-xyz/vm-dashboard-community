# Password Safe: onboarding VMs

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want the VMs the dashboard builds onboarded into Password Safe as managed systems, so their SSH keys or Windows passwords rotate, or want the rotated key synced into the PRA Vault.

Part of [Password Safe](../password-safe.md).

## Password Safe VM onboarding (managed systems)

When enabled, each freshly built **Linux** VM can be onboarded into Password Safe as a
**managed system + managed account** via a per-deploy **"Onboard into Password Safe"**
checkbox on the AWS / Azure / GCP deploy forms. Turn the capability on under **Settings →
Integrations → Password Safe → Resource registration (VMs)** (`passwordsafe_registration_enabled`).
The functional account + workgroup must already exist in Password Safe; the dashboard
resolves them over the public API and creates the managed system/account with Terraform.

> **Functional-account ownership across the three integrations.** VM onboarding and
> [k8s ServiceAccount token rotation](kubernetes-tokens.md#kubernetes-serviceaccount-token-rotation) always
> reference an operator-created account. **Cloud-database** onboarding does too, but only
> when `clouddb_ps_functional_account_mode` is `reference`; its default, `create`, mints one
> per database and deletes it on decommission. See
> [Databases → Layer 2](../../../databases.md#layer-2--password-safe-aws--azure--gcp). Easy to conflate,
> so check the mode before hunting for a missing account.

> This section is the authoritative reference for **VM** onboarding methods. For the full
> cloud-VM deploy story (provisioning, PRA Shell Jump, Entitle) see [Cloud VMs](../../../cloud/vms.md).

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
key-only auth. This requires SSH line-of-sight from a Resource Broker / Gateway. Select it per
cloud via the `*_registration_method` key (set to `ssh`).

### Windows VMs (AWS, Azure and GCP) — password-managed

A Windows VM's local administrator is onboarded as a **password-managed** account on a
traditional managed system (`method="password"`), seeded with the password the build
generated (Azure) or recovered (AWS), then rotated straight away. It uses its **own**
functional account on a Windows platform (`passwordsafe_vm_functional_account_windows*`),
never the SSH-rotation accounts above. Password Safe rotates over SMB/WinRM, so a private VM
needs a Resource Broker whose resource zone covers its subnet. Once Password Safe holds the
credential, the dashboard deletes its build-time copy. See
[Windows servers](../../../cloud/windows-servers.md) for the full flow, including where the
password is kept when Password Safe is not in use.

### Configuration keys — VM onboarding

| Key | Default | Notes |
|---|---|---|
| `passwordsafe_registration_enabled` | `false` | Global capability flag (also per-deploy opt-in) |
| `passwordsafe_workgroup` | — | Workgroup name or id the managed system lands in |
| `passwordsafe_vm_functional_account_aws` / `passwordsafe_vm_functional_account_azure` / `passwordsafe_vm_functional_account_gcp` / `passwordsafe_vm_functional_account_oci` | — | Functional account per cloud (for AWS+SSM, the custom-plugin account) |
| `passwordsafe_managed_account_name` | `adminuser` | The onboarded account (the `{name}` part for SSM) |
| `passwordsafe_vm_functional_account_windows` / `passwordsafe_vm_functional_account_windows_azure` / `passwordsafe_vm_functional_account_windows_aws` | — | Windows VMs: functional account on a **Windows** platform (generic, then per-cloud override) |
| `passwordsafe_windows_change_password_on_register` | `true` | Windows VMs: rotate the seeded administrator password right after onboarding |
| `passwordsafe_directory_functional_account` | — | Managed Active Directory: functional account on an **Active Directory** platform for the directory administrator (set on the Managed Active Directory panel; see [Directories](../../../directories.md)) |
| `passwordsafe_directory_change_password_on_register` | `true` | Managed Active Directory: rotate the seeded administrator password right after onboarding |
| `passwordsafe_aws_registration_method` | `ssm` | AWS method: `ssm` (AWS Systems Manager plugin) or `ssh` |
| `passwordsafe_ssm_account_suffix` | `local` | SSM account-name suffix; an AssumeRole ARN for EC2 cross-account mode |
| `passwordsafe_ssm_change_password_on_register` | `false` | Trigger an initial Change Password after onboarding (mints the key now) |
| `passwordsafe_azure_registration_method` | `azurevm` | Azure method: `azurevm` (Azure VM SSH Rotation plugin) or `ssh` |
| `passwordsafe_azure_change_password_on_register` | `true` | Mint `adminuser`'s first key over Run Command right after onboarding |
| `passwordsafe_gcp_registration_method` | `gcpvm` | GCP method: `gcpvm` (GCP VM SSH Rotation plugin) or `ssh` |
| `passwordsafe_gcp_change_password_on_register` | `true` | Mint `adminuser`'s first key into GCE `ssh-keys` metadata right after onboarding |
| `passwordsafe_ssh_key_enforcement_mode` | `2` | SSH method only — 0 none / 1 auto / 2 strict |
| `passwordsafe_application_host_id` | `0` | Optional. >0 sets a managed system's `ApplicationHostID` — the id of another managed system flagged `IsApplicationHost`. **Not** the Resource Broker handle: broker reachability comes from the broker's resource zone and the workgroup mapped to it. Leave at `0` unless a tenant specifically wants one |

Off-boarding is automatic: destroying the VM removes the managed system + account
(Terraform destroy from the stored state). Onboarding failures are **non-fatal** — they are
recorded on the job (`ps_error`) but never fail the deploy.

### Using the VM's key in PRA — the PRA Vault Private Key sync

The key Password Safe mints and rotates for a VM is governed and audited, but on its own it
is reachable only through a Password Safe checkout: a rep cannot check it out in PRA's
`/login`, and PRA cannot inject it into the VM's Shell Jump. Enable **Also sync each VM's
managed SSH key into a PRA Vault Private Key account** (Settings → Integrations → Password
Safe) and each onboarded VM additionally gets:

1. a **PRA Vault SSH account** named `<vm>-<account>` (e.g. `web01-adminuser`), associated
   to the VM's Jump Group so PRA can inject it, seeded with a throwaway key;
2. a Password Safe **managed system + account on the `PRA Vault Private Key` plugin**, named
   identically — the plugin resolves its PRA-side target by *name*;
3. a **`SyncedAccounts` link** making that mirror a *subscriber* of the VM's own managed
   account, then one Change Password so PRA holds a real key immediately rather than at the
   next scheduled rotation.

From then on Password Safe owns the propagation, exactly as it does for
[Kubernetes ServiceAccount tokens](kubernetes-tokens.md#keeping-the-pra-vault-copy-in-sync) — every rotation of
the VM's account is applied to the subscriber too, which runs the plugin's write into PRA.
**No key passes through the dashboard**, and the seeded throwaway is redacted out of the job
record before it is stored.

This applies to the three cloud-native plugins only (`ssm`, `azurevm`, `gcpvm`), where the
account's stored credential *is* the key Password Safe minted. The traditional `ssh` method
is deliberately excluded: there the key came from a cloud secret store the dashboard already
holds, so mirroring it into PRA would publish an existing key rather than a governed one.

The Jump Group is the deploy's own (the Shell Jump is provisioned before the Password Safe
step), falling back to `bt_jump_group_name` / the per-cloud override. With no Jump Group
resolvable the sync is **skipped** rather than half-built — the job records
`ps_vault_skipped` and the onboarding itself is untouched. Every other failure lands on
`ps_vault_error` and is likewise non-fatal.

Teardown is automatic and ordered: destroying the VM unlinks the pair, off-boards the mirror,
destroys the PRA Vault account, and only then off-boards the VM's own managed system.

#### Operator prerequisites

1. Import the **`PRA Vault Private Key`** `.psplugin` and confirm the platform name.
2. Create a **functional account on that platform** — username = the PRA OAuth client id,
   password = its secret — and set `passwordsafe_vault_sync_functional_account`. This has
   **no fallback** to `ot_ps_pravault_functional_account` or
   `clouddb_ps_pravault_functional_account`: those accounts are on the *PRA Vault Username
   Password* platform, which writes a password field and never a key, so borrowing one would
   register a mirror that reports success and syncs nothing. A functional account whose
   platform does not match `passwordsafe_vault_sync_platform` is refused.
3. Grant the API identity **Password Safe Account Management (Full control)** — what the sync
   link needs.
4. **Leave "Change Password After Release" OFF on *both* accounts.** A credential change on
   either member of a synced pair re-rotates the pair, so with it on every release of the PRA
   copy would rotate the VM's real host key.
5. Optionally set `bt_vault_account_group_id` to place the Vault accounts in a specific
   account group.

#### Configuration keys — PRA Vault key sync

| Key | Default | Notes |
|---|---|---|
| `passwordsafe_vault_sync_enabled` | `false` | Off by default — the plugin is hand-imported, so its platform cannot be assumed to exist |
| `passwordsafe_vault_sync_platform` | `PRA Vault Private Key` | Mirror platform name; also the platform the functional account is checked against |
| `passwordsafe_vault_sync_functional_account` | — | Required. Functional account on that platform. No fallback, by design |
| `passwordsafe_vault_sync_converge` | `true` | One Change Password through the new link, so PRA holds a real key now. **Not** the per-cloud `*_change_password_on_register` flag: that one governs onboarding, and on AWS it defaults off, which would leave PRA serving the throwaway key |
