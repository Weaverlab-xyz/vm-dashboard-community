# BeyondTrust Workload Credentials

> **Preview — the product is pre-GA.** Workload Credentials reaches general
> availability in late 2026. It is gated behind a **preview flag**
> (Settings → Preview features) and is off by default. 
> 

Workload Credentials (WC; codename *SMoP*, "Secrets Manager on Platform") is
BeyondTrust's cloud-native secrets product. It does two things the dashboard
cares about:

- **Static secrets** — a versioned key/value store with folders, usable as a
  secrets backend alongside AWS Secrets Manager, Azure Key Vault, GCP Secret
  Manager and BeyondTrust Secrets Safe. Free and unmetered.
- **Dynamic secrets** — mints **short-lived** AWS and Azure credentials on
  demand, so the dashboard stops holding a standing cloud secret at all.

---

## What is implemented today

| Capability | Status |
|---|---|
| Static secrets as a `wlc://` secrets backend (list / read / create / update / delete, staleness metadata) | **Implemented, verified live** |
| Dynamic AWS credentials for the dashboard's own cloud calls | **Implemented** — not yet exercised against a live dynamic secret |
| Splitting AWS into an everyday and a provisioning lease | **Implemented** — opt-in, not yet exercised live |
| Dynamic Azure credentials | **Implemented** — not yet exercised against a live dynamic secret |
| In-cluster workload identity (a pod federating its ServiceAccount token) | Planned |

The static-secret backend was deliberately first: it exercises the site, token
and API version end to end **without incurring a metered credential issuance**,
so a misconfiguration surfaces before anything bills.

## How the AWS lease behaves

Worth understanding before you enable it, because the behaviour is shaped almost
entirely by the fact that issuances are billed.

- **One lease serves the whole deployment.** It is stored in the database, not
  per-process, so the two web workers and the job worker share it. A per-process
  cache would mint three credentials for one purpose and bill for three.
- **It is renewed in the background**, at startup and on the job worker's
  existing 60-second tick, once less than `wlc_refresh_margin_pct` of its
  lifetime remains. A request only ever mints synchronously on a genuinely cold
  start.
- **A failure never clears the working credential.** A failed mint records the
  error and backs off; the last good lease keeps serving until it actually
  expires.
- **A failing configuration backs off** rather than retrying per request — a
  wrong dynamic-secret name would otherwise turn every page load into another
  billable attempt.
- **The previous lease is released on renewal.** AWS refuses early revocation and
  that is expected; the call is made anyway so the Azure path (which does accept
  it) needs no separate handling.

### The optional two-lease split

Set an **everyday** dynamic secret (`wlc_aws_readonly_secret_name`) and the dashboard
uses two leases instead of one:

- **Everyday** — everything outside a job. Warmed at startup and renewed in the
  background.
- **Provisioning** — minted when a job starts, **never pre-warmed**, allowed to expire.

The payoff is that `iam:PassRole` and `iam:CreateRole` are absent from the stored
credential between jobs, so a credential lifted from the lease row at an arbitrary
moment cannot escalate. Leaving the setting blank keeps one lease for everything, which
is the default and changes nothing.

> **The everyday policy is not literally read-only**, despite the setting name — which
> shipped before this was understood, and is now inaccurate rather than wrong enough to
> justify a config migration. The dashboard writes on the request path as well as inside
> jobs: editing a secret (`secretsmanager:*`), uploading to storage (`s3:PutObject`),
> re-tagging a VM (`ec2:CreateTags`), starting or stopping a container task
> (`ecs:RunTask` / `StopTask`). Roughly fourteen such sites. The everyday policy needs
> those and only needs to **exclude IAM**. A strictly read-only policy breaks the Secrets
> and Storage pages.

Both secrets can point at the **same role** and differ only by inline session policy —
session policies intersect, never broaden — so the split costs a few lines of HCL rather
than a second role and trust policy.

One thing it deliberately does not do: it does not revoke the provisioning lease when the
job ends. AWS refuses early revocation, so that credential lives to its TTL regardless;
dropping the row early would only hide it from the dashboard, at the cost of a fresh
billable issuance per job rather than per TTL window.

### How Azure differs

Same lease machinery, three differences worth knowing before you enable it:

- **The lease does not carry a subscription.** Workload Credentials mints a password
  onto an app registration; it has no idea which subscription the dashboard targets, so
  `azure_subscription_id` still comes from configuration. A lease without it is refused
  rather than used, because a credential that authenticates and then acts on nothing
  fails a long way from the cause.
- **Azure leases *are* revocable**, unlike AWS. Each renewal releases the previous one,
  which matters here in a way it does not for AWS: every mint adds a
  `passwordCredential` to the target app registration, and app registrations cap them.
  Skip the release and you eventually cannot mint at all.
- **TTL is 1–24 hours** (AWS is 15 minutes to 12 hours, further capped by the role's
  `MaxSessionDuration`).

The in-process credential cache is keyed on the credential **material**, so a re-minted
or rotated secret is picked up on the next call. It previously rebuilt only when the
setup wizard explicitly invalidated it, which was survivable while every source was
long-lived and wrong for one that expires — the process would have pinned a dead secret
until restart, and that invalidation is process-local so a sibling worker would have kept
its own dead copy anyway.

If AWS is on the dynamic tier and no lease can be issued, cloud calls **fail**
rather than falling back to a static key. That is deliberate: an operator who has
retired their static key should not silently get some other credential from the
environment.

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

The dashboard still needs one long-lived credential — the WC **personal access
token** — to call the API. So the honest claim is not "no static secrets":

> Three standing cloud credentials carrying `ec2:*` / `Contributor` /
> `Compute Admin` collapse into **one platform PAT**, and the cloud credentials
> themselves become short-lived, per-lease, and auditable.

The in-cluster path is the one that can reach genuinely zero standing
credentials, because a pod federates its own ServiceAccount token rather than
presenting a stored one.

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
   later.

   > **Switch to the target site *before* creating the token.** A PAT is scoped
   > to whichever site was selected when you minted it, and there is currently no
   > way to widen it (multi-site PATs are still in flight). It is easy to mint one
   > while the Pathfinder admin tenant is selected rather than the site you
   > actually want — and the resulting failure does not look like a scoping
   > problem. See [Troubleshooting](#troubleshooting).

---

## Provisioning the BeyondTrust side

[`terraform/workload_credentials/`](../../terraform/workload_credentials/) creates the
integration, the dynamic secrets, and the AWS IAM trust chain in one apply, and outputs
the exact values to paste into the settings panel:

```bash
export BEYONDTRUST_ACCESS_TOKEN='<PAT minted with the target site selected>'
export BEYONDTRUST_SITE_ID='<site GUID>'
terraform -chdir=terraform/workload_credentials init
terraform -chdir=terraform/workload_credentials apply
```

Worth using rather than the console, because four of the details fail in ways that do not
name the field: the external ID (a required input, not a value the service hands back),
`sts:TagSession` on both trust policies, `MaxSessionDuration` on the target role, and — on
Azure — an app **Object** ID that is easy to confuse with a client ID.

The module deliberately does **not** contain a copy of the dashboard's IAM policy or the
Azure role assignments. `setup-aws.sh` and `setup-azure.sh` own those; a second definition
would drift from them. See the module's README.

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
`Invalid access token` / plain `401 Unauthorized` instead, so the wording does
distinguish the two cases once you know to look.

**A TLS or certificate error rather than an HTTP status** — that is your own
egress path, not the API. Behind a TLS-inspecting corporate proxy you need the
corporate root CA in the trust store of whatever is making the call (including
the dashboard container — see the `--corp-ca` overlay).

---

## Permissions

The dashboard needs a broad set of cloud permissions because it provisions VMs,
databases, clusters, functions, images and container runners. When you move a
cloud to the dynamic tier, the credential WC mints must carry the same set the
static credential carries today.

The canonical, always-current lists are the sandbox bootstrap scripts —
`scripts/sandbox/Linux/setup-aws.sh` (the `dashboard-app-policy` IAM policy) and
`scripts/sandbox/Linux/setup-azure.sh` (the service-principal grants), plus their
PowerShell twins. Treat those as the source of truth; the summaries below explain
the parts that are easy to get wrong.

### AWS — the role Workload Credentials assumes

Three roles chain together:

```
BeyondTrust bridge role
   └─ assumes → your integration role   (trust + sts:ExternalId condition)
        └─ assumes → your target role   (carries the dashboard's permissions)
```

The integration role's trust policy names the BeyondTrust bridge principal and
requires the **external ID** that WC generates when you create the integration.
The target role trusts the integration role.

Two details that fail silently:

- **The target role's trust policy needs `sts:TagSession` as well as
  `sts:AssumeRole`.** The dynamic secret's `aws_tags` become STS **session
  tags**, which is what gives you CloudTrail attribution per issuing secret.
  Without `sts:TagSession`, tagged issuance fails.
- **`MaxSessionDuration` caps the TTL and defaults to one hour.** The dynamic
  secret can ask for up to 12 hours, but STS will not exceed the role's own
  limit — and it does not warn, it just clamps. Set it explicitly.

Permission notes beyond the canonical policy:

| Area | Note |
|---|---|
| `iam:PassRole` | Needed for instance profiles, ECS task/execution roles, EKS cluster and node roles, Lambda execution roles and the VM import/export role. Scope it to those name patterns with an `iam:PassedToService` condition rather than a broad suffix wildcard. |
| `iam:CreateRole` / `iam:AttachRolePolicy` | Terraform creates EKS, Lambda and SSM roles. Constrain with an `iam:PermissionsBoundary` condition and an `iam:PolicyARN` allow-list, or the credential can grant itself administrator. |
| `secretsmanager:ListSecrets` | Cannot be resource-scoped, so the credential can **enumerate every secret name** in the account even though it reads values only under the configured prefix. |
| `ce:GetCostAndUsage` | Cost Explorer is account-global and effectively `us-east-1`. Any `aws:RequestedRegion` condition must allow it or the Cost page breaks. |
| `AWSServiceRoleForRDS` | Must be pre-created with privileged credentials. It is a **setup step**, not a permission the role can grant itself — otherwise the first database provision fails. |

### Azure — the app registration credentials are minted onto

WC adds a temporary password to a **pre-existing app registration**, so there are
two distinct Azure AD objects:

1. **The integration app** — WC authenticates as this. Its client secret is what
   you give the WC integration.
2. **The target app** — credentials are minted onto this. It is identified by its
   **Object ID, not its Application (client) ID**. This is the most common
   mistake in Azure setup.

Grant the integration app permission to manage passwords on the target app.
Prefer **ownership**, which needs no tenant-wide Graph permission at all:

```bash
az ad app owner add --id "<target-app-object-id>" --owner-object-id "<integration-sp-object-id>"
```

This is CLI-only — the Portal's owners picker accepts users, not service
principals. The broader alternative is the `Application.ReadWrite.All` Graph
application role, which needs admin consent and a few minutes to propagate.

The **target app's own** Azure RBAC is what the dashboard runs with:

| Grant | Scope | Why |
|---|---|---|
| `Contributor` — or `Virtual Machine Contributor` + `Network Contributor` + `Storage Account Contributor` | resource group | VM, VNet, NIC and public-IP lifecycle |
| **`Storage Blob Data Contributor`** | storage account | **Data plane. `Contributor` does not grant it**, and the Terraform `azurerm` state backend authenticates with Entra, so state operations fail without it. |
| **`Key Vault Secrets Officer`** (RBAC vaults) or the access policy `get list set delete` (policy-based vaults) | the vault | `Contributor` does not grant secret data-plane access either. Which one you need depends on the vault's authorization mode. |
| `AcrPull` | the registry | container runners pulling images |
| `Cost Management Reader` | **subscription** | the Cost Management query is subscription-rooted; without it the Cost page reports Azure as unavailable |
| `User Access Administrator` | resource group | **only** for AKS and Cloud Functions, which create role assignments during provisioning. Omit it entirely otherwise. |

> **One undocumented subscription-scope requirement.** The pre-deploy quota check
> reads VM SKU and usage data at **subscription** scope and raises on failure, and
> it runs before every VM deploy. A resource-group-scoped-only principal will fail
> *every* VM deploy. Grant subscription `Reader`, or expect that.

**A limitation worth knowing:** WC mints onto a pre-existing app registration, and
each distinct permission set needs its own registration. The dashboard needs a
broad union, so on Azure you get one app carrying that union — per-operation least
privilege is not available on this path the way it is on AWS, where a dynamic
secret can narrow a shared role with an inline session policy.

---

## Costs and lease behaviour

**Dynamic credential issuances are metered.** Static secrets are not.

That single fact drives how the dashboard uses the API: it holds **one lease per
cloud and purpose** in the database, shared across every worker process, and
regenerates only when the lease is close to expiry. It never calls `generate`
per request. If you are sizing this, count credential *issuances*, not API
calls.

Practical consequences:

- **Prefer a TTL of an hour or more** on the dynamic secret. AWS credentials
  cannot be revoked early anyway, so a short TTL buys no security and multiplies
  the issuance count — a 15-minute TTL is roughly 70,000 issuances a year for one
  cloud.
- **There is no renew endpoint.** At expiry the dashboard generates a fresh
  lease; leases are not extended.
- **AWS leases cannot be revoked** (`400 lease_not_revocable`); Azure leases can,
  and the dashboard releases the previous Azure lease on refresh so passwords do
  not accumulate on the target app registration.

---

## Reference

| | |
|---|---|
| API base | `https://api.beyondtrust.io/site/<site-id>/secrets` |
| Auth | `Authorization: Bearer <PAT>` |
| Required header | `bt-secrets-api-version: 2026-04-28` |
| Terraform provider | `beyondtrust/beyondtrust` (registry), Terraform ≥ 1.11 |
| Provider env vars | `BEYONDTRUST_ACCESS_TOKEN`, `BEYONDTRUST_SITE_ID` |

The Terraform provider manages folders, static secrets, AWS and Azure
integrations, AWS and Azure dynamic secrets, and workload-identity (OIDC issuer
trust) registrations — so the whole configuration side can be provisioned as
code rather than clicked through the console.

Related: [Secrets management](../secrets-management.md) ·
[Machine-identity JIT design](../design/cloud-identity-jit.md) ·
[Password Safe](password-safe.md) · [Entitle](entitle.md)
