# The Kubernetes runner

> **Audience:** operator · **Profile:** `both` · **Read this when:** your target is a Kubernetes cluster or a database rather than a VM.

Part of [Remote Worker](../ansible.md). Cluster and database targets, which run as localhost jobs rather than over SSH.

## Kubernetes runner

`k8s_runner_<cloud>` (falling back to the global `k8s_runner`) controls how the
dashboard runs **cluster-API operations** — `kubectl apply`, `kubectl delete`,
`helm repo add`/`helm upgrade`, `kubectl get secret` — for a cluster, chosen by
**that cluster's cloud**. These back the entitle agent install, the External
Secrets Operator (ESO) rollout, and mgmt-plane operations.

| Mode | How it runs |
|---|---|
| `local` (default) | In-process, via `k8s_service`'s subprocess helpers running `kubectl`/`helm` directly from the dashboard container. |
| `ecs` / `aci` / `gcp` | A one-shot stock `dtzar/helm-kubectl` task in the chosen cloud. The dashboard token-preps the kubeconfig server-side (swaps the cloud exec-auth block for a static bearer token), base64-encodes it into a secure env var, and pipes any secret-bearing manifest to the task over stdin — so the throwaway container needs **no cloud CLIs and no cloud credentials**. |

### When to use a cloud backend

Use `ecs` / `aci` / `gcp` when **direct `kubectl`/`helm` from the dashboard
host fails because of a TLS-inspecting corporate egress proxy.** The symptom is
a TLS / SSL-certificate error when the proxy inspects traffic to a cluster API
server that presents a **private-CA** cert it can't validate — for example an
**HTTP 526** ("invalid SSL certificate"), or the proxy's own block page. A
one-shot cloud task has clean egress to the cluster API and side-steps the
proxy entirely.

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

Pick the backend **per cluster cloud** in **Configuration → Remote Worker →
Kubernetes runner** (EKS / AKS / GKE each get their own Local-or-cloud
selector) and, if it's a cloud backend, make sure the
[shared cloud infrastructure](shared-cloud.md#shared-cloud-infrastructure) for that cloud is
set (the k8s runner reuses the Ansible runner's ECS / ACI / Cloud Run network
plumbing). Override the image only if you mirror `dtzar/helm-kubectl` to a
private registry — set `k8s_runner_image`.

---


## Kubernetes-cluster and database targets (localhost runs)

Registered or provisioned Kubernetes clusters and databases are selectable
Config-Management targets too, but they don't SSH anywhere — Ansible's
`kubernetes.core` and `community.postgresql`/`mysql`/`general` modules run a
`hosts: localhost, connection: local` play and connect *out* to the API server
(via a kubeconfig) or the DB endpoint (via login vars).

- **Dedicated runner image.** These runs use `chrweav/ansible-cloud` (config key
  `ansible_cloud_image`) — Debian-based, carrying `kubernetes.core`,
  `community.postgresql`, `community.mysql`, `community.general`, the `helm`/`kubectl`
  binaries, and `psycopg2`/`PyMySQL`/`pymssql`. Build/push it from
  [`runners/ansible-cloud/`](../../../runners/ansible-cloud) (multi-arch recommended)
  and, if you don't use public Docker Hub, mirror it to ECR/ACR/Artifact Registry.
  The winrm VM image is never used for these targets.
- **The runner follows line-of-sight to the endpoint.** Not a preference — the two
  cases point in opposite directions:
  - **Cloud-hosted (aws / azure / gcp).** The control plane / DB endpoint is private
    to its VPC, so the run executes on a transient ECS / ACI / Cloud Run task
    in-subnet, reusing the same `ansible_ecs_subnet_id` / `ansible_aci_subnet_id` /
    `gcp_ansible_vpc_connector` network config as the VM cloud runner. The local
    runner can't reach those RFC1918 endpoints and its egress hits the corporate TLS
    proxy, so an `ansible_runner_<cloud>: local` override is **rejected**. The
    backend is `ansible_runner_<cloud>`, defaulting to that cloud's native runner.
  - **On-prem (`cloud = local`).** A **registered** resource on your LAN, where an
    in-cloud task has **no route at all**: a Kubernetes cluster registered from a
    kubeconfig — see [`examples/playbooks/k3s/`](../../../examples/playbooks/k3s) for
    building one — or a database registered against a Password Safe managed account, see
    [Databases → Registering an existing database](../../databases.md#registering-an-existing-database).
    Those runs execute in a sibling container **on the dashboard host**, which is the only
    thing that can reach them: same `ansible_cloud_image`, same localhost play, same
    scrubbing. Two consequences — the dashboard host needs a working `docker` CLI and
    network reach to the endpoint (a dashboard deployed *in* a cloud has
    neither, and the run is refused with a message saying so), and local filesystem
    assets work fine, because the "move it to S3 first" rule exists only for the
    in-cloud runners that can't read this host's disk.

  Databases follow the same split. A **provisioned** database is always cloud-hosted; a
  **registered** one may be either — `cloud = local` for an on-premises database, or the
  cloud it already lives in. Registered **OCI** is the one gap: it can be registered, but
  no runner resolves for `oci`, so it is refused as a target.
- **Auto-injected, scrubbed connection material.** The kubeconfig is token-prepped
  server-side (a short-lived bearer token replaces the cloud exec-auth block) and
  delivered via `K8S_AUTH_KUBECONFIG`/`KUBECONFIG`. The DB admin credential comes from one
  of two places, following the row's source: a **provisioned** database's is read from its
  provisioning job plus the encrypted config store, while a **registered** one has no
  provisioning job — its Password Safe **managed account** is checked out just-in-time at
  launch and never persisted. Either way it arrives as `db_login_*` extra-vars.
  Both ride the runner task's ephemeral env and are redacted from job output. An
  operator can still bind extra Secrets-Management **named vars** (e.g. a new role's
  password) via **Use a secret**; SSH-only options (become password, SSH key,
  managed-account) don't apply.
- **Durability.** Dispatched by the job worker as an `ansible_cloud_run` job (it
  launches a cloud task that can outlive a request worker's recycle).

Starters: [`examples/playbooks/k8s/`](../../../examples/playbooks/k8s) and
[`examples/playbooks/database/`](../../../examples/playbooks/database). Smoke-test the
image directly with `docker run … chrweav/ansible-cloud ansible-playbook -i 'localhost,'
-c local …` against a kind/k3d cluster or a throwaway Postgres/MySQL container.

---


## Troubleshooting


### Kubernetes runner

**Direct kubectl/helm fails with HTTP 526 / TLS errors** — a corp egress
proxy is inspecting TLS to the cluster's private-CA API. Set that cluster's
cloud to a cloud backend (`k8s_runner_<cloud>` = `ecs` / `aci` / `gcp`) so the
op runs from a task with clean egress.

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
