# Pairing an adapter with what it grants

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are deploying a db_grant or portainer_access adapter and want it paired with its database or Portainer rather than built by hand.

Part of [Cloud Functions](../functions.md).

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
