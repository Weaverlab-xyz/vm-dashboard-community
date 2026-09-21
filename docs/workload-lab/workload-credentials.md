# BeyondTrust Workload Credentials

> **Audience:** operator · **Profile:** `both` · **Read this when:** a workload needs a credential minted at run time rather than one stored for it.

> **Preview.** Workload Credentials is not yet generally available, and its API may
> still change. It is gated behind a **preview flag** (Settings → Preview features) and
> is off by default — nothing about your existing credentials changes until you turn it
> on. Check with BeyondTrust for availability and roadmap.

Workload Credentials (WC; codename *SMoP*, "Secrets Manager on Platform") is
BeyondTrust's cloud-native secrets product. It does two things the dashboard
cares about:

- **Static secrets** — a versioned key/value store with folders, usable as a
  secrets backend alongside AWS Secrets Manager, Azure Key Vault, GCP Secret
  Manager and BeyondTrust Secrets Safe. Free and unmetered.
- **Dynamic secrets** — mints **short-lived** AWS and Azure credentials on
  demand, so the dashboard stops holding a standing cloud secret at all.

The dynamic half — what a lease actually is, what it may do, and what an issuance
costs — is a page of its own:
[Dynamic AWS and Azure credentials](dynamic-credentials.md). Everything on this page is
free and unmetered; everything on that one bills.

---

## What is implemented today

| Capability | Status |
|---|---|
| Static secrets as a `wlc://` secrets backend (list / read / create / update / delete, staleness metadata) | **Implemented, verified live** |
| Migrating the database's own secrets into it in one step | **Implemented** — see [Emptying the database](#emptying-the-database-wc-as-the-secrets-backend); not yet run live |
| [Dynamic AWS credentials](dynamic-credentials.md) for the dashboard's own cloud calls | **Implemented** — not yet exercised against a live dynamic secret |
| [Splitting AWS into an everyday and a provisioning lease](dynamic-credentials.md#the-optional-two-lease-split) | **Implemented** — opt-in, not yet exercised live |
| [Dynamic Azure credentials](dynamic-credentials.md#how-azure-differs) | **Implemented** — not yet exercised against a live dynamic secret |
| Authenticating to WC with a **workload identity** instead of a stored PAT | **Implemented** — Azure run end to end 2026-09-15; GCP and AWS client paths unit-tested, not yet pointed at a live registration |
| In-cluster workload identity (a pod federating its ServiceAccount token) | **Implemented** — the `file` platform reads the projected token; not yet run live |

The static-secret backend was deliberately first: it exercises the site, token
and API version end to end **without incurring a metered credential issuance**,
so a misconfiguration surfaces before anything bills.

---

## This is a choice, not a migration

The dashboard has three credential postures, and they coexist. Each cloud
selects its own:

| Posture | What holds the cloud credential | Requires |
|---|---|---|
| **Static** (default) | encrypted `app_config`, or an external vault reference | nothing — no BeyondTrust licence |
| **Static + Entitle machine gate** | the same key, privilege elevated per operation | an Entitle tenant + agent |
| **Dynamic (WC)** | nothing standing — minted per lease | a Pathfinder site with WC enabled |

Turning WC on is what *unlocks* retiring **your** static credentials. It never
retires them for anyone else, and never as a side effect of an upgrade. See
[Secrets management](../secrets-management.md) for the wider tiered model.

**GCP is absent on purpose.** WC mints AWS and Azure credentials only, so a GCP
deployment stays on the static tier. A mixed install — say AWS dynamic, Azure
dynamic, GCP static — is the normal case, not a gap.

### Be clear about what this buys

The dashboard needs one long-lived credential — the WC **personal access
token** — to call the API, *unless* it runs as an Azure container and
authenticates with its own managed identity instead (see
[How the dashboard authenticates](#how-the-dashboard-authenticates)). On the PAT
path the honest claim is not "no static secrets":

> Three standing cloud credentials carrying `ec2:*` / `Contributor` /
> `Compute Admin` collapse into **one platform PAT**, and the cloud credentials
> themselves become short-lived, per-lease, and auditable.

The in-cluster path is the one that can reach genuinely zero standing
credentials, because a pod federates its own ServiceAccount token rather than
presenting a stored one.

On **any** workload-identity platform there is no PAT either, and that is what
makes it possible to move **every** remaining database secret into WC itself —
see [Emptying the database](#emptying-the-database-wc-as-the-secrets-backend).

---

## Prerequisites

1. **A US-region Pathfinder site with Workload Credentials enabled.** The
   feature can only be added to US-region sites. If your site does not have it,
   raise an IT Help ticket asking for the Workload Credentials application to be
   added, quoting your **Org ID** and **Site ID**.
2. **Your Site ID** — the `tenant_id` claim in the access token your browser
   holds after signing in to Pathfinder.
3. **A personal access token** — Pathfinder → **Manage Profile → Personal
   Access Tokens → Create Token**. Copy it immediately; it is not retrievable
   later. *(Not needed if the dashboard runs as an Azure container and will
   authenticate with a workload identity — see
   [How the dashboard authenticates](#how-the-dashboard-authenticates). A PAT is
   still the quickest way to prove the site works before switching.)*

   > **Switch to the target site *before* creating the token.** A PAT is scoped
   > to whichever site was selected when you minted it, and there is currently no
   > way to widen it (multi-site PATs are still in flight). It is easy to mint one
   > while the Pathfinder admin tenant is selected rather than the site you
   > actually want — and the resulting failure does not look like a scoping
   > problem. See [Troubleshooting](#troubleshooting).

---

## Setup

1. Settings → **Preview features** → enable **Workload Credentials
   (BeyondTrust)**.
2. Click **Configure** on that row and fill in the API base URL, Site ID and
   PAT. Leave **API version** at its default unless BeyondTrust tells you
   otherwise — it is sent as the mandatory `bt-secrets-api-version` header, and
   a wrong value fails in a way that reads like an authentication error.
3. Go to **Secrets** (`/secrets`), select **BeyondTrust Workload Credentials
   (preview)** as the backend and click **Test connection**. This calls
   `GET /session`, which validates the token without creating anything.
4. Create a secret through Browse & Edit to confirm write access.

The `wlc://` reference prefix then works anywhere the other vault prefixes do,
so an existing secret can be migrated to WC from the Secrets page.

> The PAT itself **cannot** be migrated into Workload Credentials — it is how
> the dashboard reaches WC in the first place, so storing it there would make
> the backend unreadable without itself. The migration UI refuses this
> explicitly.

---

## How the dashboard authenticates

Two modes, set on the settings panel. The second one exists because the first
leaves a credential behind.

| Mode | What it holds | Where it works |
|---|---|---|
| **Personal Access Token** (default) | the PAT, encrypted in `app_config` | anywhere |
| **Workload identity** | **nothing** | wherever the platform hands the container an OIDC token — see below |

In the second mode the container asks its own platform for a short-lived token
for its own identity, and Pathfinder accepts it because a **Workload Identity**
registered there names that identity's issuer and a constraint on its claims.

**It is not Azure-only.** `wlc_identity_platform` selects which platform is
asked, because [cloud-hosting.md](../cloud-hosting.md) documents this dashboard
running as a managed container on Azure Container Apps, GCP Cloud Run or AWS ECS:

| Platform | Token source | Note |
|---|---|---|
| `azure` | `IDENTITY_ENDPOINT`/`IDENTITY_HEADER`, else IMDS | a JSON envelope carrying `access_token` |
| `gcp` | the metadata server's instance identity endpoint | returns the JWT as **plain text** |
| `aws` | a projected token file — `AWS_WEB_IDENTITY_TOKEN_FILE` (IRSA) or `AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE` (Pod Identity) | **EKS only**, see below |
| `file` | any other projected token on disk | the Kubernetes ServiceAccount token every cluster mounts |

> **ECS cannot use this mode.** An ECS task and a plain EC2 instance get SigV4
> credentials and a signed instance identity document — neither is an OIDC token,
> and no endpoint there will issue one. Only EKS projects a real one. A dashboard
> on ECS stays on a stored PAT, and the error says so rather than reporting a
> missing file.

The older spelling `entra` for this mode still reads as `workload`, so an install
configured before it covered more than Azure keeps working untouched. The same
goes for `wlc_entra_resource`, which is read as the audience when
`wlc_identity_audience` is unset.
Every request then carries that token plus an `X-BT-Service-Name` header naming
the registration to evaluate it against. Nothing is stored, and there is nothing
to rotate.

**The registration is a manual, one-time action in Pathfinder's GUI**
(Administration → Workload Identities) and has no API in this dashboard by
design: something that could register its own trust would be holding a
credential that creates credentials. Pathfinder offers three issuer categories —
**GitHub Actions** (a CI workflow, pinned to `owner/repo` and optionally to
immutable org/repo IDs), **Azure Entra ID**, and **Custom IDP** (any OIDC issuer,
scoped by explicit AND-matched claim conditions). The dashboard wires the Azure
one, because the thing being authenticated is an Azure-hosted container; the
other two describe workloads that are not this process.

Full walkthrough, including the v1-versus-v2 issuer trap that makes a perfectly
valid token silently fail to match:
[Cloud hosting → No PAT](../cloud-hosting.md#no-pat-authenticate-to-pathfinder-with-a-workload-identity).

**Assign the identity to the worker too.** `dash-worker` is where credentials are
minted, so an identity on the web app alone yields a panel that tests green and
jobs that keep failing.

---

## Emptying the database: WC as the secrets backend

Workload Credentials is also a Tier 2 backend for ordinary static secrets
(`wlc://`), and on the workload-identity auth mode that combination is the one
configuration where the application database can end up holding **no secret
values at all** — only references.

Select **BeyondTrust Workload Credentials** under Target Backend on
Settings → Secrets Backend (`/secrets`) and run a **Dry Run** first. Each secret
is written to `<folder>/<key>` under the folder configured on this page, and its
database row is replaced with `wlc://<folder>/<key>`.

Which auth mode you are on decides whether that is *every* secret:

| Auth mode | What the database keeps afterwards |
|---|---|
| **Personal Access Token** | one row: `wlc_pat` itself. The migration refuses to move it, because the token that authenticates to WC cannot be stored inside WC |
| **Workload identity** (any platform) | nothing. Nothing reads `wlc_pat` on this mode, so it migrates like any other secret |

The dry run reports the PAT as a skipped **bootstrap credential** on the first
mode. That is the only secret held back, and switching auth modes is the way to
move it — not a limit of the backend.

Two things to know before you do it:

- **Switching back to PAT auth afterwards is refused.** Once `wlc_pat` holds a
  `wlc://` reference, stored-token auth would need the token in order to fetch
  the token. The settings panel rejects the change with that reason, and a
  request that somehow reaches the client is refused rather than reporting the
  PAT as missing — which is what the resolution loop would otherwise look like.
  Paste a real PAT into the panel in the same save if you genuinely want to go
  back.
- **The JWT root key is not part of this** and never can be — it derives the key
  that encrypts the very rows a vault credential lives in. See
  [why the JWT root key cannot be migrated](../secrets-management.md#why-the-jwt-root-key-cannot-be-migrated).

Secrets already living in another external vault are left untouched; the
migration only moves database-stored values.

---

## Verifying from the command line

Worth doing before configuring the dashboard: it isolates a credential problem
from a dashboard problem, and static operations are unmetered so it costs
nothing.

```bash
read -rsp 'WLC PAT: ' WLC_PAT; echo; export WLC_PAT
export WLC_SITE_ID='<your-site-guid>'
```

`GET /session` validates the token, the site and the API version in one call,
without creating anything:

```bash
curl -sS -o /dev/null -w 'session: %{http_code}\n' "https://api.beyondtrust.io/site/$WLC_SITE_ID/secrets/session" -H "Authorization: Bearer $WLC_PAT" -H "bt-secrets-api-version: 2026-04-28"
```

`200` means you are good to configure the dashboard. To also prove write access:

```bash
curl -sS -X POST "https://api.beyondtrust.io/site/$WLC_SITE_ID/secrets/static/wlc-probe?folder=dashboard" -H "Authorization: Bearer $WLC_PAT" -H "bt-secrets-api-version: 2026-04-28" -H 'Content-Type: application/json' -d '{"secret":{"username":"u","password":"p"}}'
```

`201` and an echoed `{"metadata": {...}, "secret": {...}}` is success. Clean up
with `DELETE` on the same path.

## Troubleshooting

**`401 Personal access token not found`**

A third 401 wording, distinct from the two below, and the most literal: the token
presented is not in the PAT store at all. It is not a scoping or authorization
problem, so nothing about the site, the folder or the dynamic-secret names will
fix it. Either the PAT was revoked or has aged out, or what reached the header is
not the PAT — a truncated paste is the usual culprit.

Confirm before changing anything, with either of these — they make the same
unmetered `GET /session` call:

- **Test connection**, on the Workload Credentials panel next to the API version.
  It uses the *saved* values, so save first. This is the fastest answer, and the
  only panel control that actually asks BeyondTrust — **Active credential source**
  further down replays the last recorded lease error out of the database, so it
  will keep showing a stale failure after the cause is fixed.
- The `curl` [above](#verifying-from-the-command-line), which proves the token
  independently of everything the dashboard adds.

Then re-mint under **Manage Profile → Personal Access Tokens** with the target
site selected and paste it into the panel. The token box is **always empty** when
the panel opens, whether or not one is stored — leave it blank to keep the stored
token, type to replace it, clear it to remove it. The placeholder is the only
thing that differs (`•••••••• stored — leave blank to keep`).

Observed live on 2026-08-21 against a correctly provisioned site, on every
`generate` for `dashboard/dashboard-everyday`, once a minute — with the folder,
the secret names and the API version all correct.

**`401` with `{"error":"Access denied for this site"}`**

The token authenticated but is not authorized for the site in the URL. In order
of likelihood:

1. **The PAT is scoped to a different site than the one you are calling.** This
   is by far the most common cause, and the wording is misleading — it reads like
   the site is missing the application. Minting a token while the *Pathfinder
   admin tenant* is selected, rather than the site itself, produces exactly this.
   Re-mint with the target site selected.
2. **The site does not have Workload Credentials provisioned.** Confirm by
   switching to it in Pathfinder and looking for the application tile. If there
   is no tile, raise the IT Help ticket in [Prerequisites](#prerequisites).
3. **Your account has no Workload Credentials role on that site.** Authentication
   and authorization are separate here; a valid token with no role assignment
   fails consistently rather than intermittently.

Note what this error is *not*: an invalid or expired token reports
`Invalid access token` / plain `401 Unauthorized`, and a token that does not exist
reports `Personal access token not found` (above). Three distinct 401s, and the
wording is the only thing that separates them — so read it rather than assuming
any 401 means the site.

**A TLS or certificate error rather than an HTTP status** — that is your own
egress path, not the API. Behind a TLS-inspecting corporate proxy you need the
corporate root CA in the trust store of whatever is making the call (including
the dashboard container — see the `--corp-ca` overlay).

---

## Reference

| | |
|---|---|
| API base | `https://api.beyondtrust.io/site/<site-id>/secrets` |
| Auth | `Authorization: Bearer <PAT>`, or a managed-identity token plus `X-BT-Service-Name: <registration>` |
| Required header | `bt-secrets-api-version: 2026-04-28` |
| Terraform provider | `beyondtrust/beyondtrust` (registry), Terraform ≥ 1.11 |
| Provider env vars | `BEYONDTRUST_ACCESS_TOKEN`, `BEYONDTRUST_SITE_ID` |

The Terraform provider manages folders, static secrets, AWS and Azure
integrations, AWS and Azure dynamic secrets, and workload-identity (OIDC issuer
trust) registrations — so the whole configuration side can be provisioned as
code rather than clicked through the console.

Related: [Dynamic AWS and Azure credentials](dynamic-credentials.md) ·
[Secrets management](../secrets-management.md) ·
[Machine-identity JIT design](../design/cloud-identity-jit.md) ·
[Password Safe](../integrations/password-safe.md) · [Entitle](../integrations/entitle.md)
