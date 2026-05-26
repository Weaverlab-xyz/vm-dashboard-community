# Phase 2 — Entitle virtual application provisioning

Operator walk-through for the `terraform/entitle_user_jit/` module +
`web_dashboard/scripts/bootstrap_entitle_app.py` wrapper. Validates
Phase 2 of the [Entitle user-JIT design](../design/entitle-user-jit.md).

After Phase 2 deploys, this confirms:

1. `terraform plan` against a clean Entitle tenant prints the expected
   shape: 3 workflows, N resources (one per dashboard-`*` Entra group
   Phase 1 created), 3 policies, 1 bundle.
2. `terraform apply` provisions them; a second `terraform apply` is a
   no-op.
3. In the Entitle UI, the **VM Dashboard** virtual application appears
   with every dashboard-`*` resource listed and routed through its
   tier-appropriate workflow.
4. The Phase 1 Entra group ids in `oauth_group_mappings` flow cleanly
   into each `entitle_resource.directory_group_id`.

Takes ~30 minutes including the bootstrap script + terraform apply.
Run against a real Entitle tenant. Phase 3's E2E loop builds on top.

## Prerequisites

- Phase 1 (`bootstrap_entitle_groups.py`) has been run against the
  target Entra tenant. `oauth_group_mappings` has one row per group.
  Confirm via:
  ```powershell
  docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
  SELECT display_name, entra_group_id FROM oauth_group_mappings
  WHERE display_name LIKE 'dashboard-%' ORDER BY display_name;"
  ```
- An Entitle tenant + API key with permissions to create
  integrations, workflows, policies, resources, and bundles.
- An existing Entra → Entitle directory integration. Created once
  via the Entitle UI; capture its id.
- Two approver identifiers (group names or user emails) — one for the
  single-approver tier, one for the two-approver tier.
- Terraform ≥ 1.5 installed on the runner.

## Step 1 — Generate the groups tfvars file

```powershell
docker compose exec app python -m web_dashboard.scripts.bootstrap_entitle_app `
  --output-tfvars /app/terraform/entitle_user_jit/groups.auto.tfvars.json
```

**Expected:**
- `Wrote N groups -> /app/terraform/entitle_user_jit/groups.auto.tfvars.json`
  where N is the dashboard-`*` row count from the Phase 1 prereq query.
- The file contains one entry per group with `display_name`,
  `directory_group_id`, `description`, and `tier` (one of
  `auto_approve` / `single_approver` / `two_approver`).
- Re-running overwrites the file deterministically.

If N is 0, stop — Phase 1 hasn't been run, or the groups table is
empty. Re-run Phase 1's bootstrap before continuing.

## Step 2 — Inspect the tier assignments

```powershell
cat terraform/entitle_user_jit/groups.auto.tfvars.json | jq '.groups | to_entries | map({key, tier:.value.tier}) | group_by(.tier) | map({tier:.[0].tier, count:length})'
```

**Expected** (default 28 prod-seeded groups):
- `auto_approve` ≈ 9 (baseline + 8 `*-read`)
- `single_approver` ≈ 10 (8 `*-write` + 2 workgroup-`*`)
- `two_approver` ≈ 9 (admin + 8 `*-delete`)

Exact counts vary with `PERMISSION_SCOPES`/`PERMISSION_LEVELS` and the
number of workgroups. If a row landed in the wrong tier, edit
`_tier_for_group()` in `bootstrap_entitle_app.py` and re-run Step 1.

## Step 3 — `terraform init`

```powershell
cd terraform/entitle_user_jit
$env:TF_VAR_entitle_api_key = '<your-entitle-api-key>'
terraform init
```

**Expected:** Terraform downloads the `beyondtrust/entitle` provider
and initialises the working directory. If the registry resolution
fails, double-check the provider source string in `versions.tf`
matches your tenant's published provider — BeyondTrust occasionally
relocates the registry namespace.

## Step 4 — `terraform plan` (dry run)

```powershell
terraform plan `
  -var "entitle_integration_id=<entra-integration-id>" `
  -var "single_approver_group=<approver-identifier>" `
  -var "two_approver_group=<two-approver-identifier>"
```

**Expected:** plan shows:
- **3 to add** for `entitle_workflow` (auto_approve / single_approver /
  two_approver).
- **N to add** for `entitle_resource.dashboard_group["…"]` where N
  matches Step 1's count.
- **3 to add** for `entitle_policy` (one per tier).
- **1 to add** for `entitle_bundle.vm_dashboard`.
- **0 to change, 0 to destroy.**

If `terraform plan` errors on a schema attribute (e.g.
`approval_steps.count` not recognized), the deployed provider has
diverged from the public docs. Update the affected `.tf` file
locally; both `workflows.tf` and `resources.tf` have comments
flagging the lines most likely to need adjustment.

## Step 5 — `terraform apply`

```powershell
terraform apply `
  -var "entitle_integration_id=<entra-integration-id>" `
  -var "single_approver_group=<approver-identifier>" `
  -var "two_approver_group=<two-approver-identifier>"
```

(or via the wrapper: `docker compose exec app python -m web_dashboard.scripts.bootstrap_entitle_app --apply --entitle-integration-id … --single-approver-group … --two-approver-group …` — the wrapper threads vars through `TF_VAR_*` env so the API key never lands in shell history.)

**Expected:** apply completes cleanly. Outputs include:
- `application_id` — the bundle's id, surface this in the dashboard's
  `Settings → Integrations → Entitle` panel for Phase 4's
  "Request access" deep links.
- `workflow_ids` — three-entry map.
- `resource_ids` — N-entry map (`dashboard-* → entitle resource id`).
- `resource_count` — N.

## Step 6 — Spot-check in the Entitle UI

Sign into your Entitle tenant's web console. Confirm:

1. **Catalog → Applications** lists "VM Dashboard" with the
   description from `application_description`.
2. Opening the application shows N resources, each named after the
   matching `dashboard-*` Entra group.
3. **Workflows** page lists the three new workflows with the names
   `vm-dashboard-auto-approve`, `vm-dashboard-single-approver`,
   `vm-dashboard-two-approver`.
4. Opening one of the `*-write` resources confirms its
   `Workflow` field points at `vm-dashboard-single-approver`; a
   `*-read` resource points at `vm-dashboard-auto-approve`; an
   `*-delete` or `admin` resource points at `vm-dashboard-two-approver`.

## Step 7 — Idempotency

```powershell
terraform apply -var "entitle_integration_id=<…>" -var "single_approver_group=<…>" -var "two_approver_group=<…>"
```

**Expected:** `No changes. Your infrastructure matches the configuration.`

This is the load-bearing property — operators can re-run the bootstrap
script + apply as part of every deployment without risk. Tier
reassignment is a single edit to `_tier_for_group()` followed by a
re-run; Terraform diffs the per-resource `workflow_id` and only
updates the affected entries.

## Step 8 — Add a new dashboard-* group end-to-end

To prove the Phase 1 → Phase 2 chain works:

1. Add a new permission scope to `web_dashboard/api/auth.py:PERMISSION_SCOPES`.
2. Re-run Phase 1: `python -m web_dashboard.scripts.bootstrap_entitle_groups --scope=permissions --yes`.
3. Re-run Phase 2 Step 1 + Step 5 (apply).

**Expected:** Step 5 reports `~ X to change` (for the bundle's
`resource_ids` list) and `+ 3 to add` (the three new
`*-read/-write/-delete` resources). The Entitle UI's VM Dashboard
catalog entry now shows the new resources.

## Step 9 — Where this fits

Phase 2 ships the **server-side configuration** in Entitle. Phase 3 is
the **end-to-end request flow** — operator A opens Entitle, picks a
`dashboard-aws-read` resource, gets auto-approved (because that
resource's workflow is `vm-dashboard-auto-approve`), and the next
dashboard login sees the new permission. The Phase 3 runbook captures
that loop end-to-end against the Entitle tenant configured here.

Phase 4 (UI affordances) consumes the resource IDs that the
`resource_ids` output surfaces — the dashboard's 403 page deep-links
to the matching resource so users can request access in one click.

## Rollback

1. `terraform destroy` from the `terraform/entitle_user_jit` directory
   removes every entity provisioned by this module. The Entra groups
   themselves are untouched — only the Entitle-side wrapper goes
   away. Phase 1's groups stay intact for a future re-bootstrap.
2. For partial rollback (e.g. one bad tier assignment), edit
   `_tier_for_group()` and re-run the bootstrap script + apply. The
   provider's diffing handles the policy/workflow swap.
3. If `terraform state` gets out of sync with the live Entitle tenant
   (operator deleted a resource via the UI), `terraform refresh`
   followed by another `apply` reconciles. The wrapper's tfvars file
   stays canonical for the desired-state inputs.
