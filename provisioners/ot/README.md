# OT demo cell image (`ot-sim`)

`ot-sim-debian.sh` bakes a self-contained OT/ICS demo cell into a Debian-family
image via the dashboard's in-app Packer feature. The deployed VM needs **zero
outbound internet** — everything is built/pulled at bake time — so it runs in the
sandbox's air-gapped private subnet, which doubles as the "plant network" in demos.

## Image contract

| What | Where | Notes |
|---|---|---|
| Modbus TCP PLC simulator | `:502` | pymodbus, baked from `python:3.12-slim`; holding registers 0–3 tick every second (counter, temp ×10 °C, flow, run flag); coil 0 toggles |
| FUXA web SCADA/HMI | `:1881` | `frangoteam/fuxa` (pinned tag); project data persists in the `fuxa_appdata` volume |
| Password Safe bootstrap account | `adminuser` | NOPASSWD sudo; the account `register_in_passwordsafe` onboards and rotates |
| Autostart | systemd unit `ot-sim` | `docker compose up -d` on `/opt/ot-sim/docker-compose.yml` |

The dashboard's **OT Demo Cell** action (GCP page → OT tab) deploys this image and
wires the BeyondTrust access layer around it: Web Jump → `http://<vm>:1881`,
Protocol Tunnel → `<vm>:502`, plus the Shell Jump and Password Safe onboarding the
normal GCE deploy path already provides. See `docs/cloud-ot.md`.

## Building the image

1. Upload `ot-sim-debian.sh` to any configured storage backend (Storage page).
2. GCP page → **Build Image** tab → name it `ot-sim`, source family `debian-12`,
   load the script from storage, build (~10–15 min).
3. The result appears under Custom Images and in the OT tab's image picker
   (names containing `ot-sim` are pre-filtered).

The Packer build VM runs in the project's `default` VPC and has egress — that is
where the pulls happen. Build-time overrides (Packer env vars): `OT_ADMIN_USER`,
`OT_FUXA_IMAGE`, `OT_PYMODBUS_VERSION`, `OT_SKIP_UPDATES=1`, `OT_SKIP_CLEANUP=1`.

## Pins

| Component | Default pin | Re-pin by |
|---|---|---|
| FUXA | `frangoteam/fuxa:1.3.4` | `OT_FUXA_IMAGE` env (never `:latest` — the bake refuses it) |
| pymodbus | `3.6.8` | `OT_PYMODBUS_VERSION` env |
| PLC sim base | `python:3.12-slim` | edit the script |

The bake smoke-tests both containers and **fails the build** if either is not
running — better than an image that boots dead inside an air-gapped subnet.

## First-time FUXA wiring (once per cell, ~1 minute)

The FUXA project is not pre-baked (its project format is version-coupled). After
opening the HMI through the Web Jump: FUXA → Connections → add a **ModbusTCP**
device at address `plc` port `502`, then add tags for holding registers 0–3 and
drop them on a view. The values tick once a second. The project persists on the
VM for the life of the cell.

## Swapping in real OpenPLC (optional)

For a demo that needs the OpenPLC brand: on the cell VM (Shell Jump),
`docker build https://github.com/thiagoralves/OpenPLC_v3.git -t openplc:local`
(pin a commit with `#<sha>`), replace the `plc` service in
`/opt/ot-sim/docker-compose.yml` (ports `502:502` + `8080:8080` for its web UI)
and `systemctl restart ot-sim`. This needs egress (temporarily enable
`gcp_vm_nat_enabled` or run the build at bake time in a customized script).
