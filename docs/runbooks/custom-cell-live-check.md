# Runbook — custom demo cell, first live run

> **Audience:** contributor · **Profile:** `demo` · **Read this when:** you are running a custom demo cell against a real cloud for the first time, or diagnosing one that misbehaves.

[Custom demo cells](../profiles/demo/custom-demo-cell.md) (#1332) shipped tested only
against a stand-in at the cloud boundary. Every resource was a fake, and the Custom cell
panel had never been driven in a browser. This runbook is the first real run: one small cell
on one cloud, through its whole life. Run it before a custom cell is used in front of a
customer.

It costs money while it runs. Keep the sizes at their smallest, and do step 6 even when an
earlier step fails.

## 0. Before you start

- An **estate** instance (`install_profile=demo`) with credentials for **one** cloud. GCP is
  the most exercised; AWS or Azure works the same way.
- **Kubernetes management** and **cloud databases** turned on (Settings → Integrations).
  Without them a blueprint with a cluster or a database is refused when you launch.
- [Stop/start for clusters and databases](../profiles/demo/custom-demo-cell.md#while-it-runs)
  (#1331) on this build. Without it, step 4's cluster and database rows say they kept
  running.
- Two accounts, or one holding both roles (#1330): a **Cell Builder** to write the blueprint
  and a **Demo Presenter** to launch it. Both need membership of the workstream you'll use.
- The cloud console open, to check for leftovers at the end.

Write down each step's result in the table at the end as you go.

## 1. Write a blueprint (Cell Builder)

On the Demo hub: **Build → Custom cell → Blueprints → New blueprint**.

1. Name it `live-check`. Give it your workstream.
2. Add **+ VM** with label `vm`, count 1, the smallest machine type and an Ubuntu image.
3. Add **+ Database** with label `db`, the smallest MySQL or PostgreSQL tier, in the region
   you use.
4. Optional: **+ K8s cluster** with label `k8s`, one node, the smallest size. Skip it if a
   cluster's build time (often 10–20 minutes) is too long for this session. Steps 4 and 6
   then cover only the VM and the database.
5. Save.

**Then the refusals.** Edit the VM item's spec, add each of these in turn, and save:

- `"workgroup": "x"`
- `"ssh_key_secret_override": "x"` (and, if you like, `"jumpoint_name": "x"`)
- the cloud's name field, such as `"instance_name": "x"` on GCP

**Pass:** each save is refused with a message naming the field, and the clean version saves.
As the **Demo Presenter**, the blueprint editor is not offered, and `POST
/api/demo/custom-cells/blueprints` returns 403.

## 2. Launch it now (Demo Presenter)

**Build → Custom cell**: under **When, and for how long** choose *Now* and 2 hours, pick
`live-check`, name the cell `lc1`, pick the workstream, then **Launch now**.

Watch:

- The cell goes **provisioning**, then **active**, once every member is up. **Members** lists
  `lc1-vm-1`, `lc1-db` (and `lc1-k8s`), each with its own status.
- Each member is also an ordinary row on its own page: the cloud console's instances,
  Databases, and Kubernetes.
- The cell appears on the hub's **Cells** tab.
- **Timers:** each member's own auto-delete time is the cell's end **plus 24 hours**
  (`BACKSTOP` in `services/custom_cell_service.py`). Check it on the VM's row and the
  database's row.

**Pass:** every member reaches its running state, the jobs on **Jobs** are named after the
cell's resources, and the timers match.

## 3. Book a cell for later

Launch a second cell, `lc2`, from the same blueprint. This time choose *At a time*, about 15
minutes from now, with a 1-hour lifetime.

**Pass:** it shows **scheduled**, and nothing exists in the cloud until the start time. It
builds within a few minutes of the start, and its end is **start + 1 hour**, not "now + 1
hour". Destroy `lc2` as soon as it is active; it has proved its point.

## 4. Suspend and resume

On `lc1`, press **Suspend**.

- The VM stops through the same power job its Stop button creates (see **Jobs**).
- The database stops.
- A cluster scales its node pools to zero (AKS uses its managed stop). The GKE and EKS
  control planes keep billing.
- The cell shows **suspended**.

Then the auto-restart rule: start the database **outside the dashboard**, in the cloud
console, and wait for the next sweep (a few minutes).

**Pass:** the sweep stops the database again because the cell is suspended. A member whose
power controls aren't available says so on its row instead of failing silently.

Press **Resume**. **Pass:** every member comes back up and the cell shows **active**.

## 5. Change the end time

Press **Change end** and move the end about 10 minutes out.

**Pass:** the cell's end moves, and the members' own timers move to the new end + 24 hours.
As a non-admin, an end past the auto-delete ceiling is held to that ceiling.

## 6. Let it end, or destroy it

Either wait for the end time from step 5, or press **Destroy**.

- Each VM is destroyed through its own destroy job. Each cluster and database goes through
  its own decommission.
- The cell goes **destroying**, then **destroyed**.
- If you destroy during a build (try it on a third cell launched and destroyed straight away),
  the building member is torn down on a later pass, **after** its build finishes, never in
  the middle of one.

**Pass:** the cloud console shows no instance, database, cluster, disk or IP named `lc1-*`
or `lc2-*`. Check once more after 30 minutes for anything the cloud releases slowly.

## 7. A policy refusal

If this instance runs admission control or spend caps, make one member break a rule (for
example, a machine type or region the policy forbids) and launch a cell.

**Pass:** the cell is still booked (the policy isn't checked when you book; that is a known
gap), the offending member is **failed** with the policy's reason on its row, and the other
members are built.

## Results

| Step | Pass / fail | Notes and job links |
|---|---|---|
| 1. Blueprint and refusals | | |
| 2. Launch now, timers | | |
| 3. Booked launch | | |
| 4. Suspend, re-stop, resume | | |
| 5. Change end | | |
| 6. End / destroy, nothing left | | |
| 7. Policy refusal | | |

File anything that fails through **Feedback** in the app, which opens a GitHub issue, with
the step number and the job links.
