# OT demo cell image (`ot-sim`)

`ot-sim-debian.sh` bakes a self-contained OT/ICS demo cell into a Debian-family
image via the dashboard's in-app Packer feature. The deployed VM needs **zero
outbound internet** — everything is built/pulled at bake time — so it runs in the
sandbox's air-gapped private subnet, which doubles as the "plant network" in demos.

## Image contract

| What | Where | Notes |
|---|---|---|
| Modbus TCP PLC simulator | `:502` | pymodbus; holding registers 0–3 tick every second (counter, temp ×10 °C, flow, run flag); coil 0 toggles |
| OPC UA server | `:4840` | asyncua; the same four values under `Objects/Plant` as typed nodes (temperature in real °C, a Double). Anonymous, no security policy |
| EtherNet/IP tag server | `:44818` | cpppo; the same four values as `DINT` CIP tags, driven on the same one-second tick |
| Siemens S7comm server | `:102` | python-snap7's **pure-Python** S7 server (no libsnap7); the same four values as big-endian DB1 words at offsets 0/2/4/6 |
| FUXA web SCADA/HMI | `:1881` | `frangoteam/fuxa` (pinned tag); project data persists in the `fuxa_appdata` volume, **pre-seeded** with the PLC connection and its four register tags |
| Password Safe bootstrap account | `adminuser` | NOPASSWD sudo; the account `register_in_passwordsafe` onboards and rotates |
| Autostart | systemd unit `ot-sim` | `docker compose up -d` on `/opt/ot-sim/docker-compose.yml` |

All four simulators share **one image and one `pip install`**, baked from
`python:3.12-slim` — the cell carries a single copy of the base layer. `OT_SIMS`
(default `modbus,opcua,enip,s7`) picks which of them the compose stack runs; `modbus`
is mandatory, because the deploy form's default tunnel preset and the seeded FUXA
project both point at it.

**Why not DNP3.** `opendnp3` needs a native library built from source, which the
"everything is a pinned wheel" contract above cannot honour, so that preset stays
pointed at real or lab gear. **Siemens S7 used to be excluded for the same reason and
is not any more**: python-snap7 reimplemented its S7 *server* in pure Python at 3.0,
so the sim is a wheel like the others (see the pin note below).

The dashboard's **OT Demo Cell** action (each cloud page → OT tab) deploys this image
and wires the BeyondTrust access layer around it: Web Jump → `http://<vm>:1881`,
Protocol Tunnel → the chosen PLC port, plus the Shell Jump and Password Safe
onboarding the normal deploy path already provides. On GCP the cell can also be fenced
into its own Purdue zone (`ot_purdue_firewall_enabled`). See `docs/cloud-ot.md`.

## Building the image

1. Upload `ot-sim-debian.sh` to any configured storage backend (Storage page).
2. GCP page → **Build Image** tab → name it `ot-sim`, source family `debian-12`,
   load the script from storage, build (~10–15 min).
3. The result appears under Custom Images and in the OT tab's image picker
   (names containing `ot-sim` are pre-filtered).

The Packer build VM runs in the project's `default` VPC and has egress — that is
where the pulls happen. Build-time overrides (Packer env vars): `OT_ADMIN_USER`,
`OT_FUXA_IMAGE`, `OT_PYMODBUS_VERSION`, `OT_ASYNCUA_VERSION`, `OT_CPPPO_VERSION`,
`OT_SNAP7_VERSION`, `OT_SIMS`, `OT_SKIP_UPDATES=1`, `OT_SKIP_CLEANUP=1`.

## Pins

| Component | Default pin | Re-pin by |
|---|---|---|
| FUXA | `frangoteam/fuxa:1.3.4` | `OT_FUXA_IMAGE` env (never `:latest` — the bake refuses it) |
| pymodbus | `3.6.8` | `OT_PYMODBUS_VERSION` env |
| asyncua | `1.1.5` | `OT_ASYNCUA_VERSION` env |
| cpppo | `5.2.5` | `OT_CPPPO_VERSION` env |
| python-snap7 | `3.1.2` | `OT_SNAP7_VERSION` env |
| Sim base image | `python:3.12-slim` | edit the script |

**cpppo must be 5.x.** The 4.x series rewrites code objects at import and dies on
Python 3.11+ with `code() argument 13 must be str, not int` — on `python:3.12-slim`
that is every bake, at the smoke test. `test_ot_provisioner.py` refuses a 4.x pin.

**python-snap7 must be 3.x.** The S7 *server* was a ctypes binding to `libsnap7` until
3.0, which Debian does not package — that is why Siemens was absent from this image.
From 3.0 the server is pure Python, so it installs as a plain wheel. A 2.x pin brings
the native dependency back and the bake fails at the smoke test.

The bake smoke-tests **every** container it assembled — the sims `OT_SIMS` selected
plus FUXA — and fails the build if any is not running, rather than shipping an image
that boots dead inside an air-gapped subnet.

## FUXA project seeding

The bake asks the **running** FUXA for its own project, adds a `ModbusTCP` device
named `PLC` (address `plc`, port `502`) carrying four tags for holding registers 0–3,
posts it back and reads it back to confirm it took. Read-modify-write, so every part
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
**ModbusTCP** device at address `plc` port `502`, add tags for holding registers 0–3.

The other sims are reachable from FUXA by compose service name too — `S7` at `s7`:102
(DB1 words 0/2/4/6), `EthernetIP` at `enip`:44818, `OPC UA` at
`opc.tcp://opcua:4840/freeopcua/server/` — so one view can show Siemens and Rockwell tags
beside the Modbus ones.

Either way the last step is yours: **drop the tags on a view**. A FUXA view is SVG,
and its item format is the most version-coupled part of the project, so the bake does
not generate one.

## Swapping in real OpenPLC (optional)

For a demo that needs the OpenPLC brand: on the cell VM (Shell Jump),
`docker build https://github.com/thiagoralves/OpenPLC_v3.git -t openplc:local`
(pin a commit with `#<sha>`), replace the `plc` service in
`/opt/ot-sim/docker-compose.yml` (ports `502:502` + `8080:8080` for its web UI)
and `systemctl restart ot-sim`. This needs egress (temporarily enable
`gcp_vm_nat_enabled` or run the build at bake time in a customized script).
