# Remote Worker (Ansible, Kubernetes & image-promote runners)

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are configuring the runner images that execute Config Management, k8s and promote jobs.

> **Formerly "Ansible."** The Settings panel is now
> **Configuration → Remote Worker**. The doc path
> (`docs/integrations/ansible.md`) is unchanged so existing links resolve.

## What is it?

The **Remote Worker** panel configures the dashboard's **three** off-host
runners. Each runs its work as a **one-shot cloud task** — a container
launched in the target cloud, run once, and destroyed when it exits — and
all three **share the same per-cloud network settings** (cluster, subnet,
security group, role, ACR, VPC connector).

| Runner | Config key | What it runs | Backend chosen by |
|---|---|---|---|
| **Ansible runner** | `ansible_runner_<cloud>` (→ `ansible_runner`) | Config-management playbooks (`.yml`) and wrapped `.sh`/`.ps1`/`.rpm`/`.deb` assets on VMs over SSH / WinRM | the **run's target cloud** |
| **Kubernetes runner** | `k8s_runner_<cloud>` (→ `k8s_runner`) | Cluster-API ops — `kubectl apply/delete`, `helm …`, `kubectl get secret` (entitle agent install, External Secrets Operator, mgmt-plane) | the **cluster's cloud** |
| **Image-promote runner** | _(automatic, per target)_ | Convert + upload a built VM image into a cloud's image library (qemu-img → AMI / Azure Managed Image / GCE image) | the **promotion's target cloud** |

Each runner picks its backend **per the job's target cloud** (see
[Per-target-cloud backend](#per-target-cloud-backend)): an AWS-target job runs
on ECS Fargate, an Azure-target job on ACI, a GCP-target job on Cloud Run. The
Ansible and Kubernetes runners can also run **`local`** (a Docker sibling /
in-process); image-promote always runs in its target cloud.

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
- **Corp proxy** — a TLS-inspecting corporate egress proxy can't validate the
  cluster API's **private-CA** cert during inspection, so it rejects direct
  `kubectl`/`helm` calls (e.g. an HTTP `526` "invalid SSL certificate", or the
  proxy's own block page). A one-shot cloud task has **clean egress** and
  side-steps the proxy.

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

## Per-target-cloud backend

Each runner chooses its backend from the **job's target cloud**, not a single
global switch. The selector offers **Local** or **that cloud's matching task
service** — there is no cross-cloud option, because the network, identity, and
storage a job needs live in its target cloud:

| Target cloud | Ansible / Kubernetes backend |
|---|---|
| **AWS** (EC2 target / EKS cluster) | `local` or **ECS Fargate** (`ecs`) |
| **Azure** (Azure VM / AKS cluster) | `local` or **ACI** (`aci`) |
| **GCP** (GCE VM / GKE cluster) | `local` or **Cloud Run** (`gcp`) |

So you can run, say, Kubernetes ops for **EKS** clusters in-process but **AKS**
clusters on an ACI task (clean egress to `*.azmk8s.io`), and Ansible against AWS
VMs on Fargate but Azure VMs locally — each independently, per runner.

**Image-promote** is inherently per-target-cloud already: promoting to AWS runs
on ECS, to Azure on ACI, to GCP on Cloud Run (no `local` — a promote always
runs in the destination cloud, where it stages the converted disk).

### Config keys

| Key | Selects the backend for | Values |
|---|---|---|
| `ansible_runner_aws` / `_azure` / `_gcp` | the Ansible runner, by the run's target cloud | `local` \| matching service — blank inherits `ansible_runner` |
| `k8s_runner_aws` / `_azure` / `_gcp` | the Kubernetes runner, by the cluster's cloud | `local` \| matching service — blank inherits `k8s_runner` |
| `ansible_runner` / `k8s_runner` | global fallback for any cloud left blank | `local` \| `ecs` \| `aci` \| `gcp` |

A per-cloud key takes precedence; when blank, the runner falls back to the
global `ansible_runner` / `k8s_runner` (default `local`) — so existing
single-runner configs keep working unchanged. On open, the panel pre-fills the
per-cloud selectors from any global value (mapped to its matching cloud) so you
see the effective config and can then adjust each cloud.

---


## The pages

| Page | What's in it |
|---|---|
| [Remote Worker config field reference](ansible/config-reference.md) | Every field on the Settings panel, and the config key behind it. |
| [Shared cloud infrastructure for the runners](ansible/shared-cloud.md) | The cluster, registry and network each runner backend needs, and the per-cloud prerequisites. |
| [The Ansible runner](ansible/ansible-runner.md) | VM targets: SSH keys, discovery, provisioning assets, bulk runs and the local Docker runner. |
| [The Kubernetes runner](ansible/kubernetes-runner.md) | Cluster and database targets, which run as localhost jobs rather than over SSH. |
| [Secrets in a Remote Worker run](ansible/secrets.md) | Hardened per-provider lookups, Password Safe managed-account checkout, and in-playbook lookups. |
| [Playbook structure](ansible/playbooks.md) | What the runner expects of a playbook, and the samples to start from. |

The image-promote runner is documented with the runner itself, in
[`runners/promote/README.md`](../../runners/promote/README.md).

## Enable in the dashboard

1. Open **`/storage`** and configure at least one backend; pick it as active
   (required for the Ansible runner).
2. Open **Settings → Integrations**. The **Remote Worker** toggle, previously
   greyed out, is now selectable.
3. Click **Configure** on the Remote Worker row to set the
   [runner backends](ansible\config-reference.md#runner-backends) — pick Local or the matching cloud
   service per target cloud for each runner — the per-cloud SSH usernames, and,
   for cloud backends, the [shared cloud infrastructure](ansible\shared-cloud.md#shared-cloud-infrastructure).
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
