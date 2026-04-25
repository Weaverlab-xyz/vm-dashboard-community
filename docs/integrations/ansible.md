# Ansible Integration

## What is it?

The Ansible integration lets you run Ansible playbooks from the dashboard as
tracked background jobs. Playbooks are stored in an S3 bucket (or served from
the local container) and executed by an Ansible runner container — either an
AWS ECS task or an Azure Container Instance (ACI), depending on where your
target hosts live.

The result is a **Config Mgmt** tab in the dashboard where you can select a
playbook, pick a target host or workgroup, and launch a run — with live log
streaming via the dashboard job monitor.

---

## Use cases

- **Post-deploy configuration** — after spinning up a new VM via the dashboard,
  run a hardening or software-install playbook against it in the same workflow.
- **Recurring config checks** — schedule playbooks (via external cron or
  manual trigger) against your fleet without maintaining a separate Ansible
  control node.
- **Cross-cloud config management** — the same playbook bucket works for AWS
  (ECS runner) and Azure (ACI runner) targets.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| S3 bucket | Stores playbooks; the ECS/ACI task syncs it at run time |
| AWS ECS cluster **or** Azure Container Instances | Runs the Ansible container; must have network access to target hosts |
| SSH key in BeyondTrust Password Safe or AWS Secrets Manager | The runner needs to authenticate to target VMs |
| BeyondTrust integration (optional but recommended) | SSH key checkout for managed accounts |

---

## Setup

### Step 1 — Create the S3 playbook bucket

```bash
aws s3 mb s3://your-org-config-mgmt --region us-east-1
```

Upload your playbooks:

```bash
aws s3 cp playbooks/ s3://your-org-config-mgmt/config-mgmt/ --recursive
```

The prefix inside the bucket is controlled by `ANSIBLE_S3_PREFIX` (default:
`config-mgmt`).

### Step 2 — Configure the ECS task (AWS runner)

The ECS task definition uses the `willhallonline/ansible` Docker image by
default. The task family and cluster can match your existing BeyondTrust
Jumpoint cluster to share infrastructure:

```
ANSIBLE_ECS_CLUSTER=bt-jumpoint
ANSIBLE_ECS_TASK_FAMILY=ansible-config-mgmt
ANSIBLE_ECS_IMAGE=willhallonline/ansible:latest
ANSIBLE_ECS_CPU=256
ANSIBLE_ECS_MEMORY=512
ANSIBLE_ECS_EXECUTION_ROLE_ARN=arn:aws:iam::123456789012:role/ecsTaskExecutionRole
```

`ANSIBLE_ECS_EXECUTION_ROLE_ARN` is only required if your image is in a private
ECR registry that needs pull authentication.

### Step 3 — SSH key configuration

The Ansible runner authenticates to target hosts with an SSH key. Two sources
are supported:

**AWS Secrets Manager (preferred):**

```
ANSIBLE_SSH_KEY_SM_NAME=ec2/ssh-keypair
```

**BeyondTrust Password Safe (legacy fallback):**

```
ANSIBLE_SSH_KEY_SECRET=AWS_KEY
```

If `ANSIBLE_SSH_KEY_SM_NAME` is set, Secrets Manager is used. If it is blank,
the dashboard falls back to fetching the key title from Password Safe.

### Step 4 — Enable in the dashboard

**`.env` file:**

```
ANSIBLE_ENABLED=true
ANSIBLE_S3_BUCKET=your-org-config-mgmt
ANSIBLE_S3_REGION=us-east-1
ANSIBLE_S3_PREFIX=config-mgmt
```

**Setup wizard** — toggle **Ansible** on in Step 5 and fill in the S3 bucket
field. **Settings → Integrations → Ansible** after first login.

---

## Azure runner (ACI)

For targets in Azure, the dashboard launches an Azure Container Instance instead
of an ECS task. The ACI uses the same playbook bucket (S3) and SSH key source.

ACI-specific config:

```
AZURE_ACI_RESOURCE_GROUP=rg-config-mgmt
AZURE_ACI_SUBNET_ID=/subscriptions/.../subnets/ansible-runner
AZURE_ANSIBLE_ACI_IMAGE=willhallonline/ansible:latest
AZURE_ACI_CPU=1.0
AZURE_ACI_MEMORY=2.0
```

The ACI runner inherits your Azure credentials from `AZURE_CLIENT_ID` /
`AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID` / `AZURE_SUBSCRIPTION_ID`.

---

## What it enables in the dashboard

| Feature | Description |
|---|---|
| **Config Mgmt tab** | Lists available playbooks from the S3 bucket |
| **Run playbook** | Select playbook + target → background job with live log streaming |
| **Job history** | All Ansible runs tracked in the jobs table with exit code and duration |
| **Post-deploy hook** | VM creation workflows can chain an Ansible run immediately after boot |

---

## Playbook structure

The runner expects playbooks at the root of the S3 prefix (or in
subdirectories). A minimal inventory-less playbook targeting a single host:

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

The dashboard passes the target host IP (or hostname) as the Ansible inventory
via `-i <host>,` at runtime — no static inventory file needed for single-host
runs.

---

## Troubleshooting

**Config Mgmt tab is missing** — check `ANSIBLE_ENABLED=true` in `.env` and
restart the stack.

**"S3 bucket not found"** — verify `ANSIBLE_S3_BUCKET` and that the IAM user
has `s3:GetObject` and `s3:ListBucket` on the bucket.

**ECS task fails to start** — check CloudWatch logs for the task family
`ansible-config-mgmt`. Common causes: missing execution role, ECR pull error,
or subnet routing to the target host.

**"SSH authentication failed" in playbook run** — confirm the key in Secrets
Manager or Password Safe matches the `~/.ssh/authorized_keys` on the target VM.
Test with `ssh -i /tmp/key user@host` from inside a container on the same
network.
