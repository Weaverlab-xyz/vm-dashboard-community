# Entitle user identity (REST)

Just-in-time **dashboard permissions**, granted by Entitle over REST. This is the
one Entitle integration the dashboard hosts itself — every other one is a Cloud
Function, because Entitle needs an endpoint and the target is elsewhere. Here the
dashboard **is** the target system, so a function hop would add a network round
trip, a second credential and a second thing to deploy, and buy nothing.

> Gated by `entitle_user_jit_enabled`, and additionally closed whenever
> `entitle_rest_secret` is unset. Design notes:
> [docs/design/cloud-functions.md](../design/cloud-functions.md).

## What it replaces

Previously Entitle granted dashboard access **through Entra groups**: it added the
user to `dashboard-<scope>-<level>`, the group arrived in the OIDC `groups` claim,
and the login path mapped it to permissions. That works, and it stays supported —
but it has two costs:

- **It only works for Entra.** Local users and any other OIDC provider could be
  granted nothing, which undercuts the point of the generic-OIDC support.
- **Grants and revokes both take effect at the user's next login.** So "just in
  time" was, in practice, "some time after you next sign in".

The REST integration grants directly and immediately, for any user the dashboard
knows about — local or OIDC, any provider.

**Both mechanisms can run at once.** They write different columns, so migrating off
Entra groups is a gradual change rather than a cutover: point new scopes at REST,
leave the existing groups in place, and retire them when you are ready.

## Prerequisites

| | |
|---|---|
| Entitle tenant | able to create a REST integration |
| Dashboard reachable from Entitle | this endpoint is inbound, unlike the rest of the dashboard's Entitle usage |
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
| `session_permissions` | OIDC/Entra group mapping | **every login** |
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

**A user cannot be found.** Actors resolve by username or email. A user created by
OIDC auto-provisioning has a username derived from the email local-part, which may
not be the identifier Entitle sends — map the actor to the email instead.
