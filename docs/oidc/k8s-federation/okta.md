# Okta

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want EKS or GKE clusters to trust Okta, so people sign in to them with their Okta account and Okta groups.

Part of [Kubernetes federation with your IdP](../k8s-federation.md). That page has the
settings, the cluster steps and what the end user runs. This one covers the Okta side.

## 1. The app kubelogin signs in with (EKS)

**Applications → Create App Integration → OIDC → Native Application.**

- Grant types: **Authorization Code** and **Device Authorization**. Device Authorization is
  what the default `device-code` sign-in uses.
- Sign-in redirect URIs: `http://localhost:8000` and `http://localhost:18000`. These are
  only used if you set the sign-in to `authcode`.
- Client authentication: **none**, so the app is a public client. kubelogin always sends PKCE.
- Assign it to the people, or the groups, who should reach clusters.

## 2. Put the groups on the ID token

Which authorization server you use decides where the groups claim is configured, and
what the issuer is.

| Authorization server | Issuer | Where the groups claim goes |
|---|---|---|
| Org | `https://<org>.okta.com` | the app's **Sign On → OpenID Connect ID Token → Groups claim filter**, e.g. `groups` *Matches regex* `k8s-.*` |
| Custom, such as `default` | `https://<org>.okta.com/oauth2/<id>` | **Security → API → Authorization Servers → (server) → Claims**: a `groups` claim of value type **Groups**, included in the **ID token**, for **any scope** |

On a custom authorization server, also allow the **Device Authorization** grant in the
server's access-policy rule for this app. Without it, the device-code sign-in fails with
`unauthorized_client` even though the app allows the grant.

Keep the filter narrow. Every group it matches lands in the token, and a large token is
slower and harder to read when something doesn't match.

## 3. Dashboard settings

**Settings → Kubernetes → Cluster federation IdP**:

| Field | Value |
|---|---|
| Provider | Okta |
| Issuer URL | the issuer from the table above, copied from `<issuer>/.well-known/openid-configuration` |
| Client ID | the Native app's client ID |
| Username claim | `sub` (the Okta user id, `00u…`) |
| Groups claim | `groups` |
| Group | the Okta group name to bind, e.g. `k8s-admins` |

On EKS that group becomes the RBAC subject `okta:k8s-admins`, and the user is
`okta:00u…`.

## 4. GKE: the workforce pool's provider

Workforce Identity does the code exchange server-side, so it needs a **second** Okta app:
a **Web Application** with a client secret, separate from the public one above. Give it
the same groups claim, and the redirect URI
`https://auth.cloud.google/signin-callback/locations/global/workforcePools/<pool>/providers/<provider>`.

```bash
gcloud iam workforce-pools providers create-oidc okta-oidc \
  --workforce-pool=<pool> --location=global \
  --issuer-uri="https://<org>.okta.com/oauth2/default" \
  --client-id="<web-app-client-id>" --client-secret-value="<secret>" \
  --web-sso-response-type=code \
  --web-sso-assertion-claims-behavior=merge-user-info-over-id-token-claims \
  --attribute-mapping="google.subject=assertion.sub,google.groups=assertion.groups"
```

Then set `gcp_workforce_pool_id` and `gcp_workforce_provider_id` (`okta-oidc` here) under
Settings → Kubernetes. The `google.groups=assertion.groups` mapping is what carries the
Okta groups into the `principalSet` subject; without it nothing matches.

## Okta-specific troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Device-code sign-in fails with `unauthorized_client` | the app, or the custom authorization server's policy rule, doesn't allow Device Authorization | allow it in both, or set the sign-in to `authcode` |
| Signed in, `Forbidden`, and the token has no `groups` | the claim is on the access token only, or the filter matches none of the user's groups | put it on the **ID token**; widen the filter |
| `Unauthorized` right after sign-in | the issuer setting is the org URL but the token came from `/oauth2/default`, or the other way round | use the issuer from the discovery document of the server the app actually uses |
| A group change doesn't show up | claims are computed when the token is issued | delete the kubelogin cache and sign in again |
