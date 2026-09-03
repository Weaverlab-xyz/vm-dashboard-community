# Cloud Functions (preview)

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you need a stable HTTPS endpoint external systems can call to act inside your network.

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
| **AWS** | An S3 bucket for packages. The dashboard's IAM identity needs `s3:PutObject`, plus `lambda:*`, `iam:CreateRole`/`AttachRolePolicy`/`PutRolePolicy` (the execution role and its inline secrets policy), `secretsmanager:CreateSecret`/`PutSecretValue`/`DeleteSecret`/`DescribeSecret`/`TagResource` (every function keeps its bearer secret there) and `logs:*` on the function's log group (including `logs:DeleteLogGroup`, or destroy leaves it behind). Two that are easy to miss: **`iam:PassRole`** on the execution role (`lambda:CreateFunction` passes it to Lambda) and **`lambda:AddPermission`** — a function URL with `authorization_type = NONE` needs a resource policy that `CreateFunctionUrlConfig` does *not* write, so without it every public invoke returns `{"Message":"Forbidden"}`. The module creates that permission itself; the deploying identity just needs to be allowed to. |
| **Azure** | The dashboard's existing storage account (`storage_azure_account`) and resource group, **plus a Key Vault** — every function keeps its bearer secret there. The service principal needs `Microsoft.Web/*`, `Microsoft.Storage/storageAccounts/listKeys/action`, `Microsoft.Web/sites/host/listKeys/action`, `Microsoft.ManagedIdentity/userAssignedIdentities/*`, secret `set` on the vault, and authority to grant on it (`Microsoft.Authorization/roleAssignments/write` for an RBAC vault, or `Microsoft.KeyVault/vaults/accessPolicies/write` for a policy-based one). |
| **GCP** | A GCS bucket for sources, the `cloudfunctions` + `artifactregistry` APIs enabled, and a noticeably larger IAM surface: `roles/cloudfunctions.developer`, `roles/run.admin`, `roles/cloudbuild.builds.builder`, `roles/artifactregistry.writer`, `roles/storage.objectAdmin` (project-wide, or bucket-scoped on the source bucket), `roles/secretmanager.admin`, plus `roles/iam.serviceAccountUser` on the runtime service account. Direct VPC egress itself needs **no extra grant** — those permissions belong to the Cloud Run service agent, which already holds them for a same-project VPC. |
| All | Terraform in the image (it is, by default). GCP additionally needs the **google provider `>= 7.21`**, which the image pre-caches. |

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

> The **adapter** workloads (`db_grant`, `portainer_access`, `azure_role_grant`) read
> their target out of the function's environment and do nothing without it — the
> deploy form names what each one needs and refuses a deploy that omits it, because
> the alternative is a function that builds successfully and then fails on every
> request. `db_grant` in particular should always be deployed *for* you — by the
> **Function (DB grant)** button on a database's row, or at provision time: see
> [Cloud databases](../databases.md) and the `clouddb_adapter_pair` job.
> `POST /check_config` is the route to ask an already-deployed adapter whether it is
> configured; it answers `{"data": {"valid": false, "problems": [...]}}` rather than
> failing when it is not. Once `db_grant` is armed (`FN_DB_DRY_RUN=0`) it also opens
> one real connection, so it answers the question a dry run cannot: whether the
> function can actually reach the database.

### Field reference

| Field | Notes |
|---|---|
| `function_package_s3_bucket` | AWS package bucket |
| `function_package_gcs_bucket` | GCP source bucket |
| `function_package_azure_container` | Blob container on the dashboard's storage account (default `function-packages`) |
| `azure_functions_plan_sku` | App Service plan SKU, default `B1` |
| `aws_functions_subnet_ids` / `aws_functions_security_group_ids` | Comma-separated; used when a function is deployed in `vpc` mode |
| `azure_functions_subnet_id` | Must be delegated to `Microsoft.Web/serverFarms` |
| `gcp_functions_network` / `gcp_functions_subnetwork` | GCP **Direct VPC egress** — no connector, nothing billed while idle. Give the subnet as a **bare name**: direct egress is region-locked, and a bare name resolves in whichever region the function lands in. |
| `gcp_functions_service_account` | Runtime service account. Blank falls back to the broad default compute SA — and skips the per-secret accessor binding, so the function cannot read its own bearer secret. |
| `gcp_functions_vpc_connector` | Legacy fallback: an **existing** Serverless VPC Access connector. Only needed to reach *another region* from a region-pinned function. Ignored when the two keys above are set. |

## What it enables in the dashboard

| Feature | Where |
|---|---|
| Deploy / list / destroy functions | **Functions** page, `/api/functions` |
| Workload catalog | `GET /api/functions/workloads` |
| Test invoke with credentials attached | **Test invoke** button, `POST /api/functions/{id}/invoke` (takes `method` + `path`, so an adapter's own routes are reachable) |
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

`get_all_permissions` is the one response whose shape is not what the definition's
example shows: both permission fields are **maps keyed by asset id**, not arrays
(`actors_permissions` is `map[asset_id] -> [{actor_id, role_code, direct_member}]`,
and `assets_permissions` is the asset-to-asset half, which is `{}` for every target
here). An array is rejected with *Structure of "Get All Permission Response" is
invalid* on that route alone — assets and actors keep syncing green, so the only
symptom is one red line in Entitle's audit log. `fnruntime.entitle.permissions_data`
builds it; use it rather than a literal.

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

### Pairing: never deploy a db_grant adapter by hand

A `db_grant` function is **named after its database** and reads its target from its own
environment, so one started from the *Deploy a function* form cannot be finished
afterwards. There are two supported ways to get one, and both derive everything from the
database row:

| | Where | Dry run? |
|---|---|---|
| **On provision** | the provision form's **Register in Entitle** choice | yes — observe first |
| **Any time after** | the **Function (DB grant)** button on the database's row | no — ready to act |

Either way the engine decides how the database reaches Entitle:

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

Things worth knowing:

- **The function lands in the database's own cloud and region.** It has to: it reaches a
  private endpoint over the VPC/VNet, and subnets are regional. Lambda, Function App and
  Cloud Run functions are all *regional* resources, so there is no zone to match. A
  region with no per-region config set is **refused** rather than deployed onto the
  default region's network — add one under **Settings → Multi-region**.
- **Dry run differs by entry point**, deliberately — and it is not a safety control.
  The adapter initiates nothing: it is an HTTP endpoint behind `fnruntime.auth`'s
  fail-closed bearer gate, and **Entitle owns the account lifecycle** (created on an
  approved request, dropped when the access ends). So `FN_DB_DRY_RUN=1` does not stop
  accounts being created — it makes an *approved* request silently do nothing, which is
  useful for confirming the SQL a new integration would emit and misleading as a
  standing state. The provision-time pairing keeps the observe-first default because
  nothing is wired up to request against it yet; the row button deploys ready to act
  (`FN_DB_DRY_RUN=0`), because it is used on a database whose integration is about to be
  real.
- **Provision-time pairing is non-fatal.** A working database is the deliverable; failed
  Entitle wiring is logged and does not fail the provision that produced it. Check the
  Jobs page if a pairing you expected did not appear.
- **Entitle registration is skippable, the deploy is not.** With
  `entitle_registration_enabled` off, the row button still deploys and configures a
  working adapter and the job completes with `entitle_skipped`; register it later with
  **Register in Entitle** on the Functions page.
- **One adapter per database, and a second is refused.** The name is derived from the
  database id, and deploying over an existing function would collide at apply, so the
  button is replaced by a **DB grant ✓** badge once one exists. To redeploy, delete the
  function on the Cloud Functions page first.

The button is hidden — not disabled — for a database that cannot take an adapter:
Postgres and Oracle (their native connector works), a *registered* rather than
provisioned database (no admin credential is stored for the adapter to act as), an
on-premises database (no cloud secret store to stage the credential in), and one with no
recorded catalog to scope grants to (RDS SQL Server creates no user database, and
`master` is the system catalog). Deploying the adapter also requires the
`cloud_function:write` permission, since that is what it does.

### Dashboard permissions

The fourth integration is not a function at all: granting **dashboard permissions**
happens on a dashboard-hosted endpoint, because there the dashboard *is* the target
system and there is no function to deploy. See
[entitle-dashboard-permissions.md](entitle-dashboard-permissions.md).

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
| `FN_DB_NAMES` | **several** databases on that server, comma-separated. Replaces `FN_DB_NAME`; see [One adapter, several databases](#one-adapter-several-databases) |
| `FN_DB_FLAVOR` | `rds` (default), `azure_sql`, or `cloudsql` — **matters for SQL Server only** |
| `FN_DB_ADMIN_USER` | the admin login that runs CREATE/DROP |
| `FN_DB_ADMIN_PASSWORD` | **never set directly** — pass it as `secret_environment` (see [Credentials](#credentials)) |
| `FN_DB_DRY_RUN` | unset or truthy = dry run. **Default is dry run.** |
| `FN_DB_CAFILE` | optional PEM bundle **inside the package** to verify the database's certificate against. Unset (the normal case) = encrypt without verifying |

`FN_DB_FLAVOR=azure_sql` is not cosmetic: Azure SQL Database is a contained-database
model, so the login goes in `master` on the logical server and the user in the target
database, over two separate connections with no `USE` between them.

Every connection is **encrypted**, on both engines, with no way to turn that off —
Azure SQL and Azure Flexible Server refuse plaintext outright, and a JIT credential
is the last traffic you want in the clear. It is *not* verified by default: a
function package pins neither the DigiCert root Azure SQL presents, nor the Amazon
RDS roots, nor Cloud SQL's per-instance server CA, and there is no host trust store
in the zip to fall back on. `FN_DB_CAFILE` is the opt-in for a deployment that wants
verification and has a bundle to do it with. The hop is inside your VPC/VNet to a
private endpoint either way.

The admin password is never a request field, and never a plaintext setting: pass it
as `secret_environment` (`{"FN_DB_ADMIN_PASSWORD": "<reference>"}`) and each cloud
resolves it as described under [Credentials](#credentials). The older per-cloud
spellings — `db_admin_secret` on GCP, `FN_DB_ADMIN_SECRET_ID` plus
`readable_secret_arns` on AWS — still work for callers that already use them.

Start in dry run. The response contains the exact SQL the function would execute,
per connection, which is how you validate the whole Entitle path before anything
touches a real database.

`db_grant` implements the four-operation ephemeral lifecycle: `create_actor` mints
the account with **no privileges**, `give_access` grants a role, `revoke_access`
removes the role but leaves the account, and `delete_actor` drops it. Entitle owns
the expiry — no request carries a TTL, and the adapter schedules nothing.

Two guards bound what a caller past the bearer secret can do, and they are the same
line `portainer_access` draws:

- **Only accounts this adapter minted.** `give_access`, `revoke_access` and
  `delete_actor` refuse any identifier that is not one of its own `jit_` accounts,
  with a 403. Without that, `delete_actor` is a `DROP USER` for any name — the
  admin login included — and `give_access` grants a role to any existing account,
  such as your application's.
- **The request selects a target, it never describes one.** Entitle echoes the whole
  `asset` object; only its `identifier` is read, and only to look up a database the
  operator configured. A host or database name in the request is ignored.

#### One adapter, several databases

Set `FN_DB_NAMES` instead of `FN_DB_NAME` and one function serves every database in
the list, each as its own Entitle asset:

```
FN_DB_NAMES = appdb,reporting,billing
```

**They must be on the same server** — `FN_DB_HOST` stays singular, and that is a
security boundary rather than an oversight:

- The admin credential a JIT adapter needs (`CREATE`/`DROP USER`) is **already
  server-level** on every managed engine here. Several databases on one server
  therefore share a blast radius whether or not they share a function, so
  consolidating them removes copies of that credential rather than adding reach.
  Two *servers* do not share one, and an endpoint holding both would genuinely
  widen it.
- Entitle's `create_actor` and `delete_actor` carry **no asset** — an actor belongs
  to the adapter, not to an asset — so an adapter spanning servers could not know
  which server to mint an account on. Within one server it can: the account is a
  server-level principal (a MySQL user, a SQL Server login) and only the *grant* is
  per-database.

What changes with more than one database:

| | One database | Several |
|---|---|---|
| `get_assets` | one asset | one per database |
| a request with no `asset.identifier` | resolves — the only database | **400**, listing what it serves; picking one would be a silent mis-grant |
| `create_actor` | account, no privileges | same, in every listed database (SQL Server gets a role-less user per database; MySQL's user is already server-wide) |
| `delete_actor` | drops the account | drops it everywhere it was made, then the login — a database user left behind when its login goes is an orphaned user a later login of the same name re-adopts |
| `get_asset_permissions/{id}` | the one asset | only that asset's accounts, read per database rather than inferred |

To add a database to an adapter that is already running, change its settings rather
than redeploying it:

```
POST /api/functions/{id}/environment
{ "environment": { "FN_DB_NAMES": "appdb,reporting,billing" } }
```

Settings are merged over the current ones and a key set to `null` is removed. The
function is re-applied **in place**, which is the point: destroy-and-redeploy loses
the endpoint URL, the bearer secret, and the Entitle integration registered against
both. An update also rebuilds the package from the running image, so it brings the
function up to that image's handler code — a no-op when the image is unchanged,
since packages are deterministic.

> **Automatic pairing is unaffected, and that is correct.** **Register in Entitle**
> deploys one adapter per database because each dashboard-provisioned database is its
> own server instance — two of them never share one, so there is no adapter to share.
> `FN_DB_NAMES` is for several databases on **one** server, which is the shape you get
> from a server that was provisioned once and grew databases since, or from databases
> registered rather than provisioned.

### portainer_access

Just-in-time Portainer access. Portainer has no Entitle connector at all, so this
adapter is the only route to it.

| Setting | Notes |
|---|---|
| `FN_PORTAINER_URL` | base URL of the Portainer instance |
| `FN_PORTAINER_API_KEY` | an access token — pass it as `secret_environment`, never as a plaintext setting (see [Credentials](#credentials)) |
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
| `FN_AZURE_TENANT_ID` / `FN_AZURE_CLIENT_ID` | the adapter's own service principal; needs **User Access Administrator** on the scopes below |
| `FN_AZURE_CLIENT_SECRET` | that principal's secret — pass it as `secret_environment` (see [Credentials](#credentials)). It is the highest-value credential in the feature, so it is never a plaintext setting |
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

### Credentials

A workload that needs a credential — a database admin password, a Portainer token,
an Azure client secret — gets it **by reference on every cloud**. You name a secret
that already lives in the cloud's own secret store, and the dashboard wires up that
cloud's resolution mechanism:

| Cloud | What you pass | What happens |
|---|---|---|
| **AWS** | the Secrets Manager **ARN** | the id lands in `<NAME>_SECRET_ID` and the ARN on the function's role; the handler reads it at cold start |
| **GCP** | the Secret Manager **secret id** | injected as a `secret_environment_variables` entry, and the runtime service account is bound `roles/secretmanager.secretAccessor` on that secret alone |
| **Azure** | the **Key Vault secret name** | becomes an `@Microsoft.KeyVault(SecretUri=…)` app setting the platform resolves through the app's system-assigned identity (grant it `get` on the vault) |

On AWS it must be the full ARN: the role's policy names ARNs, and AWS appends a
random suffix to every one, so a name cannot be turned into an ARN by concatenation.

```
POST /api/functions
{
  "cloud": "gcp", "region": "us-central1", "name": "portainer-jit",
  "workload": "portainer_access",
  "environment": { "FN_PORTAINER_URL": "https://portainer.internal",
                   "FN_PORTAINER_DRY_RUN": "0" },
  "secret_environment": { "FN_PORTAINER_API_KEY": "portainer-jit-token" }
}
```

> **`environment` is for non-secret settings, and the dashboard enforces that.** A
> credential-shaped value there is refused at deploy with a message pointing here.
> The reason it is an error rather than a warning: `environment` is stored in the
> deploy job's metadata, streams into the job's Live Output as Terraform renders the
> plan, and stays readable on the function's own console page — so by the time you
> saw a warning, the credential would need rotating.

Automatic pairing already works this way; nothing to configure. A `db_grant` adapter
deployed by **Register in Entitle** stages the admin password in the cloud's secret
store and passes only the reference.

### Writing your own

> **Workloads are repository files, not uploads.** There is deliberately **no
> upload path** for handler code: a workload is added by committing a file to
> `web_dashboard/functions/fnworkloads/` and shipping an image. This is not the
> same pipeline as [asset uploads](../storage-management.md#what-counts-as-an-asset)
> and the `.yml`/`.sh`/`.ps1` allowlist there does not apply.
>
> The reason is what these handlers hold. A workload runs with the database admin
> credential, or rights to write Azure role assignments, from inside your VPC. An
> endpoint that accepted arbitrary uploaded Python and then ran it with those
> credentials would be a remote-code-execution surface wearing a feature's clothes.
> Requiring a commit means every workload passes review, diff and CI — and it is
> also what makes the package **deterministic**, since the source is fixed at image
> build rather than varying per upload.
>
> **You do not have to upstream your workloads.** Run your own image: fork, add
> handlers under `fnworkloads/`, build, and deploy. Everything else — the packager, the
> Terraform modules, the adapter contract, the guards — works unchanged, and
> [provenance](#provenance-what-is-actually-running) records which source produced each
> deployed function, so a fleet built from several images is still auditable.

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

## Provenance: what is actually running

A deployed function runs privileged code from inside your network, so the question
"what exactly is running in there, and where did it come from?" needs to stay
answerable months later — especially once workloads come from more than one image.

Every deploy records three things on the function:

| | What it tells you | Availability |
|---|---|---|
| **source tree hash** | *what code* — two functions with the same value run the same handlers | **always** |
| **package hash** | the exact deployed artifact, including vendored dependencies | always |
| **git commit / ref / origin** | *who wrote and reviewed it* | when the image was built with the build args |

The tree hash is computed from the files that produced the package, so it cannot be
wrong. Git metadata is best-effort — an image built without the build args records
**"unknown" rather than a guess**, because a confidently wrong commit is worse than
an honest blank. A build from a modified working tree is marked `-dirty`.

To get git provenance into your image:

```bash
docker build \
  --build-arg GIT_SHA="$(git rev-parse HEAD)" \
  --build-arg GIT_REF="$(git rev-parse --abbrev-ref HEAD)" \
  --build-arg GIT_ORIGIN="$(git config --get remote.origin.url)" \
  --build-arg BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" .
```

It also lands as the standard `org.opencontainers.image.revision` label.

**Ask the function, don't trust the record.** The same values are injected as
`FN_SOURCE_COMMIT` / `FN_SOURCE_TREE`, and `echo_diag` reports them in its
`provenance` block. So verifying a deployment is one request — and it catches the
case the dashboard row cannot: a deploy that half-failed, or a function someone
changed by hand.

```bash
curl -sS -X POST "$FN_URL" -H "Authorization: Bearer $SECRET" \
  -d '{"egress": false}' | jq .provenance
```

## Authentication

Two **independent** gates. Getting past one does not get you past the other.

1. **The cloud's front door** — Lambda Function URL `AWS_IAM`, the Azure host key,
   or Cloud Run `roles/run.invoker`.
2. **A shared bearer secret**, checked inside the handler on every request,
   identically on all three clouds.

The defaults leave the front door open on AWS and GCP, because Entitle authenticates
with a plain header and would otherwise need a proxy — so the bearer secret is the
gate there. Tighten `auth_mode` per function if you front it with a gateway.

**Azure is the exception: its front door cannot be opened.** The Functions host
requires its own key on every route but `/api/health`, so the dashboard fetches it
after the deploy, stores it as `cloudfn/{id}/invoke-key`, and sends it as
`x-functions-key` — from Test invoke, and as a second header on the Entitle REST
integration it registers. A hand-written `curl` needs it too; the **Endpoint** button
shows it.

Which of the two gates refused you is in the **body**, not the status code:

| Response to an authenticated route | Who refused | What to do |
|---|---|---|
| `401`, **empty** body | the Azure host key (or GCP's `run.invoker`, or an AWS Function URL permission) — the front door, before your code ran | re-run Test invoke: it re-fetches the key from ARM. `GET /api/health` returning 200 confirms the app itself is healthy |
| `401 {"error": "unauthorized"}` | `fnruntime.auth` inside the handler | the bearer secret the dashboard holds is not the one the function verifies — redeploy to mint a matching pair |
| `500 {"error": "function not configured", ...}` | the workload | the named setting is missing; `POST /check_config` lists all of them |
| `500 {"error": "internal error", "request_id": ...}` | the workload raised | nothing about the cause is in the body, by design. For `db_grant`, `POST /check_config` opens a real connection and reports what failed — TLS, the firewall, the VNet route, the admin credential — as a sentence. The full detail (driver error code and message) is in the function's own log stream, and `FN_DEBUG=1` adds a traceback there |

The secret is minted at deploy, stored encrypted (`cloudfn/{id}/bearer`), and shown
via the **Endpoint** button. The handler **fails closed**: if it cannot resolve the
secret it returns 500, never 200 — including when the secret store is unreachable or
the function's role has lost its read grant.

**Where the function keeps it**, which is not the same question as where the
dashboard keeps it:

| Cloud | Where | Who can read it |
|---|---|---|
| **AWS** | a Secrets Manager secret the module creates (`<name>-fn-secret`); only its ARN is in the function's environment | the function's role, and principals you grant on that secret |
| **GCP** | Secret Manager, injected as a `secret_environment_variables` entry | the runtime service account |
| **Azure** | Key Vault, referenced from an app setting the platform resolves | the function's own managed identity, and principals you grant on the vault |

The AWS arrangement matters more than it looks: a Lambda's environment is returned
in full by `lambda:GetFunctionConfiguration`, which the AWS-managed **ReadOnlyAccess**
policy grants — so a bearer token stored there is readable by every read-only
principal in the account, and reading it is enough to call the function. It now costs
one Secrets Manager secret per function (~$0.40/month).

**Azure needs a Key Vault, and a deploy without one is refused rather than
downgraded to a readable app setting.** Configure it under Settings → Secrets →
Azure Key Vault (`secrets_azure_kv_url`); set `azure_key_vault_resource_group` too if
the vault does not live in the dashboard's own resource group. You do **not** grant
anything per function: the module creates a user-assigned identity, grants it read on
that vault, and points the app's Key Vault reference resolution at it.

> Why a user-assigned identity rather than the app's system-assigned one: a
> system-assigned identity does not exist until the app has been created, so the app
> would boot with an unresolvable reference and only recover when Azure re-checks
> references — up to 24 hours later. A user-assigned identity can be granted *before*
> the app exists, so the first cold start resolves. The system-assigned identity is
> still created, so an existing manual grant keeps working.

> The secret passes through Terraform state on AWS and GCP, because the resource that
> creates it takes the value. On Azure it does not — the dashboard writes it to the
> vault and Terraform only ever sees the reference. State lives in your configured
> backend; treat it as sensitive.

## Networking

`network_mode: "public"` deploys an internet-reachable endpoint. `"vpc"` also
attaches the function to your network.

| Cloud | Mechanism | Watch out for |
|---|---|---|
| AWS | `vpc_config` | A VPC-attached Lambda has **no outbound internet** unless its subnets route through NAT |
| Azure | Regional VNet integration | Needs a subnet delegated to `Microsoft.Web/serverFarms`, and a plan that supports it — `Y1` does **not** (plan-time failure) |
| GCP | Direct VPC egress (`direct_vpc_network_interface`) | **Region-locked** — the subnet must be in the function's region, or traffic to an internal IP silently drops. Cloud Run reserves IPs in /28 blocks and needs a /26 minimum subnet; sharing one subnet across functions is supported. Needs provider `>= 7.21`. |
| GCP (legacy) | Serverless VPC Access connector | Only for cross-region reach from a region-pinned function. Reference an existing one; a connector costs ~$26/mo whether invoked or not |

### Which subnet, in which region

Every cloud has a **per-region** functions network field, and it is what makes a function
land on its own region's network rather than the default region's:

| Cloud | Per-region field(s) | Flat fallback |
|---|---|---|
| AWS | `functions_subnet_ids`, `functions_security_group_ids` | `aws_functions_subnet_ids`, `aws_functions_security_group_ids` |
| Azure | `functions_subnet_id` | `azure_functions_subnet_id` |
| GCP | `functions_network`, `functions_subnetwork` | `gcp_functions_network`, `gcp_functions_subnetwork` |

Set them under **Settings → Multi-region** (the sandbox scripts emit both halves). A field
a region leaves blank falls back to its flat key, so a single-region install needs none of
this and behaves exactly as before.

The fields are deliberately **purpose-specific** rather than reusing the generic
`default_subnet_id`/`subnetwork`: a flat `*_functions_*` key answers two questions at once
— *which subnet do functions use* and *in the default region* — and overriding it with a
generic per-region subnet fixes the region by discarding the purpose, and only for
non-default regions. That asymmetry (the same feature choosing a different-purpose subnet
depending on whether the region happened to be the default) is the bug these fields exist
to avoid.

For a caller that supplies the region programmatically rather than from a picker — the
cloud-DB adapter pairing uses the *database's* region — the deploy is **refused** when that
region has no per-region config set at all, rather than quietly attaching to the default
region's network.

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
VPC-attached Lambda with no NAT. Harmless for workloads that only reach inward.

**AWS: every invoke of a `vpc`-mode function returns 500, but the deploy was clean.**
Auth failed closed. The handler resolves its bearer secret from **Secrets Manager at
cold start**, so a VPC-attached Lambda needs a path to that API — and a private
subnet with no NAT route has none. This is *not* only an outward-calling concern: it
breaks the front door for every workload. Give the VPC a `secretsmanager` **interface
endpoint** with private DNS (the sandbox scripts create one; `SANDBOX_SKIP_FN_VPCE=1`
opts out), or keep a NAT route up. It looks exactly like a wrong bearer secret.

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
