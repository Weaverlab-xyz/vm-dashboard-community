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

Every start mints a new identity for that one host. Which kind depends on the settings:

- **SPIRE with the cloud's instance identity** (recommended). This applies when **Let agents
  attest through SPIRE** is on, the dashboard's SPIRE server is in use, and the host's cloud
  is ticked under **Settings → SPIFFE and SPIRE → Deployed agents attest with their cloud's
  instance identity**.
  - **How it proves itself.** The host's SPIRE agent uses the VM's own identity document:
    `aws_iid`, `azure_imds` or `gcp_iit`. No secret is written into the user-data at all.
  - **How the dashboard pins the VM.** Once the VM exists, the dashboard makes this agent's
    node, `spiffe://<td>/node/<agent-id>`, a node entry that matches only that VM:
    - **AWS:** its instance id;
    - **Google Cloud:** its project, zone and name;
    - **Azure:** its subscription, resource group and name.

    Nothing else gets the agent's identity, even another VM in the same account.
  - **When the host stops,** the dashboard evicts that VM's node and deletes the entry.
- **SPIRE with a join token.** This applies when SPIRE attestation is on but the cloud isn't
  ticked.
  - The start does what **Migrate to SPIRE** does: it creates a node entry and a join token
    for this agent's SPIFFE ID.
  - The join token is written to a file, read into the SPIRE agent's environment, and
    deleted from the host.
- **Otherwise, a one-time enrolment code.** The agent reads it from a file at enrolment, and
  the script deletes the file two minutes later.

In both SPIRE modes:
- **The node key is on disk.** The SPIRE agent keeps it at
  `/var/lib/dashboard-agent-host/spire/data`, readable only by the SPIRE agent.
  - Every one of these node attestors attests a node **once**. A join token is single-use,
    and the three cloud attestors are trust-on-first-use.
  - So the key on disk is what lets the host come back after a reboot.
  - It rotates with every node SVID renewal.
- **Each start is a new VM,** so each start is a first attestation.
- **The agent's own signing key stays in memory.**

See [Attesting an agent through SPIRE](spire-attestation.md).

### Turning on a cloud's instance identity

Two steps, in this order. **Enable the plugin on the SPIRE server first**
(`examples/spire-server/server.conf`, or `examples/cloud-spire/server.conf` on a managed
runtime), restart the server, and **then** tick the cloud. The dashboard can't see the
server's plugins, so a cloud ticked without its plugin deploys hosts that never come
online. The start then fails after 15 minutes, naming the attestor to check.

| Cloud | Server `NodeAttestor` | Access the SPIRE server needs | Setting |
|---|---|---|---|
| AWS | `aws_iid` with `skip_block_device = true` and `disable_instance_profile_selectors = true` | Its own read-only identity allowed `ec2:DescribeInstances`, from environment variables or an instance role. Never the dashboard's keys. | Tick **AWS**. |
| Google Cloud | `gcp_iit` with `projectid_allow_list = ["<project>"]` | None: the identity token itself carries the project, zone and name that are checked. | Tick **Google Cloud**. |
| Azure | `azure_imds` with a `tenants` map keyed by the tenant **domain**, with `restrict_to_subscriptions` | An app registration with **Reader** on the subscription (`secret_auth`), or a managed identity (the default credential chain). | Tick **Azure** and set **Azure tenant domain** to the same key. |

On the host, the SPIRE agent shares the host's network, because it has to reach the
instance metadata service. Under IMDSv2's default hop limit of 1, a container on the Docker
bridge can't. It listens on nothing: its API is a unix socket.

**The SPIRE server must reach the cloud APIs it calls** (AWS and Azure), and every agent host
must still reach the SPIRE server, as with join tokens.

A host started with SPIRE attestation also gets the updater container, and an `upgrades:`
block in its policy that accepts only images the project's release workflow signed. The
Agents page can then upgrade or restart it like any other attested agent; see
[Upgrading and restarting agents centrally](lifecycle.md). A host starts the newest
release recorded there, pinned by digest, or `chrweav/dashboard-agent:latest` when none is
recorded. A host started with an enrolment code gets neither.

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
[Config-Management route](config-runs.md#which-agent-executes-a-run) for each range,
scoped to the agent's cloud and region. That is the step that sends cloud-VM runs to the
agent. A range that already has a route in that cloud and region is skipped, and the
deploy says so; an on-premises route for the same range is no clash. After the host's first successful start, those routes are also narrowed to the network
it runs in (its VPC, VNet or GCP network), since that is the only network the agent can
reach. Routes you narrowed or added yourself are left as they are.

**Delete** takes the host down, then revokes and deletes the agent and its routes. Runs for
those ranges then go back to the remote worker.

## Kubernetes work

An agent the dashboard deployed can also run a cluster's Kubernetes work, instead of the
remote worker: no cold start and no dashboard worker slot. Today that means Config
Management plays against the cluster. Each one hands the agent the cluster's
**cluster-admin kubeconfig**, so only an agent the dashboard owns qualifies. A
customer-hosted agent never does, whatever its policy says. All of these must hold:

- **The dashboard deployed it.** Its `agent_hosts` row is the proof; nothing the agent
  reports counts.
- **It attests through SPIRE**, whatever **Release dashboard-held credentials only to
  SPIRE-attested agents** is set to.
- **Allow Kubernetes work** is ticked for its host (in the deploy dialog, or later; turning it
  on takes effect at the next start, turning it off at once).
- **It runs agent 2.12 or later**, and is in the cluster's cloud.

**Then, on the Kubernetes page,** choose **Runs on worker** on a cluster and pick the agent.
The list shows every deployed agent in the cluster's cloud, with the reason any of them
cannot be chosen. An on-premises cluster keeps the dashboard's own runner.

**What the host gets.** At start, the dashboard writes a `k8s:` block into the agent's
policy and adds `agent_k8s` to its job types:

- **The image is pinned by digest.** It is `ansible_cloud_image` (kubectl, helm and
  `kubernetes.core`), resolved to a digest on Docker Hub when the host starts, and
  pre-pulled. If it cannot be resolved, Kubernetes work stays off for that start and the
  start job says so. The agent itself refuses a `k8s:` image that is not a digest.
- **Endpoints.** The block allows HTTPS and port 6443 to any address, because a cluster's
  API endpoint is public and specific to it. The agent's deny list (cloud metadata,
  loopback, link-local) still applies. Since the dashboard writes this policy, it is not an
  independent gate the way a customer's own `policy.yaml` is: that is exactly what makes
  this agent *owned*.

**How the work runs.**

- **The kubeconfig.** It is minted when the agent asks for it (a fresh short-lived cloud
  token), sealed to that agent, that job and the cluster's API endpoint, and lives only in
  the one-shot container.
- **The steps** are a closed set: apply, delete, helm upgrade, helm uninstall. The agent
  builds every command itself; no step can name one.
- **Fallback.** While the agent is offline, and its host is not about to be up, the work
  runs on the remote worker, and the job's Live Output says so.

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
