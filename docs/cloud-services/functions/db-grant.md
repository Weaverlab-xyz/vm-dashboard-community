# The db_grant workload

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want Entitle to grant and revoke database roles through a function inside your network.

Part of [Cloud Functions](../functions.md).

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
| `FN_DB_ADMIN_PASSWORD` | **never set directly** — pass it as `secret_environment` (see [Credentials](../functions.md#credentials)) |
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
resolves it as described under [Credentials](../functions.md#credentials). The older per-cloud
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

## One adapter, several databases

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
