# Phase 2 — Cloud-identity JIT for AWS EC2 (deploy + terminate)

Validates Phase 2 of the [cloud-identity JIT design](../design/cloud-identity-jit.md) —
first cloud SDK write paths wrapped in `cloud_identity_service.elevate()`.

After Phase 2 deploys, this confirms:

1. The two EC2 write paths the dashboard issues directly to boto3 are
   bracketed by `elevate()`:
   - `POST /api/aws/deploy` (single + bulk) → `aws:ec2:deploy`
   - `DELETE /api/aws/instances/{id}` → `aws:ec2:terminate`
2. Gate **off** (default) preserves pre-Phase-2 behaviour exactly —
   no Entitle round-trip, no `entitle_activations` row, deploys
   succeed on baseline AWS credentials.
3. Gate **on** + matrix configured → every deploy / terminate writes
   a row, submits an Entitle request, and (on grant) injects the
   `EntitleRequestId=<id>` tag onto the resulting EC2 instance.
4. Gate **on** + matrix missing the operation → fails closed before
   any AWS call is made.

Takes ~30 minutes. Run on the community edition
(`c:\Scripts\VM_CLI\vm-dashboard-community\`) against a real AWS
account + the same Entitle tenant used for Phase 1.

## Prerequisites

- Phase 0 + Phase 1 verified (the no-op + happy-path Entitle submit
  are already known-good).
- `cloud_identity_matrix` has at least these entries:
  ```json
  {
    "aws:ec2:deploy":    {"bundle_id": "<ec2-deploy-bundle>"},
    "aws:ec2:terminate": {"bundle_id": "<ec2-terminate-bundle>"}
  }
  ```
  Auto-approve both bundles for this synthetic user to keep the
  smoke test fast (Phase 4 covers human-approval flows).
- AWS creds wired in `/secrets` (or env vars) and a known-good AMI
  + subnet + security group to deploy into.

## Step 1 — Gate-off regression

Make sure the gate is off:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
DELETE FROM app_config WHERE key = 'cloud_identity_gate_enabled';"
docker compose exec app python -c "from web_dashboard.services import config_service; config_service.invalidate()"
```

From the UI, deploy one EC2 instance (any AMI). Wait for the Job
to reach `completed`.

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
SELECT COUNT(*) FROM entitle_activations WHERE cloud='aws';"
```

**Expected:** count is unchanged from before the deploy — zero
new rows. The deploy succeeded on baseline creds; the elevate() in
the path was a no-op. Confirm the instance is running in AWS as
usual.

## Step 2 — Flip the gate on

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
INSERT INTO app_config (key, value, workgroup, updated_at)
VALUES ('cloud_identity_gate_enabled', '1', NULL, NOW())
ON CONFLICT (key) DO UPDATE SET value = '1', updated_at = NOW();"
docker compose exec app python -c "from web_dashboard.services import config_service; config_service.invalidate()"
```

## Step 3 — Single-deploy happy path

Deploy one EC2 instance from the UI. Watch the Job logs.

**Expected timeline:**
1. Progress reaches ~40% with "Launching EC2 instance…".
2. (Behind the scenes) `elevate()` submits to Entitle, polls until
   `granted` (≤2s on auto-approve).
3. `aws_service.launch_instance` runs with the `correlation_tag`
   from the granted handle.
4. Job completes; instance reaches `pending` → `running` in AWS.

Verify the audit row + AWS tag:

```powershell
$inst = "<the new instance id from the UI>"
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
SELECT operation, status, entitle_request_id, granted_at, expires_at
FROM entitle_activations
WHERE cloud='aws' AND operation='aws:ec2:deploy'
ORDER BY requested_at DESC LIMIT 1;"

aws ec2 describe-tags --filters Name=resource-id,Values=$inst Name=key,Values=EntitleRequestId
```

**Expected:**
- Row `status='completed'` (flipped on context exit),
  `entitle_request_id` populated, `granted_at` set.
- AWS tag query returns one tag: `EntitleRequestId =
  entitle:<request_id>` matching the row.

This is the load-bearing join: CloudTrail's `RunInstances` event
for this instance now carries the tag, and the dashboard's
activation row carries the same request id. An auditor can pivot
between Entitle's approval log, the dashboard's row, and
CloudTrail's API call by a single string.

## Step 4 — Bulk-deploy: one activation per instance

Trigger a bulk deploy of N=3 instances from the UI.

**Expected:**
- Three separate `entitle_activations` rows, one per instance,
  each with a distinct `entitle_request_id` and distinct
  `payload_hash` (instance_name varies).
- Three distinct EC2 instances each tagged with their own
  `EntitleRequestId`.
- If Entitle denies one of the three (manual test below), the
  other two still complete — failures are per-row, not
  per-batch.

To force a single-row denial (optional), use Entitle's policy to
deny requests when `instance_name` matches a magic substring like
`deny-me-please`, then bulk-deploy `[ok-a, deny-me-please, ok-b]`.
**Expected:** row for `deny-me-please` is `status='denied'`, its
Job is failed; the other two Jobs complete; AWS has 2 new instances.

## Step 5 — Terminate happy path

In the UI, terminate one of the instances from Step 3 or 4.

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
SELECT operation, status, entitle_request_id
FROM entitle_activations
WHERE cloud='aws' AND operation='aws:ec2:terminate'
ORDER BY requested_at DESC LIMIT 1;"
```

**Expected:** new row, `operation='aws:ec2:terminate'`,
`status='completed'`. The EC2 instance reaches `terminated` in AWS.

Note: there's no `EntitleRequestId` tag on a terminated instance
(EC2 `TerminateInstances` doesn't take tags). The join key is
`payload_hash` — Phase 4's sweeper reconciles activation rows
against CloudTrail's `TerminateInstances` event by `instance_id`
captured in both.

## Step 6 — Fail-closed: operation missing from matrix

Temporarily drop the `aws:ec2:deploy` entry:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
UPDATE app_config
SET value = '{\"aws:ec2:terminate\": {\"bundle_id\": \"<terminate-bundle>\"}}'
WHERE key = 'cloud_identity_matrix';"
docker compose exec app python -c "from web_dashboard.services import config_service; config_service.invalidate()"
```

Try to deploy a new instance from the UI.

**Expected:**
- Job fails with status `failed` and message containing
  "Cloud-identity elevation refused EC2 deploy: operation
  'aws:ec2:deploy' is not in cloud_identity_matrix…".
- **No** new EC2 instance is created in AWS. Verify via
  `aws ec2 describe-instances --filters Name=instance-state-name,Values=pending,running`
  — count unchanged.
- A row in `entitle_activations` was created with `status='pending'`
  but no `entitle_request_id` (the operation check fires before
  the row is inserted, actually — confirm by counting rows: should
  be unchanged from Step 5).

Restore the matrix:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
UPDATE app_config
SET value = '{\"aws:ec2:deploy\": {\"bundle_id\": \"<deploy-bundle>\"}, \"aws:ec2:terminate\": {\"bundle_id\": \"<terminate-bundle>\"}}'
WHERE key = 'cloud_identity_matrix';"
docker compose exec app python -c "from web_dashboard.services import config_service; config_service.invalidate()"
```

## Step 7 — Fail-closed: Entitle denies the request

In the Entitle policy console, flip the deploy bundle to deny for
this synthetic user (or use the magic-substring trick from Step 4).
Try a deploy.

**Expected:**
- Job fails with "Entitle denied request <id>: …".
- `entitle_activations` row: `status='denied'`, `denial_reason`
  populated.
- **No** new EC2 instance.

Re-enable auto-approve before proceeding.

## Step 8 — Payload-hash binding (advanced)

Deploy two instances with **identical** parameters and check that
both produce the **same** `payload_hash`:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "
SELECT instance.id, ea.payload_hash
FROM entitle_activations ea
WHERE ea.cloud='aws' AND ea.operation='aws:ec2:deploy'
ORDER BY ea.requested_at DESC LIMIT 5;"
```

**Expected:** rows for identical-config deploys have the same
`payload_hash`; rows where instance_name / subnet / SG / AMI
differs have different hashes. This is the binding that makes a
granted Entitle activation useless if replayed against a different
deploy intent — Phase 4's sweeper validates the hash before
counting the grant as legitimate.

## Step 9 — Concurrency / sequence

Trigger a single deploy + single terminate of an unrelated instance
back-to-back. Confirm:
- Both Jobs complete.
- Two rows: one `aws:ec2:deploy` `status='completed'`, one
  `aws:ec2:terminate` `status='completed'`.
- Their `entitle_request_id`s are distinct.

This rules out shared state in the elevate() context across
operations.

## Step 10 — Where this fits

Phase 2 wraps the AWS EC2 deploy + terminate paths. Other AWS
write paths (`create_image`, `delete_ami`, `set_workgroup_tag`,
`run_ecs_jumpoint_task`) are intentionally **not yet wrapped** —
each gets its own line in `cloud_identity_matrix` + its own
elevate() splice in a follow-up. Recommended order, blast-radius
weighted: `enable_ena_support` → `create_image` → `deregister_ami`
→ `stop_ecs_jumpoint_task`.

Phase 3 ships the operation-matrix admin UI so operators don't
edit `app_config` JSON by hand. Phase 4 wraps Azure (`virtual_machines.begin_create_or_update`)
and GCP (`instances().insert`).

## Rollback

If something goes wrong:

1. `DELETE FROM app_config WHERE key = 'cloud_identity_gate_enabled';`
   → instantly reverts to Phase 0 behaviour. All wrapped paths
   become no-ops; new deploys proceed on baseline creds.
2. EC2 instances tagged with `EntitleRequestId` from prior
   wrapped deploys keep their tag — it's harmless metadata.
3. The Phase 2 code changes are confined to
   `web_dashboard/api/aws.py` + `web_dashboard/services/aws_service.py`.
   A `git revert` of the Phase 2 commit is a clean rollback if the
   elevate() splice itself is suspected.
