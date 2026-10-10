# Windows servers

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are deploying a Windows server on AWS, Azure or GCP and want to know where its administrator password goes, how PRA reaches it, or how it joins Entra ID or Active Directory.

Part of [Cloud](../cloud.md).

Deploying the VM itself, and everything a Windows and a Linux server share, is in [Cloud VMs](vms.md).

Windows builds on **AWS, Azure and GCP** follow a different path from Linux after the VM
exists: no SSH key and no Entitle SSH integration. In their place is the local
administrator password, a PRA **Shell Jump over OpenSSH** (with an RDP jump only if you ask
for one), and an identity: an Entra ID join (directly on Azure, through Azure Arc on AWS and
GCP), or an [Active Directory join](../directories.md) on AWS, GCP and Azure (Entra Domain
Services). A server gets one or the other, not both.

## Where the administrator password goes

The password is **never stored in the dashboard database**. Each Windows build writes it to
the first of these that is configured (`services/windows_admin_secret.py`):

1. `windows_admin_secret_backend`, if set on **Secrets → Windows VM administrator passwords**.
   `database` is refused.
2. **BeyondTrust Password Safe** (Secrets Safe), when the ps-cli client
   (`pscli_api_url` / `pscli_client_id` / `pscli_client_secret`) and a numeric **Secret
   Owner** (`secrets_bt_owner`) are configured. The secret lands in `secrets_bt_folder`.
3. The global secrets backend, if it is an external one.
4. The cloud's own vault: Azure Key Vault (`secrets_azure_kv_url`) for Azure, AWS Secrets
   Manager (the AWS region) for EC2, GCP Secret Manager (the GCP project) for GCE.

If none of these is configured, **the build is refused** before anything is created.
Job metadata keeps only the backend and the reference, never the password.

How the password comes into existence differs per cloud:

- **Azure** generates it and writes it to the store *before* the VM is created, so a VM is
  never created with a password nobody can read.
- **AWS** lets EC2 generate it. The launch creates a one-time RSA key pair
  (`vmdash-win-<job>`), waits for `GetPasswordData` (usually 4–15 minutes; the job shows
  progress), decrypts the password in memory, stores it, and **deletes the key pair**. No
  password or key material goes into UserData or SSM command history. (UserData does
  carry the [OpenSSH bootstrap](#pra-jumps-ssh-by-default-rdp-on-request), which holds no
  secret.) An AMI that never
  publishes a password (a custom image built without EC2Launch's random password) fails the
  deploy with that reason after 25 minutes.
- **GCP** has no "get password" call. After the instance boots, the deploy writes a
  one-time RSA public key to the `windows-keys` instance metadata. The guest agent creates
  the local account (`gcp_windows_admin_username`, default `gcpadmin`) with a random
  password and writes it, encrypted to that key, to serial port 4. The deploy decrypts it
  in memory, stores it, and removes the key from metadata. This is what
  `gcloud compute reset-windows-password` does. Windows images get a boot disk of at
  least 50 GB. A Windows image is recognised by the public `windows-cloud` project or by
  its Windows licence.

Retrieve it with **Password** on the cloud's VM list, or
`GET /api/azure/vms/{name}/admin-password` / `GET /api/aws/instances/{id}/admin-password` /
`GET /api/gcp/instances/{name}/admin-password`.
Both need the cloud's **write** permission, because they hand out a working
administrator credential, and both are audited. Destroying the VM deletes the stored
password.

## Password Safe managed account

With **Onboard into Password Safe** ticked (and `passwordsafe_registration_enabled`), the
local administrator is onboarded as a **password-managed** account (`method="password"`):

- The functional account comes from `passwordsafe_vm_functional_account_windows_<cloud>`,
  else `passwordsafe_vm_functional_account_windows`. It must be on a **Windows platform**.
  The Linux functional accounts sit on SSH-rotation plugins and are deliberately not
  reused.
- The account is seeded with the build-time password and then rotated straight away
  (`passwordsafe_windows_change_password_on_register`, default on), so afterwards only
  Password Safe knows it.
- Once Password Safe holds a working credential, the build-time copy in the secret manager
  is **deleted**: after a rotation it would be wrong. **VMs → Password** then answers 409
  and points at Password Safe check-out. If onboarding fails, the copy stays as the
  break-glass credential and the job records `ps_error`.
- Password Safe rotates over SMB/WinRM from the appliance or a Resource Broker, so a VM in a
  private subnet needs a route: a resource zone covering the subnet, or
  `passwordsafe_application_host_id`.
- Off-boarding is automatic on destroy.

Do not also enable **Windows LAPS** for the built-in administrator. Two rotators for one
account will fight; pick one owner.

## PRA jumps: SSH by default, RDP on request

With PRA enabled, each Windows VM gets a **Shell Jump** on port 22 in the cloud's Jump
Group, through its Gateway, resolved the same way as the Linux Shell Jump. The session
opens in PowerShell. Tick **Also create an RDP jump** on the deploy form for a **Remote
RDP** jump item as well. The checkbox defaults to `windows_rdp_default` (off).

Every session goes through PRA, and neither jump needs Active Directory. Both log on as the
VM's **local** administrator: OpenSSH resolves a bare username to the local account, and
the RDP jump qualifies it as `.\user` so NLA falls back to NTLM. An Entra or AD sign-in is
not required for either.

**How OpenSSH is switched on.** The build runs a first-boot PowerShell script
(`windows_server_hook.WINDOWS_SSH_BOOTSTRAP_PS1`). It installs the OpenSSH Server
Feature-on-Demand, starts `sshd`, opens TCP 22 in Windows Firewall and makes PowerShell the
login shell. It does **not** change RDP: not creating an RDP jump is what makes RDP
optional. Each cloud delivers the script and reads back its one-line verdict
(`VMDASH-SSHD:OK` or `VMDASH-SSHD:FAIL <reason>`) differently:

| Cloud | Delivery | Result read from |
|---|---|---|
| AWS | EC2 UserData (`<powershell>`), run by EC2Launch | a status file, over Systems Manager. Needs `ssm_instance_profile` |
| Azure | Run Command (`RunPowerShellScript`), after the VM is created | the Run Command output |
| GCP | `windows-startup-script-ps1` metadata | serial port 1 |

What happens next depends on the verdict:

- **OK:** a Shell Jump, plus an RDP jump only if one was asked for.
- **FAIL:** an RDP jump **instead** (`bt_rdp_fallback`), with the reason in
  `windows_ssh_error` on the job.
- **No verdict** (SSM never came online, say): both jumps, with a warning. The Shell Jump
  may work; the RDP jump is the way in if it does not.

**The SSH key is the Linux one.** The bootstrap also authorizes, for administrators, the
same public key a Linux build of that cloud gets, read from the same secret: the AWS
Secrets Manager key (`ssh_key_secret_override`, else the region's key secret), the Azure
Key Vault keypair (`ssh_key_secret_override`, else `azure_ssh_keypair_secret_name`), or
the GCP Secret Manager key (`ssh_key_secret_override`, else `gcp_ssh_key_secret_name`). It
lands in `C:\ProgramData\ssh\administrators_authorized_keys`, restricted to Administrators
and SYSTEM, which is the only file OpenSSH reads for an administrator. The deploy records
the secret it used, so anything that holds that key's private half reaches the server
exactly as it reaches a Linux VM. A missing or unreadable secret is a warning on the job
(`windows_ssh_key_error`), and SSH still accepts the password.

**Config Management reaches it the same way.** A run against a Windows cloud VM, on the
local runner or on ECS, ACI or Cloud Run, connects over SSH with that key, as the
administrator the deploy recorded (`Administrator`, the Azure admin user, or `gcpadmin`),
and adds `-e ansible_connection=ssh -e ansible_shell_type=powershell`. Extra vars outrank a
play's own `ansible_connection: winrm`, so the sample Windows playbooks run unchanged. The
dashboard recognises a Windows VM from its deploy job; a VM it did not build is treated as
Linux. On-prem Windows hosts behind a remote agent still use WinRM.

Windows Server 2025 ships with OpenSSH installed. **Server 2019 and 2022 download it from
Windows Update, so the VM needs outbound internet** (on GCP, `gcp_vm_nat_enabled`). The
cloud firewall must allow 22 from the Gateway, as it already does for Linux VMs.

Credential injection:

- When Password Safe manages the account, **no PRA Vault copy** is made. PRA injects the
  current credential through its Password Safe integration, and a copy would go stale at
  the first rotation. The managed system's port is 22 when the Shell Jump is the only
  jump, and 3389 when there is an RDP jump.
- Otherwise the build-time password is vaulted in PRA for injection, in
  `pra_windows_vault_account_group_id` if set: `<vm>-ssh-admin` for the Shell Jump and
  `<vm>-admin` for the RDP jump. The Shell Jump's Terraform state is stored scrubbed of the
  password.

Turn off **Windows servers: reach them over SSH** (`windows_ssh_enabled`) in Settings → PRA
to go back to an RDP jump on every Windows build. The jumps and their Vault accounts are
removed on destroy.

## Entra ID join (Azure only)

Tick **Join to Microsoft Entra ID** on a Windows deploy. The default comes from
`azure_windows_entra_join`, set in the setup wizard's Azure advanced section. The deploy:

1. Gives the VM a **system-assigned managed identity**.
2. Installs the **AADLoginForWindows** extension. With **Also enrol in Intune**
   (`azure_windows_entra_intune_enroll`), it passes Intune's `mdmId` so the device enrols.
3. Grants **Virtual Machine Administrator Login** to the groups in
   `azure_entra_vm_admin_group_ids`, and **Virtual Machine User Login** to
   `azure_entra_vm_user_group_ids`, scoped to that one VM. Both are comma-separated Entra
   group **object ids**.

Requirements:

- Windows Server 2019 or later (or Windows 10 20H2+ / 11).
- Outbound HTTPS from the VM to `login.microsoftonline.com`,
  `enterpriseregistration.windows.net` and `pas.windows.net`.
- For step 3, the dashboard's service principal needs
  `Microsoft.Authorization/roleAssignments/write` (Role Based Access Control Administrator
  or User Access Administrator) on the VM's resource group.

A failure in any step is a **warning on the job** (`entra_error` / `entra_role_errors`),
not a failed deploy. The vaulted or Password Safe-managed local administrator still reaches
the VM. Destroy removes the role assignments the deploy created, before deleting the VM.

Entra sign-in over RDP needs a client that supports Entra authentication for RDP. Keep the
local administrator as break-glass. For just-in-time access, an Entitle Azure integration
can grant *Virtual Machine Administrator Login* on the VM for a limited time. That is the
Windows counterpart of the Linux SSH ephemeral accounts.

## Entra ID join on AWS and GCP (Azure Arc)

Windows Server can be Entra joined directly only on Azure. On AWS and GCP it gets there
through **Azure Arc**: the server is onboarded as an Arc-enabled server, a
`Microsoft.HybridCompute/machines` resource in your Azure subscription, and the same
**AADLoginForWindows** extension an Azure VM uses is installed on that resource. The server
ends up Entra joined, with no domain controller anywhere.

Pick **Entra join through Azure Arc** under **Microsoft Entra ID** on the AWS or GCP deploy
form. The default comes from `windows_arc_entra_default`. Once the deploy has completed, a
follow-up job (`windows_arc_join`):

1. checks that the subscription can hold an Arc machine, and refuses with the command that
   fixes it when it cannot. It runs none of these commands itself:
   - the `Microsoft.HybridCompute`, `Microsoft.GuestConfiguration` and
     `Microsoft.HybridConnectivity` resource providers must be registered
     (`az provider register --namespace …`);
   - the Arc resource group must exist (`arc_resource_group`, blank = `azure_resource_group`);
2. runs the built-in `arc-onboard-windows.yml` play on the server over OpenSSH, on the
   cloud's Config Management runner. The play checks the OS, installs the Connected Machine
   agent and runs `azcmagent connect`;
3. installs AADLoginForWindows on the Arc machine;
4. grants the groups in `azure_entra_vm_admin_group_ids` / `azure_entra_vm_user_group_ids`
   **Virtual Machine Administrator / User Login** on it, exactly as on Azure.

Requirements:

- **Windows Server 2025 or later, with Desktop Experience.** The play stops on anything
  older, or on Server Core, and says so.
- **OpenSSH for Windows** on (`windows_ssh_enabled`), because the play runs over it.
- **Outbound 443** from the server to the Azure Arc endpoints and to
  `login.microsoftonline.com`, `enterpriseregistration.windows.net` and `pas.windows.net`.
- The dashboard's Azure identity needs **Azure Connected Machine Onboarding** (or
  Contributor) on the Arc resource group, plus roleAssignments/write there for step 4.
- For an ECS or Cloud Run runner, a way to deliver a credential to the task:
  collect-from-dashboard, or the ephemeral Secrets Manager copy
  (`ansible_cloud_ephemeral_secrets_enabled`).

**The onboarding credential.** `azcmagent connect` is given an ARM access token for the
dashboard's own Azure identity:

- it is minted when the job runs and lasts about an hour;
- it reaches the server only through the runner's secret channel, the same one a Password
  Safe checkout uses, and is scrubbed from the job's output;
- it is never put in instance metadata, never sent as a Systems Manager parameter, and
  never written to either job.

It does appear briefly on the guest's `azcmagent` command line, so a server with
command-line process auditing records it. Rotate nothing afterwards: it expires on its own.

**Signing in.** Use an Entra account that holds one of the two login roles. The RDP client
must be Entra joined, hybrid joined or registered in the same tenant. Alternatively, use
*Use a web account to sign in* with the server's hostname (not its IP). Conditional Access
is not supported on Arc-joined servers.

**Destroy** removes the login role assignments the job created, then deletes the Arc
machine. The Entra **device object** is left for you to remove: it is named by the guest's
hostname, which is not unique, so deleting one by name could remove the wrong device.
Entra's stale-device cleanup also takes it.

A failure at any step is a warning on the deploy job (`entra_error`); the server and its
local administrator are unaffected.

## Active Directory join (AWS and GCP)

On AWS and GCP, a Windows server can instead join a managed Active Directory at deploy:
pick one under **Join Active Directory** on the deploy form. Directories are built or
registered on the [Directories](../directories.md) page, which also covers
what each cloud requires.

- **AWS:** after the password is captured, the instance runs AWS's
  `AWS-JoinDirectoryServiceDomain` document through Systems Manager, then reboots.
- **GCP:** the instance is created with `managed-ad-domain` metadata, and the guest agent
  joins during first boot.

A failed join is a warning on the job, not a failed deploy. To have users sign in with
Entra identities as well, synchronise that AD with Entra ID (Entra Connect or Cloud Sync).
A server joins an AD domain or Entra ID through Arc, not both: picking one clears the other
on the form.

If Entra Connect syncs that domain with hybrid join configured, pick **Hybrid join** under
**Microsoft Entra ID** as well: the server is then also Entra hybrid joined, and a follow-up
check confirms it. See [Hybrid Entra join](../directories/on-premises.md#hybrid-entra-join).

## Not yet supported

- **OCI Windows.** OCI returns a Windows password through its initial-credentials API, with
  a forced change at first logon. That is not implemented, so Windows images on OCI are
  not usable from the dashboard.
- **Windows desktop pools on AWS and GCP.** Virtual desktop seats there are still
  Linux-only; see [Virtual Desktops](virtual-desktops.md).
- **Removing a joined server's computer object from AD on destroy.** Neither cloud does
  it, and it needs domain credentials on a host that can reach a domain controller.
