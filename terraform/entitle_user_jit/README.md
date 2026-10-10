# Entitle user-JIT Terraform module — Phase 2

Provisions the Entitle side of the user-based JIT authorization
flow described in [`docs/design/entitle-user-jit.md`](../../docs/design/entitle-user-jit.md).
One `terraform apply`:

1. Creates **three workflows**, one per sensitivity tier:
   - `auto_approve`: no human in the loop, up to 1h by default. Used by
     `dashboard-baseline` + every `*-read` group.
   - `single_approver`: one approval from `single_approver_group`, up to 24h.
     Used by every `*-write` group and the per-workgroup membership groups.
   - `two_approver`: two sequential approvals, up to 6h. Used by every
     `*-delete` group and the high-value `dashboard-admin` group.
2. **Adopts each dashboard-\* group's Entitle resource** and sets its tier's
   workflow and allowed durations. Entitle's Entra integration already synced
   each group in as a resource, with the group's object id as its `external_id`;
   `entitle_resource_synced` finds it by that id. Nothing is created in Entra,
   and `terraform destroy` only releases the resources from state.

The workflow sits on the resource because the resource is what users request:
the dashboard's 403 page deep-links straight to it. There are no `entitle_policy`
rules. In Entitle a policy is a *birthright* grant, which gives a group's members
roles with no request at all, so it cannot route requests to a tier.

The Entra group object ids come from the DB rows that
[`bootstrap_entitle_groups.py`](../../web_dashboard/scripts/bootstrap_entitle_groups.py)
populated in Phase 1. The [`bootstrap_entitle_app.py`](../../web_dashboard/scripts/bootstrap_entitle_app.py)
wrapper reads `oauth_group_mappings` and writes a `tfvars` file
before running `terraform apply`.

## Prerequisites

- Phase 1 (`bootstrap_entitle_groups.py`) has been run against the
  target Entra tenant. `oauth_group_mappings` has one row per group.
- An Entitle tenant + an API key with permissions to manage workflows
  and resources.
- The Entra → Entitle integration is configured in the Entitle UI and has
  **synced since Phase 1 ran**, so every dashboard-\* group exists in Entitle.
  Pass the integration's id as `entitle_integration_id`.
- The approver groups exist as Entitle directory groups. Pass their **names**,
  exactly as Entitle shows them; the plan fails unless each matches one group.

## Durations

`*_durations` variables take seconds from Entitle's fixed list (1800, 3600, 10800,
21600, 43200, 57600, 86400, 259200, 604800, ...; -1 is unlimited). The longest value
in each list is also that tier's workflow ceiling.

## Provider version

Pinned to `entitleio/entitle` `>= 3.2.2, < 4.0.0`; `entitle_resource_synced` is not
in every 3.x. CI's `terraform` job runs `terraform validate` on this module against
the provider's real schema. An earlier version of the module was written from the
provider's documentation and never applied; validate showed that almost none of its
attributes existed.

## Run

```bash
# 1. Generate tfvars from the Entra group rows in app DB:
python -m web_dashboard.scripts.bootstrap_entitle_app \
  --output-tfvars terraform/entitle_user_jit/groups.auto.tfvars.json

# 2. Plan + apply:
cd terraform/entitle_user_jit
terraform init
terraform plan  -var "entitle_api_key=$ENTITLE_API_KEY" \
                -var "entitle_integration_id=<entra-integration-id>" \
                -var "single_approver_group=<entitle-group-name>" \
                -var "two_approver_group=<entitle-group-name>"
terraform apply -var "entitle_api_key=$ENTITLE_API_KEY" \
                -var "entitle_integration_id=<entra-integration-id>" \
                -var "single_approver_group=<entitle-group-name>" \
                -var "two_approver_group=<entitle-group-name>"
```

`terraform apply` a second time is a no-op: Terraform state holds the
workflows and the adopted resources, and nothing differs. Tier reassignment is supported via a
single edit to `_tier_for_group()` in the bootstrap script.

## State

State lives wherever the operator points Terraform at — local backend
for dev, remote S3 / Azure Blob for prod. The module makes no
assumption about backend storage; pin one before running `apply` in a
non-throwaway environment.

## See also

- [Phase 2 runbook](../../docs/runbooks/entitle-user-jit-phase-2-bootstrap-entitle.md)
- [Phase 1 runbook (Entra side)](../../docs/runbooks/entitle-user-jit-phase-1-bootstrap-entra.md)
- [Design](../../docs/design/entitle-user-jit.md)
