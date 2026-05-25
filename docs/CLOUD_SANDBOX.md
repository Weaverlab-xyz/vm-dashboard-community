# Cloud Sandbox Guide

A walkthrough of `scripts/sandbox/` — bash and PowerShell scripts that
provision **isolated lab environments** in AWS, Azure, and GCP for the
VM Dashboard. Target audience: testers and lab operators who want a
production-style network topology without hand-clicking through three
cloud consoles.

Each cloud has both a bash variant (WSL / Linux / macOS) and a
PowerShell variant (Windows). Both variants are functionally equivalent
— same resources, same tags, same idempotency, same printed config
block — so pick whichever fits your shell.

> If you're onboarding for the first time and just want to deploy VMs to
> your own clouds without isolation guarantees, the
> [main onboarding guide](ONBOARDING.md) Parts A–C is the simpler path.
> Use the sandbox scripts when you want repeatable, fully isolated lab
> infra you can tear down with one command.

> **Looking for a one-line summary of each script?** See
> [`scripts/sandbox/README.md`](../scripts/sandbox/README.md). This guide
> goes deeper — what each script creates, how it isolates traffic, cost,
> verification, and tear-down. Read the script README first if you just
> want the file inventory; read this doc when you're about to run them.

- [What you get](#what-you-get)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [What each script creates](#what-each-script-creates)
  - [AWS](#aws)
  - [Azure](#azure)
  - [GCP](#gcp)
- [Wire the sandbox into the dashboard](#wire-the-sandbox-into-the-dashboard)
- [Verifying isolation](#verifying-isolation)
- [Cost](#cost)
- [Tearing it all down](#tearing-it-all-down)
- [Customising](#customising)
- [Caveats](#caveats)
- [Troubleshooting](#troubleshooting)

## What you get

A consistent topology across all three clouds:

| Layer | Purpose | Internet egress |
|---|---|---|
| **Jumpoint segment** | Hosts the BeyondTrust SRA Jumpoint container so it can phone home to PRA's relay. | ✅ Yes |
| **VM segment** | Hosts the lab VMs you deploy via the dashboard. | ❌ No — only the Jumpoint can reach them, and they cannot reach the internet directly. |

Per-cloud isolation mechanism:

| Cloud | Jumpoint host | VM isolation |
|---|---|---|
| **AWS** | ECS Fargate task in a public subnet (IGW-routed) | Private subnet with no IGW route + restrictive security group (egress to VPC only) |
| **Azure** | ACI container in a delegated subnet | NSG denies `Internet` outbound, allows `VirtualNetwork` |
| **GCP** | COS-on-GCE VM in a Cloud-NAT-attached subnet | Sibling subnet has no NAT mapping + firewall egress-deny on tagged VMs |

Each setup script also creates:

- A managed-by-dashboard service principal / IAM role / service account
  with the minimum permissions the dashboard needs.
- An SSH key pair stored as `{public_key, private_key}` JSON in the cloud's
  secret manager (Secrets Manager / Key Vault / Secret Manager).
- (Azure only) A storage account + file share for the Jumpoint container's
  `/jpt` persistence volume.

Every resource is tagged `managed-by=dashboard-sandbox` and named with a
`dashboard-sandbox-` prefix. Rollback enumerates by tag, so a lost state
file or partial setup doesn't strand resources.

## Prerequisites

The scripts run on WSL Ubuntu, bare Linux, macOS (bash variants in
[`scripts/sandbox/Linux/`](../scripts/sandbox/Linux)) or Windows
PowerShell 7 (variants in [`scripts/sandbox/Windows/`](../scripts/sandbox/Windows)).
Pick whichever fits your shell.

```bash
# Bash (WSL / Linux / macOS)
./scripts/sandbox/Linux/00-prereqs.sh
```

```powershell
# PowerShell (Windows)
.\scripts\sandbox\Windows\Test-SandboxPrereqs.ps1
```

Both prereq scripts verify the same things:

- `aws` (CLI v2)
- `az`
- `gcloud`
- `docker` + `docker compose v2`
- `jq`, `ssh-keygen`

…and print install hints (`apt` / `curl` on Linux, `winget` on Windows)
for anything missing. After installing whatever they flag, authenticate
the CLIs you plan to use:

```
aws configure                                            # or: aws sso login
az login
gcloud auth login && gcloud auth application-default login
```

Each setup script verifies its own CLI is authenticated before doing
anything destructive — running the Azure setup without `az login` fails
fast with a "not authenticated" message.

## Quick start

```bash
# Bash
./scripts/sandbox/Linux/00-prereqs.sh

# Provision whichever clouds you want — order doesn't matter, run any subset
./scripts/sandbox/Linux/setup-aws.sh
./scripts/sandbox/Linux/setup-azure.sh
./scripts/sandbox/Linux/setup-gcp.sh

# Bring up the dashboard
./scripts/onboard.sh
```

```powershell
# PowerShell
.\scripts\sandbox\Windows\Test-SandboxPrereqs.ps1

# Provision whichever clouds you want — order doesn't matter, run any subset
.\scripts\sandbox\Windows\Setup-AwsSandbox.ps1
.\scripts\sandbox\Windows\Setup-AzureSandbox.ps1
.\scripts\sandbox\Windows\Setup-GcpSandbox.ps1

# Bring up the dashboard
.\scripts\Onboard-Dashboard.ps1
```

Then open `http://localhost:8001` (the community edition's default port) and
paste the printed config blocks into the `/setup` wizard.

Each setup script is **idempotent** — re-running picks up where it left
off and reuses anything tagged `managed-by=dashboard-sandbox`. Safe to
re-run after a partial failure or a network blip.

## What each script creates

### AWS

```
VPC dashboard-sandbox-vpc (10.99.0.0/16)
  ├─ public subnet  10.99.1.0/24  → IGW → internet      [ECS Jumpoint task]
  └─ private subnet 10.99.2.0/24  → local VPC only      [user EC2 instances]

Security groups:
  dashboard-sandbox-jumpoint-sg
    egress: 0.0.0.0/0 (so PRA relay is reachable)
    ingress: from VPC (10.99.0.0/16)
  dashboard-sandbox-vm-sg
    egress: 10.99.0.0/16 only — no internet
    ingress: tcp/22 from dashboard-sandbox-jumpoint-sg

ECS:
  cluster bt-jumpoint
  IAM role ecsTaskExecutionRole (sandbox-tagged) with the AWS-managed
    AmazonECSTaskExecutionRolePolicy attached

Secrets Manager:
  dashboard/sandbox/ssh-keypair   {public_key, private_key} JSON

IAM (sandbox-tagged, deleted by rollback):
  role ecsTaskExecutionRole                      ECS task pull + logs
  role vmimport                                  vmie.amazonaws.com → S3 + EC2
  role dashboard-sandbox-promote-runner-task     ECS task → S3 PutObject
  user dashboard-sandbox-app                     Dashboard programmatic creds
    inline policy dashboard-app-policy           EC2 / ECS / SM / S3 / Logs
    access key cached at ~/.dashboard-sandbox/aws/secret_access_key (0600)
```

**Idempotency**: re-running `setup-aws.sh` looks up resources by tag and
reuses anything already present. The IGW, route tables, subnet associations,
SG rules, IAM policy attachments are all conditional inserts. The dashboard
IAM user is reused on re-runs and the inline policy is re-applied each time
so policy edits in the script land without rotating the access key. AWS
allows at most 2 access keys per user, so the script never rotates blindly;
if the cached secret is missing but a key still exists in AWS, it warns
with recovery paths rather than minting a third key.

### Azure

```
Resource group dashboard-sandbox-rg
  └─ VNet dashboard-sandbox-vnet (10.99.0.0/16)
       ├─ aci-subnet 10.99.1.0/24 (delegated to Microsoft.ContainerInstance)
       │    → internet egress (default)            [ACI Jumpoint]
       └─ vm-subnet  10.99.2.0/24
            NSG dashboard-sandbox-vm-nsg:
              outbound: allow VirtualNetwork (priority 100)
                        deny  Internet (priority 200)
              inbound:  allow VirtualNetwork
            → no internet egress                   [user Azure VMs]

Storage account dashboard-sandbox-…  (Standard_LRS, file share `jpt`)
  for the ACI Jumpoint /jpt persistence volume

Key Vault dashboard-sandbox-kv-…
  Secret azureVM-ssh-keypair    {public_key, private_key} JSON

Service principal dashboard-sandbox-sp
  Contributor on the resource group
  Read on the Key Vault (so the dashboard can fetch the keypair at runtime)
  Credentials cached at ~/.dashboard-sandbox/azure/sp.json (mode 600)
```

**Naming caveat**: Key Vault and storage account names must be globally
unique. The script appends a hash of your subscription ID to keep them
collision-safe.

### GCP

```
VPC dashboard-sandbox-vpc (custom mode)
  ├─ dashboard-sandbox-jumpoint-subnet 10.99.1.0/24
  │    → Cloud NAT → internet                     [Jumpoint COS GCE VM]
  └─ dashboard-sandbox-vm-subnet       10.99.2.0/24
       (no Cloud NAT mapping)
       → no internet egress                       [user GCE instances]

Cloud Router dashboard-sandbox-router
Cloud NAT    dashboard-sandbox-nat
  --nat-custom-subnet-ip-ranges dashboard-sandbox-jumpoint-subnet
  (only the jumpoint subnet gets NAT — the VM subnet is genuinely cut off)

Firewall rules:
  dashboard-sandbox-allow-internal
    ingress, all protos, source 10.99.0.0/16
  dashboard-sandbox-allow-ssh-from-jumpoint
    ingress tcp/22, source-tag bt-jumpoint, target-tag dashboard-sandbox-vm
  dashboard-sandbox-deny-vm-egress
    egress, all protos, target-tag dashboard-sandbox-vm, dest 0.0.0.0/0
  dashboard-sandbox-allow-vm-egress-vpc
    egress, all protos, target-tag dashboard-sandbox-vm, dest 10.99.0.0/16

Service account dashboard-sandbox-sa@<project>.iam.gserviceaccount.com
  Roles: compute.admin, secretmanager.secretAccessor,
         iam.serviceAccountUser, run.admin
  Key cached at ~/.dashboard-sandbox/gcp/sa-key.json (mode 600)

Secret Manager:
  dashboard-sandbox-ssh-keypair    {public_key, private_key} JSON
```

**Auto-tagging**: the dashboard automatically attaches the `bt-jumpoint`
network tag to its Jumpoint COS VM and the `dashboard-sandbox-vm` tag (read
from the `gcp_default_network_tag` config key the sandbox script provides)
to every user VM it deploys. The firewall rules' source/target tags match
those automatically — no manual tagging in the deploy form.

## Wire the sandbox into the dashboard

Each setup script ends with a config block formatted like this:

```
═══════════════════════════════════════════════════════════════
  GCP sandbox configuration — paste into /setup or Settings
═══════════════════════════════════════════════════════════════

gcp_project_id=my-lab-project
gcp_region=us-central1
gcp_zone=us-central1-a
gcp_network=dashboard-sandbox-vpc
gcp_subnetwork=dashboard-sandbox-vm-subnet
gcp_jumpoint_subnetwork=dashboard-sandbox-jumpoint-subnet
gcp_ssh_key_secret_name=dashboard-sandbox-ssh-keypair
gcp_jumpoint_image=beyondtrust/sra-jumpoint:latest
gcp_jumpoint_machine_type=e2-micro
gcp_default_network_tag=dashboard-sandbox-vm
gcp_service_account_json=$(cat …/sa-key.json | jq -c .)

# BeyondTrust deploy key — set in /setup or /secrets:
gcp_cloud_run_docker_deploy_key=…
```

Two ways to apply these to a running dashboard:

**Setup wizard (first run).** Open the dashboard, walk through `/setup`,
and paste the matching values into Step 2 (AWS), Step 3 (Azure), or Step 4
(GCP). Some keys are exposed under **Advanced** within each step.

**Settings panel (after first run).** Open `/settings`, expand the
relevant cloud panel, paste the values, and **Save**. Each cloud panel
patches the same encrypted config DB the wizard writes to. Restarts are
not needed — config is read per-request.

For the sandbox specifically, you'll want to also paste the BeyondTrust
deploy key in `/setup` Step 5 (or `/secrets` if you're using an external
secrets backend) — the setup scripts can't generate that for you because
it's issued by your PRA tenant.

## Verifying isolation

After a deploy, sanity-check that the VM segment really is cut off from
the internet. From the dashboard's job log, identify your VM's private
IP, then:

**AWS / GCP** (via the Jumpoint as a jump host, or the dashboard's Shell
Jump if BT is wired):

```bash
# From the lab VM
curl -m 5 https://example.com   # should hang/fail — no internet route
ip route                         # should show no default route, or only VPC routes

# Confirm the Jumpoint side has internet
curl -m 5 https://example.com   # should succeed from the Jumpoint container
```

**Azure**:

The NSG rule denies `Internet` outbound. You can verify from the Azure
portal: **VM → Networking → Outbound port rules → Effective security
rules** should show your `deny-internet-out` rule active. From the VM
itself, `curl https://example.com` should fail at TCP, not at TLS.

## Cost

Estimates if you leave the sandbox sitting idle (no VMs deployed). All
three cloud free tiers cover most of this.

| Cloud | Idle / month | Why |
|---|--:|---|
| AWS   | ~$0     | VPC, subnets, IGW, SGs, IAM are free; ECS cluster has no charge until a task runs. Secrets Manager: ~$0.40. |
| Azure | ~$0.05  | RG, VNet, NSGs, Key Vault, SP free. Storage account file share: ~$0.05. |
| GCP   | ~$1.50  | Cloud NAT bills hourly even when idle. VPC, subnets, firewall rules, Secret Manager are free. |

Running infrastructure adds the obvious things:

- AWS Fargate Jumpoint task: ~$10/mo (256 CPU / 512 MB).
- Azure ACI Jumpoint: ~$10/mo (1 vCPU / 2 GB).
- GCP `e2-micro` Jumpoint: ~$5/mo.
- User VMs: standard EC2 / VM / GCE pricing for whatever you deploy.

If GCP idle cost matters, tear down between sessions:

```bash
# Bash
./scripts/sandbox/Linux/rollback.sh --cloud gcp -y
```

```powershell
# PowerShell
.\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud gcp -Yes
```

## Tearing it all down

```bash
# Bash
./scripts/sandbox/Linux/rollback.sh --cloud aws         # one cloud
./scripts/sandbox/Linux/rollback.sh --cloud azure
./scripts/sandbox/Linux/rollback.sh --cloud gcp
./scripts/sandbox/Linux/rollback.sh --cloud all -y      # all three, skip prompts
```

```powershell
# PowerShell
.\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud aws
.\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud azure
.\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud gcp
.\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud all -Yes
```

What rollback does:

- Enumerates resources by `managed-by=dashboard-sandbox` tag/label and the
  `dashboard-sandbox-` name prefix.
- **Refuses to delete** if user VMs are still running in the sandbox network
  — terminate them via the dashboard first. This is intentional; the
  rollback won't silently destroy lab work in progress.
- Deletes resources in dependency order (Secrets/secrets first, then SGs,
  RTs, subnets, IGW, VPC) so each delete succeeds.
- Wipes the local state directory (`~/.dashboard-sandbox/<cloud>/`) on
  success.

Azure rollback uses `az group delete --no-wait` (cascade) — the entire RG
goes away in one call. AWS and GCP delete resource-by-resource.

Service principals (Azure) and service accounts (GCP) are also deleted.
The AWS `ecsTaskExecutionRole` is only deleted if it was tagged by us; an
existing role created outside the sandbox is preserved.

## Customising

Common environment-variable overrides — both variants honour the same
env vars:

```bash
# Bash
AWS_REGION=us-west-2          ./scripts/sandbox/Linux/setup-aws.sh
AZURE_LOCATION=westus2        ./scripts/sandbox/Linux/setup-azure.sh
GCP_PROJECT_ID=my-proj GCP_REGION=us-east1 ./scripts/sandbox/Linux/setup-gcp.sh
SANDBOX_STATE_DIR=/path/to/state ./scripts/sandbox/Linux/setup-aws.sh
```

```powershell
# PowerShell
$env:AWS_REGION = 'us-west-2';       .\scripts\sandbox\Windows\Setup-AwsSandbox.ps1
$env:AZURE_LOCATION = 'westus2';     .\scripts\sandbox\Windows\Setup-AzureSandbox.ps1
$env:GCP_PROJECT_ID = 'my-proj'
$env:GCP_REGION = 'us-east1';        .\scripts\sandbox\Windows\Setup-GcpSandbox.ps1
$env:SANDBOX_STATE_DIR = 'C:\state'; .\scripts\sandbox\Windows\Setup-AwsSandbox.ps1
```

CIDRs (10.99.0.0/16), subnet sizes, machine types, and IAM scope are
intentionally hard-coded. The sandbox is opinionated. Edit the script
directly if you need a different topology.

The `SANDBOX_NAME_PREFIX` and `SANDBOX_TAG_VALUE` constants in
`scripts/sandbox/Linux/lib/common.sh` (or `Windows/lib/Common.ps1`) rename
the prefix/tag if you want multiple isolated sandboxes per cloud account
— but most users don't need this.

## Caveats

- **Azure NSG service tag `Internet`** blocks the IPs Microsoft has
  classified as internet-routable. A handful of Azure-platform endpoints
  (DNS, NTP, the Azure metadata service at 169.254.169.254) are reachable
  via separate `AzurePlatform*` service tags — by design, since Azure VMs
  legitimately need DNS and metadata. Add explicit deny rules for those if
  your threat model excludes them.
- **AWS public subnet is the Jumpoint's only home.** The dashboard's
  printed config sets the *private* subnet as the deploy default, but if
  someone overrides the deploy form's subnet to the public one, the
  resulting EC2 instance will get internet access. The sandbox doesn't
  prevent that mistake — it relies on the default.
- **GCP Cloud NAT and tags are scoped to the VPC.** If you delete the VPC
  out from under the dashboard while VMs are still attached, rollback
  exits early with the running-VMs warning. Always terminate via the
  dashboard first.
- **The setup scripts don't install or configure the dashboard itself.**
  They only wire the cloud-side scaffolding. Run `./scripts/onboard.sh`
  separately to bring the app stack up.

## Troubleshooting

**"`aws` is not authenticated"** — run `aws configure` (or `aws sso login`
if you use AWS SSO) and retry. The script verifies via `sts get-caller-identity`.

**"Insufficient privileges" on Azure SP creation** — your account needs
`Application.ReadWrite.OwnedBy` on Entra and `Owner` (or equivalent) on
the subscription. Ask your tenant admin to grant or run the script as a
user with those scopes.

**GCP setup hangs at "Cloud NAT created"** — first NAT in a region can
take 60–90 s to propagate. The script polls; if it hangs longer than 5
minutes, check the GCP console under **VPC network → Cloud NAT** for the
real status.

**Rollback says "instances still running" but the dashboard shows none**
— the dashboard tracks by job extra_data; some manually-created instances
may exist outside its view. Run `aws ec2 describe-instances --filters
'Name=vpc-id,Values=…'` (or the equivalent for Azure/GCP) to find
strays, then terminate them and re-run rollback.

**"docker daemon not reachable" in WSL** — Docker Desktop's WSL
integration may be off for your distro. Open Docker Desktop settings →
**Resources → WSL integration** → enable for the right distro. Or run
Docker Engine natively in WSL: `sudo apt install docker.io docker-compose-v2`.

**Re-running `setup-*.sh` after a partial failure leaves orphans** — the
scripts are idempotent at the resource-name level; if a name conflict from
a prior failed run blocks creation, run `rollback.sh --cloud <cloud>` to
clean up and try again.

---

For the script reference (one-line file summary), see
[`scripts/sandbox/README.md`](../scripts/sandbox/README.md).
