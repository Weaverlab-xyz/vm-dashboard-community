# Phase 1 — Cloud-identity JIT Entitle submit + poll

Validates the real-elevation path from the [cloud-identity JIT
design](../design/cloud-identity-jit.md) §6.1.

After Phase 1 deploys, this confirms:

1. With the gate ON, `cloud_identity_service.elevate()` no longer
   raises the "Phase 1 not implemented" error. Instead it submits an
   access request to Entitle, polls until terminal, and yields a
   handle carrying the request id + expiry.
2. The `entitle_activations` audit row walks the full state machine:
   `pending → granted → completed` on success;
   `pending → denied` / `pending → failed` / `pending → timeout` on
   the failure branches.
3. Misconfiguration fails closed — no operation matrix entry, no
   synthetic machine email, or no Entitle credentials each produce a
   `CloudIdentityError` and a row in a non-`granted` state.
4. With the gate OFF, the no-op path from Phase 0 is unchanged.

Takes ~30 minutes. Run on the community edition
(`c:\Scripts\VM_CLI\vm-dashboard-community\`) against a real Entitle
tenant.

## Prerequisites

- Community edition running on the post-Phase-1 image
  (`docker compose up` from the community repo).
- A real Entitle tenant with:
  - At least one access bundle or role you can request (note its
    Entitle bundle/role id).
  - A synthetic "machine identity" user — record its email.
  - An auto-approve workflow tied to that synthetic user (so the
    poll terminates without human intervention during this test).
  - An API token with permission to submit + poll on
    `/public/v1/accessRequests`.
- Set the following via the `/settings` UI (or directly in
  `app_config`):
  - `entitle_api_url` = your Entitle base URL (no trailing slash;
    e.g. `https://api.entitle.io`)
  - `entitle_api_token` = the bearer token (store via `/secrets`
    or KV reference)
  - `entitle_machine_identity_email` = the synthetic user's email
  - `cloud_identity_matrix` = JSON of operation → target, e.g.:
    ```json
    {
      "aws:ec2:deploy": {"bundle_id": "<your-bundle-id>"}
    }
    ```

## Step 1 — Schema check (carryover from Phase 0)

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "\d entitle_activations" | Select-String "status|denial_reason|granted_at|expires_at"
```

**Expected:** the columns are present. No schema change in Phase 1 —
the table from Phase 0 is what we write to.

## Step 2 — Misconfiguration: empty operation matrix

Leave `cloud_identity_matrix` unset / empty. Flip the gate on:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
INSERT INTO app_config (key, value, workgroup, updated_at)
VALUES ('cloud_identity_gate_enabled', '1', NULL, NOW())
ON CONFLICT (key) DO UPDATE SET value = '1', updated_at = NOW();
DELETE FROM app_config WHERE key = 'cloud_identity_matrix';"

docker compose exec app python -c "
import asyncio
from web_dashboard.services import config_service
from web_dashboard.services.cloud_identity_service import elevate, CloudIdentityError
config_service.invalidate()

async def main():
    try:
        async with elevate('aws', 'aws:ec2:deploy', duration_minutes=5,
                           payload_hash='cafebabe'*8, requester_user_id='smoke'):
            print('FAIL — empty matrix should have raised')
    except CloudIdentityError as e:
        print('OK matrix-empty raises:', str(e)[:140])

asyncio.run(main())
"
```

**Expected:** prints `OK matrix-empty raises: operation 'aws:ec2:deploy' is not in cloud_identity_matrix…`.

## Step 3 — Misconfiguration: missing behalfOf email

Populate the matrix but clear the synthetic email:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
INSERT INTO app_config (key, value, workgroup, updated_at)
VALUES ('cloud_identity_matrix', '{\"aws:ec2:deploy\": {\"bundle_id\": \"REPLACE_ME\"}}', NULL, NOW())
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
DELETE FROM app_config WHERE key = 'entitle_machine_identity_email';"

docker compose exec app python -c "
import asyncio
from web_dashboard.services import config_service
from web_dashboard.services.cloud_identity_service import elevate, CloudIdentityError
config_service.invalidate()

async def main():
    try:
        async with elevate('aws', 'aws:ec2:deploy', duration_minutes=5,
                           payload_hash='cafebabe'*8, requester_user_id='smoke'):
            print('FAIL — missing email should have raised')
    except CloudIdentityError as e:
        print('OK email-missing raises:', str(e)[:140])

asyncio.run(main())

docker compose exec db psql -U dashboardadmin -d vmclidashboard -c \"
SELECT status, denial_reason FROM entitle_activations ORDER BY requested_at DESC LIMIT 1;\"
"
```

**Expected:**
- Python prints `OK email-missing raises: entitle_machine_identity_email is empty …`.
- DB row has `status='failed'`, `denial_reason='machine identity email not configured'`.

## Step 4 — Real submit + auto-approve happy path

Configure the matrix with the real bundle id + synthetic email:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
INSERT INTO app_config (key, value, workgroup, updated_at)
VALUES ('cloud_identity_matrix', '{\"aws:ec2:deploy\": {\"bundle_id\": \"<YOUR-REAL-BUNDLE-ID>\"}}', NULL, NOW())
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
INSERT INTO app_config (key, value, workgroup, updated_at)
VALUES ('entitle_machine_identity_email', '<your-synthetic-user@example.com>', NULL, NOW())
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();"
```

Then exercise:

```powershell
docker compose exec app python -c "
import asyncio
from web_dashboard.services import config_service
from web_dashboard.services.cloud_identity_service import elevate
config_service.invalidate()

async def main():
    async with elevate('aws', 'aws:ec2:deploy', duration_minutes=5,
                       payload_hash='facefeed'*8, requester_user_id='smoke') as h:
        print('granted:', h.entitle_request_id, 'expires:', h.expires_at, 'tag:', h.correlation_tag)

asyncio.run(main())
"
```

**Expected:**
- Python prints something like
  `granted: req_abc123 expires: 2026-05-24 21:05:00 tag: entitle:req_abc123`.
- Latency depends on Entitle's policy engine — typically <2s for auto-approve.
- `docker compose exec db psql … "SELECT status, entitle_request_id, granted_at, expires_at FROM entitle_activations ORDER BY requested_at DESC LIMIT 1;"`
  shows `status='completed'` (flipped on context exit), with
  `entitle_request_id` matching the printed value.

In the Entitle UI, the access request appears with the dashboard's
synthetic user as `behalfOf` and the requested duration.

## Step 5 — Denied / non-auto-approve path

Either:
- Temporarily disable the auto-approve workflow for the bundle so
  Entitle queues the request for human review, then deny it; OR
- Swap the matrix to a bundle the synthetic user has no entitlement
  for, so Entitle denies on policy.

Re-run the Python from Step 4.

**Expected:**
- Python raises: `Entitle denied request <id> (status=denied): …`.
- Row: `status='denied'`, `denial_reason` populated.

If the request just hangs in `pending` past the requested duration
(5min), the poller times out:
- Python raises: `Entitle did not grant request … within 5 minutes`.
- Row: `status='timeout'`.

## Step 6 — Duration ceiling clamps requests

```powershell
docker compose exec app python -c "
import asyncio
from web_dashboard.services import config_service
from web_dashboard.services.cloud_identity_service import elevate
config_service.invalidate()

async def main():
    # machine_ttl_ceiling_minutes defaults to 60; ask for 999.
    async with elevate('aws', 'aws:ec2:deploy', duration_minutes=999,
                       payload_hash='cafef00d'*8, requester_user_id='smoke') as h:
        print('handle.duration:', h.duration_minutes)

asyncio.run(main())
"
```

**Expected:** `handle.duration: 60`. The Entitle request body in the
Entitle UI shows `duration=60` too.

## Step 7 — Concurrent activations share no state

```powershell
docker compose exec app python -c "
import asyncio
from web_dashboard.services import config_service
from web_dashboard.services.cloud_identity_service import elevate
config_service.invalidate()

async def one(tag):
    async with elevate('aws', 'aws:ec2:deploy', duration_minutes=5,
                       payload_hash=(tag*16)[:64], requester_user_id=tag) as h:
        return h.entitle_request_id, h.activation_row_id

async def main():
    a, b = await asyncio.gather(one('alice'), one('bobbobbo'))
    assert a[0] != b[0], 'both got same Entitle id'
    assert a[1] != b[1], 'both got same DB row'
    print('OK concurrent:', a, b)

asyncio.run(main())
"
```

**Expected:** two distinct request ids + row ids; both context
managers complete cleanly. No `RuntimeError: re-entrant` or shared
state surprises.

## Step 8 — Flag-off regression

Disable the gate, confirm Phase 0's no-op path is preserved:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
DELETE FROM app_config WHERE key = 'cloud_identity_gate_enabled';"

docker compose exec app python -c "
import asyncio
from web_dashboard.services import config_service
from web_dashboard.services.cloud_identity_service import elevate
config_service.invalidate()

async def main():
    async with elevate('aws', 'aws:ec2:deploy', duration_minutes=15,
                       payload_hash='deadbeef'*8, requester_user_id='smoke') as h:
        assert h.is_noop is True
        assert h.correlation_tag == ''
        print('OK no-op path unchanged')

asyncio.run(main())
"
```

**Expected:** `OK no-op path unchanged`. The `entitle_activations`
table count is unchanged from the start of Step 8 (no row is
written when the gate is off).

## Step 9 — Audit reconciliation

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
SELECT cloud, operation, status, COUNT(*) FROM entitle_activations
GROUP BY cloud, operation, status ORDER BY cloud, operation, status;"
```

**Expected:** a tidy histogram showing the state-machine paths you
exercised in Steps 2–7. Use this as the basis for Phase 4's sweeper —
the sweeper reconciles `granted`/`completed` rows against Entitle's
own grant log and reports mismatches.

## Step 10 — Where this fits

Phase 1 ships the dashboard side of the loop. Phase 2 wraps the
first real cloud SDK write (recommend AWS `aws:ec2:deploy`) in
`async with elevate(...)` so a deploy flow actually exercises the
elevation. Phase 3 adds the operation-matrix admin UI so operators
don't edit `app_config` JSON by hand. Phase 4 adds the per-cloud
sweeper that reconciles activation rows against the cloud side.

## Rollback

If something goes wrong:

1. `DELETE FROM app_config WHERE key = 'cloud_identity_gate_enabled';`
   restores Phase 0 behaviour (no-op).
2. The `entitle_activations` table is additive — pre-Phase-1 rows
   are unaffected, post-Phase-1 rows can stay for audit (or be
   deleted if the bundle ids embedded in them have been rotated).
3. No code rollback needed; Phase 1 changes are confined to
   `cloud_identity_service` + `entitle_service` machine-flow helpers.
   Reverting them is a clean `git revert` of the Phase 1 commit.
