# Entra federation to GKE

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are federating a GKE cluster with Entra ID through Workforce Identity Federation, or connecting to one as its user.

Part of [Entra ID → Kubernetes real-identity federation](../entra.md).

GKE can't use the OIDC-identity-provider path (GKE Identity Service is unavailable in
Google Cloud orgs created on/after 2025-07-01). Instead a user reaches the cluster as a
**workforce identity** through **Connect Gateway** — a Google-hosted endpoint that
proxies into the private cluster via the in-cluster Connect agent. The API tunnel is
**not** used for GKE.

## One-time org setup (org admin)

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

## Federate a GKE cluster

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

## Connecting as the end user (GKE)

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
> token. (The same asymmetry is why revocation lags: see [Notes & limits](../entra.md#notes--limits).)
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

## Verify the group binding is really what grants access (GKE)

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

## Troubleshooting the user's connection (GKE)

| Symptom | Cause | Fix |
|---|---|---|
| `no Auth Provider found for name "gcp"` / `exec: gke-gcloud-auth-plugin: not found` | plugin not installed or not on `PATH` | `gcloud components install gke-gcloud-auth-plugin` |
| `error: You must be logged in to the server (Unauthorized)` | active gcloud account isn't the workforce principal (a later plain `gcloud auth login` silently replaces it), or the token carries no `groups` claim | re-run `gcloud auth login --login-config=login.json`; check `gcloud auth list`; confirm the GKE app emits the **groups claim on the ID token** (§1b) |
| `Forbidden … permission 'gkehub.gateways.get'` (or `…gateways.connect`) | the group's `principalSet` is missing the gateway IAM roles | re-run **Entra federation → Enable federation** |
| Reaches the API but `Error from server (Forbidden): … cannot list resource … in the cluster scope` | gateway IAM is fine, RBAC isn't — no ClusterRoleBinding for the `principalSet` | run **Entra group → Bind group** |
| Everything worked yesterday, now `Unauthorized`/`Forbidden` | the Entitle grant for the Entra group expired — this is the feature working | request the grant again |
| `404 … gkeMemberships/<name> not found` | fleet membership gone (project rebuilt, cluster re-created) | re-run **Enable federation**, re-download the kubeconfig |
| Reads work but every binding on your RBAC group is ignored (`Forbidden`, or a `--as` that says the caller `cannot impersonate`) | the group is **not assigned to the WIF enterprise app**, so it is absent from the `groups` claim while the sign-in group still grants what you *can* do | assign it (Enterprise applications → the WIF app → Users and groups), then re-run `gcloud auth login --login-config` — see the ⚠️ in [§1b](../entra.md#1b-gke-app-registration-eg-gke-entra-wif) |
| `cannot impersonate resource "users" … requires one of ["container.clusters.impersonate"]` with the RBAC impersonator binding in place | GKE does **not** honor the Kubernetes `impersonate` verb — it requires the Cloud IAM permission (verified live 2026-07-30) | bind the group's `principalSet` to a role carrying `container.clusters.impersonate`; the dashboard's **Impersonation access** action does this on GKE |

## GKE notes

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
  [Kubernetes → Entitle k8s JIT](../../../../cloud-services/kubernetes.md).
