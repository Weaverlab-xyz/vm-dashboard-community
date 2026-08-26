#!/bin/sh
# ot-sim-debian.sh — bake a self-contained OT/ICS demo cell into a Debian-family image.
#
# What the built image runs at boot (systemd unit `ot-sim`, docker compose):
#   plc   — a Modbus TCP "PLC" simulator on :502 (pymodbus, BUILT at bake time from
#           python:3.12-slim) whose holding registers tick every second: a counter,
#           a sine-wave temperature (x10 °C) and flow value, and a run flag — so a
#           Modbus client through a PRA Protocol Tunnel shows LIVE process data.
#   opcua — the same four process values over OPC UA on :4840 (asyncua), under
#           Objects/Plant. Anonymous, no security policy — like most plant gear.
#   enip  — the same four values as CIP tags over EtherNet/IP on :44818 (cpppo).
#   hmi   — the FUXA web SCADA/HMI on :1881 (frangoteam/fuxa, pinned version tag),
#           reached via a PRA Web Jump. Its project data persists in a named volume,
#           pre-seeded at bake time with the PLC connection and its register tags.
#
# The three sims share ONE image and one pip install; OT_SIMS picks which run. DNP3
# and Siemens S7 are deliberately absent: both need native libraries built from
# source, so those tunnel presets stay real-gear-only (see README.md).
#
# Everything is pulled/built AT BAKE TIME (the Packer build VM has egress); the
# deployed VM needs ZERO outbound internet, so the cell runs in the sandbox's
# air-gapped private subnet — that egress-less subnet IS the "plant network" story.
#
# Self-elevates to root via sudo -E (Packer invokes the shell provisioner as the
# cloud-default user). POSIX sh only — no [[ ]], no arrays, no <<<.
#
# Operator-overridable via Packer build env:
#   OT_ADMIN_USER    Password-Safe-managed bootstrap account name (default: adminuser)
#   OT_FUXA_IMAGE    FUXA image ref (default: frangoteam/fuxa:1.3.4 — pin a version,
#                    never :latest; a floating tag makes bakes unreproducible)
#   OT_PYMODBUS_VERSION  pymodbus pin for the baked PLC sim (default: 3.6.8)
#   OT_ASYNCUA_VERSION   asyncua pin for the OPC UA sim (default: 1.1.5)
#   OT_CPPPO_VERSION     cpppo pin for the EtherNet/IP sim (default: 5.2.5)
#   OT_SIMS              which sims to bake, comma-separated (default: modbus,opcua,enip;
#                        modbus is mandatory — the deploy's default tunnel preset and
#                        the seeded FUXA project both point at it)
#   OT_SKIP_UPDATES=1    skip dist-upgrade (faster iteration builds)
#   OT_SKIP_CLEANUP=1    skip image-reuse cleanup (keep host keys, machine-id, logs)
#
# See provisioners/ot/README.md for the image contract (ports, account, rebuild).

if [ "$(id -u)" -ne 0 ]; then
  exec sudo -E sh "$0" "$@"
fi

set -eu

log() { echo "[ot-sim] $*"; }
die() { echo "[ot-sim] ERROR: $*" >&2; exit 1; }

# ── 1. OS-family gate ────────────────────────────────────────────────────────
[ -f /etc/debian_version ] || die "not a Debian-family system (no /etc/debian_version)"
log "starting ot-sim bake on $(cat /etc/debian_version 2>/dev/null || echo unknown) ($(uname -m))"

# ── 2. System updates ────────────────────────────────────────────────────────
export DEBIAN_FRONTEND=noninteractive
if [ "${OT_SKIP_UPDATES:-0}" = "1" ]; then
  log "OT_SKIP_UPDATES=1 — skipping dist-upgrade"
  apt-get update -q
else
  log "applying security + bugfix updates"
  apt-get update -q
  apt-get -y -q -o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef dist-upgrade
  apt-get -y -q autoremove
fi

# ── 3. Password-Safe bootstrap account ───────────────────────────────────────
# The account the dashboard's GCP VM SSH Rotation plugin onboards and rotates
# (register_in_passwordsafe on the deploy). Mirrors bt-ready-debian.sh's adminuser.
OT_ADMIN_USER="${OT_ADMIN_USER:-adminuser}"
log "creating Password-Safe bootstrap user: $OT_ADMIN_USER"
if ! printf '%s' "$OT_ADMIN_USER" | grep -Eq '^[a-z_][a-z0-9_-]*$'; then
  die "OT_ADMIN_USER contains an invalid account name: '$OT_ADMIN_USER'"
fi
if ! id -u "$OT_ADMIN_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$OT_ADMIN_USER"
fi
SUDOERS=/etc/sudoers.d/90-ot-sim
cat > "$SUDOERS" <<EOF
# Managed by the ot-sim provisioner. Password-Safe-friendly NOPASSWD sudo.
$OT_ADMIN_USER ALL=(ALL) NOPASSWD: ALL
EOF
chmod 0440 "$SUDOERS"
if ! visudo -c -f "$SUDOERS" >/dev/null; then
  rm -f "$SUDOERS"
  die "visudo rejected 90-ot-sim — sudoers not installed"
fi

# ── 4. Docker Engine + compose plugin (from download.docker.com, not Docker Hub) ─
log "installing Docker Engine + compose plugin"
apt-get -y -q install ca-certificates curl gnupg
. /etc/os-release
case "${ID:-}" in
  debian|ubuntu) ;;
  *) die "unsupported distro ID '${ID:-unknown}' — download.docker.com serves debian/ubuntu" ;;
esac
install -m 0755 -d /etc/apt/keyrings
curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -q
apt-get -y -q install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Belt-and-braces against Docker Hub anonymous rate limits during the bake: prefer
# Google's Hub mirror for the pulls this script does make (FUXA, python:3.12-slim).
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": ["https://mirror.gcr.io"]
}
EOF
systemctl enable docker >/dev/null 2>&1 || true
systemctl restart docker
docker version >/dev/null || die "docker did not come up after install"

# ── 5. The OT sim stack (built + pre-pulled NOW, so runtime needs no egress) ──
OT_FUXA_IMAGE="${OT_FUXA_IMAGE:-frangoteam/fuxa:1.3.4}"
OT_PYMODBUS_VERSION="${OT_PYMODBUS_VERSION:-3.6.8}"
OT_ASYNCUA_VERSION="${OT_ASYNCUA_VERSION:-1.1.5}"
# 5.2.5, not the 4.x series: cpppo <5 rewrites code objects at import and dies on
# Python 3.11+ with "code() argument 13 must be str", which on python:3.12-slim is
# every bake.
OT_CPPPO_VERSION="${OT_CPPPO_VERSION:-5.2.5}"
# Which protocol servers the cell answers. The PRA tunnel presets cover five
# protocols; these are the three that simulate honestly from a pure-python,
# no-egress-at-runtime base. DNP3 (opendnp3) and S7 (snap7) both need native
# libraries built from source, so those presets stay real-gear-only — see
# provisioners/ot/README.md.
OT_SIMS="${OT_SIMS:-modbus,opcua,enip}"
case "$OT_FUXA_IMAGE" in
  *:latest) die "OT_FUXA_IMAGE must be pinned to a version tag, not :latest" ;;
  *:*) ;;
  *) die "OT_FUXA_IMAGE must carry an explicit version tag (got '$OT_FUXA_IMAGE')" ;;
esac

# POSIX sh has no arrays: membership is a comma-delimited substring test.
sim_enabled() {
  case ",$OT_SIMS," in
    *",$1,"*) return 0 ;;
    *) return 1 ;;
  esac
}
for _s in $(echo "$OT_SIMS" | tr ',' ' '); do
  case "$_s" in
    modbus|opcua|enip) ;;
    *) die "OT_SIMS names an unknown simulator '$_s' (known: modbus, opcua, enip)" ;;
  esac
done
sim_enabled modbus || die "OT_SIMS must include modbus — the cell's Modbus PLC is what \
the deploy's default tunnel preset and the FUXA project both point at"

log "writing /opt/ot-sim (compose stack + simulator sources); sims: $OT_SIMS"
mkdir -p /opt/ot-sim/plc-sim

cat > /opt/ot-sim/plc-sim/plc_sim.py <<'EOF'
"""Tiny Modbus TCP "PLC" for OT demos: registers tick so tunnel reads show life.

Holding registers (zero-based, function code 3):
  0  counter        increments every second, wraps at 65535
  1  temperature    ~400 +/- 25, i.e. degrees C x10 (sine wave)
  2  flow           ~120 +/- 30 (sine wave)
  3  running        always 1
Coil 0 toggles every second (function code 1).
"""
import math
import threading
import time

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartTcpServer


def _updater(context):
    store = context[0]
    t = 0
    while True:
        t += 1
        counter = t % 65536
        temperature = int(400 + 25 * math.sin(t / 15.0))
        flow = int(120 + 30 * math.sin(t / 7.0))
        store.setValues(3, 0, [counter, temperature, flow, 1])
        store.setValues(1, 0, [1 if t % 2 == 0 else 0])
        time.sleep(1)


def main():
    store = ModbusSlaveContext(
        di=ModbusSequentialDataBlock(0, [0] * 100),
        co=ModbusSequentialDataBlock(0, [0] * 100),
        hr=ModbusSequentialDataBlock(0, [0] * 100),
        ir=ModbusSequentialDataBlock(0, [0] * 100),
        zero_mode=True,
    )
    context = ModbusServerContext(slaves=store, single=True)
    threading.Thread(target=_updater, args=(context,), daemon=True).start()
    StartTcpServer(context=context, address=("0.0.0.0", 502))


if __name__ == "__main__":
    main()
EOF

cat > /opt/ot-sim/plc-sim/opcua_sim.py <<'EOF'
"""OPC UA face of the same simulated plant cell (asyncua), on :4840.

Exposes the SAME four process values the Modbus PLC ticks, under Objects/Plant:
Counter, Temperature, Flow, Running. Temperature is real degrees C here (a float),
not the x10 integer Modbus holding registers carry -- OPC UA is typed, so the demo
should show the typed value rather than repeat a fieldbus workaround.

Anonymous, no security policy: the point of the demo is that the NETWORK PATH is the
control (PRA brokers the only route in), which is exactly how most plant-floor OPC UA
servers are actually deployed.
"""
import asyncio
import math

from asyncua import Server, ua

ENDPOINT = "opc.tcp://0.0.0.0:4840/freeopcua/server/"


async def main():
    server = Server()
    await server.init()
    server.set_endpoint(ENDPOINT)
    server.set_server_name("OT Demo Cell")
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
    idx = await server.register_namespace("http://ot-sim.demo")

    plant = await server.nodes.objects.add_object(idx, "Plant")
    counter = await plant.add_variable(idx, "Counter", 0, ua.VariantType.Int32)
    temperature = await plant.add_variable(idx, "Temperature", 40.0, ua.VariantType.Double)
    flow = await plant.add_variable(idx, "Flow", 120.0, ua.VariantType.Double)
    running = await plant.add_variable(idx, "Running", True, ua.VariantType.Boolean)
    for node in (counter, temperature, flow, running):
        await node.set_writable()

    async with server:
        t = 0
        while True:
            t += 1
            await counter.write_value(ua.Variant(t % 65536, ua.VariantType.Int32))
            await temperature.write_value(
                ua.Variant(40.0 + 2.5 * math.sin(t / 15.0), ua.VariantType.Double))
            await flow.write_value(
                ua.Variant(120.0 + 30.0 * math.sin(t / 7.0), ua.VariantType.Double))
            await running.write_value(ua.Variant(True, ua.VariantType.Boolean))
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
EOF

cat > /opt/ot-sim/plc-sim/enip_sim.py <<'EOF'
"""EtherNet/IP face of the same simulated plant cell (cpppo), on :44818.

cpppo serves a CIP tag table it does not itself drive, so the tags would read a
flat zero forever -- indistinguishable, to anyone demoing, from the crash-loop the
troubleshooting table warns about. This wraps the server with the same one-second
tick the Modbus and OPC UA faces run, writing the four process values through
cpppo's own client so all three protocols tell the same story.

Tags (DINT, matching the Modbus holding registers):
  Counter      increments every second, wraps at 65535
  Temperature  degrees C x10 (sine wave), as on Modbus -- CIP has no unit metadata
  Flow         ~120 +/- 30 (sine wave)
  Running      always 1
"""
import math
import socket
import subprocess
import sys
import time

from cpppo.server.enip import client

ADDRESS = "0.0.0.0:44818"
HOST, PORT = "127.0.0.1", 44818
TAGS = ["Counter", "Temperature", "Flow", "Running"]


def _wait_for_port(seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=2):
                return True
        except OSError:
            time.sleep(1)
    return False


def main():
    server = subprocess.Popen(
        [sys.executable, "-m", "cpppo.server.enip", "--address", ADDRESS]
        + ["%s=DINT[1]" % t for t in TAGS])
    try:
        if not _wait_for_port(60):
            raise RuntimeError("the cpppo EtherNet/IP server never opened %d" % PORT)
        t = 0
        while True:
            if server.poll() is not None:
                raise RuntimeError("the cpppo EtherNet/IP server exited (%s)"
                                   % server.returncode)
            t += 1
            writes = ["Counter=(DINT)%d" % (t % 65536),
                      "Temperature=(DINT)%d" % int(400 + 25 * math.sin(t / 15.0)),
                      "Flow=(DINT)%d" % int(120 + 30 * math.sin(t / 7.0)),
                      "Running=(DINT)1"]
            try:
                with client.connector(host=HOST, port=PORT, timeout=5) as conn:
                    for _ in conn.synchronous(
                            operations=client.parse_operations(writes)):
                        pass
            except Exception as exc:  # noqa: BLE001
                # A single missed tick is not worth killing the container over; a
                # dead server is, and the poll() above catches that.
                print("[enip-sim] tick failed: %s" % exc, file=sys.stderr)
            time.sleep(1)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    main()
EOF

cat > /opt/ot-sim/plc-sim/Dockerfile <<EOF
FROM python:3.12-slim
RUN pip install --no-cache-dir \\
      pymodbus==$OT_PYMODBUS_VERSION \\
      asyncua==$OT_ASYNCUA_VERSION \\
      cpppo==$OT_CPPPO_VERSION
COPY plc_sim.py /app/plc_sim.py
COPY opcua_sim.py /app/opcua_sim.py
COPY enip_sim.py /app/enip_sim.py
EXPOSE 502 4840 44818
CMD ["python", "/app/plc_sim.py"]
EOF

# One image, three entrypoints: the sims share a base layer and a single pip
# install, so a bake pulls python:3.12-slim once and the cell carries one copy.
cat > /opt/ot-sim/docker-compose.yml <<EOF
# OT demo cell -- baked by provisioners/ot/ot-sim-debian.sh. Real compose on the
# VM (volumes allowed), unlike the dashboard's cloud-compose subset.
services:
  plc:
    build: ./plc-sim
    image: ot-plc-sim:baked
    container_name: ot-plc
    restart: unless-stopped
    ports:
      - "502:502"
  hmi:
    image: $OT_FUXA_IMAGE
    container_name: ot-hmi
    restart: unless-stopped
    ports:
      - "1881:1881"
    volumes:
      - fuxa_appdata:/usr/src/app/FUXA/server/_appdata
EOF

OT_SMOKE_CONTAINERS="ot-plc ot-hmi"

if sim_enabled opcua; then
  cat >> /opt/ot-sim/docker-compose.yml <<'EOF'
  opcua:
    image: ot-plc-sim:baked
    container_name: ot-opcua
    restart: unless-stopped
    command: ["python", "/app/opcua_sim.py"]
    ports:
      - "4840:4840"
EOF
  OT_SMOKE_CONTAINERS="$OT_SMOKE_CONTAINERS ot-opcua"
fi

if sim_enabled enip; then
  # enip_sim.py runs cpppo's CIP tag server AND drives it: cpppo serves a tag table
  # it does not itself update, and a flat zero forever is exactly what the
  # troubleshooting table teaches operators to read as a crashed cell.
  cat >> /opt/ot-sim/docker-compose.yml <<'EOF'
  enip:
    image: ot-plc-sim:baked
    container_name: ot-enip
    restart: unless-stopped
    command: ["python", "/app/enip_sim.py"]
    ports:
      - "44818:44818"
EOF
  OT_SMOKE_CONTAINERS="$OT_SMOKE_CONTAINERS ot-enip"
fi

cat >> /opt/ot-sim/docker-compose.yml <<'EOF'
volumes:
  fuxa_appdata:
EOF

log "building the simulators and pre-pulling FUXA ($OT_FUXA_IMAGE)"
docker compose -f /opt/ot-sim/docker-compose.yml build plc
docker compose -f /opt/ot-sim/docker-compose.yml pull hmi

# Smoke-test the stack now, while a failure still fails the BAKE instead of
# producing an image that boots dead in an air-gapped subnet nobody can debug into.
log "smoke-testing the stack ($OT_SMOKE_CONTAINERS)"
docker compose -f /opt/ot-sim/docker-compose.yml up -d
sleep 25
for c in $OT_SMOKE_CONTAINERS; do
  if [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null || echo false)" != "true" ]; then
    docker logs "$c" 2>&1 | tail -n 40 || true
    die "container $c is not running after start - refusing to bake a dead image"
  fi
done

# ── 5b. Seed the FUXA project (best-effort) ──────────────────────────────────
# Without this, every cell costs ~a minute of clicking before it shows anything:
# Connections -> add a ModbusTCP device -> add a tag per holding register. The
# project format is version-coupled to the pinned FUXA, which is why it is not
# shipped as a baked file: instead we ask the RUNNING FUXA for its own project,
# add the device to it, post it back and read it back to confirm it took.
#
# Deliberately NOT fatal. A FUXA whose project API moved leaves the image exactly
# as it was before this step existed -- empty project, wire it by hand -- which is
# strictly better than failing a 15-minute bake over a convenience.
cat > /opt/ot-sim/plc-sim/fuxa_seed.py <<'EOF'
"""Add the cell's PLC connection + register tags to the running FUXA's project.

Read-modify-write against FUXA's own API (GET /api/project -> POST /api/project),
so every key of the project we do not understand survives untouched, and a FUXA
that changed shape fails the round-trip check instead of writing a broken project.

Modbus addressing, per FUXA's modbus driver:
  memaddress "400000" = holding registers, address = 1-BASED offset in that region.
  So holding register 0 (what plc_sim.py ticks first) is address "1".
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:1881"
DEVICE_ID = "ot_sim_plc"
DEVICE_NAME = "PLC"
# (tag id suffix, display name, 1-based holding-register address)
TAGS = [("counter", "Counter", "1"),
        ("temperature", "Temperature", "2"),
        ("flow", "Flow", "3"),
        ("running", "Running", "4")]


def _call(path, payload=None, timeout=20):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=body,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode().strip()
    return json.loads(raw) if raw[:1] in ("{", "[") else {}


def _wait_for_api(seconds):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            return _call("/api/project")
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(3)
    raise RuntimeError("FUXA's project API never answered: %s" % last)


def _device():
    tags = {}
    for suffix, name, address in TAGS:
        tag_id = "%s_%s" % (DEVICE_ID, suffix)
        tags[tag_id] = {
            "id": tag_id, "name": name, "label": name, "value": "",
            "type": "UInt16", "memaddress": "400000", "address": address,
            "divisor": 1, "access": "read", "format": 0, "init": "",
            "options": {}, "daq": {"enabled": False},
            "description": "ot-sim holding register %d" % (int(address) - 1),
        }
    return {
        "id": DEVICE_ID, "name": DEVICE_NAME, "enabled": True, "type": "ModbusTCP",
        "polling": 1000, "tags": tags,
        # `plc` is the compose service name -- resolvable on the cell's own docker
        # network, and never from outside it.
        "property": {"address": "plc", "port": "502", "slaveid": "1", "options": {}},
    }


def main():
    project = _wait_for_api(180)
    if not isinstance(project, dict):
        raise RuntimeError("GET /api/project did not return an object")
    devices = project.get("devices")
    if devices is None:
        devices = {}
        project["devices"] = devices
    if not isinstance(devices, dict):
        raise RuntimeError("project.devices is %s, not a dictionary"
                           % type(devices).__name__)
    for existing in devices.values():
        if isinstance(existing, dict) and existing.get("name") == DEVICE_NAME:
            print("[fuxa-seed] a device named %s already exists - leaving it alone"
                  % DEVICE_NAME)
            return 0

    devices[DEVICE_ID] = _device()
    _call("/api/project", project, timeout=60)
    # POST /api/project restarts the FUXA runtime, so the read-back has to wait for
    # it to come up again rather than racing the restart.
    time.sleep(5)
    back = _wait_for_api(120)
    seeded = (back.get("devices") or {}).get(DEVICE_ID) or {}
    if seeded.get("name") != DEVICE_NAME:
        raise RuntimeError("device %s is absent from the project FUXA read back"
                           % DEVICE_ID)
    if len(seeded.get("tags") or {}) != len(TAGS):
        raise RuntimeError("device %s came back with %d tags, expected %d"
                           % (DEVICE_ID, len(seeded.get("tags") or {}), len(TAGS)))
    print("[fuxa-seed] seeded %s with %d tags" % (DEVICE_NAME, len(TAGS)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print("[fuxa-seed] NOT seeded: %s" % exc, file=sys.stderr)
        sys.exit(1)
EOF

log "seeding the FUXA project (device + holding-register tags)"
if docker run --rm --network host \
     -v /opt/ot-sim/plc-sim/fuxa_seed.py:/seed.py:ro \
     ot-plc-sim:baked python /seed.py; then
  log "FUXA project seeded - the cell opens on a wired PLC connection"
else
  log "WARNING: FUXA project NOT seeded (see the error above). The image is still"
  log "         good: wire the connection by hand once per cell, as described in"
  log "         provisioners/ot/README.md (FUXA project seeding)."
fi

docker compose -f /opt/ot-sim/docker-compose.yml down

log "installing the ot-sim systemd unit"
cat > /etc/systemd/system/ot-sim.service <<'EOF'
[Unit]
Description=OT demo cell (Modbus PLC simulator + FUXA HMI)
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/docker compose -f /opt/ot-sim/docker-compose.yml up -d
ExecStop=/usr/bin/docker compose -f /opt/ot-sim/docker-compose.yml down

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable ot-sim.service

# ── 6. Image cleanup for re-launch ───────────────────────────────────────────
if [ "${OT_SKIP_CLEANUP:-0}" = "1" ]; then
  log "OT_SKIP_CLEANUP=1 — leaving host keys, machine-id, and logs in place"
else
  log "cleaning ssh host keys, machine-id, cloud-init state, logs"
  rm -f /etc/ssh/ssh_host_*
  truncate -s 0 /etc/machine-id
  if [ -d /var/lib/dbus ]; then
    rm -f /var/lib/dbus/machine-id
    ln -sf /etc/machine-id /var/lib/dbus/machine-id
  fi
  rm -rf /var/lib/cloud/instances /var/lib/cloud/instance
  find /var/log -type f -name 'cloud-init*.log' -exec truncate -s 0 {} + 2>/dev/null || true
  apt-get -y -q clean
fi

log "ot-sim bake complete — sims [$OT_SIMS], FUXA HMI on :1881, PS account '$OT_ADMIN_USER'"
