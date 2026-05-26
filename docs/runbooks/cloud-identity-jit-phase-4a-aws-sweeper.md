# Phase 4a — Cloud-identity JIT sweeper (AWS reconciliation)

Validates Phase 4a of the [cloud-identity JIT design](../design/cloud-identity-jit.md) §6.7
("Audit trail + agent-revoke sweeper").

After Phase 4a deploys, this confirms:

1. A background loop runs `sweep_once()` every
   `cloud_identity_sweep_interval_minutes` (default 60). The loop is
   always launched; the sweeper itself short-circuits when the master
   gate or sweep-enabled flag is off so a runtime flag flip activates
   the next pass without an app restart.
2. With the master gate ON, AWS opt-in ON, and an outstanding
   `entitle_activations` row whose Entitle-side status has flipped to
   `revoked`, the sweeper reconciles the local row to `revoked` and
   captures the Entitle status in `denial_reason`.
3. Rows past their local `expires_at` are reconciled to `revoked` even
   without consulting Entitle (mathematical TTL expiry).
4. Rows whose `entitle_request_id` Entitle returns 404 for are flipped
   to `failed` and surfaced as orphans.
5. `GET /api/cloud-identity/orphans` returns the cached last-sweep
   summary; `POST /api/cloud-identity/sweep` forces a fresh pass.
6. With the master gate or `cloud_identity_sweep_enabled=false`, the
   sweeper no-ops cleanly and the cached result records the skip.

Takes ~30 minutes including the simulated-drift fixtures. Run on the
community edition (`c:\Scripts\VM_CLI\vm-dashboard-community\`) against
the same Entitle tenant used for Phases 1 + 2.

## Prerequisites

- Phase 0, 1, 2 + 3 verified — the sweeper reconciles rows that the
  Phase 1 elevate() flow created, gated by the Phase 3 master/per-cloud
  flags.
- Master gate ON (`cloud_identity_gate_enabled=true`), AWS opt-in ON
  (`cloud_identity_aws_enabled=true`), matrix populated with at least
  one AWS entry.
- At least one row in `entitle_activations` from a real Phase 2 deploy
  with `cloud='aws'` and `status='granted'`. The smoke test below
  generates synthetic ones if no real row exists.

## Step 1 — Confirm the background loop is running

```powershell
docker compose exec app python -c "
import asyncio
print('event loop tasks:')
for t in asyncio.all_tasks(asyncio.get_event_loop()) if False else []:
    print(' -', t.get_name())
"
```

The TestClient path won't have the loop running; use the actual
`docker compose logs app | Select-String 'ci_sweeper_loop'` instead:

```powershell
docker compose logs app | Select-String "cloud-identity sweeper"
```

**Expected:** at least one line like
`cloud_identity sweep: processed=0 reconciled=0 orphans=0 clouds=[]`
within the first sleep interval after startup. If the line is missing,
the loop didn't launch — check `docker compose logs app` for an
`asyncio.create_task(_ci_sweeper_loop())` exception.

## Step 2 — Force a sweep on demand

```powershell
$admin = Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/auth/login -Body @{username='admin'; password='<pw>'}
$h = @{Authorization="Bearer $($admin.access_token)"}

Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/cloud-identity/sweep -Headers $h | ConvertTo-Json -Depth 5
```

**Expected** (master gate + AWS opt-in ON, no granted rows yet):
```json
{
  "started_at": "...", "ended_at": "...", "duration_seconds": 0,
  "processed": 0, "reconciled": 0,
  "orphans": [],
  "by_cloud": {"aws": {"cloud":"aws","processed":0,"reconciled_revoked":0,"reconciled_past_ttl":0,"reconciled_failed":0,"orphans":[]}}
}
```

**Expected** (master gate OFF):
```json
{"skipped": "cloud_identity_gate_enabled / cloud_identity_sweep_enabled is off", ...}
```

## Step 3 — Reconcile a past-TTL row

Insert a synthetic granted-but-expired row directly:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
INSERT INTO entitle_activations
  (id, cloud, operation, status, payload_hash, requested_at, granted_at, expires_at, entitle_request_id, auto_approved)
VALUES
  ('phase4a-synth-1', 'aws', 'aws:ec2:deploy', 'granted',
   'deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef',
   NOW() - INTERVAL '2 hours', NOW() - INTERVAL '90 minutes',
   NOW() - INTERVAL '30 minutes',
   'synth-req-1', true);"
```

Force a sweep (Step 2). **Expected:** `processed=1`,
`reconciled_past_ttl=1` under `by_cloud.aws`, no orphan.

Verify the row was flipped:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
SELECT id, status, revoked_at, denial_reason FROM entitle_activations WHERE id = 'phase4a-synth-1';"
```

**Expected:** `status='revoked'`, `revoked_at` set to a recent
timestamp, `denial_reason` includes `sweeper: past local TTL`.

## Step 4 — Reconcile against a revoked Entitle request

(Requires a real Entitle tenant + an admin who can manually revoke a
request from the Entitle UI.)

1. Deploy an instance via the dashboard so a real `entitle_activations`
   row is created with `status='granted'` and a real
   `entitle_request_id` populated by Phase 1's flow.
2. In the Entitle console, revoke the request manually.
3. Run `POST /api/cloud-identity/sweep`.

**Expected:** under `by_cloud.aws`, `reconciled_revoked=1` (or more if
multiple drifted), no orphan. The row's `status` flips to `revoked`,
`denial_reason` includes
`sweeper: entitle status=revoked reason=...`.

## Step 5 — Orphan: Entitle returns 404

Insert a synthetic row pointing at a request id Entitle has never
seen:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
INSERT INTO entitle_activations
  (id, cloud, operation, status, payload_hash, requested_at, granted_at, expires_at, entitle_request_id, auto_approved)
VALUES
  ('phase4a-synth-2', 'aws', 'aws:ec2:deploy', 'granted',
   'cafef00dcafef00dcafef00dcafef00dcafef00dcafef00dcafef00dcafef00d',
   NOW() - INTERVAL '5 minutes', NOW() - INTERVAL '4 minutes',
   NOW() + INTERVAL '55 minutes',
   'nonexistent-request-12345', true);"
```

Force a sweep (Step 2). **Expected:** under `by_cloud.aws`,
`reconciled_failed=1`, an orphan with `kind='entitle_unknown_request'`
in the top-level `orphans` list.

Row state:
```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
SELECT id, status, denial_reason FROM entitle_activations WHERE id = 'phase4a-synth-2';"
```

**Expected:** `status='failed'`, `denial_reason` includes
`sweeper: entitle 404 / not found`.

## Step 6 — Orphan list survives across calls

```powershell
Invoke-RestMethod -Uri http://localhost:8000/api/cloud-identity/orphans -Headers $h | ConvertTo-Json -Depth 5
```

**Expected:** same payload as the last `POST /sweep`. The endpoint
returns the cached `cloud_identity_last_sweep` from `app_config`
without re-running. Confirm by querying:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
SELECT value FROM app_config WHERE key='cloud_identity_last_sweep';"
```

## Step 7 — Sweep-enabled flag toggles the loop

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
INSERT INTO app_config (key, value, workgroup, updated_at)
VALUES ('cloud_identity_sweep_enabled', '0', NULL, NOW())
ON CONFLICT (key) DO UPDATE SET value='0', updated_at=NOW();"
docker compose exec app python -c "from web_dashboard.services import config_service; config_service.invalidate()"

Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/cloud-identity/sweep -Headers $h | ConvertTo-Json -Depth 3
```

**Expected:** `{"skipped": "cloud_identity_gate_enabled / cloud_identity_sweep_enabled is off", ...}`.
The master gate is still on, but the sweep-specific flag silenced this
run. Useful for emergency pause without flipping the load-bearing
master gate.

Restore:
```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
UPDATE app_config SET value='1' WHERE key='cloud_identity_sweep_enabled';"
docker compose exec app python -c "from web_dashboard.services import config_service; config_service.invalidate()"
```

## Step 8 — Interval change picks up on next pass

Set the interval to 1 minute for a quick smoke:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
INSERT INTO app_config (key, value, workgroup, updated_at)
VALUES ('cloud_identity_sweep_interval_minutes', '1', NULL, NOW())
ON CONFLICT (key) DO UPDATE SET value='1', updated_at=NOW();"
docker compose exec app python -c "from web_dashboard.services import config_service; config_service.invalidate()"
```

Wait ~70 seconds. Check the logs for a second `cloud_identity sweep`
line newer than the first. **Expected:** the loop re-read the
interval on its previous tick and the new pass fires roughly 60s
later. Restore the default:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
DELETE FROM app_config WHERE key='cloud_identity_sweep_interval_minutes';"
docker compose exec app python -c "from web_dashboard.services import config_service; config_service.invalidate()"
```

## Step 9 — Clean up synthetic rows

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
DELETE FROM entitle_activations WHERE id LIKE 'phase4a-synth-%';"
```

Re-run `POST /api/cloud-identity/sweep`. **Expected:** no orphans, no
reconciliations — the table is clean.

## Step 10 — Where this fits

Phase 4a ships the sweeper foundation + AWS reconciliation. Phase 4b
(Azure) and 4c (GCP) extend `sweep_once()` to call
`sweep_azure()` / `sweep_gcp()` when those clouds' opt-in flags are on,
adding cloud-specific orphan strategies (Azure auto-expires, GCP needs
IAM policy scraping). The runbook above re-validates against AWS only;
4b/4c get their own runbooks.

Once 4a is green in QA, design Phase 4 ("tighten baseline" — strip
writes off the baseline IAM user) unblocks. Don't tighten before the
sweeper proves it catches drift in dev — the whole point of fail-closed
elevate() + drift reconciliation is to make a baseline-cred fallback
visible loudly rather than silently degrade.

## Rollback

1. `DELETE FROM app_config WHERE key='cloud_identity_sweep_enabled';`
   silences the sweeper without flipping the master gate. The loop
   keeps spinning but each pass is a no-op.
2. `DELETE FROM app_config WHERE key='cloud_identity_gate_enabled';`
   silences both elevate() and the sweeper — full Phase 0 behaviour.
3. The sweeper only *reads* Entitle and *writes* dashboard rows. It
   never issues IAM changes, so a misbehaving sweeper cannot
   over-revoke cloud-side. Worst case it incorrectly marks a row as
   revoked locally; the next real elevation will create a fresh row.
4. Code rollback: `git revert` the Phase 4a commit. Removes the
   sweeper service, the loop in main.py, and the two endpoints; the
   `entitle_activations` schema is unchanged.
