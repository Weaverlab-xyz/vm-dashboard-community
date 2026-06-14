# Deploy Docker Compose to the cloud (ECS / ACI / GCE)

## What is it?

The **Containers → Cloud** tab can deploy a Docker Compose file to a managed
container runtime without going through Portainer:

- **AWS ECS** — a Fargate task with one container per compose service.
- **Azure ACI** — a container group with one container per compose service.
- **GCP GCE** — a Container-Optimized OS instance running all services as a
  multi-container [konlet](https://cloud.google.com/container-optimized-os/docs)
  spec (Cloud Run Jobs are single-container, so a COS instance is used instead).

The compose file is **referenced from the storage backend** (the same store used
for playbooks and Packer scripts) — you upload it once on the Storage page and
pick it from a dropdown at deploy time. Deploys run as background jobs; watch
progress on the Jobs page.

> A curated catalog of ready-to-deploy apps in front of this is intentionally
> deferred to the hosted SaaS edition — the community edition ships the generic
> "bring your own compose file" capability.

## Sample compose files

Ready-to-adapt starters for common apps live in
[`examples/compose/`](../../examples/compose/) — Apache Guacamole, Kasm, Trivy,
Syft, Grype, Cosign, OPA, Conftest, Checkov, Terraform/driftctl, and a Temporal
worker. They are the community-edition stand-in for an app catalog: upload one,
edit the placeholders, and deploy. See
[examples/compose/README.md](../../examples/compose/README.md) for the per-file
guide; each conforms to the supported subset below.

## Supported compose subset

Per service: `image` (required), `entrypoint`, `command`, `environment`, `ports`,
`restart`, and CPU/memory limits (`deploy.resources.limits.cpus` / `memory`, or
the `cpus` / `mem_limit` shorthands).

Unsupported keys — `build`, `volumes`, top-level `networks` / `volumes` /
`secrets` / `configs`, `depends_on`, `profiles`, `extends`, `env_file`, and
host-passthrough env vars (`- KEY` with no value) — are **rejected** with a
clear error so a partial workload is never deployed.

`entrypoint` overrides the image ENTRYPOINT and `command` overrides its CMD, the
same as Docker Compose. The three runtimes apply them consistently (ECS
`entryPoint`+`command`; GCE konlet `command`+`args`; ACI concatenates them into
its single exec list — set both for entrypoint-based images so ACI matches
ECS/GCE).

## Target settings

Cluster / subnet / resource-group / zone settings default to the values already
configured in **Setup** (the same ones the Jumpoint and runners use). The deploy
form's **Advanced** section lets you override them per deploy. Optional CPU /
memory fields override the per-runtime defaults (ACI 1 vCPU / 1 GiB,
Fargate 256 / 512, GCE machine `e2-small`).

## Lifecycle

- **ECS** deployments appear in the ECS Tasks list; stop them there.
- **ACI** deployments appear in the ACI Containers list; stop them there.
- **GCE** deployments appear in the **GCE Compose Deployments** list
  (`labels.purpose=compose`); delete them there.

## Permissions

The deploy endpoint requires `containers:write`; deleting a GCE compose instance
requires `containers:delete` — the same scopes as the rest of the Containers tab.

## Notes & limits

- Private registry images: v1 wires the configured ACR credentials for ACI;
  ECS/GCE pulls assume the image is public or reachable by the task/instance's
  role. Per-registry auth across all three providers is a follow-up.
- GCE COS runs containers on the instance's host network, so compose `ports`
  are informational there — reachability is governed by the instance's firewall
  tags/rules.
