# Rancher: just-in-time access through Entitle

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want people to get time-boxed access to Rancher through Entitle, a grant fails, or you are considering a real auth provider on the node.

Part of [Rancher](../rancher.md).

Registering the Rancher node with Entitle, what a grantee does next, and why the node has no auth provider wired up.

## Entitle registration

Rancher has a **native** Entitle connector, so — unlike Portainer — there is no
Cloud Function adapter in the picture. Entitle mints an ephemeral account per
grant and removes it on revoke — see [Signing in after a grant](#signing-in-after-a-grant)
for the half of that the grant screen does not tell you.

If **Entitle resource registration** is enabled
(`entitle_registration_enabled`), the node auto-registers as an Entitle
**Rancher** integration at the end of the deploy job, so users can request
just-in-time Rancher RBAC through Entitle.

### What gets sent

The integration's `connection_json` is `{url, access_key, secret_key, verify}`,
where the access/secret pair is the node's `rancher_api_token` (`token-xxxxx:yyyyy`)
split on the `:`, and `verify` follows `rancher_verify_tls` (off by default — the
node serves a self-signed certificate).

`url` is Rancher's **API Endpoint** — `https://<node ip>/v3`, the string Rancher
itself prints beside the key pair on *Account & API Keys* — **not** the UI origin.
The job result names the URL it registered. Override the suffix with
`entitle_rancher_api_path`; the literal `none` sends the origin unchanged.

> **The key is `access_key`, not `access_token`.** BeyondTrust's own Rancher
> connector page prints `access_token`; the connector does not accept it. Take the
> field names from **Integrations → Add Integration → Rancher** in your tenant, not
> from the doc.

If Entitle answers `integration.invalidConfiguration` / **"Didn't find matching
connection schema"**, it matched the payload's *key set* against none of the
connector's schemas — and it will not say which key it disliked. The job error names
the keys that were sent; diff those against the Add Integration form. If the
application itself is named something other than `Rancher` in your catalog, set
`entitle_rancher_app_slug` (lowercased); a wrong name fails differently, as a 404
`Application not found`.

### Signing in after a grant

**The grant gives you a password and no username. The username is the requester's
Entitle email address** — Entitle names the ephemeral Rancher user after the email of
the person who requested, and its grant screen shows only the password. So at
Rancher's login: username = your Entitle email, password = the one on the grant.

Worth knowing before a demo, because the failure mode is a working grant that looks
broken: you have a credential, the account exists in Rancher, and there is nothing on
screen saying what to type in the first box.

### Auth providers (optional)

Nothing above needs one — the ephemeral local account is the whole flow. Wire an auth
provider only if you want grants to land on **real identities** instead: the requester
then already exists in Rancher with their own email, rather than getting a per-grant
local user.

Rancher's auth providers are configured **after** the node is up (Users &
Authentication → Auth Provider) and are independent of the deploy — the dashboard does
not configure one today, and neither does `rancher_node_deploy`:

- **Microsoft Entra ID** — reply URL `<rancher url>/verify-auth-azure`. If you have
  already set up the shared Entra app for EKS federation (`entra_oidc_client_id` /
  `entra_oidc_issuer_url`), that is a tenant Rancher can point at; add the reply URL.
- **Generic OIDC** — reply URL `<rancher url>/verify-auth`; takes client id, secret,
  issuer, Rancher URL, and optional `email` / `groups` claim overrides.

Both **finish with an interactive sign-in**: Rancher redirects you to the IdP and only
enables the provider once that login succeeds, so this is a one-time manual step per
node rather than something the deploy job can complete headlessly. Which matters more
than usual here — **the node is [ephemeral](../rancher.md#ephemeral-node)**, so recreating it wipes
`/var/lib/rancher` and the auth provider with it, and it has to be redone.

### Register and deregister by hand

The node row on **Containers → Rancher** carries the state and the controls: an
`Entitle ✓` chip with the integration id once registered, **Register in Entitle**
when it isn't, **Deregister** when it is, and **Re-apply firewall** — which
re-applies the node's allow-list for an integration that already exists, without
touching the integration itself. All three enqueue a `rancher_entitle_register` job.

You need this more often than the auto-register suggests. That registration is
**best-effort** — it logs a warning and lets the deploy succeed — so a node can be
running and unregistered with nothing else saying so. Turning
`entitle_registration_enabled` on *after* a node is already up leaves it
unregistered too.

> **Register is hidden once an integration exists, and that is deliberate.**
> Registering twice writes a second integration over the first one's Terraform
> state, so the original stays alive in Entitle with nothing able to remove it.
> Deregister first if you want to re-register.

The same operations over the API:

```
POST /api/k8s/rancher/entitle-register   {"action": "register"}
POST /api/k8s/rancher/entitle-register   {"action": "deregister"}
POST /api/k8s/rancher/entitle-register   {"action": "reachability"}   # firewall only
```

Note the permission: that route is on the k8s router and requires `k8s:write`, not
the `containers:write` the rest of the Rancher tab uses. The buttons are hidden
from anyone who lacks it rather than shown and then refused.

### Reachability — Entitle is allow-listed for you

Because the node is publicly reachable, Entitle's cloud connects to it **directly**
(no agent token). So its egress addresses are a source hitting the node's
source-restricted firewall, exactly like a Gateway's `/32` is.

**Registering allow-lists Entitle automatically.** The register job re-applies the
node firewall with Entitle's ranges merged in, and deregistering removes them
again — they are only ever open while an integration exists. The deploy-time
auto-register re-applies it too, as a *second* pass: the deploy merges the firewall
near the start and only registers at the end, so the first merge cannot know about
an integration that does not exist yet. The breakdown in
**Settings → Kubernetes → Effective firewall allow-list** names them as
`Entitle egress` so they don't read as unexplained entries.

This matters more than it sounds, because the failure is silent otherwise:
registration talks to Entitle's **API**, never to your node, so it succeeds whether
or not the node admits Entitle — and the first symptom is a *grant* that times out,
which reads like a broken integration rather than a firewall rule.

### A grant times out — `ConnectTimeoutError` on the node's address

Entitle reports it as a connect timeout from its own connector, naming the node's
public IP and port 443:

```
HTTPSConnectionPool(host='<node ip>', port=443): Max retries exceeded with url: /
 (Caused by ConnectTimeoutError(..., 'Connection to <node ip> timed out.'))
```

Nothing is wrong with the integration — that is a packet that never arrived. Work
through it in this order:

1. **Is Entitle in the node's allow-list?** Open **Settings → Kubernetes →
   Effective firewall allow-list**. That readout is the set the rule is *built*
   from, not a read of the live rule.
2. **Re-apply it.** Use **Re-apply firewall** on the node row. Deploys before
   2026-09-22 registered at the tail of the deploy job but merged the firewall
   *earlier* in the same job — before the integration existed — so the ranges were
   correctly computed as "none" and the node came up closed to Entitle with nothing
   saying so. That is the exact shape this button repairs, and it is the only way
   out of it: **Register is hidden once an integration exists**, and re-registering
   would strand the live one rather than fix it.
3. **If the list already holds the three published addresses**, your tenant
   egresses from somewhere else — they are per-deployment, not per-region (see
   below). Get your tenant's addresses from BeyondTrust and set
   `entitle_source_cidrs`, then **Re-apply firewall**.
4. **If the node isn't reachable from the internet at all**, switch to
   `entitle_rancher_private` and broker through the agent instead.

A timeout is specifically *not* a TLS problem: `rancher_verify_tls` and the node's
self-signed certificate are only reached after the connection is established. A
certificate mismatch shows up as an SSL error, not `ConnectTimeoutError`.

Where the ranges come from, in order:

| Source | Notes |
|---|---|
| `entitle_source_cidrs` | CSV of CIDRs; a bare address needs its `/32`. **Replaces** the list below rather than extending it |
| the published list | BeyondTrust's documented allow-list, keyed off the region already in `entitle_api_url` (`api.us.entitle.io` → `us`), in [`services/entitle_egress.py`](../../../web_dashboard/services/entitle_egress.py) |

**Entitle US (Pathfinder deployment)** ships in that list, so a US tenant needs no
configuration — register, and the node admits Entitle. EU is not populated; an EU
tenant supplies its own via `entitle_source_cidrs`.

> **These addresses are per-DEPLOYMENT, not merely per-region.** `entitle_api_url`
> can only tell us the region, so a tenant on a US deployment *other* than Pathfinder
> egresses from different addresses — and the built-in list would then admit three
> hosts that never call the node while still dropping every real grant. That is why
> `entitle_source_cidrs` overrides rather than extends: if you are not on Pathfinder,
> set it.

A region with no published list reads as **unknown**, never as "no ranges needed":
the register job puts the gap in its result and the node row says the integration is
registered but unreachable, rather than reporting something that cannot grant as
healthy.

For tenants who lock the node behind CIDRs that Entitle can't traverse, set
`entitle_rancher_private = true` to attach the shared Entitle agent token instead.
The agent reaches the node from inside, so no inbound ranges are opened at all and
`entitle_source_cidrs` is irrelevant. It is an env/config-only switch today — it
appears in neither the Settings panel nor `EntitleFeatureConfig`. See the
[Entitle guide](../beyondtrust/entitle.md) for enabling resource registration.

### A sync fails with `Expecting value: line 1 column 1 (char 0)`

That is Python's `json.JSONDecodeError`, raised **inside Entitle's connector** — the
dashboard cannot produce it (every parse on the registration path is guarded, and the
string appears nowhere in this repo). It means the connector called the URL in the
integration and the body it got back was not JSON. It names neither the URL, nor the
status code, nor Rancher, which is why it reads like a broken integration.

The cause is almost always the **URL**: registered as the UI origin
(`https://<node ip>`) rather than Rancher's API Endpoint (`https://<node ip>/v3`).
Rancher answers its origin with the web UI — a redirect to the SPA and a body of
HTML — and `<` is not a JSON document. Deploys before 2026-09-22 registered the
origin; see [What gets sent](#what-gets-sent).

Read the variants byte-for-byte, because each says exactly what `json.loads` was
handed and that is the fastest way to identify the body:

| Message | What the body was |
|---|---|
| `Expecting value: line 1 column 1 (char 0)` | empty, or a first byte that cannot start JSON — the `<` of HTML, the `h` of a URL, the `t` of `token-…` |
| `Extra data: line 1 column 5 (char 4)` | a **valid 4-character JSON value followed by more text** — a dotted number (`10.0…`, `1.28…`), a `2026-…` date, or `true`/`null` with junk after it. The connector parsed something and then hit trailing bytes, so it got *further* than the case above |

To fix a live integration: the `url` is inside the integration, so it has to be
rewritten. Either edit the field on the integration in Entitle and re-sync (fastest,
no redeploy), or **Deregister** and then **Register in Entitle** from the node row —
Register is hidden while an integration exists, so it must be in that order.

A node **teardown** deregisters for you before the VM goes away, and clears
`entitle_rancher_integration_id` either way — so the chip reverts on its own.

## Auth providers (optional, not wired up)

Out of the box the node has **local users only**: the `admin` account the dashboard
bootstraps, plus whatever Entitle mints for a grant (the ephemeral account's username
is the requester's Entitle email; the grant screen shows only the password). That is
enough for the demo. You would only want a real auth provider — Entra via `azuread`,
or any IdP via `genericoidc` — if grants should land on **real identities** instead of
per-grant local users.

Nothing in the dashboard configures one today. The notes below are a **spike result**
(2026-09-22, read from Rancher + `terraform-provider-rancher2` source; **not** exercised
against a live node), recorded so the next person doesn't re-derive it.

### Enabling a provider is one plain PUT — no browser round-trip

The UI's *Enable* button posts `?action=testAndApply`, which does a real IdP sign-in
and only then saves the config. **That action is not the only way to enable a provider.**
`enabled` is an ordinary, updatable field on the `AuthConfig` CRD, and the login screen
is driven by nothing else: Rancher lists a provider when `authConfig.enabled` is true and
a provider is registered for its `type`. Writing it directly sticks. This is exactly what
`rancher2_auth_config_azuread` / `_generic_oidc` do — they never call an action, they
`PUT` the config with `enabled = true`.

Three details that decide whether the call works:

- **PUT the *subtype* path, not `/v3/authConfigs/<name>`.** Norman marks `type` as
  `noupdate`, so it is stripped from a PUT to the base collection — and the auth-config
  store rejects a body with no `type` (*"invalid data for auth store update"*). The
  subtype store injects it. Use `/v3/azureADConfigs/azuread`,
  `/v3/genericOIDCConfigs/genericoidc`, `/v3/keyCloakOIDCConfigs/keycloakoidc`.
- **The PUT merges, it does not replace** (unless you pass `?replace=true`), so a partial
  body is fine and annotations survive — including `auth.cattle.io/azuread-endpoint-migrated`,
  which selects the MS Graph flow over the retired Azure AD Graph one.
- **Password fields go to secrets for you.** `clientSecret` / `applicationSecret` /
  `privateKey` are moved into `cattle-global-data` by the store on the normal API path —
  the same as the UI does.

Disabling is the reverse: `POST /v3/<subtype>s/<name>?action=disable`, or a PUT with
`enabled: false`. Only **one** non-local provider should be enabled at a time (the
Terraform provider refuses client-side; `local` is always exempt, so the dashboard's
admin token keeps working either way).

What a direct write does **not** do, that `testAndApply` does:

- It doesn't validate anything. A typo yields a login button that fails when a human
  clicks it. `POST ?action=configureTest` returns the IdP redirect URL without side
  effects and is a cheap smoke test; fetching `<issuer>/.well-known/openid-configuration`
  and a `client_credentials` token request validate the rest.
- It doesn't bind the external identity to the local `admin` user
  (`SetPrincipalOnCurrentUser`), and doesn't populate `allowedPrincipalIds`. So either set
  `accessMode: unrestricted` (anyone in the tenant can sign in, landing as a fresh Rancher
  user with no roles), or set `accessMode: restricted` and hand-build the principal IDs —
  `azuread_user://<objectId>`, `azuread_group://<objectId>`, `genericoidc_user://<sub>`.
  Those need no browser: Graph gives you the object IDs.

### The round-trip itself can't be scripted

If you did want `testAndApply`'s identity binding, it needs an OAuth **authorization
code** issued for Rancher's own `client_id` and redirect URI. ROPC and client-credentials
produce tokens, not a code, and Rancher has no path that accepts a raw `id_token`. Forging
the `/verify-auth-azure` callback is not possible either — the code is the thing you don't
have. Driving a headless browser would break on MFA/conditional access in a corporate
tenant. Treat the binding as a one-time human click, or skip it.

### Why it isn't wired into the deploy tail

Automating the Rancher side is the easy half. The blocker is the **IdP side**: the reply
URL `https://<node>/verify-auth-azure` (or `/verify-auth` for `genericoidc`) must be
registered on the app registration *exactly*, and Entra allows no wildcard that helps —
a wildcard reply URL strips the query string, which is where the code arrives.

That collides with the [ephemeral node](../rancher.md#ephemeral-node): on GCP and AWS the public IP
changes on every recreate, so every recreate would need a tenant write (Graph
`Application.ReadWrite.All`, plus pruning against the 256-URI cap) — a silent per-deploy
side effect on a shared object, which is not something the dashboard should be doing.

Two ways to make it worth wiring:

- **Host the node on Azure**, where the public IP is Standard/Static and survives a
  recreate. One reply URL, registered once by hand, stays valid — and the deploy tail
  becomes a single best-effort PUT, in the same shape as
  `rancher_service.complete_first_run_direct`.
- **Give the node a stable DNS name** on any cloud and register that once.

Either way it wants its **own app registration**, not the shared EKS-federation app
(`entra_oidc_client_id`): that one is a public client used by `kubectl oidc-login`'s
device-code flow, and Rancher needs a confidential client — a client secret, plus Graph
application permissions if you want user/group search.
