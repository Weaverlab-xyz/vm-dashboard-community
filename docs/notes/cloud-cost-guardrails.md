# Cloud cost guardrails

Rules and failure modes for keeping small AWS and Azure lab accounts honest. Written
after audits on 2026-08-26 that found **roughly 60% of AWS spend and 43% of Azure spend
was waste** — and that in both clouds the things previously blamed for it were wrong.

Cost allocation tag: **`managed-by`** (values `vm-dashboard`, `dashboard-sandbox`).
No account IDs, subscription IDs, or resource IDs here on purpose — this repo is public.
Re-run the verification commands ([AWS](#aws-verification-commands),
[Azure](#azure-verification-commands)) against your own accounts instead.

## The same three shapes in both clouds

Every item found in either audit was one of these. Look for these first:

| Shape | AWS | Azure |
|---|---|---|
| An expensive **network object serving nothing** | Secrets Manager interface VPC endpoint, ~$7.30/mo, in a VPC whose only ENI was its own | NAT gateway + its static IP, ~$36/mo, on a delegated subnet with zero IP configurations |
| **Storage outliving its compute** | Orphaned EBS root volumes from AMIs shipping `DeleteOnTermination: false` | Unattached managed disk (13 months); VM backups still `Protected` after the VMs were deleted |
| **Unassociated IPs** | Unattached Elastic IPs | Standard static public IPs with no `ipConfiguration` |

And one inverted trade worth knowing before you start:

> **AWS charges you to query cost. Azure refuses to let you.**
> Cost Explorer bills ~$0.01/request and became the single largest line item in the AWS
> account. Azure's Cost Management API is free but throttles so aggressively that it is
> effectively unusable for an audit. Neither meters cleanly.

---

# AWS

## Three traps that a naive "unattributed spend" hunt gets wrong

### 1. The cost query is itself a line item

**Cost Explorer bills ~$0.01 per `GetCostAndUsage` request.** A dashboard that polls it
on a short TTL can easily become the single most expensive thing in a small account —
in the audited account it was ~45% of the bill, larger than any actual workload.

Worse, **Cost Explorer API charges can never carry a cost-allocation tag.** They are an
account-level service charge. So driving "unattributed spend" to zero is structurally
impossible while anything polls the API, and every diagnostic run makes the number you
are chasing go up.

`cost_cache_ttl_seconds` (`config.py`) is the knob. It defaults to 24 h because CE
figures only settle a few times a day — a shorter TTL buys freshness the upstream data
does not actually have. `cost_explorer_enabled=false` turns it off entirely.

The warmer is `main.py:_warm_cost_summary`, and `cost_cache.warm_interval_seconds()` is
`ttl * 0.8`, so the TTL is what actually paces the spend.

### 2. A tagged resource is not a cheap resource

An interface VPC endpoint costs ~$0.01/hr **per AZ** (~$7.30/mo) before data processing.
That is real money — but if it carries a `managed-by` tag it will never appear in an
`{"Tags": {"MatchOptions": ["ABSENT"]}}` query. Its usage type reads `$0.00` there.

Filtering for *untagged* spend and calling the remainder "attributed, therefore fine" is
how the most expensive object in an account stays invisible. **Always sanity-check
against a whole-account `USAGE_TYPE` breakdown**, not just the unattributed slice.

### 3. "Deleted" in a doc is not deleted in the account

The previous version of this file listed two EBS volumes as decommissioned. Both were
still `available` and still billing when checked, and one had been *created the day
before the doc declared it deleted* — so what looked like a stale table was really an
active, recurring leak. Resource state belongs in the API, not in Markdown.

---

## Root cause: orphaned root volumes

Custom Packer AMIs routinely ship **`DeleteOnTermination: false`** on the root device.
Launch such an AMI without supplying your own block-device mapping and the instance
inherits that flag: on terminate, the root volume survives as an untagged `available`
volume, billing ~$0.10/GB-mo forever. It is untagged because **`TagSpecifications` with
`ResourceType: instance` does not propagate to volumes** — so the orphan also has no
owner, and lands in the unattributed bucket with nothing to trace it back to.

Audit your own images before trusting them:

```bash
aws ec2 describe-images --owners self \
  --query 'Images[].[Name,BlockDeviceMappings[0].Ebs.DeleteOnTermination,BlockDeviceMappings[0].Ebs.VolumeType]' \
  --output text
```

In the audited account 2 of 7 self-built AMIs had it set to `false`, and both orphaned
volumes traced back to one of them.

### How this repo closes it

- `terraform/ec2_instance/main.tf` — `root_block_device { delete_on_termination = true }`
  plus tags. Correct since `0c08347`.
- `aws_service._root_bdm_sync()` — forces `DeleteOnTermination: true` and `gp3` on the
  root device, and is applied at **every** `boto3` `run_instances` call site.
  Unconditionally: gating the mapping on a resize (`if root_disk_gb`) meant a
  default-sized instance silently inherited the AMI's flag.
- `aws_service._volume_tag_spec()` — adds `ResourceType: volume` so a root volume
  carries its instance's tags and any future orphan is traceable.

Both paths must stay fixed. Terraform alone is not enough — the leak came from the
`boto3` paths, which Terraform never touches.

---

## Interface endpoint lifecycle

Interface endpoints bill hourly whether or not anything uses them, so the dashboard
ref-counts them (`services/ssm_endpoint_service.py`) rather than leaving them standing:

- `SSM_SERVICES` (`ssm`/`ssmmessages`/`ec2messages`) — created on the first EC2/DB
  deploy, deleted with the last.
- `RECLAIM_ONLY_SERVICES` (`secretsmanager`) — **swept but never created.** The sandbox
  setup script creates it for vpc-mode Cloud Functions. Adding it to `SSM_SERVICES`
  would make every EC2 deploy stand up a ~$7.30/mo endpoint nothing on the SSM path
  needs — turning a leak into a standing charge.

The `secretsmanager` sweep is gated on a live count of VPC-attached Lambdas and **fails
safe**: if the count errors, the endpoint is left alone. A vpc-mode Lambda in a private
subnet with no NAT reaches Secrets Manager *only* through that endpoint, and deleting it
breaks secret reads **at runtime**, not at deploy time.

Before deleting any endpoint by hand, prove nothing is using it — a VPC whose only ENI
is the endpoint's own is safe; anything else is not:

```bash
aws ec2 describe-network-interfaces --filters Name=vpc-id,Values=<vpc-id> \
  --query 'NetworkInterfaces[].[NetworkInterfaceId,InterfaceType,Description]' --output text
```

If it carries an `aws:cloudformation:stack-name` tag, edit the stack instead or the next
deploy recreates it.

---

## AWS rules for new infrastructure

1. **Tag everything** with `managed-by`, and tag **volumes** explicitly — instance tags
   do not propagate. Cost allocation tags are **not retroactive**: a resource tagged
   today still reads as untagged for prior months.
2. **Never launch an EC2 instance without an explicit root block-device mapping**
   setting `DeleteOnTermination: true`. Do not condition it on a resize.
3. **No NAT gateway** (~$32/mo) without explicit approval.
4. **No new interface VPC endpoints** without a confirmed private-subnet requirement,
   and only through the ref-counted helpers. Gateway endpoints for S3 and DynamoDB are
   free — prefer those.
5. **No idle load balancers** (~$16/mo each).
6. **Prefer gp3 over gp2** — ~20% cheaper at equal baseline performance.
7. **Deregister the AMI to free a snapshot.** A snapshot backing a registered AMI cannot
   be deleted, and every self-owned AMI is a user-visible deploy option
   (`aws_service._list_amis_sync`) — pruning one removes a choice from the Images page.
8. **Watch for cross-region drift.** A secret or volume in a region you don't otherwise
   use bills the same and hides from single-region checks.

---

## AWS verification commands

> **The `--query` path for a grouped result is `Metrics.UnblendedCost.Amount`, not
> `Total.UnblendedCost.Amount`.** `Total` exists only at the `ResultsByTime` level. Using
> `Total` inside `Groups[]` returns `None` for every row — and if the output is piped
> through a numeric filter, the whole breakdown silently prints nothing. This is the
> single easiest way to conclude an account is clean when it isn't.

```bash
START=$(date -u +%Y-%m-01); END=$(date -u -d tomorrow +%Y-%m-%d)   # CE end is exclusive

# Unattributed spend by service
aws ce get-cost-and-usage --time-period Start=$START,End=$END \
  --granularity MONTHLY --metrics UnblendedCost \
  --filter '{"Tags":{"Key":"managed-by","MatchOptions":["ABSENT"]}}' \
  --group-by Type=DIMENSION,Key=SERVICE \
  --query 'ResultsByTime[0].Groups[].[Metrics.UnblendedCost.Amount,Keys[0]]' --output text | sort -rn

# Whole account by usage type — the sanity check that catches TAGGED waste
aws ce get-cost-and-usage --time-period Start=$START,End=$END \
  --granularity MONTHLY --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=USAGE_TYPE \
  --query 'ResultsByTime[0].Groups[].[Metrics.UnblendedCost.Amount,Keys[0]]' --output text | sort -rn

# How much is the cost page itself costing? (UsageQuantity = billable requests)
aws ce get-cost-and-usage --time-period Start=$START,End=$END \
  --granularity DAILY --metrics UnblendedCost UsageQuantity \
  --filter '{"Dimensions":{"Key":"SERVICE","Values":["AWS Cost Explorer"]}}' \
  --query 'ResultsByTime[].[TimePeriod.Start,Total.UnblendedCost.Amount,Total.UsageQuantity.Amount]' \
  --output text

# Idle resources that bill with no tag
aws ec2 describe-volumes --filters Name=status,Values=available \
  --query 'Volumes[].[VolumeId,Size,VolumeType,CreateTime]' --output text
aws ec2 describe-addresses --query 'Addresses[?AssociationId==`null`].[PublicIp]' --output text
aws ec2 describe-nat-gateways --filter Name=state,Values=available \
  --query 'NatGateways[].[NatGatewayId,VpcId]' --output text
aws ec2 describe-vpc-endpoints \
  --query 'VpcEndpoints[].[VpcEndpointId,ServiceName,VpcEndpointType]' --output text
aws elbv2 describe-load-balancers --query 'LoadBalancers[].[LoadBalancerName,Type]' --output text
```

Run the volume and endpoint checks in **every region you have ever touched**, not just
your default one.

---

# Azure

Azure's provisioning paths in this repo are **already correct** — the waste found in the
audit was hand-built lab infrastructure, not anything the code creates. That makes the
Azure half of this document mostly *detective*: what to look for, and why the obvious
tooling won't show it to you.

## Four tooling traps, in the order they bite

1. **The Cost Management query API is unusable for an audit.**
   `POST .../Microsoft.CostManagement/query` returns `429 Too many requests` relentlessly
   — at subscription *and* resource-group scope, often on the very first call of the day.
   Don't build a retry loop around it. Use the Consumption API instead:
   `GET .../Microsoft.Consumption/usageDetails?api-version=2023-05-01&metric=ActualCost`.
   Different throttle bucket, free, and it returns **per-resource** cost, which the query
   API does not.
2. **`az consumption usage list` silently returns nothing useful.** It maps a modern
   usage record onto the legacy schema, so `pretaxCost`, `usageQuantity`, and
   `instanceName` all come back `None` while the call reports success. Go straight to the
   REST endpoint with a bearer token.
3. **The `nextLink` Azure hands back contains raw spaces** in its `$filter`, which
   Python's `http.client` rejects outright as control characters. Re-encode only the
   spaces (`url.replace(" ", "%20")`) — re-quoting the whole URL double-encodes the
   skiptoken and the next page comes back empty.
4. **MFA enforcement blocks writes but not reads.** With a non-MFA token every `list`
   and `show` succeeds, so an audit looks completely healthy — and then every delete
   fails with `RequestDisallowedByAzure`. Re-authenticate interactively *before*
   concluding a cleanup worked.

> A corollary worth its own line: **check exit codes, don't chain on a pipe.**
> `az ... 2>&1 | head -3 && echo "deleted"` prints `deleted` when `head` succeeds, not
> when `az` does. That reports a clean sweep while nothing was removed.

## Cost traps

- **Deleting a VM does not stop its backup.** A Recovery Services vault keeps charging
  the protected-instance fee plus recovery-point storage indefinitely. The tell is a
  container whose `healthStatus` is `Deleted` while its item's `protectionState` is still
  `Protected` — Azure knows the VM is gone and bills anyway. Stopping it deletes the
  restore points, so it is irreversible:
  `az backup protection disable --delete-backup-data true`.
- **A NAT gateway cannot be deleted while a subnet still references it**
  (`CannotDeleteNatGatewayAssociatedToSubnet`). Remove the reference first with
  `az network vnet subnet update --remove natGateway`. A NAT gateway plus its static IP
  is ~$36/mo, and a *delegated but empty* subnet keeps it alive with nothing behind it.
- **Standard static public IPs bill while unassociated** — the Elastic IP equivalent.
- **A deallocated VM still pays full price for its OS disk.** Stopping a VM saves only
  compute. An AVD session host's 128 GiB Premium P10 is ~$19/mo whether it runs or not;
  `StandardSSD_LRS` is roughly half. Downgrade only if you accept slower boot and login —
  AVD is latency-sensitive on the OS disk, and the change is reversible.

## What the repo already guarantees (don't "fix" these)

Unlike the AWS side, Azure never had the orphaned-disk defect, because every launch path
sets the mapping explicitly:

| Path | OS disk | Public IP |
|---|---|---|
| `terraform/azure_vm/main.tf` | `disk_delete_option = "Delete"` | `count = create_public_ip ? 1 : 0`, same state as the VM |
| `azure_service._deploy_vm_sync` | `delete_option="Delete"` | `_best_effort_cleanup` deletes the PIP explicitly |
| `azure_service._run_vm_jumpoint_sync` | `delete_option="Delete"` | — |
| `azure_service._run_vm_container_node_sync` | OS `Delete`, data disk `Detach` | — |

`tests/test_node_azure.py` asserts that asymmetry. There is **no** `azurerm_nat_gateway`
and **no** Recovery Services resource anywhere in this repo — AKS moved to an outbound
load balancer, and nothing here has ever created a VM backup. If you find either in a
subscription, it was built by hand.

`rollback.sh --cloud azure` deletes the sandbox resource group only. Anything you create
outside it has no sweep at all, which is exactly how the audited waste survived.

## Azure verification commands

Set `SUB` to the subscription you are auditing.

```bash
# Per-resource cost, month to date. BOTH usageStart AND usageEnd are required — with
# only a start date the API returns 400 "Invalid Date Range, End Date is missing".
SUB=$(az account show --query id -o tsv)
TOKEN=$(az account get-access-token --subscription "$SUB" --query accessToken -o tsv)
curl -sG "https://management.azure.com/subscriptions/$SUB/providers/Microsoft.Consumption/usageDetails" \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'api-version=2023-05-01' \
  --data-urlencode 'metric=ActualCost' \
  --data-urlencode "\$filter=properties/usageStart ge '$(date -u +%Y-%m-01)' and properties/usageEnd le '$(date -u +%Y-%m-%d)'" \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);[print(f"{p["cost"]:9.2f}  {p.get("resourceName")}  [{p.get("resourceGroup")}]") for p in (r["properties"] for r in d["value"]) if p["cost"]>=0.01]'
```

That prints the **first page only**. A real audit has to follow `nextLink` and sum across
pages — remembering trap 3 above, or page two dies on a control-character error.

```bash
# Backups still billing for VMs that no longer exist.
# Filter on the ITEM's protectionState, not the container's healthStatus: a container
# keeps healthStatus=Deleted forever after cleanup, so a healthStatus check reports
# vaults you already fixed. Cross-check each virtualMachineId against `az vm list`.
az backup vault list --query '[].[name,resourceGroup]' -o tsv | while read -r V G; do
  az backup item list --vault-name "$V" -g "$G" --backup-management-type AzureIaasVM \
    --query "[?properties.protectionState=='Protected'].[properties.friendlyName,properties.virtualMachineId]" -o tsv
done
```

```bash
# Unattached disks. `az disk list` REQUIRES -g (no subscription-wide form as of CLI
# 2.88), so iterate resource groups or the command just errors.
az group list --query '[].name' -o tsv | while read -r G; do
  az disk list -g "$G" --query "[?diskState=='Unattached'].[name,resourceGroup,sku.name,timeCreated]" -o tsv
done
```

```bash
# Idle resources that bill for nothing
az network public-ip list --query "[?ipConfiguration==null && natGateway==null].[name,resourceGroup,sku.name]" -o tsv
az network nat gateway list --query '[].[name,resourceGroup,length(subnets || `[]`)]' -o tsv
az vm list -d --query "[?powerState!='VM running'].[name,resourceGroup,powerState]" -o tsv
```

A deallocated VM in that last list is not free: its OS disk bills at full price.

A NAT gateway reporting `0` subnets, or one whose subnet has no IP configurations, is
billing for nothing:

```bash
az network vnet subnet show -g <rg> --vnet-name <vnet> -n <subnet> \
  --query '{ipConfigs: length(ipConfigurations || `[]`), delegations: delegations[].serviceName}'
```

---

*Resource IDs and dollar figures go stale. Re-run the commands rather than trusting any
table — including this one.*
