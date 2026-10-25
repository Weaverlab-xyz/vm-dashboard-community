# Password Safe: AWS databases (`dbssm`)

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are handing rotation of an RDS database credential to Password Safe, through AWS Systems Manager on the ECS gateway host.

Part of [Password Safe: databases](databases.md), which has the shared model: the two ways to onboard, and where the functional account comes from.

The dashboard creates the managed user by running the DB client on the shared **ECS
gateway host over AWS SSM `SendCommand`** — the only dashboard component with
line-of-sight to the private DB. `psql` / `mysql` run as a `docker run`
(`postgres:16` / `mysql:8.4`); `sqlcmd` runs **natively** from
`/opt/mssql-tools18/bin/sqlcmd`, which the onboarding's prep step installs from
Microsoft's RHEL 9 feed (there is no sqlcmd container image — `mssql-tools18` is a
package name, not a registry repository). It
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
  `ssm-user` home for credential decryption. *(For PostgreSQL/MySQL the dashboard's own
  managed-user creation uses a `docker run` client image and does not need this — that
  half is for the plugin's ongoing rotation. For SQL Server the dashboard installs
  `mssql-tools18` on the host itself, because it runs that binary too.)*
- Create a **PRA Configuration-API account** (OAuth client) with **Vault Account
  Management** permission (or leave the PRA Config-API fields blank to reuse the SRA/PRA
  credentials).
- Run the updated `setup-aws.sh` so `ecsInstanceRole` has `AmazonSSMManagedInstanceCore`
  and the dashboard IAM user has `ssm:SendCommand` / `ssm:GetCommandInvocation`.

**Config keys:**

| Key | Default | Notes |
|---|---|---|
| `clouddb_ps_onboarding_enabled` | `false` | Master toggle (AWS **and** Azure) |
| `clouddb_ps_functional_account_mode` | `create` | `create` or `reference` — see above. Overridden, most specific first, by `..._mode_<cloud>_<engine>` (only `gcp_sqlserver` exists), `..._mode_<engine>` and `..._mode_<cloud>`; blank falls through on all three |
| `clouddb_ps_self_rotation` | `false` | Emits `use_own_credentials` on the managed account, so the DB plugin's self-rotate action runs. **Required with `reference` mode** — the via-functional-account action needs a privileged DB login a provisioned server does not have |
| `clouddb_ps_platform_postgres` / `_mysql` / `_sqlserver` | `psql/mysql/mssql SSM Custom Plugin` | Custom-plugin platform names; advisory in `reference` mode |
| `clouddb_ps_functional_account_postgres` / `_mysql` / `_sqlserver` | — | `reference` mode: the operator-created account on each SSM platform |
| `clouddb_ps_pravault_platform` | `PRA Vault Username Password` | PRA Vault plugin platform |
| `clouddb_ps_pravault_functional_account` | — | `reference` mode: the operator-created account on the PRA Vault platform |
| `clouddb_ps_workgroup` | — | Workgroup; blank → `passwordsafe_workgroup` |
| `clouddb_db_client_image_postgres` / `_mysql` / `_sqlserver` | `postgres:16` / `mysql:8.4` / — | DB-client images run on the jump host. **SQL Server is blank on purpose**: Microsoft publishes no sqlcmd image (`mcr.microsoft.com/mssql-tools18` is the *package* name and does not exist as a repository), so SQL Server uses the jump host's own `/opt/mssql-tools18/bin/sqlcmd`, which the jump-host prep installs and the rotation plugin already invokes. Set it only to force a mirrored image, and only one carrying sqlcmd 18 at that path |
| `clouddb_ps_ssm_iam_username` | — | Informational only — the plugin never sees it; the mode is selected by the key pair below |
| `clouddb_ps_ssm_access_key_id` / `_secret_access_key` | — | `create` mode only: **both set → IAM mode**, either blank → EC2 role mode |
| `clouddb_ps_ssm_account_suffix` | `NoAssumeRole` | The address's `assumeRole` segment: the placeholder or a cross-account AssumeRole ARN. **≥ 12 chars** — shorter crashes the plugin, so a short persisted value is coerced on read |
| `clouddb_ps_ssm_public_key_path` | — | The address's `certPath` segment (field 4 mssql / field 5 psql+mysql): RSA public-cert path on the PS node/broker. **Required** — onboarding refuses a blank rather than packing an empty segment |
| `clouddb_ps_ssm_ssl` | `true` | mysql only: the trailing `sslTRUE` / `sslFALSE` segment (only the literal `sslTRUE` enables TLS) |
| `clouddb_ps_ssm_plugin_private_key` / `_passphrase` | — | Plugin RSA key material the dashboard drops onto the Gateway host over SSM. **Use a separate pair from Azure's** — the private keys land on different hosts |
| `clouddb_ps_ssm_key_directory` | `/home/ssm-user` | Where the SSM plugin reads that key on the jump host; blank leaves the staging manual |
| `pra_config_api_client_id` / `_secret` | — | PRA Config-API account; blank → reuse `bt_client_id` / `bt_client_secret` |
