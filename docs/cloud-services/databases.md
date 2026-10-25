# Databases

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are standing up a managed database, or want to manage one you already run.

The dashboard manages databases two ways — **provision** a new managed database in a
cloud, or **register** one that already exists, on-premises included — and layers the
BeyondTrust PAM stack on top of what it provisions. Provisioning needs a Terraform
module, so it is cloud-only; registering needs only somewhere to reach.

The provisioned path is **provisioning + three stacked layers**, each solving a different
privileged-access problem (the same model used across the [Cloud VMs](../clouds/vms.md) and
[Kubernetes](kubernetes.md) docs):

- **Provisioning** — stand up a **private** database (AWS RDS / Azure Flexible Server +
  SQL DB / GCP Cloud SQL / OCI Autonomous DB, plus MongoDB on Atlas). The dashboard mints the admin credential
  and stores it encrypted.
- **Layer 1 — PRA** *(reach it)* — a BeyondTrust Privileged Remote Access protocol
  tunnel brokers private access to the DB; the admin credential is vaulted in PRA for
  injection. This is what makes the private database usable.
- **Layer 2 — Password Safe** *(manage its secrets)* — *optional.* Password Safe owns
  rotation of a dedicated managed DB user and keeps the PRA-vaulted credential in sync.
- **Layer 3 — Entitle** *(grant time-boxed access)* — *optional.* Onboard the DB as an
  Entitle integration so users request just-in-time access; Entitle mints **ephemeral
  accounts** (or assigns persistent roles) per engine.

The other path is [registration](databases/register.md): record a database
the dashboard did **not** create — on-premises (`cloud = local`) or in a cloud — so it
can be a [Configuration Management](../operations/config-management.md) target. Nothing is built and no
credential is stored; the admin login is a Password Safe **managed account** checked out
at run time. The fastest way to register is
[**Import from Password Safe**](databases/import.md), which lists what Password
Safe already discovered instead of asking you to type it.

The layers stack — stop after Provisioning + PRA, or add Password Safe and/or Entitle.
**They apply to what the dashboard provisions**: a registered database gets none of them,
because there is no Terraform state to tear down and no credential to vault. That is the
trade for being able to point at a database you already run. Provisioning coverage
differs by cloud/engine:

| Cloud | Provisioning | L1 PRA | L2 Password Safe | L3 Entitle |
|---|---|---|---|---|
| **AWS** | postgres / mysql / sqlserver / oracle (RDS) + mongodb (Atlas) | ✅ tunnel | ✅ `dbssm`; oracle on the native platform (not mongodb) | ✅ register + JIT |
| **Azure** | postgres / mysql (Flexible Server) + sqlserver (SQL DB + Private Endpoint) + mongodb (Atlas) | ✅ tunnel | ✅ `dbazure` | ✅ register + JIT |
| **GCP** | postgres / mysql / sqlserver (Cloud SQL, private IP) + mongodb (Atlas) | ✅ tunnel | ⚠️ `dbgcp` — sqlserver via Cloud Run; postgres / mysql inert until the Data API channel ships | ✅ postgres / mysql (via forwarder) |
| **OCI** | ⚠️ **oracle only** (Autonomous DB) — read the caveats² | ✅ tunnel¹ | ❌ | ⚠️ register (Oracle connector, unverified live) |

**MongoDB** is provisioned on **MongoDB Atlas**, on whichever of AWS / Azure / GCP the row
names, and can also be [registered](databases/register.md) when self-hosted. See
[MongoDB Atlas](databases/provisioning.md#mongodb-atlas). Entitle covers Atlas clusters only — see
[Layer 3](#layer-3--entitle-just-in-time-access).

¹ OCI has no dashboard-provisioned gateway — you supply your own (see the OCI section).
² The OCI module only started shipping in the image recently and has **never completed a live
run**; it also always provisions into `oci_region`. See [OCI](databases/provisioning.md#oci-autonomous-database--read-the-caveats).

Everything is driven by Terraform from the job worker; deploy state is written to the
active [storage backend](../operations/storage-management.md).

---

## Architecture

*This section describes a **provisioned** database. A registered one is reached however
you already reach it — see [Registering an existing database](databases/register.md).*

The database is **private** (`publicly_accessible = false`, or a public free-tier
Autonomous DB on OCI). The dashboard's backend has **no direct network path to it** — the
only way in is a BeyondTrust PRA protocol tunnel brokered through a shared, on-demand
**gateway host** that sits in (or peers into) the database's network.

```
  operator / app                dashboard backend (worker)
        │                                 │  terraform apply (db module)
        │  PRA client                     │  terraform apply (beyondtrust/sra: tunnel + Vault account)
        ▼                                 ▼
  ┌───────────┐   PRA protocol tunnel   ┌────────────────┐   private net   ┌───────────────┐
  │    PRA    │◄───────────────────────►│  Gateway host  │◄───────────────►│  managed DB   │
  │ appliance │      (jump item)        │  (per cloud)   │   :5432/3306/   │  (private)    │
  └───────────┘                         └────────────────┘   1433/1521     └───────────────┘
```

The gateway host differs per cloud (Fargate/ACI can't do protocol tunneling, so AWS
uses EC2 and Azure a VM):

| Cloud | Gateway host | Provisioned by the dashboard? |
|---|---|---|
| AWS | ECS-on-EC2 container instance (`bt-jumpoint` cluster) | ✅ on demand, ref-counted |
| Azure | privileged gateway container on an Azure VM (`clouddb-jumpoint`) | ✅ on demand, ref-counted |
| GCP | privileged gateway container on a Container-Optimized-OS GCE VM | ✅ on demand, ref-counted |
| OCI | compute instance in the VCN public subnet | ❌ **operator must pre-create it** (see OCI below) |

Per-engine tunnel resource (`beyondtrust/sra` provider, in
[terraform_pra_service.py](../../web_dashboard/services/terraform_pra_service.py)):

| Engine | Tunnel resource | Note |
|---|---|---|
| postgres | `sra_postgresql_tunnel_jump` | proxies cleartext wire protocol |
| mysql | `sra_my_sql_tunnel_jump` | proxies cleartext wire protocol |
| sqlserver | `sra_protocol_tunnel_jump` (`tunnel_type=mssql`) | TDS-aware; does its own backend TLS |
| oracle | `sra_protocol_tunnel_jump` (`tunnel_type=tcp`) | raw TCP port forward to the SQL*Net listener |
| mongodb | `sra_protocol_tunnel_jump` (`tunnel_type=tcp`) | raw TCP port forward to one `mongod` |

A `tcp` tunnel is a **port forward**, not a protocol-aware tunnel: the dashboard sends
`tunnel_definitions = "<port>;<port>"` and `tunnel_listen_address = "127.0.0.1"`, so the
client connects to `127.0.0.1` on the database's own port, and no username or database is
attached to the jump. PRA 26.3 adds dedicated Oracle and MongoDB tunnels (PRA already has a
MongoDB tunnel with an **Auth Source** field); the dashboard moves to them once the
`beyondtrust/sra` provider ships resources — its `tunnel_type` validator accepts only `tcp`
and `mssql` today. For MongoDB, connect with `directConnection=true` (the tunnel reaches one
server, not the replica set's SRV name) and `tlsAllowInvalidHostnames=true` (127.0.0.1 is
never the name on the server's certificate).

Because the Postgres/MySQL tunnels proxy **cleartext**, the DB is provisioned with TLS
made optional on the server side (`rds.force_ssl=0` / `require_secure_transport=OFF` /
Cloud SQL `ssl_mode=ALLOW_UNENCRYPTED_AND_ENCRYPTED`). SQL Server and Oracle keep TLS on
because their tunnels terminate/forward TLS themselves. Oracle and MongoDB tunnels forward
bytes, so the database's own TLS passes straight through to the client.

### The database name

The **Name** you give a database on the Provision form is slugified into the catalog
Terraform creates (`My App DB` → `my_app_db`; Oracle instead gets a deterministic
`adb…` name, capped at ADB's 14 characters). That catalog is recorded on the row, so
the **Database** column on the Databases page and the `database:` line in the
**Connection** dialog show it without depending on the provisioning job still existing —
which also gives the [Entitle cloud-function adapter](functions.md)
something to scope its grants to (`FN_DB_NAME`).

Two engines read differently:

- **SQL Server** — you connect to the `master` system database. On RDS that is *all*
  there is (the module omits `db_name`; see [AWS](databases/provisioning.md#aws-rds) below), so the column shows
  `master`. On Azure/GCP a real catalog is created as well, and the page shows both —
  the catalog, with `via master` beneath it.
- **Registered** databases show whatever was recorded at registration, `(not set)` if
  nothing was. SQL Server falls back to `master`.

---

## Pages

The tasks have a page each:

| Page | Read it when |
|---|---|
| [Provisioning a cloud database](databases/provisioning.md) | you are standing up a managed database on AWS, Azure, GCP, OCI or MongoDB Atlas, and want each engine's and each cloud's specifics |
| [Registering an existing database](databases/register.md) | you already run a database, in a cloud or on-premises, and want the dashboard to manage access to it |
| [Importing databases from Password Safe](databases/import.md) | the databases are already managed systems in Password Safe and you want them here without typing each one in |
| [Troubleshooting cloud databases](databases/troubleshooting.md) | a database will not provision, register, onboard into Password Safe or open through PRA |

---

## Layer 1 — PRA access (shared prerequisites, all clouds)

Before any **provisioned** database will get a working tunnel, configure PRA once under
**Settings → Integrations → Privileged Remote Access**:

- **PRA appliance + OAuth API account** → `bt_api_host`, `bt_client_id`,
  `bt_client_secret` (used by the SRA Terraform provider to create the tunnel).
- A **pre-existing Asset Group** and **Gateway** in PRA → `bt_jump_group_name`,
  `bt_jumpoint_name`. The dashboard does *not* create these.
- The **Gateway Docker deploy key**, pasted in (config key is per cloud — see each
  section). Without it the shared gateway host can't start and the tunnel shows
  *Unavailable* in PRA.

Each provision can override the Asset Group / Gateway / PRA credential per database
(the `jump_group`, `jumpoint_name`, `pra_credential_ref` form fields); otherwise the
`bt_*` defaults apply. (The per-cloud `*_bt_jump_group_name` / `*_jumpoint_name` keys
are for PRA *Shell Jumps*, not the DB tunnel.)

Also enable the **Databases** feature toggle (`cloud_database_enabled`) — it gates
registration as well as provisioning.

---

## Layer 2 — Password Safe (AWS + Azure + GCP)

*Optional.* When enabled (`clouddb_ps_onboarding_enabled`), provisioning an **AWS**,
**Azure** or **GCP** database additionally hands rotation of a database credential to
Password Safe and keeps the PRA-vaulted credential in sync. **OCI is not supported** —
those databases provision and get a tunnel, but no Password Safe onboarding. GCP covers
**PostgreSQL and MySQL only**, and ships off — see that section for both reasons.

The mechanism, the functional-account rules and the per-cloud setup are under Password Safe:

| Channel | Page |
|---|---|
| The shared model: the two ways in, the functional account, importing | [Password Safe: databases](../beyondtrust/password-safe/databases.md) |
| AWS (`dbssm`, Systems Manager on the ECS gateway host) | [Password Safe: AWS databases](../beyondtrust/password-safe/databases/aws.md) |
| Azure (`dbazure`, Run Command on the jump VM) | [Password Safe: Azure databases](../beyondtrust/password-safe/databases/azure.md) |
| GCP (`dbgcp`, Cloud SQL Data API) | [Password Safe: GCP Cloud SQL](../beyondtrust/password-safe/databases/gcp.md) |

### Oracle on RDS: the native platform

Oracle uses **no plugin**. It onboards on Password Safe's own **Oracle** platform, and the
**Resource Broker serving the asset's workgroup** connects to the listener itself — so that
broker must be able to reach the database on **1521**. The dashboard creates the Password
Safe objects; the network path from the broker is yours.

1. **Managed user.** Over SSM to the shared gateway host, the dashboard runs sqlplus (Oracle's
   Instant Client image, `ghcr.io/oracle/oraclelinux9-instantclient:23`, overridable with
   `clouddb_db_client_image_oracle`) and creates or resets `psafe_<id>` with `CREATE SESSION`
   only.
2. **Functional account.** The RDS master user, minted on the Oracle platform per database
   (`create` mode). In `reference` mode, `clouddb_ps_functional_account_native_oracle` names
   an account you created on that platform.
3. **Objects.** An **asset** (the endpoint's DNS name and the IP it resolves to — an RDS
   endpoint resolves to its private address from anywhere), a **database** on it (platform,
   the PDB service name, port 1521), a **managed system** over that database, and the
   **managed account** — which rotates itself (`use_own_credentials`), so the functional
   account never alters anyone else. A failed apply rolls back what it created.
4. **PRA Vault sync** works as for every other engine: the tunnel's vaulted credential is the
   managed user, mirrored on the PRA Vault platform.

| Key | Default | Notes |
|---|---|---|
| `clouddb_ps_native_workgroup` | — | Workgroup whose Resource Broker reaches the DB; blank → `clouddb_ps_workgroup` |
| `clouddb_ps_platform_native_oracle` | `Oracle` | The built-in platform's name in your tenant |
| `clouddb_ps_functional_account_native_oracle` | — | `reference` mode only |

**Not covered:** OCI Autonomous DB (no way here to run SQL on it to create the managed user)
and **MongoDB Atlas** (Atlas changes database users only through its Admin API, which no
Password Safe platform uses; a custom plugin is the path).

## Layer 3 — Entitle (just-in-time access)

*Optional.* Onboard a **provisioned** database into BeyondTrust **Entitle** as its own
integration, so users request **just-in-time** access to it instead of holding a standing
credential. Gated by `entitle_registration_enabled` plus a per-provision
**"Register in Entitle"** toggle, and there is a post-provision **Register in Entitle**
button (job `clouddb_entitle_register`) for a database provisioned earlier — plus, for
MySQL and SQL Server, a **Function (DB grant)** button that deploys the adapter those two
engines need (see [below](#mysql-and-sql-server-the-db_grant-adapter)). This layer
needs the provisioning job's admin credential, so a
[registered](databases/register.md) database is not offerable — the button is
hidden and the API refuses it with a 400. Teardown
deregisters on decommission. Full Entitle setup (owner, workflow,
durations, the agent) lives in the [Entitle integration](../beyondtrust/entitle.md) doc.

The account model is **per engine**:

| Engine | Entitle account model | Notes |
|---|---|---|
| **PostgreSQL** | **Ephemeral (JIT) accounts** — *proven* | Entitle mints a short-lived role per grant. The connector config uses `user` (not `username`) + a required `options{}` block, no top-level `database`. |
| **SQL Server** | Ephemeral accounts | **Only on Entitle-viable providers** — Azure SQL Managed Instance / AWS RDS Custom. Managed Cloud SQL / RDS-standard / Azure SQL Database are refused (`_entitle_viable`) because the connector needs sysadmin/CONTROL SERVER they can't grant. Requires a `version` field (default `2019`, `entitle_sqlserver_version`). |
| **MySQL** | **Persistent roles** (not ephemeral) | Entitle's MySQL connector assigns persistent roles rather than minting accounts. |
| **Oracle** | Ephemeral accounts — *not yet proven live* | Entitle's "Oracle Database" connector. Its config says `username` (not `user`) and needs `service_name`, which must name a **PDB** — Entitle manages pluggable databases only. Autonomous DB sends `protocol = "tcps"` (its no-wallet listener is TLS-only). The connector wants a SYSDBA or DBA-role account. On **RDS** the default `oracle-se2-cdb` engine makes the service a PDB, and the master user is expected (not yet verified live) to carry the DBA role it needs. |
| **MongoDB (Atlas)** | Ephemeral database users — *not yet proven live* | Entitle's **Atlas MongoDB** integration, an API-key integration scoped to the cluster's own Atlas project: `public_key` / `private_key` / `project_id` plus `options.connect_to_clusters`. It uses a **separate** Atlas programmatic API key with Organization Owner (Settings → Entitle → Atlas MongoDB connector), not the provisioning service account. Entitle's egress IPs must be on that key's API access list (set once in Atlas) and on each cluster's access list (`atlas_extra_access_cidrs`). No Entitle agent is used. Self-hosted MongoDB is refused: there is no database-login Mongo connector. |

**Reachability.** Because dashboard DBs are private, Entitle reaches them through the
**shared Entitle agent** (`entitle_agent_token_name`; provisioned on Kubernetes, one per
VPC) — registration raises if it isn't configured for a private target. **AWS RDS** is
reachable directly from the agent; **GCP Cloud SQL** is not (the agent's GKE VPC can't
reach Cloud SQL's private IP over non-transitive peering), so the dashboard stands up an
on-demand **socat forwarder** in the sandbox VPC and points Entitle at it — enable it with
`gcp_entitle_db_proxy_enabled`. The forwarder is placed in the **database's own region**
(zone and `db_network` from that region's config set), because the Cloud SQL private IP it
relays to is only reachable from there.

> Entitle here is independent of Password Safe (Layer 2): a DB can be registered in Entitle
> whether or not Password Safe manages its credential. The two solve different problems —
> Entitle governs *who gets in and for how long*; Password Safe governs *the credential's
> lifecycle*.

### MySQL and SQL Server: the db_grant adapter

Two of the three engines above cannot do just-in-time accounts through Entitle's *native*
connector at all — MySQL's assigns persistent roles and never mints one, and managed SQL
Server never grants the sysadmin/CONTROL SERVER its connector needs. For those, the
dashboard deploys a **`db_grant` Cloud Function adapter** beside the database, which
implements Entitle's Remote Adapter contract and runs the SQL itself from inside the
VPC/VNet.

The row's **Function (DB grant)** action deploys one (job `clouddb_adapter_pair`, gated by
`cloud_functions_enabled` plus the `cloud_function:write` permission). Everything is
derived from the database row — its cloud, its region, a name from its id, its
host/port/catalog, and its admin credential staged as a *reference* into the cloud's own
secret store. **The function lands in the database's own region**, which it must: it
reaches a private endpoint over the VPC, and subnets are regional. Functions are regional
resources, so there is no zone to match; a region with no per-region config set is refused
rather than deployed onto the default region's network.

Unlike the provision-time pairing, the row action deploys the adapter ready to act
(`FN_DB_DRY_RUN=0`). **That does not mean the adapter creates accounts** — it initiates
nothing. It is an HTTP endpoint behind a fail-closed bearer gate that only the Entitle
integration holds, it schedules nothing, and it only ever touches accounts it minted
itself. **Entitle owns the lifecycle:** an account is created when a request is approved
and dropped when the access ends. Dry run is therefore not the safer setting for a working
integration — it makes an *approved* request silently do nothing. Once one exists the action becomes a **DB grant ✓** badge; delete the
function on the Cloud Functions page to redeploy. The action is hidden for a database that
cannot take an adapter: Postgres and Oracle (the native connector works), a *registered*
database (no stored admin credential), an on-premises one (no cloud secret store), and one
with no recorded catalog to scope grants to. Full detail in
[Cloud Functions](functions.md).
---

## Workstreams (who can see a database)

A database with **no workstream** is visible only to whoever created it, plus
administrators. That is how every database provisioned or registered before this field
existed still behaves, and it is why adding the field granted and revoked nothing.

Assign one and the whole workstream can see and manage it:

- **At creation** — the **Workstream** select on the provision form and on *Register
  existing*. Optional; blank leaves the row creator-scoped. You may only pick a workstream
  you are a member of, because the workstream rule outranks the creator rule: tagging your
  own database into a workstream you are not in would hide it from you.
- **Afterwards** — the **Workstream** button on the row, administrators only. This is the
  only way an existing database gets one. Clearing it returns the database to its creator.

Retagging is admin-only because it is a **transfer**, not a filter. Everyone in the chosen
workgroup gains the row in `/api/databases`, every by-id action on it (connection details,
decommission, Entitle and Password Safe registration), the ability to run
[Config Management](../operations/config-management.md) against it, and the ability to change its
[auto-delete timer](../operations/auto-delete-timer.md). The person who loses it is not in the dialog.

> **Not the same as a Password Safe workgroup.** Password Safe has its own, unrelated
> notion of a workgroup, which this page also mentions: `clouddb_ps_import_workgroup`
> narrows the *import* candidate list (see [Importing from Password Safe](databases/import.md)).
> That one names a container in Password Safe. This one names a set of dashboard users.
> They are never the same value and neither is derived from the other.

See [Permissions](../admin/permissions.md) for how workgroups sit alongside permission scopes.

---

## Lifecycle (provision, register, decommission)

- **Provision:** from the Databases page, pick engine + cloud + region and (when
  PRA is configured) an Asset Group / Gateway. The record + admin credential are created
  synchronously; the `terraform apply`, tunnel brokering, and any Password Safe onboarding
  run in the **job worker** as a background job.
- **Register:** from the Databases page, **Register existing** → engine, location, host and
  a Password Safe managed account. The row is created synchronously with status
  `available`; there is no job, because there is nothing to build.
- **Deregister:** removing a registered row deletes the row only — no Terraform, no PRA or
  Password Safe teardown, because there was never anything to tear down.
- **Decommission:** tears down the PRA tunnel + Vault account, any Layer-2 Password Safe
  managed systems + functional accounts, any Layer-3 Entitle integration (+ the GCP
  forwarder), the database instance, and then the **JIT adapter** — its Cloud Function,
  that function's Entitle integration, and the admin credential staged for it in the
  cloud's own secret store (`dashboard-clouddb-<id>-admin` on GCP, `dashboard/clouddb-…`
  in AWS Secrets Manager, `clouddb-…` in Key Vault). Errors accumulate rather than being
  swallowed, so an orphaned tunnel/vault/instance is visible. A leftover **credential**
  fails the job — it is a live database admin password — while a leftover adapter
  **function** is reported as a warning, since it holds no credential and has its own
  **Delete** on the Cloud Functions page. A database that was never paired has nothing to
  retire and records neither.

---

