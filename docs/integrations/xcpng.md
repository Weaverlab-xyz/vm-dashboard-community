# XCP-ng / XenServer Integration

The dashboard connects to an **XCP-ng** or **XenServer** host or pool master via the
**XAPI XML-RPC API** using Python's built-in `xmlrpc.client` — no external SDK required.

---

## Prerequisites

| Requirement | Details |
|---|---|
| XCP-ng / XenServer | XCP-ng 8.x or XenServer 8 (earlier versions likely work) |
| User | `root` or a user with **Pool Admin** role |
| Network | Dashboard container must reach the pool master on port **443** (HTTPS) |
| xe-guest-utilities | Required for graceful shutdown/reboot (optional) |

---

## Connection Notes

- Always use the **pool master** address. XAPI sessions initiated against a secondary host
  will redirect internally, but using the master address avoids the extra hop.
- XAPI uses HTTPS on port 443 by default. The certificate is self-signed on stock installs —
  disable **Verify SSL** in Settings unless you have replaced it with a trusted certificate.

---

## Power Operations

| Dashboard Button | XAPI call | Guest tools required? |
|---|---|---|
| Start | `VM.start` | No |
| Shutdown (graceful) | `VM.clean_shutdown` | Yes (xe-guest-utilities) |
| Power Off (force) | `VM.hard_shutdown` | No |
| Reboot (graceful) | `VM.clean_reboot` | Yes (xe-guest-utilities) |
| Hard Reboot | `VM.hard_reboot` | No |
| Suspend | `VM.suspend` | No |
| Resume | `VM.resume` | No |
| Pause | `VM.pause` | No |
| Unpause | `VM.unpause` | No |

---

## Installing xe-guest-utilities

xe-guest-utilities (the XCP-ng equivalent of VMware Tools) enables ACPI-based graceful
shutdown and reboot, and provides in-guest IP address reporting.

### Debian / Ubuntu

```bash
sudo apt-get install -y xe-guest-utilities
sudo systemctl enable xe-linux-distribution
sudo systemctl start  xe-linux-distribution
```

### RHEL / Rocky / AlmaLinux / CentOS

```bash
sudo dnf install -y xe-guest-utilities
sudo systemctl enable xe-linux-distribution
sudo systemctl start  xe-linux-distribution
```

### SUSE / openSUSE Leap

```bash
sudo zypper install -y xe-guest-utilities
sudo systemctl enable xe-linux-distribution
sudo systemctl start  xe-linux-distribution
```

### Windows

Install the **XCP-ng Windows Guest Tools** from the XCP-ng project:  
`https://github.com/xcp-ng/win-pv-drivers/releases`

---

## Host Filter

The VM list page includes a **Host** filter dropdown. It is auto-populated from the host
reference returned by the XAPI — one entry per host in the pool. No extra configuration needed.

---

## VM Visibility

The integration filters out:

- Templates (`is_a_template = true`)
- The control domain / dom0 (`is_control_domain = true`)
- Snapshot VMs (`is_a_snapshot = true`)

Only real, runnable VMs are shown.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| 502 on VM list | Dashboard can't reach XAPI on port 443 | Check firewall / `xe host-list` reachable? |
| `RBAC_PERMISSION_DENIED` | User lacks Pool Admin role | Grant Pool Admin to the API user |
| `SESSION_AUTHENTICATION_FAILED` | Wrong credentials | Verify username/password in Settings |
| Graceful shutdown fails | xe-guest-utilities not installed | Install xe-guest-utilities inside VM |
| No IPs shown for VMs | Guest utilities not reporting | Install or start xe-linux-distribution service |
| SSL certificate error | Self-signed cert | Disable **Verify SSL** in Settings |
| Connection timeout | Pool master is secondary / floating IP | Use the dedicated pool master address |

### Checking connectivity from the dashboard container

```bash
docker exec -it vm-dashboard python3 -c "
import xmlrpc.client, ssl
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
s = xmlrpc.client.ServerProxy('https://<xcpng-host>', context=ctx)
r = s.session.login_with_password('root', '<password>', '2.0', 'dashboard-test')
print(r)
"
```

A successful response begins with `{'Status': 'Success', 'Value': 'OpaqueRef:...'}`.

## Multiple connections

Connection details used to live in Settings as a single set of fields, so there could
only ever be one XCP-ng pool. They now live in the **Connections** page (`/connections`),
which holds as many as you like — a second XCP-ng pool at another site, or the same one
under a read-only and a privileged service account.

* The **default** connection is what every page and API call uses when not told
  otherwise. The first connection of a kind becomes the default automatically.
* Pass `?connection_id=<id>` to any `/api/xcpng` endpoint to target a specific one.
* Job-backed operations (deploys, power verbs) record the connection at **enqueue**, so
  changing the default while one is queued cannot redirect it.

Your existing Settings values were copied into the first connection on upgrade. The old
panel is still there, read-only, with a banner pointing here — editing it no longer
changes what the dashboard connects to. It is kept so that rolling back to a previous
image still works.

## Over a remote agent

A XCP-ng pool the dashboard has no network route to can be reached through a
[remote agent](../remote-agents.md#hypervisor-connections) instead. Tick *Reached
through a remote agent* when adding the connection and give it the name that connection
has in the agent's own `connections.yaml`.

The dashboard then stores **no host and no credential** for it — only the name. The
agent uses XAPI over XML-RPC, from the standard library, which needs no dependency the agent does not already have.

Three separate grants must line up: the dashboard grants the agent the
`agent_hypervisor` job type, your `policy.yaml` grants the individual verbs on that
connection, and your `connections.yaml` defines it. Withhold any one and nothing runs.

### When the connection is reached through an agent

The dashboard has no route to an agent-bound connection — that is the point of binding it
to an agent — so this page cannot query it live. It shows the **last synced inventory**
instead, with a banner saying so and how old it is. Live-only figures (CPU usage, uptime,
disk) are blank there rather than zero: they were never measured, and a fabricated 0 is
worse than an empty cell.

Power actions still work: they are dispatched to the agent as jobs and appear on `/jobs`
with Live Output, exactly like a discovery scan. Start, Force Off, Shutdown, Reboot and
Hard Reboot all map to a verb; **Suspend, Resume, Pause and Unpause** do not, and are
refused with a 501 naming what is available rather than approximated onto a neighbouring
operation — see [the verbs](../remote-agents.md#the-verbs).

Shutdown and Reboot are the two that need the guest utilities. The synced inventory
carries no `tools_installed`, so those buttons are offered rather than hidden — and no
"⚠ No guest tools" badge is shown either, since the sync never measured it — and XAPI is
what answers if `xe-guest-utilities` is missing.
