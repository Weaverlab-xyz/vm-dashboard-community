# OT Demo Cell

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are showing an air-gapped plant cell and the PAM layers on top of it.

The dashboard can stand up a simulated **OT/ICS plant cell** — Modbus, Siemens
S7comm, Rockwell EtherNet/IP and OPC UA PLC simulators plus the FUXA web SCADA/HMI —
inside a cloud sandbox's **private,
egress-less subnet**, then layer the BeyondTrust PAM stack on top. Same
**provisioning + three layers** model as [Cloud VMs](../../cloud/vms.md); the OT twist is
that the air-gapped subnet *is* the plant network, and every path in is PRA-brokered:

- **Provisioning** *(stand it up)* — deploy a VM from the Packer-baked **`ot-sim`**
  image (`provisioners/ot/ot-sim-debian.sh`). Everything is baked at build time, so the
  running cell needs **zero outbound internet**: a PLC simulator whose holding registers
  tick every second (:502), the same four process values over **Siemens S7comm** (:102),
  **Rockwell EtherNet/IP** (:44818) and **OPC UA** (:4840), and FUXA (:1881) with its PLC
  connection pre-seeded. They run as workloads of **k3s**, a single-node Kubernetes
  that keeps Docker on the host beside it, so the cell is a plant IPC running a real
  cluster, and its Kubernetes API (:6443) is one more thing PRA can broker. A systemd
  unit applies the workloads at boot.
- **Layer 1 — PRA** *(reach it)* — auto-provisioned per cell:
  - **Web Jump** → `http://<vm>:1881` (the HMI, rendered and recorded on the gateway);
  - **one Protocol Tunnel per endpoint you tick** (generic TCP), named
    `ot-<cell>-<protocol>` — so a rep sees the Siemens PLC and the Rockwell PLC as
    distinct targets and a Jump Group policy can grant them separately, rather than
    one opaque item opening every port. The cell's own **Kubernetes API** is on the
    same list (`ot-<cell>-k3s` → :6443), so "read the PLC but not the cluster"
    is a policy decision rather than a network one;
  - **Shell Jump** → SSH, inherited from the cloud's normal VM deploy path.
- **Layer 2 — Password Safe** *(manage its secrets)* — *optional, default on.* The
  image's `adminuser` is onboarded via the cloud-native plugin the cloud's VM deploy
  path already uses — **`gcpvm`** on GCP (managed system address
  `projectId/zone/instanceName`), **`ssm`** on AWS (managed system DNS
  `{instance-id}:{region}`, over Systems Manager), **`azurevm`** on Azure (address
  `tenantId/subscriptionId/resourceGroup/vmName`, over Run Command). On top of that,
  the wiring makes the credential **usable in PRA**: a PRA Vault username/password
  account plus a Password Safe mirror on the "PRA Vault Username Password" plugin,
  linked with SyncedAccounts, then rotated once so PRA holds a real credential from the
  start — see
  [PRA checkout of the cell's admin credential](#pra-checkout-of-the-cells-admin-credential).
- **Layer 3 — Entitle** *(grant time-boxed access)* — *optional.* SSH ephemeral
  accounts — brokered by an agent running **in the plant**, on a DMZ host deployed
  beside the cell, because that is the only arrangement in which "Entitle manages
  access to plant resources" is true as stated. See
  [Who brokers identity in the plant](ot-demo-cell/identity.md#who-brokers-identity-in-the-plant).

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
through a tunnel, and never learns a credential. The values *change* every second
(counter, temperature, flow), so a client through the tunnel visibly shows live process
data, not a static mock — and the same four values are served over **Modbus, Siemens
S7comm, Rockwell EtherNet/IP and OPC UA**, so the story holds whichever protocol the
customer's plant speaks. Ticking several protocols on one cell is what turns "we are a
Siemens shop" and "we are a Rockwell shop" into the same demo.

The cell also **is** a Kubernetes host — the simulators are its workloads, on k3s, with
Docker still on the machine for whatever is not a Kubernetes workload — so "can a plant
IPC carry a cluster?" has a running answer instead of a slide. See
[The cell runs on k3s](#the-cell-runs-on-k3s).

## The cell runs on k3s

The baked image installs **k3s** and runs the four simulators and FUXA on it as
Deployments in the `ot-sim` namespace. Nothing the customer sees changes: same images,
same ports, same Web Jump, same protocol tunnels. Every workload runs with
`hostNetwork`, so it binds the node's own address, which is exactly what the Gateway
dials.

**Why k3s, and why Docker stays.** The cell used to run KubeSolo, and KubeSolo's
installer refuses any host that still carries Docker, so the bake purged it. But not
everything that runs beside a demo is Kubernetes-native, and this dashboard itself runs
a lot of containers. k3s brings its own containerd and CNI and runs beside Docker's,
so the cell keeps both: the plant workloads on the cluster, and an engine for anything
else. KubeSolo is still documented as an edge option for hosts where it fits
([KubeSolo](../../kubernetes/kubesolo.md)); it is no longer what the cell runs.

Tick **Kubernetes API (k3s)** on the deploy form and the cell gets a tunnel to `:6443`
beside its fieldbus ones, named `ot-<cell>-k3s`. With that jump started in the rep
console:

```bash
# once, through the Shell Jump: the cell writes a kubeconfig aimed at the tunnel
sudo cat /var/lib/ot-sim/kubeconfig-via-tunnel.yaml    # → save it as ot-cell.yaml

# then from your own machine, through the tunnel
kubectl --kubeconfig ot-cell.yaml -n ot-sim get pods -o wide
kubectl --kubeconfig ot-cell.yaml -n ot-sim logs deploy/ot-plc
```

```powershell
# the same from Windows, once ot-cell.yaml is saved beside you
kubectl --kubeconfig .\ot-cell.yaml -n ot-sim get pods -o wide
kubectl --kubeconfig .\ot-cell.yaml -n ot-sim logs deploy/ot-plc
```

That file is k3s's admin kubeconfig with its server pinned to
`https://127.0.0.1:6443`, the tunnel's local end. k3s's API certificate already names
`127.0.0.1`, so no certificate override is needed.

**How it stays air-gapped.** The cell has no egress and no registry, so the whole image
supply is baked. The bake fetches the k3s binary **and the release's air-gap image
bundle** (CoreDNS, pause, local-path) and installs with nothing to download. The
simulator and FUXA images are saved as tarballs into
`/var/lib/rancher/k3s/agent/images/`, the directory k3s imports from **on every start**,
and the manifests say `imagePullPolicy: Never`, so a missing image says *not in the
local store* instead of producing an `ImagePullBackOff` that reads like a firewall.
Traefik, ServiceLB and metrics-server are switched off: the first two would claim host
ports on a machine whose ports are the plant's protocols.

The first boot of a cell takes a few minutes longer than a docker one: it mints the
cluster's own CA and node identity (the bake deliberately does not clone those into
every cell). `systemctl status ot-sim` shows that progress; the Web Jump is worth
trying only once it reports `active (exited)`.

**Docker is on the cell, and running.** `docker ps` works, and the plant workloads are
not in it: they are `kubectl -n ot-sim get pods`, and `docker logs ot-plc` is
`kubectl -n ot-sim logs deploy/ot-plc`. The bake removes Docker's own copies of the
baked images (they live in k3s's store), but the build context stays in
`/opt/ot-sim/plc-sim`. `kubectl` (from k3s) and `helm` are on the host, and the broker's
plays in
[`examples/playbooks/ot/`](https://github.com/Weaverlab-xyz/vm-dashboard-community/tree/main/examples/playbooks/ot)
expect both. Baking with **`OT_RUNTIME=docker`** brings the plain compose stack back if a
k3s bake ever fails on a platform the script has not met.

**Tell the form which one you baked.** The deploy form has an *Image runtime* picker,
because the dashboard cannot read this off an image — both bakes produce an `ot-sim`
image, and the runtime is a bake-time environment variable that leaves no trace the
cloud exposes. It gates one thing: the cell's own platform endpoints, which exist
because the cell runs a cluster. Pick `docker` and the *Kubernetes API (k3s)* entry
disappears from the protocol list, and the deploy refuses it if you send it anyway — a
tunnel to :6443 on a cell with no cluster is a session that fails exactly like a
blocked firewall, which is the most expensive kind of demo failure there is.

**Cells deployed on KubeSolo** keep working: they still show, re-wire and tear down,
and their `ot-<cell>-kubesolo` tunnel is still recognised. A new deploy that names the
`kubesolo` runtime is refused, because the bake no longer produces that image.

**The agent does not run here** — it runs on the plant's DMZ broker, one zone over, and
that is the whole point of [Who brokers identity in the plant](ot-demo-cell/identity.md#who-brokers-identity-in-the-plant).
The plant floor keeps a true air gap; the machine with a way out is a different machine.

## Who brokers identity in the plant

How people and the Entitle agent get an identity inside the plant, and where the broker sits: see [Who brokers identity](ot-demo-cell/identity.md#who-brokers-identity-in-the-plant).

## Deploying a cell

Baking the images and deploying a cell, per cloud: see [Deploying a cell](ot-demo-cell/deploying.md#deploying-a-cell).

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

That last rotation is governed by **`ot_ps_checkout_converge`** (default **on**), and
deliberately *not* by the cloud's change-on-register flag. The two answer different
questions: change-on-register asks "rotate the credential when we first onboard it?",
the converge asks "a subscriber appeared *after* the mint — push one change through
it?". They only looked alike on GCP and Azure, where the flag defaults on. On AWS
`passwordsafe_ssm_change_password_on_register` defaults **off** (SSM auto-management
rotates on its own schedule), so every fresh AWS cell's Vault account held the throwaway
placeholder until some later rotation — a checkout that hands the rep a password which
does not log in. With `ot_ps_checkout_converge` off, that is the behaviour you get back,
on every cloud.

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

**Setting up a rep machine:** [OT protocol clients on Windows](ot-demo-cell/ot-protocol-clients.md) walks through installing the four Python clients and
running [`scripts/ot/verify_tunnels.py`](../../../scripts/ot/verify_tunnels.py), which
reads every protocol through its tunnel and tells you which are live *before* you
share your screen.

Per-protocol, with a client to demo with:

| Preset | Port | Client through `127.0.0.1:<port>` | Against the `ot-sim` cell |
|---|---|---|---|
| Modbus TCP | 502 | `mbpoll -a 1 -r 1 -c 4 127.0.0.1`, QModMaster, pymodbus | **Yes** — holding registers 0–3 tick every second |
| OPC UA | 4840 | UaExpert, `opcua-client` (endpoint `opc.tcp://127.0.0.1:4840`) | **Yes** — `Objects/Plant` → Counter, Temperature, Flow, Running; anonymous, no security policy |
| EtherNet/IP | 44818 | pylogix, cpppo (`Logix` driver at 127.0.0.1) | **Yes** — the same four values as `DINT` tags |
| Siemens S7comm | 102 | python-snap7 (`db_read(1, 0, 8)`), TIA Portal (PLC at 127.0.0.1) | **Yes** — the same four values as big-endian DB1 words at offsets 0/2/4/6 |
| Kubernetes API (k3s) | 6443 | `kubectl --kubeconfig ot-cell.yaml` — the file the cell writes at `/var/lib/ot-sim/kubeconfig-via-tunnel.yaml` | **Yes** — the cell's own cluster; the simulators are its `ot-sim` namespace |
| DNP3 | 20000 | OpenDNP3 master, Axon Test | No — standalone tunnel to real/lab gear |

Notes that save demo time:

- **The cell answers Modbus, Siemens S7comm, EtherNet/IP and OPC UA — DNP3 it does
  not.** `opendnp3` needs a native library built from source, which the baked image's
  everything-is-a-pinned-wheel contract cannot honour, so that one preset exists for
  the **standalone tunnel** card — point it at your own PLC/RTU (anything the chosen
  Gateway can reach) and demo the same brokered-access story against a real protocol
  stack. `OT_SIMS` at bake time picks which sims the image carries (default: all four).
  Siemens was in the same excluded bucket until python-snap7 3.0 reimplemented its S7
  server in pure Python; an image baked before that carries no `ot-s7` container.
- **A cell gets a tunnel per endpoint you tick** — Modbus is pre-checked, and each
  extra vendor becomes its own named jump item, so showing a Siemens PLC *and* a
  Rockwell PLC on one air-gapped host, each brokered separately, needs no manual
  wiring. To reach a protocol the image does not simulate (DNP3) or a different host,
  the **standalone tunnel** card still points anywhere the Gateway can reach.
- **The Kubernetes API is on that list but is not a fieldbus protocol**, and the form
  groups it apart for that reason: it is the cell's own k3s cluster, the thing running
  the simulators. The same preset on the **standalone tunnel** card brokers any real
  plant host's Kubernetes API on :6443 (k3s, or the KubeSolo that
  [`kubesolo/kubesolo-install.yml`](https://github.com/Weaverlab-xyz/vm-dashboard-community/tree/main/examples/playbooks/kubesolo)
  puts on an IPC) without opening anything else on it.
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
  Web Jump and **every** protocol tunnel from their stored Terraform state, unlink
  the SyncedAccounts
  pair and off-board the PRA-checkout mirror, destroy the PRA Vault checkout account,
  then the Shell Jump, Password Safe and Entitle deregistrations, then the instance —
  and release the shared gateway reference last. There is no separate OT teardown
  path to forget, on any cloud.
- **Clear** (`DELETE /api/ot/cell/{vm_job_id}`) is the exit for a cell whose VM
  deploy *failed*, which Destroy cannot see — see
  [Clearing a cell whose VM never deployed](ot-demo-cell/deploying.md#clearing-a-cell-whose-vm-never-deployed).
  It destroys no VM; it refuses if either the cell **or its DMZ broker** is still there.
  The broker matters because it deploys *first*, so the common failure is "broker up,
  cell failed" — clearing only the cell would retire the card while a VM carrying a
  working Entitle agent kept running and kept billing. Clear does destroy one thing: the
  plant's **agent token**, which is minted before either VM exists and which the normal
  destroy runner only ever collects for a deploy that *completed*.
- **Expiry**: the child is a normal deploy row for its cloud, so the cell participates
  in the auto-delete timer with no extra configuration (see
  [auto-delete-timer](../../operations/auto-delete-timer.md)). A cell with a broker gives the
  broker **its own expiry**, not the policy default: on separate clocks the two diverge
  the moment anyone extends one, and whichever reaped first would leave the other
  useless — an agent brokering access to a plant that is gone, or a cell whose Entitle
  grants quietly stop working.
- **Air-gap**: keep the on-demand egress flags **off** for the cell's cloud
  (`gcp_vm_nat_enabled` / `aws_nat_instance_enabled`; Azure VMs have no dashboard
  NAT toggle). Turning one on gives cell subnets egress and silently deflates the
  "no path out of the plant" story. On GCP,
  [Purdue-zone firewalling](ot-demo-cell/deploying.md#purdue-zone-firewalling) removes that coupling
  entirely — the cell's own egress deny outranks the NAT allow. One deliberate AWS exception:
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

## FUXA wiring

The bake **pre-seeds the project** with a `ModbusTCP` device named `PLC` (address
`127.0.0.1`, port `502`) and four tags for holding registers 0–3, by asking the running
FUXA for its own project, adding the device and posting it back. So inside the recorded
Web Jump session the remaining step is just to **drop the tags on a view** — a FUXA view
is SVG and its item format is the most version-coupled part of the project, so the bake
does not generate one.

The address is the loopback because every workload runs with `hostNetwork`: FUXA and the
simulators share the node's network namespace, so they reach each other exactly as a
client through a tunnel does. (An image baked with `OT_RUNTIME=docker` uses the compose
service names — `plc`, `s7`, `enip`, `opcua` — instead.)

If your bake log says `WARNING: FUXA project NOT seeded`, the pinned FUXA rejected the
shape and the image is exactly as it was before seeding existed: add the connection by
hand, once per cell (~1 minute) — FUXA → Connections → **ModbusTCP** at `plc`:`502`,
then tags for holding registers 0–3. Either way the project persists on the VM.

Only the Modbus device is seeded, but FUXA also speaks the other three, and every
simulator answers on the same address it does — so adding a second vendor to the same
view is a one-minute job:

| Device type | Address | Port | Tags |
|---|---|---|---|
| S7 | `127.0.0.1` | 102 | DB1 words at offsets 0, 2, 4, 6 |
| EthernetIP | `127.0.0.1` | 44818 | `COUNTER`, `TEMPERATURE`, `FLOW`, `RUNNING` |
| OPC UA | `opc.tcp://127.0.0.1:4840/freeopcua/server/` | 4840 | `Plant/Counter`, `…/Temperature`, `…/Flow`, `…/Running` |

A single recorded HMI session showing a **Siemens and a Rockwell** device side by side,
on a host with no route to the internet, is the demo this cell exists for.

## E2E verification checklist

Proving a deployed cell works, layer by layer: see the [verification checklist](ot-demo-cell/verification.md#e2e-verification-checklist).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Cell deploy fails immediately with a sizing message | Working as designed — the gateway is <2 GB; follow the remedy in the error |
| Web Jump session dies with "internal timeout starting session" | Gateway too small (if the guard was bypassed by resizing after deploy), or the gateway host is down — check the Gateways tab against reality |
| Tunnel connects but the Modbus client times out | The cell VM isn't running the stack — Shell Jump in and check `systemctl status ot-sim` / `kubectl -n ot-sim get pods` |
| Everything times out for the first few minutes of a fresh cell | Working as designed — first boot mints the cluster's CA and node identity and loads the baked images, with no registry to shortcut it. `systemctl status ot-sim` shows the progress and reaches `active (exited)` |
| Azure: Web Jump/Shell Jump work but the tunnel never establishes | The cell's Gateway resolves to an **ACI** gateway — ACI is serverless and cannot do protocol tunneling. Keep `azure_vm_jumpoint_mode=shared` and point `azure_jumpoint_name` / the form's Gateway picker at the shared **VM** gateway |
| Registers read but never change | The sim restarted into a crash loop — `kubectl -n ot-sim logs deploy/ot-plc` (S7: `ot-s7`; OPC UA: `ot-opcua`; EtherNet/IP: `ot-enip`) |
| An S7 / OPC UA / EtherNet-IP tunnel connects but nothing answers | That sim was not baked — `OT_SIMS` at bake time selects them (default is all four), and an image baked before Siemens was added has no `ot-s7`. `kubectl -n ot-sim get pods` on the cell shows which are running |
| `kubectl` through the tunnel fails on a certificate error | The kubeconfig is not the one the cell wrote. Use `/var/lib/ot-sim/kubeconfig-via-tunnel.yaml`, whose server is `https://127.0.0.1:6443`, a name k3s's certificate covers |
| A pod is `ErrImageNeverPull` | Its image is not in the node's containerd. The cell pulls nothing by design — re-run `/opt/ot-sim/k3s/apply.sh`, which re-imports from `/var/lib/rancher/k3s/agent/images` (or restart k3s, which imports that directory on start) |
| CoreDNS or an OpenFaaS pod never becomes ready, but the hostNetwork sims are fine | Pod networking is broken on the host. Docker sets the FORWARD policy to DROP; k3s's flannel adds its own accept rules, so check `sudo iptables -S FORWARD` for them and `journalctl -u k3s` for flannel errors |
| The k3s tunnel connects but nothing answers on :6443 | The image was baked with `OT_RUNTIME=docker`, so the cell runs no cluster. A new deploy refuses this — set *Image runtime* to match what you baked — so a cell in this state predates that guard, or was deployed through the API with the wrong `runtime`. Rebake with the default runtime, or untick that entry and Re-wire |
| The deploy refuses, naming `routing` | The tenant is on `routing: v0`, whose agent pulls its image from `ghcr.io` / `gcr.io/datadoghq`. No narrow allow-list can name a CDN, so the agent could not start inside the plant. Ask BeyondTrust to migrate the tenant, or deploy the cell without Entitle |
| The deploy refuses, saying the token's region is not the one the hole was drawn for | `entitle_api_url` is a proxy or a bare host, so the region fell back to the default while the tenant is somewhere else. Set it to the regional URL and redeploy — otherwise the agent would dial a host the plant denies, with nothing saying why |
| The card says "not pinned" in amber | `ot_dmz_egress_open_ports` is on, so the broker's rule is 443/8080 to anywhere. That is a weaker claim than an address list — set `ot_entitle_egress_cidrs` and **Re-wire** if you meant to have the narrow one |
| The card reports egress drift | What the rule would be drawn from now differs from what it was drawn from. **Re-wire** re-resolves and re-applies; until then the agent may have lost its channel |
| "Can it really not reach anything?" | **Probe egress** on the card. It runs a throwaway pod on the broker and reports DNS, 443, 8080 and the cell's :22 — from inside the plant, and it changes nothing |
| Clear refuses, naming the DMZ broker | The broker outlived its failed cell — it deploys first, so this is the common shape. Destroy it from the cloud's VMs tab, then clear the cell |
| The Entitle grant approves but the vendor's login is refused | The agent cannot reach the cell on :22. On a cell with its own broker, check `<cell>-ot-ingress-agent` exists; on one without, that is the old shared-agent arrangement, which the Purdue zoning blocks by design — redeploy with Entitle ticked |
| The agent install job fails at "Prove the agent's network path" | Working as designed, and it names which leg failed: DNS, 443/8080, or the cell's :22. Pod SNAT, the DNS hole and the destination set are the three candidates, in that order |
| The agent was fine and now is not | The Entitle endpoint's addresses moved. They are pinned at wiring time unless `ot_entitle_egress_cidrs` is set — **Re-wire** re-resolves and replaces the rule |
| Deploying with Entitle refuses, naming a setting | Working as designed: the in-plant agent needs the Purdue zoning, a broker image, a destination set, an in-cloud runner and an 8 GB broker. The error names the one that is missing |
| The bake fails with `OT_RUNTIME=kubesolo is no longer baked` | Working as designed: the cell and broker run k3s now. Drop the variable (k3s is the default) |
| FUXA opens with no PLC connection | The bake's project seed was skipped — search the bake log for `FUXA project NOT seeded`, and wire it by hand (`provisioners/ot/README.md`) |
| AWS cell deploy fails immediately naming the subnet | Working as designed — that subnet auto-assigns public IPs; use the private sandbox subnet or clear `ot_aws_require_private_subnet` |
| GCP cell unreachable right after enabling Purdue firewalling | The Gateway you are brokering through is not the managed one, so it does not carry the `bt-jumpoint` tag the ingress allow matches. Delete the cell's `*-ot-ingress-deny` rule, then either use the managed Gateway or add that tag to yours |
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
