# Demo hub

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you want one page for every demo cell and every page a demo runs from, or you are giving someone the Demo Presenter role.

**Demo** in the menu (`/demo`) is the estate instance's counterpart to **POV** on a POV
instance. It has three tabs:

- **Cells** lists every demo cell you can see: [OT](ot-demo-cell.md),
  [Network](net-demo-cell.md) and [Agent](agent-demo-cell.md). Each shows its state, its
  wiring (or, for an agent, whether its token is still live), its workstream, and a
  **Manage** link that opens the cell's panel on **Build**. A cell type whose preview is
  off reads *Not available here* rather than zero.
- **Build** deploys, probes, re-wires and destroys OT cells (one panel with a GCP / AWS / Azure toggle) and network
  cells, and mints and revokes agent cells. Each panel is the **same form** the cloud
  page's tab or the Workload Lab's Agent tab shows, so a cell built here is identical to
  one built there. A panel appears only where that page's tab would: the cell's feature on,
  and for OT and network cells the cloud's credentials present. The tab shows to anyone
  who can build a cell.
- **Scheduling.** The OT and network forms start with **When, and for how long**. Build
  *Now*, *At a time* or in a *Change window*, and pick how long the cell lives (4, 8 or 24
  hours, a custom number, or the auto-delete policy's default). The lifetime counts from
  when the cell is due to start, so build time is part of it. A cell is destroyed, with its
  DMZ broker a couple of minutes later, within about five minutes of its end, as the person
  who built it; it does not depend on the auto-delete feature being on. **Set end** /
  **Change end** on the Cells list moves or sets the end of a cell you can see, and a
  non-admin's end is held to the policy ceiling. A cell that has not started building
  shows *builds <time>*. Agent cells already expire with their token (8 hours by default).
- **Launchpad** links to every page a demo runs from: the cloud consoles, the Workload Lab,
  Kubernetes, databases, the hypervisors and more. Each entry is *ready*, *needs* a feature
  turned on (with a link to Settings), or *not available on this instance*. Nothing is ever
  left off the list.

The demo **scripts** stay on [Use cases](personas/README.md), which the hub links to.
Standalone OT protocol tunnels, which wire real plant gear, stay on the cloud pages.

On a POV instance the hub and its API return 404. A POV hosts its cells inside the POV
instead.

## Who sees it

Anyone holding `demo:read`. Administrators always do. The home page's three cell tiles
open the hub for the same people. Everyone else keeps the tiles' old links.

## The Demo Presenter role

Before this, letting someone run a demo meant letting them deploy anything into that cloud:
building an OT cell needed `gcp:write` (or `aws` / `azure`), and that is also what
`POST /api/gcp/deploy` takes. The **Demo Presenter** role holds the `demo` scope instead.
`demo` is accepted **beside** each cell route's existing scope, never in place of it:

| A Demo Presenter can | Because the route accepts |
|---|---|
| Open the hub and see cells on every cloud, in their workstreams | `demo:read` |
| Build an OT cell on AWS, Azure or GCP, and probe or re-wire it | `demo:write` beside `<cloud>:write` / `ot:write` |
| Build a network cell | `demo:write` beside `gcp:write` |
| Fill the Build forms: the cloud's images, machine types, zones, subnets and security groups | `demo:write` beside `<cloud>:read` |
| Destroy a running OT cell (and its DMZ broker) or network cell | `demo:delete` beside `<cloud>:delete` |
| Clear a **failed** OT cell's record | `demo:delete` beside `<cloud>:delete` |
| Mint an agent cell, on a VM and SPIRE lab they can see, running as a new service account ([below](#agent-cells-run-as-their-own-account)) | `demo:write` beside `config_mgmt:write` |
| Revoke their agent cell, and remove its SPIRE identity | `demo:delete` beside `config_mgmt:write` |

And deliberately **cannot**:

- **Deploy or destroy anything else** in a cloud. The general cloud routes are unchanged.
  Destroy goes through a cell-only route that checks the target *is* a live cell in the
  presenter's workstreams, then runs the cloud's own teardown on it: VM, PRA jump items,
  Password Safe registration, and the DMZ broker last.
- **Choose who an agent runs as**, or link an agent to a Workload Lab credential, or open
  a Kubernetes access request for one. Those stay on `config_mgmt:write`.
- **Run standalone OT protocol tunnels**, which wire real plant gear rather than a cell.

Cells stay scoped to the presenter's workstreams, exactly as on the cloud pages.

### Agent cells run as their own account

Minting an agent issues a credential that acts as a user, and every MCP tool applies that
user's permissions, so the user *is* the agent's reach. An operator with
`config_mgmt:write` picks any non-admin user. A Demo Presenter picks nobody: the cell
creates a **service account for that agent alone**, which

- holds the built-in **Demo Agent** role: it reads VMs and inventory, and only its own jobs;
- lives in the one workstream the presenter chose for the cell;
- is **disabled when the agent is revoked**, and kept so its jobs and audit rows keep a name.

The mint is refused, and nothing is created, unless:

- the workstream is one of the presenter's;
- the host is a VM in the presenter's workstreams;
- the SPIRE lab, if one is used, is one the presenter can see;
- the presenter already holds every grant the Demo Agent role carries.

The last check means an agent can never reach further than the presenter who minted it.
A presenter can revoke only agents they can see.

### What changes on upgrade

Nothing changes for existing users. `demo` only ever adds a path, so it has no backfill,
and **Full access** in the grid does not include it. The new role is seeded on first boot.
An install that already has the **Read-Only** role keeps its existing map, because built-in
maps are written once. Add `demo:read` to it there if you want it.

### A presenter never needs a cloud page

Everything a cell needs is on the hub. Installing an agent is still the two playbooks you
run yourself, as on the Workload Lab. The Build forms read their pickers through
`/api/demo/cells/<cloud>/...`, which runs the cloud page's own lookup, so the lists match
exactly. The cloud's general read routes stay closed to the role.
