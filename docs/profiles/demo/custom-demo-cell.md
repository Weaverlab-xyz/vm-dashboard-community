# Custom Demo Cell

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** a demo needs more than one fixed cell (a Kubernetes cluster, a database and a few VMs, say), or you are giving someone the Cell Builder role.

The [OT](ot-demo-cell.md), [Network](net-demo-cell.md) and [Agent](agent-demo-cell.md)
cells each have one fixed shape. A **custom cell** contains whatever the demo needs. For
example: a GKE cluster in GCP, a MySQL database and two Ubuntu servers, all with one
lifetime, one suspend schedule and one **Destroy** button.

It works like a POV blueprint. Someone writes the recipe once, and others launch from it.

| Who | Role | Does |
|---|---|---|
| A **Cell Builder** | Cell Builder (`demo_blueprints` + `demo`) | Writes and edits **blueprints**, and can also launch cells. |
| A **Demo Presenter** | Demo Presenter (`demo`) | Launches a cell from a blueprint, books it for a future date, sets its lifetime, and suspends, resumes and destroys it. Cannot edit blueprints. |

Most SEs hold **both** roles. A user can hold several roles, and gets everything any of
them grants ([Roles](../../admin/permissions/roles.md)).

## Writing a blueprint

On the [Demo hub](demo-hub.md), go to **Build → Custom cell → Blueprints → New blueprint**:

1. Give it a **name** and, optionally, a **workstream**. A blueprint with no workstream can
   be seen and launched by everyone who can launch cells.
2. Add **items**: **+ VM**, **+ K8s cluster**, **+ Database**. Each item has:
   - a **kind** and a **cloud**, chosen per item, so one cell can span clouds;
   - a **region** (required for clusters and databases; a GCE VM takes its zone in the spec);
   - a short **label** (`web`, `db`, `gke`), which becomes part of each resource's name;
   - a **count** (VMs only, 1 to 10);
   - a **spec**: the cloud's own create fields, as JSON. These are the same fields its deploy
     form sends, minus the name and the workstream. The editor fills in a starter for each
     kind and cloud.
3. Optionally set a **default lifetime** and a **suspend / resume** time.

Saving checks every item against that cloud's own create request, so a blueprint is
refused for exactly what the cloud's form would refuse. The error names the field. A
blueprint **cannot** set any of these:

- **The resource name, the workstream or a schedule.** The cell supplies these when it is
  launched.
- **Credential or integration overrides.** That means an SSH key secret, a PRA credential
  reference, a Gateway or Asset Group, a Password Safe vault account group, or a managed
  directory. Other people launch the blueprint, so these resolve from configuration at
  launch, exactly as a plain deploy does without an override.

Editing or deleting a blueprint never affects a cell already launched from it. Each cell
keeps its own copy of the items as they were when it was launched.

## Launching a cell

Go to **Build → Custom cell**:

1. Under **When, and for how long**, choose *Now*, *At a time* or a *Change window*, and how
   long the cell lives. The lifetime counts from when the cell is due to start.
2. Pick the **blueprint**, a **cell name** (1 to 24 lowercase letters, digits and hyphens)
   and a **workstream**.
3. Press **Launch now** or **Book the cell**.

Every resource is named `<cell>-<label>`, or `<cell>-<label>-<n>` for VMs, and is built
**through its own cloud's deploy path, as you**. Your workstream membership, the
admission policy, secret bindings and change-window approval all apply just as they would
if you used that cloud's own form. The one difference is that you don't need that cloud's
`write` permission: `demo:write` is enough.

A launch with problems the dashboard can detect without touching a cloud is refused at
booking time, so you find out when you book, not on the morning of the demo. This covers a
bad name, a field a cloud has since tightened, and Kubernetes management being off. A
refusal that only a cloud can give (for example, a policy that forbids a region) shows up
when the cell builds. The resource it applies to is marked **failed** with the reason, and
the rest of the cell is still built.

## While it runs

The **Custom cells** card, and the **Cells** tab, list every custom cell in your
workstreams.

| Status | Meaning |
|---|---|
| **scheduled** | Booked; nothing exists yet. |
| **provisioning** | Being built. |
| **active** | Every resource is up. |
| **suspended** | Stopped by **Suspend** or the schedule. |
| **failed** | A resource failed. The others are kept, so **Destroy** still cleans up. |
| **destroying** | Being torn down. |
| **destroyed** | Gone. |

**Members** lists each resource and its own status. Each resource is also an ordinary row
on its own page: the cloud console, Kubernetes, or Databases.

- **Suspend / Resume** stops or starts the whole cell. VMs stop, clusters scale their node
  pools to zero (AKS uses its managed stop) and databases stop. The GKE and EKS control
  planes keep billing while stopped. If a resource's power controls aren't available on this
  install, that resource keeps running and its member row says why.
- **The suspend schedule** (set on the blueprint) does the same at its times on its days.
  While the cell is suspended, a database that its cloud restarts on its own is stopped
  again. AWS RDS does this after 7 days.
- **Change end** moves the cell's end time. A non-admin's end is held to the auto-delete
  policy's ceiling.

## When it ends

At its end time, the cell is torn down within a few minutes, as the person who launched it:

- each VM through the same destroy job its own Destroy button creates;
- each cluster and database through its own decommission.

This does not depend on the auto-delete feature being on. **Destroy** does the same thing
immediately, and destroying a cell that hasn't been built yet just cancels it.

A resource that is still building when the cell is destroyed is torn down on a later pass,
once its build finishes. Destroying a half-applied build is how cloud resources get
orphaned.

Every resource also carries its **own** auto-delete timer, set to the cell's end plus 24
hours. If the cell record were ever lost, the ordinary auto-delete reaper would still
collect it.

## Limits

- **Clouds:** GCP, AWS and Azure. OCI isn't offered yet.
- **Not included:** OT, network and agent cells can't be part of a custom cell yet.
- **A VM whose deploy failed** has no instance record to destroy from. Its member says so.
  Check the cloud console in case the failed apply left something behind.
- **Item specs are edited as JSON**, seeded with a starter for each kind and cloud. They are
  not field-by-field forms.
