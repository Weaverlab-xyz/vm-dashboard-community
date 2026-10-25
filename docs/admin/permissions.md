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

A person's account and a group mapping must each name a [role](#roles). The grid next to it
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

## The sections

The fourteen original scopes:

| Scope | Levels | Notes |
|---|---|---|
| `vms` | read, write | |
| `aws`, `azure`, `gcp`, `oci` | read, write, delete | |
| `images`, `containers`, `cloud_database`, `k8s`, `cloud_function` | read, write, delete | |
| `config_mgmt` | read, write | |
| `jobs` | read | read sees every job; cancelling and rescheduling are decided by ownership (the job's creator, or an administrator) |
| `workgroups` | read, write | `write` covers only workstreams you belong to or created; changing one's change window also needs `change_windows:write`; deleting one needs an administrator |
| `secrets` | use | the Secrets page itself is administrator-only |

**Retired levels.** Until September 2026 these fourteen offered all four levels whether or
not anything checked them, so the grid showed about thirty checkboxes that saved and
granted nothing, for example `vms:delete`, `jobs:write` and `secrets:read`. Those levels
are retired:
- A one-time migration removed them from every stored permission map, role and group
  mapping. Nobody's access changed, because they never granted anything.
- A grid or script that still sends one has it dropped instead of refused.
- An Entitle revoke of one succeeds.

If you ran `bootstrap_entitle_groups.py` before then, its Entra groups for those levels
(for example `dashboard-vms-delete`) are orphaned and can be deleted.

The rest are one per navigation section:

| Scope | Levels | Covers |
|---|---|---|
| `pov` | read, write, delete, use | POV environments, their use cases, wiring, sharing and accessors. `delete` destroys any POV the user's POV access picker allows, and every POV when the picker is empty |
| `pov_own` | read, write, delete | your **own** POVs: ones **you created**, or ones **assigned to you** in your POV access picker. `read` without the general `pov:read` means you **see only your own** POVs, on the POV page, the home page and Inventory alike; `write` lets you create a POV and set up, run, power and share your own; `delete` lets you destroy your own. Nothing on anyone else's POV, and nothing platform-wide (re-checking the platform, listing every environment on it). What the POV Presenter role carries |
| `pov_templates` | read, write, delete | template builds, blueprints, and the BeyondTrust tenant registry |
| `proxmox` | read, write, delete | Proxmox: browse, deploy, import an image, delete a VM |
| `nutanix` | read, write, delete | Nutanix: the same |
| `vsphere` | read, write | vSphere: browse and power. No delete — nothing here destroys |
| `hyperv` | read, write | Hyper-V: the same |
| `xcpng` | read, write | XCP-ng: the same |
| `connections` | read, write, delete | hypervisor connection records |
| `storage` | read, write, delete | the object store's data plane. Its *configuration* stays admin-only |
| `costs` | read, write | Cloud Costs, budgets and spend caps |
| `inventory` | read | the cross-cloud inventory. Read-only: acting on a resource is the owning cloud's scope |
| `agents` | read, write, delete | the remote-agent operator API. The agent-facing protocol authenticates by signature and is unaffected |
| `audit` | read | the audit log, so it can be handed to whoever reads it without making them an admin |
| `gateways` | read, write, delete | PRA Gateways — deploy, list and tear down |
| `notifications` | read, write | outbound webhook endpoints, on the **Notifications** page. The delivery log is administrators only |
| `epml` | read, write | EPM for Linux package builds |
| `ot` | read, write, delete | the OT demo cell and its protocol tunnels. Building a cell also needs the cloud's own `write` |
| `change_windows` | write, use | `write` defines change windows and recurring schedules; `use` approves a booked change. No `read`: every run form reads windows. Both levels need an **explicit** grant — see [If a user reports a 403](#if-a-user-reports-a-403) |

Preview features are turned on and off in Settings → Preview features. All but one have
no scope of their own; each is gated like this:

| Preview feature | Gate |
|---|---|
| Virtual Desktops | administrator |
| Certificate Lab, SPIRE Lab | `cloud_function:read` to see, `cloud_function:write` to change |
| Agent Cell | `config_mgmt:write` |
| Directories | its own `directories` scope (read, write, delete), which must be granted explicitly. See [Directories → Permissions](../preview/directories.md#permissions) |

## Giving a customer read access to their own POV

This is the case the model previously could not express, and there are two ways to do it.
They are for different people.

### A named person who should also be a real user

Give them a normal login with:

- **Permissions:** `POV` → `read` and `use`. Nothing else.
- **POV access:** their environment, chosen in the picker under the grid.

They can then open the POV page, see exactly one POV — theirs — and check off use cases
against it. Any other POV answers *"No such POV environment"*, the same as an id that does
not exist, because confirming that somebody else's POV exists is itself a leak.

Leave the POV access picker **empty** and they see every POV. That is the default, and it
is what every pre-existing user has.

The picker also decides what a POV Presenter **owns**, along with the POVs they create
themselves, and a presenter sees only what they own. An empty picker therefore shows a
presenter only the POVs they created. Add a colleague's POV to their picker to let them
co-present it. When a presenter whose picker names some POVs creates a new one, it is
added to their picker automatically.

To let someone see and manage **every** POV, give them the **POV Manager** role, or the
general `pov` levels.

`use` is the important half. With `read` alone they can look but not tick, and a use-case
checklist nobody can tick is a screenshot. With `write` they could provision and destroy.

### An anonymous prospect who should not have a dashboard login at all

Use a **POV accessor** instead — see [Customer access to a POV](../profiles/pov/customer-access.md).
An accessor is an ephemeral credential bound to one environment, able to reach five
endpoints and nothing else in the product, and deleted when the POV is reaped. It is not a
restricted user; it is a different kind of principal.

Rule of thumb: if you would put their name in an org chart, give them a user with a POV
grant. If you are emailing a link to an evaluation, mint an accessor.

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

## Groups

A group mapping turns a group from your identity provider into a workstream, a role
(required), and a default permission set added on top of it. **Not Entra-only** — any OIDC provider works, and the
"Group Object ID" field takes whatever identifier that provider's groups claim emits. The
stored column is still called `entra_group_id`, which is why you will see that name in the
API and in the database.

Things to know:

- **If any mappings exist, signing in requires a match.** An OIDC user in none of the
  mapped groups is refused at sign-in (`not_authorized`). With no mappings configured,
  only users who already have an account can sign in.
- **A login also rewrites the user's workstreams** to the matched mappings' workstreams.
  Workstreams set by hand on an OIDC user do not survive their next sign-in.
- The grid is labelled "Default Permissions for new users", but it is not only for new
  users. Its permissions are re-applied to every member **at each login**, so a mistake in a
  mapping keeps reasserting itself until the mapping is fixed — and a change you make
  reaches existing members at their *next sign-in*, not immediately.
- That includes the role. Editing a role changes it immediately for users who hold it
  directly, and at next sign-in for everyone who gets it through a group.

## Roles

A role is a named set of permissions you assign to a person or to a group, instead of
ticking boxes for each of them. Editing the role changes it for everyone who holds it.

Ten roles ship with the dashboard:

| Role | For |
|---|---|
| **Administrator** | Everything, including the admin-only pages. The grid is not consulted. |
| **Operator** | Day-to-day work: deploy, run and use, but delete nothing. It does not include the audit log or change windows. |
| **Read-Only** | Every section at its read level, and nothing else. `secrets` and `change_windows` offer no read level, so neither is included. |
| **POV Presenter** | Run a proof of value end to end: **create POVs**, then see, set up, run, power, share and destroy **their own**, meaning ones they created or ones assigned to them in the POV access picker, and tick use cases. **Other people's POVs are not visible**, and neither are the platform-wide controls. It holds `pov:use` and `pov_own:read`/`write`/`delete`, but none of the general `pov:read`, `pov:write` or `pov:delete`. |
| **POV Manager** | Oversee every proof of value: see, set up, run, power, share and destroy **any** POV, and curate the POV templates. It holds every `pov` level plus `pov_templates:read`/`write`. |
| **POV Template Builder** | Author what presenters build from: build and delete POV templates, save and delete blueprints, and maintain the BeyondTrust tenant registry. It **cannot create POVs**. It holds `pov_templates:read`/`write`/`delete` and `pov_own:read`, which opens the POV page (and its Tenants tab) without showing anyone else's POVs. Give it alongside **POV Presenter** to someone who does both. |
| **Auditor** | The audit trail, job history and inventory. No writes. |
| **Cloud Admin** | Full control of the cloud accounts and what runs in them. |
| **DBA** | Cloud databases end to end, plus the secrets a database run needs. |
| **Platform / K8s** | Clusters, containers, functions, and the images and configuration behind them. |

**Built-in roles cannot be edited or deleted.** Use **Clone** and change the copy — that
keeps what the shipped roles mean stable, so a support conversation about "the Auditor
role" is about the same thing on every install. A custom role can be edited and deleted
freely; deleting one that is still assigned asks first, and the people who held it keep
only whatever their own permission grid grants. They then show **No role**, and the next
edit to each of them has to choose one.

**A role and the grid add up.** The grid on a user or a group mapping is an *override*
layered on top of their role, not a replacement for it — so the usual shape is "give them
Operator, plus `storage:delete` because they look after the share". If you want someone to
have exactly their role and nothing more, leave the grid untouched.

**There is no "Full access" role**, and that is deliberate. A role holding every section
would hand everyone assigned to it every permission in the dashboard, and that is hard to
see from the role's name. Use the Administrator role, or the Admin flag on the user, both of
which are visible for what they are.

**Who holds this role?** Click the count in the *Assigned to* column. That question used to
mean opening every user in turn, and it is most of the reason roles exist.

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
  it (see [The sections](#the-sections)).
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

## Objects, not just areas

A scope says *what*, not *which*. Two mechanisms narrow the *which*:

- **Workstreams** tag resources and users; the cloud, container, Databases and Kubernetes
  lists show you the rows whose workstream you are in. Untagged resources are visible to
  whoever deployed them. They live on the **RBAC &rarr; Workstreams** tab, beside the
  permission tabs, because the two answer the two halves of the same question.

  Unlike the other RBAC tabs, Workstreams is **not admin-only**: it has a real `workgroups`
  scope, so a user granted `workgroups:read` sees that tab and nothing else on the page.
  Deleting a workstream still needs an administrator.

  `workgroups:write` reaches only the workstreams you are a **member of or created**:
  editing one, or adding and removing its members. Otherwise a holder could add
  themselves to any workstream and act on everything tagged there. Its **change window**
  fields (*Change window*, *Require it*) also need `change_windows:write`, because
  clearing that gate allows changes outside the window. Creating a workstream, updating
  one and every membership change are written to the audit log.

  Its **deploy defaults** take the same authority as editing it, and are audited too. They
  are a fixed list holding no secrets — see
  [Workstream deploy defaults](../clouds/vms/pra.md#workstream-deploy-defaults). Its **network
  defaults** take the same authority — see
  [Workstream network defaults](../clouds/vms/pra.md#workstream-network-defaults).
  Its **bound secrets** (an SSH key secret per cloud and a PRA credential) are
  **administrator-only** to change; members can see which are bound. See
  [Workstream secret bindings](../clouds/vms/pra.md#workstream-secret-bindings). A registered vault can be
  limited to a workstream by an administrator, so only that workstream's deploys read it —
  see [Vaults](secrets-management.md#vaults-and-a-workstreams-own-vault).
  Its **post-deploy runs** (assets run on every VM deployed into it) are administrator-only
  to change too — see [Workstream post-deploy runs](../clouds/vms/pra.md#workstream-post-deploy-runs).
  **Stored assets** can be limited to one workstream by an administrator; only its
  members then see, run or replace them — see
  [Scoping an asset to a workstream](../operations/storage-management.md#scoping-an-asset-to-a-workstream).

  **Workstreams were called workgroups** until this release. The new name avoids a clash
  with Password Safe, whose own *workgroups* (the container for managed systems) appear in
  the same Settings forms; a field labelled "Workgroup" in Settings now always means the
  Password Safe one. Only the name a person reads changed. These keep the old spelling
  so scripts, policies and live resources need no change: the `/api/workgroups` routes and
  every `workgroup` field in the API, the `workgroups` permission scope, the `Workgroup`
  tag written on cloud resources, and `/workgroups`, which still opens the tab
  (`/workstreams` is its name now).
- **POV access** narrows the POV pages to named environments, as described above.

"Untagged resources are visible to whoever deployed them" is the whole rule, and it is
worth reading twice: a workstream is a property of the **row**, not of the page. A cloud
database or Kubernetes cluster carries one only once somebody assigns it — at creation, or
later with the admin-only **Workstream** button on [Databases](../cloud-services/databases.md) /
[Kubernetes](../cloud-services/kubernetes.md). Until then the row is creator-scoped, which is why adding the
field granted and revoked nothing on upgrade.

Tagging a row widens what its workstream can **do** to it, not only what they can see: the
list, every by-id action, a Configuration Management run against it, and its auto-delete
timer all answer to the same rule. Sharing a database with your team means the team can run
plays against it and extend its timer.

Functions, Certificates and SPIRE remain creator-scoped for non-admins — they show you what
you created, and that is not configurable.

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

## Adding a scope (for contributors)

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
