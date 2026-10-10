# Kubernetes

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are managing Kubernetes clusters and the privileged access into them.

The dashboard creates and manages three kinds of cluster. They share the layers on this
page (Rancher, External Secrets, PRA tunnels, Entitle and federation, workgroups and
Config Management), but they are built in different places, for different jobs:

| Kind | Where the dashboard builds it | Page |
|---|---|---|
| **Managed Kubernetes (k8s)**: EKS, AKS, GKE, OKE | Terraform in the cloud providers, from the Kubernetes page's **Provision** | [Managed Kubernetes](kubernetes/k8s.md) |
| **k3s** | Config Management plays for on-prem clusters, the OT demo cell and its DMZ broker, and the Workload Lab's SPIRE lab | [k3s](kubernetes/k3s.md) |
| **KubeSolo** | An optional Config Management play, for an edge or plant-floor host that will not carry a real cluster | [KubeSolo](kubernetes/kubesolo.md) |

The dashboard provisions (or imports) managed Kubernetes clusters and layers management +
privileged access on top — the same **provisioning + stacked layers** model as
[Databases](databases.md) and [Cloud VMs](cloud/vms.md), adapted to Kubernetes:

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

The provider-by-provider detail (what each cloud builds, its federation model and how
people reach it) is on [Managed Kubernetes](kubernetes/k8s.md). Any cluster can also be
**registered** from a kubeconfig instead of built: an existing managed cluster, or an
on-prem one (`cloud=local`), which [k3s](kubernetes/k3s.md#on-prem-clusters) covers.

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
  prerequisites: [Password Safe k8s token rotation](integrations/beyondtrust/password-safe/kubernetes-tokens.md#kubernetes-serviceaccount-token-rotation)
  and the [design note](design/k8s-sa-token-rotation.md).
- **PRA API (TCP) tunnel** — `POST /clusters/{id}/api-tunnel` creates a `tunnel_type=tcp` jump
  straight to the API server on a pinned local port (`k8s_api_tunnel_local_port`, `6443`).
  Raw TCP, so kubectl authenticates end-to-end with the downloadable kubeconfig
  (`GET …/api-tunnel-kubeconfig`) and **can `--as` impersonate** Entitle grants.
  The download is the cluster's stored kubeconfig with only the server repointed, so it is
  served **only when that kubeconfig authenticates through an exec plugin** (each person as
  themselves). One that embeds a credential — `token`, a client certificate or key, a
  password — is refused with 409 and the reason, because handing it out would give every
  `k8s:read` user the same unrevocable identity. An imported managed cluster with a static
  token is refused; re-register it with exec-plugin auth.
- **On-prem clusters: people get a Dex kubeconfig, and nothing else.** For a `cloud=local`
  cluster the same download is a `kubectl oidc-login` kubeconfig against Dex — the stored
  admin kubeconfig is never handed out ([design](design/agent-and-human-identity.md#on-prem-the-one-path-that-changes)).
  It needs two things, and the download says which is missing:
  1. **Settings → Kubernetes → Dex**: the issuer URL (exactly as Dex advertises it), the
     client ID (`kubernetes`, dex-helm.yml's public client) and, for a lab Dex with its own
     CA, that CA (embedded in the kubeconfig).
  2. **Trusts Dex** on the cluster's row (`POST /clusters/{id}/dex-trust`, `k8s:write`) —
     turn it on once `k3s/k3s-dex-auth.yml` has run on the cluster. It is the operator's
     statement that the API server trusts Dex; the dashboard cannot probe it without a
     person's token.

  The user needs the [kubelogin](https://github.com/int128/kubelogin) plugin
  (`kubectl oidc-login`); the first `kubectl` opens a browser to Dex. Usernames arrive as
  `dex:<email>`, groups as `dex:<group>` — bind RBAC to those.
- **On-prem clusters: the dashboard's own access, without the admin certificate (preview).**
  The dashboard itself still operates a `cloud=local` cluster (manifests, Helm, RBAC,
  ServiceAccount tokens, Ansible k8s targets) with the admin kubeconfig from
  `k3s-kubeconfig.yml` — a `system:masters` client certificate that never expires. With
  the [dashboard's own SPIFFE identity](oidc/dashboard-identity.md#on-prem-k3s)
  turned on, it can use a short-lived token instead:
  1. Run `k3s/k3s-dashboard-auth.yml` on the server node. **Trusts dashboard identity?** on
     the cluster's row shows the exact command, with this cluster's audience
     (`<issuer>/k8s/<cluster id>`) filled in. It adds one JWT authenticator to the same
     `AuthenticationConfiguration` the Dex and lab plays share (prefix `dashboard:`), and
     binds `dashboard:spiffe://<trust domain>/dashboard` to `cluster-admin` — the power the
     admin certificate already has.
  2. Turn the flag on (`POST /clusters/{id}/spiffe-trust`, `k8s:write`, audited).

  From then on every routine call gets a kubeconfig whose only user is a thirty-minute
  JWT-SVID minted for that cluster's audience; the server and CA are kept, the client
  certificate is dropped. A token minted for one cluster is refused by every other.
  **There is no silent fallback:** if the token cannot be minted (SPIRE down, identity
  switched off) the operation fails and names the cause. **Break-glass** is unticking the
  flag, which puts the stored admin kubeconfig back in use at once; it is never deleted.
  The people-facing API-tunnel download is unaffected — it still builds the Dex kubeconfig.
- **Entra → k8s RBAC federation** — bind **one Entra security group** to cluster RBAC
  (`POST /clusters/{id}/entra-group`, default role `entra_rbac_group_role=cluster-admin`);
  members sign in **as themselves** (group Object ID is the RBAC subject), and Entitle's
  Entra-ID integration JIT-grants membership. Per-provider trust mechanism (full detail in
  [Entra ↔ Kubernetes federation](oidc/entra-k8s-federation.md); not to be confused
  with dashboard-login SSO in [oidc.md](oidc.md)):
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
  [Entitle integration](integrations/beyondtrust/entitle.md) + [design/entitle-resource-registration.md](design/entitle-resource-registration.md).
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
      [the federation guide §1b](oidc/entra-k8s-federation.md#1b-gke-app-registration-eg-gke-entra-wif)
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

Config: `entra_rbac_group_id`, `entra_rbac_group_name` (display only) and `entra_rbac_group_role` (`cluster-admin`), `pra_k8s_namespace`
(`pra-access`), `pra_k8s_sa_name` (`pra-access`), `k8s_api_tunnel_local_port` (`6443`),
`bt_vault_account_group_id`.

---

## Workgroups (who can see a cluster)

A cluster with **no workgroup** is visible only to whoever registered or provisioned it,
plus administrators — which is what every cluster predating this field still is.

Assign one and the whole workgroup can see and manage it:

- **At creation** — the **Workgroup** select on both the register and provision forms.
  Optional; blank leaves the cluster creator-scoped. You may only pick a workgroup you
  belong to, since the workgroup rule outranks the creator rule.
- **Afterwards** — the **Workgroup** button on the row, administrators only, which is the
  only way an existing cluster gets one. Clearing it returns the cluster to its creator.

**Weigh this more carefully here than for a VM.** The kubeconfig behind a cluster row is
**cluster-admin** — that is why only `kubeconfig_ref` is stored rather than the document
itself. Tagging a cluster therefore hands its whole workgroup the console link, the
API-tunnel and Entra kubeconfig downloads, brokered access, tunnel and binding changes,
Password Safe token registration, decommission, and its
[auto-delete timer](operations/auto-delete-timer.md). It is a grant, not a label.

Requesting a cluster you cannot see answers **404**, not 403 — the same rule the POV and
lab pages use, so an id cannot be confirmed by probing for it.

See [Permissions](access/permissions.md) for how workgroups sit alongside permission scopes.

---

## Config Management

Registered/provisioned clusters appear in the [Config Management](operations/config-management.md) target
dropdown. They are **not SSH targets** — `kubernetes.core` plays run `hosts: localhost,
connection: local` and reach the API via an injected token-prepped kubeconfig. These runs
**always** use a remote in-cloud runner (never local Docker) with the `ansible-cloud` image.
Starters live in `examples/playbooks/k8s/`. See [Config Management](operations/config-management.md).

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
For the network topology see [Cloud Sandbox](cloud/sandbox.md).
