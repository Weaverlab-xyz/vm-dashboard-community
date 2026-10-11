# Kubernetes federation with your IdP

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want people signing in to managed clusters as themselves through Okta, PingOne or another OIDC provider, not only Entra ID.

Part of [OIDC and single sign-on](../oidc.md).

The **IdP federation** action on a cluster's row makes the cluster trust an identity
provider, so a person's own token authenticates them and their groups match the
**IdP group** binding. It used to trust only Microsoft Entra ID. You now choose the provider
under **Settings → Kubernetes → Cluster federation IdP**:

| Provider | Setting value | Configured with |
|---|---|---|
| Microsoft Entra ID (default) | `entra` | the `entra_oidc_*` keys, exactly as before. See [Entra → Kubernetes federation](entra-k8s-federation.md) |
| Okta | `okta` | the `k8s_oidc_*` keys below |
| PingOne | `pingone` | the `k8s_oidc_*` keys below |
| Any other OIDC provider | `oidc` | the `k8s_oidc_*` keys below |

These are the same three providers you can register under
[Directories → Identity providers](../directories/identity-providers.md) to browse groups
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

| Field | Key | Default | Notes |
|---|---|---|---|
| Provider | `k8s_oidc_provider` | `entra` | `entra`, `okta`, `pingone` or `oidc` |
| Issuer URL | `k8s_oidc_issuer_url` | — | Exactly as the IdP's discovery document advertises it, `https://` only. EKS compares this string with each token's `iss`, so keep any trailing slash the IdP uses |
| Client ID | `k8s_oidc_client_id` | — | A **public** client with no secret. `kubectl oidc-login` signs in with it, and it is the audience EKS requires |
| Username claim | `k8s_oidc_username_claim` | `sub` | A stable per-user id. On EKS the Kubernetes username becomes `<provider>:<value>`, e.g. `okta:00u1ab…` |
| Groups claim | `k8s_oidc_groups_claim` | `groups` | Must be on the **ID token**, which is what kubelogin presents |
| Sign-in | `k8s_oidc_grant_type` | `device-code` | `device-code`, or `authcode` for kubelogin's browser flow on `http://localhost:8000` / `:18000` |
| Group | `k8s_oidc_rbac_group_id` | — | The default group for **IdP group** and **Impersonation access**, as the groups claim carries it. For Okta and PingOne that is usually the group's name |

The ClusterRole (`entra_rbac_group_role`), the GKE workforce pool keys and the Entitle
prefix are shared with Entra and keep their names.

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

## Okta

1. **Applications → Create App Integration → OIDC → Native Application.**
   - Grant types: **Authorization Code** and **Device Authorization**. Device
     Authorization is what the default `device-code` sign-in uses.
   - Sign-in redirect URIs: `http://localhost:8000` and `http://localhost:18000`. These are
     only used if you set the sign-in to `authcode`.
   - Client authentication: **none**, so the app is a public client. kubelogin always sends PKCE.
   - Assign it to the people, or the groups, who should reach clusters.
2. **Put the groups on the ID token.**
   - On the org authorization server (issuer `https://<org>.okta.com`), set the app's
     **Sign On → OpenID Connect ID Token → Groups claim filter**, e.g. `groups` *Matches
     regex* `k8s-.*`.
   - On a custom authorization server (issuer `https://<org>.okta.com/oauth2/<id>`, such as
     `default`), add a `groups` claim of value type **Groups**, included in the **ID
     token**, for **any scope**. Also allow the Device Authorization grant in that server's
     access-policy rule.
   - Keep the filter narrow. Every group it matches lands in the token.
3. In the dashboard, set the provider to **Okta**, the issuer and client ID from above, and
   the group, e.g. `k8s-admins`.

## PingOne

1. **Applications → Add application → Native.**
   - Grant types: **Authorization Code** and **Device Authorization**. Token endpoint
     authentication: **None**, so the app is a public client.
   - Redirect URIs: `http://localhost:8000` and `http://localhost:18000`, for `authcode`.
   - Turn the application on, and grant access to the people or groups who should reach
     clusters.
2. **Attribute mappings:** add a `groups` attribute mapped to the user's group names
   (or group IDs, if you would rather bind by ID), and make sure it is included in the ID
   token.
3. In the dashboard, set the provider to **PingOne** and the issuer to
   `https://auth.pingone.<region>/<environment-id>/as`. The region is `com`, `eu`, `ca`,
   `asia`, `com.au` or `sg`, the same as for a registered PingOne directory. Then set the
   client ID and the group.

## Another OIDC provider

Anything that publishes `<issuer>/.well-known/openid-configuration`, issues an ID
token carrying a groups claim, and lets you register a public client works. That covers
Keycloak, Authentik, JumpCloud and Auth0 with a groups action. If the provider can't do
device authorization, set the sign-in to `authcode` and register the two localhost
redirect URIs.

Managed control planes fetch the discovery document and signing keys **from the
internet**. The issuer needs a public DNS name and a certificate from a public CA.

## Federate a cluster

The cluster-row steps are the same as with Entra:

1. **IdP federation → Enable federation.**
   - **EKS:** associates the app as the cluster's OIDC identity provider, named after the
     provider (`okta`, `pingone`, `oidc`). The job then polls until it is **ACTIVE**,
     which takes a few minutes.
   - **GKE:** registers the cluster to the fleet and grants the group's `principalSet` the
     Connect Gateway roles. First create the workforce pool's provider against your
     IdP's issuer, with a client of that IdP. Workforce Identity does the code exchange
     server-side, so that client is a **confidential** web app with a secret, separate from
     the public one above. Keep the `google.groups=assertion.groups` mapping:
     ```bash
     gcloud iam workforce-pools providers create-oidc okta-oidc \
       --workforce-pool=<pool> --location=global \
       --issuer-uri="https://<org>.okta.com/oauth2/default" \
       --client-id="<web-app-client-id>" --client-secret-value="<secret>" \
       --web-sso-response-type=code \
       --web-sso-assertion-claims-behavior=merge-user-info-over-id-token-claims \
       --attribute-mapping="google.subject=assertion.sub,google.groups=assertion.groups"
     ```
     Register `https://auth.cloud.google/signin-callback/locations/global/workforcePools/<pool>/providers/<provider>`
     as that web app's redirect URI.
2. **IdP group → Bind group.** This binds the group to the ClusterRole: `okta:k8s-admins`
   on EKS, the `principalSet` on GKE.
3. **API tunnel → Create tunnel** (EKS) and connect it in the rep console.
4. **Download the kubeconfig.** It downloads as `<cluster>-<provider>.kubeconfig`. On EKS
   it calls `kubectl oidc-login get-token` against your IdP's app. On GKE it is the same
   Connect Gateway kubeconfig as with Entra.

The end user installs int128's `kubelogin` (`kubectl krew install oidc-login`), connects
the tunnel and runs `kubectl get ns`. They complete the device code in a browser as
themselves. `kubectl auth whoami` then shows `okta:<sub>` and the `okta:`-prefixed groups.
The end-user section of the [Entra guide](entra-k8s-federation.md#installing-kubectl-oidc-login)
covers installing kubelogin and its troubleshooting table. Read "Entra" there as your
IdP.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Enable fails: *federated with Entra ID, and Settings now name Okta* | the cluster still trusts the previous IdP | **Disable** federation on it, then enable again |
| Enable fails: *no Okta app configured* | issuer or client ID missing | set `k8s_oidc_issuer_url` and `k8s_oidc_client_id` |
| Device-code sign-in fails at the IdP with `unauthorized_client` | the app doesn't have the Device Authorization grant (on Okta, check the authorization server's policy rule too) | enable it, or set the sign-in to `authcode` |
| Signed in, then `Unauthorized` | the provider config isn't `ACTIVE` yet, or the issuer string differs from the token's `iss` (a trailing slash counts) | wait for ACTIVE; copy the issuer from the IdP's discovery document and re-enable |
| `Forbidden: User "okta:…" cannot list …` | no binding for the group, the groups claim isn't on the **ID** token, or the binding names the group without its prefix | **IdP group → Bind group**; check the claim with `kubectl oidc-login get-token` and decode the token |
| An AKS cluster still signs people in with Entra | by design: AKS trusts Entra only | use Entra for AKS, or keep AKS out of the Okta / PingOne rollout |
