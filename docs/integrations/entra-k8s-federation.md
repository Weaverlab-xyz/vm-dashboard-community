# Entra ID → Kubernetes real-identity federation

Bind **one Entra (Azure AD) security group** to cluster RBAC and let its members sign
in to your clusters **as themselves** — their Entra token carries the group's Object
ID, which matches a Kubernetes `Group` subject. Entitle's Entra-ID integration
JIT-grants group membership, so access appears (and expires) just-in-time with **no
impersonation and no synthetic subject**.

This works natively on **AKS**. On **EKS** it needs the cluster to *trust Entra as an
OIDC identity provider*; on **GKE** it uses **Workforce Identity Federation + Connect
Gateway**. The one-click **Entra federation** action wires up whichever the cluster
needs. Either way it's the *same Entra group* — on GKE the RBAC subject is that group
Object ID wrapped in a workforce `principalSet` URI.

| Cloud | Trust mechanism | End-user auth | Reached via |
|---|---|---|---|
| **AKS** | native managed-AAD (no action needed) | Azure `kubelogin` | API tunnel |
| **EKS** | **Entra federation** action → OIDC identity provider | `kubectl oidc-login` (int128) | API tunnel |
| **GKE** | Workforce Identity Federation *(follow-up)* | `gcloud auth login` | Connect Gateway |

---

## The pieces

1. **Entra group → RBAC** — the per-cluster **Entra group** action binds the group's
   Object ID to a ClusterRole (default `cluster-admin`). The same group works on every
   cloud (the group Object ID is tenant-wide).
2. **Entra federation** — the per-cluster action that makes the cluster *trust* Entra.
   AKS is native; EKS associates a shared Entra app as its OIDC identity provider.
3. **API tunnel** — the existing per-cluster tunnel that reaches a private cluster's
   API server (`kubectl` → `localhost:6443` → the endpoint).
4. **Entitle Entra-ID integration** — configured once in the Entitle console; it
   JIT-grants membership in the group from (1). Nothing to configure in the dashboard.

---

## One-time setup

### 1. Shared Entra app registration ("Kubernetes federation")

Create one app registration in Entra; it serves EKS OIDC (and, later, GKE WIF):

- Note the **Application (client) ID** (the OIDC audience) and your **tenant ID**
  (issuer `https://login.microsoftonline.com/<tenant>/v2.0`).
- **Authentication → Add a platform → Mobile and desktop applications**: add redirect
  URIs `http://localhost:8000` and `http://localhost:18000` (int128 `oidc-login`
  authcode flow), and set **Allow public client flows = Yes** (enables device-code for
  headless machines).
- **Token configuration → Add groups claim → Security groups**, on the **ID token**
  (or set `groupMembershipClaims: "SecurityGroup"` in the manifest). This makes Entra
  emit group **Object IDs** in the `groups` claim — the value the RBAC binding matches.
- **API permissions**: delegated `openid`, `profile`, `email`.

### 2. Dashboard settings (Settings → Kubernetes)

- **Entra group → cluster RBAC**: set the group Object ID (+ optional name) and the
  ClusterRole.
- **Entra OIDC federation (EKS)**: set **App (client) ID**. Leave **Issuer URL** blank
  to derive it from the tenant id. Username/groups claims default to `oid`/`groups`.

### 3. Entitle Entra-ID integration

In the Entitle console, connect the Entra-ID integration and publish a resource that
grants membership in the group from step 2. (No dashboard change.)

---

## Federate an EKS cluster (the demo)

1. **Entra federation → Enable federation.** EKS associates the shared Entra app as
   the cluster's OIDC identity provider and the job polls until it's **ACTIVE** (a few
   minutes; the cluster shows `UPDATING` on AWS). This is additive — IAM / `aws-auth`
   access is unchanged, and node bootstrap + console access stay on IAM.
2. **Entra group → Bind group** — binds the group's Object ID to the ClusterRole.
3. **API tunnel → Create tunnel**, then connect it in the BeyondTrust rep console so
   `localhost:6443` forwards to the private API server.
4. **Entra federation → Download Entra kubeconfig.** This is token-free — it
   authenticates as the *user's* Entra identity via `kubectl oidc-login`.
5. On the user's machine (needs `kubectl` + the `oidc-login` plugin):
   ```
   set KUBECONFIG=<downloaded>-entra.kubeconfig
   kubectl get ns          # opens a browser / device-code sign-in to Entra
   ```
   The user must have a live Entitle grant for the group. Revoke the grant → access
   goes away.

### Installing `kubectl oidc-login`

This is **int128's** `kubelogin` (`kubectl oidc-login`), **not** Azure's `kubelogin`
(which is AKS-only). Install the `kubelogin` binary from int128/kubelogin (e.g. `krew
install oidc-login`, or download the release binary onto `PATH`). For headless
machines add `--grant-type=device-code` to the exec args; the default browser flow
listens on `http://localhost:8000`.

---

## Notes & limits

- **EKS allows one OIDC provider per cluster.** Enabling is a no-op if the Entra
  provider is already associated; a *different* IdP must be removed first.
- **Groups overage:** if a user is in more than ~200 groups, Entra drops the inline
  `groups` claim (replacing it with a Graph link the API server won't follow) and
  RBAC silently misses. For large tenants, use **"Groups assigned to the
  application"** in the app's groups-claim settings so only the relevant groups are
  emitted.
- **Two "kubelogin" tools:** int128 `kubectl oidc-login` for EKS/GKE-Entra; Azure
  `kubelogin` for AKS. They are different binaries.
- **Same group everywhere:** the group Object ID is the RBAC `Group` subject on AKS
  and EKS (bare) and on GKE (wrapped in a `principalSet` URI), so one Entitle grant
  covers all three.

---

## GKE — Workforce Identity Federation + Connect Gateway

GKE can't use the OIDC-identity-provider path (GKE Identity Service is unavailable in
Google Cloud orgs created on/after 2025-07-01). Instead a user reaches the cluster as a
**workforce identity** through **Connect Gateway** — a Google-hosted endpoint that
proxies into the private cluster via the in-cluster Connect agent. The API tunnel is
**not** used for GKE.

### One-time org setup (org admin)

Create the workforce pool + Entra OIDC provider, with the **groups** attribute mapping:

```bash
gcloud iam workforce-pools create bt-entra-pool \
  --organization=<ORG_ID> --location=global --display-name="Entra Workforce Pool"

gcloud iam workforce-pools providers create-oidc bt-entra-oidc \
  --workforce-pool=bt-entra-pool --location=global \
  --issuer-uri="https://login.microsoftonline.com/<tenant>/v2.0" \
  --client-id="<entra-app-client-id>" --client-secret-value="<secret>" \
  --web-sso-response-type=code \
  --web-sso-assertion-claims-behavior=merge-user-info-over-id-token-claims \
  --attribute-mapping="google.subject=assertion.sub,google.groups=assertion.groups"
```

The **`google.groups=assertion.groups`** mapping is load-bearing — it carries the Entra
group Object IDs into the token so the `principalSet` RBAC subject matches. The Entra
app must emit the **groups claim on the ID token** (Token configuration → Security
groups). Add the WIF redirect URI
`https://auth.cloud.google/signin-callback/locations/global/workforcePools/bt-entra-pool/providers/bt-entra-oidc`
to the app.

Then set the pool on **Settings → Kubernetes**: `gcp_workforce_pool_id=bt-entra-pool`,
`gcp_workforce_provider_id=bt-entra-oidc`, location `global`.

> **Dashboard service account** needs `roles/gkehub.admin`,
> `roles/serviceusage.serviceUsageAdmin`, and `roles/resourcemanager.projectIamAdmin`
> (or equivalent) to register the fleet, enable APIs, and grant the gateway IAM.

### Federate a GKE cluster

1. **Entra federation → Enable federation.** The dashboard fleet-registers the cluster,
   enables the Connect Gateway APIs, and grants your Entra group's
   `principalSet://…/workforcePools/<pool>/group/<entra-oid>` the
   `roles/gkehub.gatewayEditor` + `roles/gkehub.viewer` IAM roles.
2. **Entra group → Bind group.** On GKE the RBAC subject is the workforce `principalSet`
   (the dashboard builds it automatically from the group Object ID + your pool).
3. **Entra federation → Download Connect Gateway kubeconfig.**
4. On the user's machine (needs `gcloud` + `gke-gcloud-auth-plugin`), as an Entra member
   of the group:
   ```bash
   gcloud iam workforce-pools create-login-config \
     locations/global/workforcePools/bt-entra-pool/providers/bt-entra-oidc \
     --output-file=login.json
   gcloud auth login --login-config=login.json
   export KUBECONFIG=<downloaded>-entra.kubeconfig   # Connect Gateway kubeconfig
   kubectl get ns
   ```
   (Or `gcloud container fleet memberships get-credentials <membership>` to have gcloud
   write the kubeconfig itself.)

### GKE notes

- **No tunnel / no PRA jump** for GKE — Connect Gateway provides the reachability.
- **IAM vs RBAC split:** `gkehub.gateway*` authorizes *reaching* the cluster through the
  gateway; the `principalSet` ClusterRoleBinding authorizes *what you can do*. Both are
  required (the action + the Entra-group bind cover both).
- **Fleet membership** is left in place on Disable (only the gateway IAM is revoked) —
  re-enabling reuses it.
- **Connect Gateway quota:** ~10 concurrent streams per fleet host project.
