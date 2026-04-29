# Ansible Integration

## What is it?

The Ansible integration lets you run Ansible playbooks and provisioning assets
(`.sh` scripts, `.rpm` / `.deb` packages) from the dashboard as tracked
background jobs. Assets are stored in cloud object storage (S3, Azure Blob, or
GCS) and executed by an Ansible runner container.

**Storage and execution targets are independent.** You can store assets in S3
and run them against on-premises Proxmox hosts, or store them in GCS and
target EC2 instances — any combination works.

Two execution paths are available:

| Path | When to use | How it runs |
|---|---|---|
| **Local Docker** | Any target — on-premises hypervisors *and* cloud VMs | Sibling container via the mounted Docker socket; assets fetched from cloud storage then Ansible SSHes/WinRMs to the target |
| **Cloud runners** | Cloud VMs when you need the runner to be network-local to the VM | AWS ECS task, Azure Container Instance, or GCP Cloud Run Job — one per cloud |

The **Config Mgmt** tab shows an asset picker and a target picker. The target
list is built automatically from:
- On-premises hypervisors that are enabled and configured (Proxmox, vSphere,
  Hyper-V, Nutanix, XCP-ng)
- Cloud VMs already deployed via the AWS, Azure, and GCP tabs

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Asset storage | **One of:** S3 bucket, Azure Blob Storage container, or GCS bucket — stores `.yml`, `.sh`, `.rpm`, `.deb` files |
| **Local runner:** Docker socket | Already mounted in `docker-compose.yml` — no extra setup |
| **Cloud runners:** ECS / ACI / Cloud Run | Only needed if you prefer the runner to be cloud-local to the VM |
| Credentials | Local runner reuses the credentials already stored for each hypervisor integration |
| **Cloud VM SSH key (AWS)** | `ANSIBLE_SSH_KEY_SM_NAME` — AWS Secrets Manager secret holding the private key PEM |
| **Cloud VM SSH key (GCP)** | `GCP_SSH_KEY_SECRET_NAME` — GCP Secret Manager secret holding the private key PEM |

---

## Step 1 — Create asset storage

Configure **one** storage backend. The dashboard auto-detects which is set; if
multiple are configured, priority is S3 > Azure Blob > GCS.

### Option A — S3

Create the bucket and upload your initial assets:

```bash
aws s3 mb s3://your-org-config-mgmt --region us-east-1
aws s3 cp assets/ s3://your-org-config-mgmt/config-mgmt/ --recursive
```

Then configure in **Settings → Integrations → Ansible**:

| Setting | Example |
|---|---|
| S3 Bucket | `your-org-config-mgmt` |
| S3 Region | `us-east-1` |
| S3 Prefix | `config-mgmt` |

### Option B — Azure Blob Storage

Create the container and upload your initial assets:

```bash
az storage container create \
  --account-name myorgplaybooks \
  --name playbooks \
  --auth-mode login

az storage blob upload-batch \
  --account-name myorgplaybooks \
  --destination "playbooks/config-mgmt" \
  --source ./assets
```

Then configure in **Settings → Integrations → Ansible**:

| Setting | Example |
|---|---|
| Storage Account | `myorgplaybooks` |
| Container | `playbooks` |
| Prefix | `config-mgmt` |

Auth uses your existing Azure service principal. The SP needs the
**Storage Blob Data Reader** role on the storage account.

### Option C — GCS

Create the bucket and upload your initial assets:

```bash
gsutil mb -l us-central1 gs://my-org-config-mgmt
gsutil -m cp -r assets/ gs://my-org-config-mgmt/config-mgmt/
```

Then configure in **Settings → Integrations → Ansible**:

| Setting | Example |
|---|---|
| GCS Bucket | `my-org-config-mgmt` |
| GCS Prefix | `config-mgmt` |

Auth uses your GCP service account. The SA needs `roles/storage.objectViewer`
on the bucket.

---

## Step 2 — Enable in the dashboard

**Settings → Integrations → Ansible** — configure your storage backend.

**Setup wizard** — toggle **Ansible** on, then configure storage from Settings
after the wizard completes.

That's all that's required for local runs against any target. Cloud SSH key
config and cloud runner config are optional — see the sections below.

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

## Provisioning assets (.sh / .rpm / .deb)

In addition to Ansible playbooks (`.yml`), you can upload **scripts and
packages** to the same storage bucket. The dashboard auto-generates a wrapper
playbook based on the file extension.

| Extension | What happens |
|---|---|
| `.yml` / `.yaml` | Playbook is used as-is |
| `.sh` | `ansible.builtin.script` — script is copied to the remote host and executed with `/bin/bash` |
| `.rpm` | `ansible.builtin.copy` + `ansible.builtin.dnf` — package is transferred and installed with `--disable-gpg-check` |
| `.deb` | `ansible.builtin.copy` + `ansible.builtin.apt` — package is transferred and installed |

Just drop the file in your storage prefix alongside your playbooks:

```bash
# S3 example — mix of asset types
aws s3 cp hardening.yml        s3://your-org-config-mgmt/config-mgmt/
aws s3 cp install-agent.sh     s3://your-org-config-mgmt/config-mgmt/
aws s3 cp my-agent.rpm         s3://your-org-config-mgmt/config-mgmt/
aws s3 cp my-agent.deb         s3://your-org-config-mgmt/config-mgmt/
```

The **Config Mgmt** tab shows all asset types in the picker. A colour badge
indicates the type (Playbook / Script / RPM / DEB).

> **Extra vars** are forwarded only to playbooks. For scripts and packages the
> field is accepted but ignored — pass runtime parameters via the script itself
> or encode them in the filename.

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

**"No asset storage configured"** — configure a storage backend in
**Settings → Integrations → Ansible** (S3, Azure Blob, or GCS).

**Assets don't appear in the picker** — confirm the files are under the
configured prefix (default: `config-mgmt/`) and have a supported extension
(`.yml`, `.yaml`, `.sh`, `.rpm`, `.deb`). Navigate away and back to force a
fresh list fetch.

**"Permission denied" on .sh asset** — the script file must be executable on
the remote host. The auto-generated playbook uses `ansible.builtin.script`
which handles the copy, but if the remote shell rejects it, add
`chmod +x /tmp/script.sh` as a preceding task in a custom `.yml` wrapper.

**Cloud VMs not in the target list** — the list is read from the in-memory
cache populated by the AWS / Azure / GCP tabs. Visit the relevant cloud tab
first so the cache is warm, then return to Config Mgmt.

**SSH authentication failed on cloud target (AWS)** — verify `ANSIBLE_SSH_KEY_SM_NAME`
is set and the IAM role has `secretsmanager:GetSecretValue` on that secret.

**SSH authentication failed on cloud target (GCP)** — verify `GCP_SSH_KEY_SECRET_NAME`
is set and the service account has `roles/secretmanager.secretAccessor` on the
secret. Ensure the public key is in the instance's `~/.ssh/authorized_keys`
(injected at launch via `GCP_SSH_KEY_SECRET_NAME`).

**"S3 bucket not found"** — verify the bucket name and that the IAM user/role has
`s3:GetObject` + `s3:ListBucket` on the bucket.

**Azure Blob: "Authorization failed"** — assign **Storage Blob Data Reader** to
the service principal on the storage account:
```bash
az role assignment create --role "Storage Blob Data Reader" \
  --assignee <client-id> \
  --scope /subscriptions/.../storageAccounts/<account>
```

**GCS: "Access denied"** — grant `roles/storage.objectViewer`:
```bash
gsutil iam ch serviceAccount:SA_EMAIL:objectViewer gs://my-org-config-mgmt
```

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
