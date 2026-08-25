# OT Demo Cell

The dashboard can stand up a simulated **OT/ICS plant cell** — a Modbus TCP PLC
simulator plus the FUXA web SCADA/HMI — inside a cloud sandbox's **private,
egress-less subnet**, then layer the BeyondTrust PAM stack on top. Same
**provisioning + three layers** model as [Cloud VMs](cloud-vms.md); the OT twist is
that the air-gapped subnet *is* the plant network, and every path in is PRA-brokered:

- **Provisioning** *(stand it up)* — deploy a VM from the Packer-baked **`ot-sim`**
  image (`provisioners/ot/ot-sim-debian.sh`). Everything is baked at build time, so the
  running cell needs **zero outbound internet**: a PLC simulator whose holding registers
  tick every second (:502), FUXA (:1881), Docker, and a systemd unit that starts the
  stack at boot.
- **Layer 1 — PRA** *(reach it)* — three jump items per cell, all auto-provisioned:
  - **Web Jump** → `http://<vm>:1881` (the HMI, rendered and recorded on the gateway);
  - **Protocol Tunnel** (generic TCP) → the PLC port, with presets for
    **Modbus :502, OPC UA :4840, DNP3 :20000, Siemens S7 :102, EtherNet/IP :44818**;
  - **Shell Jump** → SSH, inherited from the cloud's normal VM deploy path.
- **Layer 2 — Password Safe** *(manage its secrets)* — *optional, default on.* The
  image's `adminuser` is onboarded via the cloud-native plugin the cloud's VM deploy
  path already uses — **`gcpvm`** on GCP (managed system address
  `projectId/zone/instanceName`), **`ssm`** on AWS (managed system DNS
  `{instance-id}:{region}`, over Systems Manager), **`azurevm`** on Azure (address
  `tenantId/subscriptionId/resourceGroup/vmName`, over Run Command). On top of that,
  the wiring makes the credential **usable in PRA**: a PRA Vault username/password
  account plus a Password Safe mirror on the "PRA Vault Username Password" plugin,
  linked with SyncedAccounts — see
  [PRA checkout of the cell's admin credential](#pra-checkout-of-the-cells-admin-credential).
- **Layer 3 — Entitle** *(grant time-boxed access)* — *optional.* SSH ephemeral
  accounts, inherited from the VM deploy path.

Runs on **GCP, AWS and Azure** — each cloud page has its own *OT Demo Cell* tab, and
the cell's VM is that cloud's plain deploy child (`gce_deploy` / `ec2_deploy` /
`azure_deploy`), so admission policy, expiry, Shell Jump and Password Safe behave
exactly as on a normal VM there. The whole feature is gated on **`pra_enabled`** (the
router and the tabs all follow it) — a cell without PRA would be a VM nobody can
reach, by design.

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

1. **Bake the image once per cloud**: Storage page → upload
   `provisioners/ot/ot-sim-debian.sh`; then that cloud's *Build Image* tab → name
   `ot-sim`, pick the Debian 12 source, load the script from storage, build
   (~10–15 min). See `provisioners/ot/README.md` for the image contract and pins.
   - **GCP**: source family `debian-12` (Compute Engine resolves the family itself).
   - **AWS**: the *Debian 12* preset — it names an OS **family** the build resolves
     to the newest public AMI at launch (and sets the image's `admin` login user);
     a literal AMI ID pasted into the field still wins.
   - **Azure**: the *Debian 12* preset (marketplace `Debian/debian-12/12-gen2`).
     Linux builds always publish a **Compute Gallery image version** — Azure's
     managed-image export path is broken, gallery-version is the supported route.
2. **The cloud page → OT Demo Cell tab**: pick the image (the picker pre-filters names
   containing `ot-sim`), name the cell, pick the protocol preset, pick the **PRA Jump
   Group + Gateway** that match the cell's region (see below), deploy. The VM defaults
   to the 4 GB shape everywhere (`e2-medium` / `t3.medium` / `Standard_B2s`) — a 2 GB
   cell proved too tight for Docker + the PLC sim + FUXA in live use. On GCP and Azure
   the cell never gets a public IP (the form pins it); the GCP cell also carries the
   **`ot-sim`** network tag (the forward hook for Purdue-zone firewalling). **On AWS
   there is no per-instance public-IP switch — the subnet decides — so keep the form's
   default private sandbox subnet**, or the air-gap story quietly deflates.
3. The job page shows the parent `ot_cell_deploy` job driving one deploy child
   (`gce_deploy` / `ec2_deploy` / `azure_deploy`) — the child **is** the cell's
   inventory record.

### Choosing the Jump Group and Gateway

PRA objects are not region-scoped, so nothing stops a us-east1 cell from wiring into a
Jump Group named `centralus` through a us-central1 Gateway — which is exactly what the
configured defaults will do if they were set up for another region. Each cloud's cell
resolves the same fallback chain its own Shell Jump uses: GCP
`gcp_bt_jump_group_name` / `gcp_jumpoint_name`, Azure `azure_bt_jump_group_name` /
`azure_jumpoint_name`, AWS straight to the shared `bt_jump_group_name` /
`bt_jumpoint_name` — all falling back to the shared pair. The deploy form's
**BeyondTrust PRA placement** pickers (fed by `GET /api/pra/pickers`, same as the VM
deploy modal) override the defaults per cell: all three jump items land in the chosen
Jump Group and ride the chosen Gateway, and the PRA Vault checkout account is
associated to the same Jump Group. The standalone tunnel form has the same two
pickers. Left at "(configured default)", behaviour is unchanged.

### The gateway sizing guard

A Web Jump renders **headless Chromium on the PRA gateway host**. Below ~2 GB the
renderer is OOM-killed, and the session error is indistinguishable from a blocked
firewall. The cell deploy therefore checks the **live** managed gateway host (falling
back to the configured size key) and refuses early — before launching anything — with
the remedy in the job error. Per cloud, the key and the sizes the remedy names (all
under **Settings → Integrations → Privileged Remote Access**):

| Cloud | Size key | Minimum | Preferred | Fresh-install default |
|---|---|---|---|---|
| GCP | `gcp_jumpoint_machine_type` | `e2-small` | `e2-medium` | `e2-medium` — but the sandbox setup scripts seed `e2-micro` (1 GB) to keep standing cost down, so on a sandbox install the refusal is the out-of-the-box experience |
| AWS | `bt_ecs_host_instance_type` | `t3.small` | `t3.medium` | `t3.small` (2 GB — exactly the minimum) |
| Azure | `azure_jumpoint_vm_size` | `Standard_B1ms` | `Standard_B2s` | `Standard_B2s` (4 GB) |

Changing a key never resizes a live gateway: delete the existing gateway host so the
next deploy recreates it at the new size, then retry the cell.

The guard reasons about the **dashboard-managed shared gateway only**. When the form's
Gateway picker overrides it with another Gateway, the Web Jump renders on a host
this install cannot size, so the guard steps aside (noted in the job progress) — the
≥2 GB requirement then rests on whoever runs that Gateway.

### Partial failures and re-wiring

Wiring failures (Web Jump, tunnel or the PRA-checkout pair) fail the parent job with
the remedy in its error message, but the VM and any completed wiring stay. Every
artifact is written to the child job's metadata the moment it exists, so the
**Re-wire** button (`POST /api/ot/cell/{vm_job_id}/rewire`) retries only the missing
pieces, and a destroy cleans exactly what exists. A cell deployed **before** the
PRA-checkout feature shows *wiring incomplete* once (its Password Safe onboarding
exists but the checkout pair doesn't) — Re-wire retrofits exactly the missing pieces.

## PRA checkout of the cell's admin credential

Password Safe onboarding alone puts `adminuser` on the **GCP VM SSH Rotation**
platform — rotatable, but invisible to PRA. To make it **checkout-able and injectable
in PRA**, the wiring adds three linked artifacts (skipped when the Password Safe
checkbox is off, or via `ot_ps_pra_checkout_enabled=false`):

1. a **PRA Vault username/password account** `<cell>-adminuser`, associated to the
   cell's Jump Group (`criteria.shared_jump_groups`, and placed in
   `bt_vault_account_group_id` when set so a group policy grants it to users). It is
   born with a throwaway placeholder password;
2. a **Password Safe mirror**: managed system `<cell>-pravault` on the **"PRA Vault
   Username Password"** plugin with a managed account named exactly like the Vault
   account — the plugin resolves its PRA-side target **by name** and PATCHes each
   credential change into it;
3. a **SyncedAccounts link** making the mirror a subscriber of the cell's `adminuser`
   account, followed by one Change Password on the parent so PRA holds a real
   credential immediately (the deploy-time initial mint ran before the link existed).
   This converge follows the cloud's own change-on-register flag
   (`passwordsafe_gcp_change_password_on_register` /
   `passwordsafe_azure_change_password_on_register`, both default **on**;
   `passwordsafe_ssm_change_password_on_register` defaults **off** because SSM
   auto-management rotates on schedule) — with the flag off, PRA holds the
   placeholder until the parent's next rotation.

From then on Password Safe owns the propagation: every `adminuser` rotation lands in
the PRA Vault account, no credential passes through the dashboard, and in the PRA rep
console the account appears under **Vault Accounts** for checkout — and is offered for
**injection** when starting any of the cell's jump items (it is associated at the Jump
*Group* level, so Shell Jump, Web Jump and tunnel all see it).

**Prerequisites** (one-time, tenant-side): the "PRA Vault Username Password" custom
plugin platform imported in Password Safe, and a functional account on it (username =
the PRA OAuth client id, password = its secret). Name it in
`ot_ps_pravault_functional_account` — or leave that blank if
`clouddb_ps_pravault_functional_account` is already set for the cloud-DB feature; the
OT cell falls back to it (same for `ot_ps_pravault_platform` →
`clouddb_ps_pravault_platform`). The API identity also needs *Password Safe Account
Management* for the SyncedAccounts link.

## Using the protocol tunnels

A PRA **Protocol Tunnel Jump** forwards raw TCP from the rep's machine to the target
through the Gateway: start the jump item in the **PRA representative console** (it
shows "tunnel established" with the listen port) and point your client at
**`127.0.0.1:<local port>`** — the local port defaults to the protocol's canonical
port, so client configs read naturally. The session is audited/recorded like any other
jump; closing it closes the listener.

Per-protocol, with a client to demo with:

| Preset | Port | Client through `127.0.0.1:<port>` | Against the `ot-sim` cell |
|---|---|---|---|
| Modbus TCP | 502 | `mbpoll -a 1 -r 1 -c 4 127.0.0.1`, QModMaster, pymodbus | **Yes** — holding registers 0–3 tick every second |
| OPC UA | 4840 | UaExpert, `opcua-client` (endpoint `opc.tcp://127.0.0.1:4840`) | No — standalone tunnel to real/lab gear |
| DNP3 | 20000 | OpenDNP3 master, Axon Test | No — standalone tunnel to real/lab gear |
| Siemens S7comm | 102 | python-snap7, TIA Portal (PLC at 127.0.0.1) | No — standalone tunnel to real/lab gear |
| EtherNet/IP | 44818 | pylogix, cpppo (`Logix` driver at 127.0.0.1) | No — standalone tunnel to real/lab gear |

Notes that save demo time:

- **The cell simulates Modbus only.** The other presets exist for the **standalone
  tunnel** card — point one at your own PLC/lab gear (anything the chosen Gateway can
  reach) and demo the same brokered-access story against a real protocol stack.
- **One protocol per tunnel jump.** A tunnel carries one `local;remote` port pair. A
  cell gets one PLC tunnel (chosen at deploy); to speak a second protocol to the same
  cell or host, create a standalone tunnel with a different **name** and (if both run
  at once) a distinct local port — two tunnels listening on the same local port can't
  be open simultaneously on one rep machine.
- **Clients that follow redirects/back-connections** (some EtherNet/IP and OPC UA
  stacks re-connect to the address the server advertises) must be pointed at
  `127.0.0.1` explicitly; the tunnel forwards only the brokered port.
- The tunnel is generic TCP (`tunnel_type="tcp"`): no credential injection happens on
  the wire — the protocols above are unauthenticated-by-design in most PLCs, which is
  itself a talking point: the *network path* is the control, and PRA is the only way
  in.

## Lifecycle

- **Destroy** (OT tab button → the cloud's normal delete endpoint:
  `DELETE /api/gcp/instances/{name}` / `DELETE /api/aws/instances/{instance-id}` /
  `DELETE /api/azure/vms/{name}`) and the **auto-delete timer** both run that cloud's
  same extended destroy (`gce_destroy` / `ec2_destroy` / `azure_destroy`): remove the
  Web Jump and tunnel from their stored Terraform state, unlink the SyncedAccounts
  pair and off-board the PRA-checkout mirror, destroy the PRA Vault checkout account,
  then the Shell Jump, Password Safe and Entitle deregistrations, then the instance —
  and release the shared gateway reference last. There is no separate OT teardown
  path to forget, on any cloud.
- **Expiry**: the child is a normal deploy row for its cloud, so the cell participates
  in the auto-delete timer with no extra configuration (see
  [auto-delete-timer](auto-delete-timer.md)).
- **Air-gap**: keep the on-demand egress flags **off** for the cell's cloud
  (`gcp_vm_nat_enabled` / `aws_nat_instance_enabled`; Azure VMs have no dashboard
  NAT toggle). Turning one on gives cell subnets egress and silently deflates the
  "no path out of the plant" story. One deliberate AWS exception:
  `aws_ssm_endpoints_enabled` adds **interface endpoints inside the VPC** for the
  Password Safe SSM onboarding — private AWS API access, not internet egress, so it
  doesn't break the story.

## Standalone OT protocol tunnels

Each cloud's OT tab also creates a **standalone tunnel** to *any* host the gateway can
reach — for demoing against real lab gear without deploying a cell. It is the same
generic-TCP protocol tunnel the k8s API tunnel uses, with the OT port presets and the
same Jump Group / Gateway pickers as the cell form. State lives in config keys
(`ot_tunnel_*`, each recording which cloud's shared gateway it rides), and live
tunnels hold a reference in **that cloud's** gateway idle-teardown count, so an
unrelated decommission cannot reap the gateway mid-session. Connection details and
per-protocol clients: [Using the protocol tunnels](#using-the-protocol-tunnels).

## First-time FUXA wiring

The FUXA project is not pre-baked (its project format is version-coupled). Once per
cell (~1 minute, inside the recorded Web Jump session): FUXA → Connections → add a
**ModbusTCP** device at address `plc` port `502`, add tags for holding registers 0–3,
drop them on a view. The project persists on the VM.

## E2E verification checklist

Written for GCP (the first cloud); on AWS/Azure substitute that cloud's deploy child
(`ec2_deploy` / `azure_deploy`), destroy endpoint, functional account
(`passwordsafe_vm_functional_account_aws` / `_azure`), managed-system address shape
(`{instance-id}:{region}` / `tenantId/subscriptionId/resourceGroup/vmName`) and
gateway size key (see [the sizing guard](#the-gateway-sizing-guard)). On AWS also
verify the cell landed in the **private** subnet (no public IP on the child job) and,
for Password Safe over SSM, that `aws_ssm_endpoints_enabled` is on or the subnet
otherwise reaches the SSM control plane. **As of 2026-08-25 the AWS and Azure slices
have not been E2E-verified live** — this checklist is the script for that pass.

1. Settings: `pra_enabled` on; `bt_api_host` / `bt_client_id` / `bt_client_secret` /
   `bt_jump_group_name` / `bt_jumpoint_name` set; `gcp_jumpoint_machine_type=e2-medium`
   (delete an existing gateway VM so it recreates); `gcp_vm_nat_enabled` **off**;
   Password Safe registration on with the GCP functional account.
2. Bake `ot-sim`; it appears in the OT tab's image picker.
3. Deploy a cell **with the Jump Group + Gateway pickers set to the cell's region**;
   the parent job completes; the child holds Shell Jump id + private IP; the jump
   items land in the picked Jump Group (not the configured default).
4. PRA rep console: Shell Jump SSH works; Web Jump renders FUXA (recorded); a Modbus
   client (mbpoll / QModMaster) through the tunnel at `127.0.0.1:502` reads holding
   register 0 **incrementing every second**.
5. Password Safe: managed system `projectId/zone/instanceName` exists; the mirror
   system `<cell>-pravault` exists with account `<cell>-adminuser`; the pair shows
   under `adminuser`'s **Synced Accounts**; rotate `adminuser` and watch the change
   propagate to the mirror.
6. PRA: the Vault account `<cell>-adminuser` exists, is **check-out-able** in the rep
   console / `/login`, and is **offered for injection** when starting the cell's Shell
   Jump; after the rotation in step 5, checkout returns the NEW credential and SSH with
   it succeeds.
7. Negative test: set the gateway to `e2-micro` → a new cell fails fast with the sizing
   remedy in the job error (not a mid-session OOM). With a Gateway override picked, the
   same deploy proceeds (guard skipped, noted in progress).
8. Destroy the cell → jump items gone from PRA, the Vault checkout account gone, the
   mirror + PS system off-boarded, VM deleted, gateway reaped only once nothing else
   references it.
9. Expiry: with the timer enabled, `expires_at` is stamped on the child row; a reaped
   cell cleans up identically to a destroyed one.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Cell deploy fails immediately with a sizing message | Working as designed — the gateway is <2 GB; follow the remedy in the error |
| Web Jump session dies with "internal timeout starting session" | Gateway too small (if the guard was bypassed by resizing after deploy), or the gateway host is down — check the Gateways tab against reality |
| Tunnel connects but the Modbus client times out | The cell VM isn't running the stack — Shell Jump in and check `systemctl status ot-sim` / `docker ps` |
| Azure: Web Jump/Shell Jump work but the tunnel never establishes | The cell's Gateway resolves to an **ACI** gateway — ACI is serverless and cannot do protocol tunneling. Keep `azure_vm_jumpoint_mode=shared` and point `azure_jumpoint_name` / the form's Gateway picker at the shared **VM** gateway |
| Registers read but never change | The PLC sim container restarted into a crash loop — `docker logs ot-plc` |
| `ot-sim` bake fails at image pull | Docker Hub rate limit or a stale pin — see `provisioners/ot/README.md` (re-pin via `OT_FUXA_IMAGE`) |
| AWS bake hangs at "Waiting for SSH" | The build resolved a Debian AMI but the SSH username isn't `admin` — use the Debian 12 preset (it sets both), or fix the username field |
| AWS cell got a public IP | The chosen subnet auto-assigns them — EC2 has no per-instance switch; redeploy into the private sandbox subnet |
| AWS Password Safe onboarding fails / never rotates | The private subnet can't reach the SSM control plane — turn on `aws_ssm_endpoints_enabled` (interface endpoints, not internet egress) |
| Parent job failed after "VM deployed" | Wiring failure — the error names the failed piece; fix and **Re-wire** |
| Jump items landed in the wrong Jump Group / Gateway | The configured defaults were set up for another region — use the form's PRA placement pickers; existing cells: destroy + redeploy (jump items don't move) |
| Cell shows "wiring incomplete" but Web Jump + tunnel work | The PRA-checkout pair is missing (pre-feature cell, or it failed) — **Re-wire** creates only the missing pieces |
| PRA checkout returns a password that doesn't log in | The pair never converged — check `adminuser`'s Synced Accounts in Password Safe and its last change result; a rotation on the parent re-syncs both |
| Second protocol needed to the same host | One port pair per tunnel jump — create a standalone tunnel with another name (see [Using the protocol tunnels](#using-the-protocol-tunnels)) |

## Quick preview without a bake

`examples/compose/ot-sim.yml` runs FUXA + a *static* Modbus server through the
Containers page (ECS/ACI/GCE-COS). It is **unwired** — no PRA, no Password Safe, needs
egress at start, and register values don't change. Use it for a quick look at the
containers; use the cell for the actual demo.
