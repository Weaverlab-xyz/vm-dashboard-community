# The permission sections

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are deciding which sections of the dashboard a user may read or change, and what each one covers.

Part of [Permissions](../permissions.md).

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
| `change_windows` | write, use | `write` defines change windows and recurring schedules; `use` approves a booked change. No `read`: every run form reads windows. Both levels need an **explicit** grant — see [If a user reports a 403](../permissions.md#if-a-user-reports-a-403) |

Preview features are turned on and off in Settings → Preview features. All but one have
no scope of their own; each is gated like this:

| Preview feature | Gate |
|---|---|
| Virtual Desktops | administrator |
| Certificate Lab, SPIRE Lab | `cloud_function:read` to see, `cloud_function:write` to change |
| Agent Cell | `config_mgmt:write` |
| Directories | its own `directories` scope (read, write, delete), which must be granted explicitly. See [Directories → Permissions](../../preview/directories.md#permissions) |
