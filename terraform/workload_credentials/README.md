# Workload Credentials Terraform module

Provisions the **BeyondTrust side** of the dynamic-credential tier described in
[`docs/integrations/workload-credentials.md`](../../docs/integrations/workload-credentials.md),
plus the AWS IAM trust chain it needs. One `terraform apply` creates:

1. A **folder** for the dashboard's dynamic secrets.
2. **AWS** — an integration role the BeyondTrust bridge assumes, a target role carrying
   the dashboard's existing permissions, the Workload Credentials integration, and one or
   two dynamic secrets over that role.
3. **Azure** (optional) — the Workload Credentials integration, the dynamic secret, and
   the app-registration ownership that lets the integration mint onto the target.

It exists because the alternative is clicking through a console and getting several
fiddly details right by hand — the external ID, `sts:TagSession`, `MaxSessionDuration`,
and an Object ID that is easy to confuse with a client ID. Each of those fails in a way
that does not name the field.

## What it deliberately does not do

**It does not contain a copy of the dashboard's IAM policy.**
[`scripts/sandbox/Linux/setup-aws.sh`](../../scripts/sandbox/Linux/setup-aws.sh) already
builds `dashboard-app-policy` from every code path in `aws_service.py`. This module looks
it up and attaches it. A second copy here would drift from that one the first time either
changed, and divergent duplicate definitions are the failure this repo hits most often.

Same reasoning on Azure: the module creates no role assignments. The dashboard's service
principal already has the grants, courtesy of `setup-azure.sh`. Point that setup at the
target app and the grants come with it.

**It does not create the Azure integration app registration.** That app's client secret is
the root of the whole Azure path, and creating it here would write it into Terraform
state. Create it once by hand; pass its details in. (The BeyondTrust resource treats
`client_secret` as write-only, so *that* copy never reaches state.)

## Prerequisites

- **Terraform ≥ 1.11**, run from your own machine. The BeyondTrust provider uses a
  write-only attribute, which is a 1.11 feature; on an older CLI it fails to load rather
  than degrading.

  Note this is **newer than the dashboard image's own terraform**, which is pinned to
  1.10.5 (`Dockerfile`, `ARG TERRAFORM_VERSION`). So you cannot run this module from
  inside the container, even though terraform is already there — the obvious shortcut
  fails on the version gate. Nothing else is affected: the dashboard never invokes this
  module, only the deploy modules under `terraform/ec2_instance`, `terraform/k8s_cluster`
  and so on, none of which need 1.11.
- A **US-region Pathfinder site** with Workload Credentials enabled.
- AWS credentials able to create IAM roles and read the managed policy.
- `dashboard-app-policy` present, or an explicit `aws_dashboard_policy_arn`.

## Usage

```bash
export BEYONDTRUST_ACCESS_TOKEN='<PAT minted with the target site selected>'
export BEYONDTRUST_SITE_ID='<site GUID>'

terraform -chdir=terraform/workload_credentials init
terraform -chdir=terraform/workload_credentials apply
```

> **Mint the PAT with the target site selected.** A PAT is scoped to whichever site was
> active when it was created and cannot be widened. Minting one under the Pathfinder admin
> tenant produces `401 Access denied for this site` on every call — which reads like the
> site is missing the application rather than a token-scoping problem.

Azure as well:

```bash
terraform -chdir=terraform/workload_credentials apply \
  -var enable_azure=true \
  -var azure_tenant_id=... \
  -var azure_integration_client_id=... \
  -var azure_integration_sp_object_id=... \
  -var azure_target_application_object_id=...
```

`azure_integration_client_secret` is best passed as `TF_VAR_azure_integration_client_secret`
so it stays out of shell history.

## The AWS trust chain

```
BeyondTrust bridge role                    (theirs — arn:aws:iam::071882751376:role/…)
   └─ assumes → integration role           (ours; trust + sts:ExternalId condition)
        └─ assumes → target role           (ours; sts:AssumeRole AND sts:TagSession)
                        └─ dashboard-app-policy
```

Two details that fail quietly if you build this by hand:

- **`sts:TagSession`, not just `sts:AssumeRole`.** The dynamic secret's `aws_tags` become
  STS session tags — which is what gives CloudTrail attribution per issuing secret — and
  a tagged assume is *refused* without it rather than silently untagged.
- **`MaxSessionDuration` caps the TTL.** AWS defaults it to 3600 and clamps a longer
  request with no error. A lease shorter than you configured looks like a dashboard bug.
  The module sets it explicitly (4 hours by default) and outputs the value.

## The two dynamic secrets

Both point at the **same role** and differ only by session policy, because session
policies intersect and never broaden.

| Secret | Used for | Session policy |
|---|---|---|
| `dashboard-provision` | inside a job | none — the role's full permissions |
| `dashboard-everyday` | everything else | allow `*`, then explicitly deny IAM and `sts:AssumeRole` |

The everyday policy is written as allow-everything-then-deny rather than an allow-list.
`Allow *` grants nothing the role does not already have — it just declines to narrow — and
`Deny` always wins, so the deny block removes precisely the escalation paths and nothing
else. An allow-list would have to enumerate every action the dashboard uses on its request
path, and would break a page silently the day someone added a service call.

**It is not read-only**, despite the dashboard's setting still being named
`wlc_aws_readonly_secret_name`. The dashboard writes on the request path too — editing a
secret, uploading to storage, re-tagging a VM, starting a container task, around fourteen
sites. A strictly read-only policy breaks the Secrets and Storage pages.

Set `create_everyday_secret = false` to run with one lease for everything, which is the
dashboard's default behaviour and changes nothing about how it works today.

## Costs

Dynamic credential issuances are **metered**; static secrets are not. The defaults here
are chosen accordingly — a one-hour TTL renewed at 50% means roughly 48 issuances a day
per lease, where a 15-minute TTL would mean nearly 200. AWS credentials cannot be revoked
early, so a short TTL buys no security to offset that.

## After applying

The `next_steps` output lists the dashboard-side configuration. The one check worth doing
carefully: confirm **one** lease row is shared across both web workers and `dash-worker`
rather than three. That is what the durable lease table exists for, and it is the one
property the test suite cannot assert.

## Doing the Azure ownership by hand instead

`azuread_application_owner` makes the integration service principal an owner of the target
app, which is what lets it add and remove passwords with no tenant-wide Graph permission.
The module pins `azuread >= 3.0` because that is the version whose argument shape was
verified.

If you would rather not grant Terraform directory write access, set
`azure_manage_target_ownership = false` and run it yourself. Note the Portal cannot do
this — its owners picker accepts users but not service principals — so the CLI is the only
manual route:

```bash
az ad app owner add --id "<target-app-object-id>" --owner-object-id "<integration-sp-object-id>"
```
