# Provisioning a cloud database

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are standing up a managed database on AWS, Azure, GCP, OCI or MongoDB Atlas, and want each engine's and each cloud's specifics.

Part of [Databases](../databases.md).

## AWS (RDS)

Engines: postgres / mysql / sqlserver. Sandbox: [`scripts/sandbox/Linux/setup-aws.sh`](../../../scripts/sandbox/Linux/setup-aws.sh).

**What the sandbox creates for the DB feature:** two private DB subnets in distinct AZs
(RDS needs ≥2) → the RDS **DB subnet group** `dashboard-sandbox-db`; a Postgres
**parameter group** with `rds.force_ssl=0` (`clouddb-nossl-pg16`); a MySQL-8.4
**parameter group** with `require_secure_transport=0` (`clouddb-nossl-mysql84`); a **DB
security group** allowing 5432/3306/1433/1521 *from the gateway SG only*; the `bt-jumpoint`
ECS cluster + `ecsInstanceRole` + `ecsTaskExecutionRole`; and RDS/ECS/PassRole
permissions on the scoped dashboard IAM user.

**Engine quirks:** MySQL is pinned to **8.4** (8.0's `mysql_native_password` admin is
rejected by the PRA MySQL tunnel; 8.4 defaults to `caching_sha2_password`). SQL Server
(`sqlserver-ex`) has **no `db_name`** — you connect to `master` and create databases
afterward — and its instance class is bumped to `db.t3.small` (needs ≥2 GiB).

**Oracle (RDS):** Standard Edition 2, **license-included** (the Oracle licence is in the
hourly price — no BYOL). The default engine is `oracle-se2-cdb`, a single-tenant container
database: RDS creates one PDB named after the row id (`ORA` + 5 hex characters — Oracle
names are capped at 8) and a CDB it calls `RDSCDB`, and the PDB is the service name you and
Entitle connect to. That matters because **Entitle's Oracle connector manages PDBs only**;
`oracle_engine = "oracle-se2"` (non-CDB) is accepted but can't be registered in Entitle.
`db.t3.small` minimum, 20 GiB storage minimum, port 1521, and creation takes **20–40
minutes** (the module allows 90). There is no parameter group: the PRA tunnel is a raw TCP
port forward, so nothing server-side needs relaxing. The DB security group must admit
**1521** — `setup-aws.sh` / `Setup-AwsSandbox.ps1` add it, and re-running either against an
existing sandbox adds the rule in place. Password Safe onboarding uses the native Oracle
platform — see [Oracle on RDS: the native platform](../databases.md#oracle-on-rds-the-native-platform).

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

## Azure (Flexible Server / SQL Database)

Engines: postgres / mysql (Flexible Server) + sqlserver (Azure SQL DB + Private
Endpoint). Sandbox: [`scripts/sandbox/Linux/setup-azure.sh`](../../../scripts/sandbox/Linux/setup-azure.sh).

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

> **That fallback is why the form only offers configured regions.** A region with no
> config set — or one whose `db_*` field is blank — resolves the subnet back to the flat
> key, i.e. the **default region's** subnet, while `location` is the region you picked.
> Azure refuses that pairing ~90 seconds into the apply
> (`VnetWithDifferentLocationNotSupported`), AWS a little sooner (`DBSubnetGroupNotFound`),
> and neither error names the setting at fault. So the provision form's Region picker
> lists `deployable_regions` — the configured default plus each region with a config set
> — and `POST /api/databases` refuses an unconfigured region, or a resolved subnet the
> cloud confirms is elsewhere, **before** it writes a row or creates a job. The
> confirmation step fails **open**: if the dashboard's service principal lacks *Reader*
> on the VNet's resource group the lookup returns nothing and the request proceeds, so
> seeing the Terraform error instead of the 400 points at that grant. Add regions under
> **Settings → Multi-region**, or re-run the sandbox script in the region. (OCI is
> exempt — it has no per-region sets and a free-tier ADB uses no network of ours.)

**Checklist:** run `setup-azure.sh` → import the six `azure_db_*` keys + RG/location +
`azure_jumpoint_subnet_id` → set `azure_aci_docker_deploy_key` + PRA keys → provision.

## GCP (Cloud SQL)

Engines: postgres / mysql / sqlserver. Sandbox: [`scripts/sandbox/Linux/setup-gcp.sh`](../../../scripts/sandbox/Linux/setup-gcp.sh).

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

## OCI (Autonomous Database) — read the caveats

Engine: **`oracle` only** (Autonomous Database). Sandbox: [`scripts/sandbox/Linux/setup-oci.sh`](../../../scripts/sandbox/Linux/setup-oci.sh).

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
> see [Kubernetes → OCI OKE](../kubernetes/k8s.md#oci-oke--experimental).

**Config keys** (all Settings fields): `oci_tenancy_ocid`, `oci_user_ocid`,
`oci_fingerprint`, `oci_private_key` (+ `oci_private_key_passphrase`), `oci_region`
(`us-ashburn-1`), `oci_compartment_ocid`, `oci_vcn_ocid`, `oci_default_subnet_ocid`,
`oci_vault_ocid`. There are no dedicated `oci_db_*` networking keys (Autonomous DB is
fully managed PaaS — no parameter groups, delegated subnets, or private DNS zones), and
OCI has no per-region config sets.

**Checklist:** run `setup-oci.sh` → import the `oci_*` credential + compartment/VCN keys →
**stand up your own Gateway in the OCI public subnet** and point `bt_jumpoint_name` at it
→ provision (default = free-tier public ADB).

## MongoDB Atlas

An Atlas row keeps `cloud` = the **backing provider** (AWS, Azure or GCP) and is marked by
`provider = "atlas"`. The cluster runs in that cloud and region, beside the PRA gateway that
reaches it, so it reuses the per-region config and the gateway host every other row uses.

**One Atlas project per cluster.** Atlas IP access lists and database users are
*project*-scoped. With two clusters in one project, both would share the gateway's access-list
entry, and decommissioning either would cut the other off. A project of its own makes
everything in it that cluster's alone: decommission removes it all, and Entitle's integration
is scoped to it. Projects are free. This is why the dashboard needs an **organization**, not a
project.

**Reachability — read this.** Flex clusters have **no private endpoint**. The endpoint is
public and the project access list admits only the **gateway's egress /32**, which the
dashboard reads from the gateway it ensures *before* the apply (no gateway or no egress IP =
the provision is refused before anything billable exists). TLS and the database user's
password apply on top, and the PRA tunnel is still the only path users are given. This is the
same exception free-tier OCI Autonomous DB has. PrivateLink on a dedicated tier is not built.

**Tiers:** **Flex** (default; shared, roughly $8–30 a month) or a dedicated size — **M10**,
M20, M30 from the form (M10 starts around $57 a month). Atlas is billed by MongoDB, outside the
cloud cost tooling. Creation takes about 7–10 minutes.

**Connecting.** The tunnel is a raw TCP forward to **one** replica-set member (the first host
of the `mongodb://` seed list), so connect with `directConnection=true`, and
`tlsAllowInvalidHostnames=true` because 127.0.0.1 is never the certificate's name. On a
dedicated cluster that member can be a secondary: reads need
`readPreference=secondaryPreferred`, and writes need the primary. PRA 26.3's MongoDB tunnel
lifts this once the `beyondtrust/sra` provider exposes it.

**Configuration Management:** a run connects from the runner, not the tunnel, so the
runner's egress IP must be in `atlas_extra_access_cidrs`. The play gets `db_tls`,
`db_auth_source` and the cluster's `db_replica_set`, so the driver finds the primary on a
dedicated tier too. Atlas refuses `createUser` over the wire, so the user sample stops on an
Atlas target; the database sample works there.

**Admin user:** the form's admin username, created as an Atlas database user with
`atlasAdmin` on `admin`, scoped to the cluster. Atlas manages database users only through its
Admin API, so Password Safe onboarding is not offered.

**Config keys** (Settings → Databases → MongoDB Atlas):

| Key | Default | Notes |
|---|---|---|
| `atlas_org_id` | — | Organization the per-cluster projects are created in |
| `atlas_client_id` / `atlas_client_secret` | — | Atlas **service account** (OAuth client credentials) with **Organization Project Creator**; passed to Terraform as `MONGODB_ATLAS_CLIENT_ID` / `_SECRET`, never as variables |
| `atlas_default_tier` | `FLEX` | Used when the form sends no tier |
| `atlas_extra_access_cidrs` | — | CSV added to every cluster's access list — e.g. Entitle's egress IPs |

Azure and GCP region ids map to Atlas's own names (`eastus2` → `US_EAST_2`, `us-central1` →
`CENTRAL_US`); a region with no known mapping is refused unless the API call passes
`atlas_region`.
