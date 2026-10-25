# Troubleshooting cloud databases

> **Audience:** operator · **Profile:** `demo` · **Read this when:** a database will not provision, register, onboard into Password Safe or open through PRA.

Part of [Databases](../databases.md).

- **MySQL tunnel rejects the login (`mysql_native_password` unsupported).** The engine
  must be **8.4** on every cloud (8.0's admin auth plugin is rejected by the PRA tunnel;
  flipping the server parameter doesn't fix the existing admin). The modules default to
  8.4 — don't override to 8.0.
- **Tunnel shows *Unavailable* in PRA.** Usually the gateway host never started: set the
  cloud's deploy key (`aws_ecs_docker_deploy_key` / `azure_aci_docker_deploy_key` /
  `gcp_cloud_run_docker_deploy_key`). On **OCI** there is no auto-jumpoint — you must
  pre-create one in the public subnet.
- **Decommission fails on `Error deleting item with ID [n] … status: 404`.** The PRA jump
  item or Vault account was already deleted out of band (in the PRA console, or by an
  earlier half-finished teardown), and the SRA provider treats a 404 on delete as an error
  instead of as "already gone". Teardown now drops those items from its state and destroys
  the rest, so **Delete** completes on the next attempt; if it still fails, the error names
  something other than a 404 and PRA really does still hold it.
- **SQL Server: can't create a database at provision.** By design — RDS/Cloud SQL SQL
  Server connect to `master`; create app databases afterward through the tunnel.
- **Azure: `VnetWithDifferentLocationNotSupported` at apply** ("the virtual network …
  coming from a different location *X* is not supported. Requested resource location is
  *Y*"). The region was right and the **subnet** was the default region's: *Y* has no
  per-region config set, or its set leaves `db_subnet_id` / `db_mysql_subnet_id` /
  `db_sqlserver_subnet_id` blank, so it fell back to the flat key. Fill *Y*'s DB subnet
  under **Settings → Multi-region** (or re-run `setup-azure.sh` in *Y*). The provision
  route now rejects this up front — unless the dashboard's service principal lacks
  *Reader* on the VNet's resource group, in which case the check fails open and you get
  this apply error instead. The AWS twin is `DBSubnetGroupNotFoundFault`.
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

For the base BeyondTrust/PRA setup (OAuth accounts, Asset Group/Gateway, deploy keys), see
the [Privileged Remote Access](../../beyondtrust/privileged-remote-access.md) doc. For the sandbox network
topology, see [Cloud Sandbox](../../clouds/sandbox.md).
