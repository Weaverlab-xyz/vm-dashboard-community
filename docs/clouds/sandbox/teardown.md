# Tearing the sandbox down

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are done with the sandbox and want every resource it created gone.

Part of [Cloud Sandbox Guide](../sandbox.md).

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
- Deletes resources in dependency order (Secrets/secrets first, then DB subnet
  groups, VPC endpoints, SGs, RTs, NAT, subnets, IGW, VPC) so each delete
  succeeds. (VPC endpoints hold ENIs that pin the subnet/SG, so they're swept
  before those — and any left behind keep billing.)
- Wipes the local state directory (`~/.dashboard-sandbox/<cloud>/`) on
  success.

Azure rollback uses `az group delete --no-wait` (cascade) — the entire RG
goes away in one call. AWS and GCP delete resource-by-resource.

For **GCP**, a single run tears down the shared VPC across **all** regions:
it discovers every region that still has a subnet or router on the VPC and
removes each region's subnets/router/NAT, releases orphaned Cloud Run
serverless egress IPs, removes the PSA range + `servicenetworking` peering,
and sweeps any leftover VPC firewall rules before deleting the global VPC.
Like the running-VM guard, it also **refuses** if a live Rancher management
node or an active GKE↔sandbox-VPC peering is still attached — decommission
those via the dashboard first.

For **Azure** the whole resource group goes in one call, which cascades everything
in it — so that arm **refuses** while a managed Portainer/Rancher node VM, or an
orphaned node data disk, is still present. A data disk is meant to outlive its VM,
so it can be the only thing left, and cascading over it would destroy the node's
only copy of its users, environments and settings. Tear the node down via the
dashboard (which asks about the disk separately) first.

Service principals (Azure) and service accounts (GCP) are also deleted.
The AWS `ecsTaskExecutionRole` is only deleted if it was tagged by us; an
existing role created outside the sandbox is preserved.
