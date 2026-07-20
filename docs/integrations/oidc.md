# Generic OIDC Integration

## What is it?

The OIDC integration lets people sign in to the dashboard with your existing
identity provider (IdP) instead of a local username and password. It speaks
**generic OpenID Connect**, driven entirely by the provider's discovery document
(`<issuer>/.well-known/openid-configuration`), so one implementation covers any
compliant IdP — you point it at an issuer, register a client, and it reads the
authorize, token and JWKS endpoints from the provider itself. There is no
per-provider code path to maintain.

Under the hood it uses the **authorization-code flow with PKCE** (S256, always
sent), and every returned ID token has its **signature verified against the
provider's JWKS**, along with the issuer and audience, before anyone is logged in.

OIDC login sits alongside the dashboard's other sign-in methods and doesn't
replace them:

- **Local password accounts** — unaffected.
- **FIDO2 / WebAuthn MFA** — unaffected.
- **"Sign in with Microsoft" (legacy Entra/Azure path)** — configured separately
  under the Azure settings and unaffected. If you'd rather run Entra through this
  generic path, you can — see [Provider quick reference](#provider-quick-reference).

> **Scope.** GitHub is **not** supported — it isn't an OIDC provider (no discovery
> document, no ID token). **SAML is not supported** either. This integration is
> OIDC only.

---

## Supported providers

Any spec-compliant OIDC provider works. Verified/common ones:

| Provider | Notes |
|---|---|
| Keycloak | Groups arrive in the `groups` claim (add a groups mapper to the client scope). |
| Authentik / Authelia | `groups` claim. |
| Okta | Group memberships often surface as `roles` rather than `groups` — set **Groups claim** accordingly. |
| Auth0 | Groups/roles usually need a custom claim/action to be emitted. |
| Google Workspace | Standard OIDC; Google does not emit groups in the ID token — group→workgroup mapping won't apply. |
| JumpCloud | `groups` claim. |
| Ping | `groups` claim (configurable). |
| GitLab | Standard OIDC. |
| Microsoft Entra ID | `groups` claim carries group **object IDs**, not display names — map by object ID (see below). |

If your IdP publishes `<issuer>/.well-known/openid-configuration` and issues a
standard ID token, it will work even if it isn't in this list.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| An OIDC identity provider | Must expose a discovery document at `<issuer>/.well-known/openid-configuration`. |
| Ability to register an application/client with the IdP | Confidential *or* public client — both work (PKCE is always sent). |
| A redirect (callback) URI you can register | `<dashboard-base-url>/api/auth/oauth/oidc/callback` (exact value shown in the Settings panel). |
| The dashboard reachable at a stable base URL | The base URL the browser uses must match the redirect URI you register. |
| Dashboard admin access | The OIDC panel and group mappings are admin-only. |

---

## Setup

### Step 1 — Register an application with your IdP

Create an OIDC application/client in your provider and note the **client ID** (and
**client secret**, if you use a confidential client). Set the **redirect / callback
URI** to:

```
<dashboard-base-url>/api/auth/oauth/oidc/callback
```

For example, `https://dashboard.example.com/api/auth/oauth/oidc/callback`. The
OIDC settings panel prints the exact value derived from the dashboard's current
origin — copy it from there to avoid typos. The URI must match **exactly**,
including scheme, host and port.

A **public client** (no secret) is fine — PKCE protects the code exchange. Leave
the client secret blank in that case.

If you want group-based access (recommended), make sure the application is
configured to emit a **groups** (or roles) claim in the ID token. Note which claim
name your provider uses.

### Step 2 — Configure the dashboard

Go to **Settings → Integrations → Single sign-on (OIDC)** and fill in:

| Field | Config key | Default | Notes |
|---|---|---|---|
| Issuer URL | `oidc_issuer` | — | Base URL only, e.g. `https://keycloak.example.com/realms/main`. `/.well-known/openid-configuration` is appended automatically. |
| Client ID | `oidc_client_id` | — | From step 1. |
| Client secret | `oidc_client_secret` | — | Optional — leave blank for a public client. Stored encrypted; shown as dots on reload. Leave the dots untouched to keep the stored value. |
| Button label | `oidc_provider_name` | issuer host | Shown on the login page as "Sign in with …". |
| Scopes | `oidc_scopes` | `openid profile email groups` | Space-separated. `openid` is always included even if you omit it. |
| Groups claim | `oidc_groups_claim` | `groups` | The ID-token claim that carries the user's group names/IDs, used for workgroup mapping. Set to `roles` for Okta-style setups. |

> **There is no separate "enable" toggle.** SSO goes live the moment an **issuer
> and client ID** are saved, and turns off when you clear the issuer. Settings are
> stored encrypted in the application database and apply immediately — no `.env`
> edit and no restart.

### Step 3 — Test the connection

Click **Test connection** in the panel. This fetches the live discovery document
and reports the endpoints the provider advertises, its `scopes_supported`, and
whether the groups claim you configured appears in the provider's
`claims_supported`.

> The groups-claim check is **advisory**. Many providers omit `claims_supported`
> entirely, and some emit `groups` without advertising it — so a "not advertised"
> result is not necessarily a problem. A typo'd issuer, on the other hand, fails
> here with the exact URL it couldn't reach.

### Step 4 — Map groups to workgroups (authorization)

Authentication proves *who* someone is; **group mappings decide what they can do.**
On the **/groups** admin page, add a mapping for each provider group:

- **Group ID** — the value that appears in the user's groups claim (for Entra, the
  group's **object ID**, not its name).
- **Workgroup** — the dashboard workgroup that group's members are placed in.
- **Default permissions** *(optional)* — a JSON object of permission scopes granted
  to that group.

On every SSO login the dashboard:

1. Reads the user's group claims and matches them against your mappings.
2. Assigns the matched **workgroups** and unions each matched group's
   **default permissions** into the user's session permissions (so removing someone
   from a group in the IdP drops the corresponding access on their next login).
3. **Auto-creates the user on first login** if any group matched — no
   pre-registration needed.
4. **Rejects the login** (`/login?error=not_authorized`) if the user is in *none*
   of the mapped groups.

> If you configure **no** group mappings at all, the dashboard falls back to legacy
> behavior: the user must already exist as a local account, and SSO simply
> authenticates them.

For the permission model and how group-derived permissions combine with
admin-granted ones, see the [Entitle user-JIT design doc](../design/entitle-user-jit.md).

---

## What it enables

| Capability | Description |
|---|---|
| **SSO login button** | A "Sign in with `<label>`" button appears on the login page once configured. |
| **Auto-provisioning** | Users in a mapped group are created automatically on first login. |
| **Group-driven access** | Workgroups and permissions are derived from IdP group membership and re-synced on every login. |
| **Permission enforcement** | Derived permissions feed the dashboard's scope/level checks (`vms`, `aws`, `k8s`, `cloud_database`, …). |

---

## Provider quick reference

Issuer URL format per provider (paste the **base** into the Issuer URL field):

| Provider | Issuer URL example | Groups claim |
|---|---|---|
| Keycloak | `https://keycloak.example.com/realms/<realm>` | `groups` |
| Okta | `https://<org>.okta.com` | `groups` or `roles` |
| Auth0 | `https://<tenant>.auth0.com/` | custom (namespaced) |
| Google Workspace | `https://accounts.google.com` | none in ID token |
| Microsoft Entra ID | `https://login.microsoftonline.com/<tenant-id>/v2.0` | `groups` (object IDs) |
| JumpCloud | `https://oauth.id.jumpcloud.com/` | `groups` |

---

## Troubleshooting

**"Could not fetch the OIDC discovery document from …"** — the issuer URL is wrong
or unreachable from the dashboard container. The error includes the exact URL it
tried; open it in a browser. Use **Test connection** to confirm. Remember: enter
the **base** issuer only — don't include `/.well-known/openid-configuration`
yourself.

**"ID token failed validation"** — usually an audience or issuer mismatch, or clock
skew. The **client ID** you configured must equal the token's `aud`. Confirm the
dashboard host's clock is correct.

**"Token response contained no id_token — is the 'openid' scope granted?"** — the
`openid` scope wasn't granted for this client. Ensure `openid` is in the app's
allowed scopes at the IdP (the dashboard always requests it).

**Redirect fails at the provider (redirect_uri mismatch)** — the registered
callback must match `<dashboard-base-url>/api/auth/oauth/oidc/callback` exactly,
including scheme, host and port. Copy the value shown in the Settings panel.

**`error=not_authorized` after login** — the user isn't in any mapped group, or the
IdP isn't actually emitting the groups claim. Check **/groups**, confirm the app
emits the claim (Entra sends group **object IDs**, so map by ID not name), and set
the **Groups claim** field to match your provider (e.g. `roles` for Okta).

**`error=not_registered`** — no group mappings are configured, so login falls back
to requiring a pre-existing local account and none matched this user. Add a group
mapping, or create the local account first.

**`error=account_disabled`** — the matching dashboard account is deactivated.
Re-enable it in user management.

---

## Not to be confused with

`docs/integrations/entra-k8s-federation.md` covers a different feature — federating
**Kubernetes cluster RBAC** to Entra via OIDC. This page is about **dashboard login
SSO**.
