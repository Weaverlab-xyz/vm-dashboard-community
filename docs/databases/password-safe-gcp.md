# Password Safe rotation for Cloud SQL (GCP)

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are onboarding a Cloud SQL database into Password Safe over the Data API.

Part of [Databases](../databases.md), Layer 2. The shared model and the
AWS/Azure channels are in
[Password Safe rotation (AWS + Azure)](password-safe.md).

### GCP — `dbgcp` (Cloud SQL Data API)

> **Every channel is built. None has been exercised against a live instance.**
>
> | Channel | Engines here | State |
> |---|---|---|
> | `data-api` | postgres, mysql | **Built** — verify, change managed account, change functional account and discovery. No action returns "not implemented in this build" any more. |
> | `cloud-run` | sqlserver | **Built.** Plugin transport and the Cloud Run service both implemented and unit-tested. |
> | `admin-api` | — | **Built plugin-side, and accepted by `ps_resource_service`, but the dashboard does not emit it.** It needs `cloudsql.users.update`, which among predefined roles lives only in the very broad `roles/cloudsql.admin` (against `roles/cloudsql.instanceUser` for `data-api`); it performs no database login at all, so its Verify proves only the GCP identity; and it reaches only the instance's user *registry*, so a principal created inside the database with `CREATE ROLE` can be invisible to it. Setting `clouddb_ps_gcp_channel=admin-api` logs a warning and falls back to the per-engine default. |
>
> `passwordsafe_gcp_db_registration_method` therefore still ships **`off`**: the reason is
> no longer a missing implementation, it is that the first live run has not happened.
> `docs/runbooks/clouddb-password-safe-plugin-setup.md` §7 has the order to run it in —
> **Verify Functional Account first**, because it exercises token minting, the endpoint
> path and `autoIamAuthn` in one call and fails in thirty seconds if the endpoint path is
> wrong.
>
> Two open questions are worth knowing before the first rotation, both one `--log-http`
> away and neither blocking:
>
> - **Does a successful DDL return a `results` envelope?** The plugin takes the safe
>   reading (absent envelope = failure), so if a successful `ALTER` also omits it, a
>   rotation that WORKED can be reported as failed. If a change reports failure, send the
>   full Error Details before retrying — "the ALTER failed" and "the ALTER worked but the
>   envelope was empty" are different bugs.
> - **Does `executeSql` statement text reach Cloud Audit Logs?** The change path sends
>   `ALTER … IDENTIFIED BY '<plaintext>'`. If `sqlStatement` is recorded, every rotation
>   writes a live credential into Cloud Logging — long-retained and readable under
>   `roles/logging.viewer`, a broader access model than the database itself. All
>   pre-hashed-verifier flags ship off, so there is no fallback; the mitigations are a log
>   exclusion or a Data Access audit-log exemption for `cloudsql.googleapis.com`. One
>   canary statement and one log query settle it.

Unlike the other two clouds there is **no jump host** on either channel, and the
dashboard does its own onboarding work without one either: the dedicated managed user is
created with `users.insert` on the Cloud SQL Admin API, which opens no database
connection at all.

**PostgreSQL and MySQL use `data-api`.** The plugin reaches a private-IP instance through
Google's own control plane (`instances.executeSql`), so there is no DB client to install,
no RSA key pair, no public certificate on any Resource Broker, and no relay VM — and no
infrastructure of any kind to deploy.

**SQL Server uses `cloud-run`.** Cloud SQL for SQL Server supports IAM authentication for
instance and backup operations only, never for database operations, so on `data-api` the
functional account's password would have to be mirrored into Secret Manager and re-synced
on every rotation — a second authority for a credential Password Safe exists to own, with
a split-brain window if the second write fails. The `cloud-run` channel avoids that
entirely: the credential travels in the request body to a small Cloud Run service inside
the VPC, which holds the database drivers and opens the actual connection. Nothing is
persisted, and Password Safe stays the sole authority.

`cloud-run` is also the **only** channel that can *Verify Managed Account* or let an
account rotate itself — both control-plane channels authenticate as the *caller* and
cannot test an arbitrary principal's password. Set `clouddb_ps_gcp_channel` to
`cloud-run` to put PostgreSQL and MySQL on it too.

**`data-api` on SQL Server needs `fasecret=`, and the dashboard now emits it.** The two
control-plane channels authenticate the *database session* in one of two mutually
exclusive ways: `iam=true` mints an OAuth token per connection, or `fasecret=` names a
Secret Manager version holding the functional account's password. Cloud SQL for SQL
Server has no IAM database authentication at all, so it takes the second — and the
address builder used to append `iam=true` unconditionally and emit `fasecret=` nowhere,
which made a forced `data-api` SQL Server address unparseable in both directions at once.

The secret must be **regional**. The global
`projects/<p>/secrets/<s>/versions/latest` form — which is what the plugin article's own
example prints, and what the Secrets page creates — is rejected by the Data API with
*"does not match the expected format `[projects/*/locations/*/secrets/*/versions/*]`"*.
The dashboard stages a regional one itself in `create` mode (per database, deleted on
decommission) and takes `clouddb_ps_gcp_fa_secret_version` otherwise; the address
validator refuses the global form at the click rather than letting a rotation discover it.

> **This is a second authority for a credential Password Safe exists to own.** Nothing
> re-syncs the mirrored copy, so rotating the functional account *itself* breaks every
> subsequent rotation until the secret is updated by hand. `cloud-run` mirrors nothing —
> the credential travels in the request body — which is why it stays both the default and
> the recommendation for SQL Server. `data-api` is now a *valid* fallback, not a better
> one. With neither a secret version nor a route to one, SQL Server onboarding on
> `data-api` stays off rather than building an address the plugin refuses.

> **The dashboard deploys the Cloud Run service.** Settings → Password Safe → *Cloud Run
> channel* has a **Deploy** button per region (job `clouddb_dbops_deploy`). What the
> dashboard cannot build is the plugin repository's .NET image — and it does not need to.
> The plugin depends on an HTTP contract, not on an implementation language, so the
> dashboard ships its own service (`fnworkloads/ps_dbops.py`) through the same Cloud
> Functions gen2 source-deploy path that already builds, VPC-attaches and deploys the
> `db_grant` adapter. A gen2 function *is* a Cloud Run service: Cloud Build turns the
> package into a container, and the dashboard has been doing that on GCP all along.
>
> **One service per region, not per database.** Unlike `db_grant`, this one is stateless
> with respect to the database — the instance, the catalog and the credential all arrive
> in the request, because that is the contract. Direct VPC egress is region-locked (which
> also rules out one per project), the warm instance it needs for correctness bills
> continuously, and Cloud Run reserves subnet IPs in /28 blocks.
>
> **You still deploy it yourself if you want to.** `clouddb_ps_gcp_dbops_audience` remains,
> now as an override for a service you stood up (the plugin repo's `ps-dbops-sqlserver`
> Terraform module) or one behind a custom domain or Private Service Connect. With neither
> a deployed service in the database's region nor that override, SQL Server onboarding
> stays off, because there would be no address to build.
>
> **The v1 request contract is not implemented yet.** The service deploys, authenticates,
> reaches the VPC and answers a health probe, but `/v1/credential-op` returns **501** with
> the versions it can serve, and logs the request it was sent. That is deliberate: the
> shape is defined by the plugin and is not in this repository, and a plausible guess would
> produce a service that deploys cleanly and fails every rotation. Point one managed system
> at it and click *Verify Managed Account* — the real request lands in Cloud Logging.
> See [docs/design/ps-dbops-cloud-run.md](../design/ps-dbops-cloud-run.md).

The DB is registered on the **`GCP Cloud SQL {engine}`** platform with the five-field
address `channel;project:region:instance;dbName;audience;ssl[;key=value]`:

| Channel | Address the dashboard builds |
|---|---|
| `data-api` (postgres, mysql) | `data-api;{project}:{region}:{instance};{dbName};-;-;iam=true` — MySQL appends `;host=%` |
| `data-api` (sqlserver) | `data-api;{project}:{region}:{instance};master;-;-;fasecret={regional version}` — **no `iam=`**, which the plugin rejects on this engine |
| `cloud-run` | `cloud-run;{project}:{region}:{instance};{dbName};{audience};sslTRUE` |

Field 2 is the instance **connection name**, not a hostname. On the control-plane channel
fields 4 and 5 are `-`: the Cloud SQL APIs are always TLS and never open a database
connection, and the plugin rejects a value there rather than letting anyone believe they
disabled it. On `cloud-run`, field 4 is the audience **verbatim** — a path, query or
fragment is rejected — and field 5 is the real TLS choice for the service→database hop.

On SQL Server the database is `master`: that is the admin session catalog, and the
statements are server-level (`ALTER LOGIN`).

**Two options the plugin used to accept and quietly ignore are now refused, and
`ps_resource_service` refuses them too** — the dashboard emits neither, so this matters
only for a hand-written or imported address:

| Option | Refused where | Why silence was the defect |
|---|---|---|
| `verifier=on` | anywhere but `cloud-run` | It says the new password was pre-hashed on the Resource Broker so the plaintext never reaches the wire. The Cloud SQL APIs take the password in the statement text, so accepting it on a control-plane channel reported a protection that was not happening. `verifier=off` stays legal everywhere — it promises nothing. |
| `iam=false` | on `data-api` | It left the address with no way to authenticate at all: `executeSql` has no plaintext-password field, and `fasecret=` is SQL Server only. It onboarded cleanly and failed at the first rotation with an opaque Google 401. |

`fasecret=` is checked against **three** rules rather than one: regional shape, *not* the
global form (which no amount of moving the secret fixes — Google refuses a secret created
through the global endpoint even when it is stored in the right region), and a region that
matches **field 2's**. The Data API reads the secret through the instance's own regional
endpoint, so a us-central1 instance cannot be handed a us-east1 secret.

**MySQL Account Discovery needs one grant beyond the rotation ladder:**
`GRANT SELECT ON mysql.user TO '<rotator>'@'%';`. `CREATE USER` confers no read of the
account catalogue, so without it Discovery returns MySQL 1142 while rotation and Verify
work normally. The dashboard applies it as a **second, independent** statement during
onboarding — deliberately not appended to the rotation grant, because whether Cloud SQL
permits it at all is not yet confirmed against a live instance, and one call would let
that open question take the rotation grant down with it. A failure is reported on the job
with the statement to paste.

**Sizing.** The Data API caps a request at 0.5 MB, a response at 10 MB and **10
concurrent queries per instance**, and enforces its own non-configurable 30-second
statement timeout. Stagger bulk rotations that all target one instance.

The functional account is `ADC:<dbUser>` (or `IMP:`/`SA:`) with a three-segment password
`{key|-}:{impersonate|-}:{dbPassword|-}`. What goes in the third segment is the sharpest
difference between the two channels:

- **`data-api`** — under IAM database authentication there is **no database password at
  all**. The credential is a short-lived OAuth token minted per connection, so segment 3
  is `-`. Nothing per-database is packed into the composite, which makes **`reference`
  mode the recommended configuration**: one operator-created account per engine covers
  every instance.
- **`cloud-run`** — SQL Server has no IAM database authentication, so the functional
  account is a real login with a real password. In `create` mode that is the database's
  own minted admin, which makes the composite **per-database** — the same property that
  makes `reference` mode the better answer on Azure. In `reference` mode the operator's
  account carries a stable login instead.

**`clouddb_ps_self_rotation` is honoured per channel, not per cloud.** Self-rotation
needs to log in *as* the managed account, which only `cloud-run` can do; `data-api`
refuses it at pre-flight. So the dashboard emits `use_own_credentials` for a `cloud-run`
managed system and drops it for a `data-api` one — leaving the global flag on for
AWS/Azure `reference` mode, which requires it, cannot break either GCP path. On
`cloud-run` self-rotation is the *preferred* setting: the managed login alters itself
with `OLD_PASSWORD` and the functional account needs no privilege over it at all.

**Prerequisites (manual):**

- Upload the **`GCP Cloud SQL PostgreSQL`** / **`GCP Cloud SQL MySQL`** /
  **`GCP Cloud SQL SQL Server`** plugins and **`PRA Vault Username Password`**. No key
  pair and no broker certificate — there is none in this design.
- **For SQL Server (or any engine you move onto `cloud-run`)**: list each Resource
  Broker's service account in `clouddb_ps_gcp_dbops_invokers` — **named service accounts
  only**, never `allUsers` or `allAuthenticatedUsers`, which the Terraform module refuses
  outright — and press **Deploy** for the database's region. The dashboard sets
  `--min-instances=1` (Direct VPC egress documents connection-establishment delays over a
  minute on startup, and a rotation that times out may already have applied),
  `--concurrency=8` (each request holds a database connection), `--timeout=120` and
  `--no-allow-unauthenticated`, and records the audience itself: it is the service's own
  URL, so there is no custom audience to invent. Not a manual step any more, and no
  `clouddb_ps_gcp_dbops_audience` to fill in unless you deployed the service yourself.
- Create the **rotator service account** and name it in the panel. **Keep the name
  short**: MySQL truncates an IAM database username at the `@` and caps it at **32
  characters**, so `bt-rotator` is safe and `bt-passwordsafe-cloudsql-rotator-prod` is not.
- Grant it a least-privilege role. On `data-api`, **`roles/cloudsql.instanceUser`** is
  sufficient — it carries `cloudsql.instances.executesql` and `cloudsql.instances.login`
  and, critically, **not** `cloudsql.users.update`. Prefer an IAM **condition** on
  `resource.name` to scope it to specific instances. On `cloud-run` the rotator needs no
  Cloud SQL role at all — only `roles/run.invoker` on the service — because the service
  connects with a database login rather than an IAM identity. (The rotator service
  account itself is a `data-api` concept; SQL Server does not use one.)
- Give the **Resource Brokers** a GCP identity. On a Compute Engine broker, attach the
  service account (`ADC:`, nothing stored anywhere). Off GCP, place a key file at
  `GOOGLE_APPLICATION_CREDENTIALS` on each broker, or use `IMP:` and grant the broker's
  identity `roles/iam.serviceAccountTokenCreator` on the rotator. `SA:` is **not** a
  fallback: a base64 key is ~3.2 KB, over Password Safe's 1000-character credential limit.
- Grant the functional account rights over each managed principal — **unless you use
  self-rotation on `cloud-run`, which needs none of this.** The dashboard prints the exact
  statement on the provisioning job, because it cannot issue it itself. On **PostgreSQL
  16** — the module default — `GRANT "<managed>" TO "<fa>" WITH ADMIN OPTION` per role;
  `CREATEROLE` alone is no longer sufficient. On MySQL,
  `GRANT CREATE USER ON *.* TO '<fa>'@'%'`. On SQL Server, `ALTER ANY LOGIN`, which
  `CustomerDbRootRole` carries — `sysadmin` is unavailable on Cloud SQL and
  `ALTER ANY LOGIN` is exactly enough.
- Create a **PRA Configuration-API account** as in the AWS section.

On the **`data-api`** path the dashboard enables the two per-instance prerequisites
itself at onboarding (`ensure_cloudsql_rotation_prereqs`): the **Cloud SQL Data API**,
which is *off by default per instance*, and the `cloudsql.iam_authentication` database
flag. New instances also get the flag declaratively from the Terraform module. It then
registers the rotator as an IAM database user and **reads back the name the database
actually stored** rather than deriving it — there is in-house precedent for GCP surfacing
an unexpected principal name, and a functional account naming a principal the database
does not have fails Verify with an unhelpful message.

**SQL Server on `data-api` skips the IAM half of all of that**, because it has none: the
Data API is still enabled, but the `cloudsql.iam_authentication` flag is not requested
(there is nothing to request), no rotator is registered as an IAM database user, and the
functional account is the built-in admin login rather than an IAM principal. One
predicate — `_iam_db_auth(engine, channel)` — decides the instance flag, the functional
account's database user and the address option together, because deciding them
separately is exactly how they drifted apart. Since the functional account *is* the
admin there, the rotation grant is skipped too: it already holds every right over every
principal on the instance.

On the **`cloud-run`** path none of that runs. The service connects over TCP with a
database login, so there is no Data API to enable, no IAM database authentication to turn
on, and no instance to patch — the dashboard creates the managed user and stops.

**Config keys** (the PRA-Vault plugin and workgroup are shared with the AWS keys above;
there are no client-image or key-material keys here):

| Key | Default | Notes |
|---|---|---|
| `passwordsafe_gcp_db_registration_method` | `off` | `dataapi` or `off` — the master switch for GCP, whichever channel an engine ends up on |
| `clouddb_ps_gcp_channel` | `auto` | `auto` (Data API for postgres/mysql, Cloud Run for sqlserver), or `data-api` / `cloud-run` to force one for every engine |
| `clouddb_ps_platform_gcp_postgres` / `_mysql` / `_sqlserver` | `GCP Cloud SQL PostgreSQL` / `… MySQL` / `… SQL Server` | Custom-plugin platform names; advisory in `reference` mode |
| `clouddb_ps_functional_account_gcp_postgres` / `_mysql` / `_sqlserver` | — | `reference` mode: the operator-created account on each Cloud SQL platform |
| `clouddb_ps_gcp_auth_mode` | `ADC` | `ADC` / `IMP` / `SA` — the functional-account username prefix |
| `clouddb_ps_gcp_impersonate_target` | — | `IMP` mode: the service account to impersonate |
| `clouddb_ps_gcp_rotator_service_account` | — | **`data-api` only, and not SQL Server.** The rotation identity, registered as an IAM database user per instance. Keep it ≤32 characters for MySQL |
| `clouddb_ps_gcp_fa_secret_version` | — | **`data-api` + SQL Server only.** Address option `fasecret=` — a **regional** Secret Manager version holding the functional account's password. Blank is fine in `create` mode (the dashboard stages one per database); **required** in `reference` mode |
| `clouddb_ps_gcp_dbops_audience` | — | **`cloud-run` only.** An **override** — address field 4 for a service you deployed yourself, or one behind a custom domain / PSC. A dashboard-deployed service in the database's own region **beats it**, because Direct VPC egress is region-locked and one global value would address a rotation at a service that cannot reach the instance. With neither, SQL Server onboarding stays off |
| `clouddb_ps_gcp_dbops_ssl` | `true` | `sslTRUE` / `sslFALSE` — address field 5, the service→database TLS choice |
| `clouddb_ps_gcp_dbops_invokers` | — | Comma-separated IAM members granted `roles/run.invoker` on the deployed service — the Resource Brokers' identities. A bare email is accepted and prefixed. Named principals only |
| `clouddb_ps_gcp_dbops_ingress` | `all` | `all` (public + IAM; the only thing an **on-premises** broker can reach) or `internal` |
| `clouddb_ps_gcp_dbops_min_instances` | `1` | Warm instances. A **correctness** setting — see above. Bills continuously |
| `clouddb_ps_gcp_dbops_concurrency` | `8` | Requests per instance, well under Cloud Run's default of 80: each one holds a database connection |

---
