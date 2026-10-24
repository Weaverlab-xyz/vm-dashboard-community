# Other OIDC providers

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want EKS or GKE clusters to trust an identity provider other than Entra ID, Okta or PingOne, such as Keycloak, Authentik, JumpCloud or Auth0.

Part of [Kubernetes federation with your IdP](../k8s-federation.md). That page has the
settings, the cluster steps and what the end user runs.

Set the provider to **Other OIDC provider** (`oidc`). On EKS, users and groups are then
prefixed `oidc:`.

## What the provider has to do

| Requirement | Why |
|---|---|
| Publish `<issuer>/.well-known/openid-configuration` and its signing keys on a public `https://` address | EKS and Google fetch them from the internet |
| Let you register a **public** client (no secret) | `kubectl oidc-login` signs in with it; its client ID is the token audience |
| Put a groups claim on the **ID token** | kubelogin presents the ID token, and the RBAC binding matches its groups |
| Support the device-authorization grant, **or** localhost redirect URIs | device-code is the default sign-in; `authcode` uses `http://localhost:8000` and `:18000` |

## Notes per provider

| Provider | Issuer | Groups claim |
|---|---|---|
| Keycloak | `https://<host>/realms/<realm>` | add a **Group Membership** mapper to the client, token claim name `groups`, added to the ID token. Turn off *Full group path* unless you want to bind `/team/sub` |
| Authentik | `https://<host>/application/o/<slug>/` (keep the trailing slash) | `groups`, in the default profile scope |
| JumpCloud | `https://oauth.id.jumpcloud.com/` | `groups`, once group attributes are enabled on the app |
| Auth0 | `https://<tenant>.auth0.com/` | none by default. Add a namespaced claim from a post-login action, and set **Groups claim** to its name |

The issuer must match the token's `iss` exactly, trailing slash included. Copy it from the
discovery document rather than typing it.

## GKE

Create the workforce pool's OIDC provider against the same issuer, with a **confidential**
client of that IdP (Workforce Identity does the code exchange server-side), and keep the
`google.groups=assertion.groups` attribute mapping. The command is the same as on the
[Okta](okta.md#4-gke-the-workforce-pools-provider) page with your issuer, client ID and
secret.
