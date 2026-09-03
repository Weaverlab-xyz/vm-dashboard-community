# Password Safe rotation for cloud databases (AWS + Azure)

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are handing rotation of an AWS or Azure database credential to Password Safe.

Part of [Databases](../databases.md), Layer 2. The GCP channel is separate: see
[Cloud SQL over the Data API](password-safe-gcp.md).

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
> [docs/runbooks/clouddb-password-safe-plugin-setup.md](../runbooks/clouddb-password-safe-plugin-setup.md)
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
| `clouddb_ps_functional_account_mode` | `create` | `create` or `reference` — see above |
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
