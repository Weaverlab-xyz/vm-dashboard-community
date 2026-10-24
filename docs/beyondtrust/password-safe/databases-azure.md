# Password Safe: Azure databases (`dbazure`)

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are handing rotation of an Azure Flexible Server or SQL Database credential to Password Safe, through Azure Run Command on the jump VM.

Part of [Password Safe: databases](databases.md), which has the shared model: the two ways to onboard, and where the functional account comes from.

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
| `clouddb_ps_azure_sp_client_id` / `clouddb_ps_azure_sp_client_secret` | — | `create` mode only: Azure SP for the functional account; blank → reuse `azure_client_id` / `_secret` |
| `clouddb_ps_azure_plugin_private_key` / `_passphrase` | — | Plugin RSA key material dropped on the jump VM (encrypted at rest) |
