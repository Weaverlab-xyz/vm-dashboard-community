# Remote Worker (Ansible + Kubernetes runners)

> **Formerly "Ansible."** The Settings panel is now
> **Configuration → Remote Worker**. The doc path
> (`docs/integrations/ansible.md`) is unchanged so existing links resolve.

## What is it?

The **Remote Worker** panel configures the dashboard's two off-host
runners. Both run their work as a **one-shot cloud task** — a container
launched in the target cloud, run once, and destroyed when it exits — and
both **share the same per-cloud network settings** (cluster, subnet,
security group, role, ACR, VPC connector). The image-promote runner reuses
those same settings too (see [Shared cloud infrastructure](#shared-cloud-infrastructure)).

| Runner | Config key | What it runs | Backends |
|---|---|---|---|
| **Ansible runner** | `ansible_runner` | Config-management playbooks (`.yml`) and wrapped `.sh`/`.ps1`/`.rpm`/`.deb` assets on VMs over SSH / WinRM | `local` \| `ecs` \| `aci` \| `gcp` |
| **Kubernetes runner** | `k8s_runner` | Cluster-API ops — `kubectl apply/delete`, `helm …`, `kubectl get secret` (entitle agent install, External Secrets Operator, mgmt-plane) | `local` \| `ecs` \| `aci` \| `gcp` |

The four backends:

| Backend | Where the task runs |
|---|---|
| **`local`** | In/alongside the dashboard host. Ansible: a sibling container via the mounted Docker socket. Kubernetes: in-process via `k8s_service`. |
| **`ecs`** | AWS ECS Fargate task |
| **`aci`** | Azure Container Instance |
| **`gcp`** | GCP Cloud Run Job |

**Why a cloud runner?** Two independent reasons:

- **Private subnets** — the target VM (Ansible) or cluster API (Kubernetes)
  has no route back to the dashboard host. A task launched *in* the target
  cloud sits on the right network.
- **Corp proxy** — a corporate egress proxy (e.g. Cloudflare SSL inspection)
  `526`s direct `kubectl`/`helm` calls to a cluster API that presents a
  private-CA cert. A one-shot cloud task has **clean egress** and side-steps
  the proxy.

> **Read these first:**
> - [`docs/config-management.md`](../config-management.md) — philosophy,
>   best practices, the security argument for one-shot runners, and where
>   SaaS extends this.
> - [`docs/storage-management.md`](../storage-management.md) — full
>   reference for the four storage backends (AWS S3, Azure Blob, GCS,
>   Local / UNC) the **Ansible** runner pulls assets from, and the migrate flow.
> - [`runners/promote/README.md`](../../runners/promote/README.md) — the
>   image-promote runner, which shares this panel's per-cloud infra.
>
> This page is the *integration-specific* guide: the config-field
> reference, the two runners, the shared cloud infrastructure and its
> fallback chains, per-cloud prerequisites, the Ansible playbook how-to,
> and troubleshooting.

**Storage and execution targets are independent (Ansible runner).** You can
store assets in S3 and run them against on-premises Proxmox hosts, or store
them on a corporate UNC share and target EC2 instances — any combination
works (with one constraint: cloud runners can't read from a UNC backend; see
[storage-management.md](../storage-management.md#constraint-local-backend-only-works-with-the-local-ansible-runner)).
The Kubernetes runner has no storage dependency — it streams manifests over
stdin.

---

## Config panel field reference

Every field on **Configuration → Remote Worker**, grouped as the panel
groups them. Values are stored in the dashboard's config-service store and
can also be set via the matching environment variable (the env name is the
config key upper-cased — pydantic `BaseSettings`, no prefix). Defaults and
meanings are taken from `web_dashboard/config.py`.

### Runner backends

| Panel label | Config key | Env var | Default | Meaning |
|---|---|---|---|---|
| Ansible runner | `ansible_runner` | `ANSIBLE_RUNNER` | `local` | Where Ansible playbooks execute: `local` (Docker sibling) \| `ecs` \| `aci` \| `gcp`. |
| Kubernetes runner | `k8s_runner` | `K8S_RUNNER` | `local` | Where cluster-API ops execute: `local` (in-process) \| `ecs` \| `aci` \| `gcp`. |

### Shared cloud infrastructure — AWS / ECS

These knobs are reused by the Ansible runner, the Kubernetes runner, **and**
the image-promote runner (see [Shared cloud infrastructure](#shared-cloud-infrastructure)).

| Panel label | Config key | Env var | Default | Meaning |
|---|---|---|---|---|
| ECS cluster | `ansible_ecs_cluster` | `ANSIBLE_ECS_CLUSTER` | `bt-jumpoint` | ECS cluster the Fargate task lands in. Shares the cluster with the BT Jumpoint by default. |
| ECS task family | `ansible_ecs_task_family` | `ANSIBLE_ECS_TASK_FAMILY` | `ansible-config-mgmt` | Task-definition family for the **Ansible** task (the k8s task uses its own `k8s-runner` family). Auto-registered on first run. |
| ECS subnet ID | `ansible_ecs_subnet_id` | `ANSIBLE_ECS_SUBNET_ID` | _(empty)_ | Fargate task subnet. A VPC private subnet is recommended; it must have egress to the target. |
| ECS security group IDs | `ansible_ecs_security_group_ids` | `ANSIBLE_ECS_SECURITY_GROUP_IDS` | _(empty)_ | Comma-separated security-group IDs (optional). |
| ECS execution role ARN | `ansible_ecs_execution_role_arn` | `ANSIBLE_ECS_EXECUTION_ROLE_ARN` | _(empty)_ | ECS **execution** role (image pull from a private ECR + CloudWatch log write). Required for private-registry images. |
| ECS CPU | `ansible_ecs_cpu` | `ANSIBLE_ECS_CPU` | `256` | Fargate vCPU units. |
| ECS memory | `ansible_ecs_memory` | `ANSIBLE_ECS_MEMORY` | `512` | Fargate memory (MiB). |

> The AWS region comes from the dashboard's AWS config (`aws_region`,
> default `us-east-1`), not a Remote-Worker field.

### Shared cloud infrastructure — Azure / ACI

| Panel label | Config key | Env var | Default | Meaning |
|---|---|---|---|---|
| ACI subnet ID | `ansible_aci_subnet_id` | `ANSIBLE_ACI_SUBNET_ID` | _(empty)_ | Subnet ARM ID for ACI VNet injection (so the container group can reach private targets). |
| ACR server | `ansible_aci_acr_server` | `ANSIBLE_ACI_ACR_SERVER` | _(empty)_ | Private ACR login server (e.g. `myregistry.azurecr.io`). Only needed when the runner image is hosted in a private ACR. |
| ACR username | `ansible_aci_acr_username` | `ANSIBLE_ACI_ACR_USERNAME` | _(empty)_ | ACR username / service-principal appId for the image pull. |
| ACR password | `ansible_aci_acr_password` | `ANSIBLE_ACI_ACR_PASSWORD` | _(empty)_ | ACR password / SP secret (encrypted at rest). |

> The ACI **resource group** and **location** come from the Azure config
> (`azure_resource_group`, default `vm-cli-rg`; `azure_location`, default
> `centralus`) — there are no separate Remote-Worker fields for them. Azure
> SP credentials (`azure_client_id` / `_secret` / `_tenant_id` /
> `_subscription_id`) are inherited from the Azure config.

### Shared cloud infrastructure — GCP / Cloud Run

| Panel label | Config key | Env var | Default | Meaning |
|---|---|---|---|---|
| Cloud Run region | `gcp_ansible_cloud_run_region` | `GCP_ANSIBLE_CLOUD_RUN_REGION` | _(empty → falls back to `gcp_region`)_ | Region the Cloud Run Job runs in. |
| VPC connector | `gcp_ansible_vpc_connector` | `GCP_ANSIBLE_VPC_CONNECTOR` | _(empty)_ | Serverless VPC Access connector resource name, for reaching private RFC-1918 targets. Optional. |

> The GCP **project** comes from `gcp_project_id` and the region falls back
> to `gcp_region` (default `us-central1`) — both from the GCP config.

### Ansible runner details

| Panel label | Config key | Env var | Default | Meaning |
|---|---|---|---|---|
| AWS SSH user | `ansible_aws_user` | `ANSIBLE_AWS_USER` | `ec2-user` | Default SSH username for `aws:` targets (Amazon Linux). Per-job editable; pre-filled from this. |
| Azure SSH user | `ansible_azure_user` | `ANSIBLE_AZURE_USER` | `azureuser` | Default SSH username for `azure:` targets. Per-job editable. |
| GCP SSH user | `ansible_gcp_user` | `ANSIBLE_GCP_USER` | `gcp-user` | Default SSH username for `gcp:` targets. Per-job editable. |
| ACI runner image | `ansible_aci_image` | `ANSIBLE_ACI_IMAGE` | `willhallonline/ansible:latest` | Ansible image the ACI task pulls. |
| Cloud Run runner image | `gcp_ansible_image` | `GCP_ANSIBLE_IMAGE` | `willhallonline/ansible:latest` | Ansible image the Cloud Run Job pulls. |
| ACI SSH key secret name | `ansible_aci_ssh_key_secret_name` | `ANSIBLE_ACI_SSH_KEY_SECRET_NAME` | _(empty)_ | Azure Key Vault secret name holding the Ansible SSH private key for Azure VM targets. |

> The ECS Ansible image is `ansible_ecs_image` (env `ANSIBLE_ECS_IMAGE`,
> default `willhallonline/ansible:latest`). The local runner image is
> `ansible_local_image` (env `ANSIBLE_LOCAL_IMAGE`). The AWS SSH key for
> EC2 targets comes from `ansible_ssh_key_sm_name` (env
> `ANSIBLE_SSH_KEY_SM_NAME`, default `ec2/ssh-keypair`) — see
> [Cloud VM SSH keys](#cloud-vm-ssh-keys-ansible-runner). The final-fallback
> username for an unrecognised cloud tag is `ansible_default_user`
> (default `ec2-user`).

### Kubernetes runner

| Panel label | Config key | Env var | Default | Meaning |
|---|---|---|---|---|
| Kubernetes runner | `k8s_runner` | `K8S_RUNNER` | `local` | `local` (in-process) \| `ecs` \| `aci` \| `gcp`. See [Kubernetes runner](#kubernetes-runner). |
| Kubernetes runner image | `k8s_runner_image` | `K8S_RUNNER_IMAGE` | `dtzar/helm-kubectl:latest` | Stock kubectl+helm image the cloud task runs. No custom image is needed. |

---

## Shared cloud infrastructure

The Ansible runner, the Kubernetes runner, and the image-promote runner
**all reuse the same per-cloud cluster / subnet / SG / role / ACR / VPC
settings.** Set them once and all three pick them up. Each runner reads the
shared `ansible_*` (and Azure/GCP) keys directly, with the promote runner
adding its own optional `promote_runner_*` overrides on top.

### What each runner reads

| Cloud | Ansible runner reads | Kubernetes runner reads | Image-promote runner reads |
|---|---|---|---|
| **AWS / ECS** | `ansible_ecs_cluster`, `ansible_ecs_task_family`, `ansible_ecs_image`, `ansible_ecs_cpu`, `ansible_ecs_memory`, `ansible_ecs_subnet_id`, `ansible_ecs_security_group_ids`, `ansible_ecs_execution_role_arn`, `aws_region` | `ansible_ecs_cluster`, `ansible_ecs_cpu`, `ansible_ecs_memory`, `ansible_ecs_subnet_id`, `ansible_ecs_security_group_ids`, `ansible_ecs_execution_role_arn`, `aws_region` (own task family `k8s-runner`, own image `k8s_runner_image`) | `promote_runner_ecs_*` → falls back to `ansible_ecs_*` |
| **Azure / ACI** | `azure_resource_group`, `azure_location`, `ansible_aci_subnet_id`, `ansible_aci_image`, `ansible_aci_acr_server/username/password` | `azure_resource_group`, `azure_location`, `ansible_aci_subnet_id`, `ansible_aci_acr_server/username/password` (own image `k8s_runner_image`) | `promote_runner_azure_*` → falls back to `azure_resource_group` / `azure_location` |
| **GCP / Cloud Run** | `gcp_project_id`, `gcp_ansible_cloud_run_region` (→ `gcp_region`), `gcp_ansible_image`, `gcp_ansible_vpc_connector` | `gcp_project_id`, `gcp_region` (→ `gcp_ansible_cloud_run_region`), `gcp_ansible_vpc_connector` (own image `k8s_runner_image`) | `promote_runner_gcp_*` → falls back to `gcp_region` / `storage_gcs_*` |

### Fallback chains

- **Image-promote → Ansible (AWS):** `promote_runner_ecs_cluster` →
  `ansible_ecs_cluster`; `promote_runner_ecs_subnet_id` →
  `ansible_ecs_subnet_id`; `promote_runner_ecs_security_group_ids` →
  `ansible_ecs_security_group_ids`; `promote_runner_ecs_execution_role_arn`
  → `ansible_ecs_execution_role_arn`. (The promote runner additionally
  needs a **task role** with S3 write — that one has no Ansible equivalent
  because the Ansible runner doesn't stage to S3.)
- **Kubernetes runner (ECS):** reuses `ansible_ecs_*` (cluster, subnet, SG,
  execution role, cpu, memory) + `aws_region`; only the task family
  (`k8s-runner`) and image (`k8s_runner_image`) differ.
- **Kubernetes runner (ACI):** reuses `azure_resource_group` /
  `azure_location` / `ansible_aci_subnet_id` /
  `ansible_aci_acr_server/username/password`.
- **Kubernetes runner (GCP):** reuses `gcp_project_id` / `gcp_region`
  (or `gcp_ansible_cloud_run_region`) / `gcp_ansible_vpc_connector`.

**The takeaway:** configure the ECS cluster + subnet + SG + role once (or
the ACI subnet + ACR, or the GCP region + VPC connector once), and the
Ansible runner, the Kubernetes runner, and image-promote all use it. The
`promote_runner_*` keys exist only for installs that want the promote task
on *different* infra than config-mgmt — most single-tenant installs leave
them blank. See [`runners/promote/README.md`](../../runners/promote/README.md)
for the full promote-runner key list.

---

## Kubernetes runner

`k8s_runner` controls how the dashboard runs **cluster-API operations** —
`kubectl apply`, `kubectl delete`, `helm repo add`/`helm upgrade`,
`kubectl get secret`. These back the entitle agent install, the External
Secrets Operator (ESO) rollout, and mgmt-plane operations.

| Mode | How it runs |
|---|---|
| `local` (default) | In-process, via `k8s_service`'s subprocess helpers running `kubectl`/`helm` directly from the dashboard container. |
| `ecs` / `aci` / `gcp` | A one-shot stock `dtzar/helm-kubectl` task in the chosen cloud. The dashboard token-preps the kubeconfig server-side (swaps the cloud exec-auth block for a static bearer token), base64-encodes it into a secure env var, and pipes any secret-bearing manifest to the task over stdin — so the throwaway container needs **no cloud CLIs and no cloud credentials**. |

### When to use a cloud backend

Use `ecs` / `aci` / `gcp` when **direct `kubectl`/`helm` from the dashboard
host fails because of a corporate egress proxy.** The common symptom is a
Cloudflare-style **HTTP 526** ("invalid SSL certificate") when the proxy
inspects TLS to a cluster API server that presents a **private-CA** cert it
won't trust. A one-shot cloud task has clean egress to the cluster API and
side-steps the proxy entirely.

(The same private-subnet reasoning as the Ansible runner also applies: a
cloud task can reach a cluster API that has no route back to the dashboard
host.)

### Reachability caveat

The cloud task talks to the cluster's **public** API endpoint over the
bearer token in the prepped kubeconfig. The task still needs that endpoint
to be reachable from the cloud-runner network:

- The cluster API must have a **public endpoint** (or one reachable from the
  runner's subnet / VPC connector).
- If the cluster restricts the API to **authorized CIDRs / IP allow-lists**,
  add the runner's egress (the Fargate task's public IP or NAT range, the
  ACI subnet, or the Cloud Run VPC-connector egress) to that allow-list, or
  the task's `kubectl` calls will time out.

### Configuration

Pick the mode in **Configuration → Remote Worker → Kubernetes runner** and,
if it's a cloud mode, make sure the [shared cloud infrastructure](#shared-cloud-infrastructure)
for that cloud is set (the k8s runner reuses the Ansible runner's ECS / ACI /
Cloud Run network plumbing). Override the image only if you mirror
`dtzar/helm-kubectl` to a private registry — set `k8s_runner_image`.

---

## Per-cloud prerequisites

Only needed for the cloud backends (`ecs` / `aci` / `gcp`). The local
backends need nothing beyond the Docker socket (Ansible) or in-container
`kubectl`/`helm` (Kubernetes).

### AWS (ECS Fargate)

- **ECS Fargate cluster** the dashboard can `run-task` against
  (`ansible_ecs_cluster`; reuses the BT Jumpoint cluster by default).
- **Task execution role** (`ansible_ecs_execution_role_arn`) with
  `service-role/AmazonECSTaskExecutionRolePolicy` — this is what ECS uses to
  **pull the image** (from a private ECR) and **write CloudWatch logs**. It
  is *not* the role the container code runs as. Required for private-registry
  images; can be blank if the image is public.
- **Task role** vs **execution role:** the Ansible and Kubernetes runners
  need only the *execution* role — neither container makes signed AWS API
  calls (Ansible SSHes to the VM; the k8s task uses a bearer-token
  kubeconfig). The image-**promote** runner additionally needs a *task role*
  (the identity the container assumes via the task metadata endpoint) with
  `s3:PutObject` on the staging bucket; see
  [`runners/promote/README.md`](../../runners/promote/README.md).
- **Subnet + security group** (`ansible_ecs_subnet_id`,
  `ansible_ecs_security_group_ids`) with egress to the target VM / cluster
  API (and to your image registry on 443). A private subnet is recommended;
  it needs a NAT route for image pulls.

### Azure (ACI)

- **Subscription with ACI quota.** Register the provider if this
  subscription hasn't used ACI:
  `az provider register --namespace Microsoft.ContainerInstance`.
- **Service principal** — the dashboard's existing `azure_client_id` /
  `azure_client_secret` / `azure_tenant_id` / `azure_subscription_id`. It
  needs **Contributor** (or a custom role allowing container-group
  create/delete) on the resource group ACI runs in (`azure_resource_group`).
- **ACR credentials** (`ansible_aci_acr_server` / `_username` / `_password`)
  **only when** the runner image lives in a private ACR. ACI uses these as
  image-registry credentials at container-group create time to pull the
  image; they are passed as secure values, not stored on the container.
  Leave blank for the public `willhallonline/ansible` / `dtzar/helm-kubectl`
  images.
- **VNet subnet** (`ansible_aci_subnet_id`) when the container group must
  run inside a private VNet to reach the target.

### GCP (Cloud Run Jobs)

- **APIs enabled:** `run.googleapis.com` (and `compute.googleapis.com` /
  `iam.googleapis.com` for the surrounding flows).
- **Service account** with:

  | Role | Purpose |
  |---|---|
  | `roles/run.admin` | Create, execute, and delete Cloud Run Jobs |
  | `roles/logging.viewer` | Retrieve job output from Cloud Logging |
  | `roles/iam.serviceAccountUser` | Act as a service account when submitting jobs |

- **VPC connector** (`gcp_ansible_vpc_connector`) when the job must reach
  private RFC-1918 targets — Cloud Run Jobs run in a Google-managed VPC by
  default and can't reach private addresses without one. Create one with:

  ```bash
  gcloud compute networks vpc-access connectors create ansible-runner \
    --region us-central1 --network default --range 10.8.0.0/28
  ```

  then set `gcp_ansible_vpc_connector=projects/PROJECT_ID/locations/us-central1/connectors/ansible-runner`.

---

## Cloud VM SSH keys (Ansible runner)

Cloud VM targets authenticate with an SSH key, not a password. The Ansible
runner pulls the private key from the cloud's secret store at run time:

| Cloud | Config key | Env var | Default | Source |
|---|---|---|---|---|
| AWS | `ansible_ssh_key_sm_name` | `ANSIBLE_SSH_KEY_SM_NAME` | `ec2/ssh-keypair` | AWS Secrets Manager secret name/ARN. The value may be a raw PEM or a JSON object with a `private_key` field — auto-detected. IAM needs `secretsmanager:GetSecretValue`. |
| Azure | `ansible_aci_ssh_key_secret_name` | `ANSIBLE_ACI_SSH_KEY_SECRET_NAME` | _(empty)_ | Azure Key Vault secret name holding the private key PEM. |
| GCP | `gcp_ssh_key_secret_name` | `GCP_SSH_KEY_SECRET_NAME` | _(empty)_ | GCP Secret Manager secret name; the SA needs `roles/secretmanager.secretAccessor`. |

> A legacy AWS fallback exists: `ansible_ssh_key_secret` (env
> `ANSIBLE_SSH_KEY_SECRET`, default `AWS_KEY`) — a Password Safe secret
> title. Prefer `ansible_ssh_key_sm_name`.

GCP example — store the key and grant access:

```bash
gcloud secrets create ssh-ansible-keypair --replication-policy="automatic"
gcloud secrets versions add ssh-ansible-keypair --data-file=~/.ssh/id_rsa
gcloud secrets add-iam-policy-binding ssh-ansible-keypair \
  --member="serviceAccount:SA_EMAIL" \
  --role="roles/secretmanager.secretAccessor"
```

---

## Storage prerequisite (Ansible runner)

The Ansible runner fetches its assets (playbooks, scripts, packages) from a
[storage backend](../storage-management.md). At least one backend must be
configured and active on `/storage` before the Remote Worker / Ansible
feature flag can be enabled.

The four backends — S3, Azure Blob, GCS, Local Filesystem / UNC — are
configured on the dedicated **`/storage`** page. Picking the right backend:

| Use case | Recommended backend |
|---|---|
| Cloud VMs as targets, cloud Ansible runner | The matching cloud's bucket (S3 / Blob / GCS) |
| On-prem hypervisor targets, dashboard host on a corporate LAN | Local Filesystem / UNC |
| Mixed fleet, dashboard host has internet egress | Any cloud bucket — runner downloads the asset before SSH/WinRM |

Configuration steps, asset upload, migration between backends, and per-backend
IAM details all live in [docs/storage-management.md](../storage-management.md).
(The Kubernetes runner has no storage dependency.)

---

## Enable in the dashboard

1. Open **`/storage`** and configure at least one backend; pick it as active
   (required for the Ansible runner).
2. Open **Settings → Integrations**. The **Remote Worker** toggle, previously
   greyed out, is now selectable.
3. Click **Configure** on the Remote Worker row to set the
   [runner backends](#runner-backends) (`ansible_runner`, `k8s_runner`), the
   per-cloud SSH usernames, and — for cloud backends — the
   [shared cloud infrastructure](#shared-cloud-infrastructure).
4. Toggle Remote Worker **on**. No restart required.

### Per-cloud SSH user (Ansible runner)

Each cloud's stock image ships with a different default username
(`ec2-user` / `azureuser` / `gcp-user`), so the panel exposes three fields
rather than one:

| Field | Default | Override per job? |
|---|---|---|
| `ansible_aws_user` | `ec2-user` | Yes — the run-asset form on `/config-mgmt` pre-fills from this when the operator picks an `aws:` target, but the field stays editable. |
| `ansible_azure_user` | `azureuser` | Yes — same flow for `azure:` targets. |
| `ansible_gcp_user` | `gcp-user` | Yes — same flow for `gcp:` targets. |

The pre-fill is non-clobbering: a value the operator types by hand is never
overwritten when they switch targets. The submitted `ansible_user` is
whatever the field holds at submit time.

---

## Ansible: local Docker runner (on-premises and cloud targets)

The local runner is automatic: no extra infrastructure is needed beyond the
Docker socket already mounted in `docker-compose.yml`. It handles both
on-premises hypervisors and cloud VMs — the asset is always fetched from
storage regardless of where the target lives. It is also the only runner
that can target on-premises hypervisors and the only one that forwards
WinRM `ansible_password` extra vars.

### How the inventory is built

When you click **Run**, the dashboard calls `GET /api/config-mgmt/inventory`,
which returns a dynamic Ansible JSON inventory built from every on-premises
hypervisor integration that is **both enabled and has a host configured**.

Hypervisors that are not enabled or have no host set are silently omitted —
the target picker only shows what is actually reachable. Cloud VMs appear in
separate optgroups populated from the AWS / Azure / GCP tab caches.

| Hypervisor | Ansible connection | Credentials used |
|---|---|---|
| Proxmox VE | SSH | `proxmox_password` (root@pam — requires password auth, not API-token-only) |
| VMware vSphere / ESXi | SSH | `vsphere_password` (root on ESXi; SSH must be enabled) |
| Microsoft Hyper-V | WinRM (`ansible_connection: winrm`) | `hyperv_username` + `hyperv_password`; transport/port from Settings |
| Nutanix AHV | SSH | `nutanix_password` (targets the CVM SSH interface) |
| XCP-ng / XenServer | SSH | `xcpng_password` (root — same credentials as the XAPI connection) |

### Hyper-V WinRM requirements

The Ansible `community.windows` collection (included in
`willhallonline/ansible`) is required for Windows playbooks. If `pywinrm`
is not bundled in your image, install it:

```bash
pip install pywinrm
```

Or use a custom image that includes it:

```
ANSIBLE_LOCAL_IMAGE=my-registry/ansible-winrm:latest
```

WinRM must be enabled on the Hyper-V host (`Enable-PSRemoting -Force`) — the
same requirement as the Hyper-V management integration.

### Proxmox SSH note

The local runner authenticates to Proxmox via SSH using `proxmox_password`
(the root@pam password). If you configured Proxmox with **API token only**
(no password), the SSH connection will fail. Either:
- Set `PROXMOX_PASSWORD` in addition to the token, or
- Target Proxmox VMs individually by IP rather than using the `proxmox` group.

### ESXi SSH note

SSH is disabled by default on ESXi. Enable it via:
**Host → Manage → Services → TSM-SSH → Start**, or:

```bash
vim-cmd hostsvc/enable_ssh
```

### Changing the local Ansible image

```
ANSIBLE_LOCAL_IMAGE=willhallonline/ansible:latest
```

Any image with `ansible-playbook` on its `PATH` works. The playbook and
inventory are bind-mounted into `/ansible/` inside the container.

---

## Provisioning assets (.sh / .ps1 / .rpm / .deb)

In addition to Ansible playbooks (`.yml`), you can upload **scripts and
packages** to the same storage backend. The dashboard auto-generates a
wrapper playbook based on the file extension.

| Extension | What happens |
|---|---|
| `.yml` / `.yaml` | Playbook is used as-is |
| `.sh` | `ansible.builtin.script` — script copied to the remote host and executed with `/bin/bash` |
| `.ps1` | `ansible.windows.win_script` — copied and run on a Windows host (target must have `ansible_connection=winrm`) |
| `.rpm` | `ansible.builtin.copy` + `ansible.builtin.dnf` — package is transferred and installed with `--disable-gpg-check` |
| `.deb` | `ansible.builtin.copy` + `ansible.builtin.apt` — package is transferred and installed |

Two ways to upload:

- **`/storage` page** — file picker + Upload button, goes to the active
  backend.
- **`/config-mgmt` page** — same upload form, plus inline run controls.

Either way, the upload hits `POST /api/storage/upload` and the file appears
in the asset picker on next refresh. You can also write directly to the
underlying bucket / share with the cloud's native tools (`aws s3 cp`,
`az storage blob upload`, `gsutil cp`) if you'd rather script it.

The **Config Mgmt** tab shows all asset types in the picker. A colour badge
indicates the type (Playbook / Script / PowerShell / RPM / DEB).

> **Extra vars** are forwarded only to playbooks. For scripts and packages
> the field is accepted but ignored — pass runtime parameters via the script
> itself or encode them in the filename.

---

## Cloud VM target discovery (Ansible runner)

The **Config Mgmt** tab reads the instance lists already cached by the AWS,
Azure, and GCP tabs — no extra API calls are needed. The target picker shows
three optgroups:

| Optgroup | Source | SSH key |
|---|---|---|
| EC2 Instances (AWS) | AWS instances tab cache | `ansible_ssh_key_sm_name` |
| Azure Virtual Machines | Azure VMs tab cache | `ansible_aci_ssh_key_secret_name` (or password auth) |
| GCE Instances (GCP) | GCP instances tab cache | `gcp_ssh_key_secret_name` |

If you have not yet navigated to the cloud tab (so the cache is empty), visit
it once to populate the list, then return to Config Mgmt.

---

## Ansible playbook structure

### On-premises hypervisor playbook

Target the `proxmox`, `vsphere`, `hyperv`, `nutanix`, or `xcpng` group
(whichever is configured). Or use `on_premises` to hit all of them.

```yaml
# harden-proxmox.yml
- hosts: proxmox
  become: yes
  tasks:
    - name: Ensure auditd is running
      service:
        name: auditd
        state: started
        enabled: true
```

```yaml
# restart-hyperv-service.yml
- hosts: hyperv
  tasks:
    - name: Restart the dashboard service
      win_service:
        name: DashboardSvc
        state: restarted
```

### Cloud VM playbook (single-host, ad-hoc)

For cloud targets the dashboard passes the IP as `-i <host>,` to Ansible:

```yaml
# hardening.yml
- hosts: all
  become: yes
  tasks:
    - name: Ensure sshd is running
      service:
        name: sshd
        state: started
        enabled: true
```

### Provisioning asset examples

**Script (install-agent.sh)** — upload a `.sh` file; the dashboard wraps it
automatically:

```bash
#!/bin/bash
set -euo pipefail
curl -fsSL https://packages.example.com/agent.sh | bash
systemctl enable --now example-agent
```

**RPM package (my-agent-1.0.rpm)** — upload the `.rpm` directly. The dashboard
generates:

```yaml
- hosts: all
  become: yes
  tasks:
    - name: Copy my-agent-1.0.rpm to remote
      ansible.builtin.copy:
        src: /ansible/assets/my-agent-1.0.rpm
        dest: /tmp/my-agent-1.0.rpm
    - name: Install my-agent-1.0.rpm
      ansible.builtin.dnf:
        name: /tmp/my-agent-1.0.rpm
        state: present
        disable_gpg_check: true
```

### Sample playbooks

Ready-to-adapt starters for Linux and Windows cloud VMs live in
[`examples/playbooks/`](../../examples/playbooks/) — patching, SSH hardening,
admin-user creation, Docker, node_exporter, nginx (Linux); Windows updates,
firewall, Chocolatey, local admin, and IIS (Windows). See
[examples/playbooks/README.md](../../examples/playbooks/README.md) for how to run
each. **Linux** samples run via the cloud or local runner; **Windows** (WinRM)
samples run via the **local runner**, which forwards the `ansible_password` extra
var the WinRM connection needs (the cloud runner is SSH-only and doesn't forward
extra vars).

---

## Troubleshooting

### Ansible — local Docker runner

**"Target X is not a configured hypervisor"** — the hypervisor integration is
either disabled or has no host set. Enable it and fill in the host in
**Settings → Integrations**.

**No targets appear in the picker** — no on-premises hypervisor is both enabled
and configured. Check **Settings → Integrations** and confirm that both the
toggle is on and the host field is filled.

**"docker: command not found"** — the Docker socket is not mounted. Verify
`docker-compose.yml` includes the `/var/run/docker.sock` bind mount and restart
the stack.

**SSH authentication failed (Proxmox / vSphere / XCP-ng)** — the stored
password must work for SSH (not just the management API). For Proxmox, this
means `PROXMOX_PASSWORD` must be set (API-token-only auth is not sufficient
for SSH). For ESXi, SSH must be enabled on the host.

**Hyper-V: "WinRM connection refused"** — WinRM is not enabled. Run
`Enable-PSRemoting -Force` on the Hyper-V host.

**Hyper-V: "pywinrm is not installed"** — the Ansible image doesn't include
`pywinrm`. Set `ANSIBLE_LOCAL_IMAGE` to an image that does, or build a custom
image.

**Container starts but can't reach the hypervisor** — the Ansible container
runs on the same Docker network as the dashboard (`compose` default bridge).
If the hypervisor is on a separate VLAN, ensure the Docker host has a route
to it.

### Ansible — asset storage

> Storage backend configuration, asset-list issues, and per-provider IAM
> permission errors live in
> [docs/storage-management.md](../storage-management.md#troubleshooting).
> The items below are Ansible-runner-specific concerns that the storage
> page doesn't cover.

**"No active storage backend" when running** — the feature flag got enabled
while a backend was active, but it's since been deactivated. Re-pick a
backend on `/storage` and Save.

**"Permission denied" on .sh asset at run time** — the auto-generated wrapper
uses `ansible.builtin.script` which copies + runs the file with
`executable: /bin/bash`. If the remote rejects it, write a custom `.yml`
playbook with an explicit `mode: '0755'` copy + a task to invoke it.

**.ps1 asset fails with "WinRM connection refused"** — the target's inventory
hostvars don't have `ansible_connection=winrm`. Hyper-V hostvars set this
automatically. For other hypervisors hosting Windows guests, you'll need a
custom playbook that sets `vars:` explicitly, or extend the relevant
`services/<hypervisor>_service.py` to detect Windows guests.

**Cloud VMs not in the target list** — the list is read from the in-memory
cache populated by the AWS / Azure / GCP tabs. Visit the relevant cloud tab
first so the cache is warm, then return to Config Mgmt.

**SSH authentication failed on cloud target (AWS)** — verify
`ansible_ssh_key_sm_name` is set and the IAM role has
`secretsmanager:GetSecretValue` on that secret.

**SSH authentication failed on cloud target (GCP)** — verify
`gcp_ssh_key_secret_name` is set and the service account has
`roles/secretmanager.secretAccessor` on the secret. Ensure the public key is
in the instance's `~/.ssh/authorized_keys` (injected at launch).

### Ansible — cloud runners

**ECS task fails to start** — check CloudWatch logs for the task family
`ansible-config-mgmt`. Common causes: missing execution role, ECR pull error,
or subnet routing to the target.

**GCP: "Permission denied" creating Cloud Run Job** — add `roles/run.admin`
and `roles/iam.serviceAccountUser` to the service account.

**GCP: logs empty after successful job** — add `roles/logging.viewer`:
```bash
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:SA_EMAIL" \
  --role="roles/logging.viewer"
```

**GCP: Cloud Run job can't reach target host** — set `gcp_ansible_vpc_connector`
to a Serverless VPC Access connector in the same region as your GCE instances.

### Kubernetes runner

**Direct kubectl/helm fails with HTTP 526 / TLS errors** — a corp egress
proxy is inspecting TLS to the cluster's private-CA API. Switch
`k8s_runner` to a cloud backend (`ecs` / `aci` / `gcp`) so the op runs from a
task with clean egress.

**Cloud k8s task times out reaching the API** — the cluster API isn't
reachable from the runner's network. Confirm the cluster has a public
endpoint and add the runner's egress IP/CIDR to the cluster's
authorized-networks allow-list (see
[Reachability caveat](#reachability-caveat)).

**"Kubernetes ECS/ACI/Cloud Run runner is not configured"** — the runner
couldn't resolve a required shared field. ECS needs `ansible_ecs_subnet_id`
and `ansible_ecs_execution_role_arn`; ACI needs `azure_resource_group`; GCP
needs `gcp_project_id` and a region (`gcp_region` or
`gcp_ansible_cloud_run_region`). Set them on **Configuration → Remote
Worker** / the relevant cloud config.

**Image pull fails on the cloud k8s task** — the stock
`dtzar/helm-kubectl:latest` is on Docker Hub. Behind a private registry,
mirror it and set `k8s_runner_image` (ECS needs `ansible_ecs_execution_role_arn`
with ECR pull; ACI needs `ansible_aci_acr_*`).
