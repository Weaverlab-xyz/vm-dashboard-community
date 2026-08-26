# AWS cost guardrails

Rules and failure modes for keeping a small AWS account's bill honest. Written after an
audit on 2026-08-26 that found roughly 60% of a personal lab account's spend was waste,
and that the two things the previous version of this doc blamed were both wrong.

Cost allocation tag: **`managed-by`** (values `vm-dashboard`, `dashboard-sandbox`).
No account IDs or resource IDs here on purpose — this repo is public. Re-run the
[verification commands](#verification-commands) against your own account instead.

---

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

## Rules for new infrastructure

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

## Verification commands

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

*Resource IDs and dollar figures go stale. Re-run the commands rather than trusting any
table — including this one.*
