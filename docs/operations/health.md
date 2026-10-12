# Health

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want to know whether the infrastructure the dashboard depends on is working, or why an area shows as degraded.

**Health** (`/health`, under the admin links in the nav) shows whether the pieces the
dashboard depends on are working. It checks each area in the background, on that area's
own schedule, and the page shows the last result. Opening the page never calls a
provider.

It is administrator-only, like **Audit**. One page covers every BeyondTrust tenant,
every Password Safe account the dashboard onboarded and every agent, across all
workgroups.

## Areas

An area appears once the integration it covers is turned on.

| Area | Checks | Every | Shown when |
|---|---|---|---|
| **Resources** | everything on [Inventory](../inventory.md) except directories: whether the cloud still has it and it is running, and its access wiring (see [below](#resources)) | 30 min | always |
| **BeyondTrust tenants** | each PRA and Password Safe credential still gets a token, the same handshake as **Verify** on the tenant. The result is recorded on the tenant too | 30 min | PRA or Password Safe is on |
| **PRA Gateways** | each Gateway PRA knows about has a connected node, and each Gateway host the dashboard deployed is running (the reconcile pass's *degraded* and *missing* included) | 15 min | PRA is on |
| **Password Safe accounts** | every account the dashboard onboarded still exists and is rotating: database accounts, Kubernetes tokens, directory administrators, lab and workload accounts | 60 min | Password Safe is on |
| **Entitle** | the just-in-time reconciliation sweep is running, has no failing cloud leg and no orphaned grants, and the virtual applications and persona bundles the dashboard applied are not *failed* | 15 min | Entitle is on |
| **Remote agents** | each agent is polling, and is new enough for the directories bound to it | 5 min | Remote agents are on |
| **Directories** | the [Directory health](../directories/health.md) page's results, shown here too. That page keeps its own schedule | 5 min | Directories is on |

On an estate instance with no BeyondTrust tenant rows, the tenant and Gateway areas check
the PRA and Password Safe settings the instance already uses.

## Resources

Every object the dashboard built or registered gets a row under the same id as on
**Inventory**.

| Object | Infrastructure check | Read from |
|---|---|---|
| Cloud VMs (AWS, Azure, GCP, OCI) | still exists, running, and on AWS the system and instance status checks and any scheduled event | one `DescribeInstances` and `DescribeInstanceStatus` per region; the Azure instance view; one aggregated instance list per GCP project; the OCI instance |
| Managed databases | the server's state | one `DescribeDBInstances` per region (RDS); the Cloud SQL instance list; the Azure Flexible Server or SQL server resource |
| Managed Kubernetes | the cluster's status and each node group or pool | EKS `DescribeCluster` and node groups; the GKE cluster; one AKS list per subscription |
| Hypervisor VMs | power state as of the last inventory sync | the hypervisor sync's cache, with no extra call |
| Labs, workload credentials, functions, desktops, POVs | the state the dashboard recorded | the dashboard's own rows |

**Stopped VMs.** A stopped VM is healthy when something in the dashboard stopped it: a
power action from the dashboard, a suspend schedule, or a spend cap. A VM stopped
anywhere else is degraded. A VM the cloud no longer has is failing, because the
dashboard still lists it and still manages access to it.

Each object also shows its access wiring, where it was set up:

- Password Safe onboarding;
- the PRA jump item or tunnel;
- the Entitle integration.

Wiring that was never asked for is not reported. A recorded onboarding failure, an
auto-delete less than a day away, or a spend cap reached makes the object degraded.
Whether a Password Safe account is actually rotating is the **Password Safe accounts**
area's job.

If a cloud cannot be asked (an expired credential, a missing permission), its objects
show unknown for that run. They are not marked failed.

## Reading it

Each object gets the status of its worst check:

- **Failing**: at least one check failed.
- **Degraded**: something is not right yet, but nothing has failed.
- **Healthy**: every check passed.
- **Unknown**: nothing could be checked yet.

Thresholds:

- **Password Safe rotation:** degraded when a rotation is over a day late, or the
  account has never rotated. Failing when it is over a week late, or the account no
  longer exists.
- **Entitle sweep:** degraded when it has missed three runs or is switched off.
- **Remote agents:** an agent is failing when it is offline.

**When an area cannot be checked at all.** For example, if Password Safe does not
answer, the area says the last check could not run and keeps showing the results from
the run before. It does not mark every object failed, because "could not check" and
"checked and failed" are different answers.

**Check now** on an area runs it straight away. A second press within a minute returns
the stored result.

## Ordering by focus

Areas that matter to the active [focus](../profiles/demo/personas/README.md) come first.
For example, an IAM engineer focus leads with Password Safe accounts, remote agents and
directories. No area is ever hidden by a focus.

## What it does not show

It never lists accounts, groups, roles, grants or privilege. A Password Safe row names
the resource whose account it is and says whether it is rotating, never the account or
its credential. Identity posture is BeyondTrust Identity Security Insights' data, and the
dashboard does not collect a second copy of it.
