# Permissions

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are deciding what a user may see or do — and especially before you tick "Full access (unrestricted)" or hand a POV to a customer stakeholder.

Two independent questions, and keeping them apart is the whole model:

| Question | Answered by | Where |
|---|---|---|
| What may this user **do**? | a **scope** and a **level** | the Permissions grid on Users / Groups |
| Which **objects** may they do it to? | a workgroup tag, or a POV grant | Workgroups; the POV access picker |

A scope is a feature area — roughly one per section in the navigation. A level is
`read`, `write`, `delete` or `use`. A grant is a scope plus a level: `pov:read`,
`storage:write`, `databases:delete`.

---

## The rule that surprises people

**An empty permission map means unrestricted, not "nothing".**

A user with no permissions set at all can do everything except the things that need the
Admin flag. This is deliberate backward compatibility: the permission columns were added
long after the first users were, and treating "unset" as "denied" would have locked those
accounts out of work they were already doing.

The consequence is that the grid has two very different empty states:

- **"Full access (unrestricted)" ticked** — the map is NULL. Every scope is allowed, now
  and for every scope added in future.
- **Unticked with nothing checked** — the map exists but is empty. Every scope is denied.

So the way to restrict somebody is to untick "Full access" and then grant what they need.
Leaving it ticked and unchecking boxes underneath does nothing, because the boxes are not
being read.

## Levels

| Level | Means |
|---|---|
| `read` | see the page and its data |
| `write` | create and modify |
| `delete` | destroy |
| `use` | take part without managing — see below |

`use` exists for two cases where "read" is too little and "write" is far too much:

- **`secrets:use`** — run an Ansible playbook that reads a secret out of a vault, without
  ever being shown the value.
- **`pov:use`** — tick off use cases in a POV you have been given, without being able to
  create, destroy, share, power or add logins to it. Powering an environment, waking a
  suspended one included, is `pov:write`: the route takes an arbitrary runstate, so it
  stops and suspends as readily as it starts. A *POV accessor* has a separate start-only
  wake of its own — see [Customer access to a POV](profiles/pov/customer-access.md) — and
  a stakeholder who must be able to wake their own POV needs `write` or an accessor
  alongside.

Not every scope offers every level. A scope that has nothing to delete shows no Delete
checkbox, rather than a checkbox that saves and then enforces nothing. If you send a level
a scope does not offer through the API you get a `422` naming the levels it does offer.

## The sections

The fourteen original scopes — `vms`, the four clouds, `images`, `containers`,
`config_mgmt`, `jobs`, `workgroups`, `secrets`, `cloud_database`, `k8s` and
`cloud_function` — all offer four levels. The rest are one per navigation section:

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

Preview features — Virtual Desktops, Certificate Lab and SPIRE Lab — have no scope yet and
keep their existing gating. They are turned on and off in Settings → Preview features.

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

Three sources, unioned. A level granted by any of them counts.

| Source | Set by | Lifetime |
|---|---|---|
| Baseline | an admin, on the Users page | until changed |
| Group-derived | OIDC group membership → group mapping | **rewritten on every login** |
| Just-in-time | Entitle, over the REST integration | until Entitle revokes |

The middle row is worth understanding: signing in **replaces** the group-derived set
outright. That is how losing a group membership actually reduces access — but it also means
anything written there by hand disappears at the user's next login. Per-POV grants are
stored separately for exactly this reason, so they survive.

## Groups

A group mapping turns an Entra group into a workgroup plus a default permission set. Two
things to know:

- There is **no edit**. To change a mapping's permissions, delete it and add it again.
- Its permissions are re-applied to every member **at each login**, so a mistake in a
  mapping keeps reasserting itself until the mapping is fixed.

## Scopes that are deliberately not grantable

Some things answer only to the **Admin** flag, because a grantable version of them would be
a way to become an administrator:

- **Users, Groups** — anyone who can edit a user can make themselves admin.
- **Settings / first-run setup**, and the **secret vault registry**.
- **Worker concurrency and preflight**, which are instance-wide plumbing.

Two more have no scope for narrower reasons:

- **The auto-delete timer.** Authorization there is visibility: anyone who can see a
  resource may extend its timer, because extending only ever *delays* a deletion. Clearing
  a timer outright still needs an administrator. See
  [Auto-delete Timer](auto-delete-timer.md).
- **The Dashboard home page**, which is an aggregate of things you already have access to.

## Objects, not just areas

A scope says *what*, not *which*. Two mechanisms narrow the *which*:

- **Workgroups** tag resources and users; cloud and container lists show you the rows whose
  workgroup you are in. Untagged resources are visible to whoever deployed them.
- **POV access** narrows the POV pages to named environments, as described above.

Several pages are additionally creator-scoped for non-admins — Databases, Functions,
Certificates and SPIRE show you what you created. That is not configurable.

## If a user reports a 403

1. Is **"Full access (unrestricted)"** ticked? If so the grid below it is not being read,
   and the refusal is coming from the Admin flag or a workgroup, not from a scope.
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
2. a display label in `permissionScopeLabel` (`web_dashboard/static/js/app.js`);
3. a **backfill** in `web_dashboard/database.py` granting exactly the levels a non-admin
   with an explicit map could already reach;
4. the right *form* of the check — see the table below;
5. an entry in `_NAV_SCOPE` in `tests/test_permission_catalog.py`, which fails if a nav
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
into Entra group names by `scripts/bootstrap_entitle_groups.py`. Renaming one un-grants
everyone who had it, and the symptom is a locked-out user rather than an error — so if a
display name needs to change, change the label map, not the key.
