# Cloud Functions (preview)

> **Preview feature.** Enable it in **Settings → Preview features → Cloud Functions**,
> then configure a package store in **Settings → Integrations → Cloud Functions**.
> Design notes: [docs/design/cloud-functions.md](../design/cloud-functions.md).

## What is it?

One handler, three clouds. You write (or pick) a *workload* — a single Python
function — and the dashboard deploys it as an **AWS Lambda**, an **Azure Linux
Function App**, or a **GCP Cloud Run function**, with the same behaviour on each.
Every function gets a stable HTTPS endpoint protected by a shared bearer secret,
and can optionally be attached to a VPC/VNet so it can reach private resources.

The handler source lives in the repo at `web_dashboard/functions/` and ships inside
the dashboard image, so there is no build pipeline to run and no artifact to
publish yourself.

## Use cases

| | |
|---|---|
| **Diagnose serverless networking** | `echo_diag` reports what the function can actually reach — resolving DNS and TCP separately, so "broken" becomes "DNS" or "security group" |
| **Certificate expiry watch** | `cred_expiry_watch` runs a TLS handshake sweep *from inside the VPC*, so it sees internal-only endpoints |
| **An endpoint for external systems** | Something outside your network needs to trigger an action inside it, synchronously — the case a one-shot container cannot serve |
| **Entitle REST integrations (Phase 2)** | Entitle POSTs Give/Revoke Access to the function, which performs the grant against a private target |

The dashboard already reaches private resources for its own work by running one-shot
containers inside the target cloud (ECS / ACI / Cloud Run jobs). **Reach is not what
this adds** — an always-on, externally callable endpoint is.

## Prerequisites

| Cloud | Needs |
|---|---|
| **AWS** | An S3 bucket for packages. The dashboard's IAM identity needs `s3:PutObject`, plus `lambda:*`, `iam:CreateRole`/`AttachRolePolicy` (for the execution role) and `logs:*` on the function's log group. |
| **Azure** | The dashboard's existing storage account (`storage_azure_account`) and resource group. The service principal needs `Microsoft.Web/*`, `Microsoft.Storage/storageAccounts/listKeys/action`, and `Microsoft.Web/sites/host/listKeys/action`. |
| **GCP** | A GCS bucket for sources, and a noticeably larger IAM surface: `roles/cloudfunctions.developer`, `roles/run.admin`, `roles/cloudbuild.builds.builder`, `roles/artifactregistry.writer`, `roles/storage.objectAdmin`, `roles/secretmanager.admin`, plus `roles/iam.serviceAccountUser` on the runtime service account. |
| All | Terraform in the image (it is, by default). |

> A missing GCP role surfaces as a 403 roughly 90 seconds into the build, *after* the
> plan succeeded. If a GCP deploy fails late, check the roles above first.

## Setup

1. **Settings → Preview features** → turn on **Cloud Functions**. The nav entry and
   `/api/functions` appear immediately; no restart.
2. **Settings → Integrations → Cloud Functions** → set the package store for each
   cloud you intend to use. A cloud without one is shown in the deploy form but
   blocked, with the reason.
3. *(Optional)* Fill in the **VPC / VNet attachment** section if you want functions
   that reach private resources. See [Networking](#networking).
4. Go to **Functions → Deploy function**, pick `echo_diag`, and deploy. Then use
   **Test invoke** to confirm it works.

### Field reference

| Field | Notes |
|---|---|
| `function_package_s3_bucket` | AWS package bucket |
| `function_package_gcs_bucket` | GCP source bucket |
| `function_package_azure_container` | Blob container on the dashboard's storage account (default `function-packages`) |
| `azure_functions_plan_sku` | App Service plan SKU, default `B1` |
| `aws_functions_subnet_ids` / `aws_functions_security_group_ids` | Comma-separated; used when a function is deployed in `vpc` mode |
| `azure_functions_subnet_id` | Must be delegated to `Microsoft.Web/serverFarms` |
| `gcp_functions_vpc_connector` | An **existing** Serverless VPC Access connector |

## What it enables in the dashboard

| Feature | Where |
|---|---|
| Deploy / list / destroy functions | **Functions** page, `/api/functions` |
| Workload catalog | `GET /api/functions/workloads` |
| Test invoke with credentials attached | **Test invoke** button, `POST /api/functions/{id}/invoke` |
| Endpoint + secret for an external caller | **Endpoint** button (admin only), `GET /api/functions/{id}/invoke-info` |
| Progress + logs for a deploy | **Jobs** page (`cloudfn_deploy` / `cloudfn_decommission`) |

## Workloads

Each is one module in `web_dashboard/functions/fnworkloads/`. **The filesystem is the
catalog** — adding a module makes it deployable, no registry to edit.

| Workload | What it does |
|---|---|
| `echo_diag` | Echoes the request as the function saw it (redacted), reports where it landed, and DNS/TCP-probes any `{host, port}` you pass |
| `entitle_webhook_echo` | A **no-op Entitle Remote Adapter** — serves every route with valid empty responses and reports what arrived, without granting anything. Supports `?fail=401\|403\|404\|500\|slow\|timeout` |
| `db_grant` | **Entitle Remote Adapter** for ephemeral just-in-time database accounts on MySQL and SQL Server. **Dry run by default.** See below. |
| `portainer_access` | **Entitle Remote Adapter** for just-in-time Portainer access, granted through team membership. **Dry run by default.** See below. |
| `azure_role_grant` | **Entitle Remote Adapter** for time-boxed Azure RBAC on a machine identity. **Dry run by default.** See below. |

### Wiring an Entitle REST integration

Both adapters implement the [Remote Adapter contract](https://docs.beyondtrust.com/entitle/docs/open-api-definition).
**The verb is the path**, and Entitle lets you configure each path separately, so
point them at the routes below (or leave the defaults, which match):

```
get_assets_path            /get_assets
get_actors_path            /get_actors
get_all_permissions_path   /get_all_permissions
create_actor_path          /create_actor      (required for ephemeral accounts)
delete_actor_path          /delete_actor      (required for ephemeral accounts)
give_access_path           /give_access
revoke_access_path         /revoke_access
```

Authenticate with the function's shared secret as a custom header — Entitle's
`headers` config takes `"Authorization": "Bearer <TOKEN>"` verbatim, which is
exactly what the handler verifies. Get the value from **Functions → Endpoint**.

Start with `entitle_webhook_echo`: it answers every route correctly and grants
nothing, so you can confirm the network path, the front door, the shared secret and
every `*_path` mapping before a real database is involved. A 404 from it lists the
routes it actually serves, which is almost always the fix.

> **Doing this for the first time?** Follow
> [the first-deploy runbook](../runbooks/cloud-functions-first-deploy.md), which
> orders the clouds GCP → AWS → Azure for a reason and says what fails how at each
> stage.

### Automatic pairing on provision

You do not have to deploy a `db_grant` adapter by hand. Provision a database with
**Register in Entitle** checked and the engine decides how it reaches Entitle:

| Engine | Route | Why |
|---|---|---|
| Postgres | native Entitle connector | it works; nothing to gain from replacing it |
| MySQL | **paired adapter** | the native connector assigns persistent roles and never mints an account |
| SQL Server | **paired adapter** | the native connector needs sysadmin, which managed SQL Server does not grant |

The pairing runs as one `clouddb_adapter_pair` job covering all three stages: it
stages the admin credential in the cloud's own secret store, deploys a `db_grant`
function **VPC/VNet-attached** beside the database, and registers it as a REST
integration. One job because the stages are useless individually — a half-finished
pairing leaves you unable to tell whether to retry or clean up.

Two things worth knowing:

- **The adapter is deployed in dry run.** Its first act is to report the SQL it
  *would* run. Arming it (`FN_DB_DRY_RUN=0`) is a deliberate second step.
- **Pairing is non-fatal.** A working database is the deliverable; failed Entitle
  wiring is logged and does not fail the provision that produced it. Check the Jobs
  page if a pairing you expected did not appear.

### Dashboard permissions

The fourth integration is not a function at all: granting **dashboard permissions**
happens on a dashboard-hosted endpoint, because there the dashboard *is* the target
system. See [entitle-user-identity.md](entitle-user-identity.md).

### db_grant

The Phase 2 flagship: short-lived database accounts, minted on Give Access and
dropped on Revoke. This is the case Entitle's native connectors cannot serve — its
MySQL connector assigns persistent roles and never mints an account, and its SQL
Server connector assumes a server-level login plus `USE`, which is not how Azure SQL
Database works.

Configure the target on the function itself (never in the request, so a caller
cannot redirect a grant at another database):

| Setting | Notes |
|---|---|
| `FN_DB_ENGINE` | `mysql` or `sqlserver` |
| `FN_DB_HOST` / `FN_DB_PORT` | the private endpoint; deploy the function in `vpc` mode to reach it |
| `FN_DB_NAME` | the database grants are scoped to |
| `FN_DB_FLAVOR` | `rds` (default), `azure_sql`, or `cloudsql` — **matters for SQL Server only** |
| `FN_DB_ADMIN_USER` | the admin login that runs CREATE/DROP |
| `FN_DB_DRY_RUN` | unset or truthy = dry run. **Default is dry run.** |

`FN_DB_FLAVOR=azure_sql` is not cosmetic: Azure SQL Database is a contained-database
model, so the login goes in `master` on the logical server and the user in the target
database, over two separate connections with no `USE` between them.

The admin password is never a request field. Set it as a Secret Manager secret env
var on GCP (`db_admin_secret`), an `@Microsoft.KeyVault(SecretUri=…)` app setting on
Azure (the module declares a system-assigned identity so the platform can resolve
it — grant that identity `get` on the vault), or `FN_DB_ADMIN_SECRET_ID` plus
`readable_secret_arns` on AWS.

Start in dry run. The response contains the exact SQL the function would execute,
per connection, which is how you validate the whole Entitle path before anything
touches a real database.

`db_grant` implements the four-operation ephemeral lifecycle: `create_actor` mints
the account with **no privileges**, `give_access` grants a role, `revoke_access`
removes the role but leaves the account, and `delete_actor` drops it. Entitle owns
the expiry — no request carries a TTL, and the adapter schedules nothing.

### portainer_access

Just-in-time Portainer access. Portainer has no Entitle connector at all, so this
adapter is the only route to it.

| Setting | Notes |
|---|---|
| `FN_PORTAINER_URL` | base URL of the Portainer instance |
| `FN_PORTAINER_API_KEY` | an access token; supply it by reference (GCP secret env var / Azure Key Vault reference / AWS Secrets Manager) |
| `FN_PORTAINER_VERIFY_SSL` | `0` only for a self-signed lab instance |
| `FN_PORTAINER_DRY_RUN` | unset or truthy = dry run. **Default is dry run.** |

**Access is granted through team membership, not per-user.** In Portainer an access
policy attaches to an environment or environment group and names *teams*. Configure
that once; a grant is then a single membership row, and a revoke removes it. That
keeps the reversible per-request operation separate from the standing
configuration, so a revoke can never leave a half-dismantled access policy behind.

Entitle sees each **team** as an asset, with the team name as the `role_code` — so
an operator reading a request sees the team they configured access on.

Two guards worth knowing about, because they bound the blast radius:

- `create_actor` makes a **standard** user in **no team**. An actor whose
  `give_access` never arrives can reach nothing.
- `get_actors` and `delete_actor` only ever see accounts this adapter minted (the
  `jit-` prefix). Your real Portainer users are never listed to Entitle, and a
  request to delete one is refused with a 403 — a grant integration must not be able
  to remove an operator's account.

If Portainer is reachable only from inside your network, deploy the adapter in
`vpc` mode; otherwise `public` is fine.

### azure_role_grant

Time-boxed Azure RBAC for a **machine identity**. AWS machine identity works because
Entitle attaches an IAM policy to the dashboard's IAM *user*; Azure has no
equivalent, because a machine identity there is an *application* and an application
cannot be the account privileges are requested for. So Entitle's native Azure
integration cannot grant to one, and this adapter is the only route.

| Setting | Notes |
|---|---|
| `FN_AZURE_TENANT_ID` / `FN_AZURE_CLIENT_ID` / `FN_AZURE_CLIENT_SECRET` | the adapter's own service principal; needs **User Access Administrator** on the scopes below |
| `FN_AZURE_SUBSCRIPTION_ID` | where role definitions are resolved from |
| `FN_AZURE_SCOPES` | comma-separated scopes it may grant at — subscription or resource-group paths |
| `FN_AZURE_ROLES` | comma-separated role names or GUIDs it may grant |
| `FN_AZURE_PRINCIPALS` | comma-separated service-principal **object** ids, optionally `objectid=Label` |
| `FN_AZURE_DRY_RUN` | unset or truthy = dry run. **Default is dry run.** |

> **An unbounded adapter here is a privilege-escalation primitive**, so it is bounded
> three ways. Scopes, roles and principals are all allowlists — an empty one grants
> nothing rather than everything. Management-group and tenant-root scopes are refused
> outright. And **Owner, User Access Administrator and RBAC Administrator are refused
> even if you allowlist them**: a time-boxed grant of a role that can grant further
> access is not time-boxed, because the grantee can make it permanent before it
> expires. If you genuinely need to hand one of those out, that belongs with a human
> and a change ticket, not an automated integration behind a shared secret.

Assignments are named by a GUID derived from (scope, principal, role), so a grant is
an idempotent upsert and a revoke deletes by name without listing anything — and
`get_all_permissions` reports only assignments this adapter made, never ones a human
created at the same scope.

`FN_AZURE_PRINCIPALS` accepts a label (`<object-id>=deploy-sp`) so an Entitle request
reads as a name rather than a GUID. Note it must be the service principal's **object**
id: Azure accepts an application id and silently creates an assignment that grants
nothing.

### Writing your own

Copy `examples/functions/custom_handler.py` into `fnworkloads/`, and implement one
function:

```python
from fnruntime.contract import Request, Response, Context

NAME = "my_workload"
DESCRIPTION = "One line, shown in the deploy form."

def handle(req: Request, ctx: Context) -> Response:
    return Response(200, {"ok": True})
```

Two rules:

- **Standard library only.** No pip dependencies (on AWS you may also use `boto3`,
  which the Lambda runtime ships — import it *inside* `handle`). This is what keeps
  a package ~30 KB and needs no build step.
- **Never log secrets.** Use `fnruntime.logs.emit`, which redacts automatically.

If a workload needs a cloud SDK only one runtime provides, add it to
`cloud_function_service._CLOUD_RESTRICTED`.

## Authentication

Two **independent** gates. Getting past one does not get you past the other.

1. **The cloud's front door** — Lambda Function URL `AWS_IAM`, the Azure host key,
   or Cloud Run `roles/run.invoker`.
2. **A shared bearer secret**, checked inside the handler on every request,
   identically on all three clouds.

The defaults leave the front door open on AWS and GCP, because Entitle authenticates
with a plain header and would otherwise need a proxy — so the bearer secret is the
gate there. Tighten `auth_mode` per function if you front it with a gateway.

The secret is minted at deploy, stored encrypted (`cloudfn/{id}/bearer`), and shown
via the **Endpoint** button. The handler **fails closed**: if the secret is missing
from its environment it returns 500, never 200.

## Networking

`network_mode: "public"` deploys an internet-reachable endpoint. `"vpc"` also
attaches the function to your network.

| Cloud | Mechanism | Watch out for |
|---|---|---|
| AWS | `vpc_config` | A VPC-attached Lambda has **no outbound internet** unless its subnets route through NAT |
| Azure | Regional VNet integration | Needs a subnet delegated to `Microsoft.Web/serverFarms`, and a plan that supports it — `Y1` does **not** (plan-time failure) |
| GCP | Serverless VPC Access connector | Reference an existing one; a connector costs ~$26/mo whether invoked or not |

## Troubleshooting

**The deploy form says a cloud is blocked.** Its package store isn't configured —
the reason names the exact setting. Settings → Integrations → Cloud Functions.

**`echo_diag` reports `dns: timeout` on Azure.** Almost always a missing
`WEBSITE_DNS_SERVER`. The module sets it whenever a subnet is configured;
`vnet_route_all_enabled` fixes *routing* and does nothing for DNS, so a
correctly-routed function still cannot resolve `privatelink.*` without it.

**`echo_diag` reports `connect: timeout` but DNS is fine.** Routing or the security
group / NSG. On AWS, confirm the function's security group is allowed inbound at the
target. `connect: refused` is different and *good news* — the network path works and
nothing is listening on that port.

**`egress` probe times out but the private probe succeeds.** Expected on a
VPC-attached Lambda with no NAT. Only a problem for workloads that call outward.

**Azure: the app is up but every route 404s.** The v2 programming model registered
zero functions. `GET https://<app>.azurewebsites.net/api/health` is anonymous and
answers this in one request: a 200 means indexing worked and the problem is
elsewhere; anything else means the app didn't index. The module already sets
`FUNCTIONS_EXTENSION_VERSION=~4` and `AzureWebJobsFeatureFlags=EnableWorkerIndexing`,
which are the usual causes.

**Azure: code changes aren't taking effect.** Package blobs are content-addressed
(`function-packages/<fn_id>/<sha256>.zip`) specifically to prevent this. If you see
it, check that `WEBSITE_RUN_FROM_PACKAGE` points at the hash you expect.

**GCP: the deploy fails ~90 s in, after a successful plan.** Cloud Build. Usually a
missing IAM role — see [Prerequisites](#prerequisites).

**GCP: destroy leaves things behind.** `terraform destroy` removes the function but
not the images Cloud Build pushed to Artifact Registry (`gcf-artifacts` in the
region). Clean those up separately if the storage cost matters.

**Every apply redeploys every function.** Shouldn't happen — packages are built
deterministically so an unchanged function is a no-op. If it does, run
`python tests/test_function_package.py`; the byte-stability assertion is there to
catch exactly this.

## Phase 2 — Entitle REST integrations

Entitle's REST integration posts Give Access / Revoke Access *to* a target system and
expects a synchronous response. A function is the natural receiver: it is externally
reachable, responds in milliseconds, and can sit inside the VPC beside the resource
being granted.

Use `entitle_webhook_echo` to validate the whole path — network, front door, secret,
timeouts, retries — before any grant logic exists. It reports exactly which fields it
recognised in the payload, which is how the schema gets pinned against a real tenant.

Planned integrations: ephemeral MySQL/SQL Server database accounts, Azure machine
identity, Portainer access, and dashboard user identity. See the
[design doc](../design/cloud-functions.md#7-phase-2--entitle-rest-integrations).
