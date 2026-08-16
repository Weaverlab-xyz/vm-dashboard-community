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
  [Verify the binding is what's granting access](#verify-the-group-binding-is-really-what-grants-access-gke).

- **After any group or assignment change, the user must sign in again**
  (`gcloud auth login --login-config=…`). Claims are computed at issuance, and gcloud
  refreshes an existing workforce credential against *Google STS*, not Entra — so a
  membership or assignment change never appears in a live session, no matter how long you
  wait.

Its client ID + secret feed the `gcloud iam workforce-pools providers create-oidc`
command in the [GKE section](#gke--workforce-identity-federation--connect-gateway) — they
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
tunnel**; GKE does not use it (Connect Gateway instead). The downloaded kubeconfig has
its `server` repointed to `https://127.0.0.1:<k8s_api_tunnel_local_port>` (default
**6443**) with `tls-server-name` set to the *original* API hostname, so the cluster's
own certificate still validates through localhost. The cluster CA is kept verbatim —
nothing is skipped or made insecure.

Operator: **API tunnel → Create tunnel** on the cluster row, then **connect the
Protocol Tunnel Jump in the BeyondTrust representative console**. `kubectl` fails until
that session is live.

> **The local port is one global setting, not per cluster.** Two clusters' tunnels
> cannot be connected at once — the second collides on 6443. Disconnect one first.

---

## EKS — Entra as an OIDC identity provider

### Federate an EKS cluster (the demo)

1. **Entra federation → Enable federation.** EKS associates the EKS Entra app (§1a) as
   the cluster's OIDC identity provider and the job polls until it's **ACTIVE** (a few
   minutes; the cluster shows `UPDATING` on AWS). This is additive — IAM / `aws-auth`
   access is unchanged, and node bootstrap + console access stay on IAM.
2. **Entra group → Bind group** — binds the group's Object ID to the ClusterRole.
3. **API tunnel → Create tunnel** + connect it in the rep console (see above).
4. **Entra federation → Download Entra kubeconfig.** Token-free — the exec block is
   `kubectl oidc-login get-token` against the EKS Entra app, using the **device-code**
   grant (deliberately, not the browser authcode flow: authcode SSOs the operator into
   the *machine's own* tenant, which is the wrong one for a lab/demo tenant).

### Connecting as the end user (EKS)

Prereqs: `kubectl` and **int128's** `kubelogin` on `PATH` (see below). The user needs a
live Entitle grant for the Entra group.

**1. Confirm the tunnel session is connected** in the rep console.

**2. Point `KUBECONFIG` at the downloaded file and use it.**

```bash
export KUBECONFIG=~/Downloads/<cluster>-entra.kubeconfig
kubectl get ns
```

Windows PowerShell:

```powershell
$env:KUBECONFIG = "$HOME\Downloads\<cluster>-entra.kubeconfig"
kubectl get ns
```

The first call prints a **device-code URL + code**. Complete it in a browser as the
correct Entra account — use an InPrivate/incognito window if the machine is joined to a
different tenant, or the browser's existing SSO will sign in the wrong identity.

**3. Verify who you are:**

```bash
kubectl auth whoami
```

EKS is associated with `usernamePrefix: "entra:"`, so this shows **`entra:<your-object-id>`**
— proof the request went through the OIDC path and not IAM. Groups come through
**unprefixed**, which is why the RBAC subject is the bare group Object ID.

### Installing `kubectl oidc-login`

This is **int128's** `kubelogin` (`kubectl oidc-login`), **not** Azure's `kubelogin`
(which is AKS-only). Install the `kubelogin` binary from int128/kubelogin (e.g. `krew
install oidc-login`, or download the release binary onto `PATH`). The binary must be
named `kubectl-oidc_login` for `kubectl oidc-login` to resolve it — the release archive
ships it correctly; renaming it breaks the exec block. The downloaded kubeconfig already
pins `--grant-type=device-code`, so nothing has to listen on localhost.

### Troubleshooting the user's connection (EKS)

| Symptom | Cause | Fix |
|---|---|---|
| `exec: executable kubectl-oidc_login not found` / `unknown command "oidc-login"` | int128 kubelogin missing or misnamed on `PATH` | install it (above); check `kubectl oidc-login --help` runs |
| `Unable to connect to the server: dial tcp 127.0.0.1:6443` (connection refused / timed out) | the tunnel session isn't connected, or another cluster's tunnel owns the port | connect the Protocol Tunnel Jump; disconnect the other cluster's |
| `x509: certificate is valid for <hash>.<region>.eks.amazonaws.com, not 127.0.0.1` | `tls-server-name` was lost (hand-edited kubeconfig) | re-download the kubeconfig |
| device-code sign-in succeeds, then `Unauthorized` | the OIDC provider isn't `ACTIVE` yet, or the kubeconfig's client id ≠ the audience associated on the cluster | wait for ACTIVE; confirm `entra_oidc_client_id` is the **EKS** app (§1a) and re-run Enable federation |
| signed in, but `Forbidden: User "entra:<oid>" cannot list …` | authenticated, not authorized — no ClusterRoleBinding for the group, or the `groups` claim is absent | run **Entra group → Bind group**; check the §1a groups claim and the overage note |
| revoked the Entitle grant, access continues | the cached ID token is still valid — revocation doesn't invalidate an issued token | delete `~/.kube/cache/oidc-login` (Windows `%USERPROFILE%\.kube\cache\oidc-login`); otherwise access ends at token expiry |

---

## AKS — native managed Entra + Azure RBAC

AKS needs **no trust wiring**: dashboard-provisioned clusters are created with
`azure_active_directory_role_based_access_control { managed = true, azure_rbac_enabled = true }`,
so the API server already validates Entra tokens. **Enable federation** on an AKS row is
a recorded no-op that just lights up the kubeconfig download — there is nothing to
disable later either.

Because `azure_rbac_enabled = true`, the cluster runs the Azure authorization webhook
*alongside* Kubernetes RBAC. Kubernetes authorizers are OR'd, so the **Entra group →
Bind group** ClusterRoleBinding is what grants access here — no Azure role assignment
is needed, and the group Object ID is the subject exactly as on EKS.

### Federate an AKS cluster

1. **Entra federation → Enable federation** (no-op; records state).
2. **Entra group → Bind group.**
3. **API tunnel → Create tunnel** + connect it in the rep console.
4. **Entra federation → Download Entra kubeconfig.**

### Connecting as the end user (AKS)

Prereqs: `kubectl`, the **Azure** `kubelogin` (`az aks install-cli`, or winget/brew
`Azure/kubelogin` — *not* int128's), and for the recommended path the `az` CLI.

**1. Confirm the tunnel session is connected** in the rep console.

**2. Point `KUBECONFIG` at the downloaded file and use it.**

```bash
export KUBECONFIG=~/Downloads/<cluster>-entra.kubeconfig
kubectl get ns
```

Windows PowerShell:

```powershell
$env:KUBECONFIG = "$HOME\Downloads\<cluster>-entra.kubeconfig"
kubectl get ns
```

The first call prints a **device code** to complete in any browser as the correct Entra
account — InPrivate if the machine is joined to a different tenant, same reasoning as EKS.

**No `kubelogin convert-kubeconfig` step.** The download rewrites the stored exec into an
interactive device-code sign-in (well-known AKS AAD client app + your tenant, `--server-id`
preserved), so it authenticates as the *user*. The stored kubeconfig the dashboard's own
runners use is `--login spn` — service-principal mode, which reads
`AAD_SERVICE_PRINCIPAL_CLIENT_ID`/`_SECRET` from whoever's environment runs it — and
handing that to an end user either failed outright or silently authenticated as the
dashboard's SP. A **registered** kubeconfig already on a user mode (`azurecli` /
`devicecode` / `interactive`, or the legacy `azure` auth-provider) is passed through
untouched.

**3. Verify:** `kubectl auth whoami` shows your Entra **object ID** — with no `entra:`
prefix (that prefix is EKS-only, set by us at association time).

### Troubleshooting the user's connection (AKS)

| Symptom | Cause | Fix |
|---|---|---|
| `AAD_SERVICE_PRINCIPAL_CLIENT_ID` / client-secret error from kubelogin | the file is in `--login spn` mode — you are using the **API tunnel** kubeconfig, not the **Entra** one (only the latter is rewritten to device-code) | re-download via **Entra federation → Download Entra kubeconfig** |
| It "works" but RBAC never matches and `whoami` shows an app/SP id | same cause, on a machine that *has* the dashboard SP's env vars set — you authenticated as the SP | unset `AAD_SERVICE_PRINCIPAL_*` and use the Entra download |
| Download fails with a message about `azure_tenant_id` | device-code needs the tenant; the setting is empty | set **Azure tenant ID** in Settings, then re-download |
| `kubelogin: command not found` | Azure kubelogin missing (int128's does **not** substitute) | `az aks install-cli`, or install `Azure/kubelogin` |
| `Unable to connect to the server: dial tcp 127.0.0.1:6443` | tunnel not connected, or the port is held by another cluster | connect the Protocol Tunnel Jump |
| device-code sign-in lands in the wrong tenant | machine SSO picked the joined tenant | complete the code in an InPrivate window as the lab account |
| `Forbidden: User "<oid>" cannot list …` | no ClusterRoleBinding for the group, or the Entitle grant lapsed | **Entra group → Bind group**; re-request the grant |
| revoked the grant, access continues | cached token | delete `~/.kube/cache/kubelogin` (Windows `%USERPROFILE%\.kube\cache\kubelogin`) |

---

## GKE — Workforce Identity Federation + Connect Gateway

GKE can't use the OIDC-identity-provider path (GKE Identity Service is unavailable in
Google Cloud orgs created on/after 2025-07-01). Instead a user reaches the cluster as a
**workforce identity** through **Connect Gateway** — a Google-hosted endpoint that
proxies into the private cluster via the in-cluster Connect agent. The API tunnel is
**not** used for GKE.

### One-time org setup (org admin)

Uses the **GKE app registration (§1b)** — a *different* app from the EKS one. Create the
workforce pool + Entra OIDC provider, with the **groups** attribute mapping, using that
GKE app's client ID + secret:

```bash
gcloud iam workforce-pools create bt-entra-pool \
  --organization=<ORG_ID> --location=global --display-name="Entra Workforce Pool"

gcloud iam workforce-pools providers create-oidc bt-entra-oidc \
  --workforce-pool=bt-entra-pool --location=global \
  --issuer-uri="https://login.microsoftonline.com/<tenant>/v2.0" \
  --client-id="<gke-app-client-id>" --client-secret-value="<gke-app-secret>" \
  --web-sso-response-type=code \
  --web-sso-assertion-claims-behavior=merge-user-info-over-id-token-claims \
  --attribute-mapping="google.subject=assertion.sub,google.groups=assertion.groups"
```

The **`google.groups=assertion.groups`** mapping is load-bearing — it carries the Entra
group Object IDs into the token so the `principalSet` RBAC subject matches. Without it the
`principalSet` never matches anything.

**Then finish the Entra side (§1b) — the GCP half above is useless on its own:**

1. Add the WIF **redirect URI** to the GKE app:
   `https://auth.cloud.google/signin-callback/locations/global/workforcePools/bt-entra-pool/providers/bt-entra-oidc`
2. **Token configuration → groups claim on the ID token** — required, or `assertion.groups`
   is empty and the mapping above carries nothing.
3. **Assign your RBAC group to the enterprise app** if that claim is scoped to
   "Groups assigned to the application". Being a *member* is not enough. This is the step
   people miss; it produces a setup that looks perfect and grants nothing (§1b ⚠️).
4. Every user signs in fresh afterwards — claims are issue-time.

Then set the pool on **Settings → Kubernetes**: `gcp_workforce_pool_id=bt-entra-pool`,
`gcp_workforce_provider_id=bt-entra-oidc`, location `global`.

> **Dashboard service account** needs `roles/gkehub.admin`,
> `roles/serviceusage.serviceUsageAdmin`, and `roles/resourcemanager.projectIamAdmin`
> (or equivalent) to register the fleet, enable APIs, and grant the gateway IAM.
> `scripts/sandbox/Linux/setup-gcp.sh` grants these (and enables the
> `gkehub`/`connectgateway`/`gkeconnect` **and `cloudresourcemanager`** APIs) —
> **re-run it** if you set the sandbox up before this was added. Two symptoms of the
> gap, both surfaced as a bare 403: a missing role → `403 Forbidden … services:batchEnable`;
> `cloudresourcemanager.googleapis.com` not enabled → `403 Forbidden … :getIamPolicy`
> (really a `SERVICE_DISABLED`). The dashboard also enables `cloudresourcemanager`
> itself at Enable-federation time, so a fresh project self-heals.

### Federate a GKE cluster

1. **Entra federation → Enable federation.** The dashboard fleet-registers the cluster,
   enables the Connect Gateway APIs, and grants your Entra group's
   `principalSet://…/workforcePools/<pool>/group/<entra-oid>` the
   `roles/gkehub.gatewayEditor` + `roles/gkehub.viewer` IAM roles.
2. **Entra group → Bind group.** On GKE the RBAC subject is the workforce `principalSet`
   (the dashboard builds it automatically from the group Object ID + your pool).
3. **Entra federation → Download Connect Gateway kubeconfig.** The file downloads as
   `<cluster>-entra.kubeconfig` and is **token-free**: its `server` is the Connect
   Gateway URL and its only credential is an `exec` block calling
   `gke-gcloud-auth-plugin`, which mints a token from whatever gcloud identity is
   active. Nothing in it is secret or user-specific — the *signed-in identity* is.
4. Hand the user the kubeconfig and the steps below.

### Connecting as the end user (GKE)

Prereqs on the user's machine: `gcloud`, `kubectl`, and **`gke-gcloud-auth-plugin`** —
a gcloud component that is **not** installed by default:

```bash
gcloud components install gke-gcloud-auth-plugin
```

(Verify with `gke-gcloud-auth-plugin --version`. If gcloud came from a package manager
or snap rather than the installer, `components install` is disabled — install the
plugin the same way you installed gcloud.)

**1. Sign in as the Entra-federated workforce identity** (once per session):

```bash
gcloud iam workforce-pools create-login-config \
  locations/global/workforcePools/bt-entra-pool/providers/bt-entra-oidc \
  --output-file=login.json
gcloud auth login --login-config=login.json
```

A browser opens to Entra. On success gcloud prints
`Authenticated with external account authorized user credentials for:
[principal://iam.googleapis.com/…/workforcePools/bt-entra-pool/subject/<subject>]`
followed by **`Your current project is [None].`** — that second line is **expected and
harmless** (see the note below). Confirm with `gcloud auth list` that the
`principal://…` account is the ACTIVE one.

**2. Point `KUBECONFIG` at the downloaded file and use it.**

```bash
export KUBECONFIG=~/Downloads/<cluster>-entra.kubeconfig
kubectl get ns
```

Windows PowerShell:

```powershell
$env:KUBECONFIG = "$HOME\Downloads\<cluster>-entra.kubeconfig"
kubectl get ns
```

There is **no tunnel to connect and no `kubelogin`** on this path — Connect Gateway is
the reachability, and gcloud is the credential.

**3. Confirm the identity and the grant:**

```bash
kubectl auth whoami        # your principal:// subject AND the groups in the token
kubectl auth can-i --list  # what the group's ClusterRole actually allows
```

> **A grant that lands *after* sign-in needs a fresh `gcloud auth login`.** The Entra
> group Object IDs arrive as the `google.groups` attribute of the workforce token,
> mapped from the Entra assertion **at token-exchange time**. gcloud then holds an
> `external_account_authorized_user` credential and refreshes it against *Google STS* —
> not Entra — so a newly-granted Entra group membership never appears in the current
> session, no matter how long you wait or how many times the access token refreshes.
> Re-run `gcloud auth login --login-config=login.json` and the new group is in the
> token. (The same asymmetry is why revocation lags: see [Notes & limits](#notes--limits).)
>
> Corollary worth knowing for a demo: **if `kubectl get ns` works at all, the group
> grant is already live** — on GKE both the Connect Gateway IAM *and* the cluster
> RBAC binding hang off that group's `principalSet`, so a non-member can't get even a
> namespace list.

> **`Your current project is [None]` does not need fixing.** The gateway `server` URL in
> the downloaded kubeconfig already carries the project **number**, so `kubectl` never
> consults gcloud's active project. A workforce identity also has no project-level
> browse rights, so `gcloud config set project` is cosmetic. It matters only on the
> alternative route where gcloud writes the kubeconfig itself, which must be told the
> project explicitly:
> `gcloud container fleet memberships get-credentials <membership> --project <PROJECT_ID> --location global`
> (that route needs `gkehub.memberships.get`, which `roles/gkehub.viewer` from the
> Enable-federation grant provides).

### Verify the group binding is really what grants access (GKE)

**A successful `kubectl get ns` does not prove your Entra group binding works.** On GKE,
Cloud IAM is an authorizer alongside Kubernetes RBAC: a basic `roles/viewer` (or
`editor`/`owner`) bound to *any* other principal the user matches — a different Entra
group, or an `attribute.email/<upn>` principalSet — grants project-wide read that GKE
turns into Kubernetes read access. That serves `get ns` whether or not your group ever
reached the token, so the whole setup can be inert and look healthy.

List everything the pool's principals hold before believing a result:

```bash
gcloud projects get-iam-policy <PROJECT_ID> --flatten="bindings[].members" \
  --filter="bindings.members:principalSet" --format="table(bindings.role, bindings.members)"
```

Anything basic (`roles/viewer`, `roles/editor`, `roles/owner`) on another principalSet is a
mask. To prove the binding, remove the mask and re-run `kubectl get ns` as the user:

| Result with the mask removed | Meaning |
|---|---|
| namespaces still listed | your group **is** in the token and `entra-group-binding` is doing the work — the design is verified |
| gateway-level 403 | the group is **not** in the token → the `gkehub.gateway*` grant on it is inert; fix the Entra assignment (§1b) |
| authenticated but `Forbidden` on the list | group is in the token, but the ClusterRoleBinding is missing or names the wrong subject → re-run **Bind group** |

Re-add the binding afterwards only if you actually want that standing access — a standing
basic role also **masks JIT revocation**, letting access survive grant expiry, which makes
an Entitle demo show the opposite of what it claims.

### Troubleshooting the user's connection (GKE)

| Symptom | Cause | Fix |
|---|---|---|
| `no Auth Provider found for name "gcp"` / `exec: gke-gcloud-auth-plugin: not found` | plugin not installed or not on `PATH` | `gcloud components install gke-gcloud-auth-plugin` |
| `error: You must be logged in to the server (Unauthorized)` | active gcloud account isn't the workforce principal (a later plain `gcloud auth login` silently replaces it), or the token carries no `groups` claim | re-run `gcloud auth login --login-config=login.json`; check `gcloud auth list`; confirm the GKE app emits the **groups claim on the ID token** (§1b) |
| `Forbidden … permission 'gkehub.gateways.get'` (or `…gateways.connect`) | the group's `principalSet` is missing the gateway IAM roles | re-run **Entra federation → Enable federation** |
| Reaches the API but `Error from server (Forbidden): … cannot list resource … in the cluster scope` | gateway IAM is fine, RBAC isn't — no ClusterRoleBinding for the `principalSet` | run **Entra group → Bind group** |
| Everything worked yesterday, now `Unauthorized`/`Forbidden` | the Entitle grant for the Entra group expired — this is the feature working | request the grant again |
| `404 … gkeMemberships/<name> not found` | fleet membership gone (project rebuilt, cluster re-created) | re-run **Enable federation**, re-download the kubeconfig |
| Reads work but every binding on your RBAC group is ignored (`Forbidden`, or a `--as` that says the caller `cannot impersonate`) | the group is **not assigned to the WIF enterprise app**, so it is absent from the `groups` claim while the sign-in group still grants what you *can* do | assign it (Enterprise applications → the WIF app → Users and groups), then re-run `gcloud auth login --login-config` — see the ⚠️ in [§1b](#1b-gke-app-registration-eg-gke-entra-wif) |
| `cannot impersonate resource "users" … requires one of ["container.clusters.impersonate"]` with the RBAC impersonator binding in place | GKE does **not** honor the Kubernetes `impersonate` verb — it requires the Cloud IAM permission (verified live 2026-07-30) | bind the group's `principalSet` to a role carrying `container.clusters.impersonate`; the dashboard's **Impersonation access** action does this on GKE |

### GKE notes

- **No tunnel / no PRA jump** for GKE — Connect Gateway provides the reachability.
- **IAM vs RBAC split:** `gkehub.gateway*` authorizes *reaching* the cluster through the
  gateway; the `principalSet` ClusterRoleBinding authorizes *what you can do*. Both are
  required (the action + the Entra-group bind cover both).
- **Fleet membership** is left in place on Disable (only the gateway IAM is revoked) —
  re-enabling reuses it.
- **Connect Gateway quota:** ~10 concurrent streams per fleet host project.
- **Kubernetes RBAC does apply to workforce identities** — verified live 2026-07-30 by the
  mask-removal method above; `entra-group-binding` → `view` was the sole source of a user's
  read access. Don't assume Cloud IAM is the only authorizer on GKE.
- **…except for `impersonate`, which GKE gates in Cloud IAM only.** The Kubernetes
  `impersonate` verb alone is *not* honored: `kubectl --as` fails
  `cannot impersonate resource "users" … requires one of
  ["container.clusters.impersonate"]` even with a correct ClusterRole/Binding. The
  fine-grained Entitle tier therefore needs the group's `principalSet` bound to a role
  carrying `container.clusters.impersonate` (one permission is enough — the dashboard's
  **Impersonation access** action creates a least-privilege custom role and binds it). Note
  that permission is in Google's `TESTING` stage. Details and the failure modes are in
  [Kubernetes → Entitle k8s JIT](../kubernetes.md).

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
