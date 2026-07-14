# Entra ID → Kubernetes real-identity federation

Bind **one Entra (Azure AD) security group** to cluster RBAC and let its members sign
in to your clusters **as themselves** — their Entra token carries the group's Object
ID, which matches a Kubernetes `Group` subject. Entitle's Entra-ID integration
JIT-grants group membership, so access appears (and expires) just-in-time with **no
impersonation and no synthetic subject**.

This works natively on **AKS**. On **EKS** it needs the cluster to *trust Entra as an
OIDC identity provider* — the one-click **Entra federation** action does that. (GKE
uses Workforce Identity Federation + Connect Gateway, which is a separate path — see
the end of this doc.)

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
  and EKS alike, so one Entitle grant covers both.

---

## GKE (Workforce Identity Federation) — follow-up

GKE cannot use the OIDC-identity-provider path (GKE Identity Service is unavailable in
Google Cloud organizations created on/after 2025-07-01). GKE therefore uses **Workforce
Identity Federation + Connect Gateway**: users reach the cluster through Google's
Connect Gateway (not this API tunnel), and the RBAC subject is a workforce-pool URI
(`principalSet://iam.googleapis.com/locations/global/workforcePools/<pool>/group/<entra-oid>`)
that still wraps the same Entra group Object ID. That leg is delivered separately.
