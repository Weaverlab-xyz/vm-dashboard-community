# Sample Ansible playbooks (managed-service starters)

Ready-to-adapt playbooks for configuring **Linux** and **Windows** cloud VMs, plus
**Kubernetes clusters** and **cloud databases**, via the dashboard's **Config
Management** feature
(see [docs/integrations/ansible.md](../../docs/integrations/ansible.md)). They are
the Ansible counterpart to [`examples/compose/`](../compose/) — upload one, edit
the placeholders, and run.

## How to run

1. **Upload** the `.yml` to a storage backend — Storage page, or `POST /api/storage/upload`.
2. **Run** — Config Management (`/config-mgmt`) → pick the asset → choose a target
   (a cloud VM's IP + its cloud, or an on-prem group) → optionally set extra vars → Run.
3. **Watch** the job on the Jobs page; output (and CloudWatch/Cloud Logging logs for
   cloud runners) is linked from there.

### Supplying credentials — use a secret, not plaintext

Anything sensitive a play needs (a WinRM/SSH password, a become password, an API
token a task references) can be injected from **Secrets Management** via the run
form's **Use a secret** panel instead of a plaintext extra var — the value is never
shown, never stored on the job, and is scrubbed from the job output (requires the
`secrets:use` permission). Three ways to bind one:

- **As a named variable** — e.g. map `ansible_password` (Windows) or a var the play
  references (`admin_password`, …) to a stored secret.
- **As the become/sudo password** — injected as `ansible_become_password`.
- **As a BeyondTrust Password Safe managed account** — pick the account from the
  live list; the credential is checked out just-in-time.

See [Using a Secrets-Management secret in a run](../../docs/integrations/ansible.md#using-a-secrets-management-secret-in-a-run).
Plaintext extra vars still work for non-sensitive parameters.

## Linux (`linux/`)

`- hosts: all`, `become: yes`, generic modules so they span Debian/Ubuntu and
RHEL/Rocky/Alma. These run cleanly via the **cloud runner** (it SSHes to the VM IP
as the per-cloud user with the key the dashboard injected at deploy) or the local runner.

| File | Purpose |
|---|---|
| `patch-and-reboot.yml` | Update all packages; reboot only if required |
| `ssh-hardening.yml` | Disable root login + password auth, tighten sshd (validated reload) |
| `create-admin-user.yml` | Create a sudo user + authorized key (params via extra_vars) |
| `install-docker.yml` | Install Docker Engine from the official repos, enable the service |
| `node-exporter.yml` | Install Prometheus node_exporter as a systemd unit (:9100) |
| `nginx-web.yml` | Install + enable nginx, serve a sample page (:80) |

## Windows (`windows/`)

WinRM playbooks using `ansible.windows` / `community.windows`. The static
connection settings live in each play's `vars:`; you supply the login at run time —
the admin password via **Use a secret** (recommended: bind `ansible_password` to a
stored secret or a Password Safe managed account, so it's never shown or logged), or
as a plaintext extra var:

```
ansible_user: azureuser
ansible_password: <bind via Use a secret, or the deploy-time admin password>
```

| File | Purpose |
|---|---|
| `win-updates.yml` | Install security/critical updates, reboot |
| `win-firewall-baseline.yml` | Ensure firewall profiles enabled; sample allow rule |
| `win-install-software.yml` | Install packages via Chocolatey (git, 7zip, …) |
| `win-create-local-admin.yml` | Create a local user + add to Administrators |
| `win-feature-iis.yml` | Install the IIS web server role |

### Running the Windows samples

**The local runner is the proven path.** Set `ansible_runner = local`, target the
Windows VM's IP, ensure **WinRM is reachable** (ports 5985/5986; open it in the
NSG), and supply `ansible_user` + the admin password (via **Use a secret**, or as
extra vars). On-prem Hyper-V Windows hosts work the same way and are already wired
into the dashboard inventory.

The **cloud runner** now injects named-variable / become secrets through each
provider's secret channel (it builds an `-e @file` inside the container), so it can
carry `ansible_password` too — for Windows that's the **Azure (ACI)** runner. A play
that sets `ansible_connection: winrm` in its `vars:` overrides the runner's default
SSH connection, so a WinRM run on ACI is now workable. It's newer than the local
path, so validate it end-to-end for your image before relying on it. (The ECS /
Cloud Run runners are for Linux SSH targets.)

## Kubernetes (`k8s/`)

Localhost plays (`- hosts: localhost`, `connection: local`) using `kubernetes.core`.
Pick a registered/provisioned cluster as the target (Config Management → target kind
**Kubernetes cluster**). The dashboard token-preps the cluster's kubeconfig and injects
it into the runner (`K8S_AUTH_KUBECONFIG` / `KUBECONFIG`) — you supply nothing for the
connection. These **always run on the in-cloud runner** (ECS / ACI / Cloud Run) so they
reach a private API server and bypass the corporate TLS-inspecting proxy; they use the
`chrweav/ansible-cloud` image (kubernetes.core + the helm CLI), not `ansible-winrm`.

| File | Purpose |
|---|---|
| `list-nodes.yml` | Read-only smoke test — list node names via `k8s_info` |
| `namespace-ensure.yml` | Create a namespace (`k8s_namespace`) |
| `deployment-apply.yml` | Apply a sample nginx Deployment + Service |
| `helm-install.yml` | `helm upgrade --install` a chart (`helm_release`/`helm_chart`/…) |

## Cloud databases (`database/`)

Localhost plays using `community.postgresql` / `community.mysql` / `community.general`
(mssql). Pick a provisioned database as the target (Config Management → target kind
**Cloud database**). The dashboard resolves the admin credential server-side and injects
it as **scrubbed** extra-vars — `db_login_host`, `db_login_port`, `db_login_user`,
`db_login_password` (and `db_name`) — so you never see or type it. Like the k8s plays,
these run on the in-cloud runner (in-subnet with line-of-sight to the private endpoint)
using the `ansible-cloud` image. For a new **role/user** password, bind a
Secrets-Management secret via **Use a secret** (mapped to `target_role_password` /
`target_user_password`) rather than a plaintext extra var.

| File | Purpose |
|---|---|
| `postgres-create-database.yml` | Create a PostgreSQL database (`target_db_name`) |
| `postgres-create-role.yml` | Create a PostgreSQL login role (`target_role` + secret pw) |
| `mysql-create-database.yml` | Create a MySQL database (`target_db_name`) |
| `mysql-create-user.yml` | Create a MySQL user (`target_user` + secret pw) |
| `sqlserver-create-database.yml` | Create a SQL Server database (`target_db_name`) |

## Password Safe (`password-safe/`)

Playbooks that fetch their **own** secrets from BeyondTrust Password Safe at runtime via the
`beyondtrust.secrets_safe` Ansible Galaxy collection's `secrets_safe_lookup` plugin — the
*in-playbook* counterpart to the dashboard's out-of-band **Use a secret → managed account**
checkout. When BeyondTrust is enabled, the dashboard **auto-injects** the OAuth credentials
as `PASSWORD_SAFE_*` env into every runner, so the lookups just work (no per-run setup, no
ephemeral-secrets gate). Both runner images ship the collection.

| File | Purpose |
|---|---|
| `lookup-managed-account.yml` | Retrieve a rotated managed-account password (`system/account`) |
| `lookup-secret.yml` | Retrieve a stored secret (`folder/title`) and write it to a `0600` file |
| `vm-secret-to-host.yml` | Fetch a secret on the runner, deliver it to a Linux VM target |
| `db-credential-from-ps.yml` | Fetch a DB role password from PS, then create the Postgres role |
| `onboard-safe-and-account.yml` | Management — create a safe/folder/secret via `beyondtrust.password_safe` |

See [password-safe/README.md](password-safe/README.md) for the credential contract, path
formats, and a standalone `docker run` smoke test.

## Notes

- These are starting points — review and adapt before running against real hosts.
  `ssh-hardening.yml` disables SSH password auth, so confirm key access first.
- Playbooks use fully-qualified module names. The VM runner image
  (`chrweav/ansible-winrm` — upstream `willhallonline/ansible` + `pywinrm`) ships the
  `ansible.builtin`, `ansible.posix`, `ansible.windows`, and `community.windows`
  collections, so WinRM/Windows works out of the box. The **k8s/database** plays use a
  separate image (`chrweav/ansible-cloud`) carrying `kubernetes.core`,
  `community.postgresql`, `community.mysql`, and `community.general` (+ the helm CLI and
  DB client libs) — selected automatically for those target kinds.
- `tests/test_playbook_samples.py` validates every file here is a well-formed play
  list, so a malformed sample can't ship.
