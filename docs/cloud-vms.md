# Cloud VMs

The dashboard deploys **cloud virtual machines** across AWS, Azure, GCP, and OCI, then
layers the BeyondTrust PAM stack on top — the same **provisioning + three layers** model
as [Cloud Databases](cloud-databases.md) and [Kubernetes](kubernetes.md):

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
| **AWS** | EC2 (Linux + Windows) | ✅ | ✅ `ssm` plugin (or `ssh`) | ✅ SSH ephemeral |
| **Azure** | VM (Linux + Windows) | ✅ (Linux; Windows → RDP jump) | ✅ `azurevm` plugin (or `ssh`) | ✅ SSH ephemeral |
| **GCP** | GCE (Linux) | ✅ | ✅ `gcpvm` plugin (or `ssh`) | ✅ SSH ephemeral |
| **OCI** | Compute (Linux) | ✅ (bring your own jumpoint¹) | ⚠️ `ssh` method only | ✅ SSH ephemeral |

¹ OCI has no dashboard-provisioned jumpoint — you supply your own (see the OCI section).

Unlike the other features, **cloud VM deploy has no feature toggle** — it's core
functionality available whenever a cloud's credentials are configured, gated only by RBAC
(`require_permission("aws"|"azure"|"gcp"|"oci", …)`). **Windows** is supported on **AWS and
Azure** only.

---

## Architecture

A deploy is orchestrated by a per-cloud `_run_deploy` background job and runs directly
against the cloud **SDK** (`boto3` / `azure-sdk-for-python` / `google-cloud` / `oci`), not
Terraform. (Terraform VM modules exist under `terraform/ec2_instance`, `terraform/azure_vm`,
`terraform/gce_instance` for a separate CLI-oriented path, but `/api/*/deploy` does **not**
use them.)

Ordered steps (each Layer-1/2/3 step is **non-fatal** — a failure logs a warning and the
deploy still succeeds):

1. **Ensure the jumpoint host** (only when `beyondtrust_enabled`) — AWS/Azure/GCP each
   bring up their shared/ paired jumpoint; **OCI does nothing here** (bring your own).
2. **AWS only** — ensure the shared on-demand **NAT instance** (`aws_nat_instance_enabled`)
   and **SSM interface endpoints** (`aws_ssm_endpoints_enabled`).
3. **Fetch the SSH public key** from the cloud's secret store and inject it (Linux via
   cloud-init / `admin_ssh_key` / `ssh-keys` metadata; Windows skips key injection).
4. **Launch the instance** (SDK).
5. **Layer 1** — broker the PRA **Shell Jump**.
6. **Layer 3** — Entitle SSH-ephemeral registration (opt-in).
7. **Layer 2** — Password Safe onboarding (opt-in).

VMs land in a **private** subnet with **no direct internet egress** and are reachable only
from the jumpoint (SSH/22); see [Cloud Sandbox](CLOUD_SANDBOX.md) for the per-cloud network
topology. Entry points: `/aws`, `/azure`, `/gcp`, `/oci` (per-cloud deploy + image browser)
and `/vms` (unified cross-cloud inventory).

---

## Provisioning — per cloud

Each cloud reads its credentials + a default subnet + an SSH-keypair secret from config
(emitted by the sandbox setup script). The **admin/SSH keypair** is stored in the cloud's
own secret store and retrievable per instance from the UI.

### AWS (EC2)

Sandbox: [`scripts/sandbox/Linux/setup-aws.sh`](../scripts/sandbox/Linux/setup-aws.sh).
Creates the VPC + a **private VM subnet** (`10.99.2.0/24`, local-only), the **VM security
group** (egress to the VPC only, ingress SSH/22 from the jumpoint SG), the **NAT** + SSM
endpoint SGs, a Secrets Manager **SSH keypair** secret, the ECS `bt-jumpoint` cluster, and
the scoped IAM user (`ec2:RunInstances/…`, `ec2:*KeyPair*`, `GetPasswordData`, `iam:PassRole`
for the SSM instance profile, and `ssm:SendCommand`/`GetCommandInvocation` for PS-SSM).

| Key | Default | Notes |
|---|---|---|
| `aws_region` | `us-east-2` | default region (Settings) |
| `ec2_ssh_key_secret` | — | Secrets Manager keypair secret (JSON) |
| `ec2_ssm_instance_profile` | — | instance profile attached at launch (SSM) |
| `aws_default_subnet_id` / `aws_default_security_group_id` | — | deploy-form default subnet + VM SG (import-only) |
| `aws_nat_instance_enabled` | `false` (sandbox `true`) | on-demand ref-counted NAT instance for VM egress |
| `aws_ssm_endpoints_enabled` | `false` (sandbox `true`) | on-demand SSM interface endpoints (private-subnet PS-SSM reach) |
| `aws_ecs_docker_deploy_key` + `bt_ecs_*` | — | shared jumpoint host (Layer 1) |

Deploy VMs into the **private** subnet. Enable the NAT instance if the VM needs outbound
internet (e.g. `apt`/`yum`). Windows AMIs are auto-detected (key injection skipped;
retrieve the password via `GET /api/aws/instances/{id}/ssh-key` / the console).

### Azure (VM)

Sandbox: [`scripts/sandbox/Linux/setup-azure.sh`](../scripts/sandbox/Linux/setup-azure.sh).
Creates the RG + VNet with a **vm-subnet** (`10.99.2.0/24`, NSG denies Internet egress,
allows VNet), an **aci-subnet** for the ACI jumpoint, a Key Vault **SSH keypair** secret,
and a service principal with **Contributor** on the RG.

| Key | Default | Notes |
|---|---|---|
| `azure_resource_group` / `azure_location` | `vm-cli-rg` / `centralus` | RG + default region |
| `azure_default_subnet_id` | — | deploy-form default VM subnet (import-only) |
| `azure_key_vault_url` / `azure_ssh_keypair_secret_name` | — / `azureVM-ssh-keypair` | SSH keypair secret |
| `azure_ssh_username` | `azureuser` | default Linux login |
| `azure_aci_subnet_id` / `azure_aci_docker_deploy_key` | — | ACI jumpoint (Layer 1) |

Windows is supported: the dashboard generates + vaults a local-admin password, retrievable
via `GET /api/azure/vms/{name}/admin-password`. Windows VMs use an **RDP jump**, not the
SSH Shell Jump.

### GCP (GCE)

Sandbox: [`scripts/sandbox/Linux/setup-gcp.sh`](../scripts/sandbox/Linux/setup-gcp.sh).
Creates a **vm-subnet** (`10.99.2.0/24`, **no** Cloud NAT → no internet egress) and a
**jumpoint-subnet** (Cloud NAT), a firewall `…-allow-ssh-from-jumpoint`, a Secret Manager
SSH keypair, and a service account. The dashboard **auto-attaches** `gcp_default_network_tag`
(`dashboard-sandbox-vm`) to every VM so the firewall applies.

| Key | Default | Notes |
|---|---|---|
| `gcp_project_id` / `gcp_region` / `gcp_zone` | — / `us-central1` / `us-central1-a` | project + default region/zone |
| `gcp_network` / `gcp_subnetwork` | `default` / — | VPC + VM subnet |
| `gcp_ssh_key_secret_name` | — | Secret Manager keypair secret |
| `gcp_ssh_username` | `gcp-user` | default Linux login |
| `gcp_jumpoint_subnetwork` / `gcp_cloud_run_docker_deploy_key` | — | per-VM COS jumpoint (Layer 1) |

Each GCP deploy spins up a **per-VM paired COS jumpoint** `bt-jumpoint-<vmname>`.

### OCI (Compute) — read the caveats

Sandbox: [`scripts/sandbox/Linux/setup-oci.sh`](../scripts/sandbox/Linux/setup-oci.sh).
Creates a compartment + VCN (`10.98.0.0/16`) with a **public subnet** (IGW, for your
jumpoint), a **vm-subnet** (`10.98.2.0/24`, NAT Gateway egress, no public IP), a scoped IAM
user + API keypair, and (best-effort) a KMS vault SSH-keypair secret.

| Key | Default | Notes |
|---|---|---|
| `oci_tenancy_ocid` / `oci_user_ocid` / `oci_fingerprint` / `oci_private_key` (+`_passphrase`) | — | API-signing identity |
| `oci_region` | `us-ashburn-1` | **all OCI deploys land here** regardless of the form's region |
| `oci_compartment_ocid` / `oci_vcn_ocid` / `oci_default_subnet_ocid` | — | compartment + VCN + vm-subnet |
| `oci_ssh_key_secret` / `oci_ssh_username` | — / `opc` | keypair secret + default login |
| `oci_freetier_enforce` | `true` | warn-and-confirm gate (below) |

> ⚠️ **OCI caveats.** (1) **No dashboard-provisioned jumpoint** — the deploy never ensures
> one; you must pre-create a PRA Jumpoint in the OCI public subnet and point
> `oci_bt_jump_group_name` / `oci_jumpoint_name` (or `bt_*`) at it. (2) **Region is fixed to
> `oci_region`.** (3) **Free-tier gate** — the form defaults to Always-Free
> (`VM.Standard.E2.1.Micro` / `A1.Flex`); a larger shape is rejected (HTTP 400) unless the
> request sets `acknowledge_charges=true`. (4) **SDK-only** (no Terraform VM module),
> Linux-only, no per-region config sets.

---

## Layer 1 — PRA (Shell Jump)

When `beyondtrust_enabled` and PRA is configured (`bt_api_host`, `bt_client_id`,
`bt_client_secret`, `bt_jump_group_name`, `bt_jumpoint_name`), every Linux deploy brokers a
PRA **Shell Jump** via `terraform_pra_service.provision_jump(tag=<cloud>)` (the `beyondtrust/sra`
provider), routed through the cloud's jumpoint host. The jump is removed on destroy from its
stored state.

Jump Group / Jumpoint resolution: per-deploy form `jump_group` / `jumpoint_name` → the
per-cloud override (`azure_bt_jump_group_name`/`azure_jumpoint_name`,
`gcp_bt_jump_group_name`/`gcp_jumpoint_name`, `oci_bt_jump_group_name`/`oci_jumpoint_name`) →
the `bt_*` defaults. AWS + Azure also accept a per-deploy `pra_credential_ref` (overrides
`bt_client_secret`). **Windows Azure VMs** skip the SSH jump — use an RDP jump.

The shared jumpoint host, deploy keys, and PRA OAuth setup are described in the
[BeyondTrust integration](integrations/beyondtrust.md) doc.

---

## Layer 2 — Password Safe (VM onboarding)

*Optional* (`passwordsafe_registration_enabled` + a per-deploy **"Onboard into Password
Safe"** toggle). Onboards the built VM as a Password Safe **managed system + managed
account** (the baked-in `adminuser`), so Password Safe rotates its credential. Per-cloud
method: **AWS `ssm`** (AWS Systems Manager plugin, DNS `{instance-id}:{region}`), **Azure
`azurevm`** (Azure VM SSH Rotation, address `tenant/sub/rg/vm`), **GCP `gcpvm`** (GCP VM SSH
Rotation, `projectId/zone/instance`), each with an `ssh` fallback. **OCI uses the `ssh`
method only** (no cloud-native plugin) and therefore needs SSH line-of-sight from a Resource
Broker / Jumpoint.

This is documented in full — plugin uploads, per-cloud methods, the `adminuser` account, and
the config-key table — in the [BeyondTrust integration](integrations/beyondtrust.md) doc's
**"Password Safe VM onboarding"** section. Off-boarding is automatic on VM destroy.

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

Requires `entitle_owner_id` + `entitle_workflow_id`. See the [Entitle integration](integrations/entitle.md)
doc. A separate **machine-identity JIT** track (the AWS `elevate()` wrapping of
`ec2_deploy`/`ec2_terminate`) is covered in [design/cloud-identity-jit.md](design/cloud-identity-jit.md).

---

## Images

Deploy from a stock marketplace/public image or one the dashboard's Packer flow built
(`/images/aws|azure|gcp`). The **BT-ready provisioners** under
[`provisioners/beyondtrust/`](../provisioners/beyondtrust/) harden sshd and create the
cloud-default `adminuser` login with passwordless sudo — the account both the Entitle
`sudo_user` and the Password Safe managed account rely on. Full build/promote/export flow is
in [image-management.md](image-management.md).

---

## Lifecycle & troubleshooting

- **Destroy** (`DELETE /api/{cloud}/instances|vms/{id}`) removes the instance, deregisters
  the PRA Shell Jump (from stored state), and off-boards Password Safe / Entitle if they were
  wired. AWS reclaims the shared NAT instance + SSM endpoints when the last VM is gone.
- **VM can't reach the internet** — by design (private subnet). On AWS enable
  `aws_nat_instance_enabled`; on OCI the vm-subnet already has a NAT Gateway; on GCP the VM
  subnet has no NAT (only the jumpoint subnet does).
- **Shell Jump shows Unavailable** — the jumpoint host didn't start; set the cloud's deploy
  key (`aws_ecs_docker_deploy_key` / `azure_aci_docker_deploy_key` /
  `gcp_cloud_run_docker_deploy_key`). On **OCI** you must supply your own jumpoint.
- **Can't SSH the VM** — the VM SG/NSG only allows SSH from the jumpoint; reach it through the
  PRA Shell Jump, not directly.
- **OCI deploy rejected (HTTP 400)** — a non-free-tier shape without `acknowledge_charges`;
  tick the acknowledge box or pick a free-tier shape.

For the sandbox network topology see [Cloud Sandbox](CLOUD_SANDBOX.md); for day-2 Ansible
against deployed VMs see [Config Management](config-management.md).
