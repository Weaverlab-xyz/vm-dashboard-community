# Where work runs: remote workers and remote agents

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are deciding how the dashboard should reach the infrastructure it manages, before you set up a remote worker or a remote agent.

Part of [Platform](../platform.md). This is the high-level picture. The two pages it points
at, [Remote Worker](remote-worker.md) and [Remote Agents](spiffe/remote-agents.md), have
the detail.

## Two ways to do the work

Most of what the dashboard does is a call to a cloud API, and the dashboard makes that
call itself. Some work has to happen *on* or *beside* infrastructure instead: running a
playbook against a VM, applying a manifest to a cluster, converting a disk image,
inventorying a hypervisor, hosting a Gateway. That work has to run somewhere with a route to its target, and the dashboard
offers two ways to put it there:

- **A remote worker** is a cloud-native, one-shot job. For each piece of work the dashboard
  launches a container in the target's own cloud (ECS Fargate on AWS, Azure Container
  Instances, a Cloud Run job on GCP), the container does the job, and then it is destroyed.
  Nothing is left running between jobs. It is **entirely ephemeral**.
- **A remote agent** is a container you run inside a network. It dials out to the dashboard
  over HTTPS, asks for work, does it and reports back. It needs no inbound firewall rule,
  VPN or port forward. It is **persistent**: it stays running and waiting for work.

Both run configuration jobs: a Config Management playbook or script can go either way and
end up with the same result. Beyond configuration, each does a good deal more, and not the
same things (see [What each one does](#what-each-one-does)). Customers can use either
approach, or both side by side. The dashboard does not prefer one over the other.

A third, simpler option exists for a dashboard that already sits on the same network as
its targets: the **local** runner, a sibling container on the dashboard host. It is one of
the remote worker's backends; see [Remote Worker](remote-worker.md).

## Comparing them

| | Remote worker | Remote agent |
|---|---|---|
| **Lifetime** | One container per job, destroyed when it exits | One long-running container that polls for jobs |
| **Where it runs** | In the target's cloud, on that cloud's network | Anywhere you can run a container: a datacenter, a branch office, a cloud VM, a laptop |
| **What it can reach** | What that cloud network can reach: the VPC or VNet and anything peered or routed to it | Whatever the network it sits in can reach, including networks the dashboard and the clouds have no route to |
| **Who runs it** | The dashboard launches it on demand | You, or the owner of the network it sits in |
| **Network setup** | A cluster, subnet, security group and role in each cloud you use | Outbound HTTPS 443 to the dashboard, nothing inbound |
| **Identity** | The cloud task's own role, for the length of one job | Attests to the dashboard through SPIFFE at every start (recommended), and signs every request |
| **Trust boundary** | The job's lifetime: nothing survives it | `policy.yaml` on the agent's host, which the dashboard cannot read or change |

### The remote worker: ephemeral

**Pros**
- Nothing persists. No standing host to patch, monitor or secure between jobs, and nothing
  left behind that a later job, or an attacker, could reuse.
- Every job starts from a clean, known image.
- It runs on the target cloud's network with that cloud's identity, so a private subnet
  needs no route back to the dashboard.
- It gets clean egress, which side-steps a TLS-inspecting corporate proxy that would
  otherwise break calls to a private cluster API.
- It scales per job and costs nothing when idle.

**Cons**
- It can only reach what its cloud network can reach. A datacenter with no connection to
  that cloud is out of reach.
- Each cloud you use needs its own setup: cluster, subnet, security group and role.
- Every job pays a cold start.

### The remote agent: persistent

**Pros**
- It reaches places the dashboard cannot, such as on-premises datacenters and isolated
  networks, with only an outbound connection.
- It runs anywhere a container runs, in any cloud or none.
- The host owner keeps the security boundary in a file they own, `policy.yaml`. That suits a
  network whose owner is not the dashboard's operator, often a different company.
- It is already there when a job arrives, so there is no cold start.

**Cons**
- It is a long-running process that someone has to host, update and monitor.
- It is a standing identity on someone else's network. That is why the dashboard does not
  trust an agent until it attests, and why attesting through SPIFFE is the recommended way to
  run one.
- Work waits for the next poll, every 5 seconds by default.

## What each one does

Configuration is the overlap. Everything else is specific to one of them.

### Remote worker

- **Config Management on cloud VMs**: Ansible playbooks, and `.sh`, `.ps1`, `.rpm` and
  `.deb` assets, over SSH or WinRM, from a task in the VM's own cloud.
- **Config Management on databases and Kubernetes clusters**: `localhost` plays that reach
  out to the target over its database login or kubeconfig.
- **Kubernetes cluster operations** (the Kubernetes runner): `kubectl` and `helm` against a
  cluster's API, behind the Entitle agent install, the External Secrets Operator rollout,
  management-plane operations and reading service-account token secrets.
- **Image promotion**: converting a built VM image and uploading it into AWS, Azure or GCP's
  image library.
- **Rancher API calls** from inside the node's own cloud, when a TLS-inspecting proxy
  blocks the node's certificate (`rancher_api_transport` = `runner`).

### Remote agent

- **Config Management on on-premises targets**: VMs, databases and AD or LDAP directories.
  The agent runs each job in a one-shot container beside it, so the run itself is
  ephemeral even though the agent is not.
- **Config Management on cloud VMs, if you opt in**: dashboard-deployed VMs in the VPC or
  VNet an agent runs in, routed to it by address range. See
  [Scaling configuration in the cloud](#scaling-configuration-in-the-cloud).
- **Hypervisor discovery, inventory and power** for vCenter, ESXi, Proxmox, Hyper-V and
  VMware Workstation behind a firewall.
- **Discovery** of on-premises databases, Kubernetes clusters, domain controllers and LDAP
  servers.
- **Brokered file shares**, by name, without the dashboard holding a path into the network.
- **Hosting a BeyondTrust Gateway** on the agent's host.

Most of the agent's list needs something already present inside the network, which a
cloud task launched on demand cannot be. Most of the worker's list needs a cloud's own
network and identity, which an agent on someone else's network does not have.

## Choosing

- **The targets are in a cloud the dashboard uses:** a remote worker is the natural fit,
  with nothing to keep running.
- **The targets are on-premises, or on any network the clouds cannot route to:** use a
  remote agent.
- **Both:** use both. Each job goes wherever its target is reachable from.
- **Many cloud VMs, configured often:** consider remote agents in those VPCs or VNets; see
  [Scaling configuration in the cloud](#scaling-configuration-in-the-cloud).

## Scaling configuration in the cloud

On a large cloud estate, the remote worker's limit is the dashboard, not the clouds. Each
cloud-VM run holds one of the dashboard's job-worker slots for as long as it runs. There
are at most four heavy slots per worker, and every line of Live Output is its own database
write. Raising the caps or adding worker replicas puts more load on the database, which is
why [the job worker docs](../operations/job-worker.md#running-more-than-one-worker) advise
raising the caps before adding replicas.

Remote agents add capacity outside the dashboard's worker:

- **Opt in per address range.** On **Remote Agents → Config Routes**, add a range covering
  a VPC or VNet, name the agent that runs inside it, and set **Applies to** to that cloud
  (and, optionally, its region). Config Management runs on that cloud's dashboard-deployed
  VMs whose private address is in the range then go to the agent, single runs and bulk
  runs alike. VMs outside every such route stay on the remote worker.
- **Run several at once.** Set `ansible.max_concurrent` in the agent's `policy.yaml`
  (agent 2.9 or later, up to 8). Each run is still a one-shot container beside the agent,
  so size the host at about 1 GiB per slot. To go further, run more agents and give each
  its own range.
- **What uses no worker slot.** An agent leases its jobs directly, so a run on it holds
  none of the dashboard's job-worker slots. It sends Live Output in batches of up to 500
  lines a request; the dashboard still records each line, which is what lets the output
  stream and a run be cancelled mid-way.

The tradeoffs to accept:

- **A host.** Each agent is a host with a Docker socket, in place of nothing at all
  between jobs. The dashboard can build it for you and keep it up only around a change
  window, so nothing stands between windows: see
  [Deploying an agent into a cloud](spiffe/remote-agents/deploying.md).
- **The VM's SSH key leaves the dashboard.** For each run, the dashboard seals the key to
  that one agent, for that one job. Turn on **Release dashboard-held credentials only to
  SPIRE-attested agents**, so an agent with a key on disk never receives one.

The details are in [Agent-executed Config Management → Cloud VMs](spiffe/remote-agents/config-runs.md#cloud-vms).

## Read next

| Page | Read it when |
|---|---|
| [Remote Worker](remote-worker.md) | you are configuring the one-shot cloud runners for Ansible, Kubernetes and image promotion |
| [Remote Agents](spiffe/remote-agents.md) | you need to reach a network the dashboard cannot, and want the trust model, the architecture and the agent's pages |
| [How a Config Management run executes](../operations/config-management/execution-paths.md) | you want to know which path a single run takes |
| [Why one-shot runners](../operations/config-management/runners.md#why-one-shot-runners-the-security-argument) | you want the security argument for ephemeral runners |
