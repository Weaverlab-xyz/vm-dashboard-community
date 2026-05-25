# Phase 1 — Entitle user-JIT bootstrap (Entra groups)

Operator walk-through for `web_dashboard/scripts/bootstrap_entitle_groups.py`.
Validates Phase 1 of the [Entitle user-JIT design](../design/entitle-user-jit.md).

After Phase 1 runs, the operator's Entra tenant has the security
groups that back the user-JIT flow, and the dashboard's
`oauth_group_mappings` table has one row per provisioned group.
Phase 2 (Entitle virtual application) then points Entitle at these
group object ids.

Takes ~15 minutes once the prerequisite app registration is in
place. Run against a real Entra tenant (community / dev installs
without Entra skip this phase entirely).

## Prerequisites

- An **Entra app registration** dedicated to this bootstrap (do
  NOT reuse the OAuth-login app registration — that one only has
  user-login permissions). The bootstrap app needs:
  - **API permission**: `Microsoft Graph → Application → Group.ReadWrite.All`
  - **Admin consent granted** for the above
  - A **client secret** (24h+ lifetime; store in Key Vault for prod)
- The dashboard's PostgreSQL DB reachable from wherever you run
  the script (the script imports `web_dashboard.database.SessionLocal`,
  so it honours `DATABASE_URL` / `Start-DevEnvironment.ps1`
  environment).
- `settings.azure_oauth_tenant_id` populated (or pass `--tenant-id`
  explicitly).

## Step 1 — Dry-run plan against the target tenant

```powershell
docker compose exec app python -m web_dashboard.scripts.bootstrap_entitle_groups `
  --client-id $env:BOOTSTRAP_CLIENT_ID `
  --client-secret $env:BOOTSTRAP_CLIENT_SECRET `
  --dry-run
```

**Expected:** the script prints `Planned inventory: N groups` (28
on a default prod seed: 1 admin + 1 baseline + 24 scope/levels + 2
workgroups), authenticates against Graph to read existing groups,
then prints a table showing `create` for every row (assuming
nothing pre-exists) and `created` for every mapping. No Graph
writes occur and no DB rows are inserted.

If the count looks wrong: open `web_dashboard/api/auth.py` and
verify `PERMISSION_SCOPES` × `PERMISSION_LEVELS` is what you
expect. Adding a scope there automatically adds its three groups
on the next bootstrap run.

If Graph rejects authentication, fix the app registration before
proceeding — there is no "skip Graph" mode.

## Step 2 — Apply for real (full inventory)

```powershell
docker compose exec app python -m web_dashboard.scripts.bootstrap_entitle_groups `
  --client-id $env:BOOTSTRAP_CLIENT_ID `
  --client-secret $env:BOOTSTRAP_CLIENT_SECRET `
  --yes
```

**Expected:**
- Every row of the table shows `created` under `action`.
- Final summary line: `Groups: <N> created, 0 already existed.
  Mappings: <N> new (0 already mapped).`

If a Graph 4xx fires mid-run, the DB transaction has not yet
committed (commit only happens at the end of the block) so no
half-state remains. Investigate and re-run.

## Step 3 — Verify idempotency

```powershell
docker compose exec app python -m web_dashboard.scripts.bootstrap_entitle_groups `
  --client-id $env:BOOTSTRAP_CLIENT_ID `
  --client-secret $env:BOOTSTRAP_CLIENT_SECRET `
  --yes
```

**Expected:**
- Every row now shows `exists` (group) + `exists` (mapping).
- Summary: `Groups: 0 created, <N> already existed. Mappings: 0
  new (<N> already mapped).`

This is the load-bearing property — operators can re-run the
script as part of every deployment without risk.

## Step 4 — Spot-check the DB rows

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard `
  -c "SELECT display_name, workgroup, default_permissions FROM oauth_group_mappings ORDER BY display_name;"
```

**Expected:**
- One `dashboard-admin` row with `default_permissions = {"is_admin": true}`.
- One `dashboard-baseline` row.
- One row per scope/level tuple with the matching JSON.
- One `dashboard-workgroup-<wg>` row per configured workgroup,
  with `default_permissions = NULL` and `workgroup = <wg>`.

## Step 5 — Spot-check the Entra side

In the Azure portal → Entra ID → Groups, filter on the prefix
`dashboard-`. Confirm every group exists as a security group
(not Microsoft 365 / mail-enabled).

For a per-tenant prod run (Phase 5 — not Phase 1's scope), the
prefix is `dashboard-<tenant-slug>-`; the script's `--tenant-prefix`
flag adds it.

## Step 6 — Smoke the resolver end-to-end

Pick any one provisioned group (e.g. `dashboard-vms-read`). In
Entra, add a test user to it. Sign that user into the dashboard
via "Sign in with Microsoft".

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard `
  -c "SELECT username, session_permissions FROM users WHERE email = '<test-user-email>';"
```

**Expected:** `session_permissions` includes `{"vms": ["read"]}`.

Remove the user from the Entra group, sign out + sign back in,
re-query: `vms:["read"]` is no longer present. This is Phase 0's
resolver doing its job — Phase 1's contribution is that the group
the resolver matches against now actually exists in Entra.

## Step 7 — Partial scope re-runs

For day-2 operations (a new workgroup added, a new scope appearing
in `PERMISSION_SCOPES`):

```powershell
# Just sync workgroup groups (after creating a new workgroup):
... --scope=workgroups --yes

# Just sync permission tuples (after adding a new scope to api/auth.py):
... --scope=permissions --yes
```

The other scopes are untouched — re-running with `--scope=admin`
won't disturb `dashboard-vms-read`, and vice versa.

## Step 8 — Operator-edited mappings are preserved

If an operator hand-edited a mapping in the `/groups` admin UI
(e.g. changed `dashboard-baseline`'s `default_permissions` to add
`images:["read"]`), re-running the bootstrap does NOT overwrite
the edit. The upsert matches on `entra_group_id`; once a row
exists, the script logs `exists` for the mapping and moves on.

To force a reset, delete the row in `/groups` first, then re-run
the bootstrap.

## Step 9 — Rollback

If something goes wrong:

1. **DB-side:** the script commits at the end of the run. If you
   want to undo, run:
   ```sql
   DELETE FROM oauth_group_mappings WHERE display_name LIKE 'dashboard-%';
   ```
   (or scope further by `entra_group_id IN (...)`).
2. **Entra-side:** in the Entra portal, multi-select the
   `dashboard-*` groups + Delete. There is no group-id-by-prefix
   delete in Graph that's safer than the portal multi-select.
3. The dashboard remains functional throughout — empty
   `oauth_group_mappings` falls back to the
   `settings.azure_oauth_group_map` env-var path, which is the
   pre-Phase-1 behaviour.

## Step 10 — Where this fits

Phase 1 is the **provisioning** step. The grant *flow* (Alice
requests a group, Entitle approves, Entra adds her to the group,
dashboard sees it on next login) needs Phase 2 (the Entitle
virtual application Terraform module) before it can fire
end-to-end. Phase 3 is the E2E test against the full Entitle
loop.

Phase 5 (multi-tenancy) layers per-tenant prefixes on top of this
script via `--tenant-prefix` — already wired here so the same
script services both single-tenant community and per-tenant prod
when MT phases 4-5 land.
