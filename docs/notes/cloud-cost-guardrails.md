# Cloud cost guardrails

> **Audience:** operator · **Profile:** `both` · **Read this when:** your lab cloud bill is higher than the resources you can see explain.

Rules and failure modes for keeping small AWS, Azure and GCP lab accounts honest. Written
after audits on 2026-08-26 that found **roughly 60% of AWS spend and 43% of Azure spend
was waste** — and that in both clouds the things previously blamed for it were wrong —
plus a GCP audit on 2026-08-31 where **the reported cost increase was not a cost increase
at all**, and 91% of the storage footprint turned out to be image-export scratch.

Cost allocation tag/label: **`managed-by`** (values `vm-dashboard`, `dashboard-sandbox`).
No account IDs, subscription IDs, project IDs, or resource IDs here on purpose — this
repo is public. Re-run the verification commands ([AWS](#aws-verification-commands),
[Azure](#azure-verification-commands), [GCP](#gcp-verification-commands)) against your own
accounts instead.

## Start here: is it even a cost increase?

Before hunting a resource, confirm the *cause* is spend and not accounting. A view that
shows **net** cost reports an expiring credit as an infrastructure problem, and GCP's
audit began by chasing a rise that came with a 56% **drop** in usage. Split gross from
credits first — [see the GCP section](#the-trap-that-matters-most-a-credit-cliff-reads-as-a-cost-increase);
it is the cheapest question in this document and it invalidates the whole hunt.

## The same three shapes in every cloud

Almost every item found across the three audits was one of these. Look for these first:

| Shape | AWS | Azure | GCP |
|---|---|---|---|
| An expensive **network object serving nothing** | Secrets Manager interface VPC endpoint, ~$7.30/mo, in a VPC whose only ENI was its own | NAT gateway + its static IP, ~$36/mo, on a delegated subnet with zero IP configurations | Two Cloud NAT configs, two regions, attached to subnets with zero VMs |
| **Storage outliving its compute** | Orphaned EBS root volumes from AMIs shipping `DeleteOnTermination: false` | Unattached managed disk (13 months); VM backups still `Protected` after the VMs were deleted | 185 GiB of image-export scratch + superseded VHDs, in a project with no compute at all; an orphaned Packer disk, 3.5 months |
| **Unassociated IPs** | Unattached Elastic IPs | Standard static public IPs with no `ipConfiguration` | *(none found — GCP's were all in use)* |

GCP added a fourth shape the other two did not have: **a pipeline that leaks a full-size
artefact per attempt**, including per *failed* attempt. Worth checking anywhere a job
writes a multi-GB blob to scratch.

And one inverted trade worth knowing before you start:

> **AWS charges you to query cost. Azure refuses to let you. GCP does neither.**
> Cost Explorer bills ~$0.01/request and became the single largest line item in the AWS
> account. Azure's Cost Management API is free but throttles so aggressively that it is
> effectively unusable for an audit. GCP's BigQuery billing export is free and
> unthrottled — so on GCP, query freely and re-run rather than cache.

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

# GCP

Audited 2026-08-31, and it did not fit the pattern of the other two at all. GCP's waste
was neither a network object nor an unassociated IP — it was **scratch storage from the
image-export pipeline**, and the reported cost increase that triggered the audit had
nothing to do with usage.

## The trap that matters most: a credit cliff reads as a cost increase

The audit started from "my GCP cost is climbing and I don't know what changed." Nothing
had changed. Gross usage had **fallen 56%** month over month. What ended was the
free-trial credit:

| Month | Gross usage | Credits | Net |
|---|---|---|---|
| month 1 | $0.18 | −$0.18 | **$0.00** |
| month 2 | $31.38 | −$31.38 | **$0.00** |
| month 3 | $13.73 | −$4.77 | **$8.96** |

> **A trial credit expires on a 90-day clock, not when the balance runs out.**
> Only ~$24 of a $300 trial had been consumed. The remaining ~$275 was simply
> forfeited on the calendar.

So on GCP, **always separate gross from net before looking for a cause.** Any dashboard
tile that shows net cost will report a credit expiry as an infrastructure problem, and
you can burn an entire audit hunting a resource that was always there and always
costing exactly this much. The tell is unmistakable once you look: net cost is *exactly*
$0.000/day and then non-zero every day after, with no matching step in gross.

The corollary, which is easy to miss: **your first uncredited month understates your
run-rate** if anything landed mid-month. Price the idle floor from the last few days,
never from the month total.

## Root cause of the waste: the Daisy exporter never cleans up

`gcloud compute images export` / the `gce_vm_image_export` Cloud Build step runs Daisy,
which auto-creates a scratch bucket `gs://<project>-daisy-bkt-<region>` on first use and
**leaves everything in it, forever**:

- every attempt leaves the full `outs/export-disk` intermediate — the **entire image**,
  ~17 GB for a 20 GB source;
- every *failed* attempt still leaves a `logs/` + `sources/` prefix;
- the bucket it creates has **no lifecycle rule**.

The audited lab held **36 scratch prefixes / 100 GiB** — in a project with zero VMs,
zero clusters and zero databases. A second copy of each image also accumulated in the
hub bucket, because the export destination object is timestamped per attempt and nothing
prunes superseded ones: 5 copies of one image, 84 GiB, of which one was live.

Together that was 185 GiB — enough that **storage was 100% of the idle burn**:

| Idle-day line item | share |
|---|---|
| GCS standard storage | 57% |
| Persistent disks | 31% |
| Secret Manager replicas | 8% |
| Compute image storage | 4% |

### Why the fix is a lifecycle rule, not application cleanup

Most of the leaked prefixes came from builds that **failed**. A "delete scratch after a
successful export" step in the app would have missed the majority of them, and can never
run at all for a build that timed out or was cancelled. `setup-gcp.sh` /
`Setup-GcpSandbox.ps1` therefore pre-create the Daisy bucket with a 1-day TTL, so Daisy
reuses a bucket that already has the rule rather than creating a rule-less one. The rule
is applied **unconditionally**, because a bucket auto-created by an earlier export is
exactly the case that needs it retrofitted.

## "Some egress" was 15% of the bill

`_export_candidate_zones` ordered its capacity-fallback ladder by the caller's preferred
zone, and its docstring waved the consequence off as *"a cross-region worker is fine; it
just adds some GCS egress on the way to the hub."*

It is not fine. The worker VM writes the whole ~17 GB disk to the hub bucket, so the
worker's region — not the source image, which is global — decides whether that write is
free. One day of retries against a hub bucket in another region moved **67 GB for
$1.32**, which was 15% of that month's entire net bill.

The subtle part: this was **never** fallback-only. Because the ladder was anchored on the
configured zone and never compared it to the bucket, a project whose configured zone
merely *differed* from its hub bucket paid cross-region egress on **every export, first
attempt included**. The ladder now takes a `hub_region` that outranks the preferred zone
and exhausts the hub's region before leaving it — guarded by
`tests/test_gcp_export_zone_ladder.py`, including that GCS reports bucket locations
uppercase (`US-CENTRAL1`) while compute zones are lowercase, so comparing them raw
silently disables the whole preference.

## Free tiers hide cost rather than removing it

Two GCP free tiers quietly absorbed real spend, and both are **per billing account**,
not per project:

- **`e2-micro` free tier.** An idle 24/7 `e2-micro` in a *different* project consumed
  **77%** of the discount, leaving the main project's compute to pay near-full price. Its
  own net cost read as $0.29, so no per-project view flags it.
- **GKE free tier**, one zonal cluster. Covered the cluster fully — a second concurrent
  cluster gets nothing.

Both mean a resource can look free in the breakdown while making something *else*
expensive. Check who is consuming a free tier, not just what it covers.

## Latent items — $0 today, real later

- Three **Network Intelligence Center** SKUs billed ~$2.16/mo gross and were 100%
  credited by discounts literally named *"until billing comes into effect"*. If Google
  flips those on, new cost appears with no change on your side. Disable it if unused.
- **Cloud NAT configs outlive their workloads.** Two were active, in two regions,
  attached to subnets with zero VMs and zero clusters — left behind by the on-demand
  ref-counted teardown. Cheap while nothing runs; the moment a VM lands there, both bill.
- Unlike AWS and Azure, **GCP charges nothing to query cost**. The BigQuery billing
  export makes an audit essentially free, so there is no reason to cache aggressively or
  to avoid re-running these queries.

## GCP rules for new infrastructure

1. **Any bucket a tool auto-creates for scratch gets a lifecycle rule**, applied
   unconditionally and at setup time, not after a successful run.
2. **Anything that writes a multi-GB blob picks its region from the destination**, not
   from a config default or a zone ladder.
3. **Never prune by "the newest wins"** without also deleting what it superseded — a
   timestamped destination object turns every retry into permanent storage.
4. **Read gross and net separately.** A net-only view cannot distinguish a new resource
   from an expired credit.

## GCP verification commands

The billing export is the source of truth; it needs
`bigquery.jobUser` + `dataViewer`. Substitute your own export table.

```bash
# THE first query: gross vs credits vs net, by month. Run this before anything else —
# it distinguishes "something new is running" from "a credit expired".
bq query --nouse_legacy_sql "
SELECT invoice.month AS mo,
       ROUND(SUM(cost),2) AS gross,
       ROUND(SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c),0)),2) AS credits,
       ROUND(SUM(cost)+SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c),0)),2) AS net
FROM \`<project>.<dataset>.<export_table>\` GROUP BY mo ORDER BY mo"

# Which credit stopped, and exactly when. A PROMOTION whose total is far below the trial
# amount expired on the clock; it did not run out.
bq query --nouse_legacy_sql "
SELECT c.type, c.name, ROUND(SUM(c.amount),2) AS total,
       MIN(DATE(usage_start_time)) AS first_day, MAX(DATE(usage_start_time)) AS last_day
FROM \`<project>.<dataset>.<export_table>\`, UNNEST(credits) c
GROUP BY 1,2 ORDER BY total"

# The idle floor: cost on a day when nothing was running. Price the run-rate from THIS,
# not from the month total, or a mid-month arrival will understate it.
bq query --nouse_legacy_sql "
SELECT sku.description AS sku, ROUND(SUM(cost),4) AS gross
FROM \`<project>.<dataset>.<export_table>\`
WHERE DATE(usage_start_time) = '<an-idle-day>'
GROUP BY sku HAVING gross > 0 ORDER BY gross DESC"

# Who is consuming a free tier (it is per BILLING ACCOUNT, so check every project).
bq query --nouse_legacy_sql "
SELECT project.id, sku.description, ROUND(SUM(cost),3) AS amt
FROM \`<project>.<dataset>.<export_table>\`
WHERE sku.description LIKE '%free tier%'
GROUP BY 1,2 ORDER BY amt"
```

```bash
# Image-export scratch. Expect ~0; anything else is pure leak.
gcloud storage du --summarize --readable-sizes "gs://$(gcloud config get-value project)-daisy-bkt-<region>"
gcloud storage buckets describe "gs://<bucket>" --format='yaml(lifecycle_config,location,soft_delete_policy)'
```

A `soft_delete_policy` with a non-zero retention means **deleted bytes keep billing** for
that window — budget for it before a cleanup, and note it is also your undo.

```bash
# Idle resources that bill for nothing. An empty ATTACHED_TO is an orphan; Packer leaves
# these behind on an aborted build, exactly as it does on AWS.
gcloud compute disks list --format='table(name,zone.basename(),sizeGb,type.basename(),users.list():label=ATTACHED_TO)'
gcloud compute addresses list --filter='status=RESERVED' --format='table(name,region.basename(),status,users.list())'

# NAT configs with nothing to serve: a non-empty nats list plus an empty instance list.
gcloud compute routers list --format='table(name,region.basename())'
gcloud compute routers describe <router> --region <region> --format='value(nats.list())'
gcloud compute instances list
```

Finally, set an actual budget. The audited billing account had **none** — the
`billingbudgets` API was not even enabled, and the only "budget" was a number in the
dashboard's own config, which Google knows nothing about and cannot alert on.

---

*Resource IDs and dollar figures go stale. Re-run the commands rather than trusting any
table — including this one.*
