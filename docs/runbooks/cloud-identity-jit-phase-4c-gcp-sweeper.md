# Phase 4c — Cloud-identity JIT sweeper (GCP reconciliation)

Validates Phase 4c of the [cloud-identity JIT design](../design/cloud-identity-jit.md) §6.7
("Audit trail + agent-revoke sweeper" — GCP leg).

After Phase 4c deploys, this confirms:

1. With `cloud_identity_gcp_enabled=true`, the sweeper calls
   `sweep_gcp()` alongside `sweep_aws()` and (when enabled)
   `sweep_azure()` on every pass.
2. GCP rows whose `expires_at` is past or whose Entitle-side request
   shows `revoked` / `expired` are reconciled to local
   `status='revoked'` — same reconciliation pattern as AWS.
3. Drift here is **actionable**, not informational: GCP uses
   agent-driven `setIamPolicy` (per design §5.3), so an orphan means
   the agent may have failed to revoke and an operator needs to
   intervene. The summary does **not** carry `self_expiry_trusted`
   (unlike Azure).
4. `cloud_identity_gcp_enabled=false` keeps the GCP branch dormant;
   AWS + Azure passes continue independently.

Takes ~15 minutes. Run on the community edition
(`c:\Scripts\VM_CLI\vm-dashboard-community\`).

## Prerequisites

- Phase 4a + 4b verified — the sweeper foundation + AWS + Azure legs
  are known-good.
- Master gate ON (`cloud_identity_gate_enabled=true`).
- At least one matrix entry for GCP (e.g. `gcp:compute:deploy`).
- A way to create / simulate `entitle_activations` rows with
  `cloud='gcp'`. The smoke uses synthetic rows since a real Phase 2
  GCP write path isn't wrapped yet — Phase 2 wraps AWS only.

## Step 1 — Confirm GCP dispatch is wired

```powershell
docker compose exec app python -c "
import inspect
from web_dashboard.services import cloud_identity_sweeper_service as ci
src = inspect.getsource(ci.sweep_once)
assert 'sweep_gcp' in src, 'gcp dispatch missing'
assert 'cloud_identity_gcp_enabled' in src, 'flag check missing'
print('OK sweep_once dispatches to sweep_gcp when flag set')
"
```

**Expected:** `OK sweep_once dispatches to sweep_gcp when flag set`.

## Step 2 — Sweep with GCP disabled (4a + 4b parity check)

Ensure the GCP flag is off:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
INSERT INTO app_config (key, value, workgroup, updated_at)
VALUES ('cloud_identity_aws_enabled', '1', NULL, NOW()),
       ('cloud_identity_azure_enabled', '1', NULL, NOW()),
       ('cloud_identity_gcp_enabled', '0', NULL, NOW())
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW();"
docker compose exec app python -c "from web_dashboard.services import config_service; config_service.invalidate()"
```

Force a sweep:

```powershell
$admin = Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/auth/login -Body @{username='admin'; password='<pw>'}
$h = @{Authorization="Bearer $($admin.access_token)"}
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/cloud-identity/sweep -Headers $h | ConvertTo-Json -Depth 5
```

**Expected:** `by_cloud` contains `aws` and `azure` but **not** `gcp`.
This preserves Phase 4a + 4b behaviour when GCP hasn't been promoted
yet.

## Step 3 — Flip the GCP flag on

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
UPDATE app_config SET value='1' WHERE key='cloud_identity_gcp_enabled';"
docker compose exec app python -c "from web_dashboard.services import config_service; config_service.invalidate()"
```

Force a sweep. **Expected:** `by_cloud.gcp` now appears, with
`processed=0` (no GCP rows yet). Note **no** `self_expiry_trusted`
field — GCP drift is actionable.

## Step 4 — Reconcile a synthetic past-TTL GCP row

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
INSERT INTO entitle_activations
  (id, cloud, operation, status, payload_hash, requested_at, granted_at, expires_at, entitle_request_id, auto_approved)
VALUES
  ('phase4c-synth-1', 'gcp', 'gcp:compute:deploy', 'granted',
   'c0ffeec0ffeec0ffeec0ffeec0ffeec0ffeec0ffeec0ffeec0ffeec0ffeec0ff',
   NOW() - INTERVAL '2 hours', NOW() - INTERVAL '90 minutes',
   NOW() - INTERVAL '30 minutes',
   'gcp-synth-req-1', true);"
```

Force a sweep. **Expected:** under `by_cloud.gcp`,
`reconciled_past_ttl=1`. Verify:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
SELECT id, status, revoked_at, denial_reason FROM entitle_activations WHERE id = 'phase4c-synth-1';"
```

**Expected:** `status='revoked'`, `denial_reason` includes
`sweeper: past local TTL`.

## Step 5 — Synthetic GCP-404 orphan

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
INSERT INTO entitle_activations
  (id, cloud, operation, status, payload_hash, requested_at, granted_at, expires_at, entitle_request_id, auto_approved)
VALUES
  ('phase4c-synth-2', 'gcp', 'gcp:compute:deploy', 'granted',
   'badc0ffeebadc0ffeebadc0ffeebadc0ffeebadc0ffeebadc0ffeebadc0ffeed',
   NOW() - INTERVAL '5 minutes', NOW() - INTERVAL '4 minutes',
   NOW() + INTERVAL '55 minutes',
   'gcp-nonexistent-req-77777', true);"
```

Force a sweep. **Expected:** under `by_cloud.gcp`,
`reconciled_failed=1`, an orphan in the top-level list with
`cloud='gcp'`, `kind='entitle_unknown_request'`.

**Important:** unlike Azure, a GCP orphan here is **actionable**.
Agent-driven revoke means the GCP IAM policy might still carry the
binding even though Entitle has forgotten about it. The operator
should check the GCP project's `getIamPolicy` for stale bindings on
the synthetic machine identity's service-account email and remove
them manually.

## Step 6 — All three clouds in one pass

With AWS + Azure + GCP all enabled, force a sweep.

**Expected:** `by_cloud` contains all three keys (`aws`, `azure`,
`gcp`). Independent failure handling — a misconfigured Entitle
endpoint on one cloud's poll path doesn't kill the other two.

## Step 7 — Cached orphans endpoint includes GCP

```powershell
Invoke-RestMethod -Uri http://localhost:8000/api/cloud-identity/orphans -Headers $h | ConvertTo-Json -Depth 5
```

**Expected:** same payload as Step 6. `by_cloud.gcp` block is in the
cached `cloud_identity_last_sweep` blob.

## Step 8 — Clean up synthetic rows

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
DELETE FROM entitle_activations WHERE id LIKE 'phase4c-synth-%';"
```

Re-run `POST /api/cloud-identity/sweep`. **Expected:** all three
`by_cloud` blocks show `processed=0`, no orphans.

## Step 9 — Where this fits

Phase 4c completes the per-cloud sweeper sub-phases. The acceptance
bar from the design (§6.7 — orphan reporting on every actively-
promoted cloud) is met as soon as the operator's three flags are
flipped on and the runbook above is green for the configured clouds.

Optional Phase 4c+ cross-check (deferred):
- **GCP `getIamPolicy` scrape.** Filters project IAM bindings to
  the synthetic machine identity's service-account email; flags
  bindings that no Entitle request still owns. Same dep + risk
  profile as Azure ARM cross-check. Revisit if real-world orphan
  rates suggest the Entitle-side reconciliation is missing drift.

Once all three legs (4a/4b/4c) are green in QA, design Phase 4
("tighten baseline" — strip writes off the baseline IAM user /
service principal / service account) unblocks. Do not tighten
before the sweepers prove they catch drift — fail-closed elevate()
+ drift reconciliation are the safety net for a baseline-credential
fallback bug.

## Rollback

1. `UPDATE app_config SET value='0' WHERE key='cloud_identity_gcp_enabled';`
   silences the GCP leg without touching AWS, Azure, or the master
   gate. The next sweep skips GCP entirely.
2. Code rollback: revert the Phase 4c commit. The dispatch loses
   the GCP branch and `sweep_gcp()` disappears. Existing GCP rows
   in `entitle_activations` are unaffected.
3. Any rows the GCP leg flipped to `revoked` / `failed` stay that
   way; flipping the flag off doesn't un-reconcile.
