# Managed Kubernetes (k8s)

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are provisioning an EKS, AKS, GKE or OKE cluster from the dashboard, or registering one you already run in a cloud.

Part of [Kubernetes](../kubernetes.md).

The dashboard builds a managed cluster in each cloud with Terraform: **EKS**, **AKS**,
**GKE** and, experimentally, **OKE**. Each one gets its own network, a stable egress
address and an exec-auth kubeconfig the dashboard stores, then joins the same layers as
every other cluster: Rancher, External Secrets, PRA tunnels and Entitle (see the
[Kubernetes](../kubernetes.md) hub). An existing managed cluster can be **registered**
from its kubeconfig instead of provisioned.

| Provider | Provision | Entra → RBAC federation | End-user reach |
|---|---|---|---|
| **AWS EKS** | ✅ Terraform (self-contained VPC) | shared Entra app as the cluster's **OIDC IdP** | API TCP tunnel + `kubectl oidc-login` |
| **Azure AKS** | ✅ Terraform (self-contained VNet) | **native managed-AAD** (federation is a no-op) | API TCP tunnel + `kubelogin` |
| **GCP GKE** | ✅ Terraform (self-contained VPC) | **Workforce Identity Federation + Connect Gateway** | Connect Gateway |
| **OCI OKE** | ⚠️ **experimental** — Terraform (self-contained VCN) | ❌ none | API TCP tunnel (in-process `kubectl`) |

> **OCI OKE status.** Provisioning is wired end to end — the module
> (`terraform/k8s_cluster/oci_oke`), `_PROVISION_IMPLEMENTED`, the `provision_options` pickers,
> and the **`oci (OKE)`** entry in the Provision modal. Three gaps keep it **experimental**:
>
> - **No Entra → RBAC federation.** `enable_entra_federation` has `aws`/`azure`/`gcp` branches
>   only, so there is no "authenticate as yourself" path — access is the assembled exec
>   kubeconfig (`oci ce cluster generate-token`, token-minted server-side).
> - **No OCI-native runner.** `k8s_runner_service.mode()` resolves only
>   `local | ecs | aci | gcp`, so management-plane `kubectl`/`helm` against an OKE cluster runs
>   **in-process** in the dashboard container — which must therefore reach the cluster's public
>   API endpoint — unless you point `k8s_runner_oci` at another cloud's runner.
> - **Never live-validated.** The module was absent from the Dockerfile `COPY` set until
>   recently, so *no* published image could run it: `apply` failed in `_materialize` with
>   "Terraform module template not found". It needs a **rebuilt image** plus a first live
>   tenancy run. The same omission applied to the OCI *database* module — see
>   [Databases → OCI](../databases.md#oci-autonomous-database--read-the-caveats).
>
> Router/model docstrings that say "aws/azure/gcp only / 501" predate OKE.

## Provision — per provider

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
- **VPC-peers back to the sandbox VPC** and opens the DB SG (5432/3306/1433/1521) + VM SG (22) so
  the cluster can reach sandbox DBs/VMs directly. **Decommission clusters before running the
  sandbox rollback** — rollback refuses while an active peering exists.

Config: `aws_vpc_id` (sandbox VPC to peer back to, import-only), `aws_eks_vpc_cidr`
(`10.97.0.0/16`), `aws_eks_k8s_version`, `aws_eks_node_instance_type` — all editable in
**Settings → Kubernetes Management**; the Provision modal's **Cluster VPC CIDR** field overrides
`aws_eks_vpc_cidr` per-cluster. `aws_k8s_subnet_a_id` / `aws_k8s_subnet_b_id` are **vestigial**
(still shown in Settings, ignored by the module).

### Azure AKS

Builds its **own VNet** (default `10.96.0.0/16`) with Azure CNI, egress via the **AKS-managed
outbound load balancer pinned to our own static IP** (stable, whitelistable). Supplying the IP
replaces AKS's managed outbound IP, which would otherwise live in the opaque `MC_` node RG and
could rotate. This replaced a per-cluster user-assigned NAT gateway: same `/32` contract, ~40%
less per cluster-hour and ~9× less per GB. Uses the **existing resource group**
(`azure_resource_group`, default `vm-cli-rg`) because the dashboard SP is RG-scoped. AAD-
integrated with Azure RBAC (`oidc_issuer_enabled` + `workload_identity_enabled`); creates a
**per-cluster Key Vault** + user-assigned managed identity + federated credential — the
Entitle agent's `azure_secret_manager` backend (the in-cluster Secrets path 401s on AKS).

Config (import-only): `azure_aks_k8s_version`, `azure_aks_node_vm_size`,
`azure_aks_authorized_cidrs`.

Clusters provisioned **before** the load-balancer switch still hold a NAT gateway in their
Terraform state, and `terraform destroy` removes it from state even though the module no longer
declares it. After decommissioning one of those, confirm nothing was left billing:

```bash
az network nat-gateway list -g <rg> --query "[?tags.\"managed-by\"=='vm-dashboard'].{name:name,rg:resourceGroup}" -o table
```

### GCP GKE

Builds a **self-contained VPC-native** cluster; private nodes, public control-plane endpoint
(restrict with `gcp_gke_authorized_cidrs`), egress via **Cloud Router + Cloud NAT + reserved
static IP**. Two connectivity modes (the service picks based on config):

- **Co-location** — the cluster runs *directly in* the sandbox VPC; reaches VMs **and** Cloud
  SQL private IP.
- **Peering** — the cluster gets its own VPC peered both ways (+ `…-allow-ssh-from-k8s`);
  reaches VMs only (GCP peering is **non-transitive**, so Cloud SQL stays on the PRA tunnel).

The **private control plane** gets a per-cluster `/28`, allocated as the lowest free slot in
`gcp_gke_master_cidr_base` (`172.16.0.0/16`) — GCP materializes it as a
`gke-…-pe-subnet` subnetwork and rejects overlaps VPC-wide (**other regions included**), so a
shared base with one fixed `/28` only ever fits one cluster. Slots in use are read from each
cluster's provisioning job plus a live scan of every cluster's `masterIpv4CidrBlock`, so
orphans and hand-made clusters are skipped too — note the `pe-subnet` itself does **not** show
up in `gcloud compute networks subnets list`, so the cluster scan is the only way to see a
range that a failed/`ERROR` cluster still holds.

Config (import-only): `gcp_gke_k8s_version`, `gcp_gke_machine_type`, `gcp_gke_authorized_cidrs`,
`gcp_gke_master_cidr_base`; connectivity from the region config's `network` / `k8s_subnetwork` +
secondary-range names.

### OCI OKE — experimental

Builds a **self-contained VCN** (default `10.96.0.0/16` — must **not** overlap the sandbox VCN
`10.98.0.0/16`; give each concurrent cluster a distinct block) with api / nodes / lb subnets, an
IGW, a **NAT gateway** (its `nat_ip` is the stable egress IP), and a **service gateway** so nodes
reach the OKE control plane and OCIR without traversing the internet. The cluster is a
**`BASIC_CLUSTER`** (free control plane) with a FLANNEL overlay and a **public** API endpoint; the
node pool defaults to a single Always-Free **Ampere `VM.Standard.A1.Flex`** node at 2 OCPU / 12 GB
— the whole free Ampere allocation. Leave `node_image_id` blank and the module auto-picks the newest
Oracle-Linux image whose `OKE-<version>` suffix matches the cluster's **exact** patch version and
whose flavour matches the shape. Note OKE tags only its **ARM** images (`…-aarch64-…`) — the x86
images carry no arch token at all, so the match is by exclusion (no `aarch64` ⇒ x86, no `Gen2-GPU`
⇒ non-GPU); an `x86_64` name match finds nothing and leaves the node pool with an empty image.

Credentials reach Terraform as **`TF_VAR_*`** (`terraform_provider_env.oci_env()`) rather than
provider-native env vars — the module declares `tenancy_ocid` / `user_ocid` / `fingerprint` /
`private_key` / `private_key_passphrase` / `region` as variables. `region` has **no default**, but
`settings.oci_region` falls back to `us-ashburn-1`, so it is always populated — and, as with OCI
databases, the cluster **always lands in `oci_region`** regardless of the region picked in the form.

Config: `oci_oke_vcn_cidr` (`10.96.0.0/16`) is editable in **Settings → Kubernetes Management**,
and the Provision modal's **Cluster VCN CIDR** field overrides it per-cluster — it travels on the
same `vpc_cidr` request field AWS uses (there is no separate `vcn_cidr` field). `oci_oke_k8s_version`
/ `oci_oke_node_shape` stay import-only (they seed the form's version / node-size pickers).
Compartment from `oci_compartment_ocid` (falling back to `oci_tenancy_ocid`).

**Versions are resolved live, not pinned.** OKE retires Kubernetes versions every few months and
then hard-rejects them (`400 InvalidParameter, Invalid kubernetesVersion`), so nothing in this path
carries a hard-coded default: the module reads `oci_containerengine_cluster_option` and, when
`k8s_version` is blank, picks the newest version the region offers (echoed back as the `k8s_version`
output); the form's picker reads the same list through `oci_service.oke_cluster_versions()`, falling
back to `K8S_VERSIONS["oci"]` only when OCI is unconfigured. Versions use OKE's `v`-prefixed patch
format (`v1.36.1`); a version you pin explicitly is validated against the live list at **plan** time,
so a stale pin fails before the VCN is built rather than half-way through the apply.

**Node shapes are read live too.** OKE accepts only a **subset** of the compute shapes OCI offers,
and the subset varies by region and tenancy — `VM.Standard.E4.Flex` is a normal Compute shape that
OKE does not take in `us-chicago-1`, while the newer Ampere `VM.Standard.A2.Flex` is one it does.
A shape outside the subset is not rejected at submit: it fails at **node-pool creation**, ~10 minutes
into the apply, with the VCN and cluster already built. So the Node size picker reads
`oci_service.oke_node_pool_shapes()` (the API behind `oci ce node-pool-options get`), falling back to
`K8S_NODE_TYPES["oci"]` only when OCI is unconfigured. Unlike the version pin there is **no plan-time
gate**: the live list is scoped to one region and tenancy, so it seeds the picker but never rejects a
submission, and `oci_oke_node_shape` is always merged in first — a shape valid in another region
stays reachable through config. Shapes are ordered free-tier first, bare metal last
(`BM.Standard.E5.192` is a 192-OCPU machine billed whole, and the picker is where a lab cluster
gets sized).

### Sandbox prerequisites

The sandbox scripts no longer create k8s subnets — clusters own their networks. The scripts
grant the k8s IAM/roles and emit the **peering inputs** the modules consume (AWS:
`aws_vpc_id`/`aws_vpc_cidr`/`aws_private_route_table_id` + DB/VM SGs; Azure: `azure_vnet_id`;
GCP: `gcp_network` or the co-location subnet + secondary ranges). See the "Managed
Kubernetes" row in [Cloud Sandbox](../cloud/sandbox.md).
