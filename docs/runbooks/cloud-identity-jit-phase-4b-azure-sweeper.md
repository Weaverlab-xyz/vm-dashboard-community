# Phase 4b — Cloud-identity JIT sweeper (Azure reconciliation)

Validates Phase 4b of the [cloud-identity JIT design](../design/cloud-identity-jit.md) §6.7
("Audit trail + agent-revoke sweeper" — Azure leg).

After Phase 4b deploys, this confirms:

1. With `cloud_identity_azure_enabled=true`, the sweeper now calls
   `sweep_azure()` alongside `sweep_aws()` on every pass.
2. Azure rows whose `expires_at` is in the past or whose Entitle-side
   request shows `revoked` / `expired` are reconciled to local
   `status='revoked'`, the same way AWS rows are.
3. The Azure per-cloud summary carries `self_expiry_trusted=true` and
   the `note` field — telling operators that Azure's role assignments
   self-expire via `endDateTime`, so drift surfaced here is
   informational (Azure has already cleaned up cloud-side).
4. `cloud_identity_azure_enabled=false` keeps the Azure branch
   dormant; only AWS rows are reconciled in that mode (Phase 4a
   behaviour preserved).

Takes ~15 minutes. Run on the community edition
(`c:\Scripts\VM_CLI\vm-dashboard-community\`).

## Prerequisites

- Phase 4a verified — the sweeper foundation + loop + endpoints are
  known-good.
- Master gate ON (`cloud_identity_gate_enabled=true`).
- At least one matrix entry for Azure (e.g. `azure:vm:deploy`).
- A way to create / simulate `entitle_activations` rows with
  `cloud='azure'`. The smoke uses synthetic rows since a real Phase 2
  Azure write path isn't wrapped yet — Phase 2 wraps AWS only.

## Step 1 — Confirm Azure dispatch is wired

```powershell
docker compose exec app python -c "
import inspect
from web_dashboard.services import cloud_identity_sweeper_service as ci
src = inspect.getsource(ci.sweep_once)
assert 'sweep_azure' in src, 'azure dispatch missing'
assert 'cloud_identity_azure_enabled' in src, 'flag check missing'
print('OK sweep_once dispatches to sweep_azure when flag set')
"
```

**Expected:** `OK sweep_once dispatches to sweep_azure when flag set`.

## Step 2 — Sweep with Azure disabled (4a parity check)

Ensure the Azure flag is off:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
INSERT INTO app_config (key, value, workgroup, updated_at)
VALUES ('cloud_identity_aws_enabled', '1', NULL, NOW()),
       ('cloud_identity_azure_enabled', '0', NULL, NOW())
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW();"
docker compose exec app python -c "from web_dashboard.services import config_service; config_service.invalidate()"
```

Force a sweep:

```powershell
$admin = Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/auth/login -Body @{username='admin'; password='<pw>'}
$h = @{Authorization="Bearer $($admin.access_token)"}
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/cloud-identity/sweep -Headers $h | ConvertTo-Json -Depth 5
```

**Expected:** `by_cloud` contains `aws` only. No `azure` key. This
preserves Phase 4a's behaviour when the Azure leg hasn't been promoted
yet.

## Step 3 — Flip the Azure flag on

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
UPDATE app_config SET value='1' WHERE key='cloud_identity_azure_enabled';"
docker compose exec app python -c "from web_dashboard.services import config_service; config_service.invalidate()"
```

Force a sweep. **Expected:** `by_cloud.azure` now appears, with
`processed=0` (no Azure rows yet), `self_expiry_trusted=true`, and
the `note` field about Azure self-expiry.

## Step 4 — Reconcile a synthetic past-TTL Azure row

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
INSERT INTO entitle_activations
  (id, cloud, operation, status, payload_hash, requested_at, granted_at, expires_at, entitle_request_id, auto_approved)
VALUES
  ('phase4b-synth-1', 'azure', 'azure:vm:deploy', 'granted',
   'feedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedface',
   NOW() - INTERVAL '2 hours', NOW() - INTERVAL '90 minutes',
   NOW() - INTERVAL '30 minutes',
   'azure-synth-req-1', true);"
```

Force a sweep. **Expected:** under `by_cloud.azure`,
`reconciled_past_ttl=1`, no orphan in the top-level list.

Verify:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
SELECT id, status, revoked_at, denial_reason FROM entitle_activations WHERE id = 'phase4b-synth-1';"
```

**Expected:** `status='revoked'`, `revoked_at` set, `denial_reason`
includes `sweeper: past local TTL`. Same reconciliation path as the
AWS Phase 4a Step 3, scoped to `cloud='azure'`.

## Step 5 — Synthetic Azure-404 orphan

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
INSERT INTO entitle_activations
  (id, cloud, operation, status, payload_hash, requested_at, granted_at, expires_at, entitle_request_id, auto_approved)
VALUES
  ('phase4b-synth-2', 'azure', 'azure:vm:deploy', 'granted',
   'beadbeefbeadbeefbeadbeefbeadbeefbeadbeefbeadbeefbeadbeefbeadbeef',
   NOW() - INTERVAL '5 minutes', NOW() - INTERVAL '4 minutes',
   NOW() + INTERVAL '55 minutes',
   'azure-nonexistent-req-99999', true);"
```

Force a sweep. **Expected:** under `by_cloud.azure`,
`reconciled_failed=1`, an orphan with `kind='entitle_unknown_request'`
and `cloud='azure'` in the top-level orphans list.

Row state: `status='failed'`, `denial_reason` includes the 404 note.

## Step 6 — Real Entitle drift (optional, needs real Entitle tenant)

If you have a real Entitle tenant configured to issue Azure role
assignments:

1. Create a real Azure row via direct service call (no Phase 2 Azure
   wrapping yet) — pick an existing Azure-managed grant Entitle has
   issued and insert a row pointing at its `entitle_request_id`.
2. Manually revoke the request in the Entitle console.
3. Force a sweep.

**Expected:** the row flips to `revoked`, `denial_reason` includes
`sweeper: entitle status=revoked`. Same shape as AWS Phase 4a Step 4.

## Step 7 — `self_expiry_trusted` flag is in the summary

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/cloud-identity/sweep -Headers $h | ConvertTo-Json -Depth 5
```

**Expected:** the `by_cloud.azure` object includes:
- `self_expiry_trusted: true`
- `note: "Azure role assignments self-expire via endDateTime; this sweep reconciles dashboard rows against Entitle's view only. Drift here is informational."`

These two fields are how a future admin UI distinguishes Azure orphans
(treat as informational, Azure has self-cleaned) from AWS orphans
(treat as actionable, the agent may have failed to revoke).

## Step 8 — Cached orphans endpoint includes Azure

```powershell
Invoke-RestMethod -Uri http://localhost:8000/api/cloud-identity/orphans -Headers $h | ConvertTo-Json -Depth 5
```

**Expected:** same payload as Step 7. The `by_cloud.azure` block is
in the cached `cloud_identity_last_sweep` blob in `app_config`.

## Step 9 — Clean up synthetic rows

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
DELETE FROM entitle_activations WHERE id LIKE 'phase4b-synth-%';"
```

Re-run `POST /api/cloud-identity/sweep`. **Expected:** `by_cloud.azure`
shows `processed=0`, no orphans, `self_expiry_trusted=true`.

## Step 10 — Where this fits

Phase 4b adds Azure reconciliation on top of Phase 4a's foundation.
The two share `_sweep_one_cloud()`, so changes to the reconciliation
state machine (kinds of orphans recognized, denial-reason format)
flow to both clouds automatically.

Azure-specific extensions deferred:
- **ARM `role_assignments` cross-check.** Would need
  `azure-mgmt-authorization` dep + the synthetic machine identity's
  principal id. Catches the rare case where Entitle thinks the grant
  is active but Azure has already revoked the role. Adds risk for
  marginal coverage; revisit if the AWS or GCP legs prove the cross-
  check pattern is high-yield.

Phase 4c is GCP — same shape as AWS (agent-driven revocation per §5.3
/ §6.7), so the wrapper will mirror `sweep_aws()` structure rather
than `sweep_azure()`.

## Rollback

1. `UPDATE app_config SET value='0' WHERE key='cloud_identity_azure_enabled';`
   silences the Azure leg without touching AWS or the master gate.
   The next sweep skips Azure entirely.
2. Code rollback: revert the Phase 4b commit. The dispatch becomes
   AWS-only and `sweep_azure()` disappears. Existing Azure rows in
   `entitle_activations` are unaffected.
3. Any rows flipped to `revoked` / `failed` by 4b reconciliation stay
   that way; flipping the flag off doesn't un-reconcile (the local
   row already reflects what the cloud + Entitle know).
