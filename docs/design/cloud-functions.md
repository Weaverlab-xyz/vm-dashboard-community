# Cloud Functions (preview) — design

Status: **preview** (`cloud_functions_enabled`). Phase 1 = the modular function
lifecycle + a catalog of standalone workloads. Phase 2 = using those functions as
Entitle REST integrations (see §7).

## 1. Why

The dashboard already reaches private, non-internet-facing resources on all three
clouds: it runs **one-shot containers inside the target cloud** (ECS task on AWS,
ACI on Azure, Cloud Run job on GCP — selected per-cloud by
`ansible_runner_{aws,azure,gcp}`). Database provisioning *and* configuration
management both work everywhere. **Reach is not the problem.**

Two things are missing:

1. **No always-on endpoint.** Every existing execution path is dashboard-initiated
   and outbound. Entitle's REST integration runs the other way — Entitle POSTs
   *Give Access* / *Revoke Access* **inbound** and expects a synchronous response
   (see `cloud-identity-jit.md` Appendix D). A one-shot container has no stable URL
   and tens of seconds of cold start, so it cannot serve that call.

2. **Entitle's native connectors don't cover the cases we care about.** MySQL gets
   persistent roles only (`entitle_registration_service._generate_db_hcl`,
   `allow_creating_accounts = engine != "mysql"`); the MSSQL connector does
   ephemeral accounts but not against the managed SQL flavors the dashboard deploys
   (`azurerm_mssql_database`, `google_sql_database_instance`, `aws_db_instance`);
   Azure machine identity has no native model at all; Portainer and the dashboard's
   own local/OIDC users have no connector.

A cloud function closes both with one primitive: a stable HTTPS endpoint,
millisecond response, optionally attached to the VPC/VNet beside the private
resource, running identical dashboard-authored logic on all three clouds.

Phase 1 must be useful with Entitle switched off — it is a serverless ops toolkit
on its own (§6).

## 2. The portable handler contract

Source lives in `web_dashboard/functions/`. It ships in the image for free —
`Dockerfile` already does `COPY web_dashboard/`. Deployable handler source
deliberately does **not** live in `examples/`, which is not copied into the image.

```
web_dashboard/functions/
  runtime/     contract.py  adapters.py  auth.py  logs.py  dispatch.py
  entry/       aws_entry.py  gcp_entry.py  azure_entry.py  host.json
  workloads/   echo_diag.py  entitle_webhook_echo.py  ...
```

### 2.1 The zero-dependency rule

**`runtime/` imports stdlib only.** This is the constraint the whole design rests
on. It means:

- AWS needs no Lambda layer and no vendored packages (the Lambda Python image
  already ships `boto3`).
- Azure's vendor set — required because run-from-package never runs `pip install` —
  is exactly one pure-Python wheel, `azure-functions`.
- GCP's `requirements.txt` is trivial.

Any workload that breaks the rule (see `db_grant`, §7) must use **pure-Python**
libraries. Vendoring a wheel with a compiled `.so` is unsafe: the dashboard image
is multi-arch, so an arm64 build would ship an arm64 binary into an x86_64 Lambda.
`tests/test_function_package.py` asserts the vendor tree contains no `.so`/`.pyd`.

### 2.2 Normalized types

`runtime/contract.py` defines `Request`, `Response`, `Context`. A workload author
writes exactly one function and nothing else — no decorators, no `azure.functions`,
no `event`/`context`, no Flask:

```python
NAME = "echo_diag"
DESCRIPTION = "..."

def handle(req: Request, ctx: Context) -> Response:
    return Response(200, {"ok": True})
```

`Request.headers` keys are **always lower-cased by the adapter**, and
`Request.body` is already base64-decoded. The workload module is passed *into* `dispatch.handle_request` by the entry shim
rather than imported by it, so dispatch stays pure and testable with fakes; the
packager copies exactly one workload module into the zip root as `workload.py`.

**The catalog is the filesystem.** `fnworkloads/*.py` is the source of truth —
dropping in a module makes it deployable, which is the point of the feature being
modular. `cloud_function_service._CLOUD_RESTRICTED` records only the *exceptions*:
workloads needing a cloud SDK that just one runtime ships (today only
`local_account_broker`, which needs boto3). Everything stdlib-only is universal by
default and needs no table edit.

### 2.3 Adapters

`from_aws` handles four event shapes; missing any of them is a crash, not a
degradation:

| Shape | Discriminator |
|---|---|
| Lambda Function URL / API GW HTTP v2 | `event.get("version") == "2.0"` (one branch; the stage may prefix `rawPath`) |
| API GW REST v1 / ALB | `"httpMethod" in event` — mixed-case headers, `queryStringParameters` may be `None` |
| Direct invoke (EventBridge, `aws lambda invoke`) | neither of the above — synthesized as `POST /` with the whole event as the body |

The direct-invoke shape is what makes scheduled workloads (`cred_expiry_watch`)
possible; without it a scheduled invoke dies on `event["requestContext"]`.

`to_aws` **always** emits the explicit `{"statusCode","headers","body",
"isBase64Encoded"}` envelope. Returning a bare dict from a Function URL makes
Lambda wrap it as a 200 — which would silently swallow a 401.

Per repo convention the cloud SDK imports are lazy: `from_azure` imports
`azure.functions` inside the function body, and `from_gcp` never imports Flask at
all — it duck-types `.method/.path/.headers/.args/.get_data()`. So `adapters.py`
imports cleanly on all three runtimes and is unit-testable with plain fixtures.

### 2.4 Auth — layered, fail-closed

Two independent gates. Getting past one does not get you past the other.

1. **Cloud-native front door**, where it is free: Lambda Function URL `AWS_IAM`,
   the Azure function host key, Cloud Run `roles/run.invoker`.
2. **A shared-secret bearer header**, verified in `runtime/auth.py` on every
   request regardless of cloud.

`auth.verify` fails **closed**: a missing `FN_SHARED_SECRET` returns 500, never
200. It compares with `hmac.compare_digest`, and returns a byte-identical 401 body
for "missing" and "wrong" so it leaks nothing. Direct invokes bypass the HTTP
front door, so the secret may also arrive as a `secret` field in the synthesized
body — which is why `secret` is a redacted key (§2.5).

**Gate 2 is selectable per workload, and the default never moved.** A workload may
declare a module-level `AUTH_MODE`; `dispatch` passes it to `auth.verify_for`, and
anything it does not recognise — including a typo — is a 500, never a pass. Two modes
exist:

| `AUTH_MODE` | Gate | Who declares it |
|---|---|---|
| absent / `shared_secret` | `auth.verify` — everything above | every workload but one |
| `gcp_oidc` | `auth.verify_gcp_oidc` | `ps_dbops` only |

`ps_dbops` needs it because its caller is a Password Safe Resource Broker presenting a
Google-issued ID token in the same `Authorization` header — a credential the dashboard
never issued and cannot compare against a secret it minted. So the inner gate is
*replaced*, not removed: `verify_gcp_oidc` checks the token's `aud` against
`FN_DBOPS_AUDIENCE` and its `email` against `FN_DBOPS_ALLOWED_INVOKERS`, with the same
three properties — fails closed when unconfigured, byte-identical 401s, and a
constant-time compare where it compares.

It does **not** re-verify the signature, and that is the one thing to understand before
copying this pattern. Cloud Run's front door has already validated signature, expiry and
audience and confirmed the caller holds `roles/run.invoker`; re-doing it would mean
fetching and caching Google's JWKS from a runtime whose whole contract is "stdlib only".
The claims are parsed for *authorization* on top of an *authentication* decision the
platform already made — which is only sound while the platform is actually making one.
So the gate reads `FN_AUTH_MODE_FRONT_DOOR` (set by the GCP module from `auth_mode`) and
fails closed on anything but `run_invoker`, as an **allowlist**: a missing value means a
deploy whose front door we cannot vouch for, and the safe reading of "I don't know" is
"no". `cloud_function_service._check_front_door` refuses such a deploy too, but the two
checks live in different trust domains and the container-side one is the load-bearing
half.

### 2.5 Logging

One line of JSON per request to stdout — CloudWatch, App Insights, and Cloud
Logging all capture it. `logs.redact` is a pure function and the single best
unit-test target in the runtime. Headers **always** pass through it before being
logged, so `authorization` is unconditionally `"***"`. The raw body is never
logged; only `redact(req.json())`, and only when `FN_LOG_BODY=1`.

## 3. Packaging

> Build one deterministic zip in memory, upload it to an object store **in the same
> cloud as the function**, and have Terraform reference it by bucket + key + hash.

GCP forces this: `google_cloudfunctions2_function.build_config.source` accepts only
`storage_source` or `repo_source` — there is no inline option. Since GCS is
mandatory anyway, matching it on S3 and Blob removes all divergence from the
transport, leaving divergence only in the zip layout. All three upload SDKs are
already dependencies (`boto3`, `google-cloud-storage`, `azure-storage-blob`).

`cloud_function_package.build()` is deterministic — fixed timestamps, sorted
insertion order, fixed attrs, no `__pycache__`. This matters more than it looks:
if mtimes leak in, the content hash changes on every apply and **every function
redeploys forever**, which on GCP is a 60–120 s Cloud Build each time.

| | AWS | GCP | Azure |
|---|---|---|---|
| entry file at zip root | `aws_entry.py` | `main.py` *(name fixed by the buildpack)* | `function_app.py` *(name fixed by the v2 model)* |
| declared in Terraform | `handler` | `entry_point` | discovered |
| dependencies | none | `requirements.txt` | vendored `azure-functions` |
| deploy latency | ~5 s | 60–120 s (Cloud Build) | ~20 s (restart) |
| update trigger | `source_code_hash` | `storage_source.object` | app-setting URL change |
| residue after destroy | none | Artifact Registry images | none |

## 4. Per-cloud choices

**AWS — S3-sourced, not inline `archive_file`.** `archive_file` needs the source
tree present in the deploy dir at *destroy* time as well as apply, and it zips with
real mtimes, which are not stable across the app container and the jobs-worker
container. When `subnet_ids` is set the module must **also** attach
`AWSLambdaVPCAccessExecutionRole` — without it the function creates fine and every
invoke fails with an ENI error.

**Azure — `azurerm_linux_function_app` on a `B1` App Service plan, code via
`WEBSITE_RUN_FROM_PACKAGE`.** The forks, and why:

- *Flex Consumption — rejected.* Its code path is the Kudu `/api/publish` endpoint,
  which Terraform cannot drive. It would mean a hand-rolled Kudu REST client plus
  Oryx remote build (pip-installing from PyPI at deploy time) — a second,
  non-Terraform deployment channel that AWS and GCP don't have, re-introducing
  exactly the runtime-download flakiness the Dockerfile's provider pre-cache went
  out of its way to eliminate.
- *Linux Consumption (Y1) — rejected as the default.* It **cannot do regional VNet
  integration**, so Phase 2 is impossible on it. Allowed as an opt-in `sku_name`
  for public-only demos; the module has a `precondition` that fails at plan time if
  Y1 is paired with a subnet.
- *Elastic Premium (EP1)* — works, ~10× the cost of B1 for a preview feature.
- **B1** — supports VNet integration *and* run-from-package, fully declarative, no
  cold-start surprises, and one variable moves an operator up to `EP1`/`P0v3`.

Two Azure traps encoded in the module: the blob name **must** contain the content
hash (`WEBSITE_RUN_FROM_PACKAGE` is a plain app-setting string, so overwriting a
fixed-name blob leaves Terraform seeing no diff while the app serves stale code);
and `AzureWebJobsFeatureFlags = "EnableWorkerIndexing"` + `FUNCTIONS_EXTENSION_VERSION
= "~4"` are set preemptively, against the classic v2-model failure where the app
boots, serves the default landing page, and registers zero functions.

Azure host keys have **no azurerm data source** — they are fetched post-apply over
the ARM REST API and stored as `cloudfn/{fn_id}/invoke-key`. The fetch stays
non-fatal (the function is deployed, and the bearer secret still protects it) but a
function without that key **cannot be called at all**: the Functions host refuses
every route but `/api/health` with a 401 and a zero-length body, before the
container runs, so `/check_config` — and therefore Test invoke and the Entitle
preflight — never answers.

Two things follow, and both were live failures on 2026-09-03:

- **The fetch polls.** `terraform apply` returns when ARM has *created* the app,
  which is before the Functions host has started and minted its default key; until
  it has, `listKeys` answers 200 with an **empty** `functionKeys` map. That is not
  an error, so the first implementation stored nothing, silently, and never retried.
- **It is re-fetched on demand.** Test invoke, the **Endpoint** panel and the
  Entitle preflight all repair a missing key, and an empty 401 is retried once when
  ARM reports a *different* key — which covers a key regenerated in the portal.
  Otherwise one lost race left a working function permanently un-invokable, with a
  redeploy as the only repair.

**GCP — `google_cloudfunctions2_function`.** Two things operators must know up
front, because they are the top two failure modes: every deploy runs Cloud Build
and pushes to Artifact Registry (and `terraform destroy` leaves those images
behind); and the IAM surface is much larger than the DB modules needed —
`cloudfunctions.developer`, `run.admin`, `cloudbuild.builds.builder`,
`artifactregistry.writer`, `storage.objectAdmin`, plus `iam.serviceAccountUser` on
the runtime SA. A missing role surfaces as a 403 ~90 s into the build, *after* a
successful plan.

## 5. Networking

`network_mode = "public" | "vpc"`. Each module validates its own cloud's ids with a
`precondition`, so a missing id fails at plan rather than minutes into an apply.

| Cloud | Mechanism | The gotcha |
|---|---|---|
| AWS | `vpc_config { subnet_ids, security_group_ids }` | A VPC-attached Lambda has **no public internet** unless its subnets route through NAT. Callback-using workloads hang until timeout. |
| Azure | `virtual_network_subnet_id` + `vnet_route_all_enabled` | Needs a subnet delegated to `Microsoft.Web/serverFarms` — a *different* subnet from `azure_db_subnet_id` (delegated to `…/flexibleServers`). And **`WEBSITE_DNS_SERVER = 168.63.129.16`**, or the `privatelink.*` zone won't resolve: `vnet_route_all_enabled` fixes routing, not DNS. |
| GCP | `vpc_connector` + `PRIVATE_RANGES_ONLY` | Serverless VPC Access, **not** Direct VPC egress — `cloudfunctions2.service_config` only exposes `vpc_connector`. A connector runs ≥2 `e2-micro` instances (~$26/mo) whether invoked or not, so reference an **existing** connector rather than creating one per function. *(Superseded: provider 7.21 added `direct_vpc_network_interface`, which the module now prefers — see its own comments.)* |

### 5.1 Scaling and the front door (GCP)

Four module variables exist for one workload's needs and default to today's behaviour
for every other:

| Variable | Default | Why it exists |
|---|---|---|
| `min_instances` | `0` | A **correctness** setting for `ps_dbops`: Direct VPC egress can take over a minute to establish on a cold start, and a rotation that times out may already have applied the change. Bills continuously. |
| `concurrency` | `0` (platform default, 80) | A workload that holds a database connection for the life of a request hits Cloud SQL's connection limit long before Cloud Run decides it needs another instance. |
| `ingress_settings` | `ALLOW_ALL` | Was **declared but never passed**, so every GCP function was `ALLOW_ALL` whatever the module said. Now reachable. |
| `invoker_members` | `[]` | Named `roles/run.invoker` bindings, for `auth_mode = "run_invoker"`. A Terraform `validation` refuses `allUsers`/`allAuthenticatedUsers` — an operator who wants a public function says so with `auth_mode = "none"`, which is recorded on the row and visible in the UI. |

## 6. Workload catalog

Standalone-useful first; Phase-2-enabling second.

| Workload | What it does | Phase 2 role |
|---|---|---|
| `echo_diag` | Echoes the normalized request (redacted) + cloud/region/network placement; TCP/DNS probes each caller-supplied `{host,port}` | Pre-flight check **and this feature's own end-to-end verifier** |
| `entitle_webhook_echo` | Validates the Entitle Give/Revoke payload shape and returns the success envelope; failure injection via `?fail=` | The contract test every real workload implements |
| `cred_expiry_watch` | TLS handshake sweep → issuer/`not_after`/days remaining; on-demand or scheduled | Scheduled-invoke plumbing + the function→dashboard callback direction |
| `db_grant` | Ephemeral DB account create/drop against the private DB | **The Phase 2 pilot** |
| `local_account_broker` *(AWS)* | SSM-driven ephemeral OS account + authorized key | SSH ephemeral-accounts executor for hosts Entitle's agent can't reach |
| `ps_dbops` *(GCP)* | The "bt-dbops" service: changes and verifies Cloud SQL credentials for the Password Safe plugin's `cloud-run` channel | None — a different contract (§9.1) |
| `inventory_reporter` | Reports what it can see from inside the network, POSTing back to the dashboard | Reverse callback channel for revocation reconciliation |

## 7. Phase 2 — Entitle REST integrations

Entitle POSTs Give/Revoke to the function URL with the shared secret as a header.
The function performs the grant against the target and returns the success
envelope. Four integrations are planned:

1. **Ephemeral MySQL / MSSQL DB accounts** — the pilot. **Built** (see §9).
2. **Azure machine identity** — Entitle calls a function that performs the ARM
   `roleAssignments/write` against the service principal with an `endDateTime`.
   Note the token-cache invalidation requirement after a grant
   (`cloud-identity-jit.md` §5.2); `azure_service.invalidate_credentials()` exists.
3. **Portainer** — needs `/api/users`, `/api/teams`, `/api/team_memberships` added
   to `portainer_service.py` first.
4. **Dashboard user identity** — the one exception: the dashboard **is** the target
   system, so Entitle calls `/api/entitle/rest/*` on the dashboard directly, with no
   function hop. Replaces the Entra-group indirection with direct grants on
   `User.session_permissions_dict`, working for local and OIDC users alike.

## 9. db_grant — ephemeral database accounts

Give Access mints a short-lived account on a private database; Revoke Access drops
it. `cloud_db_sql_service.grant_plan` / `revoke_plan` build the SQL and the
`db_grant` workload executes it.

### The plan shape, and why it is not a statement list

Both builders return `[(database, [statements…]), …]` — a **plan**, because Azure
SQL Database genuinely needs two connections: the login lives in `master` on the
logical server, the user lives in the target database, and there is no `USE` to
bridge them. Encoding that in the return value keeps the flavor difference in data
the tests assert on, rather than buried in a branch inside the executor.

| Flavor | Shape |
|---|---|
| `rds`, `cloudsql` | one connection to `master`, `USE [db]` to switch |
| `azure_sql` | **two** connections — `master` for the login, the target database for the contained user |

MySQL needs no flavor branching at all: `CREATE USER` is identical on RDS, Flexible
Server and Cloud SQL. Revokes are existence-guarded and, on Azure SQL, drop the
user *before* the login — the login cannot be dropped while a principal maps to it.
Entitle retries, and a revoke that errors leaves standing access behind, which is
the one outcome this feature exists to prevent.

### `FN_DB_NAME` is the grant scope, not the admin catalog

"Which database?" has **two** answers for a SQL Server row, and they are not
interchangeable. Both are correct; the disagreement is deliberate.

| Concept | Resolver | sqlserver answer |
|---|---|---|
| **Admin session** — where an admin connects, and where SQL Server's *server-level* principals live | `cloud_database_service.connection_db_name` | **always `master`**, every cloud |
| **Grant scope** — the catalog whose *data* a JIT account gets rights on | `cloud_db_adapter_service._database_name` → `FN_DB_NAME` | the **real user catalog** |

The admin-session answer is `master` because AWS RDS for SQL Server rejects `db_name`
at creation (so that Terraform var set omits it and `master` is the only catalog that
exists), because `CREATE LOGIN` is a server-level statement, and because `USE` /
a reconnect gets you anywhere else afterwards. It is not a display-only helper: the
PRA protocol tunnel, the Ansible connection vars, the managed-user creation, the Secrets
Safe admin document and the native Entitle connector all open the catalog it returns.

`FN_DB_NAME` must **not** follow it. `db_datareader` / `db_datawriter` are
*database-level* fixed roles, so scoping them to `master` would hand the JIT account
rights over the system catalog and none over the application data. The two answers
diverge precisely on **Azure and GCP**, whose modules do create a user catalog
(`azurerm_mssql_database`, `google_sql_database`); on AWS there is no user catalog, so
pairing refuses rather than falling back to `master`. `tests/test_clouddb_db_name_concepts.py`
pins the split in both directions.

### Dry run is the default

With `FN_DB_DRY_RUN` unset the workload returns the exact statements it would run
and opens no connection. That is how the Entitle path gets validated end to end
before anything touches a real database — and dry run never returns a password,
because nothing was created and a credential with no account behind it is just a
loose secret.

### The one break in the zero-dependency rule

`db_grant` opens real connections, so it ships drivers. PyMySQL is pure Python and
does TLS through the stdlib `ssl` module. SQL Server is the problem: `python-tds`
is pure Python but does TLS through **pyOpenSSL → cryptography**, which is
compiled, and Azure SQL *requires* encryption, so there is no way around it.

Vendoring a compiled wheel out of site-packages is unsafe on this multi-arch image.
So the Dockerfile fetches the wheels **for the function's platform** at build time
(`pip install --platform manylinux2014_x86_64`) into `FN_VENDOR_DIR`, and the
packager takes binaries from there and nowhere else. The "no compiled artifacts"
assertion narrows to "none from site-packages" rather than disappearing, so the
wrong-architecture hazard stays closed. This pins functions to x86_64 — the default
on all three clouds.

`cloud_db_sql_service.py` itself is copied into the zip as `sqlplan.py` — the file,
not a reimplementation — so the SQL that runs in the cloud is byte-identical to the
SQL under test. It is safe to ship because it is stdlib-only and opens no
connection; `tests/test_db_grant_workload.py` pins both properties.

### Credentials

The admin password is never a request field, and never a plaintext setting either.
Every credential any workload needs is declared once, as `secret_environment`
(`{ENV_VAR: reference}`), and `cloud_function_service._secret_environment` turns it
into that cloud's own mechanism. Terraform sees a reference; the value never enters
plan output, state, or the function's describe output:

| Cloud | Mechanism |
|---|---|
| GCP | `secret_environment_variables` — the module takes a map, and binds `roles/secretmanager.secretAccessor` on each referenced secret to the runtime SA |
| Azure | `@Microsoft.KeyVault(SecretUri=…)` app setting, resolved by the platform using the app's system-assigned identity (which is why the module declares one) |
| AWS | no platform equivalent for Lambda — the id goes in `<NAME>_SECRET_ID` and the ARN on the role, and `fnruntime.secretref` reads Secrets Manager at cold start with the boto3 the runtime already ships |

`fnruntime.secretref` is the workload-side half: **the value env var wins if set**
(GCP and Azure have already put it there), otherwise resolve an id (AWS). One
implementation, so the JSON-payload rules and the read caching are right once rather
than per workload.

The counterpart guard is on the way in: `environment` is for non-secret settings, and
`_reject_plaintext_secrets` refuses a credential-shaped value there before the row and
the Job are written. It has to be a hard error — `environment` is deliberately not in
`_SECRET_TF_KEYS`, so there is no state between "accepted" and "leaked".

The target (engine, host, port, database, flavor) also comes from the function's
own configuration, never the request — otherwise every caller of this endpoint
would be a lateral-movement primitive.

### One adapter, several databases — and why not several servers

`FN_DB_NAMES` lets one function serve N databases, each its own Entitle asset. The
request's `asset.identifier` **selects** from that allowlist; it can never extend it,
and with more than one database a missing identifier is a 400 rather than a default.

The line is drawn at the **server**, for two independent reasons:

1. **Blast radius does not actually widen.** The admin credential this adapter needs
   is server-level on every managed engine here, so databases sharing a server
   already share a blast radius. Consolidating them removes copies of that credential
   from the fleet. A second *server* is a second credential, and one endpoint holding
   both is a genuine widening.
2. **The contract forbids it.** Entitle's `create_actor` and `delete_actor` carry no
   asset — the actor is bound to the adapter — so an adapter spanning servers cannot
   know where to mint. Within one server it does not need to: the principal is
   server-scoped (MySQL user, SQL Server login) and only the grant is per-database.
   `create_actor_plan`/`delete_actor_plan` take `databases` for exactly this, and a
   delete must undo the create everywhere or it leaves orphaned users behind.

Changing which databases an adapter serves is an **update**, not a redeploy:
`update_environment` re-applies in the ORIGINAL deploy job's directory, because that
is where the function's terraform state lives — applying under the update job's own
id would build a second function beside the first. Destroy-and-redeploy is not an
alternative here: the endpoint URL, the bearer secret and the Entitle integration
registered against both are exactly what a redeploy discards.

Two guards were added with it, both of which were missing and both of which matter
at one database too: `give_access`, `revoke_access` and `delete_actor` refuse any
account outside the adapter's own `jit_` namespace (403). Without them `delete_actor`
was a `DROP USER` for any name and `give_access` could grant a role to an existing
application login — `portainer_access` already drew this line and `db_grant` had not.

### The Entitle contract (confirmed)

Source: [Remote Adapter OpenAPI definition](https://docs.beyondtrust.com/entitle/docs/open-api-definition)
and [REST integration setup](https://docs.beyondtrust.com/entitle/docs/entitle-integration-rest).

**The verb is the PATH, not a body field**, and each path is configurable per
operation in the integration config (`give_access_path`, `revoke_access_path`,
`create_actor_path`, …), so a 404 almost always means those fields disagree with
the adapter's routes — which is why the 404 body lists them.

| Route | Body |
|---|---|
| `GET /get_assets` | — (paginated via `page`, returns `{next, data:{assets}}`) |
| `GET /get_actors` | — |
| `GET /get_all_permissions` | — |
| `GET /get_asset_permissions/{asset_identifier}` | — |
| `POST /create_actor` | `{actor}` |
| `POST /give_access` | `{asset, actor_identifier, role_code}` |
| `POST /revoke_access` | `{asset, actor_identifier, role_code}` |
| `POST /delete_actor` | `{actor_identifier}` |
| `POST /check_config` | `{config}` → `{data:{valid}}` |

Three things worth stating explicitly, because each shaped the implementation:

- **There is no TTL in any request.** Entitle owns expiry and calls revoke/delete
  when a grant ends, so an adapter is stateless with respect to time — it never
  schedules anything and never needs to be told a duration.
- **Ephemeral accounts require `create_actor` / `delete_actor`.** They are a
  separate lifecycle from `give_access` / `revoke_access`, which is why
  `cloud_db_sql_service` exposes four plan builders rather than two. That split
  maps almost exactly onto the SQL Server login-vs-role-membership split that
  already existed.
- **Auth is a custom header or OAuth2 client credentials.** The layered
  bearer-header model this feature already used slots straight in — Entitle's
  `headers` config takes `"Authorization": "Bearer <TOKEN>"` verbatim.

The read routes are not optional in practice: `get_assets` is how Entitle populates
the request UI, and `get_all_permissions` is how it reconciles.

### Registering an adapter

`entitle_registration_service.register_rest()` creates the integration through the
`entitleio/entitle` provider, the same inline-HCL path the DB/SSH/K8s registrations
use. Driven from the Functions page (**Register in Entitle**) or
`POST /api/functions/{id}/entitle-register`, as a `cloudfn_entitle_register` job.

Three choices worth recording:

- **Full URLs per path, not `schema` + `host`.** Entitle accepts either, but Azure's
  base carries an `/api` prefix that relative paths would drop. One fewer thing to
  get subtly wrong per cloud.
- **`private=False` by default**, unlike every other `register_*` helper. The whole
  point of an adapter is an internet-reachable endpoint Entitle can call directly
  even when the resource behind it is private — it is the *function* that is
  VPC-attached, not the integration, so no agent is involved.
- **`allow_creating_accounts` follows `ephemeral`.** This is where the MySQL
  limitation disappears: the constraint was never MySQL's, it was Entitle's MySQL
  *connector's*, and a REST adapter does not use that connector.

`tests/test_entitle_register_rest.py` cross-checks the generated paths against the
routes `db_grant` actually serves, in both directions — a mismatch there produces an
integration that saves cleanly in Entitle and 404s on every real grant, which is not
a failure anyone would enjoy diagnosing.

⚠️  The application catalog slug (`entitle_rest_app_slug`, default `"rest api"`) is
tenant-specific and still unconfirmed — the same caveat as the DB and SSH slugs.
A wrong value fails at apply as a 404 "Application not found".

## 8. Security notes

- `_SECRET_TF_KEYS = ("shared_secret", "package_sas_url", "storage_account_access_key")`
  are stripped before job metadata is persisted and re-injected in **both**
  `run_deploy_apply` and `run_decommission`. `package_sas_url` is the easy one to
  miss — it is a credential embedded in a URL, and unstripped it lands in
  `jobs.extra_data` *and* streams into the job's Live Output.
- The modules never use a `data` source on the package object. If the blob has been
  garbage-collected, a `data "google_storage_bucket_object"` would make `destroy`
  unrecoverable.
- Terraform variables holding secrets are marked `sensitive = true`.
- The **bearer secret** is not a plaintext setting on any cloud. AWS: the module
  creates its own Secrets Manager secret and puts only the ARN in the environment (a
  Lambda's env is readable with `ReadOnlyAccess`, so the old arrangement handed a
  working credential to every read-only principal). GCP: Secret Manager, as before.
  Azure: the dashboard writes it to Key Vault and passes a reference, so it is the
  one cloud where the value reaches neither a setting nor Terraform state — and the
  module grants its own user-assigned identity read on the vault, because a
  system-assigned one does not exist early enough to resolve the first cold start.
  `fnruntime.auth` resolves it through the same `secretref` path as every other
  credential, and fails closed — without raising, because `dispatch` calls
  `verify()` outside its own try/except.
- No credential is passed to a function as a plaintext env var on any cloud. The
  deploy path refuses one (`_reject_plaintext_secrets`), using a superset of
  `fnruntime.logs`' redaction rule — what the runtime will not log is what the
  dashboard will not store. `tests/test_cloud_function_service.py` pins the
  relationship so the two cannot drift.
- Auth fails closed; see §2.4.
