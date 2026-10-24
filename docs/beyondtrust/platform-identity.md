# BeyondTrust Platform Identity

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want PRA, Password Safe, EPM-L, Entitle or Workload Credentials to authenticate without a stored token or client secret.

## What is it?

Pathfinder **Workload Identity** lets a workload authenticate to BeyondTrust product
APIs with its own short-lived OIDC token instead of a stored credential. The dashboard
presents its identity token and Pathfinder accepts it, because you registered a
**Workload Identity** that names the token's issuer and the claims it must carry.
The token comes from the cloud the dashboard runs on, or from the dashboard's own SPIFFE
identity.

The dashboard holds **one** such identity, the *BeyondTrust platform identity*, and any
of these products can use it:

| Product | Settings panel | Auth setting | Native mode it replaces |
|---|---|---|---|
| Workload Credentials | Workload Credentials | `wlc_auth_mode` | Personal Access Token |
| Privileged Remote Access | Privileged Remote Access | `pra_auth_mode` | OAuth client credentials |
| Password Safe | Password Safe | `ps_auth_mode` | OAuth client credentials |
| EPM for Linux | EPM for Linux | `epml_auth_mode` | Personal Access Token |
| Entitle | Entitle | `entitle_auth_mode` | API token |

Each product opts in separately and defaults to its native mode, so turning one on
changes nothing for the others.

> **Status.** Workload Credentials on workload identity is verified live. PRA, Password
> Safe, EPM-L and Entitle are wired, but have **not** been verified against a live tenant
> yet. That is why each product's **Workload API URL** is a setting rather than a
> constant: see [The workload API URL](#the-workload-api-url).

## What it does not cover

Workload identity covers the **dashboard's own REST calls**. These tools cannot present
a bearer JWT, so they keep using the stored credentials on each product's panel:

- the `beyondtrust/sra` Terraform provider (Shell Jump, Web Jump, RDP and tunnel
  provisioning);
- the `BeyondTrust/passwordsafe` Terraform provider (VM managed-system onboarding);
- the `entitleio/entitle` Terraform provider (resource registration);
- `ps-cli`, the Secrets Safe backend and the Ansible runner's Password Safe lookups.

So switching PRA to workload identity removes nothing from its panel. The client
credentials must stay set for everything Terraform does. Treat the mode as removing the
credential from the REST path, not from the install.

**Customer tenants are out of scope.** A POV's PRA, Password Safe or Entitle tenant
(from the POV tenant registry) is a different site with a different trust. It always
uses its own client credentials.

## Setup

### 1. Configure the identity

Open **Settings → Integrations → BeyondTrust → BeyondTrust platform identity**:

| Field | What it is |
|---|---|
| Pathfinder site ID | Fills `{site}` in each product's workload API URL. Open `https://app.beyondtrust.io/api/platform/currentSite` while signed in and copy `site_id`. Blank falls back to the Workload Credentials site, then the EPM-L site. |
| Identity platform | Where the token comes from: Azure managed identity, the GCP metadata server, an AWS EKS projected token, any projected token file, or the dashboard's own SPIFFE identity. |
| Service name | The registration's **Service Name**. It is sent as `X-BT-Service-Name` on every call. |
| Token audience / token file / client ID | Depends on the platform, as described in the panel. |

These are the same settings Workload Credentials used. They keep their `wlc_` names, so
an install that already set up Workload Credentials on workload identity is already
configured. They moved to their own panel because they are no longer specific to
Workload Credentials.

Press **Test identity**. It fetches a token and shows its `iss`, `aud`, `sub` and `exp`.
It is free and calls no BeyondTrust endpoint, so a pass proves that this container has an
identity, not that Pathfinder accepts it.

The per-platform setup is the same as for Workload Credentials:
[Azure Container Apps](../platform/cloud-hosting/azure-container-apps.md#no-pat-authenticate-to-pathfinder-with-a-workload-identity)
covers the managed identity and the Custom IDP registration in detail, and
[the dashboard's own identity](../platform/oidc/dashboard-identity.md#beyondtrust-workload-credentials)
covers the SPIFFE route.

### 2. Register it once, with every Product Scope

In Pathfinder, open **Administration → Workload Identities**. Register one identity,
or edit the one you already have for Workload Credentials, and tick a **Product Scope**
for each product you will switch over.

**Product Scopes is a separate grant from Site.** A product left out of the scopes
answers every call with a flat 401, and nothing in that response says the scope is the
reason.

You can also register **one identity per product**: same issuer and claims, different
Service Names. Each product panel has an optional **Service name** override for that.

### 3. Switch a product over

On the product's own panel, set **Authentication → Workload identity** and save. Start
with **EPM for Linux**. It is the one already on the Pathfinder gateway, so it isolates
the identity from the URL question.

## The workload API URL

PRA, Password Safe and Entitle each have a **Workload API URL**, a template in which
`{site}` is replaced with the site ID. The defaults are best guesses at the Pathfinder
gateway path:

| Product | Default | What is appended |
|---|---|---|
| PRA | `https://api.beyondtrust.io/site/{site}/pra` | `/api/config/v1/...` |
| Password Safe | `https://api.beyondtrust.io/site/{site}/passwordsafe` | `/BeyondTrust/api/public/v3/...` |
| Entitle | `https://api.beyondtrust.io/site/{site}/entitle` | `/public/v1/...` |

EPM-L has no such field. It already calls `https://api.beyondtrust.io/site/<site>/epm/linux`,
which is verified.

If a product answers **404**, or an empty **421** (the gateway's "no such path"), the URL
is what needs fixing, not the identity. The product's own host can also go here, for
example `https://tenant.beyondtrustcloud.com` for PRA, if that is where the platform
token is accepted.

**Password Safe sign-in.** The dashboard skips the OAuth token request on workload
identity but still calls `Auth/SignAppIn`, because a Password Safe API session normally
needs it. If the gateway answers that call with 404 or 405, the bearer token is taken as
the whole session, and the dashboard logs this once.

## Troubleshooting

Pathfinder folds most mismatches into one flat 401. Work through them in this order:

| Symptom | Cause |
|---|---|
| `could not get a workload identity token …` | The container has no usable identity. **Test identity** fails the same way. The message names the platform's likely cause. |
| `… missing: wlc_service_name, …` | The platform identity panel is incomplete. |
| `401 Access denied for this site` | The registration, or the site in the URL, is on the wrong site. |
| 401 naming Product Scopes / Service Name | The product is not in the registration's **Product Scopes**, or the Service Name does not match. A trailing space is stripped for these products but not by older versions. |
| 401 right after a token change | The issuer or a claim condition does not match the token. Paste the claims from **Test identity** into Pathfinder's **Validate a token** tab. It names the failing claim. |
| 404 / empty 421 | The **Workload API URL** is wrong for this product. See above. |

Going back is a one-field change. Set **Authentication** back to the native mode. The
stored credentials were never cleared.

## Related

- [Workload Credentials](../preview/workload-lab/workload-credentials.md)
- [Privileged Remote Access](privileged-remote-access.md) ·
  [Password Safe](password-safe.md) · [EPM-L](epml.md) · [Entitle](entitle.md)
