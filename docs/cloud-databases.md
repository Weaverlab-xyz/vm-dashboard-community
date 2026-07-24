# Cloud Databases

The dashboard provisions **managed cloud databases** and layers the BeyondTrust PAM
stack on top of them. The feature is **provisioning + three stacked layers**, each
solving a different privileged-access problem (this is the same model used across the
[Cloud VMs](cloud-vms.md) and [Kubernetes](kubernetes.md) docs):

- **Provisioning** — stand up a **private** database (AWS RDS / Azure Flexible Server +
  SQL DB / GCP Cloud SQL / OCI Autonomous DB). The dashboard mints the admin credential
  and stores it encrypted.
- **Layer 1 — PRA** *(reach it)* — a BeyondTrust Privileged Remote Access protocol
  tunnel brokers private access to the DB; the admin credential is vaulted in PRA for
  injection. This is what makes the private database usable.
- **Layer 2 — Password Safe** *(manage its secrets)* — *optional.* Password Safe owns
  rotation of a dedicated managed DB user and keeps the PRA-vaulted credential in sync.
- **Layer 3 — Entitle** *(grant time-boxed access)* — *optional.* Register the DB as an
  Entitle integration so users request just-in-time access; Entitle mints **ephemeral
  accounts** (or assigns persistent roles) per engine.

The layers stack — stop after Provisioning + PRA, or add Password Safe and/or Entitle.
Coverage differs by cloud/engine:

| Cloud | Provisioning | L1 PRA | L2 Password Safe | L3 Entitle |
|---|---|---|---|---|
| **AWS** | postgres / mysql / sqlserver (RDS) | ✅ tunnel | ✅ `dbssm` | ✅ register + JIT |
| **Azure** | postgres / mysql (Flexible Server) + sqlserver (SQL DB + Private Endpoint) | ✅ tunnel | ✅ `dbazure` | ✅ register + JIT |
| **GCP** | postgres / mysql / sqlserver (Cloud SQL, private IP) | ✅ tunnel | ❌ | ✅ postgres / mysql (via forwarder) |
| **OCI** | **oracle only** (Autonomous DB) | ✅ tunnel¹ | ❌ | ❌ |

¹ OCI has no dashboard-provisioned jumpoint — you supply your own (see the OCI section).

Everything is driven by Terraform from the job worker; deploy state is written to the
active [storage backend](storage-management.md).

---

## Architecture

The database is **private** (`publicly_accessible = false`, or a public free-tier
Autonomous DB on OCI). The dashboard's backend has **no direct network path to it** — the
only way in is a BeyondTrust PRA protocol tunnel brokered through a shared, on-demand
**jumpoint host** that sits in (or peers into) the database's network.

```
  operator / app                dashboard backend (worker)
        │                                 │  terraform apply (db module)
        │  PRA client                     │  terraform apply (beyondtrust/sra: tunnel + Vault account)
        ▼                                 ▼
  ┌───────────┐   PRA protocol tunnel   ┌────────────────┐   private net   ┌───────────────┐
  │    PRA    │◄───────────────────────►│  Jumpoint host │◄───────────────►│  managed DB   │
  │ appliance │      (jump item)        │  (per cloud)   │   :5432/3306/   │  (private)    │
  └───────────┘                         └────────────────┘   1433/1521     └───────────────┘
```

The jumpoint host differs per cloud (Fargate/ACI can't do protocol tunneling, so AWS
uses EC2 and Azure a VM):

| Cloud | Jumpoint host | Provisioned by the dashboard? |
|---|---|---|
| AWS | ECS-on-EC2 container instance (`bt-jumpoint` cluster) | ✅ on demand, ref-counted |
| Azure | privileged jumpoint container on an Azure VM (`clouddb-jumpoint`) | ✅ on demand, ref-counted |
| GCP | privileged jumpoint container on a Container-Optimized-OS GCE VM | ✅ on demand, ref-counted |
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

---

## Layer 1 — PRA access (shared prerequisites, all clouds)

Before any cloud database will get a working tunnel, configure PRA once under
**Settings → Integrations → BeyondTrust**:

- **PRA appliance + OAuth API account** → `bt_api_host`, `bt_client_id`,
  `bt_client_secret` (used by the SRA Terraform provider to create the tunnel).
- A **pre-existing Jump Group** and **Jumpoint** in PRA → `bt_jump_group_name`,
  `bt_jumpoint_name`. The dashboard does *not* create these.
- The **Jumpoint Docker deploy key**, pasted in (config key is per cloud — see each
  section). Without it the shared jumpoint host can't start and the tunnel shows
  *Unavailable* in PRA.

Each provision can override the Jump Group / Jumpoint / PRA credential per database
(the `jump_group`, `jumpoint_name`, `pra_credential_ref` form fields); otherwise the
`bt_*` defaults apply. (The per-cloud `*_bt_jump_group_name` / `*_jumpoint_name` keys
are for PRA *Shell Jumps*, not the DB tunnel.)

Also enable the **Cloud Databases** feature toggle (`cloud_database_enabled`).

---

## Provisioning — per cloud

### AWS (RDS)

Engines: postgres / mysql / sqlserver. Sandbox: [`scripts/sandbox/Linux/setup-aws.sh`](../scripts/sandbox/Linux/setup-aws.sh).

**What the sandbox creates for the DB feature:** two private DB subnets in distinct AZs
(RDS needs ≥2) → the RDS **DB subnet group** `dashboard-sandbox-db`; a Postgres
**parameter group** with `rds.force_ssl=0` (`clouddb-nossl-pg16`); a MySQL-8.4
**parameter group** with `require_secure_transport=0` (`clouddb-nossl-mysql84`); a **DB
security group** allowing 5432/3306/1433 *from the jumpoint SG only*; the `bt-jumpoint`
ECS cluster + `ecsInstanceRole` + `ecsTaskExecutionRole`; and RDS/ECS/PassRole
permissions on the scoped dashboard IAM user.

**Engine quirks:** MySQL is pinned to **8.4** (8.0's `mysql_native_password` admin is
rejected by the PRA MySQL tunnel; 8.4 defaults to `caching_sha2_password`). SQL Server
(`sqlserver-ex`) has **no `db_name`** — you connect to `master` and create databases
afterward — and its instance class is bumped to `db.t3.small` (needs ≥2 GiB).

**Jumpoint host:** an **ECS-on-EC2** container instance the dashboard launches on demand
(kicked early so its ~2-min boot overlaps the RDS apply) and terminates when the last
DB/VM/cluster is gone.

**Config keys:**

| Key | Default | Notes |
|---|---|---|
| `aws_db_subnet_group_name` | — | RDS subnet group (import-only key) |
| `aws_db_parameter_group_name` | — | `rds.force_ssl=0` Postgres group (Settings field) |
| `aws_db_mysql_parameter_group_name` | — | `require_secure_transport=0` MySQL-8.4 group (import-only) |
| `aws_db_security_group_id` | — | DB SG allowing tunnel ingress (import-only) |
| `aws_ecs_docker_deploy_key` | — | Jumpoint Docker deploy key |
| `bt_ecs_cluster` / `bt_ecs_launch_type` | `bt-jumpoint` / `EC2` | Jumpoint cluster (Fargate can't tunnel) |
| `bt_ecs_host_instance_type` / `bt_ecs_host_instance_profile` | `t3.small` / `ecsInstanceRole` | Jumpoint EC2 host |
| `bt_ecs_jumpoint_subnet_id` / `bt_ecs_jumpoint_security_group_id` | — | Jumpoint host placement (import-only) |

**Checklist:** run `setup-aws.sh` → import the emitted config at `/setup` → set
`aws_ecs_docker_deploy_key` + the PRA keys → provision from the Cloud Databases page.

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

**Jumpoint host:** a **real Azure VM** (`clouddb-jumpoint`) — ACI is serverless and can't
protocol-tunnel — in `azure_jumpoint_subnet_id` (falls back to `azure_aci_subnet_id`).

**Config keys:**

| Key | Default | Notes |
|---|---|---|
| `azure_db_subnet_id` / `azure_db_private_dns_zone_id` | — | Postgres delegated subnet + DNS zone (import-only) |
| `azure_db_mysql_subnet_id` / `azure_db_mysql_private_dns_zone_id` | — | MySQL delegated subnet + DNS zone (import-only) |
| `azure_db_sqlserver_subnet_id` / `azure_db_sqlserver_private_dns_zone_id` | — | SQL Server PE subnet + `privatelink…` zone (import-only) |
| `azure_resource_group` / `azure_location` | `vm-cli-rg` / `centralus` | RG + default region (Settings fields) |
| `azure_jumpoint_subnet_id` | — | Jumpoint VM subnet; falls back to `azure_aci_subnet_id` |
| `azure_aci_deploy_key` / `azure_aci_docker_deploy_key` | — | Jumpoint Docker deploy key |
| `azure_jumpoint_vm_size` | `Standard_B1s` | Jumpoint VM size |

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

**Jumpoint host:** a **privileged jumpoint container on a Container-Optimized-OS GCE VM**
(`clouddb-shared-jumpoint`), in the jumpoint subnetwork (which has NAT egress).

**Config keys:**

| Key | Default | Notes |
|---|---|---|
| `gcp_db_network` | — | VPC self-link for Cloud SQL private IP (import-only; falls back to `gcp_network`) |
| `gcp_project_id` / `gcp_region` / `gcp_zone` | — / `us-central1` / `us-central1-a` | project + default region/zone (Settings) |
| `gcp_network` / `gcp_subnetwork` | `default` / — | VPC + VM subnet |
| `gcp_jumpoint_subnetwork` | — | Jumpoint subnet (has NAT; preferred over the VM subnet) |
| `gcp_jumpoint_name` / `gcp_jumpoint_machine_type` | `clouddb-shared-jumpoint` / `e2-micro` | Jumpoint VM |
| `gcp_cloud_run_docker_deploy_key` | — | Jumpoint deploy key (→ `gcp_jumpoint_docker_deploy_key` → `gcp_jumpoint_deploy_key`) |

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

> ⚠️ **OCI caveat 1 — no dashboard-provisioned jumpoint.** `ensure_jumpoint_host` has no
> OCI branch; for `cloud=oci` it falls through to the AWS path and fails (non-fatal). **You
> must pre-create a BeyondTrust Jumpoint in the OCI public subnet** that can reach the ADB
> TLS endpoint, and point `bt_jumpoint_name` / `bt_jump_group_name` (or the per-DB
> overrides) at it.

> ⚠️ **OCI caveat 2 — region + subnet.** An OCI database is always created in
> **`oci_region`**, regardless of the region chosen in the form. A **free-tier** ADB (the
> default) is a **public** endpoint (Always-Free can't sit in a VCN) and needs no subnet. A
> **paid/private** ADB must be given a subnet via the `oci_subnet_ocid` provision option —
> do not rely on `oci_default_subnet_ocid`, which points at the VM subnet; the sandbox's
> private db-subnet OCID is not emitted to any config key.

**Config keys** (all Settings fields): `oci_tenancy_ocid`, `oci_user_ocid`,
`oci_fingerprint`, `oci_private_key` (+ `oci_private_key_passphrase`), `oci_region`
(`us-ashburn-1`), `oci_compartment_ocid`, `oci_vcn_ocid`, `oci_default_subnet_ocid`,
`oci_vault_ocid`. There are no dedicated `oci_db_*` networking keys (Autonomous DB is
fully managed PaaS — no parameter groups, delegated subnets, or private DNS zones), and
OCI has no per-region config sets.

**Checklist:** run `setup-oci.sh` → import the `oci_*` credential + compartment/VCN keys →
**stand up your own Jumpoint in the OCI public subnet** and point `bt_jumpoint_name` at it
→ provision (default = free-tier public ADB).

---

## Layer 2 — Password Safe (AWS + Azure)

*Optional.* When enabled (`clouddb_ps_onboarding_enabled`), provisioning an **AWS** or
**Azure** database additionally hands rotation of a database credential to Password Safe
and keeps the PRA-vaulted credential in sync. **GCP and OCI are not supported** — those
databases provision and get a tunnel, but no Password Safe onboarding.

Both paths create a **dedicated managed DB user** as the rotation target (not the master
admin), point the PRA tunnel's injected credential at it, register the DB as a Password
Safe **managed system + managed account**, and onboard the PRA Vault account on the
**`PRA Vault Username Password`** plugin so rotations propagate into the tunnel credential.
Every step is **non-fatal**: any failure logs a warning and falls back to the legacy
admin-credential staging, leaving the database up. Decommissioning deregisters both
managed systems and deletes both functional accounts before the instance is destroyed
(the managed DB user goes with it).

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
`sqlcmd`, as a `docker run`) on the shared **ECS jumpoint host over AWS SSM
`SendCommand`** — the only dashboard component with line-of-sight to the private DB. It
registers the DB on the **`{engine} SSM Custom Plugin`** platform with DNS name
`{instanceArn};{region};{dbEndpoint};{dbName};{publicKeyPath};{suffix}`. The **functional
account is the AWS IAM user** used for SSM (EC2-role mode by default, or IAM-key mode) —
**there is no privileged DB login**; the managed account changes *its own* password on
rotation (no elevated DB privilege needed).

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
| `clouddb_ps_platform_postgres` / `_mysql` / `_sqlserver` | `psql/mysql/mssql SSM Custom Plugin` | Custom-plugin platform names |
| `clouddb_ps_pravault_platform` | `PRA Vault Username Password` | PRA Vault plugin platform |
| `clouddb_ps_workgroup` | — | Workgroup; blank → `passwordsafe_workgroup` |
| `clouddb_db_client_image_postgres` / `_mysql` / `_sqlserver` | `postgres:16` / `mysql:8.4` / `mcr.microsoft.com/mssql-tools18` | DB-client images on the jump host |
| `clouddb_ps_ssm_iam_username` | — | IAM user (functional account); blank → EC2 role mode |
| `clouddb_ps_ssm_access_key_id` / `_secret_access_key` | — | IAM-mode credentials |
| `clouddb_ps_ssm_account_suffix` | `local` | DNS-name suffix; an AssumeRole ARN for cross-account mode |
| `clouddb_ps_ssm_public_key_path` | — | Public-key path on the PS node/broker |
| `pra_config_api_client_id` / `_secret` | — | PRA Config-API account; blank → reuse `bt_client_id` / `bt_client_secret` |

### Azure — `dbazure` (Azure VM Run Command)

Instead of AWS SSM, the three **`{engine} Azure Run Command Plugin`**s reach the private
DB by sending an **Azure VM Run Command** to the shared **`clouddb-jumpoint`** VM. The
dashboard first prepares that VM over Run Command (installs the DB clients and drops the
plugin's `private.pem` / `passphrase.txt` to `/root/psplugin`), then creates the managed
user. The DB is registered on the **`{engine} Azure Run Command Plugin`** platform with the
eight-field address `vmName;resourceGroup;subscriptionId;tenantId;dbHost;dbName;certPath;sslTRUE|sslFALSE`.
Unlike AWS, the **functional account is a privileged DB login** (the minted admin) bundled
with the Azure control-plane service principal: username `SP:<admin>` (or `MSI:<admin>`),
password `clientId:clientSecret:adminPassword` (or `-:-:adminPassword` for MSI). Set
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
| `clouddb_ps_platform_azure_postgres` / `_mysql` / `_sqlserver` | `PostgreSQL/MySQL/MSSQL Azure Run Command Plugin` | Custom-plugin platform names |
| `clouddb_ps_azure_auth_mode` | `SP` | `SP` (service principal) or `MSI` — functional-account username prefix |
| `clouddb_ps_azure_cert_path` | `C:\BeyondTrust\certs\public_cert.cer` | Public-cert path on the Resource Broker (address field 7) |
| `clouddb_ps_azure_ssl` | `true` | `sslTRUE` / `sslFALSE` (address field 8) |
| `clouddb_ps_azure_sp_client_id` / `_client_secret` | — | Azure SP for the functional account; blank → reuse `azure_client_id` / `_secret` |
| `clouddb_ps_azure_plugin_private_key` / `_passphrase` | — | Plugin RSA key material dropped on the jump VM (encrypted at rest) |

---

## Layer 3 — Entitle (just-in-time access)

*Optional.* Register a managed database as a BeyondTrust **Entitle** integration so users
request **just-in-time** access to it instead of holding a standing credential. Gated by
`entitle_registration_enabled` plus a per-provision **"Register in Entitle"** toggle, and
there is a post-provision **Register** button (job `clouddb_entitle_register`) to onboard
an existing DB. Teardown deregisters on decommission. Full Entitle setup (owner, workflow,
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
`gcp_entitle_db_proxy_enabled`.

> Entitle here is independent of Password Safe (Layer 2): a DB can be registered in Entitle
> whether or not Password Safe manages its credential. The two solve different problems —
> Entitle governs *who gets in and for how long*; Password Safe governs *the credential's
> lifecycle*.

---

## Lifecycle (provisioning & decommission)

- **Provision:** from the Cloud Databases page, pick engine + cloud + region and (when
  PRA is configured) a Jump Group / Jumpoint. The record + admin credential are created
  synchronously; the `terraform apply`, tunnel brokering, and any Password Safe onboarding
  run in the **job worker** as a background job.
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
- **Tunnel shows *Unavailable* in PRA.** Usually the jumpoint host never started: set the
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
  `passwordsafe_azure_db_registration_method != off`). Failures are non-fatal and fall back
  to the legacy admin-credential staging — check the job log for the warning.

For the base BeyondTrust/PRA setup (OAuth accounts, Jump Group/Jumpoint, deploy keys), see
the [BeyondTrust integration](integrations/beyondtrust.md) doc. For the sandbox network
topology, see [Cloud Sandbox](CLOUD_SANDBOX.md).
