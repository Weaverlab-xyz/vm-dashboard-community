# Cloud Containers

The **Containers → Cloud** tab deploys a Docker Compose file to a managed cloud container
runtime — without going through Portainer — and monitors the container workloads the
dashboard runs across AWS, Azure, and GCP.

| Runtime | What a deploy becomes | Deploy target? |
|---|---|---|
| **AWS ECS** | a **Fargate task**, one container per compose service | ✅ |
| **Azure ACI** | a **container group**, one container per compose service | ✅ |
| **GCP GCE** | a **Container-Optimized OS** VM running all services as a multi-container [konlet](https://cloud.google.com/container-optimized-os/docs) spec (Cloud Run Jobs are single-container, so a COS instance is used instead) | ✅ |

The dashboard also runs three **managed single-container nodes** — the BeyondTrust
Gateway, the Portainer server and the Rancher management plane. Those are not compose
deploys and are not listed above, but they are the same idea (one container, one VM)
and they all run on **AWS, Azure or GCP**, chosen per deploy:

| Node | Doc | What it hosts |
|---|---|---|
| **Gateway** | [Gateway hosts](integrations/gateways.md) | the PRA broker every tunnel and jump goes through |
| **Portainer server** | [Portainer](integrations/portainer.md) | Portainer CE, managing remote Docker hosts via Edge agents |
| **Rancher node** | [Rancher](integrations/rancher.md) | the Kubernetes management plane every cluster is imported into |

> **The PAM layer stack does not apply here.** Unlike [Cloud VMs](cloud-vms.md),
> [Databases](databases.md), and [Kubernetes](kubernetes.md), a compose
> deployment is an **ephemeral workload**, not a persistent access target — there is no PRA
> tunnel, Password Safe onboarding, or Entitle registration for it. This doc is about
> *provisioning* container workloads and *monitoring* the dashboard's container fleet.

Related surfaces on the same page live in their own docs: the **Portainer** tab
→ [Portainer integration](integrations/portainer.md); the **Kubernetes
(Rancher)** tab → [Kubernetes](kubernetes.md) and [Rancher integration](integrations/rancher.md);
the **Gateways** tab → [Gateway hosts](integrations/gateways.md).
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
in **Setup** (the same ones the Gateway and runners use). The deploy form's **Advanced**
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
  deploys** with the **shared BeyondTrust gateway** and the **Ansible / image-promote / k8s
  runner** tasks. On GCE the rows carry a purpose badge: `Compose` (your deploy,
  `labels.purpose=compose`) vs `Gateway` (internal, `labels.purpose=bt-jumpoint`,
  auto-recreated).
- **GCP Cloud Run Jobs** — a view of the dashboard-managed *runner* jobs
  (Ansible / promote / k8s) that are **currently in flight**; runners that have finished are
  filtered out, so an empty list means nothing is running. Cloud Run is **not** a compose
  deploy target.

So a container appearing here that you didn't deploy is usually the gateway or a runner —
leave it alone; the dashboard manages its lifecycle.

### Gateways and the node firewalls

The **Gateways** tab lists both kinds of BeyondTrust Gateway host: the **managed** one the
dashboard auto-ensures and reference-counts, and the ones an operator **deploys** to carry
session load. Two lifecycle rules follow from that, and both are enforced for you:

- Every gateway host in a cloud uses that cloud's deploy key, so they all join the **same
  PRA Gateway cluster** as additional nodes — and PRA may broker a session through any one
  of them. So the source-restricted Rancher and Portainer nodes allow a `/32` for **every**
  live gateway, re-applied on each gateway deploy and teardown. A gateway that isn't in the
  allow list can reach nothing, which looks exactly like a broken Web Jump.
- A provisioned Rancher/Portainer **Web Jump holds a reference** on the shared gateway, so
  the idle teardown can't reclaim the host that brokers it. Without that, the last cloud
  DB or cluster going away took the management UIs offline with it.

The tab is gated on `pra_enabled`, and the **Gateways** tile in the dashboard's
Containers section deep-links straight to it (`/containers#gateways`). For the full story —
why the managed gateway can't be deleted, why the region picker offers only configured
regions, and the naming rules that keep the two kinds of host apart in the cloud — see
**[Gateway hosts](integrations/gateways.md)**.


### Reaping stranded runner jobs

A Cloud Run runner deletes its own job when the execution ends, but that delete is
best-effort — it sits in a `finally`, so restarting or redeploying the worker between the
execution finishing and the delete landing strands the job. Filtering those out of the list
above hides them; it doesn't reclaim them, and they accumulate in the project.

So the listing also **reaps** as it goes: it already walks every runner region and reads
every job's state, and it deletes the finished dashboard-managed jobs it finds. Three guards
bound what it can touch — the job must be labelled `managed-by=vm-dashboard`, its execution
must have **finished** (a pending or running runner is never touched, and a job whose state
can't be read counts as running), and it must have been finished for at least
`gcp_cloud_run_job_reap_age_minutes` (default 60, floored at 30). That age guard keeps the
sweep clear of a live runner's own cleanup, which fetches logs before deleting. Failures are
logged and never surfaced — a delete that 403s must not blank the panel.

| Setting | Default | Purpose |
|---|---|---|
| `gcp_cloud_run_job_reap_enabled` | `true` | Automatic sweep during the listing. Turning it off does **not** disable the manual action below. |
| `gcp_cloud_run_job_reap_age_minutes` | `60` | How long a finished job must sit before it may be reaped. Values below 30 are raised to 30. |

To clear a backlog on demand — after a redeploy, or when the automatic sweep is off —
`POST /api/containers/gce-cloud-run-jobs/reap` (needs `containers:delete`) runs the same
sweep explicitly and reports what it deleted.

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

For the per-cloud network topology (gateway subnets, ECS cluster, ACI/COS placement) see
[Cloud Sandbox](CLOUD_SANDBOX.md).
