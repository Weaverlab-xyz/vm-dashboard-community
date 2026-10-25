# Adding a permission scope

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are adding a new area to the dashboard and it needs its own permission scope.

Part of [Permissions](../permissions.md).

Adding an entry to `PERMISSION_SCOPE_LEVELS` and gating a route on it **revokes that route
from every user who has an explicit permission map**, silently — no error, no log line, and
the administrator who set those permissions never saw a row for the new scope. So a new
scope needs, in the same change:

1. an entry in `PERMISSION_SCOPE_LEVELS` (`web_dashboard/api/auth.py`) listing only the
   levels something actually enforces;
2. a display label in `permissionScopeLabel` (`web_dashboard/static/js/app.js`), and a
   place in `PERMISSION_SCOPE_GROUPS` (`web_dashboard/api/auth.py`), which lays out the
   grid (`tests/test_permission_catalog.py` asserts every scope is in exactly one group);
3. a **backfill** in `web_dashboard/database.py` granting exactly the levels a non-admin
   with an explicit map could already reach. It must widen three places:
   `users.permissions`, `oauth_group_mappings.default_permissions`, and the **built-in
   roles** in `web_dashboard/services/role_service.py`. Built-in roles are seeded once and
   never updated, so a new scope is missing from every existing install's copy until the
   backfill adds it and `role_service.reconcile` runs;
4. the right *form* of the check — see the table below;
5. an entry in `_NAV_SCOPE` or `_NAV_EXEMPT` in `tests/test_permission_catalog.py`, which fails if a nav
   section has no scope or a route enforces a scope the catalog does not contain.

What the route was before decides both 3 and 4 together, and there are three cases:

| The route was | Use | Backfill it? |
|---|---|---|
| ungated (`get_current_user` only) | `require_permission` | **yes** — its full level set |
| `require_admin` | `require_explicit_permission` | **no** |
| on the old phantom `"admin"` scope | `require_permission` | **no** |

The first two are the obvious pair. The third existed because
`require_permission("admin", …)` named a scope that was not in the catalog, so it refused
every user with an explicit map and allowed every legacy one — meaning it needs the
permissive form *and* no backfill, a combination that looks like an oversight and is not.
`test_the_form_of_every_gate_agrees_with_the_backfill` checks all three against each
other, so a mismatch fails the build rather than silently revoking or silently granting.

Scope **keys** are persisted in every user's and group's permissions JSON and are turned
into Entra group names by `web_dashboard/scripts/bootstrap_entitle_groups.py`. Renaming one un-grants
everyone who had it, and the symptom is a locked-out user rather than an error — so if a
display name needs to change, change the label map, not the key.
