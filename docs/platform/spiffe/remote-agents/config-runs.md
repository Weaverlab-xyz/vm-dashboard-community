# Agent-executed Config Management

> **Audience:** operator · **Profile:** `both` · **Read this when:** a Config Management job has to run inside a network the dashboard cannot reach.

Part of [Remote Agents](../remote-agents.md). What the dashboard sends, the grants it needs, and what a run looks like.

## Agent-executed Config Management

Discovery finds hosts and hypervisor brokering powers them on. This applies a **playbook**
to them — to the *guests*, and to on-premises databases, over SSH or WinRM, from an
Ansible process that runs inside your network.

**Why this is the feature that makes on-prem hypervisor support worth having.** Most
on-prem hypervisors have no usable Terraform provider, so the dashboard cannot build their
VMs. All of those VMs speak Ansible. And a cloud-hosted dashboard cannot run Ansible against
them at all by itself: the "Local Docker" runner is a sibling container on the *dashboard's*
host, which has no Docker socket on ECS/ACI/Container Apps and no route to your LAN in any
case. Moving that container onto the agent's host is the whole change.

### What the dashboard sends, and what it does not

This is the part worth reading closely, because a playbook is executable content and the
[philosophy](../remote-agents.md#philosophy) says an agent must not have to trust the dashboard.

The signed job envelope carries **four scalars**: the kind of run, the transport, and the
target address and port. That is all. No playbook, no filename, no variable name, no
credential — `services/agent_ansible_meta.py` is a closed allowlist and a test asserts none
of its fields can name a thing to fetch or run.

Everything else arrives **sealed**, from `POST /api/agent/jobs/{id}/ansible-bundle`, encrypted
to an X25519 key the agent generated for that one fetch — the same mechanism as
[the credential the dashboard holds](credentials.md#the-credential-the-dashboard-holds), and for the same
reason: a signature proves the dashboard sent it, which is a weaker claim than the seal's
"sent for *this* agent, *this* job and *this* endpoint". The AAD binds the endpoint, so a
bundle released for one host cannot be relabelled as another's.

**The agent writes the Ansible inventory itself.** This is not tidiness. An inventory is a
place to write `ansible_connection: local`, which turns "configure that VM over SSH" into
"execute this playbook *inside the runner container*" — on your network, with the runner
image's `kubectl` and `helm` on `PATH`. `ansible_python_interpreter` and
`ansible_ssh_executable` are the same hole in different clothes, and `-e` outranks every
inventory variable in Ansible's precedence order, so filtering only the inventory would be
worth nothing. So the bundle carries **typed** credential fields — one key per meaning — the
agent renders the inventory from the four verified scalars, and **any `ansible_*` extra var
is refused by name**. The address in that inventory is the **resolved IP**, pinned, because
the policy check happens here and the connection happens seconds later in another container.

### Four grants, all required

Every one of them belongs to the agent that **executes** the run. By default that is the
agent whose hypervisor connection discovered the target, so "the agent" is unambiguous —
but a [Config-Management route](#which-agent-executes-a-run) can send runs for an address
range to a different agent, and then it is *that* agent's `policy.yaml` and *that* agent's
grant on the Agents tab that decide whether the run happens.

| Grant | Who owns it | Where |
|---|---|---|
| this agent may run `agent_ansible` | the dashboard operator | Agents page |
| `agent_ansible` in `job_types` | **you** | `policy.yaml` |
| `ansible: {enabled, vm_image, db_image, directory_image, targets}` + the Docker socket | **you** | `policy.yaml` + `docker-compose.sibling.yml` |
| the run's credential | the dashboard operator | the run form's secret / managed-account picker |

**`ansible.targets` is a separate list from `targets`, and that is the point.** The top-level
list grants a *port probe*; this one grants *a playbook running as root*. An operator who
widened a discovery sweep has not agreed to the second, so there is no fallback: an empty
`ansible.targets` means nothing can be configured, however wide `targets` is. Name the ports —
22 for Linux, 5985/5986 for Windows, the database port for a database.

Both images come from `policy.yaml` and never from a job. A job names only the *kind*:

```
docker pull chrweav/ansible-winrm:latest     # VM targets — SSH and WinRM
docker pull chrweav/ansible-cloud:latest     # database targets — the localhost play
```

The agent will not pull either for you, deliberately — a pull is a network fetch of
executable content.

### Which agent executes a run

By default, the agent whose hypervisor connection discovered the VM. That is usually
right, and while it is right there is nothing to configure.

It stops being right when the agent that can read a hypervisor's API has no network path
to the guests that hypervisor reports. The clearest case is VMware Workstation: `vmrest`
binds `127.0.0.1` and has no bind-address option, so the agent must run **on the Windows
host** under Docker Desktop — and Docker Desktop's Linux VM cannot reach a VMware vmnet at
all. The agent sees every guest and can reach none of them.

A **Config-Management route** separates the two. On **Remote Agents → Config Routes**, name
an address range and the agent that executes runs against it:

| Range | Executes on |
|---|---|
| `10.20.10.0/24` | `lab-runner` (a VM on that segment) |

Runs for an address in that range go to `lab-runner`. Everything else still goes to the
discovering agent. Matching is longest-prefix-first, so a `/32` overrides a `/24`, and one
range may name only one agent in one scope — a duplicate is refused rather than resolved
by guesswork.

#### Scopes: on-premises, or one cloud

An address alone does not name a machine. Every AWS region's default VPC is
`172.31.0.0/16`, and many sites use `192.168.1.0/24`, so one address can be an on-premises
guest, an EC2 VM and an Azure VM at once. Every route therefore says what it **applies
to**, and a run only ever consults routes for its own target:

| Applies to | Governs | Optional narrower |
|---|---|---|
| On-premises guests | VMs synced from a hypervisor connection | One connection: only guests it discovered |
| AWS, Azure or Google Cloud VMs | That cloud's dashboard-deployed VMs, by private address | Any of: one region, one workstream, one network |

So the same range can be routed once per scope — an on-premises route and an AWS route for
`10.40.0.0/16` live side by side and never capture each other's targets — and two regions,
or two connections, with the same range can each name their own agent. At an equal prefix
the route with more narrowers wins: one narrowed by network, region and workstream beats
one narrowed by two of them, and so on down to none.

**A workstream narrower stands for that workstream's network.** Two teams' VPCs in one
region often share a range like `10.0.0.0/16`, and region cannot tell them apart. Every
deploy records its workstream, and each workstream has its own network defaults, so a
route for `10.0.0.0/16` in AWS · us-east-2 · team-a governs only team-a's VMs. It is also a
tenancy line: one team's agent is never handed another team's VM, or the SSH key sealed
for it. The match is on the workstream a VM was **deployed** in. Reassigning a VM to
another workstream does not move its network, so it does not move which agent runs it.

**A network narrower is the exact answer** that region and workstream approximate: one AWS
VPC, Azure VNet or GCP network. The picker lists the networks this dashboard has deployed
into, with how many VMs each. New deploys record their network as they launch. VMs deployed
before that have none recorded, and **a VM with no recorded network never matches a route
narrowed to a network** — it falls back to a broader route, or to the remote worker, rather
than to a guess. The Config Routes page says how many there are and offers **Look up
networks**, which asks each cloud once; saving a network-narrowed route queues that lookup
on its own. A VM the cloud no longer has is skipped, not marked destroyed.

**A connection narrows which guests are covered, never who runs them.** The route's agent
still executes. A route narrowed to your Workstation connection means "guests that
connection discovered run on this agent" — the `vmrest` agent on the Windows host stays
the discoverer only.

**Routes from before scopes** keep every answer. A route without the old *Also route cloud
VMs* tick became an on-premises route. A route with it became four: on-premises, AWS, Azure
and Google Cloud, with the same range and agent. Delete the ones you never meant.

**What moves is who runs it, never what it runs against.** The target address is still
whichever address the *discovering* agent reported for that VM
([it needs an address](#before-a-vm-appears-as-a-target-it-needs-an-address) is unchanged),
and the route is applied to that address after it is fixed. A route cannot aim a playbook
somewhere the sync never reported.

**The routed agent needs its own grants** — all four above, in its own `policy.yaml`,
including an `ansible.targets` entry covering the range. Two agents means two policy files,
and the second one is the step people miss: the symptom is
`policy.yaml does not allow Config Management against …` from an agent that has never
appeared in a job before. The route form renders the exact block to paste.

Where you see the result: the target picker names the executing agent, the line under it
explains which route decided, and the job description records it as `… via <agent>`.

To undo it, clear **Active** on the route — runs go straight back to the discovering agent,
with no re-sync. Delete it if you do not want the record of the decision kept.

**A worked build of exactly this shape**, for two lab segments behind separate firewalls,
is in [`examples/remote-agent/config-mgmt-lab/`](../../../../examples/remote-agent/config-mgmt-lab/README.md):
two Config-Management-only `policy.yaml` files, a compose file with the Docker socket the
runner needs, and the dashboard-side steps in order.

### Before a VM appears as a target: it needs an address

`Get-VM` and `/api/vcenter/vm` describe the *VM*, not the guest inside it, so a synced row
has no address until something asks for one. Three things must all be true:

1. the guest is **powered on**;
2. guest tools are installed in it — Integration Services, VMware Tools, or
   `qemu-guest-agent`;
3. **`sync_guest_details: true`** on that connection in your `connections.yaml`.

The third is off by default because reading each guest's addresses costs an extra call per
VM on every sync, and an estate whose guests are not config targets should not pay it. Set
it, **Sync Now**, and the guests become individually selectable. Until then the row is
listed but disabled, with that reason on hover — which is the answer to "why is my VM not in
the list".

Supported on `hyperv`, `vsphere` and `proxmox`. Nutanix and XCP-ng report no guest address
yet and say so on the row rather than appearing broken. **Hyper-V needs a re-pulled
`chrweav/hypervisor-runner`** as well as the agent, because that is the image doing the
asking.

### On-premises databases

Register the database with `cloud = local` and bind it to an agent. The run is the same
`hosts: localhost` play the cloud databases use — `community.postgresql` / `mysql` /
`general` reaching *out* to the endpoint — except the controller is a container on the
agent's host instead of an in-cloud task. Its admin credential is checked out of Password
Safe just-in-time by the dashboard and sealed into the bundle, so nothing durable sits on
the agent.

### On-premises directories

An Active Directory or LDAP directory registered on the Directories page with an agent
(see [On-premises directories](../../../preview/directories/on-premises.md#discover-register-and-change-them))
is a third kind of run, `directory`, with two transports:

- **LDAP** (`local`): the same `hosts: localhost` shape as a database run, using
  `community.general.ldap_*` against the directory's host and port.
- **WinRM** (`winrm`): `microsoft.ad` modules on a domain controller, or on a
  domain-joined management host named on the run form, over HTTPS (5986). WinRM logs on as
  the directory's own account.

Both use the directory's **Password Safe managed account**, checked out by the dashboard for
that run and sealed into the bundle as `dir_host`, `dir_port`, `dir_use_ldaps`,
`dir_base_dn`, `dir_domain`, `dir_bind_dn` and `dir_bind_password`. The agent passes only
`dir_*` keys to the play and redacts the password from the output.

A directory run also needs **`directories:write`**, granted explicitly, on top of
`config_mgmt:write`, because it changes a directory with a domain credential.

The image is `ansible.directory_image`, falling back to `ansible.db_image`.
`chrweav/ansible-cloud` carries `python-ldap`, `pywinrm` and `microsoft.ad`, so one image
serves both transports. Add the directory's LDAP port, and 5986 for WinRM, to
`ansible.targets`. Directory runs need agent 2.8.0 or later.

The same run kind joins a new GCE Windows server to an on-prem domain through a
[GCP DNS link](../../../preview/directories/on-premises.md#extending-to-gcp-a-dns-link). The dashboard queues
it after the deploy, against the server's private address on 5986, so that address must be
in `ansible.targets` too.

### Cloud VMs

A dashboard-deployed AWS, Azure or GCP VM can also be configured from an agent: one running
inside that VM's VPC or VNet, instead of the remote worker. It is opt-in for each address
range. When a large estate outgrows the remote worker, it adds capacity outside the
dashboard's job worker; [Where work runs](../../where-work-runs.md#scaling-configuration-in-the-cloud)
says when that trade is worth making.

The dashboard can do all three steps for you, and keep the agent up only around a change
window: see [Deploying an agent into a cloud](deploying.md). By hand:

1. Run an agent inside the VPC or VNet, and grant it `agent_ansible`.
2. In its `policy.yaml`, give `ansible:` a `vm_image` and an `ansible.targets` entry for
   the subnet, port 22.
3. On **Remote Agents → Config Routes**, add a route for that range naming the agent, with
   **Applies to** set to the VMs' cloud — and the region, if another region uses the same
   range. The list shows how many cloud VMs each route covers.

From then on, that cloud's VMs whose private address is in the range appear under **Cloud
VMs (via agent)** in the target picker instead of under their cloud, and bulk runs from
the Inventory page send them to the agent as well. Another cloud's VMs, and on-premises
guests, at the same addresses are untouched: see
[Scopes](#scopes-on-premises-or-one-cloud).

**The address is the deploy's, never the request's.** The dashboard reads the VM's
**private** address off its deploy job, and only then works out which agent covers it. The
same ordering protects synced guests: a request may propose an address and an agent, but
neither decides. A run naming another agent is refused. So is a VM with no route: it runs
on the remote worker, never on some fallback agent.

**Its credentials follow the same rules as on the remote worker.** The SSH key is the
keypair the VM was built with, else the cloud's Ansible key. The login is the run's
`ansible_user`, else the administrator a Windows server's deploy recorded, else the
per-cloud user. The dashboard reads the key at the moment the agent asks, and seals it to
that agent for that one job. Tick **Release dashboard-held credentials only to
SPIRE-attested agents** on **Settings → Integrations → Remote Agents**, so an agent with a
key on disk never receives one.

**When the agent is offline.** By default a run routed to an agent that is not online is
refused, so nothing sits queued. To keep configuration going when the agent is down, tick
**Fall back to the remote worker when the agent is offline** on the route. A run then goes
to the remote worker, as if the VM had no route, and its Live Output says which agent it
fell back from. Some limits:

- **Cloud routes only.** The remote worker cannot reach on-premises networks, so an
  on-premises route cannot fall back.
- **Playbooks only.** The remote worker runs only playbooks against a cloud VM, so a
  script or package run is refused while the agent is down.
- **A booked run stays on its agent.** A run booked into a time an agent the dashboard
  deployed will be up still goes to that agent.

**Windows servers** are configured over OpenSSH, as the remote worker does it, and need
agent **2.9** or later. An older agent is refused when the run is queued, with the upgrade
named.

### Databases in a cloud

A database the dashboard provisioned in AWS, Azure or GCP can also be configured from an
agent in its network, through the same cloud route as that network's VMs.

**A route covers a database by its subnets.** Its endpoint is a DNS name on AWS and Azure,
so the dashboard records the database's subnets instead, when it provisions the database:

- **AWS:** every subnet in its DB subnet group, since a failover can land it in any of them.
- **Azure:** the delegated subnet (Flexible Server) or the private-endpoint subnet (SQL).
- **GCP:** its private IP.

A route covers the database when the route's range contains all of them. For a database
provisioned before this, use **Look up networks** on **Config Routes**; until then it has
no subnets recorded, matches no route, and stays on the remote worker. The Config Routes
list shows how many databases each route covers, and saving a cloud route looks them up
for you.

**The workstream a route is narrowed to is the one the database was provisioned in.**
Reassigning it later does not move it to another team's agent. A database looked up
afterwards counts its current workstream as that one.

**On the agent:**

- `ansible.db_image` must name the localhost image, `chrweav/ansible-cloud:latest`, pulled
  on the host. The agent never pulls an image itself.
- `ansible.targets` must cover the subnets on the engine's port: 5432 (PostgreSQL), 3306
  (MySQL), 1433 (SQL Server), 1521 (Oracle) or 27017 (MongoDB).

The route form shows the snippet. An agent the dashboard deployed gets both automatically.

The credential is the one the remote worker would use, read when the agent asks and sealed
to that agent for that one job. A route's **Fall back to the remote worker when the agent is
offline** covers its databases as well.

### Several runs at once

By default an agent runs one job at a time. To let it run several Config-Management runs at
once, set `ansible.max_concurrent` in its `policy.yaml` (agent 2.9 or later):

```yaml
ansible:
  enabled: true
  vm_image: chrweav/ansible-winrm:latest
  max_concurrent: 4
  targets:
    - cidr: 10.40.0.0/16
      ports: [22]
```

- **Allowed values:** a whole number from 1 to 8. Anything else stops the agent at start
  rather than being quietly clamped.
- **Host size:** each run is its own one-shot container, capped at 1 GiB, so size the
  host at about 1 GiB per slot.
- **Only Config Management runs in parallel.** Discovery, hypervisor sync, the Gateway and
  file shares still run one at a time, in their own slot.
- **The agent asks only for work it can start.** When every slot is busy it leases nothing.
- **The Agents page shows each agent's capacity**, as "N runs at once".

With more than one slot, `docker stop` lets running jobs finish for up to
`AGENT_DRAIN_SECONDS` (default 25), so give the container a longer `stop_grace_period`. A
job still running when the agent goes away is failed by the dashboard once its heartbeat
stops, as it always has been. To go past 8, run more agents and give each its own range.

### What the run looks like

A one-shot container, created per job and deleted after it: `--read-only`, `--cap-drop ALL`,
`no-new-privileges`, no bind mounts, 1 GiB of memory and a 128 MB `/tmp`. Every field of that
spec is a constant — a test asserts none of it comes from the job.

The playbook, the inventory, the SSH key and the vars go in through the Docker **archive**
API rather than the environment. That is a deliberate departure from the cloud runners'
`PLAYBOOK_B64` contract, and the reason is unforgiving: `execve` caps a *single* environment
string at 128 KB on a 4 KB-page host and 2 MB on a 64 KB-page arm64 one, so an env-delivered
playbook works on the machine it was developed on and fails on a customer's with `argument
list too long`. Files land mode 0600 and appear in no `docker inspect` output.

They are extracted into `/opt/job`, which is an **anonymous volume** — the one addition to
that otherwise mount-free spec, and not optional. The Engine refuses an archive extract with
`400 container rootfs is marked read-only` on any container whose `HostConfig` sets
`ReadonlyRootfs`, unless the destination resolves into a mount; it decides that from
`HostConfig` alone, so extracting before the container starts does not get around it. The
volume is anonymous, so it still names no path on your host, and it is removed with the
container. A `tmpfs` will not do in its place: it accepts the extract and is then mounted
empty over the top of it at start, which loses the run's files with no error at all.

Output **streams** into the existing Live Output pane, and the existing **Cancel** button
works: a watcher sends SIGTERM to the container, then SIGKILL after ten seconds. There is
also a wall-clock ceiling, `ansible.max_runtime_minutes` (default 30).

Ansible's own exit codes are reported in words rather than as a number, because "exit 4"
sends people to a search engine: 2 is "one or more hosts failed a task", 4 is "the target was
unreachable". A non-zero exit **fails** the job — it is never reported as a completed run,
which for config management would be the worst possible outcome.

### Requires an agent image of 2.3.0 or newer

The dashboard refuses to **queue** for an older one. An older agent has no `agent_ansible`
entry in its closed `HANDLERS` dict, so it would refuse the job into Live Output — where the
message reads as a `policy.yaml` problem and sends you to edit the wrong file. Refusing at
enqueue puts the remedy where it is legible and leaves no job row behind.
