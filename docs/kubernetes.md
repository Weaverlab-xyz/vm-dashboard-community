# Kubernetes

The dashboard provisions (or imports) managed Kubernetes clusters and layers management +
privileged access on top — the same **provisioning + stacked layers** model as
[Cloud Databases](cloud-databases.md) and [Cloud VMs](cloud-vms.md), adapted to Kubernetes:

- **Provision / register** *(stand it up)* — Terraform-build a new cluster, or register an
  existing/local one from a kubeconfig.
- **Management plane** — import the cluster into central **Rancher**, or install the
  **Portainer** agent; optionally install **External Secrets Operator** for secret delivery.
- **Access & identity** — the PAM story for clusters: **PRA tunnels** *(Layer 1 — reach it)*,
  **ESO / PRA vault token** *(Layer 2 — secrets)*, and **Entitle k8s JIT + Entra→RBAC
  federation** *(Layer 3 — time-boxed access)*.
- **Config Management** — run localhost Ansible plays against the cluster API.

The whole feature is gated by the **`k8s_management_enabled`** toggle (surfaces the `/k8s`
page + `/api/k8s`; permission scope `k8s`).

| Provider | Provision | Entra → RBAC federation | End-user reach |
|---|---|---|---|
| **AWS EKS** | ✅ Terraform (self-contained VPC) | shared Entra app as the cluster's **OIDC IdP** | API TCP tunnel + `kubectl oidc-login` |
| **Azure AKS** | ✅ Terraform (self-contained VNet) | **native managed-AAD** (federation is a no-op) | API TCP tunnel + `kubelogin` |
| **GCP GKE** | ✅ Terraform (self-contained VPC) | **Workforce Identity Federation + Connect Gateway** | Connect Gateway |
| **OCI OKE** | ⚠️ **experimental** — implemented in the service, **not surfaced** in the UI/Settings | — | — |

You can also **register/import** an existing or local cluster (`cloud = aws|azure|gcp|local`,
e.g. kind/k3s) from a full kubeconfig — no provisioning required.

> **OCI OKE status.** The service layer implements OKE provisioning
> (`terraform/k8s_cluster/oci_oke`, `_PROVISION_IMPLEMENTED` includes `oci`), but it is not
> exposed in the provision pickers/Settings and has no cloud runner (in-process kubectl
> only). Treat it as experimental; the router/model docstrings that say "aws/azure/gcp only /
> 501" predate it.

---

## Provision / register — per provider

All four Terraform modules (`terraform/k8s_cluster/{aws_eks,azure_aks,gcp_gke,oci_oke}`) are
**self-contained** — each builds its **own** network (VPC/VNet + subnets + egress) so
clusters don't consume sandbox subnets, and destroys it on decommission. Each exposes a
**stable egress IP** (module output `nat_public_ip` → `k8s_clusters.egress_ip`, auto-added
to the Rancher firewall whitelist). Provisioning assembles an exec-auth kubeconfig from the
module outputs, stores it as a secrets-backend reference, and flips the row to `registered`.

### AWS EKS

Builds its **own VPC** (default `10.97.0.0/16` — must **not** overlap the sandbox
`10.99.0.0/16`; give each concurrent cluster a distinct block) with 1 public + 2 private
subnets (EKS needs ≥2 AZs), an IGW, and a cheap **NAT *instance*** (arm64, holds an EIP for
the stable egress IP). Notable specifics:

- **IMDS hop limit = 2** on the node launch template — lets the IRSA-less EBS CSI controller
  reach IMDS for node-role creds (otherwise CrashLoopBackOff).
- **EBS CSI** addon is opt-in (`enable_ebs_csi`); needed for stateful workloads / a Rancher
  plane.
- **VPC-peers back to the sandbox VPC** and opens the DB SG (5432/3306/1433) + VM SG (22) so
  the cluster can reach sandbox DBs/VMs directly. **Decommission clusters before running the
  sandbox rollback** — rollback refuses while an active peering exists.

Config: `aws_vpc_id` (sandbox VPC to peer back to, import-only), `aws_eks_vpc_cidr`
(`10.97.0.0/16`), `aws_eks_k8s_version`, `aws_eks_node_instance_type`. `aws_k8s_subnet_a_id` /
`aws_k8s_subnet_b_id` are **vestigial** (still shown in Settings, ignored by the module).

### Azure AKS

Builds its **own VNet** (default `10.96.0.0/16`) with Azure CNI, egress via a **user-assigned
NAT gateway + static IP** (stable, whitelistable). Uses the **existing resource group**
(`azure_resource_group`, default `vm-cli-rg`) because the dashboard SP is RG-scoped. AAD-
integrated with Azure RBAC (`oidc_issuer_enabled` + `workload_identity_enabled`); creates a
**per-cluster Key Vault** + user-assigned managed identity + federated credential — the
Entitle agent's `azure_secret_manager` backend (the in-cluster Secrets path 401s on AKS).

Config (import-only): `azure_aks_k8s_version`, `azure_aks_node_vm_size`,
`azure_aks_authorized_cidrs`.

### GCP GKE

Builds a **self-contained VPC-native** cluster; private nodes, public control-plane endpoint
(restrict with `gcp_gke_authorized_cidrs`), egress via **Cloud Router + Cloud NAT + reserved
static IP**. Two connectivity modes (the service picks based on config):

- **Co-location** — the cluster runs *directly in* the sandbox VPC; reaches VMs **and** Cloud
  SQL private IP.
- **Peering** — the cluster gets its own VPC peered both ways (+ `…-allow-ssh-from-k8s`);
  reaches VMs only (GCP peering is **non-transitive**, so Cloud SQL stays on the PRA tunnel).

Config (import-only): `gcp_gke_k8s_version`, `gcp_gke_machine_type`, `gcp_gke_authorized_cidrs`;
connectivity from the region config's `network` / `k8s_subnetwork` + secondary-range names.

### Sandbox prerequisites

The sandbox scripts no longer create k8s subnets — clusters own their networks. The scripts
grant the k8s IAM/roles and emit the **peering inputs** the modules consume (AWS:
`aws_vpc_id`/`aws_vpc_cidr`/`aws_private_route_table_id` + DB/VM SGs; Azure: `azure_vnet_id`;
GCP: `gcp_network` or the co-location subnet + secondary ranges). See the "Managed
Kubernetes" row in [Cloud Sandbox](CLOUD_SANDBOX.md).

---

## Management plane

- **Central Rancher** (primary). A single privileged `rancher/rancher` container on a GCE
  Container-Optimized-OS VM (not a cluster), deployed/torn down from **Containers → Kubernetes
  (Rancher)**. Every managed cluster is **imported** — `cattle-cluster-agent` dials *out* to
  the node's public, source-restricted URL, so private clusters on any cloud/on-prem work with
  no inbound opening. Full setup + config table: [Rancher integration](integrations/rancher.md).
- **Portainer agent** — `POST /clusters/{id}/management` (kind `portainer`) applies the
  Portainer Agent via a transient kubectl container and registers it in the brokered Portainer
  server. See [Portainer integration](integrations/portainer.md).
- **External Secrets Operator (ESO)** — `POST /clusters/{id}/secret-delivery` Helm-installs
  ESO + a BeyondTrust `ClusterSecretStore` that syncs **Password Safe → Kubernetes Secrets**
  (auth via the `pscli_*` OAuth client). This is the Kubernetes expression of the **Password
  Safe (Layer 2)** problem. Config: `eso_namespace` (`external-secrets`),
  `eso_bt_credentials_secret`, `eso_bt_clustersecretstore`, `eso_bt_api_url`,
  `eso_bt_retrieval_type` (`SECRET`), `eso_bt_api_version` (`3.1`).

Cluster-API operations (`kubectl apply`, `helm`, secret reads) run as **transient runner
Jobs** on the job worker — in-process by default (`k8s_runner=local`) or as a one-shot cloud
task (ECS / ACI / Cloud Run) using stock `dtzar/helm-kubectl:latest`. The cloud path exists to
side-step a TLS-inspecting corporate proxy rejecting direct kubectl to a private-CA API.
Config: `k8s_runner` (`local|ecs|aci|gcp`), `k8s_runner_aws`/`_azure`/`_gcp`/`_oci`,
`k8s_runner_image`.

---

## Access & identity

Three per-cluster access paths (jobs run on the worker). Together they cover the PAM stack for
clusters: **PRA tunnels (Layer 1 — reach it)**, the **PRA vault token / ESO (Layer 2 —
secrets)**, and **Entitle + Entra federation (Layer 3 — time-boxed access)**.

- **PRA k8s tunnel** — `POST /clusters/{id}/tunnel` creates an `sra_protocol_tunnel_jump` with
  `tunnel_type=k8s` through the shared jumpoint host. Optional `vault_inject` mints a
  cluster-admin ServiceAccount bearer token in-cluster and stores it as a **PRA Vault token
  account** for injection at session launch (PRA-only access, no Entitle). **Caveat:** this
  proxy **strips `Impersonate-*` headers**, so `kubectl --as` does not work through it — use
  the API tunnel for impersonation. See [sra-provider-k8s-tunnel-bug](notes/sra-provider-k8s-tunnel-bug.md).
- **PRA API (TCP) tunnel** — `POST /clusters/{id}/api-tunnel` creates a `tunnel_type=tcp` jump
  straight to the API server on a pinned local port (`k8s_api_tunnel_local_port`, `6443`).
  Raw TCP, so kubectl authenticates end-to-end with the downloadable kubeconfig
  (`GET …/api-tunnel-kubeconfig`) and **can `--as` impersonate** Entitle grants.
- **Entra → k8s RBAC federation** — bind **one Entra security group** to cluster RBAC
  (`POST /clusters/{id}/entra-group`, default role `entra_rbac_group_role=cluster-admin`);
  members sign in **as themselves** (group Object ID is the RBAC subject), and Entitle's
  Entra-ID integration JIT-grants membership. Per-provider trust mechanism (full detail in
  [Entra ↔ Kubernetes federation](integrations/entra-k8s-federation.md); not to be confused
  with dashboard-login SSO in [oidc.md](integrations/oidc.md)):
  - **AKS** — native managed-AAD; federation is a no-op; auth via `kubelogin` over the API
    tunnel.
  - **EKS** — associates a shared **Entra app as the cluster's OIDC IdP**
    (`POST /clusters/{id}/entra-federation`); auth via `kubectl oidc-login` over the API tunnel.
    Config: `entra_oidc_client_id`, `entra_oidc_issuer_url`, `entra_oidc_username_claim`
    (`oid`), `entra_oidc_groups_claim` (`groups`).
  - **GKE** — **Workforce Identity Federation + Connect Gateway** (not the API tunnel). Config:
    `gcp_workforce_pool_id`, `gcp_workforce_provider_id`, `gcp_workforce_location` (`global`).
    EKS and GKE need **separate** Entra app registrations.
- **Entitle k8s JIT** — `POST /clusters/{id}/entitle-register` registers the cluster as an
  Entitle **Kubernetes** integration; the fine-grained tier is the **impersonator model**
  (`POST /clusters/{id}/impersonator` grants the Entra group cluster-wide `impersonate` on
  `users`; Entitle JIT-binds `<prefix>:<email>` → a role, and the user runs
  `kubectl --as=<prefix>:<email>` over the API tunnel). Config: `entitle_k8s_user_prefix`
  (`entitle`). Agent bootstrap via `POST /clusters/{id}/entitle-agent`. See the
  [Entitle integration](integrations/entitle.md) + [design/entitle-resource-registration.md](design/entitle-resource-registration.md).

Config: `entra_rbac_group_id` / `_name` / `_role` (`cluster-admin`), `pra_k8s_namespace`
(`pra-access`), `pra_k8s_sa_name` (`pra-access`), `k8s_api_tunnel_local_port` (`6443`),
`bt_vault_account_group_id`.

---

## Config Management

Registered/provisioned clusters appear in the [Config Management](config-management.md) target
dropdown. They are **not SSH targets** — `kubernetes.core` plays run `hosts: localhost,
connection: local` and reach the API via an injected token-prepped kubeconfig. These runs
**always** use a remote in-cloud runner (never local Docker) with the `ansible-cloud` image.
Starters live in `examples/playbooks/k8s/`. See [Config Management](config-management.md).

---

## Corporate TLS inspection

If your network TLS-inspects egress, the dashboard's own kubectl/helm to a private-CA API
server will fail. Either **trust the corporate root CA** in the dashboard container
(`onboard.sh --hub --corp-ca`, or bake `corp-ca/*.crt` into a from-source build) **or** use
the in-cloud **runners** (`k8s_runner=ecs|aci|gcp`), which get clean egress from inside the
cloud. This is not Kubernetes-specific — it's the same corp-CA story as the rest of the
dashboard.

---

## Troubleshooting

- **Sandbox rollback refuses / errors on AWS.** An EKS cluster still has an active VPC peering
  — **decommission clusters before rollback**.
- **EKS EBS CSI addon never goes ACTIVE.** IMDS hop limit or the CSI addon — the module sets
  hop-limit 2 and grants the node role `AmazonEBSCSIDriverPolicy` when `enable_ebs_csi` is on.
- **Cluster CIDR clash.** The EKS VPC CIDR (`aws_eks_vpc_cidr`) must not overlap the sandbox
  `10.99.0.0/16` or another concurrent cluster.
- **`kubectl --as` fails through the PRA k8s tunnel.** Expected — that proxy strips
  impersonation headers; use the **API (TCP) tunnel** for impersonation/Entitle grants.
- **kubectl to the API server fails behind a TLS-inspecting proxy.** Trust the corp CA or use
  a cloud runner (see above).

Source of truth: `web_dashboard/api/k8s.py`, `web_dashboard/services/k8s_service.py`, the
`terraform/k8s_cluster/*` modules, and `web_dashboard/api/setup.py` (`K8sManagementFeatureConfig`).
For the network topology see [Cloud Sandbox](CLOUD_SANDBOX.md).
