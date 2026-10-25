# Customising the sandbox

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you need different names, regions, address ranges or quotas from the defaults.

Part of [Cloud Sandbox Guide](../sandbox.md).

Common environment-variable overrides — both variants honour the same
env vars:

```bash
# Bash
# Region on a FIRST run. To add a second region to an existing sandbox these are
# not enough on their own — see "A second region is a second sandbox" below.
AWS_REGION=us-west-2          ./scripts/sandbox/Linux/setup-aws.sh
AZURE_LOCATION=westus2        ./scripts/sandbox/Linux/setup-azure.sh
GCP_PROJECT_ID=my-proj GCP_REGION=us-east1 ./scripts/sandbox/Linux/setup-gcp.sh
SANDBOX_STATE_DIR=/path/to/state ./scripts/sandbox/Linux/setup-aws.sh
SANDBOX_SKIP_ACR=1            ./scripts/sandbox/Linux/setup-azure.sh   # skip the ACR image mirror
# Authenticated Docker Hub import (dodges the anonymous pull limit during az acr import):
DOCKERHUB_USERNAME=me DOCKERHUB_TOKEN=dckr_pat_… ./scripts/sandbox/Linux/setup-azure.sh
```

```powershell
# PowerShell
$env:AWS_REGION = 'us-west-2';       .\scripts\sandbox\Windows\Setup-AwsSandbox.ps1
$env:AZURE_LOCATION = 'westus2';     .\scripts\sandbox\Windows\Setup-AzureSandbox.ps1
$env:GCP_PROJECT_ID = 'my-proj'
$env:GCP_REGION = 'us-east1';        .\scripts\sandbox\Windows\Setup-GcpSandbox.ps1
$env:SANDBOX_STATE_DIR = 'C:\state'; .\scripts\sandbox\Windows\Setup-AwsSandbox.ps1
$env:SANDBOX_SKIP_ACR = '1';         .\scripts\sandbox\Windows\Setup-AzureSandbox.ps1   # skip the ACR mirror
```

CIDRs (10.99.0.0/16), subnet sizes, machine types, and IAM scope are
intentionally hard-coded. The sandbox is opinionated. Edit the script
directly if you need a different topology.

`SANDBOX_NAME_PREFIX` (env var) renames every resource if you want multiple
isolated sandboxes in one cloud account; the `SANDBOX_TAG_VALUE` constant in
`scripts/sandbox/Linux/lib/common.sh` (or `Windows/lib/Common.ps1`) renames the tag
that rollback enumerates by. Most users need neither.

## A second region is a second sandbox

Changing only the region **is not enough**, and this is the one customisation that
bites. Nothing the scripts create is region-scoped by *name* — the resource group,
VNet/VPC, NSGs and clusters are all `${SANDBOX_NAME_PREFIX}-…` — and the cloud APIs
they call are idempotent PUTs that will not relocate an existing resource. So
`AZURE_LOCATION=westus2` on its own reuses the centralus resource group and VNet,
then writes `azure_region.westus2.*` config keys pointing at **centralus** subnets.
The dashboard resolves them faithfully and the deploy fails at apply
(`VnetWithDifferentLocationNotSupported`, or `DBSubnetGroupNotFound` on AWS).
`setup-azure.sh` refuses this outright; give each region its own prefix and state
dir instead:

```bash
SANDBOX_NAME_PREFIX=sandbox-westus2 \
  SANDBOX_STATE_DIR="$HOME/.sandbox-westus2" \
  AZURE_LOCATION=westus2 ./scripts/sandbox/Linux/setup-azure.sh
```

```powershell
$env:SANDBOX_NAME_PREFIX = 'sandbox-westus2'
$env:SANDBOX_STATE_DIR   = "$HOME\.sandbox-westus2"
$env:AZURE_LOCATION      = 'westus2';  .\scripts\sandbox\Windows\Setup-AzureSandbox.ps1
```

Both runs import into the **same** dashboard: the flat keys stay the first (default)
region's and the second run merges its ids in under `azure_region.westus2.*`. Roll a
region back by re-exporting the same prefix and state dir before running
`rollback.sh`.
