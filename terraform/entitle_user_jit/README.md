# Entitle user-JIT Terraform module — Phase 2

Provisions the Entitle side of the user-based JIT authorization
flow described in [`docs/design/entitle-user-jit.md`](../../docs/design/entitle-user-jit.md).
One `terraform apply` creates:

1. The **VM Dashboard** virtual application bundling every
   dashboard-`*` Entra group as a grantable Entitle resource.
2. **Three workflows** — one per sensitivity tier:
   - `auto_approve` — ≤2h TTL, no human in the loop. Used by
     `dashboard-baseline` + every `*-read` group.
   - `single_approver` — ≤24h TTL, one approver. Used by every
     `*-write` group and the per-workgroup membership groups.
   - `two_approver` — ≤8h TTL, two approvers. Used by every
     `*-delete` group and the high-value `dashboard-admin` group.
3. **Policy rules** routing each resource to its tier.

The Entra group object ids feeding `entitle_resource` come from the
DB rows that [`bootstrap_entitle_groups.py`](../../web_dashboard/scripts/bootstrap_entitle_groups.py)
populated in Phase 1. The [`bootstrap_entitle_app.py`](../../web_dashboard/scripts/bootstrap_entitle_app.py)
wrapper reads `oauth_group_mappings` and writes a `tfvars` file
before running `terraform apply`.

## Prerequisites

- Phase 1 (`bootstrap_entitle_groups.py`) has been run against the
  target Entra tenant. `oauth_group_mappings` has one row per group.
- An Entitle tenant + an API key with permissions to create
  integrations, workflows, policies, and resources.
- The Entra → Entitle directory integration is already configured
  in the Entitle UI. Pass its id via `entitle_integration_id`.
- Approver groups (single-approver and two-approver tiers) exist
  in your IdP and have stable identifiers Entitle can resolve.

## Provider schema notes

The [`entitle-terraform-provider`](https://docs.beyondtrust.com/entitle/docs/entitle-terraform-provider)
exposes `entitle_integration`, `entitle_workflow`, `entitle_policy`,
`entitle_resource`, `entitle_role`, `entitle_bundle`,
`entitle_permission`. Attribute names below are based on the public
provider documentation as of the design's v2.2 update. If the
provider has rolled forward, the module's first `terraform plan`
flags any schema drift loudly — adjust the affected files locally
before `terraform apply`.

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
                -var "single_approver_group=<group-or-user>" \
                -var "two_approver_group=<group-or-user>"
terraform apply -var "entitle_api_key=$ENTITLE_API_KEY" \
                -var "entitle_integration_id=<entra-integration-id>" \
                -var "single_approver_group=<group-or-user>" \
                -var "two_approver_group=<group-or-user>"
```

`terraform apply` a second time is a no-op — Entitle resources are
identified by name, so re-running matches existing entities and
no-ops on unchanged attributes. Tier reassignment is supported via a
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
