# Promote runner

Transient container that powers automated cross-cloud image promotion. The
dashboard launches one of these as an ECS Fargate task (AWS targets), Azure
Container Instance (Azure targets), or Cloud Run job (GCP targets), passes
a presigned URL to the hub artefact, and the runner converts + uploads the
disk image to the target cloud's storage ready for the cloud-native
import API.

## Why this exists

The dashboard's web tier shouldn't be doing multi-GB image transfers — it
would block other requests, push memory limits, and tie up the gunicorn
worker for tens of minutes per promote. Pushing the heavy lifting to a
transient task in the *target* cloud means the upload to target storage
is local (no cross-cloud egress for the final hop) and the dashboard just
launches + polls.

## Public image

The dashboard defaults to pulling `weaverlab-xyz/dashboard-promote-runner:latest`.
Operators who want to pin a hardened build can override via the
`promote_runner_image` config key on the `/storage` page.

## Build locally

```
cd runners/promote
docker build -t dashboard-promote-runner:dev .
```

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
| `--dest-gcs-object`    | `--target gcs`   | GCS object name                            |

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
