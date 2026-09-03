# Remote Worker config field reference

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are filling in the Remote Worker panel and want to know what each field does.

Part of [Remote Worker](../ansible.md). Every field on the Settings panel, and the config key behind it.

## Config panel field reference

Every field on **Configuration → Remote Worker**, grouped as the panel
groups them. Values are stored in the dashboard's config-service store and
can also be set via the matching environment variable (the env name is the
config key upper-cased — pydantic `BaseSettings`, no prefix). Defaults and
meanings are taken from `web_dashboard/config.py`.

### Runner backends

The panel shows a **runner × cloud grid**: for each runner (Ansible,
Kubernetes) pick **Local** or the matching cloud service per target cloud. Each
selector writes a per-cloud key; the global keys remain as a fallback (see
[Per-target-cloud backend](../ansible.md#per-target-cloud-backend)).

| Panel label | Config key | Env var | Default | Meaning |
|---|---|---|---|---|
| Ansible runner — AWS targets | `ansible_runner_aws` | `ANSIBLE_RUNNER_AWS` | _(empty → `ansible_runner`)_ | `local` \| `ecs`. Backend for AWS-target playbook runs. |
| Ansible runner — Azure targets | `ansible_runner_azure` | `ANSIBLE_RUNNER_AZURE` | _(empty → `ansible_runner`)_ | `local` \| `aci`. |
| Ansible runner — GCP targets | `ansible_runner_gcp` | `ANSIBLE_RUNNER_GCP` | _(empty → `ansible_runner`)_ | `local` \| `gcp`. |
| Kubernetes runner — EKS (AWS) | `k8s_runner_aws` | `K8S_RUNNER_AWS` | _(empty → `k8s_runner`)_ | `local` \| `ecs`. Backend for EKS-cluster ops. |
| Kubernetes runner — AKS (Azure) | `k8s_runner_azure` | `K8S_RUNNER_AZURE` | _(empty → `k8s_runner`)_ | `local` \| `aci`. |
| Kubernetes runner — GKE (GCP) | `k8s_runner_gcp` | `K8S_RUNNER_GCP` | _(empty → `k8s_runner`)_ | `local` \| `gcp`. |
| _(fallback)_ Ansible runner | `ansible_runner` | `ANSIBLE_RUNNER` | `local` | Global default used when a per-cloud key above is blank: `local` \| `ecs` \| `aci` \| `gcp`. |
| _(fallback)_ Kubernetes runner | `k8s_runner` | `K8S_RUNNER` | `local` | Global default used when a per-cloud key above is blank. |

> **There is no `ansible_runner_local`.** A Config-Management run against an on-prem
> Kubernetes cluster (`cloud = local`) always uses the local runner — it is the only
> backend with a route to your LAN — so there is no key to set, and a stray one is
> ignored rather than honored. See
> [Kubernetes-cluster and database targets](kubernetes-runner.md#kubernetes-cluster-and-database-targets-localhost-runs).

### Shared cloud infrastructure — AWS / ECS

These knobs are reused by the Ansible runner, the Kubernetes runner, **and**
the image-promote runner (see [Shared cloud infrastructure](shared-cloud.md#shared-cloud-infrastructure)).

| Panel label | Config key | Env var | Default | Meaning |
|---|---|---|---|---|
| ECS cluster | `ansible_ecs_cluster` | `ANSIBLE_ECS_CLUSTER` | `bt-jumpoint` | ECS cluster the Fargate task lands in. Shares the cluster with the BT Gateway by default. |
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
| ACI subnet ID | `ansible_aci_subnet_id` | `ANSIBLE_ACI_SUBNET_ID` | _(empty)_ | Subnet ARM ID for ACI VNet injection (so the container group can reach private targets). When unset, falls back to the gateway's subnet (`azure_aci_subnet_id`). **If neither is set the container group is public and cannot reach private VM/cluster IPs.** Must be delegated to `Microsoft.ContainerInstance/containerGroups` and have routing + NSG to the target subnet on the required port; reusing the gateway's subnet is the simplest proven choice. |
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
| ACI runner image | `ansible_aci_image` | `ANSIBLE_ACI_IMAGE` | `chrweav/ansible-winrm:latest` | Ansible image the ACI task pulls (default includes pywinrm). |
| Cloud Run runner image | `gcp_ansible_image` | `GCP_ANSIBLE_IMAGE` | `chrweav/ansible-winrm:latest` | Ansible image the Cloud Run Job pulls (default includes pywinrm). |
| ACI SSH key secret name | `ansible_aci_ssh_key_secret_name` | `ANSIBLE_ACI_SSH_KEY_SECRET_NAME` | _(empty)_ | Azure Key Vault secret name holding the Ansible SSH private key for Azure VM targets. |

> The ECS Ansible image is `ansible_ecs_image` (env `ANSIBLE_ECS_IMAGE`,
> default `chrweav/ansible-winrm:latest`). The local runner image is
> `ansible_local_image` (env `ANSIBLE_LOCAL_IMAGE`, same default). The AWS SSH key for
> EC2 targets comes from `ansible_ssh_key_sm_name` (env
> `ANSIBLE_SSH_KEY_SM_NAME`, default `ec2/ssh-keypair`) — see
> [Cloud VM SSH keys](ansible-runner.md#cloud-vm-ssh-keys-ansible-runner). The final-fallback
> username for an unrecognised cloud tag is `ansible_default_user`
> (default `ec2-user`).

### Kubernetes runner

| Panel label | Config key | Env var | Default | Meaning |
|---|---|---|---|---|
| Kubernetes runner | `k8s_runner` | `K8S_RUNNER` | `local` | `local` (in-process) \| `ecs` \| `aci` \| `gcp`. See [Kubernetes runner](#kubernetes-runner). |
| Kubernetes runner image | `k8s_runner_image` | `K8S_RUNNER_IMAGE` | `dtzar/helm-kubectl:latest` | Stock kubectl+helm image the cloud task runs. No custom image is needed. |

---
