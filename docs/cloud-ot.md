# OT Demo Cell

The dashboard can stand up a simulated **OT/ICS plant cell** — a Modbus TCP PLC
simulator plus the FUXA web SCADA/HMI — inside the GCP sandbox's **egress-less private
subnet**, then layer the BeyondTrust PAM stack on top. Same **provisioning + three
layers** model as [Cloud VMs](cloud-vms.md); the OT twist is that the air-gapped subnet
*is* the plant network, and every path in is PRA-brokered:

- **Provisioning** *(stand it up)* — deploy a VM from the Packer-baked **`ot-sim`**
  image (`provisioners/ot/ot-sim-debian.sh`). Everything is baked at build time, so the
  running cell needs **zero outbound internet**: a PLC simulator whose holding registers
  tick every second (:502), FUXA (:1881), Docker, and a systemd unit that starts the
  stack at boot.
- **Layer 1 — PRA** *(reach it)* — three jump items per cell, all auto-provisioned:
  - **Web Jump** → `http://<vm>:1881` (the HMI, rendered and recorded on the gateway);
  - **Protocol Tunnel** (generic TCP) → the PLC port, with presets for
    **Modbus :502, OPC UA :4840, DNP3 :20000, Siemens S7 :102, EtherNet/IP :44818**;
  - **Shell Jump** → SSH, inherited from the normal GCE deploy path.
- **Layer 2 — Password Safe** *(manage its secrets)* — *optional, default on.* The
  image's `adminuser` is onboarded via the cloud-native **`gcpvm`** plugin (managed
  system address `projectId/zone/instanceName`), inherited from the GCE deploy path.
- **Layer 3 — Entitle** *(grant time-boxed access)* — *optional.* SSH ephemeral
  accounts, inherited from the GCE deploy path.

GCP-only in this slice. The whole feature is gated on **`pra_enabled`** (the router and
the GCP page's *OT Demo Cell* tab both follow it) — a cell without PRA would be a VM
nobody can reach, by design.

---

## Why this demos well for OT customers

Secure third-party/vendor access into plant networks is the flagship OT PAM use case:
no VPN, no inbound firewall holes, recorded sessions, credentials injected rather than
shared. The cell makes that concrete — the "plant" has **no public IP and no egress**,
yet a rep reaches the HMI in a recorded browser session, reads live Modbus registers
through a tunnel, and never learns a credential. The register values *change* every
second (counter, temperature, flow), so a Modbus client through the tunnel visibly
shows live process data, not a static mock.

## Deploying a cell

1. **Bake the image once**: Storage page → upload `provisioners/ot/ot-sim-debian.sh`;
   GCP page → *Build Image* → name `ot-sim`, source family `debian-12`, load the script
   from storage, build (~10–15 min). See `provisioners/ot/README.md` for the image
   contract and pins.
2. **GCP page → OT Demo Cell tab**: pick the image (the picker pre-filters names
   containing `ot-sim`), name the cell, pick the protocol preset, deploy. The cell VM
   never gets an external IP, always carries the **`ot-sim`** network tag (the forward
   hook for Purdue-zone firewalling), and defaults to `e2-small`.
3. The job page shows the parent `ot_cell_deploy` job driving one `gce_deploy` child —
   the child **is** the cell's inventory record.

### The gateway sizing guard

A Web Jump renders **headless Chromium on the PRA gateway host**. Below ~2 GB the
renderer is OOM-killed, and the session error is indistinguishable from a blocked
firewall. Fresh installs default to `e2-medium`, but the cloud-sandbox setup scripts
seed `gcp_jumpoint_machine_type=e2-micro` (1 GB) to keep standing cost down, so the
cell deploy checks the **live** gateway VM (falling back to the configured
`gcp_jumpoint_machine_type`) and refuses early — before launching anything — with the
remedy in the job error: set `gcp_jumpoint_machine_type` to `e2-small` minimum /
`e2-medium` preferred under **Settings → Integrations → Privileged Remote Access**
(GCP overrides), delete the existing gateway VM so it recreates at the new size, retry.

### Partial failures and re-wiring

Wiring failures (Web Jump or tunnel) fail the parent job with the remedy in its error
message, but the VM and any completed wiring stay. Every artifact is written to the
child job's metadata the moment it exists, so the **Re-wire** button
(`POST /api/ot/cell/{vm_job_id}/rewire`) retries only the missing pieces, and a destroy
cleans exactly what exists.

## Lifecycle

- **Destroy** (OT tab button → the normal `DELETE /api/gcp/instances/{name}`) and the
  **auto-delete timer** both run the same extended `gce_destroy`: remove the Web Jump
  and tunnel from their stored Terraform state, then the Shell Jump, Password Safe and
  Entitle deregistrations, then the instance — and release the shared gateway reference
  last. There is no separate OT teardown path to forget.
- **Expiry**: the child is a normal `gce_deploy` row, so the cell participates in the
  auto-delete timer with no extra configuration (see [auto-delete-timer](auto-delete-timer.md)).
- **Air-gap**: keep **`gcp_vm_nat_enabled` off**. Turning it on gives cell subnets
  egress and silently deflates the "no path out of the plant" story.

## Standalone OT protocol tunnels

The OT tab also creates a **standalone tunnel** to *any* host the gateway can reach —
for demoing against real lab gear without deploying a cell. It is the same generic-TCP
protocol tunnel the k8s API tunnel uses, with the OT port presets. State lives in
config keys (`ot_tunnel_*`), and live tunnels hold a reference in the shared GCP
gateway's idle-teardown count, so an unrelated decommission cannot reap the gateway
mid-session. Connection: start the tunnel session in the PRA representative console,
point the Modbus/OPC-UA client at `127.0.0.1:<local port>`.

## First-time FUXA wiring

The FUXA project is not pre-baked (its project format is version-coupled). Once per
cell (~1 minute, inside the recorded Web Jump session): FUXA → Connections → add a
**ModbusTCP** device at address `plc` port `502`, add tags for holding registers 0–3,
drop them on a view. The project persists on the VM.

## E2E verification checklist

1. Settings: `pra_enabled` on; `bt_api_host` / `bt_client_id` / `bt_client_secret` /
   `bt_jump_group_name` / `bt_jumpoint_name` set; `gcp_jumpoint_machine_type=e2-medium`
   (delete an existing gateway VM so it recreates); `gcp_vm_nat_enabled` **off**;
   Password Safe registration on with the GCP functional account.
2. Bake `ot-sim`; it appears in the OT tab's image picker.
3. Deploy a cell; the parent job completes; the child holds Shell Jump id + private IP.
4. PRA rep console: Shell Jump SSH works; Web Jump renders FUXA (recorded); a Modbus
   client (mbpoll / QModMaster) through the tunnel at `127.0.0.1:502` reads holding
   register 0 **incrementing every second**.
5. Password Safe: managed system `projectId/zone/instanceName` exists; rotate `adminuser`.
6. Negative test: set the gateway to `e2-micro` → a new cell fails fast with the sizing
   remedy in the job error (not a mid-session OOM).
7. Destroy the cell → jump items gone from PRA, PS system off-boarded, VM deleted,
   gateway reaped only once nothing else references it.
8. Expiry: with the timer enabled, `expires_at` is stamped on the child row; a reaped
   cell cleans up identically to a destroyed one.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Cell deploy fails immediately with a sizing message | Working as designed — the gateway is <2 GB; follow the remedy in the error |
| Web Jump session dies with "internal timeout starting session" | Gateway too small (if the guard was bypassed by resizing after deploy), or the gateway host is down — check the Gateways tab against reality |
| Tunnel connects but the Modbus client times out | The cell VM isn't running the stack — Shell Jump in and check `systemctl status ot-sim` / `docker ps` |
| Registers read but never change | The PLC sim container restarted into a crash loop — `docker logs ot-plc` |
| `ot-sim` bake fails at image pull | Docker Hub rate limit or a stale pin — see `provisioners/ot/README.md` (re-pin via `OT_FUXA_IMAGE`) |
| Parent job failed after "VM deployed" | Wiring failure — the error names the failed piece; fix and **Re-wire** |

## Quick preview without a bake

`examples/compose/ot-sim.yml` runs FUXA + a *static* Modbus server through the
Containers page (ECS/ACI/GCE-COS). It is **unwired** — no PRA, no Password Safe, needs
egress at start, and register values don't change. Use it for a quick look at the
containers; use the cell for the actual demo.
