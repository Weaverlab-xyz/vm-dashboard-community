# Cloud sandbox bootstrappers

WSL-first bash scripts that provision an **isolated lab environment** in
AWS, Azure, and GCP for the VM Dashboard. Each cloud's sandbox follows the
same pattern: one network segment runs the BeyondTrust SRA Jumpoint
container with internet egress (so it can phone home to PRA), and a
second segment hosts your deployed lab VMs with **no internet path** —
the Jumpoint is the only outbound proxy.

| Cloud | Jumpoint host | VM isolation mechanism |
|---|---|---|
| AWS   | ECS Fargate task in public subnet | Private subnet with no IGW route + restrictive security group (egress within VPC only) |
| Azure | ACI container in delegated subnet | NSG denies `Internet` outbound, allows `VirtualNetwork` |
| GCP   | COS-on-GCE VM in NAT-attached subnet | Sibling subnet has no Cloud NAT mapping + firewall egress-deny rule on tagged VMs |

## Prerequisites

Run on WSL Ubuntu (also fine on bare Linux). Needs:

- `aws` CLI v2
- `az` CLI
- `gcloud` SDK
- `docker` + `docker compose v2` (for running the dashboard itself)
- `jq`, `ssh-keygen`, `curl`, `unzip`

The `00-prereqs.sh` script verifies all of these and prints apt/curl install
hints for anything missing:

```bash
./scripts/sandbox/00-prereqs.sh
```

Then authenticate each CLI you plan to use:

```bash
aws configure                                            # or: aws sso login
az login
gcloud auth login && gcloud auth application-default login
```

## Provisioning

Each setup script is **idempotent** — re-running picks up where it left off
and reuses any existing resources tagged `managed-by=dashboard-sandbox`.

```bash
./scripts/sandbox/setup-aws.sh
./scripts/sandbox/setup-azure.sh
./scripts/sandbox/setup-gcp.sh
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
ec2_ssh_key_secret=dashboard/sandbox/ssh-keypair
…
```

The values point at the sandbox-tagged resources the script just created.

## Cost

Per-cloud monthly estimates if you leave the sandbox sitting idle (no VMs
deployed):

| Cloud | Idle cost / month | Why |
|---|---|---|
| AWS   | ~$0   | VPC, subnets, IGW, SGs, IAM are free; ECS cluster is free until a task runs. Secret in Secrets Manager: ~$0.40. |
| Azure | ~$0   | Resource group, VNet, NSGs, Key Vault free at idle. Storage account ~$0.05. |
| GCP   | ~$1.50 | Cloud NAT charges per-hour even when idle (~$1.50/mo). VPC, subnets, firewall rules, Secret Manager are free. |

A running Jumpoint container/VM adds:
- AWS:  ~$10/mo for ECS Fargate (256 CPU / 512 MB).
- Azure: ~$10/mo for ACI (1 vCPU / 2 GB).
- GCP:  ~$5/mo for `e2-micro`.

## Tear-down

```bash
./scripts/sandbox/rollback.sh --cloud aws         # one cloud
./scripts/sandbox/rollback.sh --cloud all -y      # all three, no confirm
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

Sensitive files (Azure SP creds, GCP SA key) are written with mode 600.

## Customising

Common env-var overrides:

```bash
AWS_REGION=us-west-2          ./scripts/sandbox/setup-aws.sh
AZURE_LOCATION=westus2        ./scripts/sandbox/setup-azure.sh
GCP_PROJECT_ID=my-proj GCP_REGION=us-east1 ./scripts/sandbox/setup-gcp.sh
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
