# Deploying an agent into a cloud

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want the dashboard to build an agent inside a VPC or VNet for Config Management, or to have one exist only around a change window.

Part of [Remote Agents](../remote-agents.md). Building the agent's host for you, and deciding when that host exists.

## What the dashboard builds

[Cloud VMs](config-runs.md#cloud-vms) can be configured from an agent inside their VPC or
VNet instead of from the remote worker. Building that agent by hand means a host, Docker,
a policy file, an enrolment code and a route. On **Remote Agents → Agents → Deploy an
agent**, the dashboard does all of it, in AWS, Azure or Google Cloud, the way it builds
Gateways, Rancher and Portainer hosts.

A deployed agent is an ordinary agent. It appears in the Agents list above the Deployed
agents list, enrols or attests, leases its jobs and is routed to exactly like an agent you
built yourself. It differs in three ways, all deliberate:

- **It runs Config Management only.** The dashboard writes its `policy.yaml` when the host
  is built: `job_types: [agent_ansible]`, and `targets` plus `ansible.targets` set to the
  ranges on the form, ports 22, 5985 and 5986. The file is read-only on the host. Changing
  the ranges, the size or the number of runs at once takes effect the next time the host
  is built.
- **Its identity is minted for each start and spent on first boot.** See
  [Identity](#identity).
- **Stopping it destroys the host and keeps the agent.** Its routes, its history and any
  runs booked into its next window stay valid. Nothing runs, and nothing is at rest,
  between starts.

### The host

| Cloud | Image | Default size |
|---|---|---|
| AWS | ECS-optimised Amazon Linux | `t3.medium` |
| Azure | Ubuntu, with `docker.io` installed by cloud-init | `Standard_B2s` |
| Google Cloud | Container-Optimized OS | `e2-medium` |

The default sizes allow about 1 GiB for each run plus the agent, which covers the default
of 2 runs at once. Allow about 1 GiB more for each extra run, up to 8.

The host goes into the region's VM subnet (the network dashboard-deployed VMs use) unless
you name another subnet. It has **no public address and no inbound rule**: the agent only
dials out. On AWS and Google Cloud it reaches the dashboard through the same on-demand NAT
that VMs use, and that NAT is not reclaimed while a deployed agent is up. On Azure it uses
the subnet's default outbound access, as VMs do.

At first boot a script pulls the agent and Ansible runner images and starts the agent with
the Docker socket, as `examples/remote-agent/docker-compose.sibling.yml` does. When
attesting, it also starts the SPIRE agent and SPIFFE helper, as
`docker-compose.spire.yml` does.

### Before you deploy

- **The agent address must be HTTPS and pinned.** A deployed agent dials the pinned agent
  address, the one the first enrolment code fixed (see
  [The signing audience](enrolment.md#the-signing-audience-is-pinned-by-that-first-code)).
  The deploy form names the problem if there is none, or if it is plain HTTP.
- **The region needs a VM subnet.** AWS needs a subnet and a security group, Azure a
  subnet and a resource group, and Google Cloud a project and a subnetwork, under
  **Settings → Clouds**. Only regions with one are offered.
- **The subnet must reach the dashboard**, and the SPIRE server too when attesting.

## Identity

Every start mints a new, single-use identity, and the bootstrap script spends it:

- **With SPIRE attestation on** (**Let agents attest through SPIRE**, with the dashboard's
  SPIRE server in use), the start does what **Migrate to SPIRE** does: a node entry and a
  join token for this agent's SPIFFE ID. The join token is written to a file, read into the
  SPIRE agent's environment and deleted from the host. The SPIRE agent keeps its key in
  memory. This is the recommended model; see
  [Attesting an agent through SPIRE](spire-attestation.md).
- **Otherwise** it mints a one-time enrolment code. The agent reads it from a file at
  enrolment, and the script deletes the file two minutes later.

Stopping the host clears the agent's key and any unused code, so a destroyed host's
identity is dead. Revoking a deployed agent on the Agents list also takes its host down.

## When it exists

| Lifecycle | Up | Down |
|---|---|---|
| **Always** | When deployed, and on **Start** | On **Stop** or **Delete** |
| **Until a time** | When deployed, and on **Start** | At that time; it then stays parked and becomes **Always** |
| **For a change window** | *Start* minutes (default 20) before each occurrence | *Keep* minutes (default 15) after it closes |

The schedule is a pass of the existing schedule sweep, so a window-mode agent starts within
one sweep interval of its start time. Allow for that in the lead time: 20 minutes covers
the sweep, the boot, two image pulls and enrolment.

**Stopping waits for running jobs**, up to the *Keep* minutes. A run still going after that
is stopped with the host, and the dashboard fails it when its heartbeat goes stale.

**Starting a window-mode agent by hand** ahead of its window keeps it up until the end of
that next window plus *Keep*. **Stopping one by hand** during its window keeps it down until
the next occurrence.

**A failed start** leaves the agent in `error`. The host is removed if it got that far, and
nothing is retried on its own, since retrying every sweep would only repeat the failure.
Fix the cause, which the row names, and press **Start**. A window-mode agent in `error`
also needs that press before its next window.

### Booking runs into a window

A Config Management run normally refuses an agent that is not online. For a deployed agent
that is parked until its window, a run **scheduled into that window** is accepted: book it
with **Run at** or **Change window** on the run form, at a time the agent will be up. A run
for any other time is refused, and the refusal names when the agent is next up.

## Routes

**Route cloud VMs in these ranges to it** (on by default) adds a
[Config-Management route](config-runs.md#which-agent-executes-a-run) for each range, with
**Also route cloud VMs in this range** ticked. That is the step that sends cloud-VM runs to
the agent. A range that already has a route is skipped, and the deploy says so.

**Delete** takes the host down, then revokes and deletes the agent and its routes. Runs for
those ranges then go back to the remote worker.

## The API

All on `/api/agent-hosts`, behind the remote agents feature flag. Reading needs
`agents:read`, deploying and changing need `agents:write`, and deleting needs
`agents:delete`.

| Method and path | Does |
|---|---|
| `GET /api/agent-hosts` | Every deployed agent, with its status and next up and down times |
| `GET /api/agent-hosts/options` | Default sizes, change windows and the identity a deploy would use |
| `POST /api/agent-hosts` | Deploy one |
| `PATCH /api/agent-hosts/{id}` | Change the lifecycle now, or the ranges, size and runs at once from the next start |
| `POST /api/agent-hosts/{id}/start`, `/stop` | Bring the host up or take it down now |
| `DELETE /api/agent-hosts/{id}` | Take it down and delete the agent and its routes |
