# Entitle-compatible SQL Server — design

## Context

Registering a managed SQL Server database in Entitle (so users request just-in-time
access) fails at the connector's resource sync with
`User '<admin>' is missing required server permissions: [... 'CONTROL SERVER']`.
This is **not** a dashboard bug — the schema/version/reachability layers are solved and
the integration is created; the failure is one level deeper. Entitle's Microsoft SQL
Server connector needs **`sysadmin`** (standard mode) or **`CONTROL SERVER`** + a fixed
permission set (least-privilege mode). **None of the managed SQL Server flavors the
dashboard provisions today can grant that**, because the cloud providers reserve those
privileges for the platform. Postgres registers fine because Cloud SQL's Postgres admin
holds `cloudsqlsuperuser`+`CREATEROLE`, which the Postgres connector accepts.

Only **two** managed SQL Server offerings satisfy Entitle's connector, and neither is
what the dashboard builds by default. This doc records the compatibility landscape and
the scaffolding added to reach them.

Companion: [`entitle-resource-registration.md`](entitle-resource-registration.md) (the
broader registration architecture). The gate that hides "Register in Entitle" for the
non-viable flavors is `_entitle_viable` in
[`cloud_database_service.py`](../../web_dashboard/services/cloud_database_service.py).

## Compatibility matrix

| Offering | Admin reaches `sysadmin`/`CONTROL SERVER`? | Entitle MSSQL connector | Built by dashboard | `provider` |
|---|---|---|---|---|
| **Azure SQL Managed Instance** | ✅ admin login is a `sysadmin` member | ✅ works | scaffold (`db_azure_sqlserver_mi`) | `sql_managed_instance` |
| **AWS RDS Custom for SQL Server** | ✅ `sysadmin` + OS access | ✅ works | scaffold (`db_aws_sqlserver_custom`) | `rds_custom` |
| GCP Cloud SQL for SQL Server | ❌ `CustomerDbRootRole`, no `CONTROL SERVER` | ❌ | yes (`db_gcp_sqlserver`) | `cloudsql` |
| AWS RDS for SQL Server (standard) | ❌ `sysadmin` reserved for internal `rdsa` | ❌ | yes (`db_sqlserver`) | `rds` |
| Azure SQL Database (single/pooled) | ❌ logical server, only `##MS_*` fixed roles | ❌ | yes (`db_azure_sqlserver`) | `sql_database` |
| OCI | no managed SQL Server offering exists | — (VM only) | no | — |

The three default flavors are the walled ones; the two scaffolded offerings are the
viable ones. GCP has no viable managed SQL Server — the only GCP path to zero-standing
SQL Server access is a self-managed SQL Server on a VM (out of scope for cloud-DB).

## What this scaffolds

Live provisioning is **deferred** (prereqs + long-apply handling below); this lands the
structure code-complete and offline-tested, matching the repo's code-complete-then-E2E
pattern.

- **Offering discriminator** — an opt-in `sqlserver_tier` (`standard` default,
  `rds_custom`, `managed_instance`) threaded form → `ProvisionRequest` →
  `opts` → `_build_tf_variables`. Module + `provider` resolution keys on the tier at
  provision and on the recorded `row.provider` afterward, so apply/decommission target
  the same module. See `_SQLSERVER_OFFERINGS`, `_resolve_provider`, `_module_dir`,
  `_tier_for_provider` in
  [`cloud_database_service.py`](../../web_dashboard/services/cloud_database_service.py).
  The default path is provably unchanged — every new branch is guarded by a new
  tier/provider value existing rows never carry.
- **Two terraform modules** on the standard output contract (`instance_id` /
  `private_host` / `port`):
  [`terraform/db_aws_sqlserver_custom`](../../terraform/db_aws_sqlserver_custom/main.tf)
  and [`terraform/db_azure_sqlserver_mi`](../../terraform/db_azure_sqlserver_mi/main.tf).
  Both `terraform validate` clean (aws ~>5.0, azurerm ~>3.0).
- **Viability wiring** — the two new providers are in
  `_ENTITLE_VIABLE_SQLSERVER_PROVIDERS`, so once such a DB exists its "Register in
  Entitle" button appears and `_entitle_register_core` allows it.
- **Feature flag** — `clouddb_sqlserver_entitle_tiers_enabled` (default **off**) gates
  the provision-form tier selector, since the modules need prereqs and aren't
  live-validated. Flip it (config store or `config.py`) once prereqs are in place.

## Prerequisites (one-time, not created by the modules)

**AWS RDS Custom for SQL Server**
- A **Custom Engine Version (CEV)** built from your SQL Server installation media in an
  S3 bucket — there is no AWS-provided CEV, so `engine_version` has no default. Map it
  to the `aws_rds_custom_sqlserver_cev` config key.
- An **IAM instance profile** carrying the AWS-managed RDS Custom policy →
  `aws_rds_custom_instance_profile`.
- A **customer-managed KMS key** (RDS Custom requires encryption) →
  `aws_rds_custom_kms_key_id`.
- A supported **instance class** — RDS Custom does **not** support `db.t3.*` (default
  `db.r5.large`).

**Azure SQL Managed Instance**
- A subnet **delegated to `Microsoft.Sql/managedInstances`** with a dedicated **NSG +
  route table** (Azure enforces specific rules) → `azure_sqlmi_subnet_id`. No Private
  Endpoint (MI is natively VNet-injected). Mirror how the sandbox pre-creates the Azure
  SQL Database PE subnet.

These are natural additions to the sandbox setup scripts (à la `setup-gcp.sh`), tracked
as follow-on.

## Provisioning-time risk

Azure SQL MI **create takes ~hours** (well beyond the 5–10 min RDS-class apply the job
runner and progress milestones (`_DB_MILESTONES`) assume). Long-running-apply handling —
milestone pacing, timeout budgets, and the job runner surviving a multi-hour terraform
apply — is required before MI can be live-enabled and is out of scope for this scaffold.

## Migrating existing (walled) SQL Server DBs

There is **no** backup/snapshot/dump/restore/replication tooling anywhere in the
cloud-DB service, and the modules deliberately disable durability
(`skip_final_snapshot = true`, `backup_configuration.enabled = false`) — so
`run_decommission` destroys data. "Move an existing RDS-standard / Azure SQL Database
instance to a compatible target" is therefore **manual**:

1. Provision the compatible offering (`sqlserver_tier=rds_custom` / `managed_instance`).
2. Copy data out-of-band — native backup/restore or BACPAC, or an Ansible `localhost`
   play built on the existing `ansible_connection_vars()` seam
   ([`cloud_database_service.py`](../../web_dashboard/services/cloud_database_service.py)).
3. Cut clients over to the new DB's PRA tunnel.
4. `run_decommission` the old instance.

Automating step 2 (an explicit export/import capability) is net-new scope — nothing
exists to extend.

## Remaining work to go live

- [ ] AWS: build a CEV + media bucket + IAM instance profile + KMS key; set the three
      `aws_rds_custom_*` config keys.
- [ ] Azure: create the delegated MI subnet (+ NSG + route table); set
      `azure_sqlmi_subnet_id`.
- [ ] Long-apply handling for the ~hours MI create (`_DB_MILESTONES`, job timeouts).
- [ ] Flip `clouddb_sqlserver_entitle_tiers_enabled` on.
- [ ] Live E2E: provision → PRA tunnel reachable → Register in Entitle → connector
      resource sync succeeds (the privilege wall is gone on these offerings).
- [ ] Optional: sandbox setup-script steps for the prereqs above.
