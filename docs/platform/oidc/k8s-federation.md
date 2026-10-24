# Kubernetes federation with your IdP

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want people signing in to managed clusters as themselves through Entra ID, Okta, PingOne or another OIDC provider, and need to pick which one clusters trust.

Part of [OIDC and single sign-on](../oidc.md).

The **IdP federation** action on a cluster's row makes the cluster trust an identity
provider, so a person's own token authenticates them and their groups match the
**IdP group** binding. No impersonation, no shared kubeconfig. You choose the provider
under **Settings → Kubernetes → Cluster federation IdP**, and each provider has its own
setup page:

| Provider | Setting value | Setup page |
|---|---|---|
| Microsoft Entra ID (default) | `entra` | [Entra ID](k8s-federation/entra.md) |
| Okta | `okta` | [Okta](k8s-federation/okta.md) |
| PingOne | `pingone` | [PingOne](k8s-federation/pingone.md) |
| Any other OIDC provider | `oidc` | [Other OIDC providers](k8s-federation/oidc.md) |

This page covers what they share: which clusters take which provider, the settings, how
groups become RBAC subjects, the cluster steps, and what the end user runs.

These are the same three providers you can register under
[Directories → Identity providers](../../preview/directories/identity-providers.md) to browse groups
and change membership. The two features are separate: registering a directory doesn't
federate a cluster, and federating a cluster doesn't register a directory. Together they
let you add someone to the bound group from the dashboard and have them reach the
cluster as themselves on their next sign-in.

## Which clusters can use which provider

| Cluster | Entra ID | Okta, PingOne, other OIDC | How it trusts the IdP |
|---|---|---|---|
| **EKS** | yes | yes | an OIDC identity provider config on the cluster, one per cluster |
| **GKE** | yes | yes | Workforce Identity Federation + Connect Gateway. The workforce pool's OIDC provider is your IdP |
| **AKS** | yes, always | **no** | its managed AAD integration trusts Entra ID only. Whatever the setting says, an AKS cluster keeps using Entra |
| **On-prem (k3s)** | through Dex | through Dex | [Dex](dex.md) federates the upstream IdP. Point its `oidc` connector at Okta or PingOne |
| OKE | no | no | not wired for federation |

The provider is recorded on each cluster when you enable federation. **Changing the
setting doesn't move clusters that are already federated.** Each keeps its provider until
you disable federation on it, and **Disable** removes the provider the cluster was federated
*with*. To move a cluster to another IdP, disable federation, change the setting, then
enable it again. Enable refuses to switch provider in place, because EKS takes only one
OIDC provider per cluster.

## Settings

Entra keeps its own keys (`entra_oidc_*`, `entra_rbac_group_id`), described on the
[Entra ID](k8s-federation/entra.md#2-dashboard-settings-settings--kubernetes) page. Every
other provider uses these:

| Field | Key | Default | Notes |
|---|---|---|---|
| Provider | `k8s_oidc_provider` | `entra` | `entra`, `okta`, `pingone` or `oidc` |
| Issuer URL | `k8s_oidc_issuer_url` | — | Exactly as the IdP's discovery document advertises it, `https://` only. EKS compares this string with each token's `iss`, so keep any trailing slash the IdP uses |
| Client ID | `k8s_oidc_client_id` | — | A **public** client with no secret. `kubectl oidc-login` signs in with it, and it is the audience EKS requires |
| Username claim | `k8s_oidc_username_claim` | `sub` | A stable per-user id. On EKS the Kubernetes username becomes `<provider>:<value>`, e.g. `okta:00u1ab…` |
| Groups claim | `k8s_oidc_groups_claim` | `groups` | Must be on the **ID token**, which is what kubelogin presents |
| Sign-in | `k8s_oidc_grant_type` | `device-code` | `device-code`, or `authcode` for kubelogin's browser flow on `http://localhost:8000` / `:18000` |
| Group | `k8s_oidc_rbac_group_id` | — | The default group for **IdP group** and **Impersonation access**, as the groups claim carries it. For Okta and PingOne that is usually the group's name |

The ClusterRole (`entra_rbac_group_role`), the GKE workforce pool keys
(`gcp_workforce_pool_id`, `gcp_workforce_provider_id`, `gcp_workforce_location`) and the
Entitle prefix are shared by every provider and keep their names.

### Groups are prefixed on EKS

Entra emits group **Object IDs**, which can't collide with anything, so Entra
associations have no groups prefix and the binding names the bare Object ID. That
hasn't changed.

Okta, PingOne and most other IdPs emit group **names**. Without a prefix, an IdP group
called `system:masters` would *be* the cluster's built-in admin group. So for every
provider except Entra, the EKS association sets `groupsPrefix: <provider>:` and the
**IdP group** binding names `<provider>:<group>`, for example `okta:k8s-admins`. The lab
play `k3s-dex-auth.yml` does the same for Dex with `dex:`. On GKE the subject is the workforce
`principalSet`, which needs no prefix.

### Managed control planes reach the issuer over the internet

EKS fetches the discovery document and signing keys from the internet, and so does
Google for a workforce pool. The issuer needs a public DNS name and a certificate from a
public CA. Every SaaS IdP already has both. A self-hosted one (Keycloak, Authentik) has
to be published.

## Federate a cluster

The cluster-row steps are the same for every provider. The provider page covers what to
set up at the IdP first.

1. **IdP federation → Enable federation.**
   - **EKS:** associates the provider's app as the cluster's OIDC identity provider,
     named after the provider (`entra`, `okta`, `pingone`, `oidc`). The job then polls
     until it is **ACTIVE**, which takes a few minutes. Additive to IAM: node bootstrap and
     console access are unchanged.
   - **GKE:** registers the cluster to the fleet, turns on Connect Gateway and grants the
     group's `principalSet` the `gkehub.gateway` roles. The workforce pool and its OIDC
     provider for your IdP are created once at the org level beforehand. Each provider
     page has the `gcloud` command.
   - **AKS:** nothing to do. It already trusts Entra.
2. **IdP group → Bind group.** This binds the group to the ClusterRole. The subject is the
   bare Object ID for Entra, `<provider>:<group>` on EKS for anything else, and the
   `principalSet` on GKE.
3. **API tunnel → Create tunnel** (EKS and AKS) and connect it in the rep console. See
   [The API tunnel](#the-api-tunnel-eks-and-aks).
4. **Download the kubeconfig.** It downloads as `<cluster>-<provider>.kubeconfig` and holds
   no credential.
   - EKS: it calls `kubectl oidc-login get-token` against the provider's app.
   - AKS: it calls Azure `kubelogin` in device-code mode.
   - GKE: it is a Connect Gateway kubeconfig that uses `gke-gcloud-auth-plugin`.

## The API tunnel (EKS and AKS)

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

## What the end user runs (EKS)

1. Install `kubectl oidc-login` (below).
2. Make sure the tunnel session is connected.
3. Point `KUBECONFIG` at the downloaded file and run `kubectl get ns`:

   ```powershell
   $env:KUBECONFIG = "$HOME\Downloads\<cluster>-okta.kubeconfig"
   kubectl get ns
   ```

   On Linux or macOS: `export KUBECONFIG=~/Downloads/<cluster>-okta.kubeconfig`.
4. With device-code sign-in, the first call prints a URL and a code. Complete it in a
   browser as yourself. Use an InPrivate window if the machine is signed in to a different
   account. With `authcode`, a browser opens on `localhost` instead.
5. `kubectl auth whoami` shows `<provider>:<your id>` (`entra:<object-id>` for Entra), with
   your groups.

AKS and GKE users follow the end-user steps on the [Entra ID](k8s-federation/entra.md)
page, since AKS is Entra-only and GKE's sign-in is `gcloud` against the workforce pool
whatever the IdP.

### Installing `kubectl oidc-login`

This is **int128's** `kubelogin` (`kubectl oidc-login`), **not** Azure's `kubelogin`
(which is AKS-only). Install the `kubelogin` binary from int128/kubelogin (e.g. `krew
install oidc-login`, or download the release binary onto `PATH`). The binary must be
named `kubectl-oidc_login` for `kubectl oidc-login` to resolve it — the release archive
ships it correctly; renaming it breaks the exec block. With the default device-code
sign-in, nothing has to listen on localhost.

## Troubleshooting

Problems common to every provider are listed here. Each provider page adds its own.

| Symptom | Cause | Fix |
|---|---|---|
| Enable fails: *federated with Entra ID, and Settings now name Okta* | the cluster still trusts the previous IdP | **Disable** federation on it, then enable again |
| Enable fails: *no Okta app configured* | issuer or client ID missing | set `k8s_oidc_issuer_url` and `k8s_oidc_client_id` |
| `exec: executable kubectl-oidc_login not found` | int128 kubelogin missing or misnamed on `PATH` | install it (above); check `kubectl oidc-login --help` runs |
| `Unable to connect to the server: dial tcp 127.0.0.1:6443` | the tunnel session isn't connected, or another cluster's tunnel owns the port | connect the Protocol Tunnel Jump; disconnect the other cluster's |
| Signed in, then `Unauthorized` | the provider config isn't `ACTIVE` yet, or the issuer string differs from the token's `iss` (a trailing slash counts) | wait for ACTIVE; copy the issuer from the IdP's discovery document and re-enable |
| `Forbidden: User "okta:…" cannot list …` | no binding for the group, the groups claim isn't on the **ID** token, or the binding names the group without its prefix | **IdP group → Bind group**; check the claim with `kubectl oidc-login get-token` and decode the token |
| Revoked someone's group membership, access continues | the cached ID token is still valid until it expires | delete `~/.kube/cache/oidc-login` (Windows `%USERPROFILE%\.kube\cache\oidc-login`) |
| An AKS cluster still signs people in with Entra | by design: AKS trusts Entra only | use Entra for AKS, or keep AKS out of the Okta / PingOne rollout |
