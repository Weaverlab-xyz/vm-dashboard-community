# VMware Workstation Integration

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are running VMs on VMware Workstation on this machine.

## What is it?

The VMware integration lists your VMware Workstation VMs on the **Workstation** page,
and starts and stops them from there. No vSphere or ESXi licence is required — it works
with the desktop hypervisor (Workstation Pro) you probably already have.

A **remote agent** runs on the Workstation host and talks to **`vmrest`**, the REST
daemon that ships with Workstation Pro. The agent polls the dashboard outward, so nothing
has to be opened toward that machine, and the dashboard needs no route to it.

> **This replaced an SSH + PowerShell path**, in which the dashboard container SSHed back
> to the Windows host and ran a `vm_cli_api_wrapper.ps1` wrapper around the `vmrun` CLI.
> That path required the dashboard to be running on (or adjacent to) the machine holding
> the VMs, and to hold an inbound SSH key to a Windows desktop. It has been removed. If
> you are upgrading, `VM_CLI_WRAPPER_PATH` and the `SSH_*` settings no longer do anything
> and can be deleted from your `.env`. Leave `POWERSHELL_EXECUTION_MODE` alone: it no
> longer affects this integration, but Portainer still reads it to choose between
> reaching containers directly and proxying through the Azure Automation Hybrid Worker.

---

## Use cases

- **Lab environment management** — start and stop test VMs from the same interface you
  use to spin up EC2 and Azure VMs, without switching tools.
- **On-prem + cloud unified view** — see your VMware VMs and cloud instances in one
  dashboard without logging into multiple consoles.
- **Developer workflows** — power VMs on before a demo or testing session, and back down
  after.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| VMware Workstation **Pro** | `vmrest` ships with Pro. Workstation Player does not include it |
| A remote agent on that host | See [remote agents](../remote-agents.md) |
| Network | **Outbound HTTPS from the host to the dashboard.** Nothing inbound |

There is no requirement on where the dashboard itself runs — a container, a VM, or a
managed platform are all equivalent, because the agent does the reaching.

---

## Setup

### 1. Start `vmrest` on the Workstation host

```
vmrest -C     # sets the API credentials, once
vmrest        # runs the daemon on 127.0.0.1:8697
```

Confirm it works **before** involving the agent. This separates vmrest setup from agent
configuration, which is where the time otherwise goes:

```
curl -u USER:PASS -H "Accept: application/vnd.vmware.vmw.rest-v1+json" http://127.0.0.1:8697/api/vms
```

You should get a JSON list of VM ids and paths. Note that `vmrest` **rejects
`application/json`** — the vendor media type above is required.

`vmrest -C` is not optional and its absence does not look like an authentication
problem. Without a stored credential `vmrest` **refuses to listen at all** and exits:

```
To listen on TCP port, Please use -C to update credential
Not listening to either Unix Socket or TCP port, exiting
```

The port is then closed, so the dashboard reports "could not reach vmrest" — the message
for a network problem — rather than anything about credentials.

### 1a. Keep it running: reboots, logouts, closed windows

`vmrest` is a foreground console program. It stops when its window is closed, it stops
when you log out, and **a reboot does not bring it back**. The failure that follows is
confusing rather than obvious: Docker Desktop restarts at logon, the agent container
comes up healthy and starts leasing jobs, and every Workstation job then fails on a
closed port.

**The agent cannot start it for you.** It is a Linux container inside Docker Desktop's
VM — it can dial `vmrest` on the host, but it has no way to launch a Windows process
there. Autostart belongs to the host, and
[`scripts\Enable-VmrestAutostart.ps1`](../../scripts/Enable-VmrestAutostart.ps1)
registers it:

```powershell
.\scripts\Enable-VmrestAutostart.ps1
```

Run it once on the Workstation host, from an elevated PowerShell (registering a
scheduled task needs that; the task itself runs unelevated). It finds `vmrest.exe`, then
creates one scheduled task, running as you, that:

- starts `vmrest` **at logon** with no console window — the same trigger that starts
  Docker Desktop and therefore the agent, so the two come back together;
- **re-checks every 5 minutes** and starts `vmrest` again if the process has gone, which
  covers a mid-session crash as well as a reboot.

It runs as *you*, interactively, on purpose: `vmrest -C` stores its credential under your
profile and Workstation's inventory is per-user, so a task running as `SYSTEM` would look
for a credential it cannot read and enumerate an inventory that is not yours. That also
means it is tied to your session — which costs nothing here, because Docker Desktop and
the agent are too.

| | |
|---|---|
| `-Arguments '-p','8698'` | extra `vmrest` arguments, passed verbatim. Pass `-Port 8698` too, so `-Status` probes the right port |
| `-CheckIntervalMinutes 2` | how often to check that it is alive. Default 5 |
| `-Status` | report the task, the process, the port, the credential and the launcher's log. Changes nothing |
| `-Unregister` | remove the task. A running `vmrest` is left alone |

The script **refuses to register when it finds no `vmrest` credential**, because of the
exit above: an autostart for a daemon that cannot listen is a relaunch loop every five
minutes whose only symptom is a refused port. Run `vmrest -C` first.

The task runs a small generated launcher at
`%LOCALAPPDATA%\dashboard-agent\Start-Vmrest.ps1`, which logs beside itself to
`vmrest-autostart.log`. That log is the only place a hidden `vmrest` can say why it
died, so it is worth knowing about: `-Status` prints its last few lines.

### 2. Enrol an agent on that host

Follow [remote agents](../remote-agents.md). Then add a **workstation** connection bound
to that agent, with a `policy.yaml` entry granting the verbs the page needs:

```yaml
connections:
  - name: my-workstation
    verbs: [inventory_sync, power_on, power_off]
    allow_loopback: true
```

Two things there are load-bearing:

- **`allow_loopback: true`** — vmrest binds `127.0.0.1`, and the agent denies loopback by
  default. Without it the agent refuses to dial vmrest at all.
- **`power_on` and `power_off`** — with only `inventory_sync`, the VMs list correctly and
  the Start/Stop buttons are refused **by the agent**. That refusal names the file and
  the line to add, but it is by far the most common "I set it all up and the buttons
  still do not work" outcome. Grant them up front.

### 3. Enable the integration

Turn on **VMware** in **Settings → Integrations**, and make sure **remote agents** are
enabled too. The **Workstation** link then appears in the navigation menu (the ☰ button in the top bar).

### 4. Verify

Open **Workstation** and press **Sync Now**. Within a few seconds the page should list
the VMs on that host, badged with the agent's name.

---

## What it enables in the dashboard

| Feature | Description |
|---|---|
| **Workstation page** | Every VM the agent reports, from every Workstation host you have enrolled |
| **Start / Stop** | Per-VM power, queued as an agent job with live output on the Jobs page. When the job finishes the dashboard queues an inventory sync of its own, so the page reflects the new state without a manual Sync Now |
| **Bulk Start / Force Off** | Tick several rows and power the whole selection from the toolbar — one job per VM, sharing a batch you can watch on one page. The agent runs them one at a time, VMs already in the target state are skipped (the dialog says how many), and fifty per operation is the cap. See [Powering a selection](../remote-agents/hypervisors.md#powering-a-selection) |
| **OS and Path columns** | The guest OS and the VMX file's location on the agent's host |
| **Workgroup assignment** | Admins select VMs and tag them into a workgroup, which is what makes them visible to non-admins. Tagging is admin-only; the power buttons beside it are not — they need `vms:write` and access to the VM's workgroup, the same as the per-row buttons |
| **Sync Now** | Asks the agent to re-read its host immediately rather than waiting for the scheduled pass. Rarely needed after a Start or a Stop — those re-sync themselves |
| **Inventory** | Synced VMs also appear on the Deployment Inventory page |

### What it cannot do

| | |
|---|---|
| Reset, reboot, shutdown, snapshot | **No.** vmrest's API has no reset, reboot or snapshot at all. It does offer a guest shutdown, but the agent does not map it — so the dashboard refuses out loud rather than substituting a power cut for a shutdown |
| Auto-delete | **No.** A synced VM has no teardown path, so it can never carry an expiry timer |
| Discovery | **No.** A desktop hypervisor exposes nothing on the network: vmrest binds `127.0.0.1` and is off until someone starts it, so there is nothing for a subnet sweep to find. Add the connection by hand |

---

## Troubleshooting

**Workstation link is missing** — check **Settings → Integrations → VMware**.

**The page is empty, and Sync Now says the agent is not online** — the agent has not
checked in. Look at the Agents page for its last-seen time.

**The page is empty for a non-admin, but not for an admin** — this is the intended rule,
not a fault. A VM with no workgroup is admin-only, because an agent can report any VM it
likes and none of them should widen what a non-admin sees. Select the VMs as an admin and
use **Assign workgroup**.

**A non-admin sees checkboxes but no Assign workgroup button** — intended. The selection
is there for the power buttons, which they are allowed to press; tagging a VM into a
workgroup is an admin action, so only those two buttons are hidden.

**"vmrest rejected the credential"** — set it with `vmrest -C` and check the username in
the agent's `connections.yaml` matches.

**"could not reach vmrest"** — `vmrest` is not running, or the connection's
`policy.yaml` entry cannot reach it (see [step 2](#2-enrol-an-agent-on-that-host) for
which of `host.docker.internal` and `allow_loopback: true` your agent needs).

**It worked yesterday, and after a reboot the page is empty** — this is the common one,
and it is `vmrest`, not the agent. Docker Desktop restarts at logon and the agent
container comes back healthy on its own; `vmrest` does not come back at all, so the agent
is up, leasing jobs, and failing every one of them on a closed port. Fix it once with
[step 1a](#1a-keep-it-running-reboots-logouts-closed-windows). The same applies to a
`vmrest` window that was closed, or a logout.

**`vmrest` will not start, and says nothing about credentials** — if it exits immediately
with `Not listening to either Unix Socket or TCP port`, it has no stored credential. Run
`vmrest -C`. When the autostart task is what is launching it, that exit is invisible;
`.\scripts\Enable-VmrestAutostart.ps1 -Status` prints the launcher's log, where it is
recorded.

**Start/Stop is refused, naming a verb** — the connection's `policy.yaml` entry is
missing `power_on` / `power_off`. See step 2.

**The OS column shows a dash for every VM** — the agent predates guest-OS reporting.
Rebuild and re-pull the agent on that host; everything else works without it.

**The OS column shows a raw code like `sles15-64`** — the dashboard has no label for that
code yet, so it shows what the hypervisor said rather than "Unknown". Harmless, and worth
reporting so the label table can grow.
