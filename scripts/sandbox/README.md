# Cloud sandbox bootstrappers

Bash and PowerShell scripts that provision an **isolated lab environment**
in AWS, Azure, and GCP for the VM Dashboard. Each cloud's sandbox follows
the same pattern: one network segment runs the BeyondTrust SRA Jumpoint
container with internet egress (so it can phone home to PRA), and a
second segment hosts your deployed lab VMs with **no internet path** —
the Jumpoint is the only outbound proxy.

| Task | Bash (WSL / Linux / macOS) | PowerShell (Windows / cross-platform) |
|---|---|---|
| Prereqs   | `00-prereqs.sh`     | `Test-SandboxPrereqs.ps1`   |
| AWS       | `setup-aws.sh`      | `Setup-AwsSandbox.ps1`       |
| Azure     | `setup-azure.sh`    | `Setup-AzureSandbox.ps1`     |
| GCP       | `setup-gcp.sh`      | `Setup-GcpSandbox.ps1`       |
| Rollback  | `rollback.sh`       | `Rollback-Sandbox.ps1`       |
| Shared    | `lib/common.sh`     | `lib/Common.ps1`             |

Both variants are functionally equivalent — same tags, same idempotency,
same printed config block. Pick whichever fits your shell. State files
written by one variant are readable by the other (same
`~/.dashboard-sandbox/<cloud>/` location).

| Cloud | Jumpoint host | VM isolation mechanism |
|---|---|---|
| AWS   | ECS Fargate task in public subnet | Private subnet with no IGW route + restrictive security group (egress within VPC only) |
| Azure | ACI container in delegated subnet | NSG denies `Internet` outbound, allows `VirtualNetwork` |
| GCP   | COS-on-GCE VM in NAT-attached subnet | Sibling subnet has no Cloud NAT mapping + firewall egress-deny rule on tagged VMs |

## Prerequisites

Tools needed regardless of which variant you run:

- `aws` CLI v2
- `az` CLI
- `gcloud` SDK
- `docker` + `docker compose v2` (for running the dashboard itself)
- `jq`, `curl`, `ssh-keygen`

The prereq script verifies them and prints install hints for anything
missing:

```bash
# Bash (WSL / Linux / macOS)
./scripts/sandbox/Linux/00-prereqs.sh
```

```powershell
# PowerShell (Windows)
.\scripts\sandbox\Windows\Test-SandboxPrereqs.ps1
```

Then authenticate each CLI you plan to use:

```
aws configure                                            # or: aws sso login
az login
gcloud auth login && gcloud auth application-default login
```

## Provisioning

Each setup script is **idempotent** — re-running picks up where it left off
and reuses any existing resources tagged `managed-by=dashboard-sandbox`.

```bash
# Bash
./scripts/sandbox/Linux/setup-aws.sh
./scripts/sandbox/Linux/setup-azure.sh
./scripts/sandbox/Linux/setup-gcp.sh
```

```powershell
# PowerShell
.\scripts\sandbox\Windows\Setup-AwsSandbox.ps1
.\scripts\sandbox\Windows\Setup-AzureSandbox.ps1
.\scripts\sandbox\Windows\Setup-GcpSandbox.ps1
```

Each script ends with a config block to paste into the dashboard's `/setup`
wizard or **Settings → Integrations** panels. It looks like:

```
═══════════════════════════════════════════════════════════════
  AWS sandbox configuration — paste into /setup or Settings
═══════════════════════════════════════════════════════════════

aws_region=us-east-2
aws_default_subnet_id=subnet-…
aws_default_security_group_id=sg-…
aws_db_subnet_group_name=dashboard-sandbox-db   # managed-DB deploys (private RDS, 2 AZs)
aws_db_security_group_id=sg-…                    # managed-DB deploys (VM-tier SG)
ec2_ssh_key_secret=dashboard/sandbox/ssh-keypair
…
```

The values point at the sandbox-tagged resources the script just created. The
AWS setup also grants the dashboard IAM user **RDS** permissions (create/delete/
modify DB instances + subnet groups) and creates a private **DB subnet group**
spanning two AZs, so the managed-database feature can deploy a private Postgres
into the sandbox.

## One-shot: provision and auto-configure (skip the wizard)

Instead of running each `setup-*.sh` and pasting the printed block into the
wizard, `onboard-sandbox` runs the chosen clouds, collects what each one
produces, and **POSTs it straight to the dashboard's setup API** — creating the
admin and marking setup complete, so you log in directly with no `/setup` wizard.

```bash
# Bash (WSL / Linux / macOS)
./scripts/sandbox/Linux/onboard-sandbox.sh --cloud all --dashboard-url http://localhost:8001
```

```powershell
# PowerShell (Windows)
.\scripts\sandbox\Windows\Onboard-Sandbox.ps1 -Cloud all -DashboardUrl http://localhost:8001
```

It prompts for a new admin username/password (or pass them as flags). Options:

| Flag (bash / PowerShell) | Purpose |
|---|---|
| `--cloud` / `-Cloud` | `aws,azure,gcp` or `all` (prompted if omitted) |
| `--dashboard-url` / `-DashboardUrl` | dashboard base URL (default `http://localhost:8001`) |
| `--admin-user`/`--admin-pass` / `-AdminUser`/`-AdminPass` | admin to create (prompted if omitted) |
| `--token` / `-Token` | admin JWT, for **re-runs** when the dashboard is already set up (adds a cloud) |
| `--push-only` / `-PushOnly` | skip provisioning; just push the cached `config.json` files |
| `--no-push` / `-NoPush` | provision and write `config.json`, but don't call the API |

**How it works:** provisioning still runs **on your machine** with your existing
cloud SSO (the dashboard container never sees high-privilege credentials). Each
`setup-*.sh` writes a machine-readable `~/.dashboard-sandbox/<cloud>/config.json`
(the same key=value pairs it prints, minus the human comments); the wrapper merges
them and sends them to `POST /api/setup/import`, which accepts the **full** key set
(including `bt_ecs_*`, `storage_*`, `promote_runner_*`) that the typed wizard form
doesn't expose. On a fresh stack the call is unauthenticated (same as the wizard's
first-run submit); once setup is complete it requires an admin token, so the
wrapper logs in for you (or pass `--token`).

> Multi-cloud note: the few keys every cloud sets (`storage_active_backend`,
> `storage_hub_backend`) take the **last** provisioned cloud's value; adjust the
> active backend afterward on **Settings → Storage** if needed.

## Cost

Per-cloud monthly estimates if you leave the sandbox sitting idle (no VMs
deployed):

| Cloud | Idle cost / month | Why |
|---|---|---|
| AWS   | ~$0   | VPC, subnets, IGW, SGs, IAM are free; ECS cluster is free until a task runs. Secret in Secrets Manager: ~$0.40. |
| Azure | ~$5   | RG, VNet, NSGs, Key Vault free at idle. Storage account ~$0.05. Container registry (Basic, mirrors the Jumpoint/Ansible/promote images so deploy-time pulls skip Docker Hub rate limits): ~$5/mo — opt out with `SANDBOX_SKIP_ACR=1`. |
| GCP   | ~$1.50 | Cloud NAT charges per-hour even when idle (~$1.50/mo). VPC, subnets, firewall rules, Secret Manager are free. |

A running Jumpoint container/VM adds:
- AWS:  ~$10/mo for ECS Fargate (256 CPU / 512 MB).
- Azure: ~$10/mo for ACI (1 vCPU / 2 GB).
- GCP:  ~$5/mo for `e2-micro`.

## Tear-down

```bash
# Bash
./scripts/sandbox/Linux/rollback.sh --cloud aws         # one cloud
./scripts/sandbox/Linux/rollback.sh --cloud all -y      # all three, no confirm
```

```powershell
# PowerShell
.\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud aws
.\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud all -Yes
```

Rollback enumerates resources by the `managed-by=dashboard-sandbox` tag/label
and the `dashboard-sandbox-` name prefix. **It refuses to delete a cloud's
infra if user-deployed lab VMs are still running** in the sandbox network —
terminate them via the dashboard first.

The Azure rollback deletes the entire resource group (cascade), so it's the
fastest tear-down. AWS and GCP delete each resource individually.

## State files

Each setup script writes a small per-cloud state directory to
`~/.dashboard-sandbox/{aws,azure,gcp}/` with the IDs it created. This is a
fast-path hint — rollback doesn't depend on it; tag-based discovery is
authoritative.

Sensitive files (Azure SP creds, GCP SA key, AWS dashboard-user access-key
secret) are written with mode 600.

### AWS dashboard IAM user

The AWS setup script also provisions an IAM user `dashboard-sandbox-app`
with an inline policy covering every AWS API the dashboard calls (EC2 /
ECS / Secrets Manager / S3 / CloudWatch Logs / vmimport-related
ec2:ExportImage etc.). Operators no longer paste their own access key
into the `/setup` wizard — the script's output block carries the
sandbox-provisioned key id and secret directly.

The access-key secret is cached at `~/.dashboard-sandbox/aws/secret_access_key`
(mode 0600). Re-runs of `setup-aws.sh` reuse the cached secret rather
than rotating; if the cache is lost but the IAM key still exists in
AWS, the script prints two recovery paths (rotate via AWS Console, or
run `rollback.sh --cloud aws` and re-run setup for a clean key).

Rollback deletes the user, its access keys, and the cached secret in
one step — but only if the user carries the `managed-by=dashboard-sandbox`
tag the setup script applied. Operator-created users with the same
name are left alone.

## Customising

Common env-var overrides — both variants read the same env vars:

```bash
# Bash
AWS_REGION=us-west-2          ./scripts/sandbox/Linux/setup-aws.sh
AZURE_LOCATION=westus2        ./scripts/sandbox/Linux/setup-azure.sh
GCP_PROJECT_ID=my-proj GCP_REGION=us-east1 ./scripts/sandbox/Linux/setup-gcp.sh
SANDBOX_SKIP_ACR=1            ./scripts/sandbox/Linux/setup-azure.sh   # skip the Azure ACR image mirror
```

```powershell
# PowerShell
$env:AWS_REGION = 'us-west-2';      .\scripts\sandbox\Windows\Setup-AwsSandbox.ps1
$env:AZURE_LOCATION = 'westus2';    .\scripts\sandbox\Windows\Setup-AzureSandbox.ps1
$env:GCP_PROJECT_ID = 'my-proj'
$env:GCP_REGION = 'us-east1';       .\scripts\sandbox\Windows\Setup-GcpSandbox.ps1
```

CIDRs, subnet sizes, machine types, and IAM scope are intentionally not
parameterised — the sandbox is opinionated. Edit the script directly if you
need a different topology.

## Limitations / known caveats

- **GCP auto-tagging**: the sandbox firewall rules key off network tags
  (`bt-jumpoint` for the Jumpoint host, `dashboard-sandbox-vm` for user
  VMs). The dashboard auto-attaches both — the Jumpoint COS VM is tagged
  `bt-jumpoint` at launch, and `gcp_default_network_tag` (set by
  `setup-gcp.sh` in the printed config block) is merged into every user
  VM's tag list at deploy time. No manual tagging required.
- **Azure NSG deny on `Internet` service tag**: blocks public IPs Microsoft
  has classified. Some Azure-internal endpoints (DNS, NTP) are reachable via
  `AzurePlatformDNS` and `AzurePlatformGUI` service tags — this is by design.
  If you need to also block those, add explicit deny rules.
- **AWS public subnet**: only the Jumpoint task lives there. If you mistakenly
  deploy a user VM into the public subnet via the dashboard, it gets internet
  by default — the sandbox doesn't prevent that, the **default subnet** in the
  config block is the private one.
- **All three set up only the network/auth scaffolding**, not the dashboard
  itself. Bring the dashboard up with `docker compose up -d` after pasting
  the config into `/setup`.
