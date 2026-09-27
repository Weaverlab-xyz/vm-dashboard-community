# Service accounts and OAuth client credentials

> **Audience:** operator · **Profile:** `both` · **Read this when:** something that is not a person — a CI job, an MCP agent, a script — needs to call the dashboard's API, and you would otherwise hand it a PAT.

A **service account** is a workload identity inside the dashboard. It authenticates with the
OAuth 2.0 **client credentials** grant (RFC 6749 §4.4): it holds a client ID and secret,
trades them at the dashboard's own token endpoint for an access token that lives minutes,
and presents that token to the REST API, the WebSocket feeds and `/mcp`.

No third-party identity provider is needed: the dashboard is its own authorization server
for this one grant. If your workloads already have identities at an IdP, they can use its
tokens instead — see [Tokens from your own IdP](#tokens-from-your-own-idp-no-dashboard-secret).

## Why not a PAT

A Personal Access Token is a person's credential lent to a machine:

| | PAT on a user | Service account + OAuth client |
|---|---|---|
| Principal | a `users` row that looks like a person: password, sign-in page, can be made admin | a workload: no password, cannot sign in, **never** admin |
| Nothing granted means… | **everything** (an empty permission map is unrestricted, for legacy users) | **nothing** |
| What crosses the wire on every call | the long-lived secret itself | an access token that expires in 15 minutes by default |
| Expiry | optional | required on the secret (max 365 days); token TTL 1–60 minutes |
| Revocation | stops the PAT | stops the client **and every access token it already issued**, on their next use |
| Rotation | mint a new PAT, then revoke the old | rotate in place; the old secret keeps working for a grace window |
| Narrowing per job | no | request a `scope` — the token can only narrow what the account holds |

PATs still work everywhere they did. Service accounts are the recommended shape for
anything non-human.

## Setting one up

1. **Users → + New Service Account.** Give it a username, and grant access with a role or
   the permission grid. There is no "Full access" option: a service account with nothing
   granted can do nothing. The Administrator role is not offered and the API refuses it.
2. On its row, click **OAuth clients → Create**. Pick a secret lifetime (default 90 days)
   and an access-token lifetime (default 900 seconds). The **client ID** and **client
   secret** are shown once — store the secret in your secret manager now.
3. The workload exchanges them for a token:

```bash
curl -s -u "$CLIENT_ID:$CLIENT_SECRET" \
     -d grant_type=client_credentials \
     https://dashboard.example.com/api/oauth/token
# {"access_token":"eyJ…","token_type":"Bearer","expires_in":900}
```

```powershell
$pair  = "{0}:{1}" -f $env:CLIENT_ID, $env:CLIENT_SECRET
$basic = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair))
$tok   = Invoke-RestMethod -Method Post -Uri "https://dashboard.example.com/api/oauth/token" `
           -Headers @{ Authorization = "Basic $basic" } `
           -Body @{ grant_type = "client_credentials" }
Invoke-RestMethod -Uri "https://dashboard.example.com/api/jobs" `
  -Headers @{ Authorization = "Bearer $($tok.access_token)" }
```

4. Use `Authorization: Bearer <access_token>` exactly as you would a PAT. When
   `expires_in` is nearly up, run the grant again — there are no refresh tokens.

Client authentication may be HTTP Basic (`client_secret_basic`) or the `client_id` /
`client_secret` form fields (`client_secret_post`). Metadata for SDKs and MCP clients is at
`/.well-known/oauth-authorization-server` (RFC 8414).

### Narrowing a token with `scope`

Scopes are the permission grid's `section:level` pairs, space-separated:

```bash
curl -s -u "$CLIENT_ID:$CLIENT_SECRET" \
     -d grant_type=client_credentials -d "scope=jobs:read vms:read" \
     https://dashboard.example.com/api/oauth/token
```

The issued `scope` is the intersection of what was asked for and what the account holds;
a scope it does not hold is dropped, never granted. If nothing requested is held, the
request fails with `invalid_scope`. With no `scope`, the token carries the account's full
permissions.

### Rotating and revoking

- **Rotate** issues a new secret; the previous one keeps working for 60 minutes (the API
  accepts `grace_minutes` from 0 to 7 days) and never past its own expiry.
- **Revoke** deactivates the client. Tokens are checked against their client on every
  request, so a revoked client's outstanding tokens are refused immediately, not when they
  expire. Disabling the service account has the same effect.

Every create, rotate and revoke is written to the [audit log](audit-log.md).

## The agent demo cell

When the cell's token user is a service account, the cell mints an **OAuth client** rather
than a PAT, with its secret expiring when the cell's token would have. The create response's
`token` is the `client_id:secret` pair, which is what goes in the worker's token file (or
Workload Credentials / Password Safe) — the install playbook is unchanged. The worker
(`mcp_agent.py`) recognises the pair, exchanges it at `/api/oauth/token` (override with
`--oauth-token-url`), and refreshes a minute before each token expires. Revoking the cell
revokes the client, and the worker's next call is refused.

## API reference

All routes are admin-only.

| Method | Path | |
|---|---|---|
| `POST` | `/api/users/service-accounts` | create (`username`, `full_name`, `workgroups`, `permissions`, `role_id`) |
| `GET` | `/api/users/{id}/oauth-clients` | list clients (never secrets) |
| `POST` | `/api/users/{id}/oauth-clients` | create (`name`, `secret_days`, `token_ttl_seconds`) — returns `client_secret` once |
| `POST` | `/api/users/{id}/oauth-clients/{client}/rotate` | new secret (`secret_days`, `grace_minutes`) — returns it once |
| `DELETE` | `/api/users/{id}/oauth-clients/{client}` | revoke |
| `GET` | `/api/users/{id}/external-identities` | list IdP identities mapped to it |
| `POST` | `/api/users/{id}/external-identities` | map one (`name`, `subject`, `issuer`, `expected_client`) |
| `DELETE` | `/api/users/{id}/external-identities/{mapping}` | remove the mapping |
| `POST` | `/api/oauth/token` | the token endpoint (unauthenticated; client credentials only) |

Failed client authentications share the sign-in page's throttle, keyed per client ID and
per source address.

## Tokens from your own IdP (no dashboard secret)

A workload that already has an identity at your IdP — an Entra app registration or managed
identity, an Okta or Keycloak service client — can call the dashboard with the **access
token that IdP issues**, and hold no dashboard secret at all. The dashboard verifies the
token's signature against the IdP's published keys, checks its issuer, audience and expiry,
and maps it to a service account. Permissions still come only from the service account.

### 1. Trust the IdP

**Settings → Single sign-on (OIDC) → Workload tokens**:

| Setting | |
|---|---|
| Workload issuer | the IdP's issuer URL. Blank reuses the SSO issuer. SSO itself is *not* required. |
| Audience | **required** — the value your IdP puts in `aud` for tokens meant for the dashboard. A token minted for any other API at the same IdP is refused. Space-separate to accept several. |
| Extra accepted issuers | optional — other exact `iss` values signed by the same keys (see Entra below). |

**Test workload trust** fetches the IdP's discovery document and signing keys, and warns
if the issuer the IdP reports is not one you accept.

### 2. Map the workload to a service account

On the service account's row: **OAuth clients → External identities → Add**. The key field
is the **subject** — the token's `sub` claim:

| IdP | `sub` in a client-credentials token | Audience |
|---|---|---|
| Entra ID | the **service principal's object id** (Enterprise applications → the app → Object ID), or the managed identity's object (principal) id | the Application ID URI you exposed, e.g. `api://vm-dashboard`, and/or its app id |
| Okta | the **client id** (custom authorization server; the org server issues opaque tokens) | the authorization server's audience |
| Keycloak | the client's **service-account user id** (Clients → the client → Service account roles → user) | the value of an audience mapper on the client |

**Why the subject and not the client id (`azp`/`appid`)?** A *person* who signs in through
the same app registration gets a token with the same `azp` and their *own* `sub`. Mapping
on `azp` would let any user of that app act as the workload. The optional **expected client**
field adds an `azp`/`appid`/`client_id`/`cid` check on top of the subject, never instead of it.

### 3. Call the dashboard

```powershell
# An Entra client-credentials token for the dashboard's API, then a call with it.
$tok = Invoke-RestMethod -Method Post `
  -Uri "https://login.microsoftonline.com/$env:TENANT_ID/oauth2/v2.0/token" `
  -Body @{ grant_type = "client_credentials"; client_id = $env:CLIENT_ID
           client_secret = $env:CLIENT_SECRET; scope = "api://vm-dashboard/.default" }
Invoke-RestMethod -Uri "https://dashboard.example.com/api/jobs" `
  -Headers @{ Authorization = "Bearer $($tok.access_token)" }
```

On an Azure VM or pod with a managed identity, the token comes from IMDS instead and the
workload holds no secret anywhere.

### Entra v1 vs v2 tokens

Entra issues **v1** access tokens (`iss: https://sts.windows.net/<tenant>/`, with the
trailing slash) unless the API's app registration sets
`"accessTokenAcceptedVersion": 2` in its manifest; v2 tokens carry
`https://login.microsoftonline.com/<tenant>/v2.0`. Either set the manifest to 2, or add
the v1 issuer under **Extra accepted issuers**. Issuers are compared exactly.

### What is and is not checked

- Signature against the IdP's JWKS (asymmetric algorithms only — an `HS*` token is never
  treated as an IdP token, which rules out algorithm confusion). A key rotation costs one
  JWKS refetch; an unknown key id cannot make every request call the IdP.
- `iss` exact, `aud` in the configured set, `exp`/`nbf` with 60s leeway, `sub` present.
- IdP scopes and app roles are **not** translated into dashboard permissions.
- Revocation: removing the mapping, or disabling the service account, refuses the next
  request. The token's own lifetime is the IdP's to set.
- The token must be a **JWT** issued for the dashboard's audience. Opaque tokens (Okta's
  org authorization server) and tokens for other APIs (Microsoft Graph) cannot be verified.

## What is not here yet

This slots in behind the same bearer resolver (`api/auth.resolve_bearer`) and the same
principal, without changing anything above:

- **SPIFFE JWT-SVIDs as the client assertion** (RFC 7523). The worker would present its
  JWT-SVID instead of a secret, so nothing static is held anywhere — the bridge
  `agentcell_service`'s docstring names as the gap between the agent's identity and its
  authorization.
