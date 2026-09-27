# Service accounts and OAuth client credentials

> **Audience:** operator · **Profile:** `both` · **Read this when:** something that is not a person — a CI job, an MCP agent, a script — needs to call the dashboard's API, and you would otherwise hand it a PAT.

A **service account** is a workload identity inside the dashboard. It authenticates with the
OAuth 2.0 **client credentials** grant (RFC 6749 §4.4): it holds a client ID and secret,
trades them at the dashboard's own token endpoint for an access token that lives minutes,
and presents that token to the REST API, the WebSocket feeds and `/mcp`.

No third-party identity provider is involved. The dashboard is its own authorization
server for this one grant.

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
| `POST` | `/api/oauth/token` | the token endpoint (unauthenticated; client credentials only) |

Failed client authentications share the sign-in page's throttle, keyed per client ID and
per source address.

## What is not here yet

Both of these slot in behind the same bearer resolver (`api/auth.resolve_bearer`) and the
same principal, without changing anything above:

- **Tokens from an external IdP.** Accepting an Entra ID / Okta / Keycloak client-credentials
  access token directly, mapped to a service account by its client ID. The dashboard
  already talks to those providers for sign-in ([`oidc_service`](../web_dashboard/services/oidc_service.py));
  what is missing is validating their *access* tokens and the mapping table.
- **SPIFFE JWT-SVIDs as the client assertion** (RFC 7523). The worker would present its
  JWT-SVID instead of a secret, so nothing static is held anywhere — the bridge
  `agentcell_service`'s docstring names as the gap between the agent's identity and its
  authorization.
