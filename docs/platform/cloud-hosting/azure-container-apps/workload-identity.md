# Pathfinder without a PAT on Azure Container Apps

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want the dashboard on Azure Container Apps to reach BeyondTrust Pathfinder with a managed identity instead of a stored token.

Part of [Cloud Hosting: Azure Container Apps](../azure-container-apps.md).

> **Applies to:** an install using [Workload Credentials](../../../beyondtrust/workload-credentials.md).
> Skip this if you do not. **Status:** run end to end on the Azure Container
> Apps install on 2026-09-15, so the walkthrough below is a transcript rather
> than a sketch. The one leg still unproven is the *worker* minting under a
> real job, which is a different process from the one Test connection exercises.

Workload Credentials replaced three standing cloud keys with **one** standing
platform credential: a Personal Access Token, stored encrypted in `app_config`.
That token is the last secret this feature holds, and it is the one that has
actually caused an outage here — a dead PAT produces
`401 Personal access token not found` on every mint, from a dashboard that looks
entirely healthy.

That token can go away. The platform can hand the container an identity of its
own; Pathfinder can be told, once, to trust it. The container then presents a
short-lived OIDC token it fetched from the platform a moment earlier, and there
is **no BeyondTrust credential anywhere in the deployment** — nothing in
`app_config`, nothing in the Container App's secrets, nothing to rotate.

**This is not Azure-only, and the walkthrough below is the Azure one because that
is the install it was run on.** GCP and AWS have their own:
[GCP Cloud Run](../gcp-cloud-run.md#pathfinder-without-a-pat) and
[AWS (EKS)](../aws-ecs.md#pathfinder-without-a-pat). Each platform hands a
container an identity, with one exception worth knowing before you start:

| Identity platform | Where the token comes from | Registers in Pathfinder as |
|---|---|---|
| **Azure** — Container Apps, App Service, a VM | `IDENTITY_ENDPOINT`/`IDENTITY_HEADER`, else IMDS | **Azure Entra ID**, or **Custom IDP** |
| **GCP** — Cloud Run, GCE, GKE | the metadata server's instance identity endpoint | **Custom IDP**, issuer `https://accounts.google.com` |
| **AWS** — **EKS only** | a projected token file (IRSA, or Pod Identity) | **Custom IDP**, issuer = the cluster's OIDC provider URL |
| **Other** | any projected token on disk, e.g. a Kubernetes ServiceAccount token | **Custom IDP** |
| **The dashboard's own SPIFFE identity** — any host, ECS and on-prem included | `wlc.jwt`, minted by the dashboard's SPIRE server | **Custom IDP**, issuer = the dashboard's `/spiffe` URL ([steps](../../oidc/dashboard-identity.md#beyondtrust-workload-credentials)) |

> **ECS is the exception.** An ECS task — and a plain EC2 instance — gets SigV4
> credentials and a signed instance identity document from the task metadata
> endpoint. Neither is an OIDC token, and there is no endpoint that will issue
> one. A dashboard on ECS uses its own SPIFFE identity or stays on a stored Personal Access Token; the panel says
> so, and so does the error if you select AWS anyway.

Which one you pick is **Settings → Integrations → Workload Credentials →
Identity platform**. Everything after that is the same shape: register the trust
once, name the audience, and hold nothing.

**Registration is a GUI action and there is no API for it.** Pathfinder →
**Administration → Workload Identities → Register Workload Identity**. That is
deliberate on both sides: a trust that can be created by the thing being trusted
is not a trust. It takes a few minutes, once.

Pathfinder registers three kinds of issuer, and the third is the general case:

| Identity Provider Type | What it is for |
|---|---|
| **GitHub Actions** | a workflow pulling secrets in CI — pinned to `owner/repo` and, optionally, immutable org/repo **IDs** so a renamed or recreated repo stops matching |
| **Azure Entra ID** | an Entra identity whose tokens carry the **v2** issuer — convenient when it fits, and step 2 covers when it does not |
| **Custom IDP** | **any** OIDC issuer, scoped by explicit claim conditions: Okta, Ping, Keycloak, an EKS or GKE cluster's own issuer — or Entra itself |

**Custom IDP is the general mechanism rather than a fallback.** It takes an
issuer URL and a set of claim conditions, so anything that mints a signed OIDC
token carrying a stable claim to pin can be trusted. The walkthrough below uses
it against Entra because that is the host being documented; the same form with
`https://<your-org>.okta.com/oauth2/default` or a GKE cluster's issuer URL works
identically.

**The token source is the dashboard's side of this.** The trust is
IdP-agnostic; the dashboard reads its token from the platform you pick in the
table above. It cannot sign in to Okta or Ping on its own behalf. Where no
platform hands the container a token (ECS, plain EC2, an on-prem VM), the
dashboard's own SPIFFE identity can: its SPIRE server mints the token, and a
Custom IDP registration for the dashboard's issuer trusts it.

## 1. One user-assigned identity, on both container apps

Use a **user-assigned** identity rather than a system-assigned one. The app and
the worker are two separate Container Apps, so system-assigned means two
principals, two object ids, and two registrations in Pathfinder to keep in step.
And **the worker is where credentials are actually minted** — an identity on
`dash` alone gives you a panel that tests green and jobs that still fail.

```bash
az identity create -g RG-DASH -n id-dash-wlc
ID=$(az identity show -g RG-DASH -n id-dash-wlc --query id -o tsv)
az containerapp identity assign -n dash        -g RG-DASH --user-assigned "$ID"
az containerapp identity assign -n dash-worker -g RG-DASH --user-assigned "$ID"
```

Then read back the two values Pathfinder and the settings panel each need:

```bash
az identity show -g RG-DASH -n id-dash-wlc \
  --query '{principalId:principalId, clientId:clientId, tenantId:tenantId}' -o json
```

`principalId` is the **service principal object ID** — the value Pathfinder asks
for, and the one that ends up as the `sub` claim it checks. `clientId` is what
the dashboard sends when asking the platform for a token; without it the request
resolves to the *system-assigned* identity, which here is not assigned at all.

## 2. An audience for the token — usually without registering anything

A managed identity does not mint a token in the abstract — it mints one **for a
resource**, and that resource becomes the token's `aud`. You need a value to put
there. You do not necessarily need to create one.

**Start here, because it needs no directory permissions at all:** borrow a
first-party resource that already exists in every tenant, and read the two values
step 3 wants straight off a real token.

```bash
az account get-access-token --resource https://management.azure.com/ --query accessToken -o tsv \
  | python3 -c "import base64,json,sys;p=sys.stdin.read().strip().split('.')[1];p+='='*(-len(p)%4);d=json.loads(base64.urlsafe_b64decode(p));print('iss:',d['iss']);print('aud:',d['aud'])"
```

```
iss: https://sts.windows.net/<tenant-id>/
aud: https://management.azure.com/
```

Use **Azure Resource Manager** rather than Microsoft Graph as the borrowed
audience. Graph tokens can carry a nonce in the JWT header that makes the
signature unverifiable by anything except Graph — and Pathfinder has to verify
it — whereas ARM tokens are ordinary JWTs and ARM's service principal exists in
every tenant. What makes borrowing safe is not the audience but the identity:
the one from step 1 holds **no role assignments anywhere**, so a token minted
for it authorises nothing.

The trade-off is the issuer, and it settles step 3 for you. A first-party
resource issues a **v1** token — `iss = https://sts.windows.net/<tenant-id>/` —
so the registration has to be **Custom IDP**. The *Azure Entra ID* type derives
the issuer as `https://login.microsoftonline.com/<tenant-id>/v2.0` and will
never match a v1 token, with nothing in either UI to say why.

**If you would rather own the audience** — this needs directory permissions —
register an application purely to be that resource, and you can then use the
*Azure Entra ID* type:

```bash
APP_ID=$(az ad app create --display-name vm-dashboard-wlc \
           --identifier-uris api://vm-dashboard-wlc --query appId -o tsv)
az ad app update --id "$APP_ID" --set api.requestedAccessTokenVersion=2
az ad sp create --id "$APP_ID"      # so the tenant can resolve api://…
```

`requestedAccessTokenVersion: 2` is the entire reason to bother: it is what moves
the issuer to the v2 form. In the portal's manifest the same property reads
`accessTokenAcceptedVersion` — one knob, two names. Leave it at the default and
the identity produces a perfectly valid token the *Azure Entra ID* type refuses.
If the tenant rejects `api://vm-dashboard-wlc`, `api://<the new appId>` always
resolves.

Many tenants do not permit this. If `az ad app create` returns *"Directory
permission is needed for the current user to register the application …
Insufficient privileges to complete the operation"*, nothing is misconfigured —
app registration is restricted to directory roles, and the borrowed-audience
route above is the way through. The minimum role that unblocks it is
**Application Developer**, if you want this path specifically.

## 3. Register the trust in Pathfinder

**Administration → Workload Identities → Register Workload Identity**:

| Field | Value |
|---|---|
| Identity Provider Type | **Custom IDP** — or **Azure Entra ID** only if you own the audience app and set it to v2 |
| Service Name | any name; the dashboard sends it back as `X-BT-Service-Name` |
| Issuer URL | the `iss` from step 2, verbatim, trailing slash included |
| Claim Conditions | `sub` = the identity's `principalId` **GUID** from step 1 |
| | `aud` = the `aud` from step 2 |
| Site | **the site whose Workload Credentials you use** |
| Product Scopes | **Secrets** |

Four of those rows carry a trap.

**`sub` takes the GUID, not the word.** `principalId` is the name of the Azure
property you read the value *from*. Typing the literal string gets you
`Claim not present in token`, because the form then goes looking for a claim
named `principalId`. And it is `principalId`, not `clientId`: both are GUIDs on
the same identity, and the mix-up is silent in both directions — `clientId` here
yields a valid token that Pathfinder declines, while `principalId` in the panel
field of step 4 fails at the Azure token fetch instead.

**Add the `aud` condition yourself.** The Custom IDP form has no audience field —
the *Token Resource (App ID URI)* input belongs to the *Azure Entra ID* type — so
without an explicit condition, **any** token that identity obtains for **any**
resource satisfies this trust. It costs one row.

**Product Scopes is a grant separate from Site.** The site says whose data; the
scopes say which product APIs. Missing or wrong, it is another flat 401.

**The wrong site fails every call** with `401 Access denied for this site`, which
reads like the site lacking the application rather than like a scoping mistake.

Before saving, use the **Validate a token** tab. It is free, and it is the only
place in this feature that names *which* claim failed. Paste the token from the
step 2 command; the correct result is a single failure:

```
✗ sub — present, but the values do not match
```

That is the all-clear. It means the issuer string, the signature check against
the provider's JWKS, the `aud` condition and every claim path passed, and the
only mismatch is the one a laptop cannot fix — that token's subject is *you*, not
the workload. Anything else failing is a real finding, and far easier to read
here than at runtime, where all of it collapses into one `HTTP 401`.

> **A live token is a credential.** The token from step 2 is a working Azure
> Resource Manager bearer token for **your own** user, valid about an hour, and
> pasting it here sends it to the platform. It is the quickest pre-flight, and
> the field exists to accept tokens — but if your account carries real Azure
> privileges, consider going straight to Test connection in step 4 instead. That
> uses the workload's own inert identity and proves strictly more.

**Settings → Integrations → Workload Credentials**:

- **Authentication** → *Workload identity (nothing stored)*
- **Identity platform** → *Azure — Container Apps, App Service, VM*
- **Service name** → the Service Name from step 3
- **Token audience** → the `aud` from step 2 — `https://management.azure.com/`, or your own `api://…` if you registered one
- **User-assigned identity client ID** → `clientId` from step 1

**Save, then press Test connection** — it tests the *saved* values. The test is
an unmetered `GET /session`, so it is free and safe to repeat, and it is the only
thing that separates the two failures cleanly:

| What you see | What it means |
|---|---|
| `no managed identity endpoint reachable` | no identity is assigned to **this** container app |
| a hang, then a connection error naming `169.254.169.254` | the same cause wearing a disguise: Azure injects `IDENTITY_ENDPOINT` only into replicas that have an identity, and the IMDS fallback is unreachable from a Container App, so the call times out instead of saying so. Assign the identity, then restart the revision |
| `could not get a managed identity token (HTTP 400/404)` | the identity exists but the tenant will not issue for that resource — check the token resource, that `az ad sp create` was run if it is your own `api://…` app, and that the client ID field holds `clientId` and not `principalId` |
| `Workload Credentials error (HTTP 401)` | the token was fine and Pathfinder declined it — wrong site, missing Product Scopes, a Service Name that does not match (including a trailing space, which is **not** trimmed), a `sub` that is not this identity's `principalId`, an `aud` condition that does not match the token resource, or the v1/v2 issuer mismatch from step 2. The **Validate a token** tab in step 3 separates these; this message does not |

Once it passes, clear `wlc_pat`: switching modes does not delete it, and a stored
token nobody uses is still a credential somebody has to answer for. The Workload
Lab's Cloud tab mints through the same client, so it stops depending on a stored
token at the same moment.

**What this does not remove.** `DATABASE_URL` and `JWT_SECRET_KEY` are still
platform secrets on both container apps, and they still have to match between
`dash` and `dash-worker`. This removes the BeyondTrust credential, not every
credential.
