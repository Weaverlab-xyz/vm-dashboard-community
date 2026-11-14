# Demo hub

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you want one page for every demo cell and every page a demo runs from, or you are giving someone the Demo Presenter role.

**Demo** in the menu (`/demo`) is the estate instance's counterpart to **POV** on a POV
instance. It is the presenter's page; someone who does the job day to day starts from their
[Workspace](workspace.md) instead. It has three tabs:

- **Cells** lists every demo cell you can see: [OT](ot-demo-cell.md),
  [Network](net-demo-cell.md), [Agent](agent-demo-cell.md) and
  [Custom](custom-demo-cell.md). Each shows its state, its
  wiring (or, for an agent, whether its token is still live), its workstream, and a
  **Manage** link that opens the cell's panel on **Build**. A cell type whose preview is
  off reads *Not available here* rather than zero. A **Cost (est.)** column shows what
  each cell is costing; see [What a cell costs](#what-a-cell-costs).
- **Build** deploys, probes, re-wires and destroys OT cells (one panel with a GCP / AWS / Azure toggle) and network
  cells, mints and revokes agent cells, and launches **custom cells** from a blueprint
  (a **Cell Builder** writes the blueprints on the same panel; see
  [Custom Demo Cell](custom-demo-cell.md)). Each panel is the **same form** the cloud
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

## What a cell costs

Each row on **Cells** shows three numbers, and each cell type's heading adds them up:

- **$/h now**: what the cell costs per hour at the moment. A VM the dashboard has stopped
  counts only its disk.
- **so far**: the cost since each VM came up, split into running and stopped time.
- **by end**: *so far* plus the rate now until the cell's end time. It appears only when
  the cell has an end.

These are **estimates at list price**, from the same price lookup the
[VM spend caps](../../clouds/vms/power.md#spend-caps) use. They leave out Savings Plans,
reservations, credits, free tier, data transfer and snapshots, so they **err high**. Use
them to see which cells are worth destroying, not to reconcile a bill.

- **Running or stopped** comes from the dashboard's own Stop and Start jobs, not from the
  cloud. A VM stopped in the cloud's console still counts as running.
- **A VM with a spend cap** shows the total its cap has accrued, so the hub and the VM's
  own spend panel agree.
- **A Kubernetes cluster** in a custom cell is priced as its nodes: the node count times the
  node size's VM rate. Both come from what the cluster was built with, or the Terraform
  module's default when the request left them out (GKE: three `e2-standard-2`; EKS: two
  `t3.small`; AKS: two `Standard_B2s`). A stopped cluster's nodes cost nothing, because
  stopping scales its pools to zero. The **control-plane fee** is added on top, running or
  stopped. No price list returns it, so it is a setting (Settings → *Cloud Costs* →
  *Kubernetes control-plane fee*) defaulting to list price: EKS $0.10/h, GKE $0.10/h (its
  free-tier credit is not assumed), AKS $0 on the free tier. A registered cluster is not
  priced, because it is billed to its own account.
- **A managed database** in a custom cell is priced from the cloud's own price list:
  its instance per hour plus its storage. RDS uses the Pricing API (Single-AZ); Cloud SQL
  uses the Cloud Billing Catalog, by tier (zonal); Azure flexible servers use the public
  Retail Prices API, by Burstable size or General Purpose vCores. A stopped database still
  bills its storage. Only **PostgreSQL and MySQL** are priced: SQL Server and Oracle
  licence editions change the price several-fold, and Atlas and OCI Autonomous are billed
  by another party.
- **Not priced, and said so:** a database engine with no price lookup, and a VM, cluster or
  database in a region the price list can't reach. Each is named under the row. None is
  counted as $0.
- **Agent cells** show no cost. They build nothing; they run on an existing VM, whose cost
  is that VM's.
- **Prices need the same access as spend caps.** On AWS that is `pricing:GetProducts`; on
  GCP, the Cloud Billing API enabled on the project. Azure and OCI price lists are public.
  Without it the column reads *—* and each VM is listed as having no price source.

The estimate loads after the list, so a slow price lookup never holds up the page.

## Monthly budget

An administrator can cap what each presenter's cells cost in a month. Booking a cell that
would take a presenter past their budget is **refused**, and the message gives the figures.
Nothing is ever stopped by the budget, and administrators are never limited.

- **Setting it:** Settings → *Cloud Costs* → **Demo cells: monthly budget per presenter**
  (`demo_monthly_budget_usd`) is the default for everyone. It works whether or not Cloud
  Costs itself is on. On **Users**, *Demo budget* sets one person's own budget, which wins
  over the default. Blank uses the default, and 0 means no budget for that person. With the
  default at 0 and no one given a budget, nothing changes.
- **What counts, this calendar month (UTC):**
  - **spent:** what the presenter's cells' VMs and cluster nodes have cost since the 1st,
    including a VM destroyed this month, up to when it was destroyed;
  - **committed:** each running resource's rate now, until its cell's end or the end of
    the month, whichever comes first;
  - **this cell:** the new cell's rate for its whole lifetime.

  A booking is refused when the three together pass the budget. This applies to OT,
  network and custom cells, and the check happens before anything is created.
- **What isn't priced isn't counted.** A database engine with no price lookup, a region
  the price list can't reach, or a price lookup that times out are named in the message,
  and never refuse a booking by themselves.
- **It is the same list-price estimate as the cost column,** so it errs high. A custom
  cell's cluster or database destroyed earlier in the month still counts, up to when it
  went: the cell records the moment each member is first seen destroyed (within a sweep of
  the teardown), and prices it from its provisioning request. Members destroyed before
  this was recorded are not counted.

The Cells tab shows your own line: *spent + committed of your budget*. It turns amber at
90%.

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
- is **disabled when the agent is revoked**, and kept so its jobs and audit rows keep a name. Users → Users hides these retired accounts unless you tick **Show retired agent accounts**.

The mint is refused, and nothing is created, unless:

- the workstream is one of the presenter's;
- the host is a VM in the presenter's workstreams;
- the SPIRE lab, if one is used, is one the presenter can see;
- the presenter already holds every grant the Demo Agent role carries.

The last check means an agent can never reach further than the presenter who minted it.
Anyone acting on an agent cell — revoking it, linking it, opening an access request — can do so only on cells they can see: their workstreams, or ones they minted. Administrators see all of them.

### What changes on upgrade

Nothing changes for existing users. `demo` only ever adds a path, so it has no backfill,
and **Full access** in the grid does not include it. The new role is seeded on first boot.
The built-in **Read-Only** role gains `demo:read` and `demo_blueprints:read` on upgrade, once,
so it keeps covering every section at its read level. A role you cloned from it is left
as you set it.

### A presenter never needs a cloud page

Everything a cell needs is on the hub. Installing an agent is still the two playbooks you
run yourself, as on the Workload Lab. The Build forms read their pickers through
`/api/demo/cells/<cloud>/...`, which runs the cloud page's own lookup, so the lists match
exactly. The cloud's general read routes stay closed to the role.
