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

An area appears once the integration it covers is turned on. Each area is its own tab,
labelled with its count of failing objects (or degraded, when nothing is failing); the
open tab is kept in the address (`/health#cloud_ops`), so it survives **Reload** and can
be bookmarked. **Only failing and degraded** hides the healthy rows in every tab.

| Area | Checks | Every | Shown when |
|---|---|---|---|
| **Resources** | everything on [Inventory](../operations/inventory.md) except directories: whether the cloud still has it and it is running, and its access wiring (see [below](#resources)) | 30 min | always |
| **Cloud APIs and spend** | the dashboard's own cloud credentials: per provider, whether its reads keep failing (from the tile collector, so no extra call), the same for cost data, VMs at or near their spend cap, and the auto-delete timer's last pass | 15 min | always |
| **Hypervisors** | each connection answers. Direct connections get the same read as **Test**, recorded on the connection. vSphere hosts disconnected or in maintenance, Proxmox nodes offline, Proxmox image storage under 15% free (failing under 5%). An agent-bound connection is never dialled; it shows its agent and whether the agent's sync is keeping up (more than three intervals late is degraded) | 15 min | any hypervisor is on |
| **BeyondTrust tenants** | each PRA and Password Safe credential still gets a token, the same handshake as **Verify** on the tenant. The result is recorded on the tenant too. Each registered PRA tenant also lists the POVs still in its shared Asset Group (see [below](#povs-on-a-shared-asset-group)) | 30 min | PRA or Password Safe is on |
| **PRA Gateways** | each Gateway PRA knows about has a connected node, and each Gateway host the dashboard deployed is running (the reconcile pass's *degraded* and *missing* included) | 15 min | PRA is on |
| **Password Safe accounts** | every account the dashboard onboarded still exists and is rotating: database accounts, Kubernetes tokens, directory administrators, lab and workload accounts | 60 min | Password Safe is on |
| **Entitle** | the just-in-time reconciliation sweep is running, has no failing cloud leg and no orphaned grants, and the virtual applications and persona bundles the dashboard applied are not *failed* | 15 min | Entitle is on |
| **EPM for Linux** | the configured EPM-L site answers, and agent packages are built so a Linux guest can enrol. Only the two reads the EPM-L page makes; it never starts a build or mints an installation token. An instance with no EPM-L site configured reports nothing here | 30 min | EPM for Linux is on |
| **Workload credentials** | the credentials the dashboard hands to workloads. Each Workload Credentials lease the dashboard runs on: failing while its issue attempts fail (with when it retries), degraded when it expired and was not renewed. Each Kubernetes ServiceAccount token in Password Safe: failing when the vault holds the placeholder rather than a working token, or the PRA Vault mirror is unlinked; degraded when there is no mirror (one Password Safe read per cluster). Each SPIRE lab: admin SVID expired (failing) or inside a day (degraded), no trust bundle, the k3s link or federation failed. Each agent cell: its agent's token revoked or expired (failing) or inside a day (degraded). Integration tokens used in the last 30 days that expire within a week, by name only | 30 min | Workload Credentials, Password Safe, SPIRE lab, agent cells or the MCP server is on |
| **Remote agents** | each agent is polling, is new enough for the directories bound to it, and whether it is SPIRE-attested or keeps a signing key on disk (see [below](#remote-agent-identity)) | 5 min | Remote agents are on |
| **Directories** | the [Directory health](../preview/directories/health.md) page's results, shown here too. That page keeps its own schedule | 5 min | Directories is on |

On an estate instance with no BeyondTrust tenant rows, the tenant and Gateway areas check
the PRA and Password Safe settings the instance already uses.

### Remote agent identity

Each agent row has an **Identity** line: *SPIRE-attested* (its signing key lives only in
the agent's memory) or *long-lived signing key on disk* (`identity.json`, from an
enrolment code). Above the list, a blue note counts them, for example *2 of 3 agent(s)
still keep a long-lived signing key on disk; 1 are SPIRE-attested*. Revoked agents are
not counted.

This is a recommendation, not a check: both kinds are supported, so the Identity line is
always healthy and the note never makes the area degraded or failing. To move an agent
over, use **Migrate to SPIRE** on the Agents page; see
[Attesting an agent through SPIRE](remote-agents/spire-attestation.md). The note links
to the workload identity guide when this build includes it.

### POVs on a shared Asset Group

Each registered PRA tenant has a **Shared Asset Group** line naming the POVs whose jump
items are still in the tenant's appliance-wide Asset Group. Those POVs were wired before
each POV got its own `pov-<name>` group, and they still work. But a vendor cannot be
scoped to a group every POV on the appliance shares, so their Access tab offers **Move to
its own Asset Group** instead of a vendor login; see
[A POV wired before this will say so](../profiles/pov/customer-access.md#a-pov-wired-before-this-will-say-so).
A POV being destroyed is still listed until it is gone, because its teardown finds its
jump items through the same group; a destroyed one is not.

Above the list, a blue note totals them across tenants. Like the agent identity note, it
never makes the area degraded or failing. When it reads *Every wired POV has its own Asset
Group*, nothing reads a tenant's appliance-wide Asset Group setting any more.

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
- **Remote agents:** an agent is failing when it is offline. A signing key on disk never changes its status.
- **Cloud APIs:** failing when every read for a provider is failing, degraded when some
  are. The message is the provider's own, for example an expired client secret.
- **Auto-delete:** failing when a deletion failed in the last pass, degraded when
  resources are past their expiry but the timer is not armed to delete them.

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
its credential. The integration tokens row names tokens, never who owns them, and
ignores tokens nobody uses. Who holds what access is BeyondTrust Identity Security Insights' data, and the
dashboard does not collect a second copy of it. How the dashboard's own remote agents
authenticate is shown, as a recommendation (see [Remote agent identity](#remote-agent-identity)).
