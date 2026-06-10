# Promote runner

Transient container that powers automated cross-cloud image promotion.
The dashboard launches one of these as an ECS Fargate task (AWS
targets), Azure Container Instance (Azure targets), or Cloud Run job
(GCP targets), passes a presigned URL to the hub artefact, and the
runner converts + uploads the disk image to the target cloud's storage
ready for the cloud-native import API.

For the operator-facing flow (when this fires, where the artefact lives,
how to read promotion status), see
[`docs/image-management.md`](../../docs/image-management.md) — the
architecture diagram there is the canonical picture. This README is the
runner internals + per-cloud prerequisites + config-key reference.

## Why this exists

The dashboard's web tier shouldn't be doing multi-GB image transfers — it
would block other requests, push memory limits, and tie up the gunicorn
worker for tens of minutes per promote. Pushing the heavy lifting to a
transient task in the *target* cloud means the upload to target storage
is local (no cross-cloud egress for the final hop) and the dashboard just
launches + polls.

## Architecture

The orchestrator endpoint is `POST /api/images/{id}/promote`. The
dashboard:

1. Resolves the hub artefact URL on `RegisteredImage.artefact_url` and
   mints a short-lived presigned HTTPS URL via
   `storage_service.presigned_url(...)`.
2. Enqueues an `image_promote_{aws,azure,gcp}` Job and launches the
   target cloud's runner with argv (presigned URL + dest coordinates).
3. Polls the runner until exit, captures its stdout/stderr from the
   cloud-native log stream.
4. On success, calls the cloud's image-import API against the staged
   blob the runner uploaded.
5. Waits for the resulting native image to reach its terminal-ready
   state (`Available` / `Succeeded` / `READY`).
6. Deletes the staged blob and writes the final identifier to
   `RegisteredImage.promotions[<target>]`.

The dashboard handles every cloud-API call. The runner's contract is
narrow: download the source, convert if asked, upload to the dest, exit
zero.

## Public image

The dashboard defaults to pulling
`chrweav/dashboard-promote-runner:latest` — a multi-arch image
(linux/amd64 + linux/arm64) published from this repo by
`.github/workflows/publish-images.yml` on each tagged release.

Operators who want to pin a hardened build can override via
`promote_runner_image` on `/storage` — the default is a convenience,
not a requirement. The cloud-native runner (ECS / ACI / Cloud Run)
pulls from a registry it can reach, so air-gapped or private-registry
deployments should push a copy to ECR / ACR / Artifact Registry and
point `promote_runner_image` at it.

## Build locally

```
cd runners/promote
docker build -t dashboard-promote-runner:dev .
```

To publish to your own registry (example for ECR):

```
aws ecr get-login-password --region us-east-2 \
  | docker login --username AWS --password-stdin \
      <account>.dkr.ecr.us-east-2.amazonaws.com
docker tag dashboard-promote-runner:dev \
  <account>.dkr.ecr.us-east-2.amazonaws.com/dashboard-promote-runner:0.1.0
docker push \
  <account>.dkr.ecr.us-east-2.amazonaws.com/dashboard-promote-runner:0.1.0
```

Then set `promote_runner_image` on `/storage` to the pushed reference.

## Prerequisites per target cloud

Provisioning these is what
[`scripts/sandbox/Linux/setup-*.sh`](../../scripts/sandbox/Linux/) covers
for sandbox accounts. In a production environment Terraform / IaC is the
better fit, but the IAM/role shapes below are the same either way.

### AWS

- **ECS Fargate cluster** the dashboard can `run-task` against (reuses
  the Ansible / BT Jumpoint cluster if you have one).
- **Task execution role** with `service-role/AmazonECSTaskExecutionRolePolicy`
  (image pull + CloudWatch logs).
- **Task role** trusted by `ecs-tasks.amazonaws.com`, granting
  `s3:PutObject` (and `s3:AbortMultipartUpload`) on the staging bucket /
  prefix. This is the role the runner container assumes via the task's
  metadata endpoint to do the dest upload.
- **`vmimport` IAM service role** trusted by `vmie.amazonaws.com` with
  read **and** write on the staging bucket (`s3:GetObject`,
  `s3:PutObject`, `s3:AbortMultipartUpload`, plus the usual
  bucket-level metadata reads) and `ec2:CreateImage` /
  `ec2:DescribeImportImageTasks`. AWS's image-import service assumes
  this role when the dashboard calls `ec2:ImportImage`, and the same
  role is also used by `ec2:ExportImage` to write the resulting VHD
  back to the bucket — so read-only access here is enough for import
  but breaks export with a confusing "Insufficient permissions, please
  verify bucket ownership" error. Override the role name with
  `aws_vmimport_role_name` if you've named yours differently.
- **Subnet + security group** reachable to the public internet (the
  runner pulls the presigned URL over HTTPS; SGs need egress on 443).
  Public IP assignment is enabled by default; switch to a NAT-routed
  private subnet if your policy requires it.
- **Staging S3 bucket** — defaults to `storage_s3_bucket` under
  `promote-staging/` so you don't need a second bucket; override via
  `promote_runner_aws_staging_bucket` + `promote_runner_aws_staging_prefix`
  if you'd rather isolate.

### Azure

- **Subscription with ACI quota** in your dashboard's region. Register
  the `Microsoft.ContainerInstance` resource provider if you haven't
  used ACI in this subscription before:
  `az provider register --namespace Microsoft.ContainerInstance`.
- **Service principal** (the existing `azure_client_id`) with
  **Contributor** on the resource group the target managed image will
  land in, plus **Storage Blob Data Contributor** on the storage
  account that hosts the staging container. The same SP credentials
  are passed to the ACI container as secure env vars so the runner can
  write to the dest blob.
- **VNet subnet** if your ACI must run inside a private VNet (optional;
  `promote_runner_azure_subnet_id`).
- **Staging storage account + container** — defaults to
  `storage_azure_account` / `storage_azure_container` so the hub
  account doubles as staging. Override via
  `promote_runner_azure_staging_account` +
  `promote_runner_azure_staging_container` if you want a separate
  account.
- **Optional ACR credentials** (`azure_acr_server` / `_username` /
  `_password`) when hosting the runner image in a private ACR.

### GCP

- **`compute.googleapis.com`, `run.googleapis.com`, `iam.googleapis.com`**
  all enabled in the project.
- **Service account** with `roles/run.developer` (job CRUD),
  `roles/run.invoker` (kick off executions), and
  `roles/storage.objectAdmin` on the staging bucket. The dashboard's
  primary SA works if it already has those — see
  `gcp_service_account_json` in setup.
- **Project quota** for Cloud Run Jobs in your chosen region.
- **Optional VPC connector** when the runner needs egress through a
  private network (`promote_runner_gcp_vpc_connector`).
- **Staging GCS bucket** — defaults to `storage_gcs_bucket` under
  `promote-staging/`; override via `promote_runner_gcp_staging_bucket` +
  `promote_runner_gcp_staging_prefix` to isolate.

## Configuration

Every key below lives in the same `config_service` store as the rest of
the dashboard's settings. Set them on `/storage` (the page hosts a
generic `PATCH /api/storage/config` form) or via env var override.

| Key | Default | Purpose |
|---|---|---|
| `promote_runner_image` | `chrweav/dashboard-promote-runner:latest` | Container image to launch for the runner task. Override to a private registry path. |

### AWS-target

| Key | Fallback | Purpose |
|---|---|---|
| `promote_runner_ecs_cluster` | `ansible_ecs_cluster` | ECS cluster the Fargate task lands in. |
| `promote_runner_ecs_task_family` | `promote-runner` | Task definition family. Auto-registered on first promote. |
| `promote_runner_ecs_cpu` | `1024` | Fargate vCPU units. qemu-img headroom. |
| `promote_runner_ecs_memory` | `4096` | Fargate memory (MiB) — enough for multi-GB VHDs. |
| `promote_runner_ecs_subnet_id` | `ansible_ecs_subnet_id` | Subnet the task uses. Needs egress to the presigned source URL. |
| `promote_runner_ecs_security_group_ids` | `ansible_ecs_security_group_ids` | Comma-separated SG list. |
| `promote_runner_ecs_execution_role_arn` | `ansible_ecs_execution_role_arn` | ECS execution role (image pull + logs). |
| `promote_runner_ecs_task_role_arn` | _(none — required)_ | Task role with `s3:PutObject` on the staging bucket. |
| `promote_runner_aws_staging_bucket` | `storage_s3_bucket` | Where the runner drops the staged VHD. |
| `promote_runner_aws_staging_prefix` | `promote-staging` | Key prefix inside the staging bucket. |
| `aws_vmimport_role_name` | `vmimport` | Service role `ec2:ImportImage` assumes. |

### Azure-target

| Key | Fallback | Purpose |
|---|---|---|
| `promote_runner_azure_resource_group` | `azure_resource_group` | RG the ACI container group runs in. |
| `promote_runner_azure_location` | `azure_location` (then `centralus`) | Region for ACI + Image. |
| `promote_runner_azure_subnet_id` | _(optional)_ | VNet subnet to bind the container group to. |
| `promote_runner_azure_cpu` | `2` | Container vCPUs. |
| `promote_runner_azure_memory_gb` | `4` | Container memory. |
| `promote_runner_azure_staging_account` | `storage_azure_account` | Storage account for staging blobs. |
| `promote_runner_azure_staging_container` | `storage_azure_container` (then `playbooks`) | Container for staging blobs. |
| `promote_runner_azure_staging_prefix` | `promote-staging` | Blob name prefix. |
| `promote_runner_azure_target_resource_group` | `azure_resource_group` | RG the resulting managed image lands in. |
| `promote_runner_azure_target_storage_account_id` | _(optional)_ | ARM ID Azure pins the resulting image's OS disk to (compliance / BYOK). |

### GCP-target

| Key | Fallback | Purpose |
|---|---|---|
| `promote_runner_gcp_region` | `gcp_region` | Region the Cloud Run Job runs in. |
| `promote_runner_gcp_cpu` | `2000m` | Cloud Run container CPU. |
| `promote_runner_gcp_memory` | `4Gi` | Cloud Run container memory. |
| `promote_runner_gcp_vpc_connector` | _(optional)_ | Serverless VPC Access connector for private egress. |
| `promote_runner_gcp_service_account` | _(optional)_ | Workload-identity SA email for the runner. |
| `promote_runner_gcp_staging_bucket` | `storage_gcs_bucket` | GCS bucket for the staged tar.gz. |
| `promote_runner_gcp_staging_prefix` | `promote-staging` | Object prefix. |
| `promote_runner_gcp_image_family` | _(optional)_ | Family label on the resulting custom image. |

## Inputs

The runner reads everything from CLI args + a small set of env vars
(set by the orchestrator at task launch — usually injected via task
IAM role or container env overrides).

| Arg                    | Required for | Description                                    |
|------------------------|--------------|------------------------------------------------|
| `--source-url`         | always       | Presigned HTTPS URL of the hub artefact        |
| `--source-format`      | always       | `vhd`, `raw`, `qcow2`, `vmdk`                  |
| `--target-format`      | always       | Conversion target (skips qemu-img if same)     |
| `--target`             | always       | `s3` / `azure` / `gcs` — chooses upload path   |
| `--dest-s3-bucket`     | `--target s3` | Target S3 bucket                              |
| `--dest-s3-key`        | `--target s3` | Target S3 key                                 |
| `--dest-s3-region`     | optional     | Defaults to env `AWS_REGION`                   |
| `--dest-azure-account` | `--target azure` | Storage account name                       |
| `--dest-azure-container` | `--target azure` | Container name                           |
| `--dest-azure-blob`    | `--target azure` | Blob name                                  |
| `--dest-gcs-bucket`    | `--target gcs`   | GCS bucket                                 |
| `--dest-gcs-object`    | `--target gcs`   | GCS object name (should end in `.tar.gz`)  |

**GCP target quirk:** GCP's `compute.images.insert` requires the source object to be a `.tar.gz` containing exactly one entry named `disk.raw`. When `--target gcs` is paired with `--target-format raw` (the dashboard's only supported GCP path today), the runner automatically tar+gzips the converted raw file under that name before upload. The dashboard always passes `--dest-gcs-object` ending in `.tar.gz` for this reason.

Credential env vars per target:

- `s3` — task IAM role (preferred) or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / optional `AWS_SESSION_TOKEN`.
- `azure` — `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`.
- `gcs` — `GOOGLE_APPLICATION_CREDENTIALS` path to a service-account JSON.

The source URL is presigned by the dashboard at launch time, so the
container never sees source-side credentials.

## Exit codes

| Code | Meaning                                                |
|------|--------------------------------------------------------|
| 0    | Success — destination object exists at the target URL  |
| 2    | Invalid args (missing target-specific flags)           |
| 3    | qemu-img conversion failed                             |
| 4    | Any other failure (network, upload, etc.)              |

The orchestrator reads stdout/stderr from CloudWatch (AWS) / Log Analytics
(Azure) / Cloud Logging (GCP) and surfaces the tail to the operator on the
Job detail page.

## What this image deliberately doesn't do

- Doesn't call the cloud-native image-import API — that's the dashboard's
  job after the runner exits successfully.
- Doesn't authenticate to the source. Presigned URL only.
- Doesn't clean up the staged target object on failure. The orchestrator
  deletes it after the cloud-import finishes; if the runner failed first,
  the partial upload is left for the operator to inspect and the next run
  overwrites it.
