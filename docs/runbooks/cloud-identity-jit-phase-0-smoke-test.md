# Phase 0 — Cloud-identity JIT smoke test

Validates the scaffolding for the [cloud-identity JIT design](../design/cloud-identity-jit.md).

After Phase 0 deploys, this confirms:

1. The migration ran cleanly (`entitle_activations` table exists,
   `approvals.principal_kind` column exists).
2. `cloud_identity_service` is importable and the `elevate()` context
   manager works as a no-op when the gate is off (the default).
3. With the gate off, every cloud write path remains unchanged.
4. Flipping the gate on without the Phase 1 implementation raises a
   clear, operator-actionable error rather than silently doing the
   wrong thing.

Takes ~10 minutes. Run on dev.

## Prerequisites

- Dev environment running on the post-Phase-0 image.
- No new env vars or external dependencies — Phase 0 is local-only.

## Step 1 — Verify the schema migration ran

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "\d entitle_activations"
```

**Expected:** the table prints with columns including `id`, `cloud`,
`operation`, `status`, `payload_hash`, `requested_at`, `entitle_request_id`,
and the rest of the row shape from the [design Appendix](../design/cloud-identity-jit.md#7-audit-trail).

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "\d approvals" | Select-String "principal_kind"
```

**Expected:** one line confirming `principal_kind | character varying(16) | not null default 'user'`.

If either query returns "relation … does not exist" or no rows, the
migration didn't run. Check `docker compose logs app` for ALTER errors;
the migrations use savepoints so one failure doesn't abort the others.

## Step 2 — Verify the service is importable and no-op

```powershell
docker compose exec app python -c "
import asyncio
from web_dashboard.services.cloud_identity_service import elevate, ElevationHandle, CloudIdentityError

async def main():
    async with elevate(
        'aws', 'aws:ec2:deploy',
        duration_minutes=15,
        payload_hash='deadbeef' * 8,
        requester_user_id='smoke-test',
    ) as handle:
        assert isinstance(handle, ElevationHandle), 'handle wrong type'
        assert handle.is_noop is True, f'expected is_noop=True, got {handle.is_noop}'
        assert handle.correlation_tag == '', f'expected empty tag, got {handle.correlation_tag!r}'
        assert handle.cloud == 'aws'
        assert handle.operation == 'aws:ec2:deploy'
        print('OK no-op elevate:', handle)

asyncio.run(main())
"
```

**Expected:** a single line printing `OK no-op elevate: ElevationHandle(...)`
with `is_noop=True`. No DB row is inserted (we don't write activations
when the gate is off).

## Step 3 — Verify no `entitle_activations` row was created

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "SELECT COUNT(*) FROM entitle_activations;"
```

**Expected:** `count` is `0`. The no-op path must not write to the DB.

## Step 4 — Verify the gate fails closed when flipped on

This step confirms that flipping `cloud_identity_gate_enabled` on
without Phase 1 raises a clear error rather than silently doing nothing
or worse, falling back to baseline creds while pretending to be gated.

```powershell
# Insert the flag row
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
INSERT INTO app_config (key, value, workgroup, updated_at)
VALUES ('cloud_identity_gate_enabled', '1', NULL, NOW())
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
"

# Invalidate caches and re-test
docker compose exec app python -c "
import asyncio
from web_dashboard.services import config_service
from web_dashboard.services.cloud_identity_service import elevate, CloudIdentityError

config_service.invalidate()
assert config_service.get_bool('cloud_identity_gate_enabled') is True, 'flag did not flip'

async def main():
    try:
        async with elevate(
            'aws', 'aws:ec2:deploy',
            duration_minutes=15,
            payload_hash='deadbeef' * 8,
            requester_user_id='smoke-test',
        ):
            print('FAIL — expected CloudIdentityError but elevate succeeded')
    except CloudIdentityError as e:
        print('OK — gate-on without Phase 1 raised:', str(e)[:120])

asyncio.run(main())
"
```

**Expected:** prints `OK — gate-on without Phase 1 raised: ...` with
a clear "Phase 1 implementation has not been built yet" message.

## Step 5 — Cleanup

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
DELETE FROM app_config WHERE key = 'cloud_identity_gate_enabled';
DELETE FROM entitle_activations;
"
docker compose exec app python -c "from web_dashboard.services import config_service; config_service.invalidate()"
```

Confirm the gate is off:

```powershell
docker compose exec app python -c "
from web_dashboard.services import config_service
print('gate:', config_service.get_bool('cloud_identity_gate_enabled', default=False))
"
```

**Expected:** `gate: False`.

## Exit criteria

- Step 1 shows both schema additions.
- Step 2 prints OK with `is_noop=True`.
- Step 3 shows zero activation rows.
- Step 4 raises `CloudIdentityError` with the Phase-1-not-implemented message.
- Step 5 restores the off state.

If all five pass, Phase 0 is verified end-to-end. The scaffolding is in
place and dormant; no behaviour change against the live dashboard.

## What's next (not Phase 0)

| Phase | Adds |
|---|---|
| 1 | `entitle_service.submit_machine_request()` + polling + cloud-side IAM grant via Entitle agent. Wires the real elevate() path |
| 2 | First cloud SDK write paths (e.g. AWS deploy) actually wrapped in `async with elevate(...)`, but only when the gate is on |
| 3 | Operation matrix UI; per-cloud opt-in flags |
| 4+ | Per-cloud activation, sweeper, audit trail |

See [docs/design/cloud-identity-jit.md](../design/cloud-identity-jit.md) §8 for the
full phase breakdown.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `relation "entitle_activations" does not exist` | Migration didn't run — likely a SQL syntax error in `_migrations` | Check `docker compose logs app` around startup; each ALTER runs inside a savepoint so the failure should be logged but won't crash the app |
| `principal_kind` column missing on `approvals` | Same — savepoint swallowed the failure | Same check; usually the column was already present from a previous test run |
| `ImportError: cannot import name 'elevate'` from cloud_identity_service | The new module wasn't included in the image | Rebuild: `docker compose up --build app` |
| Step 4 doesn't raise — elevate just returns | The gate flag isn't being read; verify `config_service.invalidate()` was called before the test | Re-run Step 4 after `invalidate()` |
| Step 4 raises a *different* exception | Likely an import-time bug in the service module | Read the traceback; if it mentions a missing import, check that `EntitleActivation` is imported in `cloud_identity_service.py`'s lazy-import path |
