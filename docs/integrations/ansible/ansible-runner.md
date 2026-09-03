# The Ansible runner

> **Audience:** operator · **Profile:** `both` · **Read this when:** your target is a VM, on-premises or in a cloud.

Part of [Remote Worker](../ansible.md). VM targets: SSH keys, discovery, provisioning assets, bulk runs and the local Docker runner.

## Ansible: local Docker runner (on-premises and cloud targets)

The local runner is automatic: no extra infrastructure is needed beyond the
Docker socket already mounted in `docker-compose.yml`. It handles both
on-premises hypervisors and cloud VMs — the asset is always fetched from
storage regardless of where the target lives. It is also the only runner
that can target on-premises hypervisors and the only one that forwards
WinRM `ansible_password` extra vars.

### How the inventory is built

When you click **Run**, the dashboard calls `GET /api/config-mgmt/inventory`,
which returns a dynamic Ansible JSON inventory built from every on-premises
hypervisor integration that is **both enabled and has a host configured**.

Hypervisors that are not enabled or have no host set are silently omitted —
the target picker only shows what is actually reachable. Cloud VMs appear in
separate optgroups populated from the AWS / Azure / GCP tab caches.

| Hypervisor | Ansible connection | Credentials used |
|---|---|---|
| Proxmox VE | SSH | `proxmox_password` (root@pam — requires password auth, not API-token-only) |
| VMware vSphere / ESXi | SSH | `vsphere_password` (root on ESXi; SSH must be enabled) |
| Microsoft Hyper-V | WinRM (`ansible_connection: winrm`) | `hyperv_username` + `hyperv_password`; transport/port from Settings |
| Nutanix AHV | SSH | `nutanix_password` (targets the CVM SSH interface) |
| XCP-ng / XenServer | SSH | `xcpng_password` (root — same credentials as the XAPI connection) |

### WinRM and the runner image (pywinrm)

Any Windows / WinRM target — on-prem Hyper-V **or** a Windows cloud VM (AWS / Azure /
GCP) — needs [`pywinrm`](https://pypi.org/project/pywinrm/) in the **runner image**.
The dashboard's **default** runner image, **`chrweav/ansible-winrm:latest`**, is
upstream `willhallonline/ansible` **plus** `pywinrm`, so Windows works out of the box
on every runner — no image change needed. (Source:
[`runners/ansible-winrm/`](../../../runners/ansible-winrm).)

This matters only if you **override** the image. Upstream `willhallonline/ansible`
does *not* bundle `pywinrm`, so pointing a runner at it (or any image without
pywinrm) makes Windows runs fail with *"pywinrm is not installed"*. The image
settings, all defaulting to `chrweav/ansible-winrm:latest`:

| Runner | Setting |
|---|---|
| Local Docker | `ANSIBLE_LOCAL_IMAGE` / `ansible_local_image` |
| AWS ECS | `ansible_ecs_image` |
| Azure ACI | `ansible_aci_image` |
| GCP Cloud Run | `gcp_ansible_image` |

Beyond the image, a Windows run needs WinRM enabled and reachable on the target
(`Enable-PSRemoting -Force` on Hyper-V; ports 5985/5986 open to the runner) — and on
the cloud runners the credential supplied via
[Use a secret](secrets.md#using-a-secrets-management-secret-in-a-run), since they don't forward
plaintext extra vars.

### Proxmox SSH note

The local runner authenticates to Proxmox via SSH using `proxmox_password`
(the root@pam password). If you configured Proxmox with **API token only**
(no password), the SSH connection will fail. Either:
- Set `PROXMOX_PASSWORD` in addition to the token, or
- Target Proxmox VMs individually by IP rather than using the `proxmox` group.

### ESXi SSH note

SSH is disabled by default on ESXi. Enable it via:
**Host → Manage → Services → TSM-SSH → Start**, or:

```bash
vim-cmd hostsvc/enable_ssh
```

### Changing the local Ansible image

```
ANSIBLE_LOCAL_IMAGE=chrweav/ansible-winrm:latest   # the default
```

Any image with `ansible-playbook` on its `PATH` works. The playbook and
inventory are bind-mounted into `/ansible/` inside the container. Note: an image
without `pywinrm` (e.g. upstream `willhallonline/ansible`) can't drive Windows/WinRM
targets.

---


## Cloud VM SSH keys (Ansible runner)

Cloud VM targets authenticate with an SSH key, not a password. The Ansible
runner pulls the private key from the cloud's secret store at run time:

| Cloud | Config key | Env var | Default | Source |
|---|---|---|---|---|
| AWS | `ansible_ssh_key_sm_name` | `ANSIBLE_SSH_KEY_SM_NAME` | `ec2/ssh-keypair` | AWS Secrets Manager secret name/ARN. The value may be a raw PEM or a JSON object with a `private_key` field — auto-detected. IAM needs `secretsmanager:GetSecretValue`. |
| Azure | `ansible_aci_ssh_key_secret_name` | `ANSIBLE_ACI_SSH_KEY_SECRET_NAME` | _(empty)_ | Azure Key Vault secret name holding the private key PEM. |
| GCP | `gcp_ssh_key_secret_name` | `GCP_SSH_KEY_SECRET_NAME` | _(empty)_ | GCP Secret Manager secret name; the SA needs `roles/secretmanager.secretAccessor`. |

> A legacy AWS fallback exists: `ansible_ssh_key_secret` (env
> `ANSIBLE_SSH_KEY_SECRET`, default `AWS_KEY`) — a Password Safe secret
> title. Prefer `ansible_ssh_key_sm_name`.

GCP example — store the key and grant access:

```bash
gcloud secrets create ssh-ansible-keypair --replication-policy="automatic"
gcloud secrets versions add ssh-ansible-keypair --data-file=~/.ssh/id_rsa
gcloud secrets add-iam-policy-binding ssh-ansible-keypair \
  --member="serviceAccount:SA_EMAIL" \
  --role="roles/secretmanager.secretAccessor"
```

---


## Cloud VM target discovery (Ansible runner)

The **Config Mgmt** tab reads the instance lists already cached by the AWS,
Azure, and GCP tabs — no extra API calls are needed. The target picker shows
three optgroups:

| Optgroup | Source | SSH key |
|---|---|---|
| EC2 Instances (AWS) | AWS instances tab cache | `ansible_ssh_key_sm_name` |
| Azure Virtual Machines | Azure VMs tab cache | `ansible_aci_ssh_key_secret_name` (or password auth) |
| GCE Instances (GCP) | GCP instances tab cache | `gcp_ssh_key_secret_name` |

If you have not yet navigated to the cloud tab (so the cache is empty), visit
it once to populate the list, then return to Config Mgmt.

---


## Provisioning assets (.sh / .ps1 / .rpm / .deb)

In addition to Ansible playbooks (`.yml`), you can upload **scripts and
packages** to the same storage backend. The dashboard auto-generates a
wrapper playbook based on the file extension.

| Extension | What happens |
|---|---|
| `.yml` / `.yaml` | Playbook is used as-is |
| `.sh` | `ansible.builtin.script` — script copied to the remote host and executed with `/bin/bash` |
| `.ps1` | `ansible.windows.win_script` — copied and run on a Windows host (target must have `ansible_connection=winrm`) |
| `.rpm` | `ansible.builtin.copy` + `ansible.builtin.dnf` — package is transferred and installed with `--disable-gpg-check` |
| `.deb` | `ansible.builtin.copy` + `ansible.builtin.apt` — package is transferred and installed |

Two ways to upload:

- **`/storage` page** — file picker + Upload button, goes to the active
  backend.
- **`/config-mgmt` page** — same upload form, plus inline run controls.

Either way, the upload hits `POST /api/storage/upload` and the file appears
in the asset picker on next refresh. You can also write directly to the
underlying bucket / share with the cloud's native tools (`aws s3 cp`,
`az storage blob upload`, `gsutil cp`) if you'd rather script it.

The **Config Mgmt** tab shows all asset types in the picker. A colour badge
indicates the type (Playbook / Script / PowerShell / RPM / DEB).

> **Extra vars** are forwarded only to playbooks. For scripts and packages
> the field is accepted but ignored — pass runtime parameters via the script
> itself or encode them in the filename.

---


## Bulk runs (one asset, many targets)

`/config-mgmt` runs one asset against one target. To apply a playbook across a fleet,
select rows on the **Inventory** page (`/inventory`) — filter to what you want, tick
them, and a run panel appears. Each selected resource becomes its **own job**, all
tagged with a shared `batch_id`, dispatched through the ordinary run path so every
permission check, secret-store validation and runner decision behaves exactly as it
does for a single run. Queueing lands you on `/jobs?batch_id=…` — the batch filtered
out of the job list, with a status rollup across all of it.

Every run is claimed from the `jobs` table by the **job runner** (the `worker` service
in the compose files), the same way Kubernetes and database runs are. A batch
therefore survives a dashboard restart, and its jobs execute concurrently across
`WORKER_REPLICAS` (default 3) rather than one at a time — that number is the ceiling
on how many hosts a batch touches simultaneously.

**One kind per run.** Selecting a VM locks the checkboxes on Kubernetes clusters and
databases, and vice versa. The kinds aren't interchangeable at any level: a VM run
SSHes to a host, while k8s/database runs are `localhost` plays reaching *out* over a
kubeconfig or DB login — different request fields, a different runner, and a playbook
written for one is meaningless against the other.

Rows that can never be a target are disabled with the reason on hover: **virtual
desktops** (no Ansible target exists behind a seat), **Proxmox / Nutanix VMs** (their
deploy records a node + VMID rather than an address — target them through their
hypervisor *group* instead), and databases whose engine or cloud has no runner. Those
reasons are computed server-side by the same rule the endpoint enforces, so the page
can't offer a checkbox the API would reject.

Two limits worth knowing:

- **50 targets per batch.** Each is a job, so an unbounded "select all" against a
  large estate would fan out unbounded work.
- **Selection problems refuse the whole request; per-target failures don't.** A mixed
  selection or an untargetable row is caught before any job exists. But several checks
  in the run path turn on the *target's cloud*, so a mixed-cloud VM batch can be valid
  for one host and not another — those come back in the response's `failed` list and
  are named in the toast, while the rest still run.

**Secrets and managed accounts.** The inventory panel covers asset / SSH user / extra
vars. For a run needing a Secrets-Management secret or a Password Safe managed
account, use **Continue on the Config Management page →**, which carries the selection
over and applies the full run form to it — see [Managed-account
checkout](secrets.md#managed-account-checkout-beyondtrust-password-safe) for how an account is
matched across many hosts.

Full treatment in [docs/config-management.md](../../config-management.md#bulk-runs-from-the-inventory).

---


## Storage prerequisite (Ansible runner)

The Ansible runner fetches its assets (playbooks, scripts, packages) from a
[storage backend](../../storage-management.md). At least one backend must be
configured and active on `/storage` before the Remote Worker / Ansible
feature flag can be enabled.

The four backends — S3, Azure Blob, GCS, Local Filesystem / UNC — are
configured on the dedicated **`/storage`** page. Picking the right backend:

| Use case | Recommended backend |
|---|---|
| Cloud VMs as targets, cloud Ansible runner | The matching cloud's bucket (S3 / Blob / GCS) |
| On-prem hypervisor targets, dashboard host on a corporate LAN | Local Filesystem / UNC |
| Mixed fleet, dashboard host has internet egress | Any cloud bucket — runner downloads the asset before SSH/WinRM |

Configuration steps, asset upload, migration between backends, and per-backend
IAM details all live in [docs/storage-management.md](../../storage-management.md).
(The Kubernetes runner has no storage dependency.)

---


## Troubleshooting


### Ansible — local Docker runner

**"Target X is not a configured hypervisor"** — the hypervisor integration is
either disabled or has no host set. Enable it and fill in the host in
**Settings → Integrations**.

**No targets appear in the picker** — no on-premises hypervisor is both enabled
and configured. Check **Settings → Integrations** and confirm that both the
toggle is on and the host field is filled.

**"docker: command not found"** — the Docker socket is not mounted. Verify
`docker-compose.yml` includes the `/var/run/docker.sock` bind mount and restart
the stack.

**SSH authentication failed (Proxmox / vSphere / XCP-ng)** — the stored
password must work for SSH (not just the management API). For Proxmox, this
means `PROXMOX_PASSWORD` must be set (API-token-only auth is not sufficient
for SSH). For ESXi, SSH must be enabled on the host.

**Hyper-V: "WinRM connection refused"** — WinRM is not enabled. Run
`Enable-PSRemoting -Force` on the Hyper-V host.

**"pywinrm is not installed"** (any Windows/WinRM target, any runner) — you've
**overridden** the runner image with one that lacks `pywinrm` (e.g. upstream
`willhallonline/ansible`). The default `chrweav/ansible-winrm:latest` includes it;
either clear the override or point it at an image that has pywinrm. See
[WinRM and the runner image (pywinrm)](#winrm-and-the-runner-image-pywinrm).

**Container starts but can't reach the hypervisor** — the Ansible container
runs on the same Docker network as the dashboard (`compose` default bridge).
If the hypervisor is on a separate VLAN, ensure the Docker host has a route
to it.


### Ansible — asset storage

> Storage backend configuration, asset-list issues, and per-provider IAM
> permission errors live in
> [docs/storage-management.md](../../storage-management.md#troubleshooting).
> The items below are Ansible-runner-specific concerns that the storage
> page doesn't cover.

**"No active storage backend" when running** — the feature flag got enabled
while a backend was active, but it's since been deactivated. Re-pick a
backend on `/storage` and Save.

**"Permission denied" on .sh asset at run time** — the auto-generated wrapper
uses `ansible.builtin.script` which copies + runs the file with
`executable: /bin/bash`. If the remote rejects it, write a custom `.yml`
playbook with an explicit `mode: '0755'` copy + a task to invoke it.

**.ps1 asset fails with "WinRM connection refused"** — the target's inventory
hostvars don't have `ansible_connection=winrm`. Hyper-V hostvars set this
automatically. For other hypervisors hosting Windows guests, you'll need a
custom playbook that sets `vars:` explicitly, or extend the relevant
`services/<hypervisor>_service.py` to detect Windows guests.

**Cloud VMs not in the target list** — the list is read from the in-memory
cache populated by the AWS / Azure / GCP tabs. Visit the relevant cloud tab
first so the cache is warm, then return to Config Mgmt.

**SSH authentication failed on cloud target (AWS)** — verify
`ansible_ssh_key_sm_name` is set and the IAM role has
`secretsmanager:GetSecretValue` on that secret.

**SSH authentication failed on cloud target (GCP)** — verify
`gcp_ssh_key_secret_name` is set and the service account has
`roles/secretmanager.secretAccessor` on the secret. Ensure the public key is
in the instance's `~/.ssh/authorized_keys` (injected at launch).
