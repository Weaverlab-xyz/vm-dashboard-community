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
