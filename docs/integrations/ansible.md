# Ansible Integration

## What is it?

The Ansible integration lets you run Ansible playbooks from the dashboard as
tracked background jobs. Playbooks are stored in an S3 bucket and executed by
an Ansible runner container — an AWS ECS task, an Azure Container Instance
(ACI), or a GCP Cloud Run Job — depending on where your target hosts live.

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
  (ECS runner), Azure (ACI runner), and GCP (Cloud Run Jobs runner) targets.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| S3 bucket | Stores playbooks; synced at run time by the runner container |
| AWS ECS cluster **and/or** Azure Container Instances **and/or** GCP Cloud Run | Runs the Ansible container; must have network access to target hosts |
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

## GCP runner (Cloud Run Jobs)

For targets in GCP, the dashboard creates a one-shot Cloud Run Job instead of
an ECS task or ACI. The job uses the same `willhallonline/ansible` image and
the same S3 playbook bucket. Logs are retrieved from Cloud Logging after the job
completes.

GCP-specific config (`.env` or **Settings → Integrations → Ansible**):

```
GCP_ANSIBLE_CLOUD_RUN_REGION=us-central1   # defaults to GCP_ZONE region if blank
GCP_ANSIBLE_IMAGE=willhallonline/ansible:latest
GCP_ANSIBLE_VPC_CONNECTOR=                 # optional — see below
```

The Cloud Run runner uses the same GCP service account credentials as the rest
of the GCP integration (`GCP_SERVICE_ACCOUNT_JSON` or Application Default
Credentials). Ensure the service account has the following roles:

| Role | Purpose |
|---|---|
| `roles/run.admin` | Create, execute, and delete Cloud Run Jobs |
| `roles/logging.viewer` | Retrieve job output from Cloud Logging |
| `roles/iam.serviceAccountUser` | Act as a service account when submitting jobs |

### Accessing private target hosts

Cloud Run Jobs run in a Google-managed VPC by default and cannot reach private
RFC-1918 addresses on your VPC. To allow the runner to SSH to private GCE
instances, either:

**Option A — VPC connector (Serverless VPC Access):**

Create a Serverless VPC Access connector in the same region as your Cloud Run
job:

```bash
gcloud compute networks vpc-access connectors create ansible-runner \
  --region us-central1 \
  --network default \
  --range 10.8.0.0/28
```

Then set:

```
GCP_ANSIBLE_VPC_CONNECTOR=projects/PROJECT_ID/locations/us-central1/connectors/ansible-runner
```

**Option B — Direct VPC Egress (Cloud Run v2 feature):**

Direct VPC Egress attaches the Cloud Run job directly to your VPC subnet without
a connector. Configure it via the `run.googleapis.com/vpc-access-egress`
annotation in the job template — this is set automatically when
`GCP_ANSIBLE_VPC_CONNECTOR` is non-empty, using private-ranges-only egress mode.

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

**GCP: "Permission denied" creating Cloud Run Job** — ensure the service account
has `roles/run.admin` and `roles/iam.serviceAccountUser` on the project.
Run `gcloud projects get-iam-policy PROJECT_ID` to inspect the current bindings.

**GCP: Cloud Run job starts but cannot reach target host** — the job is running
in a managed VPC with no access to your private network. Set
`GCP_ANSIBLE_VPC_CONNECTOR` to a Serverless VPC Access connector in the same
region as your GCE instances.

**GCP: logs are empty after a successful job** — the service account needs
`roles/logging.viewer`. Add it with:
```bash
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:SA_EMAIL" \
  --role="roles/logging.viewer"
```
