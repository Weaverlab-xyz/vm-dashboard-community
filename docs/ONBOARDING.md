# Onboarding Guide — Infrastructure Management Dashboard (Community Edition)

This guide walks you from a fresh machine to a running dashboard deploying
resources into your own AWS, Azure, and GCP accounts. Target time:
**under 30 minutes**.

Supported hosts: **Windows** (PowerShell 7), **macOS**, **Linux**, and
**WSL** (Windows Subsystem for Linux — Docker Engine in WSL, no Docker
Desktop required).

> **Two ways to configure** (once the container is up — Part D):
> **Path A — the setup wizard:** paste your own cloud credentials and click
> through `/setup` (Parts A–C show how to obtain them; or skip a cloud to
> explore). **Path B — one script, no wizard:** the [Quick path: cloud
> sandbox](#quick-path-cloud-sandbox) `onboard-sandbox` script provisions a
> throwaway lab *and* pushes the config straight into the dashboard, so you never
> open `/setup`. Neither is required if you only want to explore the UI.

- [Quick path: cloud sandbox](#quick-path-cloud-sandbox) — automated, isolated lab infra in any/all of AWS/Azure/GCP
- [Part A — AWS setup](#part-a--aws-setup)
- [Part B — Azure setup](#part-b--azure-setup)
- [Part C — GCP setup](#part-c--gcp-setup)
- [Part D — Run the dashboard](#part-d--run-the-dashboard)
- [Part E — Feature-test checklist](#part-e--feature-test-checklist)
- [Part F — Troubleshooting](#part-f--troubleshooting)
- [Appendix A — VMware Workstation integration](#appendix-a--vmware-workstation-integration)
- [Appendix B — VMware vSphere / ESXi integration](#appendix-b--vmware-vsphere--esxi-integration)
- [Appendix C — Proxmox VE integration](#appendix-c--proxmox-ve-integration)
- [Appendix D — Microsoft Hyper-V integration](#appendix-d--microsoft-hyper-v-integration)
- [Appendix E — Nutanix AHV integration](#appendix-e--nutanix-ahv-integration)
- [Appendix F — XCP-ng / XenServer integration](#appendix-f--xcp-ng--xenserver-integration)
- [Appendix G — Sign in with Microsoft (Entra OAuth)](#appendix-g--sign-in-with-microsoft-entra-oauth)
- [Appendix H — BeyondTrust integrations](#appendix-h--beyondtrust-integrations)
- [Appendix I — Entitle resource registration](#appendix-i--entitle-resource-registration)
- [Appendix J — MCP server (AI client integration)](#appendix-j--mcp-server-ai-client-integration)
- [Appendix K — Portainer CE integration](#appendix-k--portainer-ce-integration)
- [Appendix L — Remote Worker (Ansible + Kubernetes runners)](#appendix-l--remote-worker-ansible--kubernetes-runners)
- [Appendix M — Cloud Functions (preview)](#appendix-m--cloud-functions-preview)

---

## Prerequisites (one-time)

Install these once per machine:

| Tool            | Why                                | Where                                                     |
|-----------------|------------------------------------|-----------------------------------------------------------|
| Docker          | Runs the dashboard and Postgres    | **Windows/Mac:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) · **Linux/WSL:** [Docker Engine](https://docs.docker.com/engine/install/ubuntu/) |
| PowerShell 7+   | Runs the Windows onboarder (Windows only) | <https://aka.ms/powershell>                        |
| git             | Clone the repo                     | macOS: `xcode-select --install`; Windows: <https://git-scm.com/download/win>; Linux/WSL: your package manager |
| AWS CLI (v2)    | Create the IAM user and access key | <https://aws.amazon.com/cli/>                             |
| Azure CLI       | Create the Azure service principal | <https://learn.microsoft.com/cli/azure/install-azure-cli> |
| gcloud CLI      | Create the GCP service account (optional) | <https://cloud.google.com/sdk/docs/install> |

**Windows / macOS:** Start Docker Desktop and wait for the whale icon to
settle before continuing.

**Linux / WSL:** Start Docker Engine with `sudo service docker start` (or
`sudo systemctl start docker` if your distro uses systemd). Add your user
to the `docker` group once so you don't need `sudo` on every command:

```bash
sudo usermod -aG docker $USER
# then open a new terminal (or run: newgrp docker)
```

Clone the repo:

```bash
git clone <repo-url> vm-dashboard-community
cd vm-dashboard-community
```

---

## Quick path: cloud sandbox

**Optional — only if you don't already have cloud credentials.** If you're
labbing this up — testing, demoing, or running training environments — these
scripts provision a disposable, **fully isolated** sandbox in AWS, Azure, and
GCP and print a credential block you paste into the wizard (Part D), letting you
skip the manual Parts A–C. The repo ships bash scripts (WSL / Linux / macOS) and
PowerShell equivalents (Windows).

**Fastest — skip the wizard too:** `onboard-sandbox` provisions *and* pushes the
config into the dashboard for you (creating your admin), so you never open `/setup`:

```bash
./scripts/sandbox/Linux/onboard-sandbox.sh --cloud all
# Windows:  .\scripts\sandbox\Windows\Onboard-Sandbox.ps1 -Cloud all
```

Or run the per-cloud scripts and paste their printed config into the wizard:

```bash
# Bash (WSL / Linux / macOS)
./scripts/sandbox/Linux/00-prereqs.sh        # one-time prereq check
./scripts/sandbox/Linux/setup-aws.sh         # provision AWS sandbox
./scripts/sandbox/Linux/setup-azure.sh       # provision Azure sandbox
./scripts/sandbox/Linux/setup-gcp.sh         # provision GCP sandbox
./scripts/sandbox/Linux/rollback.sh --cloud all -y   # tear it all down
```

```powershell
# PowerShell (Windows)
.\scripts\sandbox\Windows\Test-SandboxPrereqs.ps1
.\scripts\sandbox\Windows\Setup-AwsSandbox.ps1
.\scripts\sandbox\Windows\Setup-AzureSandbox.ps1
.\scripts\sandbox\Windows\Setup-GcpSandbox.ps1
.\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud all -Yes
```

Both variants are functionally equivalent — same resources, same tags,
same idempotency, same printed config block. Pick whichever fits your
shell.

What you get per cloud:

- A dedicated VPC/VNet with two subnets — one for the BeyondTrust SRA
  Gateway container (internet egress so it can phone home to PRA), one
  for your lab VMs (**no** internet egress).
- A managed-by-dashboard service principal / IAM role / service account
  with the minimum permissions needed.
- An SSH key pair stored as `{public_key, private_key}` JSON in the
  cloud's secret manager.
- Tagged resources so the rollback script can clean up reliably.

Each setup script ends with a config block to paste into the dashboard's
`/setup` wizard or **Settings → Integrations** panels — those values
replace the manual setup in Parts A/B/C below.

**When to use the sandbox path:**
- ✅ Repeatable, isolated lab environments for testing or demos.
- ✅ One-command tear-down at the end of a session.
- ✅ Network isolation between deployed VMs and the public internet.
- ❌ Production or shared dashboards with existing cloud infra — the
  scripts create new VPCs/VNets and assume they own them.

**See [docs/CLOUD_SANDBOX.md](CLOUD_SANDBOX.md)** for the full walkthrough:
topology diagrams per cloud, cost estimates, verification, customisation
hooks, and troubleshooting. The [`scripts/sandbox/README.md`](../scripts/sandbox/README.md)
also has a one-line summary per file if you want a quick orientation
before reading the long doc.

After running the sandbox scripts, **skip ahead to
[Part D — Run the dashboard](#part-d--run-the-dashboard)** — Parts A, B,
and C are the manual alternative.

---

## Part A — AWS setup

The dashboard deploys EC2 instances into **your** AWS account using an IAM
user dedicated to the dashboard.

### 1. Create the IAM user and attach a policy

The dashboard needs a **customer-managed** policy. The AWS-managed policies an
earlier version of this guide recommended do not work — see
[Why not the AWS-managed policies](#why-not-the-aws-managed-policies) below.

**Recommended — lift the canonical policy.** `dashboard-app-policy` in
[`scripts/sandbox/Linux/setup-aws.sh`](../scripts/sandbox/Linux/setup-aws.sh)
(the `DASHBOARD_POLICY_DOC` heredoc) covers every AWS call the dashboard makes,
across every feature. That script is the source of truth — restating every
statement here would only go stale.

```powershell
aws iam create-user --user-name dashboard-dev

# Copy the DASHBOARD_POLICY_DOC JSON out of setup-aws.sh into
# dashboard-app-policy.json, substituting your account id and your storage
# bucket prefix for the ${...} placeholders, then:
aws iam create-policy `
  --policy-name dashboard-app-policy `
  --policy-document file://dashboard-app-policy.json

aws iam attach-user-policy --user-name dashboard-dev `
  --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/dashboard-app-policy
```

> **It has to be a managed policy, not an inline one.** Inline user policies are
> capped at 2048 bytes and this document is ~5.2 KB (the managed-policy quota is
> 6144). Managed policies are also versioned, so
> `aws iam create-policy-version --set-as-default` propagates later edits
> without rotating the access key.

**Minimal — core VM deploys only.** If you only want the Cloud VMs page, these
are the permissions that path actually needs:

| Purpose | Actions |
|---------|---------|
| Instance lifecycle | `ec2:Describe*`, `ec2:RunInstances`, `ec2:StartInstances`, `ec2:StopInstances`, `ec2:TerminateInstances`, `ec2:RebootInstances`, `ec2:ModifyInstanceAttribute`, `ec2:CreateTags`, `ec2:DeleteTags`, `ec2:GetPasswordData` |
| Security groups and key pairs | `ec2:CreateSecurityGroup`, `ec2:DeleteSecurityGroup`, `ec2:AuthorizeSecurityGroupIngress`/`Egress`, `ec2:RevokeSecurityGroupIngress`/`Egress`, `ec2:CreateKeyPair`, `ec2:DeleteKeyPair`, `ec2:ImportKeyPair` |
| **Terraform remote state** | `s3:ListBucket`, `s3:GetBucketLocation`, `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:AbortMultipartUpload`, `s3:ListBucketMultipartUploads`, `s3:ListMultipartUploadParts` — on the bucket **and** on `<bucket>/*` |
| Instance profiles | `iam:PassRole`, scoped to the roles you let the dashboard pass |
| Credential test | `sts:GetCallerIdentity` |

Everything past that is per-feature. Each row below names the `Sid` in
`setup-aws.sh` so you can lift just that statement:

| Feature | Extra permissions | `Sid` in `setup-aws.sh` |
|---------|-------------------|-------------------------|
| Cross-cloud image promote (VHD export / import) | `ec2:ImportImage`, `ec2:ExportImage`, `ec2:DescribeImportImageTasks`, `ec2:DescribeExportImageTasks`, `ec2:CancelImportTask`, `ec2:CancelExportTask` — plus a `vmimport` role with its own trust policy | `DashboardVMImportExport` |
| Gateways and remote runners (ECS) | ECS cluster / task-definition / task actions, `ssm:GetParameter` and `ssm:GetParameters` on the ECS-optimized-AMI parameter path, `iam:CreateServiceLinkedRole` for `ecs.amazonaws.com`, and `iam:PassRole` on `ecsTaskExecutionRole` / `ecsInstanceRole` | `DashboardECS`, `DashboardECSOptimizedAMI`, `DashboardServiceLinkedRole`, `DashboardPassRoles` |
| Kubernetes clusters (EKS) | `eks:*`, role CRUD scoped to `role/k8s-*` and `role/*-role`, `iam:CreateServiceLinkedRole` for the `eks*` service principals, `iam:GetRole` | `DashboardEKS`, `DashboardRoles`, `DashboardEKSServiceLinkedRole`, `DashboardEKSGetRole` |
| Cloud databases (RDS) | DB instance and DB subnet group CRUD, `rds:DescribeDBEngineVersions`, `rds:DescribeOrderableDBInstanceOptions`, tag actions | `DashboardRDS` |
| Cloud Functions (Lambda) | `lambda:*`, plus Secrets Manager on `secret:*-fn-secret-*` | `DashboardLambda`, `DashboardSecretsManager` |
| Cloud Costs | `ce:GetCostAndUsage`, `ce:GetCostForecast`, `ce:GetDimensionValues`, `ce:GetTags` | `DashboardCostExplorer` |
| External secrets backend | Secrets Manager CRUD on `secret:dashboard/*` — see [secrets-management.md](secrets-management.md#iam-permissions-required-per-backend) | `DashboardSecretsManager` |
| Password Safe VM onboarding | `ssm:SendCommand`, `ssm:GetCommandInvocation`, `ssm:ListCommandInvocations` | `DashboardSSMRunCommand` |
| Job log streaming | `logs:*` | `DashboardLogs` |

#### Why not the AWS-managed policies

Earlier revisions of this guide told you to attach `AmazonEC2FullAccess`,
`AmazonS3ReadOnlyAccess` and `IAMReadOnlyAccess`. That combination is both
broader than necessary and too narrow to work:

- **`AmazonEC2FullAccess`** grants more EC2 than the dashboard uses, and nothing
  at all for ECS, EKS, Lambda, RDS, Secrets Manager or Cost Explorer — every one
  of which this same guide sets up later (Appendix L, Appendix M, and the
  external-vault step in Part D).
- **`AmazonS3ReadOnlyAccess` breaks deploys outright.** Terraform state lives in
  your active storage backend, so an apply must *write* `s3:PutObject` /
  `s3:DeleteObject` for both the `.tfstate` object and its `.tflock` companion
  (the dashboard uses S3-native state locking). Read-only fails every apply and
  destroy. Worse, with no storage backend configured at all, state falls back to
  the container's local disk, where losing that directory **orphans live cloud
  resources** — see [infrastructure-as-code.md](infrastructure-as-code.md#state-the-thing-that-makes-iac-work).
- **`IAMReadOnlyAccess` cannot `iam:PassRole`**, which is the action attaching an
  instance profile actually requires. The old "looking up instance profiles"
  rationale named the wrong mechanism.

### 2. Create the access key

```powershell
aws iam create-access-key --user-name dashboard-dev
```

Copy the `AccessKeyId` and `SecretAccessKey` from the output — you will
paste them into `.env` in Part D.

### 3. Pick a default region

The dashboard uses `AWS_REGION` as the default for all deploys. Common
picks: `us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1`.

---

## Part B — Azure setup

The dashboard deploys Azure VMs into **your** Azure subscription using a
service principal (SP) scoped to a single resource group.

### 1. Log in and pick a subscription

```powershell
az login
az account show --query id -o tsv
```

Copy the subscription id.

### 2. Create the resource group and pick a region

```powershell
az group create --name dashboard-rg --location eastus
```

The `AZURE_RESOURCE_GROUP` value in `.env` becomes the default RG for deployed
VMs, and `AZURE_LOCATION` its region (e.g. `centralus`, `eastus`,
`westeurope`). Create it now rather than letting the first deploy create it —
the SP in the next step is scoped to this resource group, so it has to exist
first.

### 3. Create the service principal

```powershell
az ad sp create-for-rbac `
  --name "dashboard-dev" `
  --role Contributor `
  --scopes /subscriptions/<your-subscription-id>/resourceGroups/dashboard-rg
```

The output includes `appId`, `password`, and `tenant` — you need all three
(plus the subscription id) for `.env`.

> **Scope it to the resource group, not the subscription.** The sandbox scripts
> scope `Contributor` to a single RG, and so should you; a subscription-wide
> grant hands the dashboard every resource you own. Widen it only if you
> deliberately want deploys landing outside `dashboard-rg`.

> **Security note:** the client secret (`password`) rotates. Azure will
> warn you when it nears expiry; create a new one and update `.env`.

### 4. Grant the additional roles

`Contributor` on the RG is **not sufficient on its own.** It is a control-plane
role, so it cannot write blob data — and Terraform state is blob data. Add the
grants for the features you intend to use:

| Feature | Grant | Scope |
|---------|-------|-------|
| **Terraform state + storage backend** | `Storage Blob Data Contributor` | the storage account |
| Secrets backend (Key Vault) | secret permissions `get list set delete` | the vault |
| Kubernetes clusters (AKS) | `User Access Administrator` | the resource group |
| Cloud Costs | `Cost Management Reader` | the **subscription** |
| Container registry pulls | `AcrPull` | the registry |
| Image promote into a Compute Gallery | a custom role with the gallery/image actions, or `Contributor` | the gallery's RG |

```powershell
# Terraform state and the azure_blob storage backend — data plane, required
az role assignment create `
  --assignee <appId> `
  --role "Storage Blob Data Contributor" `
  --scope /subscriptions/<sub-id>/resourceGroups/dashboard-rg/providers/Microsoft.Storage/storageAccounts/<account>

# Key Vault secrets backend — write is required so per-VM admin passwords can
# be vaulted and removed again on teardown
az keyvault set-policy --name <vault> `
  --object-id <sp-object-id> `
  --secret-permissions get list set delete

# AKS only: the cluster module creates its own role assignment, which
# Contributor cannot do (it lacks Microsoft.Authorization/roleAssignments/write)
az role assignment create `
  --assignee <appId> `
  --role "User Access Administrator" `
  --scope /subscriptions/<sub-id>/resourceGroups/dashboard-rg

# Cloud Costs only: cost data is queried at subscription scope
az role assignment create `
  --assignee <appId> `
  --role "Cost Management Reader" `
  --scope /subscriptions/<sub-id>
```

> **Key Vault: access policy or RBAC.** The command above uses a vault access
> policy, which is what the sandbox scripts do. If your vault uses the RBAC
> permission model instead, the equivalent is the **Key Vault Secrets Officer**
> role — see [secrets-management.md](secrets-management.md#iam-permissions-required-per-backend).

For the authoritative list, see the role assignments in
[`scripts/sandbox/Linux/setup-azure.sh`](../scripts/sandbox/Linux/setup-azure.sh);
that script is the source of truth.

---

## Part C — GCP setup

The dashboard deploys Compute Engine instances into **your** GCP project using
a service account. GCP is optional — AWS and Azure work without it.

### 1. Prerequisites

Install the Google Cloud CLI (gcloud) if you haven't already:
<https://cloud.google.com/sdk/docs/install>

```bash
gcloud auth login
gcloud config set project <YOUR_PROJECT_ID>
```

### 2. Enable required APIs

For core VM deploys:

```bash
gcloud services enable compute.googleapis.com secretmanager.googleapis.com iam.googleapis.com
```

Optional features each need their own API. Rather than list a set that goes
stale, enable the same ones the sandbox script does — see the two
`gcloud services enable` calls in
[`scripts/sandbox/Linux/setup-gcp.sh`](../scripts/sandbox/Linux/setup-gcp.sh),
which cover Cloud Run, Cloud Build, GKE (plus Fleet/Connect Gateway),
BigQuery, Cloud Functions, Artifact Registry, Service Networking and Cloud SQL
Admin.

### 3. Create a service account and download a key

```bash
# Create the service account
gcloud iam service-accounts create dashboard-sa \
  --display-name "VM Dashboard SA"

# Core roles: instances, impersonation for attached SAs, and secrets
for ROLE in roles/compute.admin \
            roles/iam.serviceAccountUser \
            roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding <PROJECT_ID> \
    --member "serviceAccount:dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
    --role "$ROLE"
done

# Terraform state lives in a bucket, so the SA needs to WRITE objects there.
# Deliberately bucket-scoped, not project-wide.
gcloud storage buckets add-iam-policy-binding gs://<YOUR_STATE_BUCKET> \
  --member "serviceAccount:dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role "roles/storage.objectAdmin"

# Download the JSON key
gcloud iam service-accounts keys create sa-key.json \
  --iam-account "dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com"
```

Keep `sa-key.json` safe. You'll paste its entire contents into the wizard.

> **`roles/compute.admin` alone is not enough.** It grants nothing on Cloud
> Storage, and Terraform keeps its state in your active storage backend — so
> without the `storage.objectAdmin` binding above, every apply and destroy
> fails. And with no storage backend configured at all, state falls back to the
> container's local disk, where losing that directory **orphans live cloud
> resources**. See
> [infrastructure-as-code.md](infrastructure-as-code.md#state-the-thing-that-makes-iac-work).

Optional features need more roles on top:

| Feature | Extra roles |
|---------|-------------|
| Kubernetes clusters (GKE) | `roles/container.admin`, `roles/gkehub.admin`, `roles/resourcemanager.projectIamAdmin`, `roles/iam.roleAdmin`, `roles/serviceusage.serviceUsageAdmin` — see [kubernetes.md](kubernetes.md) for why `container.admin` alone is insufficient |
| Cloud databases (Cloud SQL) | `roles/cloudsql.admin`, `roles/servicenetworking.networksAdmin` |
| Cloud Functions / Cloud Run | `roles/run.admin`, `roles/run.developer`, `roles/run.invoker`, `roles/cloudfunctions.developer`, `roles/cloudbuild.builds.builder`, `roles/artifactregistry.writer`, `roles/secretmanager.admin` |
| Image export (VHD) | `roles/cloudbuild.builds.editor`, plus the roles the Cloud Build service identities need — see [image-management.md](image-management.md) |
| Cloud Costs | `roles/bigquery.jobUser`, `roles/bigquery.dataViewer` (see below) |
| External secrets backend (writing secrets) | `roles/secretmanager.secretVersionAdder`, or `roles/secretmanager.admin` on the project &mdash; see [secrets-management.md](secrets-management.md#iam-permissions-required-per-backend) |
| Job log viewing | `roles/logging.viewer` |

The full set the sandbox grants is the `for role in ...` loop in
[`setup-gcp.sh`](../scripts/sandbox/Linux/setup-gcp.sh), which carries a
why-comment per role. That script is the source of truth — listing every role
here would only go stale.

> **Cloud Costs on GCP is a BigQuery query, not a cost API.** You must first
> create a **Cloud Billing export to BigQuery** in the Billing console — no
> setup script can create it for you. Then set the export table on the Cloud
> Costs settings page (`<project>.<dataset>.gcp_billing_export_v1_XXXX`) and
> grant the service account `roles/bigquery.jobUser` +
> `roles/bigquery.dataViewer`. **If the export dataset lives in a different
> project**, grant `dataViewer` on that dataset in that project too — a
> project-level binding here will not reach it.

### 4. (Optional) Store an SSH key pair in Secret Manager

If you want the dashboard to inject SSH keys automatically:

```bash
# Create a JSON secret with your public key
echo '{"public_key":"ssh-rsa AAAA... user@host"}' | \
  gcloud secrets create my-ssh-keypair \
    --data-file=- \
    --replication-policy=automatic

# Grant the service account access (if not already inherited from secretAccessor above)
gcloud secrets add-iam-policy-binding my-ssh-keypair \
  --member "serviceAccount:dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role "roles/secretmanager.secretAccessor"
```

Note the secret name (`my-ssh-keypair`) — you'll enter it in the wizard.

### 5. Enter credentials in the wizard

When you run the onboard script, the wizard Step 4 (GCP) asks for:

| Field | Where to get it |
|-------|-----------------|
| Project ID | `gcloud config get project` |
| Region | Your preferred GCP region (e.g. `us-central1`) |
| Zone | A zone in that region (e.g. `us-central1-a`) |
| Service Account JSON | Full contents of `sa-key.json` |
| SSH Key Secret Name | Name of the Secret Manager secret from step 4 |

---

## Part D — Run the dashboard

Pick the onboarder that matches your host OS. Both do the same thing:
preflight checks, generate the JWT key file and bootstrap `.env` (DB
secret only), bring up Compose, poll `/api/health`, open the browser.

> **Wizard or script?** Step 1 starts the container — **both** paths need it.
> Step 2 is **Path A** (walk the wizard). For **Path B** (no wizard), do Step 1,
> then run the [Quick path: cloud sandbox](#quick-path-cloud-sandbox)
> `onboard-sandbox` script — it provisions a lab and configures the dashboard for
> you, and you log in directly.

### 1. Run the onboard script (one command)

By default the onboarder **builds the image from source**. Add `--hub` (bash)
/ `-Hub` (PowerShell) to **pull the prebuilt multi-arch image** from Docker Hub
(`chrweav/infra-dashboard`) instead — no local build, and it runs on both
AMD64 and ARM64 (Apple Silicon, AWS Graviton, Raspberry Pi 5).

**Windows** (PowerShell 7):

```powershell
.\scripts\Onboard-Dashboard.ps1 -Hub      # pull prebuilt image (drop -Hub to build from source)
```

**macOS / Linux / WSL / Raspberry Pi** (bash):

```bash
./scripts/onboard.sh --hub                # pull prebuilt image (drop --hub to build from source)
```

The script:

- Verifies Docker is running and `docker compose` is available.
- Copies `.env.example → .env` if missing (only bootstrap secrets needed — no cloud credentials in this file).
- Generates `.jwt_secret_key` (owner-read-only on disk — this is the root of trust
  for all encrypted credentials stored in the database). The file is excluded from git
  and from the Docker build context; it is mounted into the container at runtime via
  Docker Secrets and is never written to `.env`.
- Auto-generates `POSTGRES_PASSWORD` in `.env` if it's still at the placeholder value.
- Brings up the Compose stack (`db` + `app` + `worker`, the last at
  `WORKER_REPLICAS` replicas, default 3) — with `--hub`, from `docker-compose.hub.yml`
  using the published image (a `docker compose pull` runs first so reruns pick up new
  releases); otherwise it builds the `app` image locally.
- Waits for `http://localhost:8001/api/health` to respond.
- Opens your browser.

> **Behind a TLS-inspecting corporate proxy?** The `--hub` path skips the image
> build entirely, so the corp-CA build steps and the `ONBOARD_SKIP_DEBIAN_UPDATES`
> escape hatch are **not needed**. You do still need Docker to trust your corporate
> root CA for the `docker pull` itself — see
> [WSL: `docker pull` fails with a certificate error](#wsl-docker-pull-fails-with-a-certificate-error)
> below. And if you use features that make outbound TLS calls *from inside the
> container* (Terraform-backed provisioning, e.g. cloud databases), add
> `--corp-ca` so the published image trusts your proxy's CA at runtime —
> step 5 of the same section.

### 2. Complete the setup wizard

Your browser opens to the **setup wizard**. It appears automatically on
first visit because no credentials are stored yet.

| Step | What to fill in |
|------|-----------------|
| **Admin account** | Username and password you'll use to log in |
| **AWS** | Access Key ID, Secret Access Key, and default region from Part A |
| **Azure** | Service principal credentials from Part B. Optionally expand **Sign in with Microsoft** to add Entra OAuth (see Appendix B) |
| **GCP** | Project ID, region/zone, and service account JSON key from Part C. Expand **Advanced** to set the SSH key secret name |
| **OCI** | Tenancy/user OCID, key fingerprint, private key and region. Every field may be left blank to skip OCI |
| **Feature flags** | Enable optional integrations — all default off (see Appendices A–F for on-prem hypervisors; Appendix J for MCP). Toggles only; the per-integration fields live in **Settings → Integrations** |

Steps are listed by name rather than number on purpose: the wizard's step list is data, and
citing an ordinal is how this table came to omit OCI and call Feature flags "step 5".

Click **Complete setup**. Credentials are encrypted with AES-256 and
stored in the application database — not in any file on disk.

### 3. Log in

The wizard redirects to `/login`. Sign in with the username and password
you created in wizard Step 1.

### 4. Stopping and restarting

```bash
docker compose down              # stop the stack (add -f docker-compose.hub.yml if you used --hub)
./scripts/onboard.sh             # bring it back up (Windows: .\scripts\Onboard-Dashboard.ps1)
```

If you started the stack with `--hub` / `-Hub`, keep that flag when you bring it
back up (`./scripts/onboard.sh --hub`) so it targets the published image.

Postgres data persists in the `pgdata` Docker volume across restarts. The
wizard won't appear again — your credentials are already in the database.

### Scaling the job worker

Long jobs — Kubernetes and cloud-database provisions, Packer image builds, and
image export/promote — run in the dedicated `worker` container, not the web app.
**Each worker runs one such job at a time**, so if quick jobs pile up behind a
long one (e.g. an export waiting on a 25-minute cluster provision), run more
workers. The job queue claims each job atomically, so extra workers never
double-run a job.

The stack starts **3 workers** by default (the onboard scripts migrate installs
still pinned at the old default of 1). Change the count in `.env`, then bring the
stack up as usual:

```bash
WORKER_REPLICAS=5          # in .env — number of long jobs that can run at once
./scripts/onboard.sh       # (keep --hub if you used it)
```

Or scale ad-hoc without editing `.env`:

```bash
docker compose up -d --scale worker=5          # add -f docker-compose.hub.yml if you used --hub
```

`WORKER_CPU_LIMIT` / `WORKER_MEM_LIMIT` (also in `.env`) cap each worker's CPU and
memory so several concurrent heavy jobs can't exhaust the host — tune them to your
machine. Defaults: 3 workers, 2 CPUs and 2 GB each (3 busy workers can use up to
~3× those limits, so lower `WORKER_REPLICAS` on small hosts).

### Reconfiguring credentials after first run

To update credentials or toggle feature flags after setup, navigate to
`/setup` in your browser while logged in as admin. The wizard reopens in
reconfigure mode: existing values are pre-filled, and leaving a secret
field blank keeps the stored value unchanged.

### Protect and back up the JWT key

`.jwt_secret_key` is the root of trust for the entire application — every
integration credential stored in the database is encrypted with a key derived
from it. The onboard script protects it with owner-only filesystem permissions
and mounts it into the container as a Docker secret.

**In the community edition this key cannot be migrated to a cloud vault.** It's
the bootstrap key that decrypts the encrypted database — *including* the
credentials the dashboard would need to reach any vault — so there's no startup
ordering that lets it live in a vault (see
[Why the JWT root key cannot be migrated](secrets-management.md#why-the-jwt-root-key-cannot-be-migrated)).
At startup the dashboard reads it from `JWT_SECRET_KEY_FILE` → the
`/run/secrets/jwt_key` Docker secret → the `JWT_SECRET_KEY` env var, in that order.

So, for the community edition:

- **Back it up** somewhere safe (password manager, encrypted drive). Lose it and
  every stored credential is unrecoverable and the app won't start — see
  [JWT key file: backup and loss recovery](#jwt-key-file-backup-and-loss-recovery) below.
- **Don't commit it** — it's gitignored and excluded from the image build context.
- On shared or long-lived hosts, restrict OS access to the file; the host's
  filesystem permissions (or the Docker secret mount) are the security boundary.

> *Integration credentials* (your AWS/Azure/GCP keys) **can** be moved into an
> external vault — AWS Secrets Manager, Azure Key Vault, GCP Secret Manager, or
> BeyondTrust Secrets Safe — from **Settings → Secrets Backend** (`/secrets`).
> That's a separate feature from the root key; see
> [`docs/secrets-management.md`](secrets-management.md). Removing the on-disk root
> key entirely (fetched at boot via cloud workload identity) is on the
> **SaaS-edition roadmap** — see [`docs/saas-comparison.md`](saas-comparison.md).

### Platform notes

- **WSL (Windows Subsystem for Linux):** Docker Desktop is not required.
  Install Docker Engine inside your WSL distro, start it with
  `sudo service docker start`, then run `./scripts/onboard.sh`. The
  script detects WSL automatically: it prints WSL-specific hints if the
  daemon isn't running, and opens the dashboard in your Windows-side
  browser (via `wslview` if installed, otherwise `cmd.exe /c start`).
  Ports from WSL2 are automatically forwarded to Windows, so
  `http://localhost:8001` works in your Windows browser without any extra
  configuration.
- **Apple Silicon (M1/M2/M3/M4):** Docker images build natively as
  `linux/arm64` — no platform flag needed. The same applies to
  Raspberry Pi 5 (ARM64).
- The **VMware Workstation** feature flag (Appendix A) is Windows host-only;
  do not enable it on macOS, Linux, or WSL. The **VMware vSphere / ESXi**
  flag (Appendix B) connects to a remote vCenter/ESXi host and works on any
  OS.
- The optional **MCP server** (Appendix C) needs no extra containers —
  it runs inside the main app and is always available once the stack is up.
- **Portainer**, **Remote Worker** (Ansible + Kubernetes runners),
  **Proxmox VE**, **VMware vSphere / ESXi**,
  **Microsoft Hyper-V**, **Nutanix AHV**, **XCP-ng / XenServer**, and
  **Entitle** are optional integrations with their own backing infrastructure.
  See the detailed guides in [`docs/integrations/`](integrations/).
- **Secrets management** — how credentials are encrypted, how to migrate to an
  external vault, and security best practices: [`docs/secrets-management.md`](secrets-management.md).
- **Storage management** — where playbooks and asset files live, and how to
  migrate between cloud object stores or a corporate file share:
  [`docs/storage-management.md`](storage-management.md).
- **Config management** — philosophy, best practices, and how the
  dashboard's ephemeral-runner approach reduces secret sprawl:
  [`docs/config-management.md`](config-management.md).
- **Infrastructure as code** — how cloud VMs, Shell Jumps, and images
  are provisioned through Terraform/Packer modules with per-job state:
  [`docs/infrastructure-as-code.md`](infrastructure-as-code.md).
- **Image management** — the build-once-promote-many lifecycle: build
  a portable image artefact, store it in your storage backend, then
  promote it to AWS / Azure / GCP:
  [`docs/image-management.md`](image-management.md).
- **Hosting it in a cloud instead of on this host** — Azure Container Apps,
  Cloud Run or ECS, with a gateway sidecar that keeps the agent endpoint
  public and the UI private. Note that a cloud-hosted dashboard has no route
  to on-premises hypervisors and cannot use the local Ansible runner:
  [`docs/cloud-hosting.md`](cloud-hosting.md).
- **Moving configuration to a second instance** — every Settings value, without
  re-typing it: [`docs/config-migration.md`](config-migration.md).

---

## Part E — Feature-test checklist

Run through this checklist after first login to confirm the stack is
healthy end-to-end.

- [ ] **Login.** Log in as `admin`. The dashboard page loads without
      browser console errors.
- [ ] **Change password.** Settings → Security → Change Password. Log
      out, log back in with the new password.
- [ ] **AWS: list AMIs.** AWS tab → the community-AMI gallery populates
      (Ubuntu, Amazon Linux, etc.). No 5xx errors.
- [ ] **AWS: deploy an instance.** Pick the smallest AMI, `t3.micro`,
      default VPC. Submit. Watch the Jobs page. Within ~90 seconds the
      instance appears in your AWS Console under the selected region.
- [ ] **AWS: terminate.** Back on the AWS tab, terminate the instance
      you just deployed. Confirm it disappears from both the dashboard
      and the AWS Console.
- [ ] **Azure: list images.** Azure tab → Marketplace tab shows the
      hardcoded Ubuntu/RHEL/Debian images. Private Images tab lists any
      managed images or Shared Image Gallery entries in your
      subscription (empty is fine).
- [ ] **Azure: deploy a VM.** Pick a Marketplace image,
      `Standard_B1s`, default networking. Submit. Within ~3 minutes the
      VM appears in your Azure Portal → Virtual Machines.
- [ ] **Azure: stop/delete.** Stop and delete the VM from the Azure tab.
      Confirm it disappears from both the dashboard and the portal.
- [ ] **Jobs.** The Jobs page lists all actions you just took with
      timestamps, durations, and status.

If any step fails, skip to [Part F](#part-f--troubleshooting).

---

## Part F — Troubleshooting

### Onboarding script exits at preflight

- **"PowerShell 7+ is required"** — you're running Windows PowerShell 5.
  Install PS7 (<https://aka.ms/powershell>) and rerun with `pwsh`.
- **"docker not found"** — Docker isn't installed or isn't on `PATH`.
  Windows/Mac: reinstall Docker Desktop. Linux/WSL: install Docker Engine
  (`sudo apt install docker.io`) and restart your terminal.
- **"Docker daemon is not responding"** — Windows/Mac: Docker Desktop is
  installed but not running — launch it and wait for the whale icon to
  settle. Linux/WSL: run `sudo service docker start` (or
  `sudo systemctl start docker`) then rerun the script.

### WSL: `docker pull` fails with a certificate error

**Symptom:** `docker pull postgres:16-alpine` (or any image) fails with:
```
x509: certificate signed by unknown authority
```

**Cause:** Your network uses an SSL-inspection proxy (Zscaler, Palo Alto, etc.)
that re-signs outbound TLS traffic with a corporate root CA. WSL does not
inherit Windows' trusted root store, so Docker inside WSL rejects the
intercepted certificate.

**Fix — run once per WSL distro install:**

**Step 1 — identify and export the proxy root CA (PowerShell on Windows):**

```powershell
# List trusted roots — look for your security vendor (Zscaler, etc.)
Get-ChildItem Cert:\LocalMachine\Root | Select-Object Subject, Thumbprint | Sort-Object Subject

# Export the relevant cert (replace <Thumbprint> with the value above)
$cert = Get-ChildItem Cert:\LocalMachine\Root\<Thumbprint>
Export-Certificate -Cert $cert -FilePath "$env:TEMP\corp-root.cer" -Type CERT
```

If you are unsure which cert to export, export them all and let WSL sort it out:

```powershell
New-Item -ItemType Directory -Force "$env:TEMP\roots" | Out-Null
Get-ChildItem Cert:\LocalMachine\Root | ForEach-Object {
    Export-Certificate -Cert $_ `
        -FilePath "$env:TEMP\roots\$($_.Thumbprint).cer" -Type CERT
}
```

**Step 2 — import into WSL and update the system trust store:**

```bash
# Single cert
openssl x509 -inform DER \
    -in /mnt/c/Users/$(cmd.exe /c echo %USERNAME% 2>/dev/null | tr -d '\r')/AppData/Local/Temp/corp-root.cer \
    -out /tmp/corp-root.pem
sudo cp /tmp/corp-root.pem /usr/local/share/ca-certificates/corp-root.crt
sudo update-ca-certificates
```

If you exported all certs, convert and import them in a loop:

```bash
WINTEMP="/mnt/c/Users/$(cmd.exe /c echo %USERNAME% 2>/dev/null | tr -d '\r')/AppData/Local/Temp/roots"
sudo mkdir -p /usr/local/share/ca-certificates/windows-roots
for f in "$WINTEMP"/*.cer; do
    name=$(basename "$f" .cer)
    openssl x509 -inform DER -in "$f" \
        -out "/usr/local/share/ca-certificates/windows-roots/$name.crt" 2>/dev/null || true
done
sudo update-ca-certificates
```

**Step 3 — add the cert to Docker's registry trust store:**

```bash
sudo mkdir -p /etc/docker/certs.d/registry-1.docker.io
sudo cp /tmp/corp-root.pem /etc/docker/certs.d/registry-1.docker.io/ca.crt
sudo service docker restart
```

**Verify host-side pulls work:**

```bash
docker pull hello-world
```

**Step 4 — make the cert available inside the image build:**

Steps 1–3 fix the host's connection to Docker Hub, but `apt-get update` and
`pip install` *inside* the image build go through the same TLS-inspecting
proxy and need the cert too. Drop a copy into `corp-ca/` at the repo root:

```bash
cp /tmp/corp-root.pem ./corp-ca/corp-root.crt
```

The Dockerfile copies any `.crt` / `.pem` file in `corp-ca/` into the image's
system trust store and points `pip`, `requests`, and `curl` at it. Files in
that directory are gitignored, so your cert stays local.

Now rerun `./scripts/onboard.sh --build`.

**Step 5 — (`--hub` only) trust the cert *inside the published container*:**

The published image is built by CI on a clean network, so it carries no corp
CA — and unlike the build path, there's no image build to inject it into.
That's fine until a feature makes outbound TLS calls *from inside the
container*: Terraform-backed provisioning (cloud databases) runs
`terraform init`, which fetches providers from `registry.terraform.io` and
fails with:

```
terraform init failed: ... x509: certificate signed by unknown authority
```

Fix: start the hub stack with the corp-CA overlay, which bind-mounts the
host's system CA bundle (updated in step 2 above) read-only over the
container's, and sets `AWS_CA_BUNDLE` for boto3:

```bash
./scripts/onboard.sh --hub --corp-ca
# or manually:
docker compose -f docker-compose.hub.yml -f docker-compose.corp-ca.yml up -d
```

On a non-Linux host (no `/etc/ssl/certs/ca-certificates.crt`), point
`CORP_CA_BUNDLE` at a PEM file containing the public roots plus your corp CA.

### Stack starts but `/api/health` doesn't respond

```powershell
docker compose logs --tail 100 app
```

Common causes:

| Symptom in logs                                   | Likely cause                                      | Fix                                                                                          |
|---------------------------------------------------|---------------------------------------------------|----------------------------------------------------------------------------------------------|
| `InvalidClientTokenId` / `InvalidSignature`       | AWS access key wrong or rotated                   | Rerun `aws iam create-access-key`, then update via the reconfigure wizard (`/setup`)         |
| `AuthenticationFailed` from Azure                 | Azure SP secret wrong or expired                  | Regenerate with `az ad sp credential reset`, then update via the reconfigure wizard (`/setup`) |
| `connection refused` on port 5432                 | Postgres container not healthy                    | `docker compose ps`; check `db` container logs                                               |
| `Address already in use` on 8001                  | Another process is bound to 8001                  | Stop it, or change the port mapping in `docker-compose.yml`                                  |

### Login fails with "Invalid credentials"

- The admin account is created in **Step 1 of the setup wizard** on
  first run. Use the username and password you entered there.
- If you've forgotten the password, change it from **Settings → Security**
  while logged in, or reset the entire stack:
  ```bash
  docker compose down -v   # ⚠ wipes the database and all stored credentials
  ./scripts/onboard.sh     # brings it back up; wizard appears again on first visit
  ```

### JWT key file: backup and loss recovery

`.jwt_secret_key` at the repo root is the **root of trust** for all credentials
you store through the setup wizard. The app uses it to encrypt every integration
secret (AWS keys, Azure SP credentials, etc.) in the database.

**It cannot be migrated to a vault** in the community edition — it's the bootstrap
key that decrypts everything (including any vault credentials), so it must be
present at startup from the host. See [Protect and back up the JWT
key](#protect-and-back-up-the-jwt-key) above and
[why](secrets-management.md#why-the-jwt-root-key-cannot-be-migrated). (Removing the
on-disk key via cloud workload identity is a SaaS-edition feature.)

**Protect it:** back it up somewhere safe (password manager, encrypted drive), and
don't commit it to git (it's in `.gitignore`).

**If you lose it**, every stored credential is unrecoverable and the app will
refuse to start (the key file is required). Recovery procedure:

```bash
# 1. Stop the stack
docker compose down

# 2. Remove the old key and database volume (⚠ wipes all stored credentials)
rm .jwt_secret_key
docker volume rm vm-dashboard-community_pgdata   # adjust prefix to match 'docker volume ls'

# 3. Rerun the onboard script — it regenerates the key and the wizard reappears
./scripts/onboard.sh
```

**Rotating the key** is not currently supported without clearing the database.

### Where to file issues

Open a GitHub issue with:

1. The output of `.\scripts\Onboard-Dashboard.ps1` (copy the terminal)
2. The last 100 lines of `docker compose logs app`
3. Your OS / Docker Desktop / PowerShell versions
4. What you expected vs. what happened

**Do not paste `.env` contents** — they contain your cloud credentials.

---

## Appendix A — VMware Workstation integration

Lists, starts and stops VMware Workstation VMs. A **remote agent** runs on the
Workstation host and talks to `vmrest`, the REST daemon that ships with
Workstation Pro; the agent polls outward, so the dashboard needs no route to
that machine and nothing has to be opened toward it.

> **Full guide:** [docs/integrations/vmware.md](integrations/vmware.md)

### Prerequisites

- VMware Workstation **Pro** (Player does not include `vmrest`)
- A remote agent enrolled on that host — see
  [docs/remote-agents.md](remote-agents.md)
- Outbound HTTPS from the host to the dashboard. Nothing inbound

### Enable the integration

1. On the Workstation host, set the vmrest credentials once and start it:
   ```powershell
   vmrest -C
   vmrest
   ```
2. Enrol an agent on that host, then add a **workstation** connection bound to
   it. Its `policy.yaml` entry needs:
   ```yaml
   connections:
     - name: my-workstation
       verbs: [inventory_sync, power_on, power_off]
       allow_loopback: true
   ```
   `allow_loopback` is required because vmrest binds `127.0.0.1`, and the
   power verbs are required or the Start/Stop buttons are refused by the agent.
3. Turn on **VMware** and **remote agents** in Settings → Integrations.
4. The "Workstation" nav entry appears. Open it and press **Sync Now**.

> **Upgrading?** This used to work by SSHing from the container to the Windows
> host and running a PowerShell wrapper around `vmrun`. That path has been
> removed: `VM_CLI_WRAPPER_PATH` and the `SSH_*` settings no longer do
> anything, and `docker-compose.override.windows.yml` is no longer needed.
> `POWERSHELL_EXECUTION_MODE` no longer affects this integration, but do **not**
> delete it — Portainer still reads it to decide whether to reach containers
> directly or through the Azure Automation Hybrid Worker.

---

## Appendix B — VMware vSphere / ESXi integration

> **Full guide:** [docs/integrations/vsphere.md](integrations/vsphere.md)

Optional. Connects to a vCenter Server or standalone ESXi host via the
vSphere Web Services API (pyVmomi). Supports VM power operations and status
listing. Works with vCenter 6.7+ and ESXi 6.7+. Works on any host OS —
not Windows-only.

### Enable the integration

1. **Settings → Integrations** → toggle **VMware vSphere / ESXi** on.
2. Fill in the vCenter or ESXi hostname, a read/write user (e.g. a dedicated
   service account with the VM power-user role), and the datacenter name.
   Standalone ESXi uses `ha-datacenter`.
3. Click **Save**. No restart required.

---

## Appendix C — Proxmox VE integration

> **Full guide:** [docs/integrations/proxmox.md](integrations/proxmox.md)

Optional. Connects to a Proxmox VE node or cluster via the Proxmox REST API.
Supports VM and LXC container start, stop, reboot, and status listing. Works
with Proxmox VE 6.x and later.

### Enable the integration

1. **Settings → Integrations** → toggle **Proxmox VE** on.
2. Fill in your Proxmox host URL, a user with API access (e.g.
   `root@pam` or a dedicated API user), and the corresponding API token or
   password.
3. Click **Save**. No restart required.

See the full guide for creating a least-privilege API token on the Proxmox
side.

---

## Appendix D — Microsoft Hyper-V integration

> **Full guide:** [docs/integrations/hyperv.md](integrations/hyperv.md)

Optional. Manages Hyper-V VMs on a Windows Server or Windows 10/11 Pro host
via WinRM (Windows Remote Management) and remote PowerShell. No agent is
required on the host.

### Prerequisites

- Windows Server 2016–2025 or Windows 10/11 Pro / Enterprise with Hyper-V
  enabled on the target host.
- WinRM enabled and reachable from the container:
  ```powershell
  Enable-PSRemoting -Force
  ```
- A Windows user account (local or domain) with Hyper-V Administrator rights
  on the target host.

### Enable the integration

1. **Settings → Integrations** → toggle **Microsoft Hyper-V** on.
2. Fill in the Hyper-V host address, username, and password.
3. Click **Save**. No restart required.

---

## Appendix E — Nutanix AHV integration

> **Full guide:** [docs/integrations/nutanix.md](integrations/nutanix.md)

Optional. Connects to **Prism Central** (or Prism Element) via the Nutanix
REST API v3. Supports VM start, graceful ACPI shutdown, force stop, reboot,
and status listing. Graceful shutdown requires Nutanix Guest Tools to be
installed in the VM.

### Enable the integration

1. **Settings → Integrations** → toggle **Nutanix AHV** on.
2. Fill in the Prism Central hostname, a Prism user account (with VM power
   operation permissions), and the password.
3. Click **Save**. No restart required.

---

## Appendix F — XCP-ng / XenServer integration

> **Full guide:** [docs/integrations/xcpng.md](integrations/xcpng.md)

Optional. Connects to an XCP-ng or XenServer host or pool master via the
XAPI XML-RPC API. Supports VM start, clean shutdown, force shutdown, reboot,
and status listing.

### Enable the integration

1. **Settings → Integrations** → toggle **XCP-ng / XenServer** on.
2. Fill in the host URL (e.g. `https://xcp-host.local`), username (typically
   `root`), and password.
3. Click **Save**. No restart required.

---

## Appendix G — Sign in with Microsoft (Entra OAuth)

Optional. Lets users log in with their work Microsoft account instead of
a local password.

### Create a second Azure app registration

This is a **different** registration from the resource-management service
principal in Part B.

1. Azure Portal → **App registrations** → **New registration**.
   - Name: `Dashboard OAuth (dev)`
   - Supported account types: single-tenant
2. **Authentication** → **Add platform** → **Web**.
   - Redirect URI: `http://localhost:8001/api/auth/oauth/azure/callback`
3. **API permissions** → **Add a permission** → **Microsoft Graph** →
   **Delegated** → `openid`, `profile`, `email`.
4. **Certificates & secrets** → **New client secret**. Copy the value.

### Wire it up

**During initial setup:** In the setup wizard, go to Step 3 (Azure) and
expand the **Sign in with Microsoft — optional** panel. Enter the Client
ID, Client Secret, and Tenant ID, then complete the wizard as normal.

**After initial setup:** Navigate to `/setup` in your browser (admin
login required). The wizard reopens in reconfigure mode. Go to Step 3
and expand the OAuth panel — the Client ID and Tenant ID will be
pre-filled if already configured; leave the secret field blank to keep
the stored value.

The redirect URI is derived automatically from your browser's host —
you do not set it in the dashboard. Register the same URI that appears
in the wizard hint (`{your-host}/api/auth/oauth/azure/callback`) in the
Azure app registration under **Authentication**.

Once saved, the login page shows a **Sign in with Microsoft** button
without a restart.

Optional: map Entra group object IDs to dashboard workgroups from
**Settings → Groups** — users in a mapped group are auto-created and
assigned workgroups on first OAuth login.

---

## Appendix H — BeyondTrust integrations

All optional, and **each is enabled independently** — you only need the ones you license.

> **Full guide:** [docs/integrations/beyondtrust.md](integrations/beyondtrust.md), which
> maps the whole surface. Per-product detail:
> [Password Safe](integrations/password-safe.md),
> [Privileged Remote Access](integrations/privileged-remote-access.md),
> [EPM-L](integrations/epml.md).

- **Password Safe** (`password_safe_enabled`) — the dashboard checks out SSH keys and
  passwords on demand, so credentials never need to be stored locally, and onboards the
  VMs, databases and Kubernetes tokens it builds as managed systems + accounts.
- **Privileged Remote Access** (`pra_enabled`) — Shell Jump, Web Jump, Remote RDP and
  protocol-tunnel jump items, plus the Gateway hosts they broker through.
- **EPM for Linux** (`epml_enabled`) — agent package builds and installation tokens.

### Prerequisites

- For Password Safe: a **Password Safe / Secrets Safe** tenant. `ps-cli` ships as the
  `beyondtrust-bips-cli` pip dependency, so it is already in any image built from this
  repo — there is no separate binary to install.
- For PRA: a **PRA** appliance or cloud tenant, plus a Jump Group and Gateway that
  already exist in it (the dashboard looks them up by name).

### Part 1 — Password Safe OAuth application (ps-cli)

ps-cli authenticates to Password Safe with an OAuth 2.0 client credentials
grant. Create the application in Password Safe:

1. **Password Safe** → **Configuration** → **API Registration** →
   **Add API Registration**.
   - Authentication type: **Client Credentials**
   - Copy the **Client ID** and **Client Secret**.
2. Grant the registration access to the secrets and managed accounts the
   dashboard needs. At minimum: **Secrets > Read**, **Requests > Create**,
   **Credentials > Read**.

### Part 2 — PRA Config API credentials (if using PRA)

The `sra` Terraform provider authenticates to the BeyondTrust PRA Configuration API with
its own client credentials. Obtain them from:

**BeyondTrust PRA** → **Configuration** → **API Configuration** →
**Add API Account**. Copy the **Client ID** and **Client Secret**.

The API host is the hostname of your PRA appliance
(e.g. `https://pra.company.com`). If your PRA and Password Safe are the
same appliance, the host and credentials may be the same as Part 1.

### Enable and configure

1. **Settings → Integrations** → toggle **Password Safe** on. The configuration
   panel opens automatically.

2. Fill in its **API connection** section:

   | Field | Value |
   |-------|-------|
   | Password Safe URL | Base URL of your Password Safe instance, e.g. `https://ps.company.com` |
   | OAuth Client ID | From the API Registration you created in Part 1 |
   | OAuth Client Secret | From the API Registration |
   | API run-as account | The BeyondInsight user the OAuth client runs as (needed for VM onboarding) |

3. If you use PRA, toggle **Privileged Remote Access** on and fill in its
   **API connection** section:

   | Field | Value |
   |-------|-------|
   | PRA API Host | Your PRA appliance URL, e.g. `https://pra.company.com` |
   | OAuth Client ID | From the PRA API Account |
   | OAuth Client Secret | From the PRA API Account |

   Then set the Jump Group and Gateway under **Shell Jump provisioning** — the pickers
   enumerate both from the PRA Config API.

4. Click **Save** on each panel. No restart required.

> **Secret note:** Client secrets are encrypted with AES-256 in the
> application database. Leaving a secret field blank on a subsequent save
> keeps the stored value — you only need to re-enter secrets when rotating
> them.

---

## Appendix I — Entitle resource registration

> **Full guide:** [docs/integrations/entitle.md](integrations/entitle.md)

Optional. As the dashboard builds **Linux VMs** and **cloud databases**, it
registers each as an **Entitle** integration (SSH ephemeral accounts /
PostgreSQL / MySQL / SQL Server) so users request just-in-time access in Entitle.
Entitle is a BeyondTrust company; requires an active Entitle tenant. (The former
*approval-gate* integration has been removed.)

### Enable the integration

1. **Settings → Integrations → Entitle** → fill in the API URL/token, the
   Terraform provider key, and `owner`/`workflow` IDs; toggle **Register built VMs
   & databases in Entitle** on.
2. Per build, check **Register in Entitle** on the VM/DB form (opt-in, default off).
3. **Private** targets (private RDS, PRA-only VMs) need a shared **Entitle agent** —
   install it into a managed cluster via `POST /api/k8s/clusters/{id}/entitle-agent`.
   Public targets need no agent.

---

## Appendix J — MCP server (AI client integration)

> **Full guide:** [docs/integrations/mcp-server.md](integrations/mcp-server.md)

The dashboard exposes an [MCP (Model Context Protocol)](https://modelcontextprotocol.io)
server at `/mcp`. Any compatible AI client — Claude Desktop, Claude Code,
Cursor, Continue, or any MCP-capable tool — can connect to it with read-only
access to jobs, VMs, EC2 instances, and Azure VMs.

No extra containers or services are needed — the server runs inside the main
`app` container.

### Step 1 — Create a Personal Access Token

1. Open the dashboard → **Settings** (top-right avatar or `/settings`).
2. Scroll to **Security Keys → API Tokens** (or go directly to `/tokens`).
3. Click **New Token**, give it a name (e.g. `claude-desktop`), set an
   expiry if desired, and click **Create**.
4. Copy the token — it looks like `vmcli_<64 hex characters>`.
   It is shown only once.

### Step 2 — Configure your AI client

#### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "vm-dashboard": {
      "url": "http://localhost:8001/mcp",
      "headers": {
        "Authorization": "Bearer vmcli_<your-token>"
      }
    }
  }
}
```

Restart Claude Desktop. A new **vm-dashboard** entry appears in the tool
picker.

#### Claude Code (CLI)

```bash
claude mcp add --transport http vm-dashboard http://localhost:8001/mcp \
  --header "Authorization: Bearer vmcli_<your-token>"
```

#### Other clients

Point the client at `http://<host>:8001/mcp` with the
`Authorization: Bearer vmcli_<token>` header. The server uses the
HTTP Streamable transport (SSE).

### Available tools

| Tool | Description |
|------|-------------|
| `dashboard_summary` | Active jobs, today's failures, enabled integrations |
| `list_jobs` | Recent jobs — filter by status and/or workgroup |
| `get_job` | Full detail for one job by UUID |
| `list_vms` | VMware VMs (requires VMware integration enabled) |
| `list_ec2_instances` | EC2 instances deployed via this dashboard |
| `list_amis` | Available AMIs from AWS |
| `list_azure_vms` | Azure VMs deployed via this dashboard |

All tools are **read-only**. Deploy, start, and stop actions must be
performed through the web UI.

---

## Appendix K — Portainer CE integration

> **Full guide:** [docs/integrations/portainer.md](integrations/portainer.md)

Optional. Connects to a self-hosted **Portainer CE** instance to manage
Docker containers from the dashboard.

### Enable the integration

1. **Settings → Integrations** → toggle **Portainer CE** on.
2. Fill in the Portainer base URL and an API access token (created in
   Portainer under **Account → Access Tokens**).
3. Click **Save**. No restart required.

---

## Appendix L — Remote Worker (Ansible + Kubernetes runners)

> **Full guide:** [docs/integrations/ansible.md](integrations/ansible.md)

> Formerly "Ansible config management." The Settings panel is now
> **Configuration → Remote Worker**; it configures the Ansible runner
> *and* the Kubernetes (kubectl/helm) runner, which share the same
> per-cloud cloud-task settings.

Optional. Enables the **Config Mgmt** tab for running Ansible playbooks
and provisioning assets against managed VMs, plus the Kubernetes runner
for cluster-API ops. Each runner can run locally or as a one-shot cloud
task (AWS ECS Fargate / Azure ACI / GCP Cloud Run).

### Enable the integration

1. **Settings → Integrations** → toggle **Remote Worker** on.
2. Configure the runner backends (`ansible_runner`, `k8s_runner`) and any
   shared cloud infrastructure in the **Remote Worker** configuration panel.
3. Click **Save**. No restart required.

---

## Appendix M — Cloud Functions (preview)

> **Full guide:** [docs/integrations/cloud-functions.md](integrations/cloud-functions.md)
> **First deploy:** [docs/runbooks/cloud-functions-first-deploy.md](runbooks/cloud-functions-first-deploy.md) — ordered GCP → AWS → Azure, with what fails how at each stage.

Optional, and **preview**. Deploys a dashboard-authored Python handler as an
**AWS Lambda**, an **Azure Linux Function App**, or a **GCP Cloud Run
function** — the same handler, unchanged, on all three. Each gets a stable
HTTPS endpoint protected by a shared bearer secret, and can optionally be
attached to a VPC/VNet so it can reach private resources.

This is the one thing the dashboard could not do before: an **always-on
endpoint an external system can call** to act inside your network. The
existing one-shot runners (ECS / ACI / Cloud Run jobs) already reach private
resources, but they are dashboard-initiated and slow to start, so they cannot
serve a synchronous inbound call.

### Enable the integration

1. **Settings → Preview features** → toggle **Cloud Functions** on. The nav
   entry and `/api/functions` appear immediately; no restart.
2. **Settings → Integrations → Cloud Functions** → set a package store for each
   cloud you want to deploy to (an S3 bucket, a GCS bucket, and/or a blob
   container on the dashboard's existing storage account). A cloud without one
   is shown in the deploy form but blocked, with the reason.
3. *(Optional)* Fill in the **VPC / VNet attachment** fields if you need
   functions that reach private resources.
4. **Functions → Deploy function** → pick `echo_diag` and deploy, then use
   **Test invoke**. It reports what the function can actually reach, separating
   DNS failures from routing/security-group failures.

### Good to know

- Packages are built **deterministically** and named by content hash, so an
  unchanged function is a Terraform no-op.
- The handler **fails closed**: with no shared secret in its environment it
  returns 500, never an unauthenticated 200.
- GCP deploys run Cloud Build (60–120 s) and leave Artifact Registry images
  behind after `terraform destroy`.
- Azure needs a plan that supports VNet integration — `B1` (the default) does,
  `Y1` (Consumption) does not.
