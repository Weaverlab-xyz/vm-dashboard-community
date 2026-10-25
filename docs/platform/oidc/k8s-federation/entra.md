# Entra ID → Kubernetes real-identity federation

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want people signing in to clusters as themselves rather than sharing a kubeconfig.

Part of [Kubernetes federation with your IdP](../k8s-federation.md). Entra is the default
provider, and the only one AKS trusts. The API tunnel, installing `kubectl oidc-login` and
the shared settings are on that page.

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
| **AKS** | native managed-AAD (no action needed) | Azure `kubelogin` (device code) | API tunnel |
| **EKS** | **Entra federation** action → OIDC identity provider | `kubectl oidc-login` (int128) | API tunnel |
| **GKE** | **Entra federation** action → Workforce Identity Federation | `gcloud auth login` | Connect Gateway |

> **EKS and GKE need *different* Entra app registrations** — you cannot reuse one for
> both (see [One-time setup §1](#1-entra-app-registrations--one-per-cloud)). The Entra
> *group* is the same everywhere; only the app registration differs.

---

## The pieces

1. **Entra group → RBAC** — the per-cluster **Entra group** action binds the group's
   Object ID to a ClusterRole (default `cluster-admin`). The same group works on every
   cloud (the group Object ID is tenant-wide).
2. **Entra federation** — the per-cluster action that makes the cluster *trust* Entra.
   AKS is native; EKS associates its **EKS** Entra app as an OIDC identity provider; GKE
   uses a workforce pool backed by a **separate GKE** Entra app. EKS and GKE use
   different app registrations (see §1).
3. **API tunnel** — the existing per-cluster tunnel that reaches a private cluster's
   API server (`kubectl` → `localhost:6443` → the endpoint).
4. **Entitle Entra-ID integration** — configured once in the Entitle console; it
   JIT-grants membership in the group from (1). Nothing to configure in the dashboard.

---

## One-time setup

### 1. Entra app registrations — one per cloud

**EKS and GKE need separate app registrations — you cannot reuse one for both.** Their
OIDC clients are configured in mutually incompatible ways:

- **EKS** end users sign in with `kubectl oidc-login`, which is a **public client** — no
  client secret, "Allow public client flows = Yes", and native (localhost) redirect URIs.
- **GKE** Workforce Identity Federation is a **confidential web client** — it needs a
  **client secret** and the Google web redirect URI, and the workforce pool provider
  does the code exchange server-side.

Making one registration behave as both a secret-less public client and a secret-bearing
web client doesn't work in practice, so create two. (AKS needs no app registration — its
managed-AAD integration is built in.) Both apps emit the **same** group claim, and the
Entra *group* is shared across all clouds; only the registration differs.

#### 1a. EKS app registration (e.g. "EKS Entra OIDC")

- Note the **Application (client) ID** (the OIDC audience) and your **tenant ID**
  (issuer `https://login.microsoftonline.com/<tenant>/v2.0`).
- **Authentication → Add a platform → Mobile and desktop applications**: add redirect
  URIs `http://localhost:8000` and `http://localhost:18000` (int128 `oidc-login`
  authcode flow), and set **Allow public client flows = Yes** (enables device-code for
  headless machines). **No client secret.**
- **Token configuration → Add groups claim → Security groups**, on the **ID token**
  (or set `groupMembershipClaims: "SecurityGroup"` in the manifest). This makes Entra
  emit group **Object IDs** in the `groups` claim — the value the RBAC binding matches.
  If you instead scope it to **"Groups assigned to the application"**, you must assign the
  RBAC group to *this* enterprise app as well — same trap as [§1b](#1b-gke-app-registration-eg-gke-entra-wif),
  same silent failure (the group never reaches the `groups` claim, so the binding matches
  nothing).
- **API permissions**: delegated `openid`, `profile`, `email`.

#### 1b. GKE app registration (e.g. "GKE Entra WIF")

A **second, distinct** registration for Workforce Identity Federation:

- **Authentication → Add a platform → Web**: redirect URI
  `https://auth.cloud.google/signin-callback/locations/global/workforcePools/<pool>/providers/<provider>`.
- **Certificates & secrets → New client secret** — the value is passed to the workforce
  pool provider (`--client-secret-value`, §GKE below).
- **Token configuration → Add groups claim**, on the **ID token**. Pick one, and know what
  it costs you:

  | Setting | Emits | You must also |
  |---|---|---|
  | **Security groups** | every security group the user is in | nothing — but risks the ~200-group [overage](#notes--limits) |
  | **Groups assigned to the application** | *only* groups assigned to this enterprise app | **assign every group you bind** (below) |

- **⚠️ If you chose "Groups assigned to the application": assign the RBAC group to the
  enterprise app.** *Entra admin center → Enterprise applications → (this app) → Users and
  groups → Add user/group.* Assign **both** the group that gates sign-in *and* the group
  you bind to cluster RBAC — they are usually different groups, and only assigned ones
  reach the token.

  Skip it and the failure is invisible from the cloud side: the RBAC group is absent from
  the assertion, so **every** binding on its `principalSet` — Kubernetes RBAC *and* Cloud
  IAM — matches nothing, while `kubectl` still appears to work because the sign-in group
  carries its own access. Nothing in GCP looks misconfigured, because nothing in GCP *is*.
  This cost a full afternoon on 2026-07-30; see
  [Verify the binding is what's granting access](entra/gke.md#verify-the-group-binding-is-really-what-grants-access-gke).

- **After any group or assignment change, the user must sign in again**
  (`gcloud auth login --login-config=…`). Claims are computed at issuance, and gcloud
  refreshes an existing workforce credential against *Google STS*, not Entra — so a
  membership or assignment change never appears in a live session, no matter how long you
  wait.

Its client ID + secret feed the `gcloud iam workforce-pools providers create-oidc`
command in the [GKE section](entra/gke.md) — they
are **not** entered into the dashboard.

### 2. Dashboard settings (Settings → Kubernetes)

- **Entra group → cluster RBAC**: set the group Object ID (+ optional name) and the
  ClusterRole.
- **Entra OIDC federation (EKS)**: set the **EKS app's (client) ID** (from §1a). Leave
  **Issuer URL** blank to derive it from the tenant id. Username/groups claims default to
  `oid`/`groups`.

### 3. Entitle Entra-ID integration

In the Entitle console, connect the Entra-ID integration and publish a resource that
grants membership in the group from step 2. (No dashboard change.)

---

## End-user quick reference

What the *user* installs and runs, per cloud. All three end at `kubectl get ns` against
the same Entra group — only the plumbing differs.

| | **EKS** | **AKS** | **GKE** |
|---|---|---|---|
| Install | int128 `kubelogin` (`kubectl oidc-login`) | Azure `kubelogin` | `gke-gcloud-auth-plugin` |
| Sign in | nothing up front — `kubectl` triggers device code | nothing up front — `kubectl` triggers device code | `gcloud auth login --login-config=login.json` |
| Prep the kubeconfig | none | none | none |
| Reachability | API tunnel connected in the rep console | API tunnel connected in the rep console | Connect Gateway (no tunnel) |
| K8s username | `entra:<object-id>` | `<object-id>` | workforce `principal://…` |

---

## The API tunnel (EKS and AKS only)

EKS and AKS users reach a private API server through the per-cluster **API (TCP)
tunnel**; GKE uses Connect Gateway instead. How the tunnel and its kubeconfig work is
on the hub: [The API tunnel](../k8s-federation.md#the-api-tunnel-eks-and-aks).

---

## Pages

One page per cloud:

| Page | Read it when |
|---|---|
| [Entra federation to EKS](entra/eks.md) | you are federating an EKS cluster with Entra ID, or connecting to one as its user |
| [Entra federation to AKS](entra/aks.md) | you are federating an AKS cluster with Entra ID, or connecting to one as its user |
| [Entra federation to GKE](entra/gke.md) | you are federating a GKE cluster with Entra ID through Workforce Identity Federation, or connecting to one as its user |

---

---

## Notes & limits

- **Separate Entra app registration per cloud.** EKS (public client, no secret) and GKE
  WIF (confidential web client, client secret) can't share one registration — see §1.
  The Entra *group* is shared; the app registration is not.
- **EKS allows one OIDC provider per cluster.** Enabling is a no-op if the Entra
  provider is already associated; a *different* IdP must be removed first.
- **Groups overage:** if a user is in more than ~200 groups, Entra drops the inline
  `groups` claim (replacing it with a Graph link the API server won't follow) and
  RBAC silently misses. For large tenants, use **"Groups assigned to the
  application"** in the app's groups-claim settings so only the relevant groups are
  emitted — but then **every** group you bind must be assigned to that enterprise
  application, or it never appears in the token and its bindings match nothing. That
  failure is invisible from the cloud side and is the single most expensive trap on this
  page; see the ⚠️ in [§1b](#1b-gke-app-registration-eg-gke-entra-wif).
- **Three different client-side auth binaries**, one per cloud: int128
  `kubectl oidc-login` for EKS, Azure `kubelogin` for AKS, and
  `gke-gcloud-auth-plugin` for GKE. The two `kubelogin`s are *different binaries* with
  the same name; GKE uses neither.
- **Same group everywhere:** the group Object ID is the RBAC `Group` subject on AKS
  and EKS (bare) and on GKE (wrapped in a `principalSet` URI), so one Entitle grant
  covers all three.
- **Every cloud's download rewrites the auth block; the *tunnel* download does not.** The
  stored kubeconfig is the machine-identity one the dashboard's runners use (`aws eks
  get-token` / `kubelogin --login spn` / `gke-gcloud-auth-plugin` as the dashboard SA).
  **Entra federation → Download Entra kubeconfig** swaps that for a user-interactive
  sign-in per cloud; **API tunnel → Download kubeconfig** deliberately does not. Handing
  someone the tunnel file is the most common way to end up authenticated as the
  *dashboard* rather than as yourself.
- **Revocation is not immediate on any cloud.** Entitle revoking group membership stops
  the *next* token from carrying the group; an already-issued token stays valid until it
  expires, and all three clients cache tokens on disk
  (`~/.kube/cache/oidc-login`, `~/.kube/cache/kubelogin`, gcloud's own credential
  store). For a clean before/after demo, delete the cache directory after revoking.
- **One tunnel at a time (EKS/AKS).** `k8s_api_tunnel_local_port` is a single global
  setting, so concurrent tunnels to two clusters collide on the port.
- **Impersonation is a different feature.** The `--as` Entitle path (see
  `entitle_k8s_*`) authenticates as the *dashboard's* identity and impersonates a user;
  everything on this page authenticates as the user's own Entra identity. They are
  independent — don't mix the two kubeconfigs.
