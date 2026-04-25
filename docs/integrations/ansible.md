# Ansible Integration

## What is it?

The Ansible integration lets you run Ansible playbooks from the dashboard as
tracked background jobs. Playbooks are stored in cloud object storage (S3,
Azure Blob, or GCS) and executed by an Ansible runner container.

Two execution paths are available:

| Path | When to use | How it runs |
|---|---|---|
| **Local Docker** | On-premises hypervisors (Proxmox, vSphere, Hyper-V, Nutanix, XCP-ng) | Sibling container via the mounted Docker socket — no cloud infrastructure needed |
| **Cloud runners** | Cloud VMs (EC2, Azure VMs, GCE) | AWS ECS task, Azure Container Instance, or GCP Cloud Run Job — one per cloud |

The **Config Mgmt** tab shows a playbook picker and a target picker. The target
list is built automatically from whatever on-premises hypervisors are enabled
and configured — if Hyper-V is not configured, it does not appear.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Playbook storage | **One of:** S3 bucket, Azure Blob Storage container, or GCS bucket |
| **Local runner:** Docker socket | Already mounted in `docker-compose.yml` — no extra setup |
| **Cloud runners:** ECS / ACI / Cloud Run | Only needed for cloud VM targets |
| Credentials | Local runner reuses the credentials already stored for each hypervisor integration |

---

## Step 1 — Create playbook storage

Configure **one** storage backend. The dashboard auto-detects which is set; if
multiple are configured, priority is S3 > Azure Blob > GCS.

### Option A — S3

```bash
aws s3 mb s3://your-org-config-mgmt --region us-east-1
aws s3 cp playbooks/ s3://your-org-config-mgmt/config-mgmt/ --recursive
```

```
ANSIBLE_S3_BUCKET=your-org-config-mgmt
ANSIBLE_S3_REGION=us-east-1
ANSIBLE_S3_PREFIX=config-mgmt
```

### Option B — Azure Blob Storage

```bash
az storage container create \
  --account-name myorgplaybooks \
  --name playbooks \
  --auth-mode login

az storage blob upload-batch \
  --account-name myorgplaybooks \
  --destination "playbooks/config-mgmt" \
  --source ./playbooks
```

```
ANSIBLE_AZURE_STORAGE_ACCOUNT=myorgplaybooks
ANSIBLE_AZURE_CONTAINER=playbooks
ANSIBLE_AZURE_PREFIX=config-mgmt
```

Auth uses your existing Azure service principal. The SP needs the
**Storage Blob Data Reader** role on the storage account.

### Option C — GCS

```bash
gsutil mb -l us-central1 gs://my-org-config-mgmt
gsutil -m cp -r playbooks/ gs://my-org-config-mgmt/config-mgmt/
```

```
ANSIBLE_GCS_BUCKET=my-org-config-mgmt
ANSIBLE_GCS_PREFIX=config-mgmt
```

Auth uses your GCP service account. The SA needs `roles/storage.objectViewer`
on the bucket.

---

## Step 2 — Enable in the dashboard

**Settings → Integrations → Ansible** — configure your storage backend.

**Setup wizard** — toggle **Ansible** on, then configure storage from Settings
after the wizard completes.

That's all that's required for local on-premises runs. Cloud runner config is
optional — see the sections below.

---

## Local Docker runner (on-premises targets)

The local runner is automatic: no extra infrastructure is needed beyond the
Docker socket already mounted in `docker-compose.yml`.

### How the inventory is built

When you click **Run Playbook**, the dashboard calls `GET /api/config-mgmt/inventory`,
which returns a dynamic Ansible JSON inventory built from every on-premises
hypervisor integration that is **both enabled and has a host configured**.

Hypervisors that are not enabled or have no host set are silently omitted —
the target picker only shows what is actually reachable.

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

SSH key source for cloud runners (cloud VMs need a key, not a password):

```
ANSIBLE_SSH_KEY_SM_NAME=ec2/ssh-keypair   # AWS Secrets Manager (preferred)
ANSIBLE_SSH_KEY_SECRET=AWS_KEY            # BeyondTrust Password Safe (fallback)
```

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

For cloud targets you supply a bare IP or hostname in the target field.
The dashboard passes it as `-i <host>,` to Ansible:

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

### Playbook storage

**"No playbook storage configured"** — set at least one of `ANSIBLE_S3_BUCKET`,
`ANSIBLE_AZURE_STORAGE_ACCOUNT`, or `ANSIBLE_GCS_BUCKET`.

**"S3 bucket not found"** — verify the bucket name and that the IAM user has
`s3:GetObject` + `s3:ListBucket`.

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
