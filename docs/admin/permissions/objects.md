# Permissions on objects, not just areas

> **Audience:** operator · **Profile:** `both` · **Read this when:** you need access to one VM, database or POV rather than a whole area of the dashboard.

Part of [Permissions](../permissions.md).

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
  [Workstream deploy defaults](../../clouds/vms/pra.md#workstream-deploy-defaults). Its **network
  defaults** take the same authority — see
  [Workstream network defaults](../../clouds/vms/pra.md#workstream-network-defaults).
  Its **bound secrets** (an SSH key secret per cloud and a PRA credential) are
  **administrator-only** to change; members can see which are bound. See
  [Workstream secret bindings](../../clouds/vms/pra.md#workstream-secret-bindings). A registered vault can be
  limited to a workstream by an administrator, so only that workstream's deploys read it —
  see [Vaults](../secrets-management.md#vaults-and-a-workstreams-own-vault).
  Its **post-deploy runs** (assets run on every VM deployed into it) are administrator-only
  to change too — see [Workstream post-deploy runs](../../clouds/vms/pra.md#workstream-post-deploy-runs).
  **Stored assets** can be limited to one workstream by an administrator; only its
  members then see, run or replace them — see
  [Scoping an asset to a workstream](../../operations/storage-management.md#scoping-an-asset-to-a-workstream).

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
later with the admin-only **Workstream** button on [Databases](../../cloud-services/databases.md) /
[Kubernetes](../../cloud-services/kubernetes.md). Until then the row is creator-scoped, which is why adding the
field granted and revoked nothing on upgrade.

Tagging a row widens what its workstream can **do** to it, not only what they can see: the
list, every by-id action, a Configuration Management run against it, and its auto-delete
timer all answer to the same rule. Sharing a database with your team means the team can run
plays against it and extend its timer.

Functions, Certificates and SPIRE remain creator-scoped for non-admins — they show you what
you created, and that is not configurable.
