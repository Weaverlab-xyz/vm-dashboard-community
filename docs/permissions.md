# Permissions

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are deciding what a user may see or do — and especially before you tick "Full access (unrestricted)" or hand a POV to a customer stakeholder.

Two independent questions, and keeping them apart is the whole model:

| Question | Answered by | Where |
|---|---|---|
| What may this user **do**? | a **role**, or a **scope** and a **level** | RBAC &rarr; Users / Groups / Roles |
| Which **objects** may they do it to? | a workgroup tag, or a POV grant | RBAC &rarr; Workgroups; the POV access picker |

A scope is a feature area — roughly one per section in the navigation. A level is
`read`, `write`, `delete` or `use`. A grant is a scope plus a level: `pov:read`,
`storage:write`, `cloud_database:delete`.

---

## The rule that surprises people

**An empty permission map means unrestricted, not "nothing".**

A user with no permissions from **any** source can do everything except the things that
need the Admin flag. "Any source" matters: a user's effective permissions are the union of
four maps (see [below](#where-a-users-permissions-actually-come-from)), and only when all
four are empty is the result unrestricted. This is deliberate backward compatibility: the permission columns were added
long after the first users were, and treating "unset" as "denied" would have locked those
accounts out of work they were already doing.

The consequence is that the grid has two very different empty states:

- **"Full access (unrestricted)" ticked** — the user's own map is NULL. If nothing else
  grants them anything, every scope is allowed, now and for every scope added in future.
  **But if they also hold a role, a group mapping's permissions or an Entitle grant, they
  get exactly that union and nothing more** — the ticked box contributes nothing, it does
  not widen them to everything. To give such a user full access, use the Admin flag or
  the Administrator role.
- **Unticked with nothing checked** — the map lists every section with nothing granted
  against any of them. Every scope is denied.

So the way to restrict somebody is to untick "Full access" and then grant what they need.
Leaving it ticked and unchecking boxes underneath does nothing, because the boxes are not
being read.

The second state used to be unreachable, and that is worth knowing if you are reading an
older map. The obvious payload for "restricted, nothing granted" is an empty object, an
empty object is stored as NULL, and NULL is the *first* state — so asking for nothing got
you everything. The grid now writes out every section explicitly, each with its own list of
granted levels, so the map is never empty unless you asked for full access. A section
absent from a map is still a denial, which is what makes an older, shorter map keep
behaving exactly as it did.

## New users start restricted

Creating a user from **RBAC → Users → + New User** gives them a permission map with nothing
granted. The grid is on the create panel for that reason: grant what they need before you
save, or they will be able to sign in and see nothing. The panel says so when you are about
to create one that way.

Before this, the create form had no grid at all and saved no map, which left the account in
the NULL state above — every section, every level, for anyone the admin added. Tick
"Full access (unrestricted)" if that is genuinely what you want.

**Group mappings are the opposite.** A new mapping on RBAC → Groups starts with "Full
access (unrestricted)" ticked, and the panel says auto-provisioned users will have
unrestricted access. An OIDC user whose only source is such a mapping (no role on the
mapping, nothing granted on their own row) is unrestricted. Untick it, or give the mapping
a role, before members sign in.

## Levels

| Level | Means |
|---|---|
| `read` | see the page and its data |
| `write` | create and modify |
| `delete` | destroy |
| `use` | take part without managing — see below |

`use` exists for the cases where "read" is too little and "write" is far too much. It
does something on exactly three scopes:

- **`secrets:use`** — run an Ansible playbook that reads a secret out of a vault, without
  ever being shown the value.
- **`pov:use`** — tick off use cases in a POV you have been given, without being able to
  create, destroy, share, power or add logins to it. Powering an environment, waking a
  suspended one included, is `pov:write`: the route takes an arbitrary runstate, so it
  stops and suspends as readily as it starts. A *POV accessor* has a separate start-only
  wake of its own — see [Customer access to a POV](profiles/pov/customer-access.md) — and
  a stakeholder who must be able to wake their own POV needs `write` or an accessor
  alongside.
- **`change_windows:use`** — approve a change booked into a change window. Deliberately a
  different authority from `change_windows:write`, which maintains the calendar. See
  [Change Windows](scheduling/change-windows.md).

On every other scope that offers it, `use` is currently a checkbox that grants nothing —
see the next section.

Not every scope offers every level. The scopes added one per navigation section offer
only the levels something enforces: vSphere has nothing to delete, so it shows no Delete
checkbox. If you send a level a scope does not offer through the API you get a `422`
naming the levels it does offer.

The original fourteen are the exception — see the next section.

## The sections

The fourteen original scopes — `vms`, the four clouds, `images`, `containers`,
`config_mgmt`, `jobs`, `workgroups`, `secrets`, `cloud_database`, `k8s` and
`cloud_function` — all offer all four levels, whether or not anything checks them. These
checkboxes save and then **grant nothing**, because no route or check reads them:

| Scope | Levels that currently do nothing |
|---|---|
| `vms` | `delete`, `use` |
| `aws`, `azure`, `gcp`, `oci`, `images`, `containers`, `cloud_database`, `k8s`, `cloud_function` | `use` |
| `config_mgmt` | `delete`, `use` |
| `jobs` | `write`, `delete`, `use` — cancelling and rescheduling a job are decided by ownership (the job's creator, or the Admin flag), not by scope |
| `workgroups` | `delete`, `use` — deleting a workgroup needs the Admin flag |
| `secrets` | `read`, `write`, `delete` — the Secrets page is admin-only throughout; only `use` does anything |

They are kept because narrowing an offered level would make every stored map that holds it
fail validation (`422`) the next time an admin saved it. Granting them is harmless; just do
not rely on them.

The rest are one per navigation section:

| Scope | Levels | Covers |
|---|---|---|
| `pov` | read, write, delete, use | POV environments, their use cases, wiring, sharing and accessors |
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
| `notifications` | read, write | outbound webhook endpoints and delivery history |
| `epml` | read, write | EPM for Linux package builds |
| `ot` | read, write, delete | the OT demo cell and its protocol tunnels. Building a cell also needs the cloud's own `write` |
| `change_windows` | write, use | `write` defines change windows and recurring schedules; `use` approves a booked change. No `read`: every run form reads windows. Both levels need an **explicit** grant — see [If a user reports a 403](#if-a-user-reports-a-403) |

Preview features have no scope of their own. They are turned on and off in Settings →
Preview features, and each is gated like this:

| Preview feature | Gate |
|---|---|
| Virtual Desktops | Admin flag |
| Certificate Lab, SPIRE Lab | `cloud_function:read` to see, `cloud_function:write` to change |
| Agent Cell | `config_mgmt:write` |

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

`use` is the important half. With `read` alone they can look but not tick, and a use-case
checklist nobody can tick is a screenshot. With `write` they could provision and destroy.

### An anonymous prospect who should not have a dashboard login at all

Use a **POV accessor** instead — see [Customer access to a POV](profiles/pov/customer-access.md).
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

A group mapping turns a group from your identity provider into a workgroup, an optional
role, and a default permission set. **Not Entra-only** — any OIDC provider works, and the
"Group Object ID" field takes whatever identifier that provider's groups claim emits. The
stored column is still called `entra_group_id`, which is why you will see that name in the
API and in the database.

Things to know:

- **If any mappings exist, signing in requires a match.** An OIDC user in none of the
  mapped groups is refused at sign-in (`not_authorized`). With no mappings configured,
  only users who already have an account can sign in.
- **A login also rewrites the user's workgroups** to the matched mappings' workgroups.
  Workgroups set by hand on an OIDC user do not survive their next sign-in.
- The grid is labelled "Default Permissions for new users", but it is not only for new
  users. Its permissions are re-applied to every member **at each login**, so a mistake in a
  mapping keeps reasserting itself until the mapping is fixed — and a change you make
  reaches existing members at their *next sign-in*, not immediately.
- That includes the role. Editing a role changes it immediately for users who hold it
  directly, and at next sign-in for everyone who gets it through a group.

## Roles

A role is a named set of permissions you assign to a person or to a group, instead of
ticking boxes for each of them. Editing the role changes it for everyone who holds it.

Eight roles ship with the dashboard:

| Role | For |
|---|---|
| **Administrator** | Everything, including the admin-only pages. The grid is not consulted. |
| **Operator** | Day-to-day work: deploy, run and use, but delete nothing. It does not include the audit log or change windows. |
| **Read-Only** | Every section at its read level, and nothing else. `change_windows` offers no read level, so it is not included. |
| **POV Presenter** | Run a proof of value — tick use cases, and read the environments and estate behind them. Pair it with the POV access picker. It holds `pov:read` and `pov:use` but not `pov:write`, so it **cannot wake or power** an environment. The role's own description in the app says it can; that description is wrong. |
| **Auditor** | The audit trail, job history and inventory. No writes. |
| **Cloud Admin** | Full control of the cloud accounts and what runs in them. |
| **DBA** | Cloud databases end to end, plus the secrets a database run needs. |
| **Platform / K8s** | Clusters, containers, functions, and the images and configuration behind them. |

**Built-in roles cannot be edited or deleted.** Use **Clone** and change the copy — that
keeps what the shipped roles mean stable, so a support conversation about "the Auditor
role" is about the same thing on every install. A custom role can be edited and deleted
freely; deleting one that is still assigned asks first, and the people who held it keep
only whatever their own permission grid grants.

**A role and the grid add up.** The grid on a user or a group mapping is an *override*
layered on top of their role, not a replacement for it — so the usual shape is "give them
Operator, plus `storage:delete` because they look after the share". If you want someone to
have exactly their role and nothing more, leave the grid untouched.

**There is no "unrestricted" role**, and that is deliberate. Unrestricted is stored as an
empty permission map, and an empty map on a *person* means full access — so an unrestricted
role would hand everyone who holds it every permission in the dashboard, including every
section added in future. Use the Administrator role, or the Admin flag on the user, both of
which are visible for what they are.

**Who holds this role?** Click the count in the *Assigned to* column. That question used to
mean opening every user in turn, and it is most of the reason roles exist.

## Scopes that are deliberately not grantable

Some things answer only to the **Admin** flag, because a grantable version of them would be
a way to become an administrator:

- **RBAC (Users, Groups, Roles)** — anyone who can edit a user, or a role a user
  holds, can make themselves admin. The fourth tab, **Workgroups**, is the exception: it
  scopes *objects* rather than actions, so it has a grantable `workgroups` scope. The page
  is `/rbac`. `/users`, `/groups` and `/workgroups` still resolve, straight to their tab,
  so older bookmarks and runbook links keep working.
- **Settings / first-run setup**, and **Secrets Management** — the whole page, including
  the vault registry. The `secrets` scope's `read`, `write` and `delete` levels do not open
  it (see [The sections](#the-sections)).
- **Virtual Desktops**, while it is a preview.
- **Worker concurrency and preflight**, which are instance-wide plumbing.

Two more have no scope for narrower reasons:

- **The auto-delete timer.** Authorization there is visibility: anyone who can see a
  resource may change its timer. Extending by a number of hours only ever delays a
  deletion. Setting an **absolute date** is also open to them, though, and that date may be
  *earlier* than the current one — no sooner than 60 minutes from now. So seeing a resource
  is enough to bring its deletion forward. Clearing a timer outright needs an administrator
  **and** the `resource_expiry_allow_never` setting. See
  [Auto-delete Timer](auto-delete-timer.md).
- **The Dashboard home page**, which is an aggregate of things you already have access to.

## Objects, not just areas

A scope says *what*, not *which*. Two mechanisms narrow the *which*:

- **Workgroups** tag resources and users; the cloud, container, Databases and Kubernetes
  lists show you the rows whose workgroup you are in. Untagged resources are visible to
  whoever deployed them. They live on the **RBAC &rarr; Workgroups** tab, beside the
  permission tabs, because the two answer the two halves of the same question.

  Unlike the other RBAC tabs, Workgroups is **not admin-only**: it has a real `workgroups`
  scope, so a user granted `workgroups:read` sees that tab and nothing else on the page.
  Deleting a workgroup still needs the Admin flag.
- **POV access** narrows the POV pages to named environments, as described above.

"Untagged resources are visible to whoever deployed them" is the whole rule, and it is
worth reading twice: a workgroup is a property of the **row**, not of the page. A cloud
database or Kubernetes cluster carries one only once somebody assigns it — at creation, or
later with the admin-only **Workgroup** button on [Databases](databases.md) /
[Kubernetes](kubernetes.md). Until then the row is creator-scoped, which is why adding the
field granted and revoked nothing on upgrade.

Tagging a row widens what its workgroup can **do** to it, not only what they can see: the
list, every by-id action, a Configuration Management run against it, and its auto-delete
timer all answer to the same rule. Sharing a database with your team means the team can run
plays against it and extend its timer.

Functions, Certificates and SPIRE remain creator-scoped for non-admins — they show you what
you created, and that is not configurable.

## If a user reports a 403

1. Is **"Full access (unrestricted)"** ticked? If the user has **no** role, group mapping
   or Entitle grant, the grid is not being read, and the refusal is coming from the Admin
   flag or a workgroup, not from a scope. If they do have one of those, they are not
   unrestricted at all: they hold exactly what those sources grant. Check the role and
   group mappings.
2. Does the message name a scope and level (*"Requires 'storage:write' permission"*)? Grant
   that pair. If the message says *"or administrator"*, the grant must be explicit — an
   unrestricted map will **not** satisfy it, because that route used to require the Admin
   flag and widening it to every legacy account would have been a security change.
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
