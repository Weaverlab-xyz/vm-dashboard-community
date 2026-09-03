# The dashboard deploys `bt-dbops`

> **Audience:** contributor · **Profile:** `demo` · **Read this when:** you are working on the in-VPC service the Cloud SQL rotation plugin calls.

The `cloud-run` channel of the Password Safe GCP Cloud SQL plugin needs a small service
inside the VPC — "bt-dbops" — that holds the database drivers and opens the actual
connection, so a credential can travel in a request body instead of being mirrored into
Secret Manager. It used to be an operator step, and three places in this repo said the
dashboard could not do it.

**It can.** What the dashboard cannot build is the plugin repository's .NET image, and it
does not need to: the plugin depends on an HTTP contract, not on an implementation
language. `web_dashboard/functions/fnworkloads/ps_dbops.py` serves that contract through
the same Cloud Functions gen2 source-deploy path that already builds, VPC-attaches and
deploys the `db_grant` adapter.

**This matters more than it first looked.** When this was written `cloud-run` was the
*only* working path for Cloud SQL SQL Server — forcing `data-api` produced an address the
plugin rejected twice over. That has since been fixed separately (§9), so `data-api` is
now a valid fallback. It is not a better one: it mirrors the functional account's password
into Secret Manager, which is precisely the second authority this channel exists to avoid.
`cloud-run` remains the default and the recommendation.

Status: **built, except the v1 request parser** (§5). The service deploys, authenticates,
reaches the VPC, answers a health probe, and logs the real plugin request.

---

## 1. Where the image comes from

A Cloud Functions gen2 function *is* a Cloud Run service: `build_config.source` hands a
GCS zip to Cloud Build, which produces a container, pushes it to `gcf-artifacts` and
deploys it. So "build from source via Cloud Build" and "reuse the gen2 source-deploy path"
are the same path — the second is the first with the build orchestration, Artifact
Registry lifecycle and image GC already written and already exercised in production by
`db_grant`.

Reused unchanged:

| Machinery | Where |
|---|---|
| Zip packaging + driver vendoring | `services/cloud_function_package.py` — `pymysql`, `pytds`, `OpenSSL`, `cryptography` were **already vendored** for `db_grant`, so `ps_dbops` needed no Dockerfile change. SQL Server is `pytds`: pure Python plus a compiled TLS chain from `/opt/fn-vendor`. No ODBC driver, no system package. |
| VPC placement | `cloud_function_service._resolved_network` — Direct VPC egress, region-locked, per-region subnet via `resolve_region`. |
| Deploy / record / stream / destroy | `deploy`, `run_deploy_apply`, `update_environment`, `start_decommission`. |
| Front door | `auth_mode = "run_invoker"` already means *no `allUsers` binding*, i.e. `--no-allow-unauthenticated`. |

The alternative — a dedicated `google_cloud_run_v2_service` module with a Dockerfile and
an explicit `gcloud builds submit` — buys exactly one thing (`custom_audiences`, which §3
shows we do not need) at the cost of an Artifact Registry repository to keep, an image tag
scheme, a build-status poll loop, image garbage collection, and a second deploy path that
will drift from the first. It stays the documented fallback if §5's contract work turns up
something gen2 cannot express.

## 2. One service per (project, region)

Not per database, and not once per project.

`bt-dbops` is **stateless with respect to the database**: the instance, the catalog, the
TLS choice and the credential all arrive in the request, because that is the contract.
That is the opposite of `db_grant`, which takes its target from the environment precisely
so a caller cannot redirect a grant. So there is nothing per-database to bind, and three
things argue against binding anyway:

- `--min-instances=1` (§4) is **always-on billing**. One idle instance per region is a few
  dollars a month; one per database is a line item that grows with the fleet.
- Direct VPC egress is **region-locked** — the subnet must be in the function's region.
  That is what rules out "once per project" as well.
- Cloud Run reserves subnet IPs in /28 blocks and needs a /26 minimum.

The name is `bt-dbops`, unsuffixed: Cloud Run names are unique per project and region, so
one name per region already *is* one service per region, and a redeploy converges on the
same service instead of accumulating one per attempt. The lookup keys on
`(workload, cloud, region)` rather than the name, so a hand-renamed service is still found.

**The cost of sharing, stated plainly.** One service per region means one endpoint that
any broker holding `roles/run.invoker` can aim at *any* Cloud SQL instance reachable from
that VPC. IAM authenticates the caller; it does not scope the target. So the service
carries an allowlist in its own environment:

```
FN_DBOPS_ALLOWED_INSTANCES = "proj:us-east1:db-a,proj:us-east1:db-b"
```

It **fails closed** — empty refuses everything, and `*` is an explicit, logged opt-out.
The list is *derived from inventory* (`clouddb_dbops_service.onboarded_instances`) and
sorted, not accumulated, so a redeploy reconstructs it exactly and an unchanged fleet
produces no terraform diff. GCP `cloud-run` onboarding admits the instance **before** the
managed system is created, inline — doing it after would register a system whose very
first rotation is refused by our own service, a failure that reads like a permissions
problem and is not.

## 3. The audience is a recorded fact

`ps_resource_service._validate_dbgcp_dns_name` constrains this more than it first appears:
field 4 must be a **bare origin**, "because the audience doubles as the request target". So
a URL-shaped audience is used verbatim as the HTTP target, and an *opaque* one makes the
plugin fall back to `AppSettings:GcpCloudSql:DbOpsBaseUrl` — a per-broker config edit, on
every broker, forever. That is the thing to design out.

**Field 4 is the deployed service's own origin.** No custom audience is needed: Cloud Run's
default accepted audience *is* the service URL, and `--add-custom-audiences` exists to
*decouple* the two. When they are the same string there is nothing to decouple, and nothing
for an operator to invent.

Resolution order in `cloud_database_service._dbops_audience`:

1. The recorded `invoke_url` of the deployed `ps_dbops` function **in this database's own
   region**, reduced to `scheme://host`. Only an `available` service counts.
2. `clouddb_ps_gcp_dbops_audience` — unchanged, now an override for a BYO service, a custom
   domain, or a Private Service Connect front door.
3. Blank → onboarding stays off for that engine, exactly as before.

**The order is deliberately this way round.** The instinct is "explicit operator config
wins", and that instinct is the flat-key-outranks-per-region trap this repo has been bitten
by before. An operator sets the key for a `us-east1` service, later onboards a database in
`europe-west1` where the dashboard has deployed its own, and every rotation for that
database is addressed to a service that cannot reach the instance. The failure is a timeout
during a credential change — which §4 explains is the worst outcome this system produces. A
per-region *recorded fact* must beat a global *setting*.

The deploy is **two applies** for a real ordering reason: the audience is the service's own
URL, which does not exist until the first apply has finished. The second stamps
`FN_DBOPS_AUDIENCE`, which the container also uses to verify the `aud` claim it receives
(§6). In the window between, the service is deployed and refuses every request — visible,
safe, and better than a deploy that cannot be made at all.

## 4. Load-bearing deploy flags

Four `gcp_cloudrun` module variables were added; all default to today's behaviour, so
`db_grant` and every hand-deployed function plan byte-identically.

| Flag | Module variable | Set by `ps_dbops` |
|---|---|---|
| `--min-instances=1` | `min_instances` → `service_config.min_instance_count` | `1` |
| `--concurrency=8` | `concurrency` → `max_instance_request_concurrency` | `8` |
| `--no-allow-unauthenticated` | `auth_mode = "run_invoker"` (existing) | forced; `none` is **refused** at the click |
| `roles/run.invoker` for named brokers | `invoker_members` → `google_cloud_run_service_iam_member` `for_each` | from `clouddb_ps_gcp_dbops_invokers` |
| `--timeout=120`, `--max-instances=5` | existing `timeout_seconds`, `max_instances` | `120`, `5` |

`invoker_members` carries a Terraform `validation` that refuses `allUsers` and
`allAuthenticatedUsers` outright — an operator who genuinely wants a public function says
so with `auth_mode = "none"`, which is recorded on the row and visible in the UI. Empty is
allowed and means *nobody can call it yet*: a legitimate intermediate state, flagged on the
job rather than blocking the deploy.

`min-instances=1` is the one to defend, because it looks like a cost decision and is not.
Direct VPC egress documents connection-establishment delays over a minute on instance
start, and a rotation that times out **may already have applied the password change** —
Password Safe then holds a credential the database has replaced, and the account is locked
out until someone reconciles by hand. One warm instance per region removes that window. CPU
throttling stays at the default: the instance staying *alive* is what keeps the network
interface attached; it needs no CPU between requests.

## 5. The contract, implemented from the plugin repo's specification

`POST /v1/credential-op` version 1 is implemented. The source is the plugin repository's
own `PLAN-CloudRunSqlServer.md` — §2 for the envelope and the status/code table, §4
for the statement each operation runs — which states it implements `IDbOpsClient` as
already declared plugin-side in `Shared/Services/Transports.cs`. That is a written
specification from the other side of the wire, which is what the seam was waiting for; it
is not the same thing as a captured request, and the difference is recorded below.

Four operations: `verify`, `change`, `change-self`, `list-accounts`.

| Field | Handling |
|---|---|
| `contractVersion` | 1. An unserved integer is **501** naming what this build serves — that is what makes the address's `ver=` option usable. Garbage is **400**, because that is a malformed request, not a different service |
| `operation` | Hyphenated per the plan; `change_self`/`changeself`/`discover` and friends are accepted and **logged**, so an alias papering over a real contract difference is still visible |
| `engine` | `sqlserver` and `mysql`. The plan's service is SQL Server only; this one also serves MySQL because `clouddb_ps_gcp_channel` can be forced to `cloud-run` for every engine. `postgres` is refused by name |
| `privateIp` | **Required.** See below |
| `timeoutSeconds` | Bounds the login attempt, clamped to 5–300. The plugin is the party that knows how long it will wait |
| `passwordFormat` | `plaintext` only. `prehashed` is **422**, and the `HASHED` statement form is not built at all — see below |

**`privateIp` is required, and that is an IAM consequence rather than an omission.** The
plan keeps this service account deliberately tiny — none of `roles/cloudsql.client`,
`roles/cloudsql.instanceUser` or `roles/cloudsql.admin` — so there is no `instances.get`
available here and the address cannot be resolved from `instanceConnectionName`. The
plugin sends it (it did not always; that was one of two wire-contract gaps the plugin
author fixed). The 400 says all of this, because "privateIp is required" on its own reads
like a field somebody could make optional.

**`prehashed` is refused rather than flagged off.** The plan ships its verifier flag off
and gives the reason: a wrong salt length or SHA-512 framing produces an `ALTER` that
*succeeds* while leaving a login nobody can authenticate as — **after** Password Safe has
recorded the new password as authoritative. A flag whose only correct setting is "off" is
worse than an honest refusal, so the statement form does not exist in this module.

**Two things the specification does not settle, both marked `ASSUMPTION` in the code:**

1. **Which credential `change-self` authenticates as.** §4's statement is
   `ALTER LOGIN [target] … OLD_PASSWORD = N'old'`, and `OLD_PASSWORD` only lets a login
   change *its own* password — so the authenticating identity defaults to `targetUser`
   with `currentPassword`, and a request naming a different `loginUser` is refused here
   rather than sent. SQL Server would answer that with a permission error, and a
   permission error on a self-rotation is the most misleading failure this service could
   return.
2. **The `statementKind` vocabulary.** The plan gives one example value, `ALTER_LOGIN`.
   `CONNECT`, `ALTER_USER` and `LIST_LOGINS` are this module's, so a plugin that switches
   on that field is unproven.

`FN_DBOPS_CAPTURE` now defaults **off** — leaving it on would write a redacted request body
into Cloud Logging on every rotation forever, which is the opposite of this channel's whole
argument. Turn it on for exactly as long as it takes to compare one real request against
the parser:

```
gcloud run services update <service> --region <region> --set-env-vars FN_DBOPS_CAPTURE=1
```

and read it back with `jsonPayload.msg="dbops_contract_capture"`. A redeploy from the
dashboard puts it back to off.

Everything below the parser was already implemented and tested and still is:
`change_password_statements` (change, self-change with `OLD_PASSWORD`/`USER()`, per-engine
quoting), `_connect`, `_execute`, `change_password`, `verify_credential`, `check_instance`,
plus `list_accounts` and `server_version_statement` added with the contract. See
`tests/test_ps_dbops_workload.py`.

### Failure mapping

The plugin switches on `code`, so a wrong mapping does not mislabel a failure — it sends
the plugin down the wrong branch. `401 DB_AUTH_FAILED` in particular is, in the plan's own
words, *a legitimate Verify "false", not an infrastructure failure*.

One case is genuinely undecidable and is reported as such: SQL Server **15151** is
*"Cannot alter the login 'x', because it does not exist or you do not have permission"* —
one error for two causes, deliberately, so an unprivileged caller cannot enumerate logins
by probing. It maps to `403` and the `detail` says the other reading is possible and names
the `CustomerDbRootRole` grant, because of the two that is the one an operator can act on.

An unrecognised driver error becomes `500 DB_ERROR` rather than borrowing a documented
code. An unknown `code` degrades to "it failed", which is the only true statement
available; a borrowed one is an instruction to the plugin.

Nothing credential-shaped leaves the service. `detail` is scrubbed of every password in
the request before it is returned or logged — structurally, by substring replacement of
values this module already holds, not by pattern-matching hopefully. The response reports
`statementKind`, never the statement, which on a change carries the new password. And the
broad `except` around the operations is deliberate: an unclassified driver exception
escaping would be rendered as a 500 **with a traceback**, out of a credential-handling
path.

### The old open question, now closed

Whether the plugin sends a resolved host or expects the service to resolve
`project:region:instance` itself: it sends one. Had it been the other way, the service
account would have needed `sqladmin.instances.get` (`roles/cloudsql.viewer`) —
which does not contradict the docs' claim that it needs none of `cloudsql.client`,
`instanceUser` or `admin`, but is a fourth role nobody has mentioned.

Postgres is present in the statement layer and refused at `_connect` with a sentence naming
the reason: no Postgres driver is vendored, because Postgres has a channel that needs no
service at all. Adding `pg8000` to `_WORKLOAD_VENDOR` is the whole change if it is ever
wanted.

## 6. Authentication: the inner gate is replaced, not removed

`fnruntime.auth` verifies a dashboard-minted shared secret and fails closed. The plugin
puts a *Google OIDC ID token* in the same header, so that gate would 401 every request.

A workload may now declare a module-level `AUTH_MODE`; `dispatch` passes it to
`auth.verify_for`, and anything unrecognised — including a typo — is a 500, never a pass.
`ps_dbops` declares `gcp_oidc`, which checks `aud` against `FN_DBOPS_AUDIENCE` and `email`
against `FN_DBOPS_ALLOWED_INVOKERS`, keeping the three properties
`tests/test_function_auth.py` pins: fails closed when unconfigured, byte-identical 401s for
every failure mode, constant-time compares.

It does **not** re-verify the signature. Cloud Run's front door has already validated
signature, expiry and audience and confirmed the caller holds `roles/run.invoker`;
re-verifying would mean fetching and caching Google's JWKS from a runtime whose whole
contract is "stdlib only". The claims are parsed for *authorization* on top of an
*authentication* decision the platform already made — which is only sound while the
platform is making one. So the gate reads `FN_AUTH_MODE_FRONT_DOOR` (set by the module from
`auth_mode`) and fails closed on anything but `run_invoker`, as an **allowlist**: a missing
value means a deploy whose front door we cannot vouch for, and the safe reading of "I don't
know" is "no". `cloud_function_service._check_front_door` refuses such a deploy too, but the
two live in different trust domains and the container-side check is the load-bearing half.

A static test asserts `ps_dbops` is the **only** workload that opts out of the shared
secret, so a second one is a deliberate decision rather than a copy-paste.

## 7. Ingress: public + IAM, with the trade-off written down

`ingress_settings` was declared in the module and **never passed** by `_build_tf_variables`,
so every GCP function was `ALLOW_ALL` whatever the module said. It is wired through now, with
`clouddb_ps_gcp_dbops_ingress` (`all` | `internal`) defaulting to `all`.

`all`, because an **on-premises** Resource Broker cannot reach `ingress=internal`, and
on-prem brokers are the common case. The consequence, unsoftened: **this is a
credential-changing API on a globally resolvable endpoint, protected by IAM rather than by
network position.** Compensating controls, in order:

1. `constraints/iam.allowedPolicyMemberDomains` at the org, which makes an `allUsers`
   binding impossible — belt for the Terraform validation's braces.
2. The `invoker_members` validation, refusing the two wildcard principals.
3. The instance allowlist (§2), so a compromised broker cannot pivot to an unrelated
   instance.
4. Token `aud` and principal verification in the container (§6).

`internal` is offered for the all-in-GCP install. **Private Service Connect is the
follow-on**, and it is cheap to adopt precisely because §3 made the audience a recorded
fact: flipping the front door changes `FN_DBOPS_AUDIENCE`, the dashboard re-stamps, and
Password Safe never notices.

## 8. What was built

- `terraform/cloud_function/gcp_cloudrun/main.tf` — `min_instances`, `concurrency`,
  `invoker_members`, a validated `ingress_settings`, and `FN_AUTH_MODE_FRONT_DOOR`.
- `web_dashboard/functions/fnworkloads/ps_dbops.py` — the workload.
- `web_dashboard/functions/fnruntime/auth.py` / `dispatch.py` — `verify_gcp_oidc`,
  `verify_for`, and the `AUTH_MODE` hook.
- `web_dashboard/services/clouddb_dbops_service.py` — deploy orchestration, audience
  recording, allowlist derivation.
- `web_dashboard/services/cloud_function_service.py` — the new tf variables,
  `_check_front_door`, `ps_dbops` as GCP-only.
- `web_dashboard/services/cloud_database_service.py` — `_dbops_audience`, the loosened
  cloud-run gate, `_admit_instance_to_dbops`.
- `web_dashboard/api/cloud_databases.py` — `POST /api/databases/dbops/deploy`,
  `GET /api/databases/dbops/status`.
- `web_dashboard/jobs_worker.py` — `clouddb_dbops_deploy` (HEAVY: it drives two applies
  inline).
- `web_dashboard/config.py`, `api/setup.py`, `templates/settings.html` — four new keys and
  the panel that replaces "you deploy it".
- Tests: `tests/test_ps_dbops_workload.py`, `tests/test_clouddb_dbops_service.py`, and
  additions to `tests/test_function_auth.py`.

`scripts/sandbox/Linux/setup-gcp.sh` needed **no change**: `cloudbuild`, `run`,
`cloudfunctions` and `artifactregistry` are already enabled and granted. The build
capability was there all along.

## 9. Not done

- **The v1 parser** (§5) — blocked on a captured request, by design.
- **Postgres / MySQL on `cloud-run`** — only reachable by forcing
  `clouddb_ps_gcp_channel`. SQL Server is the engine with no alternative, and it needs no
  new driver.
- **Live E2E.** Nothing here has been run against a real Cloud SQL instance or a real
  Resource Broker. The health probe is the first thing to try.
- ~~**The `data-api` SQL Server address bugs**~~ — **done separately.** `iam=` is now
  suppressed for the one engine with no IAM database authentication, `fasecret=` is
  emitted with a **regional** secret version (`gcp_service.write_regional_secret`, never
  the Secrets-page backend, whose global form the Data API rejects outright), and
  `ps_resource_service` refuses both the global form and an address carrying `iam=` and
  `fasecret=` together. One predicate, `_iam_db_auth(engine, channel)`, now decides the
  instance flag, the functional account's database user and the address option — they
  drifted apart because they were three separate decisions.
