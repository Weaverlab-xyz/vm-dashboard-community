# Importing databases from Password Safe

> **Audience:** operator · **Profile:** `demo` · **Read this when:** the databases are already managed systems in Password Safe and you want them here without typing each one in.

Part of [Databases](../databases.md).

Password Safe already runs a discovery scanner, and it scans **with managed
credentials** — so it knows a database's platform, port, instance and accounts, not just
that something is listening. Importing reads that inventory and registers the rows you
pick. It is the same outcome as registering by hand, with nothing to type.

**It only reads.** Nothing in Password Safe is created, changed or deleted.

**How to import.** **Databases** page → **Import from Password Safe**, or
`GET /api/databases/ps-candidates` + `POST /api/databases/ps-import`. Both need
`cloud_database:read`/`write` **and** `secrets:use` — listing every managed system and
account in the tenant is strictly more than the host-scoped lookup the run form uses, and
importing pins a managed account for later checkout. The button renders only when
`password_safe_enabled` is on.

**What it reads.** Four collections over the Password Safe **public REST API** — no
`ps-cli` binary needed, so it works in a container that doesn't ship one:

| Collection | Used for |
|---|---|
| `Platforms` | platform name → engine |
| `ManagedSystems` | the candidate rows: name, host, port, workgroup |
| `Databases` | port, instance name and version enrichment |
| `ManagedAccounts` | the accounts this API identity can **request** |

Reading accounts from the *requestable* list is the point: it is the same permission
surface the run-time checkout uses, so a missing Requestor role shows up as a greyed-out
row **now** rather than as a failed playbook run later. See the blockquote below.

**Platform → engine.** Matched on substrings of the Password Safe platform name and short
name, first hit wins: `sqlserver` (`ms sql`, `mssql`, `sql server`), `postgres`
(`postgresql`, `postgres`, `psql`, `greenplum`), `mysql` (`mysql`, `mariadb` — MariaDB
registers as mysql), `oracle` (`oracle`, `oradb`), `mongodb` (`mongo`). `mysql` is matched before `oracle` so
"Oracle MySQL" resolves correctly. A platform that maps to nothing is **shown and greyed
out**, never silently dropped — otherwise "my database isn't in the list" is
undiagnosable. Add your own with `clouddb_ps_import_platform_map`.

**Why a row is greyed out.** Eligibility is decided server-side and the reason travels
with the row, so the list never offers something the import would refuse:

| Reason | Fix |
|---|---|
| already registered in the dashboard | nothing to do — it's here already |
| onboarded by this dashboard's own cloud-DB Password Safe integration | nothing to do — it's managed here |
| the platform isn't a database engine the dashboard supports | add a `clouddb_ps_import_platform_map` entry, if it really is one |
| no DNS name, host name or IP address | fix the managed system in Password Safe |
| no requestable Password Safe account | grant the Requestor role — see below |

**Location is yours to choose.** Password Safe records no cloud, so the dialog asks once
per batch and defaults to `clouddb_ps_import_default_cloud`. It decides which Ansible
runner reaches the database, so a wrong value fails the *run*, not the import.

**Host spelling matters.** "Already imported" compares host and engine literally. Password
Safe holding `db01` where an existing row says `db01.corp.internal` reads as a new
database, and both can be registered. The dashboard cannot resolve your private DNS to
tell them apart — it is not on that network. Prefer consistent naming; the import shows
the exact host it will record.

**Settings** → Integrations → Password Safe → *Databases* tab → *Database Import*:

| Key | Default | Purpose |
|---|---|---|
| `clouddb_ps_import_workgroup` | *(blank)* | Restrict candidates to one Password Safe workgroup. Blank = everything the API identity can see. |
| `clouddb_ps_import_default_cloud` | `local` | Location preselected in the dialog. |
| `clouddb_ps_import_max_systems` | `500` | Cap on candidates. The dialog says so when it truncates — narrow with a workgroup rather than raising this. |
| `clouddb_ps_import_platform_map` | *(blank)* | JSON platform→engine overrides, e.g. `{"Percona Server": "mysql"}`. Invalid JSON is ignored. |

Two more settings that affect how the dashboard reaches a database:

| Key | Default | |
|---|---|---|
| `clouddb_ansible_use_ps_account` | off | Config Management runs against a **provisioned** database connect as its Password Safe managed user instead of the admin credential. That user is created with no grants, so a playbook that needs more fails on permissions until you grant it out of band. Registered databases already connect as their own managed account. |
| `clouddb_db_client_image_postgres` / `clouddb_db_client_image_mysql` / `clouddb_db_client_image_sqlserver` | `postgres:16` / `mysql:8.4` / blank | The DB-client container images run on the jump host; override for a mirrored registry. SQL Server is blank on purpose: no `sqlcmd` image exists, so the jump host's own `/opt/mssql-tools18/bin/sqlcmd` is used. Setting it forces the container path, and the image must carry `sqlcmd` 18 at that path. |

The candidate list is cached for 5 minutes per workgroup; "already imported" is computed
fresh on every request, so an import is reflected immediately.

Imported rows are ordinary registered rows — everything under
[Registering an existing database](register.md) applies to them
unchanged, including that no credential is stored.
