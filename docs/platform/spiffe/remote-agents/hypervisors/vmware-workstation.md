# VMware Workstation Pro through an agent

> **Audience:** operator · **Profile:** `both` · **Read this when:** your VMs run on VMware Workstation Pro on a machine the dashboard reaches through an agent.

Part of [Hypervisor connections through an agent](../hypervisors.md).

Workstation is the one hypervisor here that runs on somebody's desktop — nearly always a
**Windows** desktop, so read [Running on Windows](../agent-host.md#running-on-windows) alongside this. It was
twice written off as unreachable because the dashboard drives it with `vmrun` against local
VMX paths. That was true of `vmrun` and wrong overall: Workstation **Pro** ships `vmrest`, a
REST daemon that is plain JSON over HTTP. An agent on that host reaches it with no extra
dependency and no container.

On the Workstation host:

```
vmrest -C     # set the API credentials, once
vmrest        # run the daemon — 127.0.0.1:8697
```

**Neither of those lines survives a reboot, and the first one is a prerequisite rather
than a nicety** — with no stored credential `vmrest` refuses to listen and exits
("Not listening to either Unix Socket or TCP port"), so the symptom is a closed port and
not a 401. And because `vmrest` is a foreground console program, it also stops when its
window is closed or when you log out, while the agent container restarts at logon
perfectly happily — an agent that is online and failing every Workstation job on a
refused connection is what that looks like from the dashboard. The agent cannot start it:
a Linux container in Docker Desktop's VM can dial the host but cannot launch a process on
it. Register the host-side autostart once, with
[`scripts\Enable-VmrestAutostart.ps1`](../../../../../scripts/Enable-VmrestAutostart.ps1) — see
[Keep it running](../../../../hypervisors/vmware.md#1a-keep-it-running-reboots-logouts-closed-windows).

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
badged with the agent's name. They are tagged into workstreams the same way Proxmox and
Nutanix VMs are, and an untagged VM is admin-only — which is what stops an agent widening
what a non-admin can see.
