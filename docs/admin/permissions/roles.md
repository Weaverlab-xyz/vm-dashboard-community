# Roles

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are giving people access through a role rather than one permission at a time.

Part of [Permissions](../permissions.md).

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
