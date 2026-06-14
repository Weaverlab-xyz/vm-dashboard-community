# Sample Docker Compose files (cloud container app starters)

Ready-to-adapt compose files for deploying common apps to **AWS ECS**, **Azure
ACI**, and **GCE (Container-Optimized OS)** via the dashboard's
**Containers → Cloud → Deploy Compose** feature
(see [docs/integrations/cloud-compose.md](../../docs/integrations/cloud-compose.md)).

These are the community-edition answer to an application catalog: instead of a
one-click catalog (a SaaS-edition feature), you upload one of these files, edit
the placeholders, and deploy. The apps mirror the curated
[non-privileged container backlog](../../docs/non-privileged-container-backlog.md).

## How to deploy

1. **Upload** the `.yml` to a storage backend — Storage page, or `POST /api/storage/upload`.
2. **Deploy** — Containers → Cloud → **Deploy Compose** → pick the file → choose
   ECS / ACI / GCE → (optional) set a name, CPU/memory, advanced target overrides.
3. **Watch** the job on the Jobs page. Long-running services then appear in the
   ECS / ACI / GCE lists; one-shot jobs print results to the container logs
   (CloudWatch for ECS, container logs for ACI, Cloud Logging for GCE).

## What's here

| File | App | Notion stream | Shape |
|---|---|---|---|
| `guacamole.yml` | Apache Guacamole (guacd + web) | `saas-virtual-desktop` | service · port 8080 · external auth DB |
| `kasm-desktop.yml` | Kasm streamed desktop | `saas-virtual-desktop` | service · port 6901 |
| `trivy-image-scan.yml` | Trivy image CVE scan | `saas-image-supplychain` | one-shot · runs as-is |
| `syft-sbom.yml` | Syft SBOM | `saas-self-supplychain` | one-shot · runs as-is |
| `grype-scan.yml` | Grype vuln scan | `saas-image-supplychain` | one-shot · runs as-is |
| `cosign-verify.yml` | Cosign verify | `saas-self-supplychain` | one-shot · adapt verify args |
| `opa-server.yml` | Open Policy Agent | `saas-action-admission` | service · port 8181 |
| `conftest-test.yml` | Conftest | `saas-iac-hardening` | one-shot · adapt (supply input) |
| `checkov-scan.yml` | Checkov (tfsec/Terrascan alt) | `saas-iac-hardening` | one-shot · adapt (supply input) |
| `terraform-plan.yml` | Terraform plan / driftctl | `saas-iac-hardening` / `saas-config-drift` | one-shot · adapt (config + creds) |
| `temporal-worker.yml` | Temporal worker | `image-promote-saas` | service · your worker image |

"runs as-is" = scans a remote registry image and works on first deploy (edit the
target image to scan your own). "adapt" = needs your input/config wired in first —
the file ships a deployable `--version`/demo command so you can confirm the
container runs end-to-end, with the real command in its header comment.

## Two things to know about the supported subset

These files conform to the compose subset the cloud deploy accepts: per service
`image`, `command`, `environment` (explicit `KEY=VALUE`), `ports`, `restart`, and
`deploy.resources.limits.{cpus,memory}`. **Not supported:** `build`, `volumes`,
`networks`, `depends_on`, `secrets`, `configs`, `profiles`, `env_file`, port
ranges. Two consequences worth calling out:

- **No `${VAR}` interpolation.** Values are taken literally — edit placeholders
  (`change-me`, `nginx:latest`, image names) directly in the file.
- **No volumes.** Apps that need persistence or local file input use an external
  managed service (e.g. Guacamole's auth DB) or have the input baked into a
  derived image. This is the "little more work" the community edition trades for
  not having a catalog.

### ACI `command` caveat

On **Azure ACI**, a container's `command` **replaces the image entrypoint**
(Azure semantics), whereas on **ECS** and **GCE** it overrides only the image's
default arguments (the entrypoint is kept — standard compose behavior). For the
one-shot tool images here (Trivy, Syft, OPA, …) the shipped `command` is the
compose-correct form, so **deploy those to ECS or GCE**. To run them on ACI,
prefix the tool binary in the command (e.g. `["trivy", "image", "nginx:latest"]`
instead of `["image", "nginx:latest"]`). Each affected file notes this inline.
