# Hypervisor connections through an agent

> **Audience:** operator · **Profile:** `both` · **Read this when:** a hypervisor the dashboard cannot route to needs managing.

Part of [Remote Agents](../remote-agents.md). The verbs, what the agent can reach, the sibling runner and VMware Workstation Pro.

## Hypervisor connections

Once an agent is enrolled it can also **broker hypervisor operations** — inventory sync
on a schedule, and power verbs on demand — for vCenter, Proxmox, Nutanix, XCP-ng, Hyper-V
and bare ESXi endpoints the dashboard has no route to. Which of the two each product gets
is [its own table](#what-the-agent-can-and-cannot-reach); Nutanix syncs but cannot be
powered, and a page whose button has no agent verb says so rather than sending the
nearest one.

### The dashboard may hold the secret, never the target

A dashboard connection row bound to an agent stores the agent's **name for it** and never a
host or a username. The job says *"run `inventory_sync` on `dc1-vcenter`"* and the agent
resolves the endpoint from its own
[`connections.yaml`](../../../examples/remote-agent/connections.example.yaml).

This binding decides which agent **discovers and powers** the estate. Which agent
**configures** the guests it finds is a separate question, because the two are not always
the same host — see
[which agent executes a run](config-runs.md#which-agent-executes-a-run).

That asymmetry is the difference between this and a proxy, and it is not about secrecy — it
is about aiming. A dashboard that could set `host` could redirect the agent's authenticated
session at an endpoint of its choosing and harvest the credential on first use; one that
could set `username` could spray a known password across accounts. So those stay yours, in
your file, gated by your `policy.yaml`, which no dashboard API can reach.

The credential is the part that can move. The recommended place for it is the dashboard,
fetched per job by an agent attested through SPIRE. That is the last row below, and
[Central storage with SPIRE](credentials.md#central-storage-with-spire-the-recommended-model)
explains why. The other rows stay supported:

| Where the credential lives | An attacker who reads files on the agent host gets | An attacker who compromises the dashboard gets |
|---|---|---|
| `password` / `password_file` | **the hypervisor password.** Offline, permanent, and a file read is not an event anybody sees | a verb and a name |
| `password_sealed` | **the hypervisor password**, if they can read the state volume as well as the config file — and **nothing** from a copy of the config file alone. See [Sealing a credential this host keeps](credentials.md#sealing-a-credential-this-host-keeps) | a verb and a name |
| `ps_managed_account` | a Password Safe OAuth client — usually entitled to more than the one account | a verb and a name |
| `dashboard_secret` + a stored password | the agent's identity key: the ability to *request* a credential while a job runs, audited and revocable | the password |
| `dashboard_secret` + `ps_account://` | the same narrow request ability | a Password Safe account id, subject to its policy, approval workflow and rotation |
| `dashboard_secret` + `ps_account://`, agent attested through SPIRE, release limited to SPIRE-attested agents | **no agent key**: it is only in memory and is replaced on every restart. On a cloud VM, nothing at all. On a join-token host, the SPIRE agent's own key, which works only until SPIRE rotates it | a Password Safe account id, subject to its policy, approval workflow and rotation |

The last two rows are the only configurations in which **neither** side holds a standing
hypervisor credential. The last row is the only one in which a copy of the agent host's
disk yields nothing durable either. It is the recommended configuration.

A host-side credential (`password`, `password_file`, `password_sealed`) is no longer the
recommendation, but it stays available. Nothing is migrated for you: a connection takes its
credential from the dashboard only once its entry in your `connections.yaml` says
`dashboard_secret: true`.

The cost is two files joined by a string, and a typo in either yields
`unknown connection 'dc1-vcenter'`. The connection form mitigates that by offering an
agent picker rather than a free-text uuid.

### Four grants, all required

Nothing runs unless all four agree, and they belong to different people:

| Grant | Who owns it | Where |
|---|---|---|
| this agent may run `agent_hypervisor` | the dashboard operator | Agents page |
| this verb is allowed on this connection | **you** | `policy.yaml` → `connections:` |
| this connection exists, and where its credential comes from | **you** | `connections.yaml` |
| the credential itself, when `dashboard_secret` is set | the dashboard operator | Connections tab |

Withhold any one and nothing happens. The fourth is the only one the dashboard owns, and it
owns it only because you said so in the third — a credential set on the Connections tab for
an agent-bound connection does nothing at all until your file opts that entry in. A refusal
from the second or third arrives in
Live Output naming the file and the line to add — the dashboard cannot fix it and does
not pretend to.

### The verbs

`inventory_sync` is read-only and runs on a schedule (default every 30 minutes;
override per connection with `options.sync_interval_minutes`). `power_on`, `power_off`,
`power_reset`, `shutdown`, `reboot` and `restart` are on-demand, issued by the existing
power buttons on the hypervisor pages when the resolved connection is agent-bound.

`set_tags` writes a guest's tags, and is **Proxmox only** — it is the one product here
whose tags the dashboard reads, and vSphere, Nutanix and XCP-ng each model them
differently. It is also the first verb to carry a value an operator typed, so two things
are true of it that are not true of the others:

* Every tag is checked against Proxmox's own `pve-tag-id` charset — letters, digits,
  underscore, hyphen, plus and dot, starting with a letter, digit or underscore — by the
  dashboard before a job row exists, **and again by the agent** before the call. A token
  that fails is refused by name, never repaired: a repaired tag is a different tag.
  Nothing matching that charset can express a path, a URL, a shell metacharacter or a
  space, which is what keeps the payload allowlist's promise intact.
* The job carries the **complete desired tag set**, not a delta. Proxmox stores tags in
  one config field and replaces it wholesale, so the dashboard reads the current set,
  applies your add/remove, and sends the result. An empty list is therefore a real
  instruction — "remove the last tag" — not a missing field.
* That read is the agent's last **inventory sync**, so the sync has to report tags. Agents
  built before it did sent no tags at all, and an edit computed against that gap
  **overwrote every tag the guest already had** with just the ones you added. The
  dashboard now keeps "this agent did not report tags" apart from "this guest has none",
  and it refuses the edit for that guest. The refusal tells you to update the agent and
  run **Sync Now**. After one sync from an updated agent, the guest's tags appear in the
  table and on `/inventory`, and edits preserve them.

`set_tags` also queues an `inventory_sync` on completion, like the power verbs and unlike
`snapshot`, because `tags` is a column the cache stores: without it the page would keep
showing the pre-edit tags and the *next* edit would be computed against them.

**It needs a re-pulled agent and a policy line.** Add `set_tags` to that connection's
`verbs:` list in your `policy.yaml`. An agent that predates the verb refuses it out loud —
`policy.yaml` cannot grant a verb that did not exist when it was written, so the refusal
names the file and the line to add rather than failing silently.

A sync that comes back with **no VMs at all** is not applied to a connection that already
has some. A zero-VM pass is how a *deleted* VM stops being listed — the sync removes every
row it did not just write — and it is also exactly what an agent hands back when it could
not read the host: on Hyper-V, an unloaded PowerShell module and a service account that
cannot enumerate VMs both produce the same empty list. The agent therefore states which
one it is, and an agent too old to say so never empties a cache. The rows stay, and the
connection carries the reason on the Connections tab (hover the red **Error**) instead
of the page going quietly blank. The cost is the mirror image: a host you have genuinely
emptied keeps showing its last VMs until you re-pull the agent — and, for Hyper-V or
ESXi, the `chrweav/hypervisor-runner` image with it.

One `inventory_sync` is also queued automatically whenever a power job finishes, against
the connection it acted on, so the page reflects a Start or a Stop without anyone pressing
Sync Now. It is attributed to whoever pressed the button and appears on the Jobs page as
`Inventory sync: <connection> (page 1) — after power_off`. Two things follow from where it
sits: a power op that *failed* queues one too (an agent that lost the response to a call
it did make is indistinguishable from one that never made it, and the VM may well have
moved), and a burst of power ops queues **one** sync rather than one each — the last op to
finish queues it, and it sees everything the burst moved. A graceful `shutdown` or
`reboot` is the case where one press is not enough: the sync runs seconds later, while the
guest is still on its way down, so it honestly records a VM that is still running.

### Syncs are hidden on the Jobs page

A connection polled every 30 minutes writes 48 job rows a day, more if its inventory
pages, and every one of them completes green with nothing for anyone to do. So the Jobs
page **hides completed inventory syncs by default** — tick **Show hypervisor check-ins**
to see them. It is a separate box from **Show routine sweeps** because these are a cadence
you set per connection, and a stale inventory is diagnosed by reading the syncs without
the dashboard's own housekeeping on top.

Three things are never hidden, which is what makes the default safe to leave on:

- **Power ops.** A `power_off` is the same `agent_hypervisor` job type as a sync — one
  agent handler, one grant — but it is an operator action, and it stays on the page.
- **A sync that failed or was cancelled.** That is a connection whose cached inventory is
  now out of date, so it stays visible here and in the dashboard's failed-jobs panel.
- **A sync that is still running**, including the one you just queued with Sync Now.

Nothing is deleted. Unlike the auto-delete sweep's rows, a hidden check-in stays in the
database and comes back the moment you tick the box.

### Pages

The larger parts have a page each:

| Page | Read it when |
|---|---|
| [Powering hypervisor VMs through an agent](hypervisors/power.md) | you want VMs on an agent-run hypervisor started, stopped or suspended, one at a time or as a selection |
| [VMware Workstation Pro through an agent](hypervisors/vmware-workstation.md) | your VMs run on VMware Workstation Pro on a machine the dashboard reaches through an agent |

### What the agent can and cannot reach

| Product | Transport | Inventory | Power |
|---|---|---|---|
| vCenter | vSphere Automation REST API | yes | on/off/reset, plus shutdown via the separate `guest/power` endpoint (needs VMware Tools) — Suspend is refused, see above |
| Proxmox VE | `/api2/json` + API token | yes | on/off/shutdown/reboot — the full set the page offers |
| XCP-ng | XAPI (stdlib XML-RPC) | yes | on/off/shutdown/reboot/hard reboot — Suspend, Resume, Pause and Unpause are refused, see above |
| Nutanix Prism | Prism v3 REST | yes | no — a v3 power change is a full spec PUT with a metadata version, not an action |
| VMware Workstation Pro | `vmrest`, on the same host | yes | on/off only — vmrest has no reset, reboot or snapshot |
| bare ESXi | SOAP, via the [sibling runner](#the-sibling-runner) | yes | the same verbs as the runner carries |
| Hyper-V | WinRM, via the [sibling runner](#the-sibling-runner) | yes | Start, Force Off, Shutdown (`Stop-VM`, needs Integration Services) and Restart (`Restart-VM -Force`, a hard restart). Reboot has no cmdlet at all; Pause, Resume and Save have no verb — all refused rather than approximated, see above |

`esxi` is a distinct connection kind from `vsphere` on the agent's side even though the
dashboard has only `vsphere`: same product, different transport, and the agent is the only
side that knows which one a given endpoint is. A job for `vsphere` is served by an `esxi`
connection; the reverse is refused, because that direction would send SOAP work to a
vCenter.

Four of six products over REST, and **no new agent dependency for any of them**. That is
why the agent image still installs only `requests`, `PyYAML` and `cryptography`: those two
audit tests are the security argument in executable form. The remaining two get their heavy
dependencies in a container that lives for seconds, which keeps the long-lived supervisor
inert — the property `test_the_agent_imports_no_execution_machinery` protects.

### The sibling runner

Hyper-V and **bare ESXi** are the only two the agent cannot talk to itself: WinRM is SOAP
with NTLM/Negotiate, and a standalone ESXi host serves only the SOAP API. Rather than put
a real auth stack and pyVmomi into an image whose three-dependency restraint *is* the
security argument, those two run in a one-shot container the agent creates, reads one line
of JSON from, and deletes.

**This needs the Docker socket, and the socket is root on the host.** It is not mounted by
the default deployment — [`docker-compose.yml`](../../../examples/remote-agent/docker-compose.yml)
still promises the agent launches nothing, and that stays true. Turning it on is a
separate, deliberate act:

| Grant | Who | Where |
|---|---|---|
| the `agent_hypervisor` job type | dashboard operator | Agents page |
| `sibling: {enabled, image}` | **you** | `policy.yaml` |
| the socket itself | **you** | [`docker-compose.sibling.yml`](../../../examples/remote-agent/docker-compose.sibling.yml) |

Withhold any one and nothing runs. Prefer the rootless Docker or Podman user socket; the
example overlay uses the rootless path deliberately, so reaching for the root socket has to
be a conscious edit. On **Docker Desktop** that path does not exist and the socket needs its
group set — see [The sibling runner on Docker Desktop](agent-host.md#the-sibling-runner-on-docker-desktop).

What the agent does with it is deliberately narrow. Every field of the container spec is a
constant or comes from your policy — the image, the network, and a `HostConfig` with no
bind mounts, no capabilities, a read-only root filesystem and `no-new-privileges`. **None
of it is derived from anything the dashboard sends**, because there is no field through
which to ask; a test asserts that. The credential rides in the environment of the create
call rather than argv, so it never appears in `ps` on the host. Containers are labelled and
orphans from a crashed agent are swept at startup.

The agent will not pull the image for you. A pull is a network fetch of executable content,
and that is your decision rather than a job's:

```
docker pull chrweav/hypervisor-runner:latest
```

### Large inventories

A sync returns one **page** plus an opaque cursor; the dashboard applies it and enqueues
the next. The chain is capped at 40 pages (10 000 VMs), which is what stops a
misbehaving agent making the dashboard enqueue work forever. Every page of one sync
shares a `batch_id`, so N job rows roll up as one run on `/jobs`.

The cap that forces this is `MAX_RESULT_BYTES` (256 KB), and raising it is not an
option — it is the only bound on an agent's write path into the database.
