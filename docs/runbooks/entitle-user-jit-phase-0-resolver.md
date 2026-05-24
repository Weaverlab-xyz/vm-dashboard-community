# Phase 0 — Entitle user-JIT resolver verification

Validates the resolver behaviour from the [Entitle user-JIT design](../design/entitle-user-jit.md).

After Phase 0 deploys, this confirms:

1. The migration ran cleanly (`users.session_permissions` column exists).
2. The Azure OAuth callback computes the **union** of `default_permissions`
   across every matched `oauth_group_mappings` row (not just the first).
3. The union is re-applied on **every** login — losing a group membership
   in Entra (Entitle revoking the assignment) actually drops the
   corresponding scope on the next sign-in.
4. The admin-set baseline in `users.permissions` is preserved through
   sign-in; `effective_permissions = union(baseline, session)`.
5. Pre-Phase-0 admin-set users keep working when no group claim is
   present in the token.

Takes ~20 minutes. Run on dev.

## Prerequisites

- Dev environment running on the post-Phase-0 image.
- Azure OAuth configured (`AZURE_OAUTH_CLIENT_ID/SECRET/TENANT_ID/REDIRECT_URI`).
- Three Entra security groups available for testing (any names — they
  will be wired to dashboard mappings via the `/groups` admin page):
  - `T_GROUP_A` — will map to `default_permissions = {"aws": ["read"]}`
  - `T_GROUP_B` — will map to `default_permissions = {"vms": ["read"]}`
  - `T_GROUP_C` — will map to `default_permissions = {"is_admin": true}`
- A test Entra account you can assign/unassign from those groups.
  (Use Entitle for the real flow once Phase 2 lands; for Phase 0 use
  direct Entra group management.)

## Step 1 — Verify the schema migration ran

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "\d users" | Select-String "session_permissions"
```

**Expected:** one line confirming `session_permissions | text |`.

If the column is missing the migration didn't run — check
`docker compose logs app` for ALTER errors and confirm the
`"ALTER TABLE users ADD COLUMN session_permissions TEXT"` entry is
present in `database.py:_migrations`.

## Step 2 — Wire up test group mappings

Sign in as an existing admin user. Open `/groups`. For each of the
three Entra groups created in prerequisites, add an `oauth_group_mappings`
row:

| Entra group ID | Workgroup | default_permissions JSON |
|---|---|---|
| `<oid of T_GROUP_A>` | `Hydra` | `{"aws": ["read"]}` |
| `<oid of T_GROUP_B>` | `Hydra` | `{"vms": ["read"]}` |
| `<oid of T_GROUP_C>` | `Hydra` | `{"is_admin": true}` |

(Use whatever workgroup already exists in your dev DB — `Hydra` is the
default seed.)

## Step 3 — Union of group-derived permissions on first login

1. In Entra, assign the test account to **both** `T_GROUP_A` and `T_GROUP_B`
   (not `T_GROUP_C`).
2. Sign in via "Sign in with Microsoft" using the test account.
3. Assuming this is a new user, the dashboard auto-creates the row.
   Inspect the resulting row:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "SELECT username, permissions, session_permissions FROM users WHERE username LIKE '%test%' ORDER BY created_at DESC LIMIT 1;"
```

**Expected:**
- `permissions` is NULL (admin baseline untouched on auto-create).
- `session_permissions` is JSON containing **both** `aws:["read"]` **and**
  `vms:["read"]` — proving the union ran, not "first match wins".

4. Hit `/api/auth/me` with the resulting token. The response's `permissions`
   field should reflect the union (the endpoint returns
   `effective_permissions_dict`, which equals the session map here).

## Step 4 — Revocation on the next login

1. In Entra, remove the test account from `T_GROUP_B` (but leave it in
   `T_GROUP_A`).
2. Sign out and sign in again with the test account.
3. Re-inspect the row:

```powershell
docker compose exec db psql -U dashboardadmin -d vmclidashboard -c "SELECT username, permissions, session_permissions FROM users WHERE username LIKE '%test%' ORDER BY created_at DESC LIMIT 1;"
```

**Expected:**
- `session_permissions` no longer contains `vms:["read"]`. Only
  `aws:["read"]` remains.
- `permissions` is still NULL.

This is the load-bearing property — Entitle removing a group assignment
in Phase 2+ flows directly through this code path.

## Step 5 — Admin baseline survives a JIT-only revocation

1. As admin, in `/users`, hand-grant the test user
   `permissions = {"images": ["read"]}` (this is the admin-set baseline,
   independent of group claims).
2. In Entra, remove the test account from `T_GROUP_A` as well so it has
   **no** matched groups. (If the `oauth_group_mappings` table is non-empty
   and no group matches, login is rejected with
   `?error=not_authorized` — which is the documented behaviour.)
3. Sign in again. Confirm rejection with `not_authorized`.
4. Re-add the test account to `T_GROUP_A` only. Sign in.
5. Re-inspect the row:

**Expected:**
- `permissions` still contains `{"images": ["read"]}` (admin baseline
  was never touched by the OAuth path).
- `session_permissions` contains only `{"aws": ["read"]}`.
- `/api/auth/me` returns `permissions` = the union:
  `{"images": ["read"], "aws": ["read"]}`.

## Step 6 — JIT admin elevation via dashboard-admin group

1. Add the test account to `T_GROUP_C` (mapped to
   `{"is_admin": true}`).
2. Sign in. `/api/auth/me` should now return `is_admin: true` even
   though `users.is_admin` for this row is `false` in the DB. The
   admin checkmark in the user list still shows unchecked — admin
   came via the session map.
3. Hit an admin-only endpoint (e.g. `GET /api/users`). It should succeed.
4. Remove the test account from `T_GROUP_C`. Sign in again.
5. `/api/auth/me` returns `is_admin: false`. The same admin-only
   endpoint should now 403.

## Step 7 — Legacy admin-set user untouched

1. As a pre-Phase-0 admin-set local user (no Entra account, signs in via
   password), sign in.
2. Confirm `session_permissions` is NULL (no OAuth callback ran).
3. Confirm `effective_permissions_dict` returns the admin-set baseline
   unchanged.

This proves the Phase 0 resolver change is backward compatible.

## Step 8 — Roll forward

Phase 0 ships the resolver. The Entra/Entitle bootstrap that produces
the actual `dashboard-*` groups is Phases 1–2 of the
[Entitle user-JIT design](../design/entitle-user-jit.md). Until then,
the resolver works against any Entra group mapping seeded via the
`/groups` admin page — exactly as it did before, but now correctly
computing the union and re-applying on every login.

## Step 9 — Rollback

If something goes wrong:

1. The `session_permissions` column is additive — no destructive
   migration to reverse.
2. To disable the new behaviour entirely without code rollback:
   `UPDATE users SET session_permissions = NULL;`
   The resolver still computes `effective_permissions =
   permissions_dict` once the column is NULL, matching pre-Phase-0
   behaviour exactly.
3. If a full code rollback is needed, redeploy the previous image;
   the column will be left in place harmlessly.
