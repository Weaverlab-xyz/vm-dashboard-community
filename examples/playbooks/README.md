# Sample Ansible playbooks (managed-service starters)

Ready-to-adapt playbooks for configuring **Linux** and **Windows** cloud VMs, plus
**Kubernetes clusters** and **databases**, via the dashboard's **Config
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

### Optional: fetch a secret from Password Safe inside the play

Several samples can source their secret **directly from BeyondTrust Password Safe**
instead, without any run-form interaction. Each such play declares an optional
`…_secret` var — set it to a SECRET path (`folder/title`) and the value is fetched
mid-run; leave it blank and the play behaves exactly as before:

| Playbook | Optional var |
|---|---|
| `windows/win-create-local-admin.yml` | `new_admin_password_secret` |
| `database/postgres-create-role.yml` | `target_role_password_secret` |
| `database/mysql-create-user.yml` | `target_user_password_secret` |
| `portainer/*.yml` | `portainer_pat_secret` |

The `PASSWORD_SAFE_*` credentials are auto-injected into every runner, so nothing else
is needed. The lookup runs on the **controller** (the runner container), so a remote
target never needs Password Safe reachability — that's why it works in the Windows
play too. [`password-safe/`](password-safe/) has the full treatment, including the
managed-account variant and the write-side modules.

Two things to know:

- **The Password Safe path wins** when both it and a directly-supplied value are set.
  The fetch writes to a private `_ps_*` fact rather than back onto the caller's
  variable — Ansible extra vars outrank `set_fact`, so writing back would be silently
  ignored whenever the value was also supplied.
- **Connection credentials stay with the run form.** Binding the SSH key, become
  password or `ansible_password` through **Use a secret** covers cases an in-playbook
  lookup can't (SSH keys, `sshpass`, ephemeral cloud secrets). These `…_secret` vars
  are for secrets a *task* consumes.

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

## Docker Swarm (`swarm/`)

`hosts: all`, `become: true` plays that stand up and operate a Swarm on **on-prem
Linux hosts** — Swarm's natural home. Prerequisite: Docker, via
[`linux/install-docker.yml`](linux/install-docker.yml).

| File | Purpose |
|---|---|
| `swarm-init.yml` | Initialise a manager; prints the worker + manager join commands |
| `swarm-join.yml` | Join a node as worker (default) or manager |
| `swarm-open-ports.yml` | Open 2377/tcp, 7946/tcp+udp, 4789/udp (firewalld or ufw) |
| `swarm-stack-deploy.yml` | `docker stack deploy` a compose file on a manager |
| `swarm-status.yml` | Read-only — node role, and on a manager the nodes/services/stacks |
| `swarm-leave.yml` | Remove a node from the swarm (guarded; `confirm: true` required) |

### Bootstrapping a cluster

Config Management runs against **one target per run**, so a cluster is built one node
at a time with the join token relayed between runs:

1. `swarm-open-ports.yml` on every node (skip if your network already allows it).
2. `swarm-init.yml` on the first manager → its output contains `manager_addr` and
   `join_token`.
3. `swarm-join.yml` on each remaining node, passing those two as extra vars
   (`join_role: manager` for additional managers, using the manager token).
4. `swarm-status.yml` on the manager to confirm everyone arrived.

**Use the local runner.** Cloud runners don't forward plaintext extra vars, and the
relay depends on them.

### Keeping the token out of job output

Set `join_token_secret` on **both** halves and the token is never printed anywhere:

```
swarm-init.yml   join_token_secret: infra/swarm-worker-token   # writes it
swarm-join.yml   join_token_secret: infra/swarm-worker-token   # reads it back
```

`swarm-init.yml` stores the token with `beyondtrust.secrets_safe.secrets_create` and
prints only the path; `swarm-join.yml` fetches it with the lookup. Both use the
auto-injected `PASSWORD_SAFE_*` credentials, so there's nothing extra to configure —
but the **write side needs create rights** on `token_safe` (default `Automation`),
which is more than the read scope the other samples need, and the safe and folder must
already exist ([`password-safe/onboard-safe-and-account.yml`](password-safe/onboard-safe-and-account.yml)
creates them).

Three things worth knowing:

- **Without those paths set, the join token is printed** by `swarm-init.yml` —
  deliberately, since the relay needs you to read it. Anyone who can see the job and
  reach `:2377` can then join the swarm, so rotate it when you're done
  (`docker swarm join-token --rotate worker`) or use the Password Safe route above.
- **These drive the `docker` CLI, not `community.docker`.** That collection isn't in
  the runner image's documented set, and its modules would additionally need the
  Docker SDK for Python on every target, which `install-docker.yml` doesn't install.
  Idempotency is hand-rolled off a `docker info` state probe and pinned by
  `tests/test_playbook_swarm.py`.
- **Swarm honours the compose `deploy:` block** — `replicas`, `placement`,
  `update_config` — so the [`examples/compose/`](../compose/) files work here and can
  be extended with them. That's the opposite of the dashboard's *cloud* compose deploy
  (Containers → Cloud), which targets ECS/ACI/GCE and rejects those keys outright.

## Kubernetes: k3s (`k3s/`)

`hosts: all`, `become: true` plays that **build** an on-prem Kubernetes cluster —
something the dashboard otherwise can't do (its Terraform modules are cloud-only and
Rancher is import-only). k3s: single binary, bundled CNI, and a join token that's just
a file read.

| File | Purpose |
|---|---|
| `k3s-server-init.yml` | Install the first control-plane node; store or print the node token |
| `k3s-join.yml` | Join a node as `agent` (default) or additional `server` |
| `k3s-open-ports.yml` | Open 6443/tcp, 8472/udp, 10250/tcp (+ etcd for HA) |
| `k3s-kubeconfig.yml` | Fetch the admin kubeconfig, rewrite its server address, print or store |
| `k3s-status.yml` | Read-only — service state, version, and on a server the nodes/pods |
| `k3s-uninstall.yml` | Run k3s's uninstall script (guarded; `confirm: true` required) |

### Building a cluster

Same node-by-node shape as `swarm/`, since a run targets one host:

1. `k3s-open-ports.yml` on every node (skip if your network already allows it).
2. `k3s-server-init.yml` on the first server → gives you `server_url` and the node token.
3. `k3s-join.yml` on each remaining node (`node_role: server` for extra control-plane
   nodes — the first server must have been started with `cluster_init: true`).
4. `k3s-status.yml` on a server to confirm everyone registered.
5. `k3s-kubeconfig.yml` on a server to collect the kubeconfig for registration.

**Use the local runner**, and note the install fetches `get.k3s.io` over HTTPS, so the
nodes need egress. Air-gapped installs are out of scope.

Set `node_token_secret` / `kubeconfig_secret` on both halves to route the token and
kubeconfig through Password Safe instead of job output — the same pattern as
`swarm/`, and the write side needs create rights on the safe.

### Running playbooks against the cluster you just built

Once registered, a `cloud=local` cluster is a Config Management target like any other:
pick it under **Kubernetes Clusters** and run the [`k8s/`](k8s/) samples against it.

It runs differently from a cloud cluster, though, and the difference is the reason it
works at all. An EKS/AKS/GKE run is dispatched to a transient runner *inside* that cloud,
because the control plane is private to its VPC. Your on-prem cluster is the opposite
case — an ECS task has no route to your LAN — so the run happens in a sibling container
**on the dashboard host**, which is the only thing with line-of-sight. Two consequences:

- The dashboard host needs a working `docker` CLI and network reach to the cluster's API
  address. A dashboard deployed *in* a cloud has neither and will refuse the run with a
  message saying so.
- Local filesystem storage works fine for these runs. The "move it to S3 first" rule
  exists because in-cloud runners can't read the dashboard's disk; the local runner is
  the dashboard's disk.

### One thing that will bite you otherwise

- **The registered kubeconfig is stored and used verbatim, as standing cluster-admin.**
  The dashboard only rewrites *cloud* exec-auth kubeconfigs; anything else is used
  as-is. k3s's admin kubeconfig is a client certificate you can't revoke without
  re-issuing the cluster CA. For anything past a lab, mint a dedicated ServiceAccount
  with a scoped ClusterRole and register a kubeconfig built from its token.

  This matters more now that the cluster is a Config Management target: that credential
  is no longer just stored, it's what every playbook run authenticates with. Registering
  cluster-admin means anyone who can start a run has cluster-admin.

Importing into Rancher works — the agent dials *outbound*, so no inbound opening is
needed — but the Rancher node's firewall only auto-whitelists clusters the dashboard
*provisioned*. A registered cluster has no known egress IP, so add your site's NAT
address to `rancher_allowed_source_cidrs` by hand.

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

## Databases (`database/`)

Localhost plays using `community.postgresql` / `community.mysql` / `community.general`
(mssql). Pick a provisioned **or registered** database as the target (Config Management →
target kind **Databases**). The dashboard resolves the admin credential server-side — from
the provisioning job for a database it built, or a just-in-time Password Safe
managed-account checkout for a registered one — and injects
it as **scrubbed** extra-vars — `db_login_host`, `db_login_port`, `db_login_user`,
`db_login_password` (and `db_name`) — so you never see or type it. Like the k8s plays,
these use the `ansible-cloud` image: on the in-cloud runner for a cloud-hosted database
(in-subnet with line-of-sight to the private endpoint), or a sibling container on the
dashboard host for an on-premises one. For a new **role/user** password, bind a
Secrets-Management secret via **Use a secret** (mapped to `target_role_password` /
`target_user_password`) rather than a plaintext extra var.

| File | Purpose |
|---|---|
| `postgres-create-database.yml` | Create a PostgreSQL database (`target_db_name`) |
| `postgres-create-role.yml` | Create a PostgreSQL login role (`target_role` + secret pw) |
| `mysql-create-database.yml` | Create a MySQL database (`target_db_name`) |
| `mysql-create-user.yml` | Create a MySQL user (`target_user` + secret pw) |
| `sqlserver-create-database.yml` | Create a SQL Server database (`target_db_name`) |

## Portainer (`portainer/`)

Localhost plays that reach **out** to the Portainer REST API with
`ansible.builtin.uri` — they configure your Docker hosts *through* Portainer rather
than SSHing to them, so the target you pick is irrelevant (nothing is installed on it).

The connection is **auto-injected** when Portainer is configured — from Settings →
Integrations → Portainer CE, or written by a managed-node deploy. `PORTAINER_URL`,
`PORTAINER_PAT` and `PORTAINER_VERIFY_SSL` arrive as environment variables on the
runner (the same channel as the `PASSWORD_SAFE_*` vars), so you supply nothing for the
connection and the token never appears in a job's output — it's added to the scrub set.
Override `portainer_url` / `portainer_pat` as extra vars to target a different server.

`PORTAINER_VERIFY_SSL` is worth honouring rather than hard-coding `validate_certs: true`:
a dashboard-deployed node serves a **self-signed** certificate on :9443, so the deploy
turns verification off and these plays follow suit.

| File | Purpose |
|---|---|
| `list-endpoints.yml` | Read-only smoke test — list environments and their online status |
| `deploy-stack.yml` | Create **or update** a compose stack (`stack_name`, `endpoint_id`, `stack_file`/`stack_content`) |
| `stack-remove.yml` | Remove a stack; a missing stack is a no-op, not a failure |
| `prune-containers.yml` | Reclaim disk — prune stopped containers, optionally images/volumes |

> `prune-containers.yml` is destructive, and `prune_volumes: true` deletes any volume
> not attached to a container. It is off by default; opt in deliberately.

The API tasks set `no_log: true` because the token rides the request headers — a
PS-fetched token is never seen by the dashboard, so it can't be scrubbed from job
output the way the injected one is. That also masks API error bodies; drop the
`no_log` on a single task temporarily if you need to debug a failing call.

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
