# Ansible Integration

## What is it?

The Ansible integration lets you run Ansible playbooks and provisioning assets
(`.sh` / `.ps1` scripts, `.rpm` / `.deb` packages) from the dashboard as tracked
background jobs. Assets are stored on a [storage backend](../storage-management.md)
of your choice and executed by a one-shot Ansible runner container that's
destroyed when the run finishes.

> **Read these first:**
> - [`docs/config-management.md`](../config-management.md) — philosophy,
>   best practices, the security argument for one-shot runners, and where
>   SaaS extends this.
> - [`docs/storage-management.md`](../storage-management.md) — full
>   reference for the four storage backends (AWS S3, Azure Blob, GCS,
>   Local / UNC), their per-backend settings, and the migrate flow.
>
> This page is the *integration-specific* guide: feature-flag activation,
> per-runner setup, cloud-VM credential plumbing, and Ansible-specific
> troubleshooting.

**Storage and execution targets are independent.** You can store assets in S3
and run them against on-premises Proxmox hosts, or store them on a corporate
UNC share and target EC2 instances — any combination works (with one
constraint: cloud runners can't read from a UNC backend; see
[storage-management.md](../storage-management.md#constraint-local-backend-only-works-with-the-local-ansible-runner)).

Four execution paths are available:

| Path | When to use | How it runs |
|---|---|---|
| **Local Docker** | Any target — on-premises hypervisors *and* cloud VMs reachable from the dashboard host | Sibling container spawned via the mounted Docker socket; assets fetched from storage then Ansible SSHes / WinRMs to the target |
| **Cloud runners** | Cloud VMs in private subnets without a path back to the dashboard host | AWS ECS task, Azure Container Instance, or GCP Cloud Run Job — one per cloud |

The **Config Mgmt** tab shows an asset picker and a target picker. The target
list is built automatically from:
- On-premises hypervisors that are enabled and configured (Proxmox, vSphere,
  Hyper-V, Nutanix, XCP-ng)
- Cloud VMs already deployed via the AWS, Azure, and GCP tabs

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Asset storage | **An active storage backend** configured on `/storage`. Required before the Ansible feature flag can be enabled. See [docs/storage-management.md](../storage-management.md). |
| **Local runner:** Docker socket | Already mounted in `docker-compose.yml` — no extra setup |
| **Cloud runners:** ECS / ACI / Cloud Run | Only needed if you prefer the runner to be cloud-local to the VM |
| Credentials | Local runner reuses the credentials already stored for each hypervisor integration |
| **Cloud VM SSH key (AWS)** | `ANSIBLE_SSH_KEY_SM_NAME` — AWS Secrets Manager secret holding the private key PEM |
| **Cloud VM SSH key (GCP)** | `GCP_SSH_KEY_SECRET_NAME` — GCP Secret Manager secret holding the private key PEM |

---

## Step 1 — Configure storage

The four backends — S3, Azure Blob, GCS, Local Filesystem / UNC — are
configured on the dedicated **`/storage`** page. The Ansible feature flag
will refuse to enable until at least one backend is configured and
selected as active.

Picking the right backend by use case:

| Use case | Recommended backend |
|---|---|
| Cloud VMs as targets, cloud Ansible runner | The matching cloud's bucket (S3 / Blob / GCS) |
| On-prem hypervisor targets, dashboard host on a corporate LAN | Local Filesystem / UNC |
| Mixed fleet, dashboard host has internet egress | Any cloud bucket — runner downloads the asset before SSH/WinRM |

Configuration steps, asset upload, migration between backends, and per-
backend IAM details all live in [docs/storage-management.md](../storage-management.md).
Come back here when storage shows green on `/storage`.

---

## Step 2 — Enable in the dashboard

1. Open **`/storage`** and configure at least one backend; pick it as active.
2. Open **Settings → Integrations**. The Ansible toggle, previously
   greyed out, is now selectable.
3. Click **Configure** on the Ansible row to set the runner
   (`local` / `ecs` / `aci` / `gcp`) and the per-cloud SSH usernames
   (see below).
4. Toggle Ansible **on**. Done.

Cloud SSH key config and cloud runner config are optional — see the
sections below if you plan to target cloud VMs.

### Per-cloud SSH user

Each cloud's stock image ships with a different default username
(`ec2-user` / `azureuser` / `gcp-user`), so the Settings → Ansible
panel exposes three fields rather than one:

| Field | Default | Override per job? |
|---|---|---|
| `ansible_aws_user` | `ec2-user` | Yes — the run-asset form on `/config-mgmt` pre-fills from this when the operator picks an `aws:` target, but the field stays editable. |
| `ansible_azure_user` | `azureuser` | Yes — same flow for `azure:` targets. |
| `ansible_gcp_user` | `gcp-user` | Yes — same flow for `gcp:` targets. |

The pre-fill is non-clobbering: a value the operator types by hand is
never overwritten when they switch targets. The submitted `ansible_user`
is whatever the field holds at submit time.

---

## Local Docker runner (on-premises and cloud targets)

The local runner is automatic: no extra infrastructure is needed beyond the
Docker socket already mounted in `docker-compose.yml`. It handles both
on-premises hypervisors and cloud VMs — the asset is always fetched from
cloud storage regardless of where the target lives.

### How the inventory is built

When you click **Run**, the dashboard calls `GET /api/config-mgmt/inventory`,
which returns a dynamic Ansible JSON inventory built from every on-premises
hypervisor integration that is **both enabled and has a host configured**.

Hypervisors that are not enabled or have no host set are silently omitted —
the target picker only shows what is actually reachable. Cloud VMs appear
in separate optgroups populated from the AWS / Azure / GCP tab caches.

| Hypervisor | Ansible connection | Credentials used |
|---|---|---|
| Proxmox VE | SSH | `proxmox_password` (root@pam — requires password auth, not API-token-only) |
| VMware vSphere / ESXi | SSH | `vsphere_password` (root on ESXi; SSH must be enabled) |
| Microsoft Hyper-V | WinRM (`ansible_connection: winrm`) | `hyperv_username` + `hyperv_password`; transport/port from Settings |
| Nutanix AHV | SSH | `nutanix_password` (targets the CVM SSH interface) |
| XCP-ng / XenServer | SSH | `xcpng_password` (root — same credentials as the XAPI connection) |

### Hyper-V WinRM requirements

The Ansible `community.windows` collection (included in
`willhallonline/ansible`) is required for Windows playbooks. If `pywinrm`
is not bundled in your image, install it:

```bash
pip install pywinrm
```

Or use a custom image that includes it:

```
ANSIBLE_LOCAL_IMAGE=my-registry/ansible-winrm:latest
```

WinRM must be enabled on the Hyper-V host (`Enable-PSRemoting -Force`) — the
same requirement as the Hyper-V management integration.

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

### Changing the Ansible image

```
ANSIBLE_LOCAL_IMAGE=willhallonline/ansible:latest
```

Any image with `ansible-playbook` on its `PATH` works. The playbook and
inventory are bind-mounted into `/ansible/` inside the container.

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

Either way, the upload hits `POST /api/storage/upload` and the file
appears in the asset picker on next refresh. You can also write
directly to the underlying bucket / share with the cloud's native tools
(`aws s3 cp`, `az storage blob upload`, `gsutil cp`) if you'd rather
script it.

The **Config Mgmt** tab shows all asset types in the picker. A colour
badge indicates the type (Playbook / Script / PowerShell / RPM / DEB).

> **Extra vars** are forwarded only to playbooks. For scripts and
> packages the field is accepted but ignored — pass runtime parameters
> via the script itself or encode them in the filename.

---

## Cloud runners (cloud VM targets)

Use cloud runners when your target VMs live in AWS, Azure, or GCP. Cloud
runners launch a fresh container in the cloud that has network access to your
VMs. They are not needed for on-premises targets.

### AWS ECS runner

```
ANSIBLE_ECS_CLUSTER=bt-jumpoint
ANSIBLE_ECS_TASK_FAMILY=ansible-config-mgmt
ANSIBLE_ECS_IMAGE=willhallonline/ansible:latest
ANSIBLE_ECS_CPU=256
ANSIBLE_ECS_MEMORY=512
ANSIBLE_ECS_EXECUTION_ROLE_ARN=arn:aws:iam::123456789012:role/ecsTaskExecutionRole
```

`ANSIBLE_ECS_EXECUTION_ROLE_ARN` is only required if your image is in a private
ECR registry.

SSH key source for AWS cloud targets (cloud VMs need a key, not a password):

```
ANSIBLE_SSH_KEY_SM_NAME=ec2/ssh-keypair   # AWS Secrets Manager secret name/ARN
```

The secret value may be a raw PEM string or a JSON object with a `private_key`
field. The dashboard auto-detects which format is used.

### Azure ACI runner

```
AZURE_ACI_RESOURCE_GROUP=rg-config-mgmt
AZURE_ACI_SUBNET_ID=/subscriptions/.../subnets/ansible-runner
AZURE_ANSIBLE_ACI_IMAGE=willhallonline/ansible:latest
AZURE_ACI_CPU=1.0
AZURE_ACI_MEMORY=2.0
```

The ACI runner inherits Azure credentials from `AZURE_CLIENT_ID` /
`AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID` / `AZURE_SUBSCRIPTION_ID`.

### GCP Cloud Run Jobs runner

```
GCP_ANSIBLE_CLOUD_RUN_REGION=us-central1
GCP_ANSIBLE_IMAGE=willhallonline/ansible:latest
GCP_ANSIBLE_VPC_CONNECTOR=   # optional — see below
```

Required service account roles:

| Role | Purpose |
|---|---|
| `roles/run.admin` | Create, execute, and delete Cloud Run Jobs |
| `roles/logging.viewer` | Retrieve job output from Cloud Logging |
| `roles/iam.serviceAccountUser` | Act as a service account when submitting jobs |

Cloud Run Jobs run in a Google-managed VPC by default and cannot reach private
RFC-1918 addresses. To reach private GCE instances, create a Serverless VPC
Access connector:

```bash
gcloud compute networks vpc-access connectors create ansible-runner \
  --region us-central1 --network default --range 10.8.0.0/28
```

Then set `GCP_ANSIBLE_VPC_CONNECTOR=projects/PROJECT_ID/locations/us-central1/connectors/ansible-runner`.

### SSH key source for GCE targets

```
GCP_SSH_KEY_SECRET_NAME=ssh-ansible-keypair   # GCP Secret Manager secret name
```

Store the private key (PEM) as a secret in Secret Manager. The service account
needs `roles/secretmanager.secretAccessor` on that secret.

```bash
gcloud secrets create ssh-ansible-keypair --replication-policy="automatic"
gcloud secrets versions add ssh-ansible-keypair --data-file=~/.ssh/id_rsa
gcloud secrets add-iam-policy-binding ssh-ansible-keypair \
  --member="serviceAccount:SA_EMAIL" \
  --role="roles/secretmanager.secretAccessor"
```

---

## Cloud VM target discovery

The **Config Mgmt** tab reads the instance lists already cached by the AWS,
Azure, and GCP tabs — no extra API calls are needed. The target picker shows
three optgroups:

| Optgroup | Source | SSH key |
|---|---|---|
| EC2 Instances (AWS) | AWS instances tab cache | `ANSIBLE_SSH_KEY_SM_NAME` |
| Azure Virtual Machines | Azure VMs tab cache | Password auth (no key required) |
| GCE Instances (GCP) | GCP instances tab cache | `GCP_SSH_KEY_SECRET_NAME` |

If you have not yet navigated to the cloud tab (so the cache is empty), visit
it once to populate the list, then return to Config Mgmt.

---

## Playbook structure

### On-premises hypervisor playbook

Target the `proxmox`, `vsphere`, `hyperv`, `nutanix`, or `xcpng` group
(whichever is configured). Or use `on_premises` to hit all of them.

```yaml
# harden-proxmox.yml
- hosts: proxmox
  become: yes
  tasks:
    - name: Ensure auditd is running
      service:
        name: auditd
        state: started
        enabled: true
```

```yaml
# restart-hyperv-service.yml
- hosts: hyperv
  tasks:
    - name: Restart the dashboard service
      win_service:
        name: DashboardSvc
        state: restarted
```

### Cloud VM playbook (single-host, ad-hoc)

For cloud targets the dashboard passes the IP as `-i <host>,` to Ansible:

```yaml
# hardening.yml
- hosts: all
  become: yes
  tasks:
    - name: Ensure sshd is running
      service:
        name: sshd
        state: started
        enabled: true
```

### Provisioning asset examples

**Script (install-agent.sh)** — upload a `.sh` file; the dashboard wraps it
automatically:

```bash
#!/bin/bash
set -euo pipefail
curl -fsSL https://packages.example.com/agent.sh | bash
systemctl enable --now example-agent
```

**RPM package (my-agent-1.0.rpm)** — upload the `.rpm` directly. The dashboard
generates:

```yaml
- hosts: all
  become: yes
  tasks:
    - name: Copy my-agent-1.0.rpm to remote
      ansible.builtin.copy:
        src: /ansible/assets/my-agent-1.0.rpm
        dest: /tmp/my-agent-1.0.rpm
    - name: Install my-agent-1.0.rpm
      ansible.builtin.dnf:
        name: /tmp/my-agent-1.0.rpm
        state: present
        disable_gpg_check: true
```

### Sample playbooks

Ready-to-adapt starters for Linux and Windows cloud VMs live in
[`examples/playbooks/`](../../examples/playbooks/) — patching, SSH hardening,
admin-user creation, Docker, node_exporter, nginx (Linux); Windows updates,
firewall, Chocolatey, local admin, and IIS (Windows). See
[examples/playbooks/README.md](../../examples/playbooks/README.md) for how to run
each. **Linux** samples run via the cloud or local runner; **Windows** (WinRM)
samples run via the **local runner**, which forwards the `ansible_password` extra
var the WinRM connection needs (the cloud runner is SSH-only and doesn't forward
extra vars).

---

## Troubleshooting

### Local Docker runner

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

**Hyper-V: "pywinrm is not installed"** — the Ansible image doesn't include
`pywinrm`. Set `ANSIBLE_LOCAL_IMAGE` to an image that does, or build a custom
image.

**Container starts but can't reach the hypervisor** — the Ansible container
runs on the same Docker network as the dashboard (`compose` default bridge).
If the hypervisor is on a separate VLAN, ensure the Docker host has a route
to it.

### Asset storage

> Storage backend configuration, asset-list issues, and per-provider
> IAM permission errors live in [docs/storage-management.md](../storage-management.md#troubleshooting).
> The items below are Ansible-runner-specific concerns that the storage
> page doesn't cover.

**"No active storage backend" when running** — the Ansible feature flag
got enabled while a backend was active, but it's since been
deactivated. Re-pick a backend on `/storage` and Save.

**"Permission denied" on .sh asset at run time** — the auto-generated
wrapper uses `ansible.builtin.script` which copies + runs the file with
`executable: /bin/bash`. If the remote rejects it, write a custom `.yml`
playbook with an explicit `mode: '0755'` copy + a task to invoke it.

**.ps1 asset fails with "WinRM connection refused"** — the target's
inventory hostvars don't have `ansible_connection=winrm`. Hyper-V
hostvars set this automatically. For other hypervisors hosting Windows
guests, you'll need a custom playbook that sets `vars:` explicitly, or
extend the relevant `services/<hypervisor>_service.py` to detect Windows
guests.

**Cloud VMs not in the target list** — the list is read from the
in-memory cache populated by the AWS / Azure / GCP tabs. Visit the
relevant cloud tab first so the cache is warm, then return to Config
Mgmt.

**SSH authentication failed on cloud target (AWS)** — verify
`ANSIBLE_SSH_KEY_SM_NAME` is set and the IAM role has
`secretsmanager:GetSecretValue` on that secret.

**SSH authentication failed on cloud target (GCP)** — verify
`GCP_SSH_KEY_SECRET_NAME` is set and the service account has
`roles/secretmanager.secretAccessor` on the secret. Ensure the public
key is in the instance's `~/.ssh/authorized_keys` (injected at launch
via `GCP_SSH_KEY_SECRET_NAME`).

### Cloud runners

**ECS task fails to start** — check CloudWatch logs for the task family
`ansible-config-mgmt`. Common causes: missing execution role, ECR pull error,
or subnet routing to the target.

**GCP: "Permission denied" creating Cloud Run Job** — add `roles/run.admin`
and `roles/iam.serviceAccountUser` to the service account.

**GCP: logs empty after successful job** — add `roles/logging.viewer`:
```bash
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:SA_EMAIL" \
  --role="roles/logging.viewer"
```

**GCP: Cloud Run job can't reach target host** — set `GCP_ANSIBLE_VPC_CONNECTOR`
to a Serverless VPC Access connector in the same region as your GCE instances.
