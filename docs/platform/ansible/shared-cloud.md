# Shared cloud infrastructure for the runners

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are giving the runners somewhere to run — a cluster, a registry, a subnet.

Part of [Remote Worker](../ansible.md). The cluster, registry and network each runner backend needs, and the per-cloud prerequisites.

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
them blank. See [`runners/promote/README.md`](../../../runners/promote/README.md)
for the full promote-runner key list.

---


## Per-cloud prerequisites

Only needed for the cloud backends (`ecs` / `aci` / `gcp`). The local
backends need nothing beyond the Docker socket (Ansible) or in-container
`kubectl`/`helm` (Kubernetes).

### AWS (ECS Fargate)

- **ECS Fargate cluster** the dashboard can `run-task` against
  (`ansible_ecs_cluster`; reuses the BT Gateway cluster by default).
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
  [`runners/promote/README.md`](../../../runners/promote/README.md).
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
  Leave blank for the public `chrweav/ansible-winrm` / `dtzar/helm-kubectl`
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

- **VPC reach** when the job must SSH to a private RFC-1918 target — Cloud Run
  Jobs run in a Google-managed VPC by default and can't reach private addresses
  without one of the two modes below (otherwise SSH times out and the play fails
  `UNREACHABLE`, container exit code 4). Direct VPC egress wins when both are set.

  - **Direct VPC egress (preferred — no standing infra):** set both
    `gcp_run_network` (VPC name) and `gcp_run_subnetwork` (a subnet in the Cloud
    Run region). The job's NIC lands straight in the subnet — no connector to
    provision or pay for, and immune to the connector's shared-core zonal
    stockouts. Egress stays private-ranges-only. Ensure a firewall rule allows
    `tcp:22` from the subnet range to the target VM.

    ```
    gcp_run_network=dashboard-sandbox-vpc
    gcp_run_subnetwork=dashboard-sandbox-vm-subnet
    ```

  - **VPC connector (legacy):** create a Serverless VPC Access connector and set
    `gcp_ansible_vpc_connector`:

    ```bash
    gcloud compute networks vpc-access connectors create ansible-runner \
      --region us-central1 --network default --range 10.8.0.0/28
    ```

    then set `gcp_ansible_vpc_connector=projects/PROJECT_ID/locations/us-central1/connectors/ansible-runner`.

---


## Troubleshooting


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

**GCP: Cloud Run job can't reach target host** (play fails `UNREACHABLE`, "ssh:
connect to host … port 22: Operation timed out", container exit code 4) — give
the runner VPC reach: set `gcp_run_network` + `gcp_run_subnetwork` for direct VPC
egress (preferred, no standing infra), or `gcp_ansible_vpc_connector` for a
Serverless VPC Access connector — matching the Cloud Run region to the target's
region. Also confirm a firewall rule permits `tcp:22` from the runner's subnet
range to the VM.

**Azure: ACI runner UNREACHABLE / `ssh: connect to host <ip> port 22: Operation
timed out`** — the ACI container has no route to the target VM's private IP. Set
`ansible_aci_subnet_id` to a VNet-delegated subnet with line-of-sight to the target
subnet; when unset it now falls back to the gateway's `azure_aci_subnet_id`. With no
subnet the container group is public and cannot reach private targets. (A working PRA
Shell Jump to the same VM confirms the gateway's subnet reaches it — reuse that subnet.)
