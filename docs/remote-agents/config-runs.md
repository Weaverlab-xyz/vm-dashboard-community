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
[philosophy](../remote-agents.md#philosophy) above says the dashboard is not trusted.

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

| Grant | Who owns it | Where |
|---|---|---|
| this agent may run `agent_ansible` | the dashboard operator | Agents page |
| `agent_ansible` in `job_types` | **you** | `policy.yaml` |
| `ansible: {enabled, vm_image, db_image, targets}` + the Docker socket | **you** | `policy.yaml` + `docker-compose.sibling.yml` |
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
