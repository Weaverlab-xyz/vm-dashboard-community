# Cloud VMs

The dashboard deploys **cloud virtual machines** across AWS, Azure, GCP, and OCI, then
layers the BeyondTrust PAM stack on top — the same **provisioning + three layers** model
as [Databases](databases.md) and [Kubernetes](kubernetes.md):

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
| **OCI** | Compute (Linux) | ✅ (bring your own gateway¹) | ⚠️ `ssh` method only | ✅ SSH ephemeral |

¹ OCI has no dashboard-provisioned gateway — you supply your own (see the OCI section).

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

1. **Ensure the gateway host** (only when `beyondtrust_enabled`) — AWS uses a shared
   ref-counted ECS host, Azure the shared `clouddb-jumpoint` VM (see
   `azure_vm_jumpoint_mode`), GCP the shared COS host (see `gcp_vm_jumpoint_mode`);
   **OCI does nothing here** (bring your own). In a batch this happens once for the
   whole run.
2. **AWS only** — ensure the shared on-demand **NAT instance** (`aws_nat_instance_enabled`)
   and **SSM interface endpoints** (`aws_ssm_endpoints_enabled`).
3. **Fetch the SSH public key** from the cloud's secret store and inject it (Linux via
   cloud-init / `admin_ssh_key` / `ssh-keys` metadata; Windows skips key injection).
4. **Launch the instance** (SDK).
5. **Layer 1** — broker the PRA **Shell Jump**.
6. **Layer 3** — Entitle SSH-ephemeral registration (opt-in).
7. **Layer 2** — Password Safe onboarding (opt-in).

VMs land in a **private** subnet with **no direct internet egress** and are reachable only
from the gateway (SSH/22); see [Cloud Sandbox](CLOUD_SANDBOX.md) for the per-cloud network
topology. Entry points: `/aws`, `/azure`, `/gcp`, `/oci` (per-cloud deploy + image browser)
and `/vms` (unified cross-cloud inventory).

---

## Provisioning — per cloud

Each cloud reads its credentials + a default subnet + an SSH-keypair secret from config
(emitted by the sandbox setup script). The **admin/SSH keypair** is stored in the cloud's
own secret store and retrievable per instance from the UI.

### Deploying more than one VM

Every deploy form has a **Count** (1–20). Leave it at 1 and nothing changes. Set it higher
and the base name is expanded into a numbered series — `web` × 3 becomes `web-01`,
`web-02`, `web-03` — with the form previewing the exact names before you submit.

A count above 1 creates one `*_bulk_deploy` **parent** job plus one `queued` child per VM,
all sharing a `batch_id`; the browser lands on `/jobs?batch_id=…`, which rolls the batch up
into total / running / failed. The children deploy **sequentially** inside a single worker
slot, so a batch of N takes roughly N × the single-deploy time and occupies one of the
`WORKER_REPLICAS` slots for the duration — that, plus default cloud vCPU quotas, is why the
ceiling is 20 (`MAX_DEPLOY_COUNT` in [`services/vm_naming.py`](../web_dashboard/services/vm_naming.py)).

Names are expanded by truncating the *base*, never the numeric suffix, so a series stays
unique at any provider's length limit. Two limits are worth knowing:

* **Azure batches are budgeted to 15 characters**, not the 64-char ARM limit, because the
  in-guest hostname is derived as `vm_name[:15]`. A longer base would give every VM in the
  batch the same hostname, which breaks Entitle and Password Safe onboarding — both key off
  hostname. Single deploys are unaffected.
* **GCP names must be RFC1035** (lowercase, leading letter, hyphens); a base that isn't is
  rejected with a 400 rather than silently rewritten.

Names are checked against VMs the dashboard has already deployed *or is deploying*, and a
clash returns **409** rather than creating anything. That matters because Azure, GCP and OCI
resolve a destroy by first match on name, so duplicates would make a later teardown
ambiguous.

### The two ways to deploy several VMs

All four clouds offer both, and they are different operations:

| | **Count** (on the deploy form) | **Bulk Deploy** (on the image list) |
|---|---|---|
| What it makes | N copies of **one** image | one VM **per selected image** |
| Names | auto-numbered from a base | typed per VM |
| Where | the deploy modal | tick images, then the *Bulk Deploy (n)* button |

Both produce the same job shape — one `*_bulk_deploy` parent plus one `queued` child per
VM, sharing a `batch_id` — so both land on the `/jobs?batch_id=` rollup and both share a
single Gateway for the run.

Use Count for "five identical lab boxes"; use Bulk Deploy for "one each of these three
images". GCP and OCI gained Bulk Deploy after AWS and Azure, so older screenshots may show
their image lists without checkboxes.

Policy guardrails ([Policy Guardrails](policy-guardrails.md)) are enforced **per VM** on every
path — count batches and multi-select bulk included — before any job row is created.

### AWS (EC2)

Sandbox: [`scripts/sandbox/Linux/setup-aws.sh`](../scripts/sandbox/Linux/setup-aws.sh).
Creates the VPC + a **private VM subnet** (`10.99.2.0/24`, local-only), the **VM security
group** (egress to the VPC only, ingress SSH/22 from the gateway SG), the **NAT** + SSM
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
| `aws_ecs_docker_deploy_key` + `bt_ecs_*` | — | shared gateway host (Layer 1) |

Deploy VMs into the **private** subnet. Enable the NAT instance if the VM needs outbound
internet (e.g. `apt`/`yum`). Windows AMIs are auto-detected (key injection skipped;
retrieve the password via `GET /api/aws/instances/{id}/ssh-key` / the console).

### Azure (VM)

Sandbox: [`scripts/sandbox/Linux/setup-azure.sh`](../scripts/sandbox/Linux/setup-azure.sh).
Creates the RG + VNet with a **vm-subnet** (`10.99.2.0/24`, NSG denies Internet egress,
allows VNet), an **aci-subnet** for the ACI gateway, a Key Vault **SSH keypair** secret,
and a service principal with **Contributor** on the RG.

| Key | Default | Notes |
|---|---|---|
| `azure_resource_group` / `azure_location` | `vm-cli-rg` / `centralus` | RG + default region |
| `azure_default_subnet_id` | — | deploy-form default VM subnet (import-only) |
| `azure_key_vault_url` / `azure_ssh_keypair_secret_name` | — / `azureVM-ssh-keypair` | SSH keypair secret |
| `azure_ssh_username` | `azureuser` | default Linux login |
| `azure_aci_subnet_id` / `azure_aci_docker_deploy_key` | — | ACI gateway (Layer 1) |
| `azure_jumpoint_subnet_id` | — | subnet for the shared VM gateway (falls back to `azure_aci_subnet_id`) |
| `azure_vm_jumpoint_mode` | `shared` | `shared` (the ref-counted `clouddb-jumpoint` VM) or `aci` (a container group per VM) |

Azure single deploys borrow the **shared, ref-counted gateway VM** that cloud databases,
k8s tunnels and VDI seats already use, following `azure_vm_jumpoint_mode` (editable under
**Settings → BeyondTrust → Azure overrides**, so the choice is reversible without a
redeploy). Batches still share one ACI container group. Two things override the mode: a
deploy supplying its own **Gateway deploy key** always gets ACI (the shared host resolves
its key from config, so there is nowhere to honour a per-deploy override), and `aci` mode
restores the pre-2026-07 per-deploy container.

`shared` is the default because ACI has two limits a real VM does not:

* **No protocol tunneling.** ACI is serverless and cannot grant `NET_ADMIN` / `NET_RAW` /
  `IPC_LOCK` or `/dev/net/tun`, so an ACI-brokered VM gets a Shell Jump but never a
  Protocol Tunnel. The shared VM runs the container privileged (`azure_service.run_vm_jumpoint`).
* **One shared identity store.** Every ACI group gets a random name
  (`bt-jumpoint-azure-<uuid8>`) but they all mount the same `/jpt` Azure File share, which
  is where the Gateway persists its identity. Successive groups contend over that one
  install, and once the `.installed-<key-hash>` marker disagrees with what is on disk the
  container **crash-loops** (`ExitCode 1`, no log output) and never registers with PRA. The
  Containers page still shows it *Running*, because that is the ACI **group** state — check
  `containers[0].instanceView.currentState` for `CrashLoopBackOff`. Recovery: empty the
  `jpt` share so the next container reinstalls clean.

Which shape a VM used is recorded as `jumpoint_mode` on its deploy job; destroy releases a
shared reference and lets `jumpoint_host_service` decide, or stops the ACI group when no
sibling VM still references it.

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
| `gcp_jumpoint_subnetwork` / `gcp_cloud_run_docker_deploy_key` | — | COS gateway subnet + deploy key (Layer 1) |
| `gcp_vm_jumpoint_mode` | `shared` | `shared` (one ref-counted host) or `paired` (an `e2-micro` per VM) |

GCP deploys borrow the **shared, ref-counted gateway host** that cloud databases, k8s
tunnels and VDI seats already use — one host, rather than an `e2-micro` per VM. Batches
always share. Single deploys follow `gcp_vm_jumpoint_mode` (`shared` by default,
`paired` for the pre-2026-07 behaviour of a dedicated `bt-jumpoint-<vmname>`), editable
under **Settings → BeyondTrust → GCP overrides** so the choice is reversible without a
redeploy.

Two things override the mode. A deploy that supplies its own **Gateway deploy key** is
always paired — the shared host resolves its key from config, so there is nowhere to
honour a per-deploy override on it. And the shared host lands on the
`jumpoint_subnetwork` (the only sandbox subnet with Cloud NAT) rather than the VM
subnet; reachability is unaffected either way, because the sandbox SSH rule is
tag-based (`--source-tags bt-jumpoint`) and so applies VPC-wide.

Which shape a VM used is recorded as `jumpoint_mode` on its deploy job, and destroy
handles both: a paired gateway is deleted once no sibling VM references it, a shared
one only has its reference released. Rows predating the field are inferred as paired,
so no migration is needed.

### OCI (Compute) — read the caveats

Sandbox: [`scripts/sandbox/Linux/setup-oci.sh`](../scripts/sandbox/Linux/setup-oci.sh).
Creates a compartment + VCN (`10.98.0.0/16`) with a **public subnet** (IGW, for your
gateway), a **vm-subnet** (`10.98.2.0/24`, NAT Gateway egress, no public IP), a scoped IAM
user + API keypair, and (best-effort) a KMS vault SSH-keypair secret.

| Key | Default | Notes |
|---|---|---|
| `oci_tenancy_ocid` / `oci_user_ocid` / `oci_fingerprint` / `oci_private_key` (+`_passphrase`) | — | API-signing identity |
| `oci_region` | `us-ashburn-1` | **all OCI deploys land here** regardless of the form's region |
| `oci_compartment_ocid` / `oci_vcn_ocid` / `oci_default_subnet_ocid` | — | compartment + VCN + vm-subnet |
| `oci_ssh_key_secret` / `oci_ssh_username` | — / `opc` | keypair secret + default login |
| `oci_freetier_enforce` | `true` | warn-and-confirm gate (below) |

> ⚠️ **OCI caveats.** (1) **No dashboard-provisioned gateway** — the deploy never ensures
> one; you must pre-create a PRA Gateway in the OCI public subnet and point
> `oci_bt_jump_group_name` / `oci_jumpoint_name` (or `bt_*`) at it. (2) **Region is fixed to
> `oci_region`.** (3) **Free-tier gate** — the form defaults to Always-Free
> (`VM.Standard.E2.1.Micro` / `A1.Flex`); a larger shape is rejected (HTTP 400) unless the
> request sets `acknowledge_charges=true`. The gate is evaluated over the whole request,
> so a **Count** that would exceed the envelope trips it even when each VM is individually
> free — three free micros is one more than the tier allows. Changing the count clears any
> acknowledgment you had already ticked. (4) **SDK-only** (no Terraform VM module),
> Linux-only, no per-region config sets. (5) **The shape list is scoped to the availability
> domain and the image** — OCI does not offer every shape in every AD of a region, and an
> image boots only on the shapes it was built for, so changing either refetches the picker
> (deploy form and Packer build form both). A shape the new scope no longer offers is
> **cleared**, not substituted, and the form says why; pick one from the narrowed list. It
> narrows through the same lookup as `oci_service.check_launch_placement`, so the picker never
> offers a shape that gate would refuse. **But that gate only runs on the Packer build path**
> (`api/packer.py`, `packer_build_service.py`) — `/api/oci/deploy` and `/api/oci/bulk-deploy`
> do **not** precheck placement, so on those two the picker is the only guard and an API
> client bypassing the form still gets the bare `404 NotAuthorizedOrNotFound`. The bulk modal's
> shape list is not scoped at all (no AD picker; one shared shape across N images).

---

## Layer 1 — PRA (Shell Jump)

When `beyondtrust_enabled` and PRA is configured (`bt_api_host`, `bt_client_id`,
`bt_client_secret`, `bt_jump_group_name`, `bt_jumpoint_name`), every Linux deploy brokers a
PRA **Shell Jump** via `terraform_pra_service.provision_jump(tag=<cloud>)` (the `beyondtrust/sra`
provider), routed through the cloud's gateway host. The jump is removed on destroy from its
stored state.

Jump Group / Gateway resolution: per-deploy form `jump_group` / `jumpoint_name` → the
per-cloud override (`azure_bt_jump_group_name`/`azure_jumpoint_name`,
`gcp_bt_jump_group_name`/`gcp_jumpoint_name`, `oci_bt_jump_group_name`/`oci_jumpoint_name`) →
the `bt_*` defaults. AWS + Azure also accept a per-deploy `pra_credential_ref` (overrides
`bt_client_secret`). **Windows Azure VMs** skip the SSH jump — use an RDP jump.

The shared gateway host, deploy keys, and PRA OAuth setup are described in the
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
Broker / Gateway.

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
  subnet has no NAT (only the gateway subnet does).
- **Shell Jump shows Unavailable** — the gateway host didn't start; set the cloud's deploy
  key (`aws_ecs_docker_deploy_key` / `azure_aci_docker_deploy_key` /
  `gcp_cloud_run_docker_deploy_key`). On **OCI** you must supply your own gateway.
- **Can't SSH the VM** — the VM SG/NSG only allows SSH from the gateway; reach it through the
  PRA Shell Jump, not directly.
- **OCI deploy rejected (HTTP 400)** — a non-free-tier shape without `acknowledge_charges`;
  tick the acknowledge box or pick a free-tier shape. With a **Count**, the whole batch is
  measured against the envelope, so this can fire on a shape that is free on its own.
- **OCI Shape reset itself to “— pick a shape —”** — expected: the shape list is scoped to
  the selected availability domain and image, and the one you had isn't offered in the new
  scope. The note under the picker names it. Choose from the narrowed list; the form won't
  submit until you do.
- **OCI shape picker is empty** — OCI returned no shapes for that availability domain at all
  (a failed `ListShapes` looks identical to an empty one). Try another AD, or **Refresh**. The
  *image* narrowing can't cause this: when the image/AD intersection comes back empty, or the
  tenancy's policy blocks `ListImageShapeCompatibilityEntries`, it falls open to the full AD
  list — deliberately matching `check_launch_placement`, so the picker never offers less than
  the API accepts. In that fallen-open state the advisory architecture warning is your only
  hint, so mind it.
- **Deploy rejected (HTTP 409, `vm_name_collision`)** — the names this deploy would create
  are already taken by VMs the dashboard deployed or is deploying. Pick a different base
  name, or destroy the existing VMs first. The check is deliberately strict: Azure, GCP and
  OCI resolve a destroy by first match on name, so duplicates make teardown ambiguous.
- **A batch child is stuck `queued`** — children are created unclaimable on purpose and are
  driven by their `*_bulk_deploy` parent. Check the parent (same `batch_id`): if it failed
  or was reconciled away, its children have nothing to drive them.

For the sandbox network topology see [Cloud Sandbox](CLOUD_SANDBOX.md); for day-2 Ansible
against deployed VMs see [Config Management](config-management.md).
