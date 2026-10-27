# Cloud Sandbox Guide

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want an isolated cloud account for the dashboard's labs, bootstrapped rather than hand-built.

Part of [Cloud](../clouds.md).

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
> [main onboarding guide](../getting-started.md) Parts A–C is the simpler path.
> Use the sandbox scripts when you want repeatable, fully isolated lab
> infra you can tear down with one command.

> **Looking for a one-line summary of each script?** See
> [`scripts/sandbox/README.md`](../../scripts/sandbox/README.md). This guide
> goes deeper — what each script creates, how it isolates traffic, cost,
> verification, and tear-down. Read the script README first if you just
> want the file inventory; read this doc when you're about to run them.

- [What you get](#what-you-get)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [What each script creates](sandbox/what-it-creates.md)
  - [AWS](sandbox/what-it-creates.md#aws)
  - [Azure](sandbox/what-it-creates.md#azure)
  - [GCP](sandbox/what-it-creates.md#gcp)
- [Wire the sandbox into the dashboard](sandbox/wiring.md)
- [Verifying isolation](#verifying-isolation)
- [Cost](#cost)
- [Tearing it all down](sandbox/teardown.md)
- [Customising](sandbox/customising.md)
- [Caveats](#caveats)
- [Troubleshooting](#troubleshooting)

## What you get

A consistent topology across all three clouds:

| Layer | Purpose | Internet egress |
|---|---|---|
| **Gateway segment** | Hosts the BeyondTrust SRA Gateway container so it can phone home to PRA's relay. | ✅ Yes |
| **VM segment** | Hosts the lab [VMs you deploy](vms.md) via the dashboard. | ❌ No — only the Gateway can reach them, and they cannot reach the internet directly. |
| **DB segment** | Dedicated private subnets for [managed cloud databases](../cloud-services/databases.md) (AWS shown; Azure/GCP/OCI have their own DB subnets below). | ❌ No — brokered only through the PRA tunnel. |
| **[Managed Kubernetes](../cloud-services/kubernetes.md)** | Clusters build their **own** network — the dashboard's Terraform creates each cluster's VPC/VNet + subnets + egress (AWS: a small NAT instance) and destroys it on decommission. No sandbox k8s subnet. The AWS EKS build additionally VPC-peers back to the sandbox VPC and opens the DB/VM SGs for direct access. | Cluster-owned (per-cluster NAT). Management-plane access via Entitle/PRA + the AWS peering. |
| **[Cloud Functions](../cloud-services/functions.md)** | `network_mode=vpc` functions reach private resources. Azure gets a dedicated `functions-subnet` delegated to `Microsoft.Web/serverFarms`; AWS reuses the private subnet plus a Secrets Manager interface endpoint (without which a vpc-mode Lambda cannot read its bearer secret and 500s on every invoke); GCP uses Direct VPC egress into the VM subnet — no connector, nothing billed while idle. **No OCI support.** | ❌ No on AWS (no NAT unless a VM is up). Azure: Storage + Key Vault via service endpoints only — generic egress needs a NAT gateway. |
| **Desktops segment** (Azure) | Dedicated **non-delegated** subnet (`10.99.6.0/24`) for VDI desktop pools, separate from the VM segment because the RS jump client must register with the appliance at first boot. Azure alone needs its own segment: its `aci-subnet` is delegated and cannot host a VM NIC, so there is nothing safe to inherit. AWS and GCP pools default to `aws_desktops_subnet_id` / `gcp_desktops_subnetwork` and fall back to the ordinary VM subnet, which the sandbox already creates. | ⚠️ **443 only** — the NSG allows outbound HTTPS (jump-client registration + Windows activation/updates) but denies other Internet; RDP brokered in via the Gateway. |

Per-cloud isolation mechanism:

| Cloud | Gateway host | VM isolation |
|---|---|---|
| **AWS** | ECS Fargate task in a public subnet (IGW-routed) | Private subnet with no IGW route + restrictive security group (egress to VPC only) |
| **Azure** | ACI container in a delegated subnet | NSG denies `Internet` outbound, allows `VirtualNetwork` |
| **GCP** | COS-on-GCE VM in a Cloud-NAT-attached subnet | Sibling subnet has no NAT mapping + firewall egress-deny on tagged VMs |

Each setup script also creates:

- A managed-by-dashboard service principal / IAM role / service account
  with the minimum permissions the dashboard needs.
- An SSH key pair stored as `{public_key, private_key}` JSON in the cloud's
  secret manager (Secrets Manager / Key Vault / Secret Manager).
- (Azure only) A storage account + file share for the Gateway container's
  `/jpt` persistence volume.

Every resource is tagged `managed-by=dashboard-sandbox` and named with a
`dashboard-sandbox-` prefix. Rollback enumerates by tag, so a lost state
file or partial setup doesn't strand resources.

## Prerequisites

The scripts run on WSL Ubuntu, bare Linux, macOS (bash variants in
[`scripts/sandbox/Linux/`](../../scripts/sandbox/Linux)) or Windows
PowerShell 7 (variants in [`scripts/sandbox/Windows/`](../../scripts/sandbox/Windows)).
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

Stand up the dashboard, then configure it one of two ways — **Path B: one script,
no wizard**, or **Path A: per-cloud scripts + the `/setup` wizard**.

```bash
# Bash — check prereqs, then bring up the dashboard
./scripts/sandbox/Linux/00-prereqs.sh
./scripts/onboard.sh                       # see the README for the pull-based run

# Path B — one script: provision your cloud(s) AND configure the dashboard (no wizard)
./scripts/sandbox/Linux/onboard-sandbox.sh --cloud all

# — or — Path A: provision per-cloud, then paste each printed block into /setup
./scripts/sandbox/Linux/setup-aws.sh
./scripts/sandbox/Linux/setup-azure.sh
./scripts/sandbox/Linux/setup-gcp.sh
```

```powershell
# PowerShell — check prereqs, then bring up the dashboard
.\scripts\sandbox\Windows\Test-SandboxPrereqs.ps1
.\scripts\Onboard-Dashboard.ps1

# Path B — one script (provision + configure, no wizard):
.\scripts\sandbox\Windows\Onboard-Sandbox.ps1 -Cloud all

# — or — Path A: provision per-cloud, then paste each printed block into /setup
.\scripts\sandbox\Windows\Setup-AwsSandbox.ps1
.\scripts\sandbox\Windows\Setup-AzureSandbox.ps1
.\scripts\sandbox\Windows\Setup-GcpSandbox.ps1
```

Open `http://localhost:8001` (the community edition's default port). With **Path B**
you just log in — `onboard-sandbox` already pushed the config to the dashboard's
setup API. With **Path A** you paste each script's printed block into the `/setup`
wizard (see [Wire the sandbox into the dashboard](sandbox/wiring.md)).
`onboard-sandbox` runs the per-cloud `setup-*.sh` for you, so use one path or the
other — not both.

Each setup script is **idempotent** — re-running picks up where it left
off and reuses anything tagged `managed-by=dashboard-sandbox`. Safe to
re-run after a partial failure or a network blip.

## Pages

The details have a page each:

| Page | Read it when |
|---|---|
| [What each sandbox script creates](sandbox/what-it-creates.md) | you want to know exactly what the sandbox scripts create in each cloud before you run them |
| [Wiring the sandbox into the dashboard](sandbox/wiring.md) | the sandbox scripts have run and you are giving the dashboard their output |
| [Tearing the sandbox down](sandbox/teardown.md) | you are done with the sandbox and want every resource it created gone |
| [Customising the sandbox](sandbox/customising.md) | you need different names, regions, address ranges or quotas from the defaults |

## Verifying isolation

After a deploy, sanity-check that the VM segment really is cut off from
the internet. From the dashboard's job log, identify your VM's private
IP, then:

**AWS / GCP** (via the Gateway as a jump host, or the dashboard's Shell
Jump if BT is wired):

```bash
# From the lab VM
curl -m 5 https://example.com   # should hang/fail — no internet route
ip route                         # should show no default route, or only VPC routes

# Confirm the Gateway side has internet
curl -m 5 https://example.com   # should succeed from the Gateway container
```

**Azure**:

The NSG rule denies `Internet` outbound. You can verify from the Azure
portal: **VM → Networking → Outbound port rules → Effective security
rules** should show your `deny-internet-out` rule active. From the VM
itself, `curl https://example.com` should fail at TCP, not at TLS.

The sandbox sets `azure_vm_nat_enabled=true`, so while a dashboard VM exists in the
vm-subnet you will also see a `dashboard-vm-egress-vm-subnet` allow rule just above the
deny, and a `dashboard-vm-nat-<vnet>` NAT gateway attached to the subnet. That is the
dashboard's on-demand egress; both are removed with the subnet's last VM. See
[On-demand VM egress on Azure](vms/provisioning.md#on-demand-vm-egress-on-azure).

## Cost

Estimates if you leave the sandbox sitting idle (no VMs deployed). The
cloud free tiers cover most of this. For the full audit behind these figures —
including per-cluster costs and which standing resources can and can't move into
per-resource Terraform — see
[sandbox-provisioning-cost-audit](../notes/sandbox-provisioning-cost-audit.md).

| Cloud | Idle / month | Why |
|---|--:|---|
| AWS   | ~$7.70  | VPC, subnets, IGW, SGs, IAM are free; ECS cluster has no charge until a task runs. Secrets Manager: ~$0.40. No NAT gateway or Elastic IP is created. SSM interface VPC endpoints are created on-demand per running EC2/DB (~$7/mo each, ~$22/mo for all three) and removed automatically when the last one is decommissioned — nothing while idle. **The Secrets Manager interface endpoint (~$7.30) is the one that does stand: `network_mode=vpc` Cloud Functions read their bearer secret through it, so unlike the SSM three it cannot be on-demand.** Opt out with `SANDBOX_SKIP_FN_VPCE=1` if you only need public-mode functions. |
| Azure | ~$6.60  | RG, VNet, NSGs, Key Vault, SP free. Container registry (Basic): ~$5/mo — opt out with `SANDBOX_SKIP_ACR=1`. **Three private DNS zones (Postgres, MySQL, SQL Server): ~$0.50/mo each.** Those are what make VNet-integrated managed databases resolvable; they're created unconditionally, with no opt-out, and `rollback.sh --cloud azure` removes them with the resource group. Storage account file share: ~$0.05. |
| GCP   | ~$1.56  | Cloud NAT bills hourly even when idle. Secret Manager: ~$0.06 per active version. VPC, subnets, Cloud Router, firewall rules and the PSA reserved range are free. |
| OCI   | ~$0.10  | KMS vault (the shared `DEFAULT` type, not the ~$1/hr `VIRTUAL_PRIVATE`) plus one key version and one secret; skip with `OCI_SKIP_VAULT=1`. VCN, subnets, gateways, security lists and IAM are free — and unlike AWS, an OCI NAT gateway carries no hourly charge. **Cloud Functions is not supported on OCI**, so `setup-oci.sh` provisions nothing for it. |

Once `cost_explorer_enabled` is on, **`/costs` reports this baseline as its own
"sandbox" scope**, separate from resources the dashboard provisioned — the two are
distinguished by the `managed-by` tag value (`dashboard-sandbox` vs `vm-dashboard`).
Two caveats worth knowing, both surfaced as notes on the page itself:

- **Azure** tags don't inherit from a resource group to its children, so untagged
  children (disks, NICs, ACI) land in "unattributed". Enabling *Cost Management →
  Manage tag inheritance* at subscription scope fixes it with no config change here.
- **GCP** Cloud Router and Cloud NAT — i.e. the entire GCP idle cost — accept no
  labels at all. To attribute them, label the project itself (forward-only; past
  billing-export rows are immutable):
  ```bash
  gcloud projects update "$PROJECT_ID" --update-labels managed-by=dashboard-sandbox
  ```

Running infrastructure adds the obvious things:

- AWS Fargate Gateway task: ~$10/mo (256 CPU / 512 MB).
- Azure ACI Gateway: ~$10/mo (1 vCPU / 2 GB).
- GCP `e2-micro` Gateway: ~$5/mo.
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

## Caveats

- **Azure NSG service tag `Internet`** blocks the IPs Microsoft has
  classified as internet-routable. A handful of Azure-platform endpoints
  (DNS, NTP, the Azure metadata service at 169.254.169.254) are reachable
  via separate `AzurePlatform*` service tags — by design, since Azure VMs
  legitimately need DNS and metadata. Add explicit deny rules for those if
  your threat model excludes them.
- **AWS `vpc`-mode Cloud Functions depend on the Secrets Manager endpoint.**
  A VPC-attached Lambda resolves its bearer secret from Secrets Manager at cold
  start, and the private subnet has no internet route unless the on-demand NAT
  instance happens to be up for an unrelated VM. Skip the endpoint
  (`SANDBOX_SKIP_FN_VPCE=1`) and a vpc-mode function deploys cleanly, then **fails
  auth closed — a 500 on every invoke**, including the dashboard's own Test invoke.
  It reads like a bad bearer secret; it is a missing network path.
- **The Azure `functions-subnet` carries no NSG on purpose.** The function module
  turns on `vnet_route_all_enabled` whenever a subnet is attached, so *all* app
  egress leaves through that subnet — including the Functions host's own calls to
  `AzureWebJobsStorage` and `WEBSITE_RUN_FROM_PACKAGE`. Attaching the VM NSG (which
  denies the `Internet` service tag) would starve the host and the app would boot
  with zero indexed functions. `Microsoft.Storage` + `Microsoft.KeyVault` service
  endpoints keep those hops on the Azure backbone instead.
- **Cloud Functions is AWS / Azure / GCP only.** There is no
  `terraform/cloud_function/oci_*` module and no `oci` branch in
  `cloud_function_service`, so the four-cloud tables above do not imply OCI support.
- **AWS public subnet is the Gateway's only home.** The dashboard's
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
[`scripts/sandbox/README.md`](../../scripts/sandbox/README.md).
