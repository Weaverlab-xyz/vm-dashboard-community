# Nutanix AHV Integration

The dashboard connects to **Prism Central** (or Prism Element) via the Nutanix REST API v3
to list and control AHV virtual machines.

---

## Prerequisites

| Requirement | Details |
|---|---|
| Nutanix cluster | AOS 5.20+ with Prism Central or Prism Element |
| API user | Local or AD user with **Prism Admin** or **Cluster Admin** role |
| Network | Dashboard container must reach Prism on port **9440** (HTTPS) |
| Nutanix Guest Tools | Required for graceful ACPI shutdown/reboot (optional) |

---

## API User Setup

### Option A — Use the built-in admin account

The default `admin` account has full access. Suitable for lab environments.

### Option B — Create a dedicated read/write user (recommended)

1. In Prism Central, go to **Settings → Local User Management → + New User**
2. Set a username (e.g. `dashboard-svc`) and a strong password
3. Assign the **Prism Admin** or **Cluster Admin** role
4. Save

---

## Power Operations

| Dashboard Button | API Transition | Guest Tools Required? |
|---|---|---|
| Start | `ON` | No |
| Shutdown (graceful) | `ACPI_SHUTDOWN` | Yes (NGT) |
| Power Off (force) | `OFF` | No |
| Reboot (graceful) | `ACPI_REBOOT` | Yes (NGT) |
| Reset (hard) | `RESET` | No |
| Pause | `PAUSE` | No |
| Resume | `RESUME` | No |

**Nutanix Guest Tools (NGT)** is the equivalent of VMware Tools. Install it inside the VM to
enable ACPI-based graceful shutdown and reboot. Without NGT the graceful buttons are shown but
will return an error from Prism if the VM does not respond to the ACPI signal.

---

## Installing Nutanix Guest Tools

1. In Prism, select the VM → **Manage Guest Tools → Enable NGT → Mount**
2. Inside the VM:
   - **Windows**: run the NGT installer from the mounted CD
   - **Linux**: `mount /dev/cdrom /mnt && sudo /mnt/installer/linux/install_ngt.py`
3. Verify: VM details in Prism should show **Guest Tools** as Enabled

---

## Cluster Filter

The VM list page includes a **Cluster** filter dropdown. It is auto-populated from the
`cluster_reference.name` field returned by the API — no extra configuration needed.

---

## SSL / Self-Signed Certificates

Prism ships with a self-signed certificate. Either:

- **Disable verify SSL** in the Settings panel (suitable for lab / on-premises use)
- **Upload a trusted CA bundle** to the container and set the CA bundle path in
  **Settings → Integrations → Nutanix → CA Bundle Path**, then re-enable SSL verification

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| 502 on VM list | Dashboard can't reach Prism on port 9440 | Check firewall / routing |
| 401 Unauthorized | Wrong credentials | Verify username/password in Settings |
| 403 Forbidden | User lacks required role | Assign Prism Admin or Cluster Admin |
| Graceful shutdown times out | NGT not installed / not running | Install NGT inside the VM |
| VM shows `ACPI_SHUTDOWN` error | VM is not responsive to ACPI signal | Use **Power Off** (force) instead |
| SSL certificate error | Self-signed cert and verify SSL is on | Disable verify SSL in Settings |

### Checking connectivity from the dashboard container

```bash
docker exec -it vm-dashboard curl -sk https://<prism-host>:9440/api/nutanix/v3/users/me \
  -u admin:<password> | python3 -m json.tool
```

A successful response contains `"kind": "user"` in the result.

## Multiple connections

Connection details used to live in Settings as a single set of fields, so there could
only ever be one Prism Central. They now live in the **Connections** page (`/connections`),
which holds as many as you like — a second Prism Central at another site, or the same one
under a read-only and a privileged service account.

* The **default** connection is what every page and API call uses when not told
  otherwise. The first connection of a kind becomes the default automatically.
* Pass `?connection_id=<id>` to any `/api/nutanix` endpoint to target a specific one.
* Job-backed operations (deploys, power verbs) record the connection at **enqueue**, so
  changing the default while one is queued cannot redirect it.

Your existing Settings values were copied into the first connection on upgrade. The old
panel is still there, read-only, with a banner pointing here — editing it no longer
changes what the dashboard connects to. It is kept so that rolling back to a previous
image still works.

## Over a remote agent

A Prism Central the dashboard has no network route to can be reached through a
[remote agent](../remote-agents.md#hypervisor-connections) instead. Tick *Reached
through a remote agent* when adding the connection and give it the name that connection
has in the agent's own `connections.yaml`.

The dashboard then stores **no host and no credential** for it — only the name. The
agent uses the Prism v3 REST API, which needs no dependency the agent does not already have.

Three separate grants must line up: the dashboard grants the agent the
`agent_hypervisor` job type, your `policy.yaml` grants the individual verbs on that
connection, and your `connections.yaml` defines it. Withhold any one and nothing runs.

Inventory sync only. Power verbs are refused by the agent: a v3 power
change is a full spec PUT carrying a metadata version rather than a simple
action, and getting that wrong writes to the VM instead of failing.

### When the connection is reached through an agent

The dashboard has no route to an agent-bound connection — that is the point of binding it
to an agent — so this page cannot query it live. It shows the **last synced inventory**
instead, with a banner saying so and how old it is. Live-only figures (CPU usage, uptime,
disk) are blank there rather than zero: they were never measured, and a fabricated 0 is
worse than an empty cell.

Power actions still work: they are dispatched to the agent as jobs and appear on `/jobs`
with Live Output, exactly like a discovery scan.
