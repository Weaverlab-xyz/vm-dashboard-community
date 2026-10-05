# OT demo cell image (`ot-sim`)

`ot-sim-debian.sh` bakes a self-contained OT/ICS demo cell into a Debian-family
image via the dashboard's in-app Packer feature. The deployed VM needs **zero
outbound internet** — everything is built/pulled at bake time — so it runs in the
sandbox's air-gapped private subnet, which doubles as the "plant network" in demos.

The simulators run on **k3s**, a single-node Kubernetes, so the cell is a plant IPC with
a real cluster on it. **Docker stays installed beside it**: k3s runs its own containerd
and does not need the host to be Docker-free, and not everything a plant (or this
dashboard) runs is Kubernetes-native. `OT_RUNTIME=docker` bakes the plain compose stack
instead — same images, same ports, no cluster — and is the fallback if a k3s bake
fails on a platform this script has not met. `OT_RUNTIME=kubesolo`, which the cell ran
before, is refused with that explanation.

**Two roles, two images.** `OT_ROLE=cell` (the default) is the plant floor, described
below. `OT_ROLE=broker` is the plant's **industrial-DMZ host**: the same k3s, the
same clients, the BeyondTrust Entitle agent's Helm chart baked in, and *no simulators* —
a DMZ host that answers Modbus is a lie about where it sits. A cell deployed with
Entitle gets one of each, and only the broker is given a way out. The role cannot be
chosen at deploy time (the VM deploy paths have no user-data hook), so bake both.

| | `OT_ROLE=cell` | `OT_ROLE=broker` |
|---|---|---|
| Simulators + FUXA | yes | no |
| k3s + kubectl + helm | yes | yes |
| Docker | yes, and it stays | never installed |
| Entitle agent chart | no | `/opt/entitle/charts/entitle-agent.tgz` (+ `CHART.txt`) |
| Egress probe image | no | `busybox:1.36`, pre-pulled into containerd |
| Bake time | ~10–15 min | ~5 min |

The chart is baked because the agent install's *other* egress dependency is the Helm
repo, which is a CDN — and a CDN cannot be named in the narrow allow-list a plant
boundary is built from. The agent's images still come from Entitle at run time; that is
what the 443 hole is for. See
[Who brokers identity in the plant](../../docs/profiles/demo/ot-demo-cell.md#who-brokers-identity-in-the-plant).

## Image contract

| What | Where | Notes |
|---|---|---|
| Modbus TCP PLC simulator | `:502` | pymodbus; holding registers 0–3 tick every second (counter, temp ×10 °C, flow, run flag); coil 0 toggles |
| OPC UA server | `:4840` | asyncua; the same four values under `Objects/Plant` as typed nodes (temperature in real °C, a Double). Anonymous, no security policy |
| EtherNet/IP tag server | `:44818` | cpppo; the same four values as `DINT` CIP tags, driven on the same one-second tick |
| Siemens S7comm server | `:102` | python-snap7's **pure-Python** S7 server (no libsnap7); the same four values as big-endian DB1 words at offsets 0/2/4/6 |
| FUXA web SCADA/HMI | `:1881` | `frangoteam/fuxa` (pinned tag); project data persists in `/var/lib/ot-sim/fuxa`, **pre-seeded** with the PLC connection and its four register tags |
| Kubernetes API | `:6443` | k3s, installed air-gapped and pinned (`OT_K3S_VERSION`, default `v1.31.4+k3s1`), with traefik, servicelb and metrics-server off. Brokered by its own PRA tunnel when the deploy form's *Kubernetes API (k3s)* entry is ticked |
| kubectl + helm | `/usr/local/bin` | kubectl is k3s's own (linked by its installer); helm is installed by the bake. `examples/playbooks/ot/` expects both on the host |
| Docker | `docker.service` | The engine that built the images, left installed and enabled. Its copies of the baked images are removed (they live in k3s's store); the build context stays in `/opt/ot-sim/plc-sim` |
| Password Safe bootstrap account | `adminuser` | NOPASSWD sudo; the account `register_in_passwordsafe` onboards and rotates |
| Autostart | systemd unit `ot-sim` | `/opt/ot-sim/k3s/apply.sh` — waits for the API, imports any missing image, applies `/opt/ot-sim/k3s/ot-sim.yaml`. (`OT_RUNTIME=docker`: `docker compose up -d` on `/opt/ot-sim/docker-compose.yml`) |

All four simulators share **one image and one `pip install`**, baked from
`python:3.12-slim` — the cell carries a single copy of the base layer. `OT_SIMS`
(default `modbus,opcua,enip,s7`) picks which of them run; `modbus` is mandatory,
because the deploy form's default tunnel preset and the seeded FUXA project both point
at it.

Every workload runs with `hostNetwork` and `imagePullPolicy: Never`. The first is what
keeps the PRA tunnels pointed at the node's own address — the Gateway dials
`<cell ip>:502`, never a cluster IP — and keeps the fieldbus ports off the CNI's
portmap path. The second is the honest failure mode for a host with no registry behind
it: a missing image says *not in the local store* rather than producing an
`ImagePullBackOff` that reads like a blocked firewall.

**Why not DNP3.** `opendnp3` needs a native library built from source, which the
"everything is a pinned wheel" contract above cannot honour, so that preset stays
pointed at real or lab gear. **Siemens S7 used to be excluded for the same reason and
is not any more**: python-snap7 reimplemented its S7 *server* in pure Python at 3.0,
so the sim is a wheel like the others (see the pin note below).

The dashboard's **OT Demo Cell** action (each cloud page → OT tab) deploys this image
and wires the BeyondTrust access layer around it: Web Jump → `http://<vm>:1881`, one
Protocol Tunnel per ticked endpoint (the PLC protocols, and the cell's own Kubernetes
API), plus the Shell Jump and Password Safe onboarding the normal deploy path already
provides. On GCP the cell can also be fenced into its own Purdue zone
(`ot_purdue_firewall_enabled`). See `docs/profiles/demo/ot-demo-cell.md`.

### How the cluster survives an egress-less subnet

The bake **installs k3s air-gapped**: it downloads the pinned k3s binary, that
release's air-gap image bundle (CoreDNS, pause, local-path) and its installer while the
build VM still has egress, and runs the installer with `INSTALL_K3S_SKIP_DOWNLOAD=true`.
Then three things worth knowing before debugging a cell:

- `/var/lib/rancher/k3s/agent/images/` is the cell's image registry. The air-gap bundle
  and every baked image (`ot-plc-sim.tar`, `fuxa.tar`) are tarballs there, and **k3s
  imports that directory on every start**. `/var/lib/ot-sim/images.txt` names what
  containerd calls each one, and `apply.sh` checks them at boot and imports anything
  missing, so a lost containerd store costs minutes, not the cell.
- images move only through **`k3s ctr`**, never a bare `ctr`. On the cell `ctr` is
  Docker's client (from `containerd.io`), which defaults to Docker's containerd: the
  wrong store, with no error.
- the bake **wipes the cluster's identity** at the end (the whole `server/` tree, the
  agent's certificates, `/etc/rancher/node`, but not `agent/images` or
  `agent/containerd`), so every cell mints its own CA and registers its own node.
  Without that, each cell would carry the build VM's Node object (a hostname it does
  not have) and every cell in the estate would share one admin credential. This is the
  same reasoning as the ssh host keys and machine-id the cleanup already drops, and it
  is why a first boot takes a few minutes: `systemctl status ot-sim` shows the progress.

**Docker and k3s on one host.** Docker sets the iptables FORWARD policy to DROP. The
plant workloads use `hostNetwork` and do not care, but CoreDNS (and OpenFaaS on the
broker) use pod networking, which k3s's flannel keeps working with its own accept
rules. The bake waits for CoreDNS to become ready, so a host where that is not true
fails the bake rather than a demo.

## Building the image

1. Upload `ot-sim-debian.sh` to any configured storage backend (Storage page).
2. GCP page → **Build Image** tab → name it `ot-sim`, source family `debian-12`,
   load the script from storage, build (~10–15 min).
3. The result appears under Custom Images and in the OT tab's image picker
   (names containing `ot-sim` are pre-filtered).
4. For the broker, build again with `OT_ROLE=broker` and put `broker` in the name
   (e.g. `ot-broker`). The role is not recorded on the image, so the pickers go by
   name: the *DMZ broker image* picker lists names containing `broker`, and the cell
   picker hides them. "Show all private images" lifts both filters.

The Packer build VM runs in the project's `default` VPC and has egress — that is
where the pulls happen. Build-time overrides (Packer env vars): `OT_RUNTIME`,
`OT_K3S_VERSION`, `OT_HELM_VERSION`, `OT_ADMIN_USER`,
`OT_FUXA_IMAGE`, `OT_PYMODBUS_VERSION`, `OT_ASYNCUA_VERSION`, `OT_CPPPO_VERSION`,
`OT_SNAP7_VERSION`, `OT_SIMS`, `OT_SKIP_UPDATES=1`, `OT_SKIP_CLEANUP=1`.

`OT_SKIP_CLEANUP=1` also skips the cluster-identity reset described above — an
iteration aid, never an image to hand out: cells baked that way all carry the build
VM's cluster. A broker bake takes the same treatment, and the same overrides:
`OT_ROLE=broker`, plus `OT_ENTITLE_CHART_VERSION` / `OT_ENTITLE_CHART_REPO` /
`OT_ENTITLE_CHART` and `OT_PROBE_IMAGE`.

## Pins

| Component | Default pin | Re-pin by |
|---|---|---|
| FUXA | `frangoteam/fuxa:1.3.4` | `OT_FUXA_IMAGE` env (never `:latest` — the bake refuses it) |
| pymodbus | `3.6.8` | `OT_PYMODBUS_VERSION` env |
| asyncua | `1.1.5` | `OT_ASYNCUA_VERSION` env |
| cpppo | `5.2.5` | `OT_CPPPO_VERSION` env |
| python-snap7 | `3.1.2` | `OT_SNAP7_VERSION` env |
| k3s | `v1.31.4+k3s1` | `OT_K3S_VERSION` env — the binary, the air-gap image bundle and the installer are all fetched for that tag |
| helm | `v3.16.3` | `OT_HELM_VERSION` env (the pin `examples/playbooks/ot/` uses) |
| kubectl | k3s's own | follows `OT_K3S_VERSION` |
| Sim base image | `python:3.12-slim` | edit the script |

**cpppo must be 5.x.** The 4.x series rewrites code objects at import and dies on
Python 3.11+ with `code() argument 13 must be str, not int` — on `python:3.12-slim`
that is every bake, at the smoke test. `test_ot_provisioner.py` refuses a 4.x pin.

**python-snap7 must be 3.x.** The S7 *server* was a ctypes binding to `libsnap7` until
3.0, which Debian does not package — that is why Siemens was absent from this image.
From 3.0 the server is pure Python, so it installs as a plain wheel. A 2.x pin brings
the native dependency back and the bake fails at the smoke test.

**Image loads go through `k3s ctr`.** It is the client that ships with the k3s binary,
so it always matches k3s's containerd and talks to k3s's socket.
`test_ot_k3s.py` refuses any image move through another client — including inside the
`apply.sh` the bake writes, since a cell checks and re-imports at every boot.

The bake smoke-tests **every** workload it assembled — the sims `OT_SIMS` selected plus
FUXA — and fails the build if any is not running, rather than shipping an image that
boots dead inside an air-gapped subnet. On the k3s runtime it goes one step
further and proves each port *answers*, because a Ready rollout is not the same claim
as a listening PLC.

## FUXA project seeding

The bake asks the **running** FUXA for its own project, adds a `ModbusTCP` device
named `PLC` (address `127.0.0.1` — or `plc`, the compose service name, on the docker
runtime — port `502`) carrying four tags for holding registers 0–3, posts it back and
reads it back to confirm it took. Read-modify-write, so every part
of the project this script does not understand survives untouched, and a FUXA whose
API moved fails the round-trip check instead of writing a broken project.

Addressing, per FUXA's Modbus driver: `memaddress` `400000` is the holding-register
region and `address` is a **1-based** offset within it, so holding register 0 is
address `"1"`. Tag type is `UInt16`.

**The seed never fails the bake.** FUXA's project format is version-coupled — that is
why a project file is not simply baked in — so if the pinned image rejects the shape,
the bake logs

```
[ot-sim] WARNING: FUXA project NOT seeded (see the error above). ...
```

and finishes. The image is then exactly what it was before this step existed, and the
connection is wired by hand, once per cell (~1 minute): FUXA → Connections → add a
**ModbusTCP** device at address `127.0.0.1` port `502`, add tags for holding registers
0–3.

The other sims answer on the same address — `S7` at `127.0.0.1`:102 (DB1 words
0/2/4/6), `EthernetIP` at `127.0.0.1`:44818, `OPC UA` at
`opc.tcp://127.0.0.1:4840/freeopcua/server/` — so one view can show Siemens and Rockwell
tags beside the Modbus ones. (Under `OT_RUNTIME=docker` those are the compose service
names `plc`, `s7`, `enip` and `opcua` instead.)

Either way the last step is yours: **drop the tags on a view**. A FUXA view is SVG,
and its item format is the most version-coupled part of the project, so the bake does
not generate one.

## Swapping in real OpenPLC (optional)

For a demo that needs the OpenPLC brand, build it at bake time in a customized copy of
this script (`docker build https://github.com/thiagoralves/OpenPLC_v3.git -t
openplc:local`, pinning a commit with `#<sha>`), save it into
`/var/lib/rancher/k3s/agent/images/` alongside the others, and point the `ot-plc`
Deployment in `/opt/ot-sim/k3s/ot-sim.yaml` at it — adding `8080` for its web UI. Doing
it on a deployed cell instead means giving that cell egress, which it does not have:
Docker is on the cell, but there is nothing for it to pull from. The honest options are
a custom bake, or a temporary `gcp_vm_nat_enabled`.
