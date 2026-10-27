# Cloud VMs

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are deploying cloud VMs and want the full access and onboarding story.

Part of [Cloud](../clouds.md).

The dashboard deploys **cloud virtual machines** across AWS, Azure, GCP, and OCI, then
layers the BeyondTrust PAM stack on top — the same **provisioning + three layers** model
as [Databases](../cloud-services/databases.md) and [Kubernetes](../cloud-services/kubernetes.md):

- **Provisioning** *(stand it up)* — launch an instance into a **private** subnet and inject
  an SSH key. Done directly through each cloud's **SDK** (not Terraform — see Architecture).
- **Layer 1 — PRA** *(reach it)* — broker a BeyondTrust **Shell Jump** so an operator can
  SSH the private VM through the PRA representative console.
- **Layer 2 — Password Safe** *(manage its secrets)* — *optional.* Onboard the VM as a
  Password Safe managed system + managed account so Password Safe rotates its credential.
- **Layer 3 — Entitle** *(grant time-boxed access)* — *optional.* Register the VM for
  **SSH ephemeral accounts** so users request just-in-time access.

| Cloud | Provisioning | L1 PRA (Shell Jump) | L2 Password Safe | L3 Entitle |
|---|---|---|---|---|
| **AWS** | EC2 (Linux + Windows) | ✅ (Shell Jump; Windows: RDP jump opt-in) | ✅ `ssm` plugin (or `ssh`); Windows: password-managed | ✅ SSH ephemeral (Linux) |
| **Azure** | VM (Linux + Windows) | ✅ (Shell Jump; Windows: RDP jump opt-in) | ✅ `azurevm` plugin (or `ssh`); Windows: password-managed | ✅ SSH ephemeral (Linux) |
| **GCP** | GCE (Linux + Windows) | ✅ (Shell Jump; Windows: RDP jump opt-in) | ✅ `gcpvm` plugin (or `ssh`); Windows: password-managed | ✅ SSH ephemeral (Linux) |
| **OCI** | Compute (Linux) | ✅ (shared gateway, or bring your own¹) | ⚠️ `ssh` method only | ✅ SSH ephemeral |

¹ OCI has no dashboard-provisioned gateway — you supply your own (see the OCI section).

Unlike the other features, **cloud VM deploy has no feature toggle** — it's core
functionality available whenever a cloud's credentials are configured, gated only by RBAC
(`require_permission("aws"|"azure"|"gcp"|"oci", …)`). **Windows** is supported on **AWS,
Azure and GCP** — see [Windows servers](windows-servers.md).

---

## Architecture

A deploy is orchestrated by a per-cloud `_run_deploy` background job and runs directly
against the cloud **SDK** (`boto3` / `azure-sdk-for-python` / `google-cloud` / `oci`), not
Terraform. (Terraform VM modules exist under `terraform/ec2_instance`, `terraform/azure_vm`,
`terraform/gce_instance` for a separate CLI-oriented path, but `/api/*/deploy` does **not**
use them.)

There is exactly **one** `_run_deploy` per cloud, and it is the only place that cloud's
SDK is asked to create a VM. A batch does not repeat it: `_run_bulk_deploy` acquires
whatever is genuinely shared for the run — the gateway, and on AWS the NAT instance,
SSM endpoints and SSH key; on Azure the quota check, ACI container and Key Vault key —
then loops calling `_run_deploy` with those injected. Each instance still owns its own
job row, so one failure fails that row and the batch carries on.

That shape is load-bearing rather than tidy. When the batch path was a second copy of
the deploy body it drifted, and every divergence was invisible because a batch still
reported success: AWS batches stopped passing `os_type` and booted Linux VMs with no SSM
agent, and Azure batches ignored the per-deploy Gateway key. `tests/test_deploy_runner_parity.py`
asserts no `_run_bulk_deploy` calls its cloud's launch function directly.

Ordered steps (each Layer-1/2/3 step is **non-fatal** — a failure logs a warning and the
deploy still succeeds):

1. **Ensure the gateway host** (only when `pra_enabled`) — AWS uses a shared
   ref-counted ECS host, Azure the shared `clouddb-jumpoint` VM (see
   `azure_vm_jumpoint_mode`), GCP the shared COS host (see `gcp_vm_jumpoint_mode`), and
   OCI a shared Oracle Linux instance when `oci_vm_jumpoint_mode` is `shared` —
   **`none` by default**, which keeps OCI's historical bring-your-own behaviour. In a
   batch this happens once for the whole run.
2. **Ensure on-demand egress** — AWS: the shared **NAT instance** (`aws_nat_instance_enabled`)
   plus **SSM interface endpoints** (`aws_ssm_endpoints_enabled`). GCP: a **Cloud NAT**
   gateway for the VM subnet plus the egress allow rule (`gcp_vm_nat_enabled`). Both are
   ref-counted and reclaimed when the last VM goes. Azure: a **NAT gateway** for the
   VNet plus an NSG allow that outranks the subnet's internet deny
   (`azure_vm_nat_enabled`), only where the subnet has no way out, removed when the
   subnet's last VM goes. OCI does nothing here.
3. **Fetch the SSH public key** from the cloud's secret store and inject it (Linux via
   cloud-init / `admin_ssh_key` / `ssh-keys` metadata; Windows skips key injection).
4. **Launch the instance** (SDK).
5. **Layer 1** — broker the PRA **Shell Jump**.
6. **Layer 3** — Entitle SSH-ephemeral registration (opt-in).
7. **Layer 2** — Password Safe onboarding (opt-in).

VMs land in a **private** subnet with **no direct internet egress** and are reachable only
from the gateway (SSH/22); see [Cloud Sandbox](sandbox.md) for the per-cloud network
topology. Entry points: `/aws`, `/azure`, `/gcp`, `/oci` (per-cloud deploy + image browser)
and `/vms` (unified cross-cloud inventory).

---

## Who may act on a VM

One rule decides every action on a cloud VM: you may act on it if it is tagged into one of
your workstreams, and an untagged VM is for administrators only. That covers destroy and
power, and also reading its **SSH private key** or **Administrator password** and
**creating an image** from it (a full copy of its disk; on Azure, *generalize* also
leaves the VM unusable). Those last three once skipped the check, so anyone with the
cloud's permission could read another workstream's credentials or copy its disks. The
AWS SSH-key read is now audited, like the password reads.

On Azure, every VM the dashboard deploys shares one key pair from Key Vault, so the rule
can only stop someone with no VM in any of their workstreams reading it; use per-VM
credentials through Password Safe where that matters.

## Pages

The tasks have a page each:

| Page | Read it when |
|---|---|
| [Provisioning cloud VMs](vms/provisioning.md) | you are deploying VMs on AWS, Azure, GCP or OCI: the deploy form, deploying several at once, each cloud's specifics, and the workstream defaults a deploy picks up |
| [Powering cloud VMs](vms/power.md) | you want VMs started, stopped or suspended, on a schedule or under a spend cap, including VMs this dashboard did not deploy |
| [PRA Shell Jumps for cloud VMs](vms/pra.md) | you want a deployed VM reachable through PRA, added after deploy, or need to know which Asset Group and Gateway its jump lands in |
| [Tags and labels on cloud VMs](vms/tags.md) | you want to see, filter or edit cloud VM tags, across one cloud or the whole estate |
| [Cloud VM lifecycle and troubleshooting](vms/troubleshooting.md) | you are destroying a VM, or one cannot be reached, will not deploy or will not come back |

---

## Layer 2 — Password Safe (VM onboarding)

*Optional* (`passwordsafe_registration_enabled` + a per-deploy **"Onboard into Password
Safe"** toggle). Onboards the built VM as a Password Safe **managed system + managed
account** (the baked-in `adminuser`), so Password Safe rotates its credential. Per-cloud
method: **AWS `ssm`** (AWS Systems Manager plugin, DNS `{instance-id}:{region}`), **Azure
`azurevm`** (Azure VM SSH Rotation, address `tenant/sub/rg/vm`), **GCP `gcpvm`** (GCP VM SSH
Rotation, `projectId/zone/instance`), each with an `ssh` fallback. **OCI uses the `ssh`
method only** (no cloud-native plugin) and therefore needs SSH line-of-sight from a Resource
Broker / Gateway.

This is documented in full — plugin uploads, per-cloud methods, the `adminuser` account, and
the config-key table — in the [Password Safe](../beyondtrust/password-safe.md) doc's
**"Password Safe VM onboarding"** section. Off-boarding is automatic on VM destroy.

With `passwordsafe_vault_sync_enabled` on, the three cloud-native methods additionally
mirror the VM's rotating SSH key into a **PRA Vault Private Key** account associated to the
VM's Asset Group, so the key can be checked out in PRA's `/login` and injected into the VM's
Shell Jump. Password Safe owns the propagation through a `SyncedAccounts` link — see
[Using the VM's key in PRA](../beyondtrust/password-safe/vm-onboarding.md#using-the-vms-key-in-pra--the-pra-vault-private-key-sync).

---

## Layer 3 — Entitle (SSH ephemeral accounts)

*Optional* (`entitle_registration_enabled` + a per-deploy **"Register in Entitle"** toggle).
Registers the VM as an Entitle **SSH Ephemeral Accounts** integration so users request
just-in-time SSH access; Entitle mints a short-lived account per grant, using the VM's own
build keypair and `sudo` as the image's cloud-default user (`ubuntu`/`ec2-user`/`azureuser`/
`gcp-user`, override `entitle_ssh_sudo_user`).

- **Public VM** → registered with no agent.
- **Private VM** (the sandbox default) → attaches the **shared Entitle agent** (Kubernetes,
  one per VPC) via `entitle_agent_token_name`.

Requires `entitle_owner_id` + `entitle_workflow_id`. See the [Entitle integration](../beyondtrust/entitle.md)
doc. A separate **machine-identity JIT** track (the AWS `elevate()` wrapping of
`ec2_deploy`/`ec2_terminate`) is covered in [design/cloud-identity-jit.md](../design/cloud-identity-jit.md).

---

## Windows servers

Windows builds on AWS, Azure and GCP have their own page: [Windows servers](windows-servers.md).

## Images

Deploy from a stock marketplace/public image or one the dashboard's Packer flow built
(`/images/aws|azure|gcp`). The **BT-ready provisioners** under
[`provisioners/beyondtrust/`](../../provisioners/beyondtrust/) harden sshd and create the
cloud-default `adminuser` login with passwordless sudo — the account both the Entitle
`sudo_user` and the Password Safe managed account rely on. Full build/promote/export flow is
in [image-management.md](../operations/image-management.md).

---

