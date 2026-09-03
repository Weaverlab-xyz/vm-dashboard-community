# Entitle dashboard permissions

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want dashboard access without standing admins, or you need to tell the two mechanisms apart.

How Entitle grants **permissions inside this dashboard** — time-boxed access to a
scope (`aws`, `k8s`, …) or to administrator, without standing privilege.

> This is about access **to the dashboard itself**. Entitle granting access to the
> *infrastructure the dashboard builds* — SSH to a VM, a database account, a
> Kubernetes cluster — is a different track entirely; see
> [entitle.md](entitle.md).

## Two mechanisms — which one am I using?

There are two, and they are easy to confuse because they grant the same thing. **The
REST mechanism is the current one.** The Entra-group mechanism still works and is
still supported, so nothing breaks if you are already on it.

| | **REST** (current) | **Entra groups** (legacy) |
|---|---|---|
| How Entitle grants | calls this dashboard directly | adds the user to `dashboard-<scope>-<level>` in Entra |
| Works for | **any user** — local, Entra, any OIDC provider | Entra users only |
| Takes effect | **immediately** | at the user's next login |
| Revoke takes effect | **immediately** | at the user's next login |
| Needs | `entitle_rest_secret`, dashboard reachable from Entitle | Entra tenant, group provisioning, OIDC `groups` claim |
| Config flag | `entitle_user_jit_enabled` + `entitle_rest_secret` | `entitle_user_jit_enabled` |
| Detail | this document | [design](../design/entitle-user-jit.md) + [runbooks](../runbooks/entitle-user-jit-phase-1-bootstrap-entra.md) |

**Both can run at once.** They write different columns and are unioned into a user's
effective permissions, so moving from groups to REST is a gradual change rather than
a cutover: point new scopes at REST, leave the existing groups alone, retire them
when you are ready.

> **This endpoint is hosted by the dashboard — there is no Cloud Function to
> deploy.** It uses the same *Remote Adapter contract* the Cloud Functions adapters
> serve (`db_grant`, `portainer_access`, `azure_role_grant`), which is why the
> Entitle-side setup looks identical. But those exist because Entitle needs an
> endpoint and the target is somewhere else; here the dashboard **is** the target, so
> a function hop would add a round trip, a second credential and a second thing to
> deploy, and buy nothing. See [cloud-functions.md](cloud-functions.md) for the
> adapters that *are* functions.

---

# The REST mechanism (current)

## Prerequisites

| | |
|---|---|
| Entitle tenant | able to create a REST integration |
| Dashboard reachable from Entitle | this endpoint is **inbound**, unlike the rest of the dashboard's Entitle usage |
| `entitle_user_jit_enabled` | Settings → Integrations → Entitle |
| `entitle_rest_secret` | a strong random string; **unset means the endpoint is closed, not open** |

## Setup

1. **Settings → Integrations → Entitle** → set **`entitle_rest_secret`** to a
   generated secret and enable **user JIT**.
2. In Entitle, create a **REST integration** pointing at your dashboard:

   ```
   get_assets_path           /api/entitle/rest/get_assets
   get_actors_path           /api/entitle/rest/get_actors
   get_all_permissions_path  /api/entitle/rest/get_all_permissions
   give_access_path          /api/entitle/rest/give_access
   revoke_access_path        /api/entitle/rest/revoke_access
   ```

   Auth — either spelling works:
   ```json
   { "headers": { "Authorization": "Bearer <entitle_rest_secret>" } }
   ```

3. There is **no `create_actor` / `delete_actor`**: dashboard users are real
   accounts that already exist, so this integration is not ephemeral. Entitle
   grants an existing actor a role.

4. Check it: `POST /api/entitle/rest/check_config` returns the scopes and levels it
   serves. Reaching it at all already proves the half of the configuration most
   likely to be wrong.

## What Entitle sees

**Assets** are permission scopes — one per scope, plus a separate one for
administrator:

```
dashboard:scope:aws      role_options: read, write, delete, use
dashboard:scope:k8s      …
dashboard:admin          role_options: admin
```

Administrator is its own asset deliberately: it is not a scope/level pair, it is the
`is_admin` flag, and keeping it out of the generic loop means granting it is always
an explicit act.

**Actors** are dashboard users — local and OIDC alike. An actor resolves by username
*or* email, case-insensitively.

## The isolation rule

**Entitle may only ever touch what Entitle granted.**

A user's permissions come from three independent sources:

| Source | Set by | Rewritten when |
|---|---|---|
| `permissions` | an admin, in the dashboard | never automatically |
| `session_permissions` | OIDC/Entra group mapping (the legacy mechanism) | **every login** |
| `jit_permissions` | this integration | only by this integration |

They are unioned into the user's effective permissions. This endpoint writes only
the third, which means:

- an operator's own grant **survives** an Entitle revoke
- a group-derived permission is the group's to remove, not Entitle's
- an Entitle grant **survives the user's next login** — which is why it is a
  separate column at all, since the login path overwrites `session_permissions`
  wholesale
- `get_all_permissions` reports **only Entitle's own grants**, so Entitle cannot
  reconcile away access it never gave

That third column is also what lets both mechanisms run side by side without
fighting each other.

## Authentication

A dedicated shared secret, **not** a Personal Access Token. A PAT inherits its
owning user's permissions, and an endpoint whose entire job is granting permissions
must not authenticate with a credential that already has some.

- Unset secret → **503**, closed. Never open by omission.
- Missing and wrong secrets return an **identical 401**, so probing tells nothing.
- Compared in constant time.

The endpoint also answers **503 rather than a 302** while the dashboard is still in
first-run setup, so Entitle reads "not yet" instead of an HTML redirect it can only
treat as an integration failure.

## Troubleshooting

**Every call returns 503 with `entitle_rest_not_configured`.** `entitle_rest_secret`
is unset. That is the endpoint being closed, which is the intended state until you
configure it.

**Every call returns 401.** The secret does not match. Both `Authorization: Bearer`
and `X-Entitle-Secret` are accepted, so it is the value rather than the header.

**Everything returns 404.** The router is gated on `entitle_user_jit_enabled`.

**A grant reports success but the user sees no change.** They need to reload —
permissions are read per request, so this is usually a stale page rather than a
failed grant. Confirm with `GET /api/entitle/rest/get_all_permissions`, which lists
exactly what Entitle currently holds.

**A revoke reports success but access remains.** Check which source granted it: an
admin-set baseline or an Entra group permission is untouched by an Entitle revoke,
by design. `get_all_permissions` shows only Entitle's grants — if the permission is
not listed there, Entitle did not grant it and cannot remove it.

**The audit log says "Failed to fetch the permissions" while accounts and resources
sync fine.** The response shape, not the connection. Entitle validates
`get_all_permissions` against `Get All Permission Response`, where both permission
fields are **maps keyed by asset id** — `actors_permissions` is
`map[asset_id] -> [{actor_id, role_code, direct_member}]` — even though the example
in the OpenAPI definition renders them as arrays. An array fails validation on this
one route only, and Entitle keeps whatever it last believed about who holds access.
Every adapter here is keyed correctly; `tests/test_entitle_permissions_shape.py`
keeps it that way.

**A user cannot be found.** Actors resolve by username or email. A user created by
OIDC auto-provisioning has a username derived from the email local-part, which may
not be the identifier Entitle sends — map the actor to the email instead.

---

# The Entra-group mechanism (legacy)

Still supported; not deprecated. Prefer REST for anything new, because it works for
non-Entra users and takes effect immediately.

Entitle adds the user to an Entra group named `dashboard-<scope>-<level>` with a
TTL. At the user's next login the group arrives in the OIDC `groups` claim, and the
login path maps it through `OAuthGroupMapping` rows into `session_permissions`.
When the TTL expires and Entitle removes the group, the *next* login recomputes the
union without it.

That "next login" step is the mechanism's defining limitation, in both directions: a
granted permission is not usable until the user signs in again, and a revoked one
remains usable until they do.

Setup lives in its own documents, which remain accurate:

- [Design](../design/entitle-user-jit.md) — the full model and its phases
- [Runbook: bootstrap Entra groups](../runbooks/entitle-user-jit-phase-1-bootstrap-entra.md)
- [Runbook: bootstrap Entitle](../runbooks/entitle-user-jit-phase-2-bootstrap-entitle.md)
- [Runbook: the permission resolver](../runbooks/entitle-user-jit-phase-0-resolver.md)

## Migrating to REST

No cutover needed, because the two write different columns:

1. Configure the REST mechanism alongside the existing groups.
2. Move scopes across one at a time — create the REST-backed Entitle resource, and
   remove the corresponding `dashboard-<scope>-<level>` group from Entitle's
   workflow when you are satisfied.
3. Leave the `OAuthGroupMapping` rows in place until no group grants anything, then
   delete them.

At no point does a user lose access mid-migration: `effective_permissions_dict` is
the union of all three sources, so a permission granted by either mechanism counts.
