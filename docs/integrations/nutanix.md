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
