#!/bin/sh
# ot-sim-debian.sh — bake a self-contained OT/ICS demo cell into a Debian-family image.
#
# What the built image runs at boot (systemd unit `ot-sim`, docker compose):
#   plc  — a Modbus TCP "PLC" simulator on :502 (pymodbus, BUILT at bake time from
#          python:3.12-slim) whose holding registers tick every second: a counter,
#          a sine-wave temperature (x10 °C) and flow value, and a run flag — so a
#          Modbus client through a PRA Protocol Tunnel shows LIVE process data.
#   hmi  — the FUXA web SCADA/HMI on :1881 (frangoteam/fuxa, pinned version tag),
#          reached via a PRA Web Jump. Its project data persists in a named volume.
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
case "$OT_FUXA_IMAGE" in
  *:latest) die "OT_FUXA_IMAGE must be pinned to a version tag, not :latest" ;;
  *:*) ;;
  *) die "OT_FUXA_IMAGE must carry an explicit version tag (got '$OT_FUXA_IMAGE')" ;;
esac

log "writing /opt/ot-sim (compose stack + PLC simulator source)"
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

cat > /opt/ot-sim/plc-sim/Dockerfile <<EOF
FROM python:3.12-slim
RUN pip install --no-cache-dir pymodbus==$OT_PYMODBUS_VERSION
COPY plc_sim.py /app/plc_sim.py
EXPOSE 502
CMD ["python", "/app/plc_sim.py"]
EOF

cat > /opt/ot-sim/docker-compose.yml <<EOF
# OT demo cell — baked by provisioners/ot/ot-sim-debian.sh. Real compose on the
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
volumes:
  fuxa_appdata:
EOF

log "building the PLC simulator and pre-pulling FUXA ($OT_FUXA_IMAGE)"
docker compose -f /opt/ot-sim/docker-compose.yml build plc
docker compose -f /opt/ot-sim/docker-compose.yml pull hmi

# Smoke-test the stack now, while a failure still fails the BAKE instead of
# producing an image that boots dead in an air-gapped subnet nobody can debug into.
log "smoke-testing the stack"
docker compose -f /opt/ot-sim/docker-compose.yml up -d
sleep 25
for c in ot-plc ot-hmi; do
  if [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null || echo false)" != "true" ]; then
    docker logs "$c" 2>&1 | tail -n 40 || true
    die "container $c is not running after start — refusing to bake a dead image"
  fi
done
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

log "ot-sim bake complete — PLC sim on :502, FUXA HMI on :1881, PS account '$OT_ADMIN_USER'"
