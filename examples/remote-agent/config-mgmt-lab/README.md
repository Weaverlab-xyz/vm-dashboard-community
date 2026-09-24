# Config-Management-only delegate agents

> **Audience:** operator · **Read this when:** you have servers on network segments the
> dashboard cannot route to, and you want to run playbooks against them and nothing else.

This kit deploys **delegate agents** — agents whose entire job is to execute Config
Management on one network segment. They discover nothing, broker no hypervisor, serve no
file share and run no Gateway.

Worked below for two segments behind separate firewalls, because two is where the
interesting part shows up. The pattern is per-segment, so a third is a third copy of the
same four steps.

The reference documentation for the feature is
[Agent-executed Config Management](../../../docs/remote-agents/config-runs.md) — read that
for what the dashboard sends and what a run looks like. This kit is the concrete build of
it.

## The shape, and why it is three agents rather than two

The agent that can **read** your hypervisor inventory often cannot **reach** the guests in
it. The sharpest case is VMware Workstation: `vmrest` binds `127.0.0.1` with no
bind-address option, so the brokering agent has to run on the Windows host under Docker
Desktop — whose Linux VM enumerates the vmnet adapters and leaves every one of them down.
No amount of configuration fixes that; it is a property of mirrored networking. The same
split appears wherever a management API and its guests sit on different networks.

So the roles split:

```
  hypervisor host                        segment A                    segment B
  ┌─────────────────────────┐            ┌──────────────────┐        ┌──────────────────┐
  │ brokering agent         │            │ delegate agent   │        │ delegate agent   │
  │ · reads the mgmt API    │            │ · runs playbooks │        │ · runs playbooks │
  │ · reports guest IPs     │            └────────┬─────────┘        └────────┬─────────┘
  └───────────┬─────────────┘                     │                           │
              │   inventory                       │  ssh/22                   │  ssh/22
              ▼                                   ▼                           ▼
       ┌─────────────┐                      segment A hosts             segment B hosts
       │  dashboard  │  ◄── all three poll OUT over 443; no inbound rule anywhere
       └─────────────┘
```

The brokering agent keeps supplying inventory and is **not** changed by this kit. The
delegates supply reach. What binds them together is a **Config-Management route**: a
CIDR → agent mapping the dashboard consults to decide who executes a run.

A route decides *who executes*, never *what address is targeted*. The address stays
pinned to one the discovering agent itself reported. That ordering is the whole security
argument — a route is an input to the agent decision and can never become an input to the
address decision.

## Before you start

Per segment, a small Linux VM **inside** it, with:

- **Docker**, rootless preferred. A Config-Management run does not execute inside the
  agent — the agent launches a one-shot sibling container from the image named in
  `policy.yaml`. No Engine, no runs.
- **Outbound 443** to the dashboard's agent hostname. Nothing inbound, ever.
- **A route to that segment's hosts on 22.** The agent VM is on-segment, so this is
  normally free; if the firewall segments *within* the segment, check it.

And know each segment's CIDR — specifically, **the network of the addresses the
hypervisor sync actually reports** for its guests, which is not always the segment the
lab is named after. Mixing up two segments' ranges is the most likely mistake here, and
step 4 is where it shows up.

## 1 · On each agent VM

```bash
cp policy.example.yaml policy.yaml
cp .env.example .env
```

Edit `policy.yaml` and set both CIDRs to **this** segment's range — the inert `targets:`
one and the load-bearing `ansible.targets` one. They are separate lists by design, with
no fallback between them.

Then edit `.env`: the dashboard URL, the enrolment code from step 2, and
`DOCKER_SOCKET_GID` (`stat -c %g $XDG_RUNTIME_DIR/docker.sock`).

Pull the runner image yourself. **The agent will not pull it**, and a missing image fails
every run with a message that reads like a policy problem:

```bash
docker pull chrweav/ansible-winrm:latest
```

That image serves SSH *and* WinRM despite the name; this policy grants port 22 only.

## 2 · Register each agent

Agents page → **Register agent**. Copy the enrolment code into `.env`, then:

```bash
docker compose up -d && docker compose logs -f agent
```

The code is single-use and expires in 15 minutes. On first start the agent generates an
Ed25519 keypair, redeems the code, and stores its identity in the named volume — so the
code is needed once. Blank it from `.env` afterwards.

Wait for the agent to show **online**, and check the reported version is **2.3 or
higher**. Below that the `agent_ansible` handler does not exist and the dashboard refuses
the run at enqueue. An agent reporting an older version is a host that has not pulled.

## 3 · Narrow each agent to Config Management

**This step has no UI. It is API-only, and skipping it leaves the agent granted every job
type the dashboard supports.**

An empty `allowed_job_types` means *the default set* — all of them — not *nothing*. A
freshly enrolled agent is therefore unrestricted on the dashboard side until you narrow
it. Your `policy.yaml` already refuses everything but `agent_ansible`, so this is defence
in depth rather than the only thing standing there — but it is the half you can audit
from the dashboard, and the half that survives someone else editing the policy file.

Against the **internal/UI** hostname, not the agent one:

```bash
curl -X PATCH https://dashboard.internal/api/agent/<agent_id> \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"allowed_job_types": ["agent_ansible"], "site": "segment-a"}'
```

Repeat per agent. Note "grant nothing" is deliberately not expressible — to stop an
agent, revoke it.

## 4 · Route each segment to its agent

**Remote Agents → Config Routes → Add route**, once per segment:

| Range | Executes on | Label |
|---|---|---|
| `10.20.10.0/24` | the segment A delegate | `segment A` |
| `10.20.30.0/24` | the segment B delegate | `segment B` |

The route form renders the exact `ansible.targets` block that agent's `policy.yaml`
needs. Compare it against what you wrote in step 1 rather than pasting blindly — if they
disagree, the form is describing the range you actually typed.

Matching is longest-prefix-first, like a routing table, and the unique constraint on
`cidr` means two agents can never claim one range — that is the one ambiguity prefix
length cannot settle, so it is refused at write time.

**The `matches` count beside each route is the verification**, and the reason a route
needs no Test button: there is nothing to dial, so coverage is the only useful feedback.
A correct CIDR binds to a non-zero number of synced VMs. **Zero means the range is wrong**
— or the guests have not reported addresses yet, which needs them powered on, guest tools
installed, and `sync_guest_details: true` on that connection in the brokering agent's
`connections.yaml`.

Confirm the decision directly, with a host address you know the segment of:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  'https://dashboard.internal/api/connections/config-mgmt-routes/resolve?address=10.20.10.50'
```

If it names the *other* segment's delegate, the two ranges are the wrong way round —
which is a five-second fix now and a confusing afternoon later.

`source: "route"` means a range decided. `source: "none"` means nothing matched and the
run would fall back to the brokering agent — the host that cannot reach the segment, i.e.
the problem this kit exists to fix, silently.

## 5 · Run one

Config Management → pick a host under **On-Prem VMs (via agent)**. The picker names the
agent that will execute; the enqueue gate then accepts exactly that one, resolved through
the same rule, so the two cannot disagree.

Start with `AGENT_MODE=audit` in `.env` if you want to watch routing land before anything
is reconfigured — the agent leases the job and logs precisely what it *would* run.

## Troubleshooting

| What you see | What it is |
|---|---|
| Agent exits non-zero at start, `bad cidr` | A target list is not a network. Check for a missing space after the dash — `-cidr:` is a key name, not a list entry. |
| Agent crash-loops on `Permission denied` reading its policy | SELinux. The `,Z` suffix is on the mount in `docker-compose.yml`; if you rewrote it, put it back. |
| Every run fails seconds in, Engine errors | `DOCKER_SOCKET_GID` unset or wrong. uid 10001 vs a mode-0660 socket = `EACCES` on a socket that is plainly mounted. |
| Run refused at enqueue, "not granted the Config-Management job type" | Step 3 narrowed the agent to something that excludes `agent_ansible`. |
| Run refused at enqueue naming a *different* agent | The route is missing or its CIDR does not cover the host's address. Use the `resolve` call above. |
| `policy.yaml does not allow Config Management against …`, from an agent new to you | The routed agent's own `ansible.targets` does not cover the range. This is the step people miss — the route and the policy are two separate grants. |
| `UNREACHABLE … Operation timed out` | The runner container has no path to the host. Check `ansible.network` is not `none`, and that the agent VM itself can `ssh` the target. |
| Host not selectable at all | It reports no address. Powered on, guest tools installed, `sync_guest_details: true` — all three. |

## What this kit deliberately does not do

- **No database targets.** `db_image` is unset, so a database run is refused by name
  rather than half-working.
- **No WinRM.** Port 22 only. Granting 5985/5986 to an agent that will never use them is
  a wider grant for no benefit.
- **No `limits:` block.** Its only two keys bound a *discovery scan*, and these agents do
  not discover. The ceiling on a run is `ansible.max_runtime_minutes`.
