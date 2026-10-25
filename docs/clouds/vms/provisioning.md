# Provisioning cloud VMs

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are deploying VMs on AWS, Azure, GCP or OCI: the deploy form, deploying several at once, each cloud's specifics, and the workstream defaults a deploy picks up.

Part of [Cloud VMs](../vms.md).

Each cloud reads its credentials + a default subnet + an SSH-keypair secret from config
(emitted by the sandbox setup script). The **admin/SSH keypair** is stored in the cloud's
own secret store and retrievable per instance from the UI.

## The deploy form

The single-VM deploy form on each cloud shows what most deploys decide: image, workstream,
name and count, size, region or location, subnet and security groups, and public IP. Below
those sit the **Add to BeyondTrust PRA** checkbox (see
[PRA is an opt-in](pra.md#pra-is-an-opt-in-on-the-deploy-form)) and a collapsed
**Advanced options** section holding everything that falls back to a configured default or
is off by default: the SSH key secret override, Entitle and Password Safe registration,
Active Directory and Entra ID join, the RDP jump, and the Gateway deploy key. Nothing was
removed — expand Advanced to reach any of it.

The bulk-deploy forms have the same **Add each VM to BeyondTrust PRA** checkbox. It starts
at the picked workstream's default and applies to every VM in the batch.

## Deploying more than one VM

Every deploy form has a **Count** (1–20). Leave it at 1 and nothing changes. Set it higher
and the base name is expanded into a numbered series — `web` × 3 becomes `web-01`,
`web-02`, `web-03` — with the form previewing the exact names before you submit.

A count above 1 creates one `*_bulk_deploy` **parent** job plus one `queued` child per VM,
all sharing a `batch_id`; the browser lands on `/jobs?batch_id=…`, which rolls the batch up
into total / running / failed. The children deploy **sequentially** inside a single worker
slot, so a batch of N takes roughly N × the single-deploy time and occupies one of the
`WORKER_REPLICAS` slots for the duration — that, plus default cloud vCPU quotas, is why the
ceiling is 20 (`MAX_DEPLOY_COUNT` in [`services/vm_naming.py`](../../../web_dashboard/services/vm_naming.py)).

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

## The two ways to deploy several VMs

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

Policy guardrails ([Policy Guardrails](../../operations/scheduling/policy-guardrails.md)) are enforced **per VM** on every
path — count batches and multi-select bulk included — before any job row is created.

## AWS (EC2)

Sandbox: [`scripts/sandbox/Linux/setup-aws.sh`](../../../scripts/sandbox/Linux/setup-aws.sh).
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
internet (e.g. `apt`/`yum`). Windows AMIs are auto-detected: the Administrator password is
recovered with a one-time key pair and stored outside the dashboard — see
[Windows servers](../windows-servers.md).

## Azure (VM)

Sandbox: [`scripts/sandbox/Linux/setup-azure.sh`](../../../scripts/sandbox/Linux/setup-azure.sh).
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
**Settings → Integrations → Privileged Remote Access → Shell Jump provisioning**, so the choice is reversible without a
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

Windows is supported: the dashboard generates a local-admin password and stores it in
Password Safe or a secret manager (never the dashboard database), and Windows VMs get a
**Shell Jump over OpenSSH**, with an RDP jump on request. They can also be joined to Entra ID — see
[Windows servers](../windows-servers.md).

## GCP (GCE)

Sandbox: [`scripts/sandbox/Linux/setup-gcp.sh`](../../../scripts/sandbox/Linux/setup-gcp.sh).
Creates a **vm-subnet** (`10.99.2.0/24`, **no** Cloud NAT → no internet egress by default)
and a **jumpoint-subnet** (Cloud NAT), a firewall `…-allow-ssh-from-jumpoint`, a Secret Manager
SSH keypair, and a service account. The dashboard **auto-attaches** `gcp_default_network_tag`
(`dashboard-sandbox-vm`) to every VM so the firewall applies — and, when `gcp_vm_nat_enabled`
is on, opens on-demand egress for the vm-subnet at deploy time (see below).

| Key | Default | Notes |
|---|---|---|
| `gcp_project_id` / `gcp_region` / `gcp_zone` | — / `us-central1` / `us-central1-a` | project + default region/zone |
| `gcp_network` / `gcp_subnetwork` | `default` / — | VPC + VM subnet |
| `gcp_ssh_key_secret_name` | — | Secret Manager keypair secret |
| `gcp_ssh_username` | `gcp-user` | default Linux login |
| `gcp_jumpoint_subnetwork` / `gcp_cloud_run_docker_deploy_key` | — | COS gateway subnet + deploy key (Layer 1) |
| `gcp_vm_jumpoint_mode` | `shared` | `shared` (one ref-counted host) or `paired` (an `e2-micro` per VM) |
| `gcp_vm_nat_enabled` | `true` | on-demand ref-counted Cloud NAT + egress rule for VM internet |
| `gcp_vm_nat_name` / `gcp_vm_egress_rule_name` | `dashboard-sandbox-vm-nat` / `dashboard-sandbox-vm-egress-ondemand` | names of the two on-demand resources |
| `gcp_vm_egress_rule_priority` | `900` | must beat the sandbox's priority-1000 egress deny (lower wins) |

### On-demand VM egress

The sandbox denies VM internet through **two independent gates**: the vm-subnet is left off
the shared Cloud NAT, *and* `…-deny-vm-egress` denies all egress at priority 1000 on the VM
network tag (a priority-999 rule allows the sandbox supernet back, which is why SSH still
works). Opening one gate alone changes nothing.

Rather than making the sandbox permanently open — which bills for egress infrastructure
serving VMs that don't exist — the dashboard opens both **by reference count**, the GCP
analog of `aws_nat_instance_enabled`. On the first VM deploy it adds a **second** Cloud NAT
gateway to the sandbox's existing Cloud Router (scoped to the vm-subnet's primary range) plus
a priority-900 egress ALLOW; when the last VM **in that region** is destroyed it removes both.
The count is region-scoped because each sandbox region has its own Cloud Router, so a VM in
one region never pins another region's gateway.

The sandbox's own NAT and deny rule are never modified — both halves are separately named,
additive resources. That matters more than it looks: `routers.patch` replaces the `nats` list
wholesale, so the existing gateways are read back and re-sent verbatim; dropping them would
cut the PRA Gateway's own egress, which is the SSH path to every VM. No-ops quietly when the
region has no Cloud Router, so non-sandbox projects are untouched.

GCP deploys borrow the **shared, ref-counted gateway host** that cloud databases, k8s
tunnels and VDI seats already use — one host, rather than an `e2-micro` per VM. Batches
always share. Single deploys follow `gcp_vm_jumpoint_mode` (`shared` by default,
`paired` for the pre-2026-07 behaviour of a dedicated `bt-jumpoint-<vmname>`), editable
under **Settings → Integrations → Privileged Remote Access → Shell Jump provisioning** so the choice is reversible without a
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

## OCI (Compute) — read the caveats

Sandbox: [`scripts/sandbox/Linux/setup-oci.sh`](../../../scripts/sandbox/Linux/setup-oci.sh).
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
> Linux-only, no per-region config sets. (5) **The shape has to exist in your availability
> domain and the image has to support it** — OCI does not offer every shape in every AD of a
> region (the free AMD micro `VM.Standard.E2.1.Micro` exists only in the older ones;
> `us-chicago-1` offers no E2 shape at all), and an image boots only on the shapes it was
> built for (the other free shape, `A1.Flex`, is Ampere — it needs an `aarch64` image). So
> the shape list is **scoped to the AD and the image**, and changing either refetches the
> picker (deploy form and Packer build form both); a shape the new scope no longer offers is
> **cleared**, not substituted, and the form says why. The picker narrows through the same
> lookup as `oci_service.check_launch_placement`, and **every entry point gates on that
> lookup too** — the Packer build route and runner, plus `/api/oci/deploy` and
> `/api/oci/bulk-deploy` — so an unlaunchable pairing is refused with HTTP 400
> `shape_not_launchable`, naming the shape and listing what would work, instead of becoming
> a bare `404 NotAuthorizedOrNotFound` — or a `400 InvalidParameter` naming shape and image —
> a second into the job. That gate is what covers the
> paths the picker doesn't: an API client bypassing the form, and the bulk modal, whose list
> is not scoped at all (no AD picker; one shared shape across N images) — there **every**
> selected image is checked against the shared shape before anything is created.
