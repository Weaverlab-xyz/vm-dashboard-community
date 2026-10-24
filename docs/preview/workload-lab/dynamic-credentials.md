# Dynamic AWS and Azure credentials

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are moving a cloud off its standing access key, and need the lease behaviour, the IAM chain and what an issuance costs before you turn it on.

> **Dynamic AWS is verified live; dynamic Azure is not yet.** AWS leases have been
> minted and used against a live dynamic secret. Azure is implemented, but its lease
> mechanics below are read off the implementation and the API contract, not off a
> run. Workload Credentials itself is
> generally available and has its own toggle under Settings → Integrations → BeyondTrust;
> it is off by default, and each cloud is opted in separately.

The metered half of [Workload Credentials](../../beyondtrust/workload-credentials.md). Instead of the
dashboard holding a standing AWS access key or Azure client secret, WC mints one per
lease, hands it over for a bounded window, and takes it back. **Every issuance on this
page bills** — which is the argument for reading it before enabling it, and the reason the
static-secret backend was deliberately shipped first.

Read [Workload Credentials](../../beyondtrust/workload-credentials.md) for the product itself, the
prerequisites, and how the dashboard authenticates to it. None of the dynamic tier works
until that part does.

---

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
billable issuance per job rather than per TTL window. That is why the provisioning secret
has a shorter TTL than the everyday one (30 minutes against an hour in the
[Terraform module](../../../terraform/workload_credentials/README.md#the-two-ttls)): its TTL
is how long it outlives the job.

Nothing on AWS is handed a fixed copy of the credential, so a job can outlast its lease.
The dashboard's own calls go through one client factory (`aws_service.aws_client`) whose
clients carry botocore refreshable credentials: a few minutes before each lease expires,
the next call fetches the lease again, so a client created at the start of a deploy and
left in a ten-minute waiter keeps working. The purpose is fixed when the client is made.

Terraform and Packer runs can last longer than that too, so on AWS they are not handed keys.
They get an AWS profile whose `credential_process` is the dashboard itself
(`web_dashboard/wlc_credential_process.py`), and the AWS SDK in the provider, the S3 state
backend and Packer's Amazon plugin runs it again a few minutes before each credential
expires. The profile file holds the command, never a credential. The purpose is fixed
when the job writes it, so a deploy keeps the provisioning lease all the way through.

Turn `wlc_subprocess_credential_process` off to hand them fixed keys instead. Those are
handed over only while at least `wlc_subprocess_min_remaining_pct` (80% by default) of the
lease remains, otherwise the dashboard issues a fresh one first, and a run longer than
what is left fails on an expired credential. Azure has no equivalent: an Azure run gets
the service principal's secret, which outlives any one run at the Azure TTL floor of an
hour.

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

#### The everyday credential from the SPIFFE identity

This works for **AWS and Azure**. On AWS it replaces the everyday dynamic secret; on Azure,
which has one dynamic secret, it avoids the second target app registration a lease split
would need. Set `wlc_aws_everyday_source` or `wlc_azure_everyday_source` to `federation`
(Settings → Workload Credentials → *everyday credential*). On AWS the everyday calls then
assume the role in `aws_federation_role_arn`; apply
[`terraform/dashboard_federation/aws`](../../../terraform/dashboard_federation/README.md) with
`everyday_only = true` so that role carries the same deny on creating and passing roles as
the everyday secret. On Azure:

- **Pages and API calls** run as the app in `azure_client_id`, through the federated
  credential that trusts the dashboard's [SPIFFE identity](../../design/dashboard-workload-identity.md).
  No secret, and no lease.
- **Jobs** (inside the provisioning scope) mint the Workload Credentials password, which
  keeps the role-assignment rights. Nothing pre-mints it, so between jobs there is no live
  privileged password at all.
- **It fails closed.** If federation is not usable outside a job (the SPIFFE identity is
  off, the Azure audience is not ticked, the token file is missing, or a stored
  `azure_client_secret` is still set), Azure calls fail with that reason. They never fall
  back to the lease, which would hand the request path the privileged credential.

**Routine read-only jobs keep the everyday credential.** Every job runs in the provisioning
scope except `jobs_worker.EVERYDAY_JOB_TYPES`, today just `pov_env_reconcile`: the POV
instance's ten-minute read-back. In the provisioning scope it would keep the billed,
write-capable lease alive around the clock, re-minted every TTL, with nobody building
anything. A job type may join that set only if nothing it reaches writes to a cloud.

Grant the two identities differently. `scripts/wlc/setup-azure-apps.sh --everyday-sp
<azure_client_id>` copies the reference service principal's roles onto the federated app
**except** any role that can write role assignments: Owner, User Access Administrator, Role
Based Access Control Administrator, and custom roles whose actions reach
`Microsoft.Authorization/roleAssignments/write`. The target app keeps the full set.

One grant follows from the split. A job's Terraform runs as the provisioning app, and the
AKS module grants cluster admin to whoever runs the apply. Set
*Everyday identity's object id* (`azure_everyday_principal_id`, from
`az ad sp show --id <azure_client_id> --query id`) and new AKS clusters grant it the same
role, so the Kubernetes pages can reach them. Clusters built before the switch need that
assignment added by hand.

If AWS is on the dynamic tier and no lease can be issued, cloud calls **fail**
rather than falling back to a static key. That is deliberate: an operator who has
retired their static key should not silently get some other credential from the
environment.

---

## Provisioning the BeyondTrust side

[`terraform/workload_credentials/`](../../../terraform/workload_credentials/) creates the
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

## Permissions

The dashboard needs a broad set of cloud permissions because it provisions VMs,
databases, clusters, functions, images and container runners. When you move a
cloud to the dynamic tier — and it is
[a choice, not a migration](../../beyondtrust/workload-credentials.md#this-is-a-choice-not-a-migration) —
the credential WC mints must carry the same set the
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

`scripts/wlc/setup-azure-apps.sh` does all of this — both registrations, the
ownership, and the target app's Azure RBAC. Run it before the Terraform module:

```bash
scripts/wlc/setup-azure-apps.sh --reference-sp <the azure_client_id you use today>
```

It copies the role assignments from that reference principal rather than applying a
hardcoded list, so the target app ends up with exactly what your working service
principal has. That matters more than it sounds: the grants do **not** arrive on their
own — `setup-azure.sh` creates and grants its *own* principal, so a fresh app
registration starts with nothing and every call 403s.

The manual equivalent, if you would rather not run a script:

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

- **Prefer a TTL of an hour or more** on the everyday (or only) dynamic secret,
  which the dashboard keeps warm. AWS credentials cannot be revoked early anyway,
  so a short TTL there buys no security and multiplies the issuance count — a
  15-minute TTL is roughly 70,000 issuances a year for one cloud. The
  provisioning secret is the exception: it is minted per job rather than kept
  warm, so a shorter TTL (30 minutes by default) costs little and shortens how
  long a privileged credential outlives the job.
- **There is no renew endpoint.** At expiry the dashboard generates a fresh
  lease; leases are not extended.
- **AWS leases cannot be revoked** (`400 lease_not_revocable`); Azure leases can,
  and the dashboard releases the previous Azure lease on refresh so passwords do
  not accumulate on the target app registration.

---

## Related

* [Workload Credentials](../../beyondtrust/workload-credentials.md) — the product, the prerequisites, and how
  the dashboard authenticates to it.
* [Short-lived cloud credentials](cloud.md) — the Workload Lab tab that hands a lease to a
  workload rather than consuming one itself.
* [What consumes these credentials](consumers.md) — the play that spends a lease and then
  asserts the lease died.
* [Machine-identity JIT design](../../design/cloud-identity-jit.md) — why the dashboard's own
  credential posture is shaped this way.
