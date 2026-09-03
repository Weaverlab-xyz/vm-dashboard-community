# Kubernetes

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are managing Kubernetes clusters and the privileged access into them.

The dashboard provisions (or imports) managed Kubernetes clusters and layers management +
privileged access on top — the same **provisioning + stacked layers** model as
[Databases](databases.md) and [Cloud VMs](cloud-vms.md), adapted to Kubernetes:

- **Provision / register** *(stand it up)* — Terraform-build a new cluster, or register an
  existing/local one from a kubeconfig.
- **Management plane** — import the cluster into central **Rancher**; optionally install
  **External Secrets Operator** for secret delivery.
- **Access & identity** — the PAM story for clusters: **PRA tunnels** *(Layer 1 — reach it)*,
  **ESO / PRA vault token, with Password Safe owning the token's rotation** *(Layer 2 —
  secrets)*, and **Entitle k8s JIT + Entra→RBAC federation** *(Layer 3 — time-boxed access)*.
- **Config Management** — run localhost Ansible plays against the cluster API.

The whole feature is gated by the **`k8s_management_enabled`** toggle (surfaces the `/k8s`
page + `/api/k8s`; permission scope `k8s`).

| Provider | Provision | Entra → RBAC federation | End-user reach |
|---|---|---|---|
| **AWS EKS** | ✅ Terraform (self-contained VPC) | shared Entra app as the cluster's **OIDC IdP** | API TCP tunnel + `kubectl oidc-login` |
| **Azure AKS** | ✅ Terraform (self-contained VNet) | **native managed-AAD** (federation is a no-op) | API TCP tunnel + `kubelogin` |
| **GCP GKE** | ✅ Terraform (self-contained VPC) | **Workforce Identity Federation + Connect Gateway** | Connect Gateway |
| **OCI OKE** | ⚠️ **experimental** — Terraform (self-contained VCN) | ❌ none | API TCP tunnel (in-process `kubectl`) |

You can also **register/import** an existing or local cluster (`cloud = aws|azure|gcp|local`,
e.g. kind/k3s) from a full kubeconfig — no provisioning required. To *build* that on-prem
cluster in the first place, [`examples/playbooks/k3s/`](../examples/playbooks/k3s/) stands
up k3s over SSH and emits a registration-ready kubeconfig. A registered `cloud=local`
cluster is a Config Management target like any other, except that its runs execute in a
sibling container **on the dashboard host** rather than on an in-cloud runner — an on-prem
API endpoint is reachable from your LAN, not from an ECS task. That host therefore needs a
`docker` CLI and a route to the cluster. One caveat documented with the playbooks: a
registered kubeconfig is stored and used verbatim — no token is minted for it — so it is
standing cluster-admin for every run made against it.

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
>   [Databases → OCI](databases.md#oci-autonomous-database--read-the-caveats).
>
> Router/model docstrings that say "aws/azure/gcp only / 501" predate OKE.

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
Kubernetes" row in [Cloud Sandbox](CLOUD_SANDBOX.md).

---

## Management plane

- **Central Rancher** (primary). A single privileged `rancher/rancher` container on one VM
  (not a cluster), hosted on **AWS, Azure or GCP** — you pick which at deploy time —
  deployed/torn down from **Containers → Kubernetes (Rancher)**. Every managed cluster is
  **imported**: `cattle-cluster-agent` dials *out* to the node's public, source-restricted
  URL, so private clusters on any cloud/on-prem work with no inbound opening, whichever
  cloud the node itself is in. Full setup + config table:
  [Rancher integration](integrations/rancher.md).
- **External Secrets Operator (ESO)** — `POST /clusters/{id}/secret-delivery` Helm-installs
  ESO + a BeyondTrust `ClusterSecretStore` that syncs **Password Safe → Kubernetes Secrets**
  (auth via the `pscli_*` OAuth client). This is the Kubernetes expression of the **Password
  Safe (Layer 2)** problem. Config: `eso_namespace` (`external-secrets`),
  `eso_bt_credentials_secret`, `eso_bt_clustersecretstore`, `eso_bt_api_url`,
  `eso_bt_retrieval_type` (`SECRET`), `eso_bt_api_version` (`3.1`). See the **Secret
  delivery walkthrough** below for end-to-end usage with the `examples/k8s/` manifests.

Cluster-API operations (`kubectl apply`, `helm`, secret reads) run as **transient runner
Jobs** on the job worker — in-process by default (`k8s_runner=local`) or as a one-shot cloud
task (ECS / ACI / Cloud Run) using stock `dtzar/helm-kubectl:latest`. The cloud path exists to
side-step a TLS-inspecting corporate proxy rejecting direct kubectl to a private-CA API.
Config: `k8s_runner` (`local|ecs|aci|gcp`), `k8s_runner_aws`/`_azure`/`_gcp`/`_oci`,
`k8s_runner_image`.

### Secret delivery walkthrough (ESO)

Installing ESO only stands up the *plumbing* — the operator plus the
`beyondtrust-store` ClusterSecretStore. Nothing syncs until a workload declares an
`ExternalSecret` naming a Password Safe entry. End to end:

1. **Install the plumbing (once per cluster).** Run the secret-delivery action —
   `POST /clusters/{id}/secret-delivery` (kind `eso`), or the **Secrets** button on
   the cluster. It Helm-installs ESO into `external-secrets` and applies the
   BeyondTrust `ClusterSecretStore` (`beyondtrust-store`), authenticated with the
   `pscli_*` OAuth client. (Prerequisite: Password Safe OAuth must be configured.)
2. **Store the credential in Password Safe.** Create it in Secrets Safe (or a managed
   account) and note its path — that becomes the `ExternalSecret`'s `remoteRef.key`.
   The path format follows `eso_bt_retrieval_type`: `SECRET` → `folder/title`,
   `MANAGED_ACCOUNT` → `system/account`.
3. **Declare an `ExternalSecret`.** Apply a manifest referencing
   `secretStoreRef: { kind: ClusterSecretStore, name: beyondtrust-store }` that maps
   Password Safe entries → keys in a `target` Secret. ESO reconciles it and creates a
   native Kubernetes `Secret` — no secret value ever lives in the manifest, only the
   pointer.
4. **Consume the Secret** from your Deployment/StatefulSet like any other Secret —
   `secretKeyRef` for a single key, or `envFrom.secretRef` to load every key as an
   env var.

Ready-to-adapt starters in [`examples/k8s/`](../examples/k8s/):

- [`app-externalsecret.yaml`](../examples/k8s/app-externalsecret.yaml) — the minimal
  case: one key (`DB_PASSWORD`) → env via `secretKeyRef`.
- [`app-db-externalsecret.yaml`](../examples/k8s/app-db-externalsecret.yaml) — a
  multi-key connection bundle loaded wholesale with `envFrom.secretRef`.
- [`redis-eso-statefulset.yaml`](../examples/k8s/redis-eso-statefulset.yaml) — a
  stateful example: Redis `requirepass` sourced from Password Safe.

(`app-secret.yaml` ships the inline-`Secret` anti-pattern these replace — a literal
credential in the manifest, landing in git and etcd in clear text.)

---

## Access & identity

Three per-cluster access paths (jobs run on the worker). Together they cover the PAM stack for
clusters: **PRA tunnels (Layer 1 — reach it)**, the **PRA vault token / ESO (Layer 2 —
secrets)**, and **Entitle + Entra federation (Layer 3 — time-boxed access)**.

- **PRA k8s tunnel** — `POST /clusters/{id}/tunnel` creates an `sra_protocol_tunnel_jump` with
  `tunnel_type=k8s` through the shared gateway host. Optional `vault_inject` mints a
  cluster-admin ServiceAccount bearer token in-cluster and stores it as a **PRA Vault token
  account** for injection at session launch (PRA-only access, no Entitle). Once the token is
  Password Safe-managed (below) the dashboard **reads it from Password Safe instead of
  minting**, and removing the tunnel no longer deletes the ServiceAccount — every issued
  token is bound to its uid. **Caveat:** this
  proxy **strips `Impersonate-*` headers**, so `kubectl --as` does not work through it — use
  the API tunnel for impersonation. See [sra-provider-k8s-tunnel-bug](notes/sra-provider-k8s-tunnel-bug.md).
- **Password Safe token rotation** — `POST /clusters/{id}/ps-token` onboards the injected
  ServiceAccount token (`<pra_k8s_namespace>/<pra_k8s_sa_name>`, default
  `pra-access/pra-access`) as a Password Safe **managed account** on the *Kubernetes Service
  Account Token* plugin, applies the in-cluster rotator RBAC, and registers a *PRA Vault
  Token* managed account — closing the gap where the vaulted token was minted once and never
  rotated. Registration then **syncs** the PRA Vault account to the token account
  (`POST ManagedAccounts/{id}/SyncedAccounts/{syncedAccountID}`); a managed account and its
  subscribers always share a credential, so Password Safe delivers every rotation to PRA
  itself and the dashboard runs nothing on a schedule. `…/ps-token/rotate` rotates on demand;
  `GET …/ps-token/status` reads the live link state out of Password Safe; `DELETE …/ps-token`
  unlinks the pair and off-boards both managed systems. **In the default LongLived mode
  rotation revokes the old token**, so use Bound mode (Settings → token mode) on clusters
  whose tunnel must not break, since Bound never revokes. Full detail and the operator
  prerequisites: [Password Safe k8s token rotation](integrations/password-safe.md#kubernetes-serviceaccount-token-rotation)
  and the [design note](design/k8s-sa-token-rotation.md).
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
  `users`; Entitle JIT-binds `<prefix>:<sanitized-email>` → a role, and the user runs
  `kubectl --as=<prefix>:<sanitized-email>`). Config: `entitle_k8s_user_prefix`
  (`entitle`). Agent bootstrap via `POST /clusters/{id}/entitle-agent`. See the
  [Entitle integration](integrations/entitle.md) + [design/entitle-resource-registration.md](design/entitle-resource-registration.md).
  - **GKE needs BOTH halves — CONFIRMED LIVE 2026-07-30** with a real Entra-federated
    workforce identity on `gcp-east`. GKE does **not** honor the Kubernetes `impersonate`
    verb: with the `entitle-impersonator` ClusterRole/Binding correctly in place and *no*
    Cloud IAM impersonate permission, `--as` fails with
    `users "entitle:…" is forbidden: User "principal://…/subject/…" cannot impersonate
    resource "users" … requires one of ["container.clusters.impersonate"] permission(s)
    in Cloud IAM or a Kubernetes RBAC role with verb "impersonate"`. Binding the group's
    `principalSet` to a role carrying `container.clusters.impersonate` makes `--as`
    succeed. Kubernetes RBAC *is* live for these identities — the same group's
    `entra-group-binding` → `view` was proven to be the sole source of read access once the
    masking Cloud IAM binding was removed — so this is impersonation-specific, not a
    general RBAC gap.
    - **⚠️ Prerequisite that invalidated hours of testing: the Entra group must be
      ASSIGNED TO THE WIF ENTERPRISE APP** or it never appears in the token's `groups`
      claim, and *every* binding on it (RBAC and Cloud IAM) silently matches nothing. Read
      the ⚠️ in
      [the federation guide §1b](integrations/entra-k8s-federation.md#1b-gke-app-registration-eg-gke-entra-wif)
      before debugging anything else. Until the group was assigned, three escalating IAM
      grants (custom role, custom role + `container.clusters.get`, then
      `roles/container.admin`) all appeared to do nothing, which looked like a GKE
      authorizer limitation and was not.
    - **Verify a claim is landing before trusting any result.** Access can be served by a
      *different* group's Cloud IAM binding — here a basic `roles/viewer` on the sign-in
      group — which makes `kubectl get ns` succeed whether or not the group you care about
      is present. The clean probe is to remove the masking binding and see what survives.
      The same confound makes the 2026-07-16 "GKE group binding validated" result
      unreliable; the binding is now properly validated as of this entry.
    - **A standing `roles/viewer` on any Entra group also masks JIT revocation** — access
      survives grant expiry, so a demo shows the opposite of what it claims. Leave it off.
    - **Entitle's two grants are separate.** Requesting the *Kubernetes* resource creates
      the `entk8s-*` binding but grants no Entra group membership (that is an
      Entra-ID-integration bundle). Both must be live at once, and both were short-TTL
      here, so re-check each right before testing.
    - Unexplained leftover: `kubectl auth can-i` → `Forbidden: unknown (post
      selfsubjectaccessreviews.authorization.k8s.io)` even un-impersonated, though
      `system:basic-user` grants that to `system:authenticated` everywhere. Suggests the
      gateway-injected identity may not carry `system:authenticated`. Harmless, but it is
      why the `kubectl auth` probes are useless here.

    None of the Enable-federation roles
    (`roles/gkehub.gatewayEditor`, `roles/gkehub.viewer`) carry the permission, so
    `apply_impersonator_binding`'s GCP path also calls
    `gcp_service.grant_impersonate_iam`: create-or-reuse the project custom role
    `dashboardGkeImpersonator` holding **only** `container.clusters.impersonate`, then
    bind the group's `principalSet` to it. Notes:
    - **One permission is enough — verified live 2026-07-30.** `container.clusters.get` was
      added to the role as a hypothesis and then removed; `--as` kept working with
      `container.clusters.impersonate` alone, so that is all the role should carry.
    - **`roles/iam.roleAdmin`** on the dashboard SA is required to create that role
      (added to both sandbox setup scripts — **re-run** yours, or pre-create the role by
      hand); without it the action fails with a 403 naming the missing role.
    - Not `roles/container.admin`, which carries the permission but also hands the group
      standing cluster admin and defeats the fine-grained story.
    - **Cloud IAM propagation is ~1–2 min** — a `--as` inside that window still 403s and
      looks identical to a missing grant. The job's final progress line says so.
    - The binding is **project-level** (GKE clusters have no IAM policy of their own), so
      removal **ref-counts**: it is revoked only when no other GKE cluster still has the
      same group bound. The custom role itself is left in place.
    - Unlike group claims, Cloud IAM is evaluated per request — **no `gcloud auth login`
      re-run needed** after the grant.
    - **`container.clusters.impersonate` is in Google's `TESTING` stage.** Confirmed by
      hand 2026-07-30: it *is* allowed in a custom role, but `gcloud iam roles create`
      warns it is "not mature and they can go away in the future … do not use them in
      production systems". The custom role is GA; the permission in it is not. Treat this
      tier as lab-grade on GKE, and suspect a Google-side change first if the grant
      starts failing with a permission-not-recognised error.
    - If you already created a role by hand to unblock a demo (e.g. `gkeImpersonator`),
      the action will create its own `dashboardGkeImpersonator` alongside it rather than
      adopt it — the distinct name keeps the dashboard from patching an operator-owned
      role. Both grant the same thing; delete the manual role and its binding once the
      automated path is verified.
  - **The subject is not the raw email.** Entitle sanitizes it when it builds the
    binding — `karen.walker@weaverlab.xyz` became `entitle:karen.walker-weaverlab.xyz`
    (the `@` → `-`), while the credential Entitle shows the user still reads
    `karen.walker@weaverlab.xyz`. Nothing in this repo does that rewrite, so don't assume
    the rule — read the binding:
    `kubectl get clusterrolebinding -o custom-columns=NAME:.metadata.name,ROLE:.roleRef.name,SUBJECT:.subjects[*].name`
    (Entitle's are named `entk8s-<hash>`).
  - **The `kubectl auth` self-review probes are useless on GKE via Connect Gateway.**
    A workforce identity there is denied both self-review APIs *regardless of `--as`* —
    `auth whoami` → `the selfsubjectreviews API is not enabled in the cluster or you do
    not have permission to call it` (which reads like a cluster feature gap), and
    `auth can-i` → `Forbidden: unknown (post selfsubjectaccessreviews.authorization.k8s.io)`
    — even while ordinary reads (`get ns`, `get clusterrolebinding`) succeed. Normally
    `system:basic-user` grants these to `system:authenticated`, so the gateway-injected
    identity appears not to carry that group. Probe with a real verb
    (`kubectl --as=… get ns`) instead, and never read a self-review failure as evidence
    about impersonation.
  - **Transport:** the API (TCP) tunnel on EKS/AKS. On **GKE with a workforce identity**
    it's **Connect Gateway** — the API tunnel is not an option there at all, since the
    GKE API server can't validate a workforce token. Connect Gateway *does* forward
    `Impersonate-User` (confirmed live 2026-07-30: the denial above is a GKE authorizer
    decision about the impersonation attempt, so the header reached the API server).

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
  `10.99.0.0/16` or another concurrent cluster; same for the OKE VCN CIDR (`oci_oke_vcn_cidr`)
  against the sandbox VCN `10.98.0.0/16`. Both are overridable per-cluster in the Provision modal.
- **GKE apply fails "Conflicting IP cidr range … conflicts with existing subnetwork
  `gke-…-pe-subnet`".** Two clusters want the same control-plane `/28`. Ranges are allocated
  per cluster now; if it recurs, the live scan couldn't run (check the provision log for
  "live range scan failed" — the SA needs `compute.subnetworks.list` +
  `container.clusters.list`) or `gcp_gke_master_cidr_base` is exhausted/overlapping.
- **`kubectl --as` fails through the PRA k8s tunnel.** Expected — that proxy strips
  impersonation headers; use the **API (TCP) tunnel** for impersonation/Entitle grants.
- **kubectl to the API server fails behind a TLS-inspecting proxy.** Trust the corp CA or use
  a cloud runner (see above).

Source of truth: `web_dashboard/api/k8s.py`, `web_dashboard/services/k8s_service.py`, the
`terraform/k8s_cluster/*` modules, and `web_dashboard/api/setup.py` (`K8sManagementFeatureConfig`).
For the network topology see [Cloud Sandbox](CLOUD_SANDBOX.md).
