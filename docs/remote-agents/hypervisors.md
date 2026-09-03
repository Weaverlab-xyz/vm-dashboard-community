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
[`connections.yaml`](../../examples/remote-agent/connections.example.yaml).

That asymmetry is the difference between this and a proxy, and it is not about secrecy — it
is about aiming. A dashboard that could set `host` could redirect the agent's authenticated
session at an endpoint of its choosing and harvest the credential on first use; one that
could set `username` could spray a known password across accounts. So those stay yours, in
your file, gated by your `policy.yaml`, which no dashboard API can reach.

The credential is the part that can move, and there is no single right answer:

| Where the credential lives | An attacker who reads files on the agent host gets | An attacker who compromises the dashboard gets |
|---|---|---|
| `password` / `password_file` | **the hypervisor password.** Offline, permanent, and a file read is not an event anybody sees | a verb and a name |
| `password_sealed` | **the hypervisor password**, if they can read the state volume as well as the config file — and **nothing** from a copy of the config file alone. See [Sealing a credential this host keeps](credentials.md#sealing-a-credential-this-host-keeps) | a verb and a name |
| `ps_managed_account` | a Password Safe OAuth client — usually entitled to more than the one account | a verb and a name |
| `dashboard_secret` + a stored password | the agent's identity key: the ability to *request* a credential while a job runs, audited and revocable | the password |
| `dashboard_secret` + `ps_account://` | the same narrow request ability | a Password Safe account id, subject to its policy, approval workflow and rotation |

The default is unchanged and stays available. The last row is the only configuration in which
**neither** side holds a standing hypervisor credential.

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
| the credential itself, when `dashboard_secret` is set | the dashboard operator | Connections page |

Withhold any one and nothing happens. The fourth is the only one the dashboard owns, and it
owns it only because you said so in the third — a credential set on the Connections page for
an agent-bound connection does nothing at all until your file opts that entry in. A refusal
from the second or third arrives in
Live Output naming the file and the line to add — the dashboard cannot fix it and does
not pretend to.


### The verbs

`inventory_sync` is read-only and runs on a schedule (default every 30 minutes;
override per connection with `options.sync_interval_minutes`). `power_on`, `power_off`,
`power_reset`, `shutdown`, `reboot` and `restart` are on-demand, issued by the existing
power buttons on the hypervisor pages when the resolved connection is agent-bound.

A sync that comes back with **no VMs at all** is not applied to a connection that already
has some. A zero-VM pass is how a *deleted* VM stops being listed — the sync removes every
row it did not just write — and it is also exactly what an agent hands back when it could
not read the host: on Hyper-V, an unloaded PowerShell module and a service account that
cannot enumerate VMs both produce the same empty list. The agent therefore states which
one it is, and an agent too old to say so never empties a cache. The rows stay, and the
connection carries the reason on the Connections page (hover the red **Error**) instead
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

Not every button on those pages has one of them behind it, and the ones that do not are
**refused rather than approximated**. `restart` is why the mapping is per product rather
than shared: each kind resolves it differently — Proxmox `/status/shutdown` (graceful),
vSphere `?action=reset` (a hard reset), XCP-ng `VM.clean_reboot` (a reboot), Hyper-V
nothing at all. `shutdown` and `reboot` exist because that made every Shutdown button
unmappable, and one Reboot button too:

| Button | Proxmox | vCenter | XCP-ng | Hyper-V |
|---|---|---|---|---|
| Power On / Force Off | yes | yes | yes | yes |
| Shutdown (graceful) | yes — `/status/shutdown` | yes — `guest/power?action=shutdown`, **needs VMware Tools** | yes — `VM.clean_shutdown` | yes — `Stop-VM`, **needs Integration Services** |
| Reboot / Restart | yes — `/status/reboot` | not offered | yes — `VM.clean_reboot` | yes — `Restart-VM -Force`, a **hard** restart |
| Reset / Hard Reboot | not offered | yes | yes | not offered |
| Suspend / Resume / Pause / Unpause / Save | not offered | **refused** | **refused** | **refused** |

Two of those are graceful in the strict sense — they ask software *inside* the guest. A
vCenter Shutdown is not a power action at all but a call to `/api/vcenter/vm/{vm}/guest/
power`, and it answers 503 when VMware Tools is not running; the agent turns that into a
message saying so, because "answered 503" on a plainly-running VM points nowhere. Hyper-V
Shutdown is bare `Stop-VM`, which Microsoft documents as shutting down "through the guest
operating system" — it carries neither `-TurnOff` (the power cut) nor `-Force`, which on
`Stop-VM` means "regardless of any unsaved application data" and would quietly make it a
different promise. Use Force Off when the guest cannot answer.

Hyper-V is the one product with **no graceful reboot at all**: `Restart-VM` is documented
as a "hard" restart, "like powering the computer down, then back up again", so it is what
the Restart button's `power_reset` runs and there is nothing left for `reboot` to be. The
agent refuses that combination by name rather than letting the runner answer "unknown
verb", which would read as an agent too old for the dashboard.

The mapping is one table, [`agent_hypervisor_meta.PAGE_OPS`][page-ops], keyed by kind —
it was once a copy per router, and three identical copies is how `shutdown` came to hard
-reset a vCenter VM. A refused button is greyed out on the page with the reason in its
tooltip, and the endpoint answers 501 naming the substitution that would have been wrong
and the buttons that do work. The refusal happens before a job row exists, so nothing
appears on /jobs.

**Adding a verb is a three-file change, and a partial one is worse than none.** The
dashboard normalizes an unrecognised verb to `inventory_sync`, so a verb granted here but
missing from an agent would run a discovery scan and report success. The three are this
allowlist, the agent's per-kind maps, and the sibling runner.

Version skew across them is safe in the direction it actually happens — the agent is
deployed separately and lags. An old agent given a new verb refuses it out loud: it reads
the verb raw and never normalizes, so `policy.check_verb` rejects it first, naming
`policy.yaml` and the line to add. **This is why `shutdown` and `reboot` are new verbs
rather than a redefinition of `restart`:** redefining a verb an old agent already
implements, and an old `policy.yaml` already grants, would silently change what that
agent does with a button — the exact failure the whole table exists to prevent.

The corollary is operational: after upgrading the dashboard, **every deployed agent must
be re-pulled** before its Shutdown button works, and until then those agents refuse the
verb in Live Output. Proxmox Shutdown is the one button this affects that worked before —
it used to ride `restart`, which really is `/status/shutdown` on Proxmox alone. It was
moved anyway, because leaving it would have kept "restart means shutdown here" alive as a
per-kind special case, and that reading is what made Reboot unmappable in the first place.

[page-ops]: ../web_dashboard/services/agent_hypervisor_meta.py

**`snapshot`** creates a snapshot named `dash-<job id>`. The name is *generated*, never
supplied — which is exactly why it was held back at first: a created thing needs a name,
and a name is a free-form string. There is still no field in the protocol through which
operator text could reach a hypervisor, and the job id makes the snapshot traceable back to
the row that made it, which a typed name would not be.

Deliberately absent: **deploy / clone / delete / console** — they need sizes, networks and
cloud-init, a payload shape indistinguishable from a config file, and a config file is one
step from a script. Those stay dashboard-direct.

`power_off` and `power_reset` are separate verbs rather than one verb with a `force`
flag, because a boolean on a destructive verb gets defaulted wrong exactly once.


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
the default deployment — [`docker-compose.yml`](../../examples/remote-agent/docker-compose.yml)
still promises the agent launches nothing, and that stays true. Turning it on is a
separate, deliberate act:

| Grant | Who | Where |
|---|---|---|
| the `agent_hypervisor` job type | dashboard operator | Agents page |
| `sibling: {enabled, image}` | **you** | `policy.yaml` |
| the socket itself | **you** | [`docker-compose.sibling.yml`](../../examples/remote-agent/docker-compose.sibling.yml) |

Withhold any one and nothing runs. Prefer the rootless Docker or Podman user socket; the
example overlay uses the rootless path deliberately, so reaching for the root socket has to
be a conscious edit.

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


### VMware Workstation Pro

Workstation is the one hypervisor here that runs on somebody's desktop — nearly always a
**Windows** desktop, so read [Running on Windows](agent-host.md#running-on-windows) alongside this. It was
twice written off as unreachable because the dashboard drives it with `vmrun` against local
VMX paths. That was true of `vmrun` and wrong overall: Workstation **Pro** ships `vmrest`, a
REST daemon that is plain JSON over HTTP. An agent on that host reaches it with no extra
dependency and no container.

On the Workstation host:

```
vmrest -C     # set the API credentials, once
vmrest        # run the daemon — 127.0.0.1:8697
```

Then add a `workstation` connection bound to that host's agent. It is **agent-bound
only**: the dashboard has no transport for Workstation, so a connection without an agent
is refused rather than created and left broken.

**`vmrest` binds `127.0.0.1`, and that address means something different inside a
container.** The agent denies loopback unconditionally — that deny is what stops a discovery
sweep probing the agent's own container or a cloud metadata endpoint, and it is re-added even
if an operator deletes it. But lifting the deny is only half the problem: inside the
container `127.0.0.1` *is the container*, so the address has to be one that actually reaches
the host as well as one the policy permits. Which of the two you need depends on how the
agent is attached to the network:

- **Docker Desktop on Windows or macOS** — point the connection at
  `host.docker.internal:8697` and add it as a target. Docker Desktop routes that name into
  the host's own network namespace, which is what makes a loopback-bound `vmrest` reachable
  at all. It is not a loopback address from the agent's side, so `allow_loopback` is *not*
  needed:

  ```yaml
  targets:
    - fqdn: host.docker.internal
      ports: [8697]
  ```

  The name is resolved once when the policy loads and pinned to the address it returned, so
  the allow-list is in IPs by the time a connection is checked.

- **An agent sharing the host's network namespace** (Linux, `--network host`) — here
  `127.0.0.1` really is the host's loopback, and the connection opts out of the deny
  explicitly in `policy.yaml`, next to its verbs:

  ```yaml
  connections:
    - name: my-workstation
      verbs: [inventory_sync, power_on, power_off]
      allow_loopback: true
  ```

  That exempts **that connection**, on the port its `connections.yaml` entry names, and
  nothing else. Discovery still refuses loopback however this is set.

  **`power_on` and `power_off` are not optional if you want the buttons.** With only
  `inventory_sync` the VMs list correctly on the Workstation page and Start/Stop are
  refused **by the agent** — the refusal names this file and the verb to add, but it is
  the most common "I set it all up and the buttons still do not work" outcome. Grant them
  when you write the entry, not after.

If you can make `vmrest` listen on a routable address instead, do that and skip both
exceptions — it is the only one of the three that needs nothing special from the container
runtime. Note that `vmrest` documents a `-p` port option but no bind-address option, so on a
stock install this usually is not available to you.

**Inventory plus power on and off — and nothing more.** vmrest's API is
`on/off/shutdown/suspend/pause/unpause` with **no reset, no reboot and no snapshot**, so
`restart`, `power_reset` and `snapshot` are refused with a message saying so. Mapping
`restart` onto `shutdown` would quietly do something other than what was asked.

Synced VMs appear on the **Workstation** page alongside the ones this host scans locally,
badged with the agent's name. They are tagged into workgroups the same way Proxmox and
Nutanix VMs are, and an untagged VM is admin-only — which is what stops an agent widening
what a non-admin can see.


### Large inventories

A sync returns one **page** plus an opaque cursor; the dashboard applies it and enqueues
the next. The chain is capped at 40 pages (10 000 VMs), which is what stops a
misbehaving agent making the dashboard enqueue work forever. Every page of one sync
shares a `batch_id`, so N job rows roll up as one run on `/jobs`.

The cap that forces this is `MAX_RESULT_BYTES` (256 KB), and raising it is not an
option — it is the only bound on an agent's write path into the database.
