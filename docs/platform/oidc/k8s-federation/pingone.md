# PingOne

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want EKS or GKE clusters to trust PingOne, so people sign in to them with their PingOne account and groups.

Part of [Kubernetes federation with your IdP](../k8s-federation.md). That page has the
settings, the cluster steps and what the end user runs. This one covers the PingOne side.

## 1. The app kubelogin signs in with (EKS)

**Applications → Add application → Native.**

- Grant types: **Authorization Code** and **Device Authorization**.
- Token endpoint authentication: **None**, so the app is a public client. kubelogin
  always sends PKCE.
- Redirect URIs: `http://localhost:8000` and `http://localhost:18000`, used only if you
  set the sign-in to `authcode`.
- Turn the application on, and grant access to the people or groups who should reach
  clusters.

## 2. Put the groups on the ID token

In the app's **Attribute mappings**, add a `groups` attribute mapped to the user's
**group names**, and make sure it is included in the ID token. Map **group IDs** instead
if you'd rather bind by ID. Group names are easier to read in RBAC, but a rename in
PingOne then silently breaks the binding.

## 3. Dashboard settings

**Settings → Kubernetes → Cluster federation IdP**:

| Field | Value |
|---|---|
| Provider | PingOne |
| Issuer URL | `https://auth.pingone.<region>/<environment-id>/as` |
| Client ID | the Native app's client ID |
| Username claim | `sub` |
| Groups claim | `groups` |
| Group | the PingOne group to bind, e.g. `k8s-admins` |

The region is `com`, `eu`, `ca`, `asia`, `com.au` or `sg`, the same as for a PingOne
directory registered under [Identity providers](../../../preview/directories/identity-providers.md),
and the environment id is that directory's environment. Copy the exact issuer from
`<issuer>/.well-known/openid-configuration`.

On EKS the group becomes the RBAC subject `pingone:k8s-admins`, and the user is
`pingone:<sub>`.

## 4. GKE: the workforce pool's provider

Workforce Identity does the code exchange server-side, so it needs a **second** PingOne
application: a **Web App** with a client secret, separate from the public one above. Give
it the same `groups` mapping, and the redirect URI
`https://auth.cloud.google/signin-callback/locations/global/workforcePools/<pool>/providers/<provider>`.

```bash
gcloud iam workforce-pools providers create-oidc pingone-oidc \
  --workforce-pool=<pool> --location=global \
  --issuer-uri="https://auth.pingone.<region>/<environment-id>/as" \
  --client-id="<web-app-client-id>" --client-secret-value="<secret>" \
  --web-sso-response-type=code \
  --web-sso-assertion-claims-behavior=merge-user-info-over-id-token-claims \
  --attribute-mapping="google.subject=assertion.sub,google.groups=assertion.groups"
```

Then set `gcp_workforce_pool_id` and `gcp_workforce_provider_id` (`pingone-oidc` here)
under Settings → Kubernetes.

## PingOne-specific troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Device-code sign-in is refused | the app doesn't have the Device Authorization grant | add it, or set the sign-in to `authcode` |
| `Unauthorized` right after sign-in | the issuer names the wrong region or environment | copy it from the discovery document |
| Signed in, `Forbidden`, and the token has no `groups` | the attribute mapping is missing or not on the ID token | add the `groups` mapping to the ID token |
| Access stopped after a group was renamed | the binding names the old group name | re-bind, or map group IDs instead |
