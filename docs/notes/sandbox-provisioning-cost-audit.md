# Sandbox + provisioning audit: cost and gaps

Audit of the four sandbox bootstrappers and the provisioning paths for cloud VMs, cloud
databases and Kubernetes clusters, answering three questions: what's missing, what can get
cheaper by moving from the sandbox script into per-resource Terraform, and why `/costs`
showed no sandbox spend.

Dated 2026-07-29. Line references are to that commit.

## Summary

Three findings reframed the exercise:

1. **The AWS sandbox is already cost-optimal** — ~$0.40/mo idle. The NAT gateway was
   already removed (`setup-aws.sh:10-12`, `:187-193`) and the three SSM interface
   endpoints already moved to on-demand ref-counting (`:355-362`, worth ~$22/mo). Nothing
   meaningful is left to move to Terraform on AWS. Its problems are **functional**.
2. **The real cost asymmetry isn't in the sandbox scripts at all.** It's per-cluster, in
   `terraform/k8s_cluster/azure_aks/main.tf`, which spent ~2× its EKS/GKE siblings on
   network + OS disk. See [Per-cluster cost](#per-cluster-cost).
3. **`/costs` missed sandbox spend because of a tag-value mismatch.** `cost_service`
   filtered `managed-by=vm-dashboard`; every sandbox script tags
   `managed-by=dashboard-sandbox` (`lib/common.sh:11-13`). Same key, different value,
   exact-match filter. Sandbox spend was inside the unfiltered account-total card the
   whole time, just never attributed.

## Standing idle cost, corrected

Per cloud, sandbox created, nothing deployed. `docs/CLOUD_SANDBOX.md` previously said
Azure was ~$5 and omitted the private DNS zones.

| Cloud | Idle/mo | What actually bills | Opt-out |
|---|--:|---|---|
| AWS | $0.40 | Secrets Manager secret (SSH keypair) | — |
| Azure | **$6.60** | ACR Basic $5 · **3× private DNS zone $0.50** · storage account + file share $0.11 | ACR only (`SANDBOX_SKIP_ACR=1`) |
| GCP | $1.56 | Cloud NAT gateway ~$1.50 · Secret Manager version $0.06 | — |
| OCI | $0.10 | KMS `DEFAULT` vault + key version + secret | `OCI_SKIP_VAULT=1` |

The three Azure private DNS zones (`setup-azure.sh:117-120`, `:139-142`, `:161-164`) are
created **unconditionally with no flag**, and were undocumented. They're the Azure analog
of the AWS DB subnet group / GCP private-services access: without them a VNet-integrated
flexible server or SQL private endpoint doesn't resolve.

Two things worth noting in the sandbox's favour: the OCI script correctly picks a shared
`DEFAULT` vault rather than `VIRTUAL_PRIVATE` (~$1/**hour**), and an OCI NAT gateway —
unlike an AWS one — carries no hourly charge.

### Free at idle in every cloud

Worth stating so nobody "optimises" these: VPC/VNet/VCN, all subnets, route tables, IGW,
every security group / NSG / firewall rule / security list, **all** IAM (roles, policies,
users, service principals, service accounts, groups, keys), the ECS cluster object, RDS
parameter and subnet groups, GCP Cloud Router, the PSA reserved range and its peering,
API/provider enablements, and empty S3/GCS buckets.

## Can Terraform make this cheaper?

Only where a resource's lifetime genuinely matches a provisioned resource's. Auditing each
standing item against that test:

| Item | Verdict |
|---|---|
| Azure PG + MySQL private DNS zones ($1.00/mo) | **Yes, movable.** Zone names are arbitrary as long as the suffix matches, so `db_azure_postgres` / `db_azure_mysql` could each create their own per DB → $0 when no Azure DB exists. |
| Azure SQL Server private DNS zone ($0.50/mo) | **No.** Azure fixes the name at `privatelink.database.windows.net`, so a second SQL DB collides. It needs a ref-counted service (the `ssm_endpoint_service.py` pattern), not Terraform. |
| Azure ACR ($5/mo) | **No.** It's a shared image mirror that exists to dodge Docker Hub pull limits; its lifetime matches the *sandbox*, not any resource. Deleting and re-importing 4 images per job would be slower and re-incur the pulls. The lever here is flipping the default to opt-in, not Terraform. |
| AWS Secrets Manager secret ($0.40/mo) | **No.** Holds the SSH keypair every VM deploy needs. |
| GCP Cloud NAT ($1.50/mo) | Out of scope — GCP was explicitly excluded from this pass and is already the tidiest of the four. |

That asymmetry — PG/MySQL movable, SQL Server not — is why the DNS-zone work was deferred
rather than done piecemeal: doing it properly means one ref-counted helper covering all
three engines, not two Terraform edits plus a leftover.

**Net: there is no large Terraform-shaped saving in the sandbox scripts.** The savings are
in the per-cluster Terraform that already exists.

## Per-cluster cost

East US, USD, 730 h/mo, from the Azure Retail Prices API and public AWS/GCP rates. Fixed
cost only, excluding data processing.

| AKS, per cluster-month | Before | After |
|---|--:|--:|
| Network (NAT gateway → LB outbound rule, + Standard public IP) | $36.50 | **$21.90** |
| 2 × `Standard_B2s` | $60.74 | $60.74 |
| 2 × OS disk (128 GiB Premium default) | $39.42 | $39.42 |
| **Total** | **$136.66** | **$122.06** |

`azure_aks` was the only one of the three k8s modules that specified **neither** a NAT
alternative nor node disk sizing:

| | Egress | Node disk |
|---|---|---|
| `aws_eks` | `t4g.nano` NAT instance, ~$3/mo (its own comment says so) | EKS default 20 GiB gp2, ~$2/mo for two |
| `gcp_gke` | Cloud Router + Cloud NAT + reserved static IP | **explicit** `disk_size_gb` + `disk_type` (`main.tf:362-363`) |
| `azure_aks` | Standard NAT gateway, **$32.85/mo** | **unset** → AKS default 128 GiB Premium, $19.71/mo **per node** |

EKS pays a $73/mo control plane, which is simply what EKS costs and isn't avoidable.

### What changed

The NAT gateway is gone. The cluster now egresses through the AKS-managed outbound load
balancer with **our own static public IP as its sole outbound frontend**, which preserves
the exact contract the NAT gateway existed for — a stable, knowable `/32` for the Rancher
node firewall, exported as `nat_public_ip` → `K8sCluster.egress_ip`. Supplying
`outbound_ip_address_ids` *replaces* AKS's managed outbound IP rather than supplementing
it (the options are mutually exclusive), so AKS never adds one behind our back.

Verified against the pinned provider: `azurerm ~> 3.35` resolves to **v3.117.1**, and
`terraform validate` passes with `load_balancer_profile.outbound_ip_address_ids` and
`load_balancer_sku = "standard"`.

Be precise about the saving: an AKS outbound LB is **not free**. Its outbound rule bills at
$0.025/hr — the Azure meter is literally named "Included LB Rules **and Outbound Rules**".
So this is $36.50 → $21.90, a **$14.60** saving, not the $33 a naive reading of the NAT
gateway line suggests. Data processing does drop 9× ($0.045 → $0.005/GB).

**A NAT VM mirroring `aws_eks` would be cheaper still** (~$9/cluster-month) and was
rejected: it adds ~8 resources, needs a `tls` provider or a credential variable for
`azurerm_linux_virtual_machine`'s `admin_ssh_key`, and — decisively — introduces a
cloud-init race in the module's most expensive failure path. With `userDefinedRouting` the
nodes reach even the public API server through that VM, so losing the race fails node
registration 20–40 min in and triggers `_rollback_failed_provision`
(`k8s_service.py:885-903`). Bad trade for demo clusters.

### Still on the table: the node OS disk

`os_disk_size_gb = 32` on `default_node_pool` would save a further ~$27/cluster-month
(P10 $19.71 → P4 $6.14 per node). Not applied — it was outside the agreed scope of this
pass. Two things to know if it's picked up:

- `os_disk_type = "Ephemeral"` is **not** available on `Standard_B2s`: Ephemeral needs the
  VM cache disk ≥ `os_disk_size_gb`, and B2s has ~8 GiB against an AKS floor of 30.
- Measure rather than trusting the documented default, which has moved between AKS
  versions:
  ```bash
  az aks show -g <rg> -n <cluster> --query "agentPoolProfiles[0].{sizeGb:osDiskSizeGb,type:osDiskType,vm:vmSize}" -o table
  ```

### Backward compatibility

Clusters provisioned before the change hold `azurerm_nat_gateway` in their remote state.
`terraform destroy` is `apply -destroy`: the plan comes from **prior state**, not config,
so state-only (orphaned) resources are still destroyed, and each state instance records
its own `dependencies` so association-before-gateway ordering survives. `azurerm_public_ip.nat`
deliberately keeps its Terraform address **and** its `-nat-pip` Azure name so those
destroys stay a plain match. Post-decommission audit for a pre-change cluster:

```bash
az network nat-gateway list -g <rg> --query "[?tags.\"managed-by\"=='vm-dashboard'].{name:name,rg:resourceGroup}" -o table
```

## Cost attribution: why `/costs` was blind to the sandbox

`cost_service` now queries **both** tag values and splits each cloud into `dashboard`,
`sandbox` and `unattributed`. AWS and Azure activate cost-allocation tags by **key**, so
covering the second value needed no new tag activation, and both scopes still come back in
a single request per cloud (Cost Explorer bills ~$0.01 each).

Where tag filtering *cannot* reach, the page now says so in per-cloud notes rather than
silently under-reporting:

| Cloud | What escapes | Why it's structural | Handling |
|---|---|---|---|
| GCP | Cloud Router + Cloud NAT — the **entire** GCP idle cost | Neither accepts labels | Query also reads `project.labels`; labelling the project attributes them (forward-only, documented one-liner in `CLOUD_SANDBOX.md`) |
| Azure | Disks, NICs, public IPs, ACI created beside a tagged parent | Azure tags don't inherit RG → child, and Azure creates children untagged | Enable *Cost Management → Manage tag inheritance*; zero code, and inherited tags lose to resource tags so semantics are exactly right |
| OCI | All tag-based attribution, permanently | The Usage API reports only cost-tracking (defined) tags; the sandbox writes a **freeform** tag (`setup-oci.sh:51`) | Scoped by compartment instead. Honest consequence: `dashboard_total` is `None` and the sandbox figure includes dashboard deploys, since both share the compartment |
| AWS | Untaggable line items (inter-AZ transfer, some VPC charges) | Nothing to tag | Falls in the account-total remainder |

The OCI tag filter was **dead code** — it could never have matched anything, which is why
OCI's breakdown was always empty.

Two related fixes:

- `aws_service._create_ssm_endpoint_sync` tagged the **dashboard-created** SSM interface
  VPC endpoint (~$7/mo each) `dashboard-sandbox`, so genuine dashboard spend was
  under-reported. Now `vm-dashboard`. Safe: `rollback.sh:108-129` sweeps endpoints by
  `vpc-id`, not by tag.
- The adjacent security group in `_ensure_ssm_vpce_security_group_sync` **deliberately
  keeps** `dashboard-sandbox` and must not be "fixed to match". `rollback.sh:48` builds
  `Name=tag:managed-by,Values=dashboard-sandbox` and step 2b deletes SGs with exactly that
  filter; AWS refuses `DeleteVpc` while a non-default SG remains, so retagging it would
  wedge the whole sandbox teardown with `DependencyViolation`. Security groups are free and
  never appear in Cost Explorer, so nothing is lost.
  `tests/test_managed_by_tag_values.py` pins that as the single sanctioned exception.

`setup-gcp.sh` also normalised **both** label key and value to underscores
(`managed_by=dashboard_sandbox`) while the dashboard's own GCP code used hyphens. Hyphens
are legal in GCP label keys and values, so the normalisation bought nothing. Fixed forward,
but the query must keep accepting both forms permanently: billing-export rows are
immutable, existing sandboxes stay underscored until re-run, and the month of the change
legitimately contains both.

## Functional gaps

### Fixed: the Windows AWS bootstrapper was materially behind

The biggest "what's missing" finding. `Setup-AwsSandbox.ps1` never created several things
`setup-aws.sh` does, so **anyone onboarding AWS from PowerShell got broken cloud-DB tunnels
and broken Gateway deploys**:

| Missing | Consequence |
|---|---|
| `clouddb-nossl-pg16` / `clouddb-nossl-mysql84` parameter groups | RDS forces SSL; the PRA protocol tunnel has no backend-TLS option, so cloud-DB sessions fail |
| `AWSServiceRoleForRDS` | First RDS create in a virgin account can fail |
| `dashboard-sandbox-db-sg` | Config fell back to the VM SG — no Jumpoint-scoped ingress |
| `ecsInstanceRole` + instance profile + 2 policy attachments | Gateway host's ECS agent can't register. The policy already granted `PassRole` on `role/ecsInstanceRole` — a role the script never created |
| `bt_ecs_launch_type=EC2` | **Worst of the set.** Absent, the Jumpoint defaults to Fargate, which cannot protocol-tunnel at all |
| `bt_ecs_host_instance_profile` | Dashboard has no profile to attach to the on-demand Jumpoint host |

Now at parity: both scripts issue an identical set of AWS CLI operations and emit an
identical set of 37 config keys. The `AmazonEC2ContainerServiceforEC2Role` attach is
outside the role-create branch, which is the detail that caused the earlier fleet-wide
gateway outage — a pre-existing `ecsInstanceRole` (AWS's own default name) would otherwise
never receive the grant.

`lib/Common.ps1` gained `Invoke-Retry`, the missing twin of `lib/common.sh`'s `retry`,
needed to absorb IAM eventual consistency after `create-role`. The pre-existing
unretried `ecsTaskExecutionRole` attach now uses it too.

### Open — not addressed here

1. **OCI Terraform modules aren't in the shipped image.** `cloud_database_service.py:59`
   marks `("oracle","oci")` implemented and `k8s_service.py:203` marks `"oci"`
   provisionable, but `Dockerfile:104-124` never COPYs `terraform/db_oci_autonomous` or
   `terraform/k8s_cluster/oci_oke`, and `.dockerignore:24-40` excludes the DB one from the
   build context entirely. Both fail at `terraform._materialize` with "module template not
   found" (`terraform.py:129-135`). `docs/kubernetes.md:24` flags OKE experimental; the OCI
   *database* path carries no caveat at all. **This is the highest-value remaining gap.**
2. **Dead VM modules.** `terraform/ec2_instance`, `terraform/azure_vm`,
   `terraform/gce_instance` are referenced by nothing, shipped in nothing, and untouched
   since their initial commits. `terraform.py:35-37`'s vestigial `_TEMPLATE_DIR` default
   still points at `ec2_instance`. (`docs/infrastructure-as-code.md` claimed VM deploys ran
   Terraform — corrected in this pass.)
3. **`rollback.sh` leaves GCP privilege behind.** `:531-533` revokes 6 of the 16 project
   role bindings granted at `setup-gcp.sh:460-470`, and the **10 grants on Cloud Build's
   default service accounts** (`:532-542`, including project-wide `storage.admin` and
   `compute.admin`) are never revoked. Those SAs are project-owned and survive teardown —
   residual privilege, not just policy bloat.
4. **`terraform/deployments/<job_id>/` is never cleaned** after a successful destroy.
   `_DEPLOYMENTS_DIR` (`k8s_service.py:202`) is only ever created; every `shutil.rmtree`
   in that file targets a runner `tmpdir` instead. `storage_service.py:639` scans that
   same root for local-backend state.
5. **Azure multi-region DB hole.** The MySQL DNS zone and the SQL Server PE subnet + zone
   are read from flat config (`cloud_database_service.py:328`, `:355-356`) while
   `region_config.py:64-75` only carries fields for `db_subnet_id`, `db_mysql_subnet_id`
   and `db_private_dns_zone_id`. A non-default-region MySQL or SQL Server DB gets the
   **default region's** zone/subnet.
6. **`Setup-AzureSandbox.ps1`** omits the optional external image-gallery block
   (`setup-azure.sh:406-479`). Low priority — it's gated behind `AZURE_IMAGE_GALLERY_RG`.
7. **Dead config keys** `aws_k8s_subnet_a_id` / `aws_k8s_subnet_b_id`
   (`config.py:253-254`, already annotated "no longer consumed").

## Test-suite note

`tests/test_cost_service.py` stubs cloud SDKs in `sys.modules`, and four of its tests
already failed whenever the suite ran as one process: sibling test modules replace
`boto3` / `google.cloud.bigquery` in `sys.modules`, and a sibling importing the real
`cost_service` first leaves it bound to the real `aws_service`. Fixed by making the lazy
SDK stubs re-installable and rebinding `cost_service`'s sibling attributes in `_restore()`.
The file now passes identically alone and inside `pytest tests/`.
