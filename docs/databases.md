# Databases

The dashboard manages databases two ways — **provision** a new managed database in a
cloud, or **register** one that already exists, on-premises included — and layers the
BeyondTrust PAM stack on top of what it provisions. Provisioning needs a Terraform
module, so it is cloud-only; registering needs only somewhere to reach.

The provisioned path is **provisioning + three stacked layers**, each solving a different
privileged-access problem (the same model used across the [Cloud VMs](cloud-vms.md) and
[Kubernetes](kubernetes.md) docs):

- **Provisioning** — stand up a **private** database (AWS RDS / Azure Flexible Server +
  SQL DB / GCP Cloud SQL / OCI Autonomous DB). The dashboard mints the admin credential
  and stores it encrypted.
- **Layer 1 — PRA** *(reach it)* — a BeyondTrust Privileged Remote Access protocol
  tunnel brokers private access to the DB; the admin credential is vaulted in PRA for
  injection. This is what makes the private database usable.
- **Layer 2 — Password Safe** *(manage its secrets)* — *optional.* Password Safe owns
  rotation of a dedicated managed DB user and keeps the PRA-vaulted credential in sync.
- **Layer 3 — Entitle** *(grant time-boxed access)* — *optional.* Onboard the DB as an
  Entitle integration so users request just-in-time access; Entitle mints **ephemeral
  accounts** (or assigns persistent roles) per engine.

The other path is [registration](#registering-an-existing-database): record a database
the dashboard did **not** create — on-premises (`cloud = local`) or in a cloud — so it
can be a [Configuration Management](config-management.md) target. Nothing is built and no
credential is stored; the admin login is a Password Safe **managed account** checked out
at run time. The fastest way to register is
[**Import from Password Safe**](#importing-from-password-safe), which lists what Password
Safe already discovered instead of asking you to type it.

The layers stack — stop after Provisioning + PRA, or add Password Safe and/or Entitle.
**They apply to what the dashboard provisions**: a registered database gets none of them,
because there is no Terraform state to tear down and no credential to vault. That is the
trade for being able to point at a database you already run. Provisioning coverage
differs by cloud/engine:

| Cloud | Provisioning | L1 PRA | L2 Password Safe | L3 Entitle |
|---|---|---|---|---|
| **AWS** | postgres / mysql / sqlserver (RDS) | ✅ tunnel | ✅ `dbssm` | ✅ register + JIT |
| **Azure** | postgres / mysql (Flexible Server) + sqlserver (SQL DB + Private Endpoint) | ✅ tunnel | ✅ `dbazure` | ✅ register + JIT |
| **GCP** | postgres / mysql / sqlserver (Cloud SQL, private IP) | ✅ tunnel | ⚠️ `dbgcp` — sqlserver via Cloud Run; postgres / mysql inert until the Data API channel ships | ✅ postgres / mysql (via forwarder) |
| **OCI** | ⚠️ **oracle only** (Autonomous DB) — read the caveats² | ✅ tunnel¹ | ❌ | ❌ |

¹ OCI has no dashboard-provisioned gateway — you supply your own (see the OCI section).
² The OCI module only started shipping in the image recently and has **never completed a live
run**; it also always provisions into `oci_region`. See [OCI](#oci-autonomous-database--read-the-caveats).

Everything is driven by Terraform from the job worker; deploy state is written to the
active [storage backend](storage-management.md).

---

## Architecture

*This section describes a **provisioned** database. A registered one is reached however
you already reach it — see [Registering an existing database](#registering-an-existing-database).*

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
[terraform_pra_service.py](../web_dashboard/services/terraform_pra_service.py)):

| Engine | Tunnel resource | Note |
|---|---|---|
| postgres | `sra_postgresql_tunnel_jump` | proxies cleartext wire protocol |
| mysql | `sra_my_sql_tunnel_jump` | proxies cleartext wire protocol |
| sqlserver | `sra_protocol_tunnel_jump` (`tunnel_type=mssql`) | TDS-aware; does its own backend TLS |
| oracle | `sra_protocol_tunnel_jump` (`tunnel_type=tcp`) | generic TCP to the ADB TLS listener |

Because the Postgres/MySQL tunnels proxy **cleartext**, the DB is provisioned with TLS
made optional on the server side (`rds.force_ssl=0` / `require_secure_transport=OFF` /
Cloud SQL `ssl_mode=ALLOW_UNENCRYPTED_AND_ENCRYPTED`). SQL Server and Oracle keep TLS on
because their tunnels terminate/forward TLS themselves.

### The database name

The **Name** you give a database on the Provision form is slugified into the catalog
Terraform creates (`My App DB` → `my_app_db`; Oracle instead gets a deterministic
`adb…` name, capped at ADB's 14 characters). That catalog is recorded on the row, so
the **Database** column on the Databases page and the `database:` line in the
**Connection** dialog show it without depending on the provisioning job still existing —
which also gives the [Entitle cloud-function adapter](integrations/cloud-functions.md)
something to scope its grants to (`FN_DB_NAME`).

Two engines read differently:

- **SQL Server** — you connect to the `master` system database. On RDS that is *all*
  there is (the module omits `db_name`; see [AWS](#aws-rds) below), so the column shows
  `master`. On Azure/GCP a real catalog is created as well, and the page shows both —
  the catalog, with `via master` beneath it.
- **Registered** databases show whatever was recorded at registration, `(not set)` if
  nothing was. SQL Server falls back to `master`.

---

## Importing from Password Safe

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
registers as mysql), `oracle` (`oracle`, `oradb`). `mysql` is matched before `oracle` so
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

**Settings** → Integrations → Password Safe → *Database Import*:

| Key | Default | Purpose |
|---|---|---|
| `clouddb_ps_import_workgroup` | *(blank)* | Restrict candidates to one Password Safe workgroup. Blank = everything the API identity can see. |
| `clouddb_ps_import_default_cloud` | `local` | Location preselected in the dialog. |
| `clouddb_ps_import_max_systems` | `500` | Cap on candidates. The dialog says so when it truncates — narrow with a workgroup rather than raising this. |
| `clouddb_ps_import_platform_map` | *(blank)* | JSON platform→engine overrides, e.g. `{"Percona Server": "mysql"}`. Invalid JSON is ignored. |

The candidate list is cached for 5 minutes per workgroup; "already imported" is computed
fresh on every request, so an import is reflected immediately.

Imported rows are ordinary registered rows — everything under
[Registering an existing database](#registering-an-existing-database) applies to them
unchanged, including that no credential is stored.

---

## Registering an existing database

Registration records a database the dashboard **didn't create**, so it can be a
[Configuration Management](config-management.md) target. It is the database sibling of
registering a Kubernetes cluster from a kubeconfig ([Kubernetes](kubernetes.md)): the
dashboard builds nothing, changes nothing, and stores no credential. `cloud = local` is
the on-premises case.

The registerable set is deliberately wider than the provisionable one — **provisioning
needs a Terraform module, registering needs only somewhere to reach.** Any of `local`,
`aws`, `azure`, `gcp` or `oci` may be registered, and engines are **postgres / mysql /
sqlserver / oracle**. Configuration Management runs cover postgres / mysql / sqlserver;
the `ansible-cloud` runner image ships no Oracle client, and no Ansible runner resolves
for `oci` at all, so an OCI row registers and lists but can't be a run target.

**How to register.** **Databases** page → **Register existing**, or
`POST /api/databases/register` (permission `cloud_database:write`). To register several
at once from what Password Safe already knows, use
[Import from Password Safe](#importing-from-password-safe) instead. The button only
renders when the Password Safe integration is on (`password_safe_enabled`), because the admin
login *has* to be a Password Safe managed account. The `cloud_database_enabled` feature
toggle gates registration and provisioning alike.

| Field | Notes |
|---|---|
| **Engine** | postgres / mysql / sqlserver / oracle |
| **Location** | `local` (on-premises) / aws / azure / gcp / oci |
| **Host** | Required — it is how the runner reaches the database. One row per engine + host; a duplicate is refused. |
| **Port** | Optional; defaults per engine (5432 / 3306 / 1433 / 1521). |
| **Database name** | Optional. SQL Server falls back to `master`. |
| **Admin account** | A Password Safe **managed system + account**, looked up live by host — the same lookup the Configuration Management run form uses. Onboard the database in Password Safe *first*; the list is empty until you do. |

**The credential is never stored.** The row keeps the managed system id, account id and
account name — nothing else. At run time the dashboard checks the credential out and lets
the request **expire** on its duration rather than checking it back in, matching what the
inline runners already do for VMs. Contrast the provisioned path, which reads its admin
credential from the provisioning job's Terraform variables and the encrypted config store.

> **The account must be *requestable* by the dashboard's Password Safe API identity** —
> onboarding it is necessary but not sufficient. The checkout goes through a Password Safe
> *request*, so that identity's group needs the **Requestor** role, with an access policy
> granting **View** (API-only is fine), on a Smart Rule containing the account. Without it
> the row registers and lists happily and the run fails in the worker with `Password Safe
> checkout failed for database …: 4031, statuscode: 403`. Two things make this easy to
> misdiagnose: a *database* Smart Rule needs its own grant — the ones covering
> Linux/SSM/Waagent accounts do not extend to it, so the VM path working tells you nothing
> — and Smart Rule membership is recomputed on a schedule, so a freshly created account is
> not requestable the instant it exists. Check with
> `ps-cli access-policies test -s-id <system> -a-id <account>`; an empty list means no
> policy applies yet.
>
> **The import catches this early.** Because
> [Import from Password Safe](#importing-from-password-safe) sources its account list from
> the *requestable* accounts, a managed system showing **no requestable account** there is
> this same problem, surfaced before the row is ever created rather than hours later in a
> worker log. If a database you know is onboarded shows that reason, it is the Requestor
> role or the Smart Rule — not the onboarding.

**Where the run executes** follows the database's location, exactly as it does for
clusters — an on-premises database runs in a sibling container on the dashboard host,
because nothing in a cloud has a route to your LAN; a cloud-hosted one runs on that
cloud's transient in-subnet runner. See
[Ansible → Kubernetes-cluster and database targets](integrations/ansible.md#kubernetes-cluster-and-database-targets-localhost-runs).

**Every runner takes the just-in-time credential, AWS and GCP included.** A database run is
a localhost play, and its connection variables ride the runner's *inline* environment
channel as `CONN_VARS_B64` — the ECS task override env, ACI `secure_value`, the Cloud Run
env, or a `0600 --env-file` locally — decoded to a `0600` file and passed as `-e @…`.
Nothing needs to pre-exist in a cloud secret store, so the ephemeral-store opt-in that
governs a **VM SSH** managed-account run (`ansible_cloud_ephemeral_secrets_enabled`, see
[Secrets Management](secrets-management.md#ephemeral-cloud-secrets)) does **not** apply
here. That opt-in exists only because the SSH path injects named/become secrets by
*reference* (`valueFrom`) on ECS and Cloud Run; the database path never does.

**What registration does not give you.** A registered row has no Terraform state and no
provisioning job, so none of the three layers apply: no PRA protocol tunnel, no Layer-2
Password Safe onboarding of a rotated DB user, and no Layer-3 Entitle integration (Entitle
onboarding reads the provisioning job's admin credential, which such a row doesn't have).
Nothing is created in the cloud, and no gateway host is brokered on its behalf.

**Delete deregisters.** Removing a registered row drops it from the inventory and leaves
the database running — the dashboard holds no Terraform state for someone else's database,
and destroying it would be the wrong verb. The row's action button says **Deregister**
rather than Decommission, and the confirm dialog says the database itself is untouched.
The reverse is refused too: a provisioned database is decommissioned, never deregistered.

---

## Layer 1 — PRA access (shared prerequisites, all clouds)

Before any **provisioned** database will get a working tunnel, configure PRA once under
**Settings → Integrations → Privileged Remote Access**:

- **PRA appliance + OAuth API account** → `bt_api_host`, `bt_client_id`,
  `bt_client_secret` (used by the SRA Terraform provider to create the tunnel).
- A **pre-existing Jump Group** and **Gateway** in PRA → `bt_jump_group_name`,
  `bt_jumpoint_name`. The dashboard does *not* create these.
- The **Gateway Docker deploy key**, pasted in (config key is per cloud — see each
  section). Without it the shared gateway host can't start and the tunnel shows
  *Unavailable* in PRA.

Each provision can override the Jump Group / Gateway / PRA credential per database
(the `jump_group`, `jumpoint_name`, `pra_credential_ref` form fields); otherwise the
`bt_*` defaults apply. (The per-cloud `*_bt_jump_group_name` / `*_jumpoint_name` keys
are for PRA *Shell Jumps*, not the DB tunnel.)

Also enable the **Databases** feature toggle (`cloud_database_enabled`) — it gates
registration as well as provisioning.

---

## Provisioning — per cloud

### AWS (RDS)

Engines: postgres / mysql / sqlserver. Sandbox: [`scripts/sandbox/Linux/setup-aws.sh`](../scripts/sandbox/Linux/setup-aws.sh).

**What the sandbox creates for the DB feature:** two private DB subnets in distinct AZs
(RDS needs ≥2) → the RDS **DB subnet group** `dashboard-sandbox-db`; a Postgres
**parameter group** with `rds.force_ssl=0` (`clouddb-nossl-pg16`); a MySQL-8.4
**parameter group** with `require_secure_transport=0` (`clouddb-nossl-mysql84`); a **DB
security group** allowing 5432/3306/1433 *from the gateway SG only*; the `bt-jumpoint`
ECS cluster + `ecsInstanceRole` + `ecsTaskExecutionRole`; and RDS/ECS/PassRole
permissions on the scoped dashboard IAM user.

**Engine quirks:** MySQL is pinned to **8.4** (8.0's `mysql_native_password` admin is
rejected by the PRA MySQL tunnel; 8.4 defaults to `caching_sha2_password`). SQL Server
(`sqlserver-ex`) has **no `db_name`** — you connect to `master` and create databases
afterward — and its instance class is bumped to `db.t3.small` (needs ≥2 GiB).

**Gateway host:** an **ECS-on-EC2** container instance the dashboard launches on demand
(kicked early so its ~2-min boot overlaps the RDS apply) and terminates when the last
DB/VM/cluster is gone.

**Config keys:**

| Key | Default | Notes |
|---|---|---|
| `aws_db_subnet_group_name` | — | RDS subnet group (import-only key) |
| `aws_db_parameter_group_name` | — | `rds.force_ssl=0` Postgres group (Settings field) |
| `aws_db_mysql_parameter_group_name` | — | `require_secure_transport=0` MySQL-8.4 group (import-only) |
| `aws_db_security_group_id` | — | DB SG allowing tunnel ingress. Attached to the instance when the provision form selects no security group; unset → the VPC *default* SG, which has no ingress on the engine port |
| `aws_ecs_docker_deploy_key` | — | Gateway Docker deploy key |
| `bt_ecs_cluster` / `bt_ecs_launch_type` | `bt-jumpoint` / `EC2` | Gateway cluster (Fargate can't tunnel) |
| `bt_ecs_host_instance_type` / `bt_ecs_host_instance_profile` | `t3.small` / `ecsInstanceRole` | Gateway EC2 host |
| `bt_ecs_jumpoint_subnet_id` / `bt_ecs_jumpoint_security_group_id` | — | Gateway host placement (import-only) |

**Checklist:** run `setup-aws.sh` → import the emitted config at `/setup` → set
`aws_ecs_docker_deploy_key` + the PRA keys → provision from the Databases page.

### Azure (Flexible Server / SQL Database)

Engines: postgres / mysql (Flexible Server) + sqlserver (Azure SQL DB + Private
Endpoint). Sandbox: [`scripts/sandbox/Linux/setup-azure.sh`](../scripts/sandbox/Linux/setup-azure.sh).

**What the sandbox creates:** **three separate DB subnets** — a Postgres-delegated
subnet, a MySQL-delegated subnet (a delegated subnet can host only one flexible-server
type), and a plain SQL Server **Private-Endpoint** subnet — plus **three private DNS
zones** (`*.private.postgres.database.azure.com`, `*.private.mysql.database.azure.com`,
and the fixed `privatelink.database.windows.net`); and a service principal with
Contributor on the resource group.

**Engine quirks:** MySQL pinned to **8.4** (needs `azurerm` ≥ 4.55; same tunnel reason as
AWS). Postgres/MySQL Flexible Servers get `require_secure_transport=OFF`. SQL Server uses
Azure SQL DB + a Private Endpoint (always TLS — fine, the mssql tunnel does backend TLS);
any Flexible-Server SKU picked in the form is coerced to a valid SQL-DB SKU.

**Gateway host:** a **real Azure VM** (`clouddb-jumpoint`) — ACI is serverless and can't
protocol-tunnel — in `azure_jumpoint_subnet_id` (falls back to `azure_aci_subnet_id`).

**Config keys:**

| Key | Default | Notes |
|---|---|---|
| `azure_db_subnet_id` / `azure_db_private_dns_zone_id` | — | Postgres delegated subnet + DNS zone (import-only) |
| `azure_db_mysql_subnet_id` / `azure_db_mysql_private_dns_zone_id` | — | MySQL delegated subnet + DNS zone (import-only) |
| `azure_db_sqlserver_subnet_id` / `azure_db_sqlserver_private_dns_zone_id` | — | SQL Server PE subnet + `privatelink…` zone (import-only) |
| `azure_resource_group` / `azure_location` | `vm-cli-rg` / `centralus` | RG + default region (Settings fields) |
| `azure_jumpoint_subnet_id` | — | Gateway VM subnet; falls back to `azure_aci_subnet_id` |
| `azure_aci_deploy_key` / `azure_aci_docker_deploy_key` | — | Gateway Docker deploy key |
| `azure_jumpoint_vm_size` | `Standard_B1s` | Gateway VM size |

All six are per-region: running the sandbox in a second location emits
`azure_region.<location>.db_*` alongside the flat keys, and the provisioner resolves the
database's own region before falling back to them. The flat keys stay the default
region's copy, so a single-region install behaves exactly as before.

**Checklist:** run `setup-azure.sh` → import the six `azure_db_*` keys + RG/location +
`azure_jumpoint_subnet_id` → set `azure_aci_docker_deploy_key` + PRA keys → provision.

### GCP (Cloud SQL)

Engines: postgres / mysql / sqlserver. Sandbox: [`scripts/sandbox/Linux/setup-gcp.sh`](../scripts/sandbox/Linux/setup-gcp.sh).

**What the sandbox creates:** **Private Services Access** (an allocated `/20` + a
`servicenetworking` VPC peering) so Cloud SQL gets a private IP on the sandbox VPC; a
Cloud Router + Cloud NAT for the jumpoint/k8s subnets; and the `cloudsql.admin` +
`servicenetworking.networksAdmin` roles on the service account.

**Engine quirks:** MySQL uses `MYSQL_8_4` **and** `edition=ENTERPRISE` (so a shared-core
`db-f1-micro` stays valid on 8.4; needs `google` provider 6.x). SQL Server uses a
`db-custom-*` tier (no shared-core) and the built-in **`sqlserver`** login (set via the
instance root password — there's no separate `google_sql_user`); the service forces
`master_username=sqlserver`. Postgres/MySQL use
`ssl_mode=ALLOW_UNENCRYPTED_AND_ENCRYPTED` for the cleartext tunnel.

**Gateway host:** a **privileged gateway container on a Container-Optimized-OS GCE VM**
(`clouddb-shared-jumpoint`), in the gateway subnetwork (which has NAT egress).

**Config keys:**

| Key | Default | Notes |
|---|---|---|
| `gcp_db_network` | — | VPC self-link for Cloud SQL private IP (import-only; falls back to `gcp_network`) |
| `gcp_project_id` / `gcp_region` / `gcp_zone` | — / `us-central1` / `us-central1-a` | project + default region/zone (Settings) |
| `gcp_network` / `gcp_subnetwork` | `default` / — | VPC + VM subnet |
| `gcp_jumpoint_subnetwork` | — | Gateway subnet (has NAT; preferred over the VM subnet) |
| `gcp_jumpoint_name` / `gcp_jumpoint_machine_type` | `clouddb-shared-jumpoint` / `e2-micro` | Gateway VM |
| `gcp_cloud_run_docker_deploy_key` | — | Gateway deploy key (→ `gcp_jumpoint_docker_deploy_key` → `gcp_jumpoint_deploy_key`) |

**Checklist:** run `setup-gcp.sh` → import `gcp_project_id`/`gcp_region`/`gcp_network`/
`gcp_subnetwork`/`gcp_jumpoint_subnetwork`/`gcp_db_network` → set
`gcp_cloud_run_docker_deploy_key` + PRA keys → provision.

### OCI (Autonomous Database) — read the caveats

Engine: **`oracle` only** (Autonomous Database). Sandbox: [`scripts/sandbox/Linux/setup-oci.sh`](../scripts/sandbox/Linux/setup-oci.sh).

**What the sandbox creates:** a dedicated compartment, a VCN (`10.98.0.0/16`) with
public / vm / db subnets, a scoped IAM user + group + policy + API keypair, and
(best-effort) a KMS vault + SSH-keypair secret.

**Autonomous DB specifics:** the admin login is always **`ADMIN`** (only the password
varies); `is_free_tier=true` is the **default**; `is_mtls_connection_required=false` so
the `tcp` tunnel can connect over TLS without a client wallet. It's reached over a generic
`tcp` PRA tunnel to the ADB TLS listener (1521).

> ⚠️ **OCI caveat 1 — no dashboard-provisioned gateway.** `ensure_jumpoint_host` has no
> OCI branch; for `cloud=oci` it falls through to the AWS path and fails (non-fatal). **You
> must pre-create a BeyondTrust Gateway in the OCI public subnet** that can reach the ADB
> TLS endpoint, and point `bt_jumpoint_name` / `bt_jump_group_name` (or the per-DB
> overrides) at it.

> ⚠️ **OCI caveat 2 — region + subnet.** An OCI database is always created in
> **`oci_region`**, regardless of the region chosen in the form. A **free-tier** ADB (the
> default) is a **public** endpoint (Always-Free can't sit in a VCN) and needs no subnet. A
> **paid/private** ADB must be given a subnet via the `oci_subnet_ocid` provision option —
> do not rely on `oci_default_subnet_ocid`, which points at the VM subnet; the sandbox's
> private db-subnet OCID is not emitted to any config key.

> ⚠️ **OCI caveat 3 — never live-validated; needs a rebuilt image.** `terraform/db_oci_autonomous`
> was absent from the Dockerfile `COPY` set (and excluded from the build context by
> `.dockerignore`) until recently, so **no published image could provision an OCI database** —
> `apply` failed in `terraform._materialize` with "Terraform module template not found", even
> though `("oracle", "oci")` is listed in `_IMPLEMENTED`. The module now ships, but that means
> this path has **never completed a live run**: treat the first provision as a bring-up, and make
> sure you are on an image built after the fix. The OCI **OKE** module was missing the same way —
> see [Kubernetes → OCI OKE](kubernetes.md#oci-oke--experimental).

**Config keys** (all Settings fields): `oci_tenancy_ocid`, `oci_user_ocid`,
`oci_fingerprint`, `oci_private_key` (+ `oci_private_key_passphrase`), `oci_region`
(`us-ashburn-1`), `oci_compartment_ocid`, `oci_vcn_ocid`, `oci_default_subnet_ocid`,
`oci_vault_ocid`. There are no dedicated `oci_db_*` networking keys (Autonomous DB is
fully managed PaaS — no parameter groups, delegated subnets, or private DNS zones), and
OCI has no per-region config sets.

**Checklist:** run `setup-oci.sh` → import the `oci_*` credential + compartment/VCN keys →
**stand up your own Gateway in the OCI public subnet** and point `bt_jumpoint_name` at it
→ provision (default = free-tier public ADB).

---

## Layer 2 — Password Safe (AWS + Azure + GCP)

*Optional.* When enabled (`clouddb_ps_onboarding_enabled`), provisioning an **AWS**,
**Azure** or **GCP** database additionally hands rotation of a database credential to
Password Safe and keeps the PRA-vaulted credential in sync. **OCI is not supported** —
those databases provision and get a tunnel, but no Password Safe onboarding. GCP covers
**PostgreSQL and MySQL only**, and ships off — see that section for both reasons.

### Two ways in: at provision, or afterwards

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
> [docs/runbooks/clouddb-password-safe-plugin-setup.md](runbooks/clouddb-password-safe-plugin-setup.md)
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

### Where the functional account comes from

`clouddb_ps_functional_account_mode` picks one of two contracts:

| Mode | Functional account | Panel holds |
|---|---|---|
| **`reference`** | **Operator-created.** Resolved by name, never created, **never deleted on decommission** — the same contract as `passwordsafe_vm_functional_account_*` (VM) and `k8s_ps_functional_account_*` (k8s). The managed system inherits **its** platform, so the `clouddb_ps_platform_*` names become advisory. | the account name |
| `create` (default) | **Dashboard-created**, one per database, deleted on decommission. | the credential material packed into it |

`reference` keeps the static cloud credential — the IAM access key, the Azure client
secret — out of the dashboard's config store entirely. It needs **one account per engine
per cloud**, because a functional account belongs to a platform. A blank name in
`reference` mode is an error, not a fall-through to `create`.

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

### AWS — `dbssm` (AWS Systems Manager)

The dashboard creates the managed user by running the DB client (`psql` / `mysql` /
`sqlcmd`, as a `docker run`) on the shared **ECS gateway host over AWS SSM
`SendCommand`** — the only dashboard component with line-of-sight to the private DB. It
registers the DB on the **`{engine} SSM Custom Plugin`** platform (v24.2.x). The plugin
indexes the managed-system address at fixed **per-engine** positions — mssql has no
database segment, and mysql alone carries a trailing ssl flag:

```
mssql (5):  instanceId;region;dbEndpoint;certPath;assumeRole
psql  (6):  instanceId;region;dbEndpoint;databaseName;certPath;assumeRole
mysql (7):  instanceId;region;dbEndpoint;databaseName;certPath;assumeRole;sslTRUE|sslFALSE
```

- `instanceId` is the shared gateway host's EC2 instance id (the SSM target). The DB
  **port rides the managed system's Port field, never the address** — a `Server=…:5432`
  in the plugin log is the log line appending the Port field, not an address mistake.
- `assumeRole` must be **≥ 12 characters**: the plugin `Substring(0,12)`s it to detect
  an `arn:aws:iam:` prefix, so anything shorter (the old `local` default) crashes every
  action with *"Index and length must refer to a location within the string"*. The
  placeholder is `NoAssumeRole`; a full role ARN switches EC2 mode to STS AssumeRole. A
  configured value under 12 characters is coerced to the placeholder on read.
- The packed address rides the **DNS Name field only**. The IP Address field is the
  same `127.0.0.1` placeholder every other plugin shape uses: Password Safe refuses a
  create with no IP at all (*"The field 'IPAddress' is required."*) **and** refuses one
  that is not a literal IP (*"Bad IP value: '…' in 'IPAddress' field"*), so an address
  that doubles as an IP cannot exist. Both wordings were live rejections, each after a
  full RDS apply; registration now rejects a non-IP `ip_address` before Terraform runs.

In `create` mode the **functional account packs the AWS transport credential *and* the
DB admin credential**: username `EC2:<dbAdmin>` or `IAM:<dbAdmin>`, password always the
three-part `<AccessKeyId>:<SecretAccessKey>:<dbAdminPassword>` — the plugin splits
before it looks at the mode, so EC2 mode ships `x:x:` placeholders for parts 1–2. IAM
mode is selected by setting **both** key fields. Part 3 is what *Verify Functional
Account* logs into the database with, and it gives the via-functional-account change a
privileged login (the RDS master user); none of these values may contain `:` (the
field delimiter) — dashboard-generated credentials never do.

**Prerequisites (manual):**

- Upload the three **`{engine} SSM Custom Plugin`**s and **`PRA Vault Username Password`**
  (`Beekeeper-UsernamePasswordPRAVault.docx` + the per-engine SSM guides).
- Prep the **jump host** for the SSM DB plugin: the DB client binary at the path the
  plugin invokes, plus the RSA key pair (`private.pem` + `passphrase.txt`) in the
  `ssm-user` home for credential decryption. *(The dashboard's own managed-user creation
  uses a `docker run` client image and does not need this — this is for the plugin's
  ongoing rotation.)*
- Create a **PRA Configuration-API account** (OAuth client) with **Vault Account
  Management** permission (or leave the PRA Config-API fields blank to reuse the SRA/PRA
  credentials).
- Run the updated `setup-aws.sh` so `ecsInstanceRole` has `AmazonSSMManagedInstanceCore`
  and the dashboard IAM user has `ssm:SendCommand` / `ssm:GetCommandInvocation`.

**Config keys:**

| Key | Default | Notes |
|---|---|---|
| `clouddb_ps_onboarding_enabled` | `false` | Master toggle (AWS **and** Azure) |
| `clouddb_ps_functional_account_mode` | `create` | `create` or `reference` — see above |
| `clouddb_ps_self_rotation` | `false` | Emits `use_own_credentials` on the managed account, so the DB plugin's self-rotate action runs. **Required with `reference` mode** — the via-functional-account action needs a privileged DB login a provisioned server does not have |
| `clouddb_ps_platform_postgres` / `_mysql` / `_sqlserver` | `psql/mysql/mssql SSM Custom Plugin` | Custom-plugin platform names; advisory in `reference` mode |
| `clouddb_ps_functional_account_postgres` / `_mysql` / `_sqlserver` | — | `reference` mode: the operator-created account on each SSM platform |
| `clouddb_ps_pravault_platform` | `PRA Vault Username Password` | PRA Vault plugin platform |
| `clouddb_ps_pravault_functional_account` | — | `reference` mode: the operator-created account on the PRA Vault platform |
| `clouddb_ps_workgroup` | — | Workgroup; blank → `passwordsafe_workgroup` |
| `clouddb_db_client_image_postgres` / `_mysql` / `_sqlserver` | `postgres:16` / `mysql:8.4` / `mcr.microsoft.com/mssql-tools18` | DB-client images on the jump host |
| `clouddb_ps_ssm_iam_username` | — | Informational only — the plugin never sees it; the mode is selected by the key pair below |
| `clouddb_ps_ssm_access_key_id` / `_secret_access_key` | — | `create` mode only: **both set → IAM mode**, either blank → EC2 role mode |
| `clouddb_ps_ssm_account_suffix` | `NoAssumeRole` | The address's `assumeRole` segment: the placeholder or a cross-account AssumeRole ARN. **≥ 12 chars** — shorter crashes the plugin, so a short persisted value is coerced on read |
| `clouddb_ps_ssm_public_key_path` | — | The address's `certPath` segment (field 4 mssql / field 5 psql+mysql): RSA public-cert path on the PS node/broker. **Required** — onboarding refuses a blank rather than packing an empty segment |
| `clouddb_ps_ssm_ssl` | `true` | mysql only: the trailing `sslTRUE` / `sslFALSE` segment (only the literal `sslTRUE` enables TLS) |
| `clouddb_ps_ssm_plugin_private_key` / `_passphrase` | — | Plugin RSA key material the dashboard drops onto the Gateway host over SSM. **Use a separate pair from Azure's** — the private keys land on different hosts |
| `clouddb_ps_ssm_key_directory` | `/home/ssm-user` | Where the SSM plugin reads that key on the jump host; blank leaves the staging manual |
| `pra_config_api_client_id` / `_secret` | — | PRA Config-API account; blank → reuse `bt_client_id` / `bt_client_secret` |

### Azure — `dbazure` (Azure VM Run Command)

Instead of AWS SSM, the three **`{engine} Azure Run Command Plugin`**s reach the private
DB by sending an **Azure VM Run Command** to the shared **`clouddb-jumpoint`** VM. The
dashboard first prepares that VM over Run Command (installs the DB clients and drops the
plugin's `private.pem` / `passphrase.txt` to `/root/psplugin`), then creates the managed
user. The DB is registered on the **`{engine} Azure Run Command Plugin`** platform with the
eight-field address `vmName;resourceGroup;subscriptionId;tenantId;dbHost;dbName;certPath;sslTRUE|sslFALSE`.
In `create` mode, and unlike AWS, the **functional account is a privileged DB login** (the
minted admin) bundled with the Azure control-plane service principal: username `SP:<admin>`
(or `MSI:<admin>`), password `clientId:clientSecret:adminPassword` (or `-:-:adminPassword`
for MSI). Because that embeds a **per-database** password, a single pre-created Azure account
(`reference` mode) is only viable with the plugin's **self-rotate** change action, where the
managed account rotates itself and the functional account supplies only the Azure
control-plane token. That action is selected by `use_own_credentials` on the managed account
— turn on `clouddb_ps_self_rotation`. All three plugins resolve the Azure control-plane
credential in three tiers — the functional account's own service principal (`SP:` names), else
a broker-level one under `AppSettings:Azure{Postgres,MySql,Mssql}:ControlPlane`, else
`DefaultAzureCredential` — so `SP:` functional accounts need nothing broker-side even on an
off-Azure Resource Broker. Set
`passwordsafe_azure_db_registration_method=off` to keep the toggle on for AWS but skip Azure.

**Prerequisites (manual):**

- Upload the three **`{engine} Azure Run Command Plugin`**s
  (`Beekeeper-AzurePostgresRunCommand.docx`, `…Mssql…`, `…MySql…`).
- Generate the plugin **RSA-4096 key pair** (`scripts/make-plugin-cert.sh` in the plugin
  repo): copy `public_cert.cer` to every Password Safe **Resource Broker** at
  `clouddb_ps_azure_cert_path`, and paste `private.pem` + passphrase into
  `clouddb_ps_azure_plugin_private_key` / `_passphrase` (stored encrypted; the dashboard
  drops them onto the jump VM).
- Grant the **service principal** used for the functional account
  (`clouddb_ps_azure_sp_client_id`, or `azure_client_id` when blank) **Virtual Machine
  Contributor** (or `Microsoft.Compute/virtualMachines/read` + `.../runCommand/action`) on
  the jump-VM resource group.
- Create a **PRA Configuration-API account** as in the AWS section.
- The `pscli` API account needs **Requestor** access (Smart Rule → Access Policy) to the
  new managed account before a checkout / rotation-on-request succeeds.

**Config keys** (the PRA-Vault plugin, workgroup, and DB-client images are shared with the
AWS keys above):

| Key | Default | Notes |
|---|---|---|
| `passwordsafe_azure_db_registration_method` | `runcommand` | `runcommand` or `off` (skip Azure, keep AWS) |
| `clouddb_ps_platform_azure_postgres` / `_mysql` / `_sqlserver` | `PostgreSQL/MySQL/MSSQL Azure Run Command Plugin` | Custom-plugin platform names; advisory in `reference` mode |
| `clouddb_ps_functional_account_azure_postgres` / `_mysql` / `_sqlserver` | — | `reference` mode: the operator-created account on each Run Command platform |
| `clouddb_ps_azure_auth_mode` | `SP` | `create` mode only: `SP` (service principal) or `MSI` — functional-account username prefix |
| `clouddb_ps_azure_cert_path` | `C:\BeyondTrust\certs\public_cert.cer` | Public-cert path on the Resource Broker (address field 7) |
| `clouddb_ps_azure_ssl` | `true` | `sslTRUE` / `sslFALSE` (address field 8) |
| `clouddb_ps_azure_sp_client_id` / `_client_secret` | — | `create` mode only: Azure SP for the functional account; blank → reuse `azure_client_id` / `_secret` |
| `clouddb_ps_azure_plugin_private_key` / `_passphrase` | — | Plugin RSA key material dropped on the jump VM (encrypted at rest) |

### GCP — `dbgcp` (Cloud SQL Data API)

> **Channel readiness is uneven, and it decides what actually works.**
>
> | Channel | Engines here | State |
> |---|---|---|
> | `cloud-run` | sqlserver | **Built.** Plugin transport and the Cloud Run service both implemented and unit-tested, **not yet exercised against a live Cloud SQL instance**. |
> | `data-api` | postgres, mysql | Interface-complete, **not wired to GCP** — returns *"not implemented in this build"*. |
> | `admin-api` | — | Same, and not offered: it needs `cloudsql.users.update`, which among predefined roles lives only in the very broad `roles/cloudsql.admin`. |
>
> So today a **SQL Server** rotation should genuinely work once you deploy the service,
> while a **PostgreSQL or MySQL** rotation returns the not-implemented message.
> `passwordsafe_gcp_db_registration_method` ships **`off`** either way.
>
> The not-implemented message is still a useful result on the Data API path: reaching it
> proves the address parsed, the functional account parsed, the platform bound and the
> capability pre-flight passed. A parse error or a platform mismatch is a real failure;
> that one is not.

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

**`data-api` is not a fallback for SQL Server.** Forcing the channel does not work today,
for two separate reasons in the address builder: it appends `iam=true` unconditionally,
which the plugin *rejects* on SQL Server because Cloud SQL for SQL Server has no IAM
database authentication at all; and it never emits `fasecret=`, which that combination
*requires* — with no IAM token there has to be a Secret Manager version holding the
functional account's password. So `cloud-run` is the only working path for SQL Server,
not merely the recommended one, and standing the service up is a prerequisite rather than
an ergonomics choice.

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
> See [docs/design/ps-dbops-cloud-run.md](design/ps-dbops-cloud-run.md).

The DB is registered on the **`GCP Cloud SQL {engine}`** platform with the five-field
address `channel;project:region:instance;dbName;audience;ssl[;key=value]`:

| Channel | Address the dashboard builds |
|---|---|
| `data-api` | `data-api;{project}:{region}:{instance};{dbName};-;-;iam=true` — MySQL appends `;host=%` |
| `cloud-run` | `cloud-run;{project}:{region}:{instance};{dbName};{audience};sslTRUE` |

Field 2 is the instance **connection name**, not a hostname. On the control-plane channel
fields 4 and 5 are `-`: the Cloud SQL APIs are always TLS and never open a database
connection, and the plugin rejects a value there rather than letting anyone believe they
disabled it. On `cloud-run`, field 4 is the audience **verbatim** — a path, query or
fragment is rejected — and field 5 is the real TLS choice for the service→database hop.

On SQL Server the database is `master`: that is the admin session catalog, and the
statements are server-level (`ALTER LOGIN`).

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
| `clouddb_ps_gcp_rotator_service_account` | — | **`data-api` only.** The rotation identity, registered as an IAM database user per instance. Keep it ≤32 characters for MySQL |
| `clouddb_ps_gcp_dbops_audience` | — | **`cloud-run` only.** An **override** — address field 4 for a service you deployed yourself, or one behind a custom domain / PSC. A dashboard-deployed service in the database's own region **beats it**, because Direct VPC egress is region-locked and one global value would address a rotation at a service that cannot reach the instance. With neither, SQL Server onboarding stays off |
| `clouddb_ps_gcp_dbops_ssl` | `true` | `sslTRUE` / `sslFALSE` — address field 5, the service→database TLS choice |
| `clouddb_ps_gcp_dbops_invokers` | — | Comma-separated IAM members granted `roles/run.invoker` on the deployed service — the Resource Brokers' identities. A bare email is accepted and prefixed. Named principals only |
| `clouddb_ps_gcp_dbops_ingress` | `all` | `all` (public + IAM; the only thing an **on-premises** broker can reach) or `internal` |
| `clouddb_ps_gcp_dbops_min_instances` | `1` | Warm instances. A **correctness** setting — see above. Bills continuously |
| `clouddb_ps_gcp_dbops_concurrency` | `8` | Requests per instance, well under Cloud Run's default of 80: each one holds a database connection |

---

## Layer 3 — Entitle (just-in-time access)

*Optional.* Onboard a **provisioned** database into BeyondTrust **Entitle** as its own
integration, so users request **just-in-time** access to it instead of holding a standing
credential. Gated by `entitle_registration_enabled` plus a per-provision
**"Register in Entitle"** toggle, and there is a post-provision **Register in Entitle**
button (job `clouddb_entitle_register`) for a database provisioned earlier — plus, for
MySQL and SQL Server, a **Function (DB grant)** button that deploys the adapter those two
engines need (see [below](#mysql-and-sql-server-the-db_grant-adapter)). This layer
needs the provisioning job's admin credential, so a
[registered](#registering-an-existing-database) database is not offerable — the button is
hidden and the API refuses it with a 400. Teardown
deregisters on decommission. Full Entitle setup (owner, workflow,
durations, the agent) lives in the [Entitle integration](integrations/entitle.md) doc.

The account model is **per engine**:

| Engine | Entitle account model | Notes |
|---|---|---|
| **PostgreSQL** | **Ephemeral (JIT) accounts** — *proven* | Entitle mints a short-lived role per grant. The connector config uses `user` (not `username`) + a required `options{}` block, no top-level `database`. |
| **SQL Server** | Ephemeral accounts | **Only on Entitle-viable providers** — Azure SQL Managed Instance / AWS RDS Custom. Managed Cloud SQL / RDS-standard / Azure SQL Database are refused (`_entitle_viable`) because the connector needs sysadmin/CONTROL SERVER they can't grant. Requires a `version` field (default `2019`, `entitle_sqlserver_version`). |
| **MySQL** | **Persistent roles** (not ephemeral) | Entitle's MySQL connector assigns persistent roles rather than minting accounts. |
| **Oracle (OCI)** | — | Not supported by the Entitle DB connector. |

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
[Cloud Functions](integrations/cloud-functions.md).
---

## Lifecycle (provision, register, decommission)

- **Provision:** from the Databases page, pick engine + cloud + region and (when
  PRA is configured) a Jump Group / Gateway. The record + admin credential are created
  synchronously; the `terraform apply`, tunnel brokering, and any Password Safe onboarding
  run in the **job worker** as a background job.
- **Register:** from the Databases page, **Register existing** → engine, location, host and
  a Password Safe managed account. The row is created synchronously with status
  `available`; there is no job, because there is nothing to build.
- **Deregister:** removing a registered row deletes the row only — no Terraform, no PRA or
  Password Safe teardown, because there was never anything to tear down.
- **Decommission:** tears down the PRA tunnel + Vault account, any Layer-2 Password Safe
  managed systems + functional accounts, any Layer-3 Entitle integration (+ the GCP
  forwarder), and finally the database instance — accumulating (not swallowing) errors so
  an orphaned tunnel/vault/instance is visible.

---

## Troubleshooting

- **MySQL tunnel rejects the login (`mysql_native_password` unsupported).** The engine
  must be **8.4** on every cloud (8.0's admin auth plugin is rejected by the PRA tunnel;
  flipping the server parameter doesn't fix the existing admin). The modules default to
  8.4 — don't override to 8.0.
- **Tunnel shows *Unavailable* in PRA.** Usually the gateway host never started: set the
  cloud's deploy key (`aws_ecs_docker_deploy_key` / `azure_aci_docker_deploy_key` /
  `gcp_cloud_run_docker_deploy_key`). On **OCI** there is no auto-jumpoint — you must
  pre-create one in the public subnet.
- **SQL Server: can't create a database at provision.** By design — RDS/Cloud SQL SQL
  Server connect to `master`; create app databases afterward through the tunnel.
- **Azure/GCP: provision fails at `terraform apply` for a region.** Region/engine/SKU
  capability is **not** validated up front; a region that lacks Flexible Server / MySQL
  8.4 / the SKU fails at apply. Pick a supported region (and matching `azure_db_*` /
  `gcp_db_*` values for it).
- **OCI DB landed in the wrong region.** OCI databases are always created in `oci_region`,
  not the form's region field.
- **Password Safe onboarding didn't happen (AWS/Azure).** It's gated by
  `clouddb_ps_onboarding_enabled` **and** `pscli_*` being configured (and, for Azure,
  `passwordsafe_azure_db_registration_method != off`) — **and** by the provision form's
  **Onboard into Password Safe** checkbox, which is recorded per database. Failures are
  non-fatal and fall back to the legacy admin-credential staging — check the job log for
  the warning. Whatever the cause, the row's **Register in Password Safe** action retries
  it without rebuilding the database.
- **Registering: "no Password Safe managed accounts found for this host."** The lookup is by
  the host string you typed and needs an already-onboarded managed system in Password Safe.
  Onboard the database there first, and check `password_safe_enabled` plus the `pscli_*` OAuth
  client — the dashboard deliberately has no "type a database password" path.
- **A registered on-prem database run fails with `docker: command not found`.** A
  `cloud = local` database runs in a sibling container on the dashboard host, because
  nothing in a cloud has a route to your LAN. That host needs the Docker socket mounted and
  network reach to the database.
- **A registered OCI database can't be selected in Configuration Management.** Expected —
  no Ansible runner resolves for `oci`. Registration still gives you the inventory row.

For the base BeyondTrust/PRA setup (OAuth accounts, Jump Group/Jumpoint, deploy keys), see
the [Privileged Remote Access](integrations/privileged-remote-access.md) doc. For the sandbox network
topology, see [Cloud Sandbox](CLOUD_SANDBOX.md).
