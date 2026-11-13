# Demo hub

> **Audience:** presenter, admin · **Profile:** `demo` · **Read this when:** you want one page for every demo cell and every page a demo runs from, or you are giving someone the Demo Presenter role.

**Demo** in the menu (`/demo`) is the estate instance's counterpart to **POV** on a POV
instance. It has two tabs:

- **Cells** lists every demo cell you can see: [OT](ot-demo-cell.md),
  [Network](net-demo-cell.md) and [Agent](agent-demo-cell.md). Each shows its state, its
  wiring (or, for an agent, whether its token is still live), its workgroup, and a
  **Manage** link to the page that builds and tears it down. A cell type whose preview is
  off reads *Not available here* rather than zero.
- **Launchpad** links to every page a demo runs from: the cloud consoles, the Workload Lab,
  Kubernetes, databases, the hypervisors and more. Each entry is *ready*, *needs* a feature
  turned on (with a link to Settings), or *not available on this instance*. Nothing is ever
  left off the list.

The hub only reads. Building, re-wiring and clearing a cell all stay on that cell's own
page. The demo **scripts** stay on [Use cases](personas/README.md), which the hub links to.

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
| Open the hub and see cells on every cloud, in their workgroups | `demo:read` |
| Build an OT cell on AWS, Azure or GCP, and probe or re-wire it | `demo:write` beside `<cloud>:write` / `ot:write` |
| Build a network cell | `demo:write` beside `gcp:write` |
| Clear a **failed** OT cell's record | `demo:delete` beside `<cloud>:delete` |

And deliberately **cannot**:

- **Deploy or destroy anything else** in a cloud. The general cloud routes are unchanged.
- **Destroy a running OT or network cell.** That is still the cloud's own instance Destroy,
  so it needs `<cloud>:delete`. Pair the role with that, or ask an operator.
- **Mint an agent cell.** Minting issues an API token that acts as whichever non-admin user
  the caller picks, and every MCP tool applies that user's permissions. A presenter could
  pick a Cloud Admin user and hold that user's reach, so the agent cell stays on
  `config_mgmt:write`.
- **Run standalone OT protocol tunnels**, which wire real plant gear rather than a cell.

Cells stay scoped to the presenter's workgroups, exactly as on the cloud pages.

### What changes on upgrade

Nothing changes for existing users. `demo` only ever adds a path, so it has no backfill,
and **Full access** in the grid does not include it. The new role is seeded on first boot.
An install that already has the **Read-Only** role keeps its existing map, because built-in
maps are written once. Add `demo:read` to it there if you want it.

### The pages a presenter still opens

The **Manage** links go to the cloud consoles (`/gcp#ot`, `/gcp#net`, ...). Those pages
also load images, machine types and networks through the cloud's own read routes. Give a
presenter `<cloud>:read` alongside the role so those pickers fill. Moving the cell forms
onto the hub with their own narrow option lookups is the planned next step.
