# Cloud Containers

The **Containers → Cloud** tab deploys a Docker Compose file to a managed cloud container
runtime — without going through Portainer — and monitors the container workloads the
dashboard runs across AWS, Azure, and GCP.

| Runtime | What a deploy becomes | Deploy target? |
|---|---|---|
| **AWS ECS** | a **Fargate task**, one container per compose service | ✅ |
| **Azure ACI** | a **container group**, one container per compose service | ✅ |
| **GCP GCE** | a **Container-Optimized OS** VM running all services as a multi-container [konlet](https://cloud.google.com/container-optimized-os/docs) spec (Cloud Run Jobs are single-container, so a COS instance is used instead) | ✅ |

> **The PAM layer stack does not apply here.** Unlike [Cloud VMs](cloud-vms.md),
> [Cloud Databases](cloud-databases.md), and [Kubernetes](kubernetes.md), a compose
> deployment is an **ephemeral workload**, not a persistent access target — there is no PRA
> tunnel, Password Safe onboarding, or Entitle registration for it. This doc is about
> *provisioning* container workloads and *monitoring* the dashboard's container fleet.

Related surfaces on the same page live in their own docs: the **On-Premises** tab
(Portainer) → [Portainer integration](integrations/portainer.md); the **Kubernetes
(Rancher)** tab → [Kubernetes](kubernetes.md) and [Rancher integration](integrations/rancher.md).
Note the **"Containers" nav link is gated on `portainer_enabled`** (default on) even though
the Cloud tab works regardless — a cloud-only operator who disables Portainer reaches it via
the direct `/containers` URL.

---

## Deploy Compose

The compose file is **referenced from the storage backend** (the same store used for
playbooks and Packer scripts) — upload it once on the [Storage](storage-management.md) page
and pick it from a dropdown at deploy time. Deploys run as background jobs; watch progress on
the Jobs page. The deploy endpoint requires the `containers:write` permission (deleting a GCE
compose instance requires `containers:delete`).

> A curated app catalog in front of this is intentionally deferred to the hosted SaaS
> edition — the community edition ships the generic "bring your own compose file" capability.

**Sample compose files.** Ready-to-adapt starters for common apps live in
[`examples/compose/`](../examples/compose/) — Apache Guacamole, Kasm, Trivy, Syft, Grype,
Cosign, OPA, Conftest, Checkov, Terraform/driftctl, and a Temporal worker. Upload one, edit
the placeholders, and deploy; each conforms to the supported subset below. See
[`examples/compose/README.md`](../examples/compose/README.md) for the per-file guide.

### Supported compose subset

Per service: `image` (required), `entrypoint`, `command`, `environment`, `ports`, `restart`,
and CPU/memory limits (`deploy.resources.limits.cpus` / `memory`, or the `cpus` / `mem_limit`
shorthands).

Unsupported keys — `build`, `volumes`, top-level `networks` / `volumes` / `secrets` /
`configs`, `depends_on`, `profiles`, `extends`, `env_file`, and host-passthrough env vars
(`- KEY` with no value) — are **rejected** with a clear error so a partial workload is never
deployed.

`entrypoint` overrides the image ENTRYPOINT and `command` overrides its CMD, the same as
Docker Compose. The three runtimes apply them consistently (ECS `entryPoint`+`command`; GCE
konlet `command`+`args`; ACI concatenates them into its single exec list — set both for
entrypoint-based images so ACI matches ECS/GCE).

### Target settings

Cluster / subnet / resource-group / zone settings default to the values already configured
in **Setup** (the same ones the Jumpoint and runners use). The deploy form's **Advanced**
section overrides them per deploy. Optional CPU / memory fields override the per-runtime
defaults.

| Runtime | Config keys (defaults) | Notes |
|---|---|---|
| **AWS ECS** | `bt_ecs_cluster` (`bt-jumpoint`), `bt_ecs_launch_type` (`EC2`), `ansible_ecs_subnet_id`, `ansible_ecs_security_group_ids`, `ansible_ecs_execution_role_arn`, `ansible_ecs_cpu`/`ansible_ecs_memory` (`256`/`512`), `aws_region` | Fargate task in the shared cluster |
| **Azure ACI** | `azure_aci_resource_group` (→ `azure_resource_group`), `azure_aci_subnet_id`, `azure_aci_cpu`/`azure_aci_memory` (`1.0`/`2.0`), `azure_location`, `azure_acr_server`/`_username`/`_password` | private-registry auth (ACR) is wired for **ACI only** in v1 |
| **GCP GCE-COS** | `gcp_project_id`, `gcp_zone`, `gcp_subnetwork`; machine type hardcoded `e2-small` | COS VM running the konlet spec |

---

## Monitoring the container fleet

The Cloud tab also lists container workloads the dashboard manages — and it's important to
know **most of them are shared infrastructure, not your compose deploys**:

- **ECS Tasks / ACI Containers / GCE Container Instances** — these lists mix your **compose
  deploys** with the **shared BeyondTrust jumpoint** and the **Ansible / image-promote / k8s
  runner** tasks. On GCE the rows carry a purpose badge: `Compose` (your deploy,
  `labels.purpose=compose`) vs `Jumpoint` (internal, `labels.purpose=bt-jumpoint`,
  auto-recreated).
- **GCP Cloud Run Jobs** — a **read-only** view of the 5 most recent dashboard-managed
  *runner* jobs (Ansible / promote / k8s). Cloud Run is **not** a compose deploy target.

So a container appearing here that you didn't deploy is usually the jumpoint or a runner —
leave it alone; the dashboard manages its lifecycle.

---

## Lifecycle

- **ECS** deployments appear in the ECS Tasks list; stop them there.
- **ACI** deployments appear in the ACI Containers list; stop them there.
- **GCE** deployments appear in the **GCE Compose Deployments** list
  (`labels.purpose=compose`); delete them there (`containers:delete`).

## Notes & limits

- **Private registry images:** v1 wires the configured ACR credentials for ACI; ECS/GCE pulls
  assume the image is public or reachable by the task/instance's role. Per-registry auth
  across all three providers is a follow-up.
- **GCE COS** runs containers on the instance's host network, so compose `ports` are
  informational there — reachability is governed by the instance's firewall tags/rules.

For the per-cloud network topology (jumpoint subnets, ECS cluster, ACI/COS placement) see
[Cloud Sandbox](CLOUD_SANDBOX.md).
