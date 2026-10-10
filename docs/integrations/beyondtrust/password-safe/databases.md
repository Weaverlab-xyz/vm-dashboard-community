# Password Safe: databases

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are handing rotation of a database credential to Password Safe, or importing databases Password Safe already manages, and want the shared model before the per-cloud channel.

Part of [Password Safe](../password-safe.md). This is Layer 2 of the [Databases](../../../databases.md) stack.

Handing a **database** credential to Password Safe, so the vault owns the password and rotates it rather than this dashboard storing one. The three clouds reach a managed database three different ways, and the way in decides the setup, so each has its own page:

| Page | Read it when |
|---|---|
| [AWS: `dbssm`](databases-aws.md) | your database is on RDS, reached through the ECS gateway host over AWS Systems Manager |
| [Azure: `dbazure`](databases-azure.md) | your database is Azure Flexible Server or SQL Database, reached through a jump VM over Azure Run Command |
| [GCP: `dbgcp`](databases-gcp.md) | your database is Cloud SQL, rotated over the Data API with no jump host |

This page holds what the three share: what the dashboard does, the two ways to ask for it, where the functional account comes from, and importing databases Password Safe already manages.

## What the dashboard does

The dashboard also provisions **managed cloud databases** (AWS / Azure / GCP / OCI), reaches
them through a PRA protocol tunnel, and can optionally **onboard AWS, Azure and GCP databases
into Password Safe** for credential rotation (via the `{engine} SSM Custom Plugin`,
`{engine} Azure Run Command Plugin` or `GCP Cloud SQL {engine}` plugin, and the shared
`PRA Vault Username Password` plugin). GCP needs no jump host on either of its channels:
PostgreSQL and MySQL go over Google's control plane (the Cloud SQL Data API), and SQL
Server through a small Cloud Run service you deploy. It ships off — every channel is
implemented plugin-side now, but none has been exercised against a live Cloud SQL
instance. That whole feature — base provisioning, per-cloud
prerequisites, and the Password Safe onboarding — is documented separately in
**[Databases](../../../databases.md)**. The tunnel half needs
[Privileged Remote Access](../privileged-remote-access.md).

Onboarding is asked for **per database**, in one of two places: the **Onboard into Password
Safe** checkbox on the Provision form (ticked by default once `clouddb_ps_onboarding_enabled`
is on), or the row's **Register in Password Safe** action afterwards — which is how a
database built before Password Safe was configured gets onboarded without being rebuilt.
Registering after the fact **re-brokers the PRA tunnel** so it injects the rotatable managed
user rather than the master admin; see
[Databases → Two ways in](#two-ways-in-at-provision-or-afterwards).

The dashboard can also **register** a database it did not create — on-premises or in a cloud —
so it can be a Configuration Management target. That path has no tunnel and no onboarding: its
admin login is a Password Safe **managed account**, checked out just-in-time per run and never
stored, so the database has to be onboarded in Password Safe *before* it can be registered.

## Two ways in: at provision, or afterwards

| | Where | What it does |
|---|---|---|
| **At provision** | The **Onboard into Password Safe** checkbox on the Provision form. Starts ticked when `clouddb_ps_onboarding_enabled` is on. | Onboards as part of the provisioning job, interleaved with the apply. Clearing it skips the onboarding for that database. (It does not skip the separate legacy admin-credential staging, which is on its own `pscli_*` gate.) |
| **Afterwards** | The row's **Register in Password Safe** action (and **Remove** to undo it). | The same three steps against a database that already exists. Async — enqueues a `clouddb_ps_register` job; watch it in Jobs. |

The row action is what you want for a database provisioned before Password Safe was
configured. It is offered only for a **dashboard-provisioned AWS, Azure or GCP** database that
is `available` and not already onboarded — a *registered* database has no admin credential
stored here to create the managed user with, so onboard it in Password Safe directly and
use **Import from Password Safe** instead.

> **The row action re-brokers the PRA tunnel.** Password Safe rotates the *managed user*,
> and the PRA Vault mirror pushes each rotation into the vaulted credential the tunnel
> injects — so that credential has to be the managed user, not the master admin. The
> existing jump and Vault account are **destroyed first** and a new pair brokered (the
> `sra` provider has no import, so re-brokering without the destroy would strand them).
> Any open session drops. If the re-broker then fails the job fails loudly, saying the
> database currently has no tunnel — run the action again once the provider error is
> fixed.
>
> **Remove leaves the managed database user in place.** Unlike a decommission the database
> is still there, and that user is what the tunnel injects. The job log says so; drop it by
> hand if you want the database back on its admin login.

> **Setting this up or testing the plugins?**
> [docs/runbooks/clouddb-password-safe-plugin-setup.md](../../../runbooks/clouddb-password-safe-plugin-setup.md)
> is the field-by-field operator runbook: prerequisites, exactly what to put in each
> settings-panel field, the test procedure, and how to read a failure.

All three paths create a **dedicated managed DB user** as the rotation target (not the
master admin), point the PRA tunnel's injected credential at it, onboard the DB as a
Password Safe **managed system + managed account**, and onboard the PRA Vault account on the
**`PRA Vault Username Password`** plugin so rotations propagate into the tunnel credential.
Failures leave the database up, but they are reported differently. A failure in the
*managed-user creation* falls back to legacy admin-credential staging and appends a
`Password Safe managed-user creation failed …` line to the job log — the tunnel keeps the
admin credential and the job stays green. A failure in *managed-system onboarding* has no
fallback and **fails the job**: the error message carries the cause and the remedy. The
database row stays `available` either way, so the row's **Register in Password Safe**
action finishes the onboarding without re-provisioning.

## Where the functional account comes from

`clouddb_ps_functional_account_mode` picks one of two contracts:

| Mode | Functional account | Panel holds |
|---|---|---|
| **`reference`** | **Operator-created.** Resolved by name, never created, **never deleted on decommission** — the same contract as `passwordsafe_vm_functional_account_*` (VM) and `k8s_ps_functional_account_*` (k8s). The managed system inherits **its** platform, so the `clouddb_ps_platform_*` names become advisory. | the account name |
| `create` (default) | **Dashboard-created**, one per database, deleted on decommission. | the credential material packed into it |

`reference` keeps the static cloud credential — the IAM access key, the Azure client
secret — out of the dashboard's config store entirely. It needs **one account per engine
per cloud**, because a functional account belongs to a platform. A blank name in
`reference` mode is an error, not a fall-through to `create`.

**The mode resolves on four rungs**, most specific first:
`clouddb_ps_functional_account_mode_<cloud>_<engine>` → `..._mode_<engine>` →
`..._mode_<cloud>` → `clouddb_ps_functional_account_mode`. Blank falls through on every
one of them, so a key you have not set can never outrank one you have. Only
`gcp_sqlserver` exists on the cloud+engine rung: it is the single cell the coarser rungs
cannot express, because `..._mode_sqlserver` governs AWS and Azure SQL Server too and
those want the opposite answer — see
[Password Safe rotation for Cloud SQL](databases-gcp.md).

It also needs `clouddb_ps_self_rotation` on: the account it names is unprivileged on the
database, so only the plugin's self-rotate action can change a credential. Its DB login must
still exist on each managed server for *Verify Functional Account* to pass — the dashboard
creates only the managed user.

Decommissioning deregisters both managed systems and, in `create` mode only, deletes both
functional accounts before the instance is destroyed (the managed DB user goes with it).

> **Password sync note (both clouds).** The dashboard registers both managed systems, but
> making Password Safe *propagate* a DB rotation into the PRA Vault managed account may
> require a Password Safe **SmartRule / linked-account** configuration the Terraform
> provider cannot express — set that up in Password Safe if your policy requires the two to
> move together.

The two custom plugins per cloud (and the shared PRA Vault plugin) are manual `.PSPLUGIN`
uploads in BeyondInsight → **Configuration → Privileged Access Management → Platform
Plugins**; plugin internals are documented in the Beekeeper articles. Set the platform-name
config keys to match what you uploaded.


**Every platform, functional-account and mode key**, so the patterns above can be searched by
name. Platform keys must match the platform names of the plugins you uploaded;
functional-account keys are read only in `reference` mode.

| | PostgreSQL | MySQL | SQL Server |
|---|---|---|---|
| **AWS platform** | `clouddb_ps_platform_postgres` (`psql SSM Custom Plugin`) | `clouddb_ps_platform_mysql` (`mysql SSM Custom Plugin`) | `clouddb_ps_platform_sqlserver` (`mssql SSM Custom Plugin`) |
| **AWS functional account** | `clouddb_ps_functional_account_postgres` | `clouddb_ps_functional_account_mysql` | `clouddb_ps_functional_account_sqlserver` |
| **Azure platform** | `clouddb_ps_platform_azure_postgres` (`PostgreSQL Azure Run Command Plugin`) | `clouddb_ps_platform_azure_mysql` (`MySQL Azure Run Command Plugin`) | `clouddb_ps_platform_azure_sqlserver` (`MSSQL Azure Run Command Plugin`) |
| **Azure functional account** | `clouddb_ps_functional_account_azure_postgres` | `clouddb_ps_functional_account_azure_mysql` | `clouddb_ps_functional_account_azure_sqlserver` |
| **GCP platform** | `clouddb_ps_platform_gcp_postgres` (`GCP Cloud SQL PostgreSQL`) | `clouddb_ps_platform_gcp_mysql` (`GCP Cloud SQL MySQL`) | `clouddb_ps_platform_gcp_sqlserver` (`GCP Cloud SQL SQL Server`) |
| **GCP functional account** | `clouddb_ps_functional_account_gcp_postgres` | `clouddb_ps_functional_account_gcp_mysql` | `clouddb_ps_functional_account_gcp_sqlserver` |
| **Mode, per engine** | `clouddb_ps_functional_account_mode_postgres` | `clouddb_ps_functional_account_mode_mysql` | `clouddb_ps_functional_account_mode_sqlserver` |

The mode's other rungs are `clouddb_ps_functional_account_mode_aws`,
`clouddb_ps_functional_account_mode_azure` and `clouddb_ps_functional_account_mode_gcp` (per cloud), `clouddb_ps_functional_account_mode_gcp_sqlserver` (the one cloud+engine
cell), and `clouddb_ps_functional_account_mode` (global, default `create`). Every rung but
the global one is blank by default, which means "fall through".

## Importing databases from Password Safe

Since Password Safe's own discovery scanner already found and onboarded these databases —
with managed credentials, so it knows the platform, port, instance and accounts — the
Databases page can read that inventory directly instead of asking an operator to retype it.
**Databases** → **Import from Password Safe** lists the candidates and registers the ones you
tick. It **reads only**; nothing in Password Safe is created or changed.

Two things worth knowing here rather than in the feature doc:

- **This path uses the public REST API, not `ps-cli`.** It reads `Platforms`,
  `ManagedSystems`, `Databases` and `ManagedAccounts` over HTTPS with the same
  `pscli_api_url` / `pscli_client_id` / `pscli_client_secret` OAuth client configured in
  [Step 1](../password-safe.md#step-1--password-safe-oauth-application-ps-cli), so it works in an image with no
  `ps-cli` binary. The run-time credential *checkout* still goes through `ps-cli`.
- **The account list comes from the accounts the API identity can `request`.** That is the
  same permission surface the checkout uses, so a missing **Requestor** role or Smart Rule
  shows up as a greyed-out candidate instead of a `4031, statuscode: 403` in a worker log
  hours later.

Configuration keys (Settings → Integrations → Password Safe → *Database Import*):
`clouddb_ps_import_workgroup`, `clouddb_ps_import_default_cloud`,
`clouddb_ps_import_max_systems`, `clouddb_ps_import_platform_map`. All optional and all
documented in **[Databases → Importing from Password Safe](../../../databases.md#importing-from-password-safe)**.

---
