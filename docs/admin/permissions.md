# Permissions

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are deciding what a user may see or do — and especially before you tick "Full access" or hand a POV to a customer stakeholder.

Part of [Identity and access](../admin.md).

Two independent questions, and keeping them apart is the whole model:

| Question | Answered by | Where |
|---|---|---|
| What may this user **do**? | a **role**, or a **scope** and a **level** | RBAC &rarr; Users / Groups / Roles |
| Which **objects** may they do it to? | a workstream tag, or a POV grant | RBAC &rarr; Workstreams; the POV access picker |

**Administrator**, on this page and in the app, means any one of four things: the **Admin**
flag on the user, the built-in **Administrator** role, a group mapping whose role or
permissions confer admin, or an admin grant from Entitle. Every admin check honours all
four. (Until September 2026 the cloud consoles, the hypervisor pages, job cancel and
reschedule, and the Secrets page honoured only the flag.)

A scope is a feature area — roughly one per section in the navigation. A level is
`read`, `write`, `delete` or `use`. A grant is a scope plus a level: `pov:read`,
`storage:write`, `cloud_database:delete`.

---

## An empty permission map grants nothing

A user with no permissions from **any** source can sign in and see nothing. A user's
effective permissions are the union of four maps (see
[below](#where-a-users-permissions-actually-come-from)), and a scope absent from all of them
is denied.

**This changed.** Until this release an empty map meant *unrestricted*: every section except
the Admin-only ones. That was backward compatibility for accounts that predate permissions,
and it is how an SSO group mapping saved with no permissions handed every member the
storage, inventory and credential routes. On upgrade, a one-time migration wrote an
explicit **Full access** map onto every user and group mapping that relied on the old rule,
so nobody's access changed. The migration records what it converted in `schema_markers`
(`rbac_empty_means_nothing_v1`). Users holding that map show every box ticked in the grid,
so tighten them there.

**"Full access"** in the grid is no longer stored as an empty map. It is written out as
every section and level, except the authorities that only an administrator could grant
before: `agents`, `audit`, `change_windows`, `connections`, `costs`, `directories`,
`notifications`, `pov_own`, the `write` and `delete` levels of `pov_templates`, and
`jobs:read` (seeing everyone's jobs, not just your own). An empty map never granted those
either. A scope added in a later release is **not** included in a map written earlier:
grant it when it arrives.

So the grid's states are:

- **"Full access" ticked:** the user gets the full map above.
- **Some sections ticked:** the user gets exactly those.
- **Nothing ticked:** the map lists every section with nothing granted, so every scope is
  denied.

A role, a group mapping's permissions and an Entitle grant all add to the user's own map.
None of them can take anything away from it.

## Every person and every group mapping has a role

A person's account and a group mapping must each name a [role](permissions/roles.md). The grid next to it
is an addition **on top of** that role, never a substitute for one. Access that lived only
in a grid was a second, unnamed permission set that no role edit could reach.

- **Creating** a user or a mapping without a role is refused (`422`, "Choose a role").
  Through the API that means sending `role_id`.
- **A role cannot be cleared** from a person or a mapping. Move it to another role instead.
- **Unticking Admin** (or moving off the Administrator role) has to land on a role in the
  same save.
- **Rows saved before this rule keep working.** They sign in and keep their access, and
  RBAC marks them with an amber **No role** badge. The next edit to one of them has to choose
  a role. Activating or deactivating an account, resetting its password and the
  **Sign in with SSO** switch are not edits, and work as before.

Exempt, because this page does not author their access:

- the **Admin** flag, which is itself the whole grant;
- **service accounts**, which can still be granted with a map alone;
- accounts **single sign-on provisioned**. Their access is rebuilt from their group
  mapping, and so from that mapping's role, at every sign-in;
- **your own row**, because you cannot change your own role.

## New users start restricted

Creating a user from **RBAC → Users → + New User** gives them their role and a permission
map with nothing granted on top of it. The grid is on the create panel so you can add what
the role does not cover before you save.

An account created through the API without a `permissions` field also starts with nothing
granted beyond its role. Send `{}` for Full access, or a map.

**Passwords can be at most 72 bytes.** bcrypt, which hashes them, reads no further, so a
longer one is refused when it is set (here, on edit, in the setup wizard and on a headless
import) rather than accepted with its end ignored. Bytes, not characters: an accented letter
is 2, an emoji 4. A longer password set before this limit still signs in: it was always
checked on its first 72 bytes, and still is.

**Service accounts** are granted with a map or a role, never "Full access". A
[service account](service-accounts.md) is a workload principal that authenticates with an
OAuth client. It can never be an administrator either. An OAuth access token may carry a
`scope` that narrows the account's permissions further for that one token.

## Removing admin leaves nothing granted

Unticking **Admin** on a user, or moving them off the **Administrator** role, leaves them
with **only the role you choose** in that save (one is
[required](#every-person-and-every-group-mapping-has-a-role)). The grid resets to nothing
ticked, with "Full access" unticked, so nothing the grid held before comes back. Tick
anything they need beyond the role in the same save, or later.

Two things behind it are worth knowing:

- **The server enforces it too.** A demotion through the API that does not send
  permissions stores "nothing granted". Permissions sent in the same request are used
  as sent, and a role assigned in the same request still grants.
- **Old grants do not come back.** The Admin flag bypasses the grid, so anything stored
  there before the user became an admin has gone unseen since. Demotion clears it rather
  than silently restoring it.

An administrator is also created with nothing granted underneath the flag, so unticking
Admin later leaves them with nothing.

**Group mappings grant their role and nothing more by default.** A new mapping on RBAC →
Groups must name a role; its grid adds sections, or "Full access", on top. A member
who matches several mappings gets the union of what they grant. A mapping that the upgrade
converted from "no permissions" keeps its old behaviour exactly: it grants Full access only
to a member whom no other matched mapping grants anything. Saving the mapping from the grid
turns it into an ordinary map. Groups listed in the `.env` `AZURE_OAUTH_GROUP_MAP` (used
only when no mappings exist on the Groups page) still grant Full access, because that file
cannot express permissions.

## Levels

| Level | Means |
|---|---|
| `read` | see the page and its data |
| `write` | create and modify |
| `delete` | destroy |
| `use` | take part without managing — see below |

`use` exists for the cases where "read" is too little and "write" is far too much. Three
scopes offer it:

- **`secrets:use`** — run an Ansible playbook that reads a secret out of a vault, without
  ever being shown the value.
- **`pov:use`** — tick off use cases in a POV you have been given, without being able to
  create, destroy, share, power or add logins to it. Powering an environment, waking a
  suspended one included, is `pov:write`: the route takes an arbitrary runstate, so it
  stops and suspends as readily as it starts. A *POV accessor* has a separate start-only
  wake of its own — see [Customer access to a POV](../profiles/pov/customer-access.md) — and
  a stakeholder who must be able to wake their own POV needs `write` or an accessor
  alongside.
- **`change_windows:use`** — approve a change booked into a change window. Deliberately a
  different authority from `change_windows:write`, which maintains the calendar. See
  [Change Windows](../operations/scheduling/change-windows.md).

**Every scope offers exactly the levels something enforces**, so every checkbox on the grid
does something. vSphere has nothing to delete, so it shows no Delete checkbox. If you send a
level a scope does not offer through the API you get a `422` naming the levels it does
offer. The one exception is a *retired* level (see the next section), which is dropped
silently, because it never granted anything.

## Pages

The larger parts have a page each:

| Page | Read it when |
|---|---|
| [The permission sections](permissions/sections.md) | you are deciding which sections of the dashboard a user may read or change, and what each one covers |
| [Giving a customer read access to their POV](permissions/customer-read-access.md) | a customer stakeholder needs to see their own POV and nothing else |
| [Permission groups](permissions/groups.md) | you are mapping identity-provider groups to dashboard access |
| [Roles](permissions/roles.md) | you are giving people access through a role rather than one permission at a time |
| [Permissions on objects, not just areas](permissions/objects.md) | you need access to one VM, database or POV rather than a whole area of the dashboard |
| [Adding a permission scope](permissions/adding-a-scope.md) | you are adding a new area to the dashboard and it needs its own permission scope |

## Where a user's permissions actually come from

Four sources, unioned. A level granted by any of them counts.

| Source | Set by | Lifetime |
|---|---|---|
| Role | an admin, on RBAC &rarr; Roles | until the role changes or is unassigned |
| Baseline | an admin, on RBAC &rarr; Users | until changed |
| Group-derived | OIDC group membership → group mapping | **rewritten on every login** |
| Just-in-time | Entitle, over the REST integration | until Entitle revokes |

The middle row is worth understanding: signing in **replaces** the group-derived set
outright. That is how losing a group membership actually reduces access — but it also means
anything written there by hand disappears at the user's next login. Per-POV grants are
stored separately for exactly this reason, so they survive.

## Scopes that are deliberately not grantable

Some things answer only to the **Admin** flag, because a grantable version of them would be
a way to become an administrator:

- **RBAC (Users, Groups, Roles)** — anyone who can edit a user, or a role a user
  holds, can make themselves admin. The fourth tab, **Workstreams**, is the exception: it
  scopes *objects* rather than actions, so it has a grantable `workgroups` scope. The page
  is `/rbac`. `/users`, `/groups` and `/workstreams` (or the older `/workgroups`) still resolve, straight to their tab,
  so older bookmarks and runbook links keep working.
- **Settings / first-run setup**, and **Secrets Management** — the whole page, including
  the vault registry. The `secrets` scope's `read`, `write` and `delete` levels do not open
  it (see [The sections](permissions/sections.md)).
- **Virtual Desktops**, while it is a preview.
- **Worker concurrency and preflight**, which are instance-wide plumbing.
- **[Health](health.md)**, one view over every tenant, onboarded account
  and agent across all workstreams.

Two more have no scope for narrower reasons:

- **The auto-delete timer.** Authorization there is visibility: anyone who can see a
  resource may **delay** its deletion, by extending it or setting a later date. Only an
  administrator may bring a deletion forward: an earlier date, a timer on a resource that
  had none, or an extend the lifetime ceiling would clamp below the current expiry.
  Clearing a timer outright needs an administrator **and** the
  `resource_expiry_allow_never` setting. See [Auto-delete Timer](../operations/auto-delete-timer.md).
- **The Dashboard home page**, which is an aggregate of things you already have access to.

## What the menu shows

A page whose data a scope grants appears in the menu for **anyone who holds that scope**,
not only for administrators. Granting the scope through a role or a group mapping is
enough; nobody has to know a URL.

| Menu link | Shown to |
|---|---|
| **Templates** (POV) | `pov_templates` **write**. `read` alone is what lets a POV Presenter pick a template when building a POV, so it does not open the builder. |
| **Agents** | `agents` read |
| **Audit** | `audit` read |
| **Storage** | `storage` read |
| **Costs** | `costs` read |
| **Images** | `images` read |
| **Notifications** | `notifications` read, while the feature is on |
| **Health**, **Secrets**, **RBAC**, **Desktops** | administrators only. Their routes are administrator-only too (see [above](#scopes-that-are-deliberately-not-grantable)). |

The menu is a convenience, never the control: every route still checks its own permission,
so a hidden link never means a reachable page and a shown one never means more than the
scope allows. The browser learns your permissions when you sign in and re-checks them every
few minutes, so a role an administrator changes reaches the menu without signing out. A
group mapping's grants still change only at your next sign-in, because that is when they
are applied.

Within a page, buttons follow the same rule. **Register in Entitle** on a cloud function
shows for `cloud_function` write. A workstream's **Delete** is for administrators only,
because deleting one is.

**Settings → Integrations** is administrators only, because it configures the instance.
Features with a scope of their own therefore have a screen outside it, and the Settings
drawer keeps only the instance settings:

| Feature | Where | Needs |
|---|---|---|
| Notification endpoints | **Notifications** page (`/notifications`) | `notifications` read to list them, write to add, test, edit or delete one. The delivery log and the delivery settings stay in Settings. |
| Change windows | **Schedules** page, *Change windows* | anyone can see them, because every run form offers them; `change_windows` write adds, edits or deletes one. The approval settings and the grace period stay in Settings. |
| The agents' signing audience, and the dashboard's SPIFFE identity | **Agents** page, *Identity* tab | `agents` read to see them; write to reset the audience or check the issuer. Public base URL and the SPIFFE audiences stay in Settings. |
| Password Safe DB-Ops services | **Databases** page, *Password Safe DB-Ops services* | `cloud_database` read to see them; Deploy and Sync invokers need `cloud_database` write **and** `cloud_function` write, because they deploy a function. The invoker list stays in Settings. |
| Rancher and Portainer node firewalls | **Containers** page, *Show firewall sources* | `containers` read |

## If a user reports a 403

1. Does the user hold **anything**? An empty map grants nothing (see
   [above](#an-empty-permission-map-grants-nothing)). Check their own grid, their role, their
   group mappings and any Entitle grant.
2. Does the message name a scope and level (*"Requires 'storage:write' permission"*)? Grant
   that pair. If the message says *"or administrator"*, the grant must be named:
   "Full access" does **not** include it, because that route used to require the Admin
   flag.
3. For a POV answering *"No such POV environment"* on a POV you know exists: check the POV
   access picker on that user. An id they were not granted is indistinguishable from one
   that does not exist, on purpose.
4. For an OIDC user whose permissions keep reverting: the group mapping is overwriting them
   at each login. Fix the mapping, not the user.

