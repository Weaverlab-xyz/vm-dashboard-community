#!/bin/sh
# ot-sim-debian.sh — bake a self-contained OT/ICS demo cell into a Debian-family image.
#
# The cell is a plant IPC, so it runs what a plant IPC can run: KubeSolo, the
# single-node Kubernetes this repo already recommends for OT hosts too small for a
# real cluster (docs/kubesolo.md). The simulators below are its workloads — same
# images, same ports, same PRA wiring as when they ran on docker compose, which is
# still available as OT_RUNTIME=docker.
#
# The same script also bakes the plant's DMZ broker (OT_ROLE=broker): KubeSolo and the
# Entitle agent's chart, no simulators. That host is what makes the identity half of
# the demo honest — the agent runs inside the plant, and it is the only machine there
# with a way out.
#
# What the built image runs at boot (systemd unit `ot-sim`):
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
# The four sims share ONE image and one pip install; OT_SIMS picks which run. Only
# DNP3 is absent: opendnp3 needs a native library built from source, so that preset
# stays real-gear-only (see README.md).
#
# Everything is pulled/built AT BAKE TIME (the Packer build VM has egress); the
# deployed VM needs ZERO outbound internet, so the cell runs in the sandbox's
# air-gapped private subnet — that egress-less subnet IS the "plant network" story.
#
# Self-elevates to root via sudo -E (Packer invokes the shell provisioner as the
# cloud-default user). POSIX sh only — no [[ ]], no arrays, no <<<.
#
# Operator-overridable via Packer build env:
#   OT_ROLE          which machine this image is: cell (default) or broker. `cell` is
#                    the plant floor — the simulators and the HMI. `broker` is the
#                    plant's industrial DMZ host: the same KubeSolo carrying the
#                    BeyondTrust Entitle agent and nothing else, so the thing that
#                    brokers access to plant resources sits IN the plant. Bake one of
#                    each; the role cannot be chosen at deploy time, because the VM
#                    deploy paths have no user-data hook.
#   OT_ENTITLE_CHART_VERSION  broker only: the Entitle agent chart version to bake
#                    (default: whatever the repo calls latest at bake time — the
#                    resolved version is written to /opt/entitle/charts/CHART.txt).
#                    Also OT_ENTITLE_CHART_REPO / OT_ENTITLE_CHART.
#   OT_PROBE_IMAGE   broker only: the image the agent install's egress probe runs as a
#                    pod (default busybox:1.36, pulled at bake time)
#   OT_RUNTIME       what runs the workloads: kubesolo (default) or docker. KubeSolo
#                    makes the cell a single-node Kubernetes host — Docker is then a
#                    BUILD-time dependency only and is purged before KubeSolo goes on
#                    (its installer refuses a host that still has Docker).
#   OT_KUBESOLO_VERSION  KubeSolo release to bake (default: v1.2.0). It must have an
#                    -offline build: that variant carries every image KubeSolo itself
#                    needs inside the binary, which is what lets the cell boot with no
#                    egress at all.
#   OT_HELM_VERSION      helm to put on the host (default: v3.16.3 — the pin the
#                    KubeSolo plays in examples/playbooks/kubesolo/ use)
#   OT_KUBECTL_VERSION   kubectl to put on the host (default: whatever dl.k8s.io calls
#                    stable at bake time). KubeSolo ships neither client.
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

# What runs the cell's workloads. `kubesolo` is the default because the cell is the
# only plant floor this repo ships: KubeSolo is offered to OT customers as the way to
# carry Kubernetes on plant hardware, and a demo cell running docker compose could
# never show it. `docker` keeps the previous compose stack, unchanged — the fallback
# if a KubeSolo bake ever fails on a platform this script has not met.
OT_RUNTIME="$(echo "${OT_RUNTIME:-kubesolo}" | tr '[:upper:]' '[:lower:]')"
case "$OT_RUNTIME" in
  kubesolo|docker) ;;
  *) die "OT_RUNTIME must be 'kubesolo' or 'docker' (got '$OT_RUNTIME')" ;;
esac

# Which machine this image is. `cell` is the plant floor: the simulators and the HMI.
# `broker` is the plant's industrial DMZ host — the same KubeSolo, carrying the
# BeyondTrust Entitle agent and nothing else, because a DMZ host that answers Modbus
# is a lie about where it sits. The two are separate images because there is no
# user-data hook on the deploy paths, so the role cannot be chosen at launch.
OT_ROLE="$(echo "${OT_ROLE:-cell}" | tr '[:upper:]' '[:lower:]')"
case "$OT_ROLE" in
  cell) ;;
  broker)
    if [ "$OT_RUNTIME" = "docker" ]; then
      die "OT_ROLE=broker has no docker runtime — the broker exists to run the Entitle \
agent's Helm chart, which needs Kubernetes"
    fi ;;
  *) die "OT_ROLE must be 'cell' or 'broker' (got '$OT_ROLE')" ;;
esac

# ── KubeSolo, its clients, and the Entitle chart (both roles) ────────────────
OT_KUBESOLO_VERSION="${OT_KUBESOLO_VERSION:-v1.2.0}"
OT_HELM_VERSION="${OT_HELM_VERSION:-v3.16.3}"
OT_KUBECTL_VERSION="${OT_KUBECTL_VERSION:-}"
KUBESOLO_PATH=/var/lib/kubesolo
KUBESOLO_KUBECONFIG=$KUBESOLO_PATH/pki/admin/admin.kubeconfig
KUBESOLO_SOCK=$KUBESOLO_PATH/containerd/containerd.sock
OT_ARCH="$(dpkg --print-architecture)"
# Broker only: the Entitle agent chart and the probe image, baked so neither the
# Helm repo nor Docker Hub has to be reachable from a plant DMZ at run time.
OT_ENTITLE_CHART_REPO="${OT_ENTITLE_CHART_REPO:-https://anycred.github.io/entitle-charts/}"
OT_ENTITLE_CHART="${OT_ENTITLE_CHART:-entitle-agent}"
OT_ENTITLE_CHART_VERSION="${OT_ENTITLE_CHART_VERSION:-}"
OT_ENTITLE_CHART_DIR=/opt/entitle/charts
# busybox, pinned: nc and nslookup are all the egress probe needs, and the probe has
# to run as a POD — proving the plant boundary from where the agent will sit, not
# from the host, which is a different source address and a different answer.
OT_PROBE_IMAGE="${OT_PROBE_IMAGE:-busybox:1.36}"

install_k8s_clients() {
  log "installing kubectl and helm — KubeSolo ships neither, and the plays expect both"
  if [ -z "$OT_KUBECTL_VERSION" ]; then
    OT_KUBECTL_VERSION="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"
  fi
  curl -fsSLo /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/$OT_KUBECTL_VERSION/bin/linux/$OT_ARCH/kubectl"
  chmod 0755 /usr/local/bin/kubectl
  curl -fsSL "https://get.helm.sh/helm-$OT_HELM_VERSION-linux-$OT_ARCH.tar.gz" -o /tmp/helm.tar.gz
  tar -xzf /tmp/helm.tar.gz -C /tmp
  install -m 0755 "/tmp/linux-$OT_ARCH/helm" /usr/local/bin/helm
  rm -rf /tmp/helm.tar.gz "/tmp/linux-$OT_ARCH"
}

install_kubesolo() {
  # The -offline build, not the default one: it carries CoreDNS, the CNI plugins and
  # the rest of what KubeSolo starts INSIDE the binary. The default build pulls them
  # from a registry at first start, which in an egress-less subnet means a cluster
  # that never comes up — and the bake would not notice, because the BUILD VM has
  # egress.
  log "installing KubeSolo $OT_KUBESOLO_VERSION (-offline build: its images ride in the binary)"
  # Downloaded, then run — not piped: sh in a pipeline reports ITS status, so a failed
  # download would silently install nothing and only surface five minutes later as
  # "KubeSolo never wrote its kubeconfig".
  curl -sfL https://get.kubesolo.io -o /tmp/kubesolo-install.sh \
    || die "could not download the KubeSolo installer from get.kubesolo.io"
  KUBESOLO_VERSION="$OT_KUBESOLO_VERSION" KUBESOLO_OFFLINE=true \
    KUBESOLO_PATH="$KUBESOLO_PATH" sh /tmp/kubesolo-install.sh \
    || die "the KubeSolo installer failed — its own output is above; \
a release with no -offline build is the usual cause"
  rm -f /tmp/kubesolo-install.sh

  export KUBECONFIG="$KUBESOLO_KUBECONFIG"
  log "waiting for the KubeSolo API and a Ready node"
  _waited=0
  while [ ! -f "$KUBECONFIG" ]; do
    _waited=$((_waited + 1))
    if [ "$_waited" -gt 60 ]; then
      journalctl -u kubesolo --no-pager -n 40 2>/dev/null || true
      die "KubeSolo never wrote $KUBECONFIG"
    fi
    sleep 5
  done
  kubectl wait --for=condition=Ready node --all --timeout=300s \
    || die "the KubeSolo node never became Ready (journalctl -u kubesolo)"
}

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

# ── 4. Base packages, and — for the cell only — Docker ──────────────────────
# python3 is for both roles: the FUXA project seed and the bake's port probes run on
# the host now, so they cannot depend on a container being up.
log "installing base packages"
apt-get -y -q install ca-certificates curl gnupg python3

# Docker builds the simulators and pulls FUXA, and on the KubeSolo runtime section 5b
# then purges it, because KubeSolo's installer refuses a host that still has Docker on
# it. The broker builds nothing, so it never installs Docker in the first place — and
# therefore has nothing to purge before that check runs.
if [ "$OT_ROLE" = "cell" ]; then
log "installing Docker Engine + compose plugin"
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
fi

if [ "$OT_ROLE" = "cell" ]; then

# ── 5. The OT sim stack (built + pre-pulled NOW, so runtime needs no egress) ──
OT_FUXA_IMAGE="${OT_FUXA_IMAGE:-frangoteam/fuxa:1.3.4}"
OT_PYMODBUS_VERSION="${OT_PYMODBUS_VERSION:-3.6.8}"
OT_ASYNCUA_VERSION="${OT_ASYNCUA_VERSION:-1.1.5}"
# 5.2.5, not the 4.x series: cpppo <5 rewrites code objects at import and dies on
# Python 3.11+ with "code() argument 13 must be str", which on python:3.12-slim is
# every bake.
OT_CPPPO_VERSION="${OT_CPPPO_VERSION:-5.2.5}"
# 3.x, not 2.x: python-snap7 implemented its S7 SERVER in pure Python from 3.0, so
# the S7 sim needs no libsnap7. Below that the server is a ctypes binding to a
# native library Debian does not package, which is what kept Siemens off this image.
OT_SNAP7_VERSION="${OT_SNAP7_VERSION:-3.1.2}"
# Which protocol servers the cell answers. The PRA tunnel presets cover five
# protocols; these are the four that simulate honestly from a pure-python,
# no-egress-at-runtime base. Only DNP3 (opendnp3) still needs a native library
# built from source, so that preset stays real-gear-only — see
# provisioners/ot/README.md.
OT_SIMS="${OT_SIMS:-modbus,opcua,enip,s7}"
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
    modbus|opcua|enip|s7) ;;
    *) die "OT_SIMS names an unknown simulator '$_s' (known: modbus, opcua, enip, s7)" ;;
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

cat > /opt/ot-sim/plc-sim/s7_sim.py <<'EOF'
"""Tiny Siemens S7comm "PLC" for OT demos — DB1 words tick so reads show life.

DB1, big-endian words. Read with python-snap7 (``db_read(1, 0, 8)``), or in FUXA
add an S7 device at address `s7` port 102:
  offset 0  counter      increments every second, wraps at 65535
  offset 2  temperature  ~400 +/- 25, i.e. degrees C x10 (sine wave)
  offset 4  flow         ~120 +/- 30 (sine wave)
  offset 6  running      always 1

Deliberately the SAME four values as the Modbus / OPC UA / EtherNet-IP sims, so one
demo script reads the same "plant" through any vendor's protocol.

python-snap7 >= 3.0 implements the S7 server in PURE PYTHON — no libsnap7 — and
``register_area`` keeps a reference to the bytearray below rather than copying it,
so the updater thread mutating it in place is exactly what an S7 client reads back.
``start()`` binds :102 on its own daemon thread and returns, so main() must park.
"""
import math
import struct
import threading
import time

import snap7
from snap7.server import Server

DB_NUMBER = 1
DB_SIZE = 8

_db = bytearray(DB_SIZE)


def _updater():
    t = 0
    while True:
        t += 1
        struct.pack_into(
            ">HHHH", _db, 0,
            t % 65536,
            int(400 + 25 * math.sin(t / 15.0)),
            int(120 + 30 * math.sin(t / 7.0)),
            1,
        )
        time.sleep(1)


def main():
    server = Server()
    server.register_area(snap7.SrvArea.DB, DB_NUMBER, _db)
    threading.Thread(target=_updater, daemon=True).start()
    server.start(tcp_port=102)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
EOF

cat > /opt/ot-sim/plc-sim/Dockerfile <<EOF
FROM python:3.12-slim
RUN pip install --no-cache-dir \\
      pymodbus==$OT_PYMODBUS_VERSION \\
      asyncua==$OT_ASYNCUA_VERSION \\
      cpppo==$OT_CPPPO_VERSION \\
      python-snap7==$OT_SNAP7_VERSION
COPY plc_sim.py /app/plc_sim.py
COPY opcua_sim.py /app/opcua_sim.py
COPY enip_sim.py /app/enip_sim.py
COPY s7_sim.py /app/s7_sim.py
EXPOSE 502 4840 44818 102
CMD ["python", "/app/plc_sim.py"]
EOF

# One image, four entrypoints: the sims share a base layer and a single pip
# install, so a bake pulls python:3.12-slim once and the cell carries one copy.
# Built here, before the runtime split below, because both runtimes run the same
# images — the KubeSolo one just carries them as tarballs instead of in Docker's
# store.
log "building the simulators and pre-pulling FUXA ($OT_FUXA_IMAGE)"
docker build -t ot-plc-sim:baked /opt/ot-sim/plc-sim
docker pull "$OT_FUXA_IMAGE"

# What has to answer once the stack is up, whichever runtime carries it. The docker
# runtime additionally asserts every container is running; the KubeSolo one waits on
# the rollouts. Both then prove the listener, because "the container is up" has never
# been the same claim as "the PLC answers".
OT_SMOKE_PORTS="502 1881"
if sim_enabled opcua; then OT_SMOKE_PORTS="$OT_SMOKE_PORTS 4840"; fi
if sim_enabled enip; then OT_SMOKE_PORTS="$OT_SMOKE_PORTS 44818"; fi
if sim_enabled s7; then OT_SMOKE_PORTS="$OT_SMOKE_PORTS 102"; fi

# Where FUXA dials the Modbus PLC: a compose service name on docker, the node's own
# loopback on KubeSolo (the pods share the node's network namespace — see 5b).
FUXA_PLC_ADDRESS=plc

if [ "$OT_RUNTIME" = "docker" ]; then

# ── 5a. Runtime: docker compose (the pre-KubeSolo stack, kept as the fallback) ─
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

if sim_enabled s7; then
  # :102 is privileged, which is fine inside the container (it runs as root). The
  # pure-python server logs a COTP framing warning on some client handshakes; reads
  # are unaffected, so it must not be mistaken for a failure.
  cat >> /opt/ot-sim/docker-compose.yml <<'EOF'
  s7:
    image: ot-plc-sim:baked
    container_name: ot-s7
    restart: unless-stopped
    command: ["python", "/app/s7_sim.py"]
    ports:
      - "102:102"
EOF
  OT_SMOKE_CONTAINERS="$OT_SMOKE_CONTAINERS ot-s7"
fi

cat >> /opt/ot-sim/docker-compose.yml <<'EOF'
volumes:
  fuxa_appdata:
EOF

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

else

# ── 5b. Runtime: KubeSolo — the cell IS a single-node Kubernetes host ─────────
# Why the demo cell runs Kubernetes at all: KubeSolo is the answer this repo gives an
# OT customer who cannot put a cluster on the plant floor (docs/kubesolo.md), and the
# cell is the only plant floor it ships. On docker compose that answer was a slide.
# The simulators do not change — same images, same ports, same PRA wiring — they just
# become the workloads of a cluster that also has room for the Entitle agent.
#
# Docker cannot stay. KubeSolo's installer refuses a host with docker on PATH, a
# docker.sock, or an active docker service: it brings its own containerd and CNI, and
# two of those arguing over iptables is precisely the failure that only shows up in
# front of a customer. So the order is: build with Docker, export, purge Docker,
# install KubeSolo, import into ITS containerd.
OT_IMAGE_DIR=/var/lib/ot-sim/images

# The pods run with hostNetwork (see the manifest), so FUXA reaches the PLC on the
# node's own loopback rather than by a compose service name.
FUXA_PLC_ADDRESS=127.0.0.1

log "exporting the built images — the cell has no registry to pull them back from"
mkdir -p "$OT_IMAGE_DIR" /var/lib/ot-sim/fuxa
docker save ot-plc-sim:baked -o "$OT_IMAGE_DIR/ot-plc-sim.tar"
docker save "$OT_FUXA_IMAGE" -o "$OT_IMAGE_DIR/fuxa.tar"

# FUXA's project data is a hostPath here, not a named volume, and nothing copies the
# image's own _appdata into an empty hostPath the way docker does for a fresh volume.
# Do it now, while Docker can still read the image — otherwise FUXA starts against an
# empty directory and the seed below has no project to add a device to.
log "priming FUXA's appdata directory from the image"
if docker create --name ot-fuxa-appdata "$OT_FUXA_IMAGE" >/dev/null 2>&1; then
  docker cp "ot-fuxa-appdata:/usr/src/app/FUXA/server/_appdata/." /var/lib/ot-sim/fuxa/ \
    || log "WARNING: could not copy FUXA's appdata out of the image — it starts empty"
  docker rm ot-fuxa-appdata >/dev/null 2>&1 || true
fi
# The cell is a single-purpose appliance and this directory is its own state, so a
# permissive mode beats guessing which UID the pinned FUXA image runs as.
chmod 0777 /var/lib/ot-sim/fuxa

# containerd's own client, kept because it is how an image gets into a containerd that
# has no registry behind it. A CLI, not an engine — KubeSolo's prerequisite check is
# about Docker, and /usr/local/bin survives the purge because apt does not own it.
if [ ! -x /usr/bin/ctr ]; then
  die "containerd.io did not provide /usr/bin/ctr — without it the baked images cannot \
be loaded into KubeSolo, and the cell has no registry to pull them from"
fi
install -m 0755 /usr/bin/ctr /usr/local/bin/ctr

log "removing Docker — KubeSolo's installer refuses a host that still carries it"
systemctl disable --now docker.service docker.socket containerd.service >/dev/null 2>&1 || true
apt-get -y -q purge docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
apt-get -y -q autoremove
rm -rf /var/lib/docker /var/lib/containerd /etc/docker /var/run/docker.sock
rm -f /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.asc
ip link delete docker0 >/dev/null 2>&1 || true
if command -v docker >/dev/null 2>&1; then
  die "docker is still on PATH after the purge — KubeSolo's installer would refuse this host"
fi
apt-get update -q
# Docker pulled iptables in as a dependency, so autoremove above may have taken it
# with it — and kube-proxy needs it. Ask for it by name, so it is a manual package
# nothing can reclaim.
apt-get -y -q install iptables
# Docker's chains and its FORWARD policy of DROP live in the running kernel, not in
# the packages, so they would outlive the purge until a reboot the bake never does —
# and the first thing they break is KubeSolo's pod networking, starting with CoreDNS.
iptables -P FORWARD ACCEPT >/dev/null 2>&1 || true
for _table in filter nat mangle; do
  iptables -t "$_table" -F >/dev/null 2>&1 || true
  iptables -t "$_table" -X >/dev/null 2>&1 || true
done

install_k8s_clients
install_kubesolo

# containerd normalises a bare image name to docker.io/library/<name> and a
# single-slash name to docker.io/<name>; a name that already carries a registry host
# is stored verbatim. Recording what it ends up called is what lets the boot-time
# importer tell "already loaded" from "must import" without re-deriving these rules.
normalize_ref() {
  case "$1" in
    */*/*) echo "$1" ;;
    */*)
      case "${1%%/*}" in
        *.*|*:*|localhost) echo "$1" ;;
        *) echo "docker.io/$1" ;;
      esac ;;
    *) echo "docker.io/library/$1" ;;
  esac
}

log "importing the baked images into KubeSolo's containerd"
: > "$OT_IMAGE_DIR/images.txt"
for _pair in "ot-plc-sim:baked|ot-plc-sim.tar" "$OT_FUXA_IMAGE|fuxa.tar"; do
  _ref="$(normalize_ref "${_pair%%|*}")"
  _tarball="${_pair##*|}"
  ctr --address "$KUBESOLO_SOCK" --namespace k8s.io images import "$OT_IMAGE_DIR/$_tarball" \
    || die "ctr could not import $_tarball into KubeSolo's containerd"
  # Proven here rather than assumed: the manifests pull nothing (imagePullPolicy:
  # Never), so a name containerd does not hold is a cell that boots ErrImageNeverPull.
  if ! ctr --address "$KUBESOLO_SOCK" --namespace k8s.io images ls -q | grep -qx "$_ref"; then
    die "$_tarball imported but containerd does not list $_ref — the pods would never start"
  fi
  echo "$_ref $_tarball" >> "$OT_IMAGE_DIR/images.txt"
done

log "writing /opt/ot-sim/kubesolo (the plant workloads, as KubeSolo runs them)"
mkdir -p /opt/ot-sim/kubesolo
cat > /opt/ot-sim/kubesolo/ot-sim.yaml <<EOF
# The OT demo cell's plant workloads -- baked by provisioners/ot/ot-sim-debian.sh.
# Same images and same ports as the docker runtime's compose stack.
#
# hostNetwork, and no Service: a PLC answers on the machine's own address, which is
# also what the PRA protocol tunnels dial (the Gateway reaches <cell ip>:502, never a
# cluster IP). It also keeps the fieldbus ports off the CNI's portmap path, so the
# cell cannot boot with Kubernetes healthy and the plant unreachable.
#
# imagePullPolicy: Never, because there is no registry and no egress: the images come
# from the tarballs in /var/lib/ot-sim/images, loaded by apply.sh. A missing one then
# fails as ErrImageNeverPull -- "it is not in the local store" -- instead of an
# ImagePullBackOff that reads like a blocked firewall.
#
# strategy: Recreate everywhere: a rolling update would start the new pod while the
# old one still holds the host port, and the rollout would wedge.
apiVersion: v1
kind: Namespace
metadata:
  name: ot-sim
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ot-plc
  namespace: ot-sim
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: ot-plc
  template:
    metadata:
      labels:
        app: ot-plc
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      containers:
        - name: plc
          image: ot-plc-sim:baked
          imagePullPolicy: Never
          command: ["python", "/app/plc_sim.py"]
          ports:
            - containerPort: 502
              hostPort: 502
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ot-hmi
  namespace: ot-sim
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: ot-hmi
  template:
    metadata:
      labels:
        app: ot-hmi
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      containers:
        - name: hmi
          image: $OT_FUXA_IMAGE
          imagePullPolicy: Never
          ports:
            - containerPort: 1881
              hostPort: 1881
          volumeMounts:
            - name: appdata
              mountPath: /usr/src/app/FUXA/server/_appdata
      volumes:
        - name: appdata
          hostPath:
            path: /var/lib/ot-sim/fuxa
            type: DirectoryOrCreate
EOF

OT_SMOKE_DEPLOYS="ot-plc ot-hmi"

if sim_enabled opcua; then
  cat >> /opt/ot-sim/kubesolo/ot-sim.yaml <<'EOF'
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ot-opcua
  namespace: ot-sim
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: ot-opcua
  template:
    metadata:
      labels:
        app: ot-opcua
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      containers:
        - name: opcua
          image: ot-plc-sim:baked
          imagePullPolicy: Never
          command: ["python", "/app/opcua_sim.py"]
          ports:
            - containerPort: 4840
              hostPort: 4840
EOF
  OT_SMOKE_DEPLOYS="$OT_SMOKE_DEPLOYS ot-opcua"
fi

if sim_enabled enip; then
  cat >> /opt/ot-sim/kubesolo/ot-sim.yaml <<'EOF'
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ot-enip
  namespace: ot-sim
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: ot-enip
  template:
    metadata:
      labels:
        app: ot-enip
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      containers:
        - name: enip
          image: ot-plc-sim:baked
          imagePullPolicy: Never
          command: ["python", "/app/enip_sim.py"]
          ports:
            - containerPort: 44818
              hostPort: 44818
EOF
  OT_SMOKE_DEPLOYS="$OT_SMOKE_DEPLOYS ot-enip"
fi

if sim_enabled s7; then
  # :102 is privileged. The container runs as root and Kubernetes leaves
  # CAP_NET_BIND_SERVICE in the default set, so the bind succeeds exactly as it did
  # under docker. The pure-python server logs a COTP framing warning on some client
  # handshakes; reads are unaffected, so it must not be read as a failure.
  cat >> /opt/ot-sim/kubesolo/ot-sim.yaml <<'EOF'
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ot-s7
  namespace: ot-sim
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: ot-s7
  template:
    metadata:
      labels:
        app: ot-s7
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      containers:
        - name: s7
          image: ot-plc-sim:baked
          imagePullPolicy: Never
          command: ["python", "/app/s7_sim.py"]
          ports:
            - containerPort: 102
              hostPort: 102
EOF
  OT_SMOKE_DEPLOYS="$OT_SMOKE_DEPLOYS ot-s7"
fi

cat > /opt/ot-sim/kubesolo/apply.sh <<'EOF'
#!/bin/sh
# Bring the OT demo cell's plant workloads up on KubeSolo. Baked by
# provisioners/ot/ot-sim-debian.sh, run at boot by ot-sim.service, and safe to re-run
# by hand: kubectl apply is declarative and an image already in the store is skipped.
set -eu

KUBESOLO_PATH=/var/lib/kubesolo
KUBECONFIG=$KUBESOLO_PATH/pki/admin/admin.kubeconfig
export KUBECONFIG
IMAGE_DIR=/var/lib/ot-sim/images
CTR="ctr --address $KUBESOLO_PATH/containerd/containerd.sock --namespace k8s.io"

log() { echo "[ot-sim] $*"; }

# 1. Wait for the API. The bake drops the cluster's identity so that every cell mints
#    its own CA, node and state on first boot -- so this is a real wait, not a
#    formality, and it is the step that takes the time on a cell's first start.
tries=0
while [ ! -f "$KUBECONFIG" ] || ! kubectl get --raw /readyz >/dev/null 2>&1; do
  tries=$((tries + 1))
  if [ "$tries" -gt 120 ]; then
    echo "[ot-sim] ERROR: KubeSolo's API never came up (journalctl -u kubesolo)" >&2
    exit 1
  fi
  sleep 5
done
kubectl wait --for=condition=Ready node --all --timeout=300s >/dev/null

# 2. Load the images. There is no registry inside the plant network: these tarballs
#    ARE the image source, which is why the manifest sets imagePullPolicy: Never.
while read -r ref tarball; do
  [ -n "${ref:-}" ] || continue
  if $CTR images ls -q | grep -qx "$ref"; then continue; fi
  log "importing $ref"
  # </dev/null so the import cannot consume the image list this loop is reading.
  $CTR images import "$IMAGE_DIR/$tarball" </dev/null
done < "$IMAGE_DIR/images.txt"

# 3. Apply, then wait on each Deployment, so a boot that only half-worked says so in
#    `systemctl status ot-sim` instead of in front of a customer.
kubectl apply -f /opt/ot-sim/kubesolo/ot-sim.yaml
for deploy in $(kubectl -n ot-sim get deploy -o name); do
  kubectl -n ot-sim rollout status "$deploy" --timeout=300s
done

# 4. A kubeconfig that works through the PRA protocol tunnel. The tunnel listens on
#    127.0.0.1:6443 on the REP's machine, while the API server's certificate is issued
#    to this node -- so the name to verify against has to travel with the file rather
#    than be something the rep is expected to know.
node_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -n "$node_ip" ] && [ -f "$KUBECONFIG" ]; then
  KUBECONFIG_SRC="$KUBECONFIG" NODE_IP="$node_ip" python3 - \
    > /var/lib/ot-sim/kubeconfig-via-tunnel.yaml <<'PY'
import os
import re

src = open(os.environ["KUBECONFIG_SRC"], encoding="utf-8").read()
print(re.sub(r"(?m)^(\s*)server: https://.*$",
             lambda m: "%sserver: https://127.0.0.1:6443\n%stls-server-name: %s"
                       % (m.group(1), m.group(1), os.environ["NODE_IP"]),
             src), end="")
PY
  chmod 0600 /var/lib/ot-sim/kubeconfig-via-tunnel.yaml
fi

log "the plant workloads are up (kubectl -n ot-sim get pods)"
EOF
chmod 0755 /opt/ot-sim/kubesolo/apply.sh

log "starting the plant workloads on KubeSolo ($OT_SMOKE_DEPLOYS)"
/opt/ot-sim/kubesolo/apply.sh || die "the workloads did not come up on KubeSolo — \
refusing to bake an image whose plant is dead"

# A Ready rollout still is not a listener: the same distinction verify_tunnels.py
# draws for the rep is drawn here, while a failure can still fail the BAKE.
wait_for_port() {
  _tries=0
  while [ "$_tries" -lt 60 ]; do
    if python3 -c "import socket, sys; s = socket.socket(); s.settimeout(2); \
sys.exit(0 if s.connect_ex(('127.0.0.1', $1)) == 0 else 1)"; then
      return 0
    fi
    _tries=$((_tries + 1))
    sleep 5
  done
  return 1
}

log "smoke-testing the stack (ports: $OT_SMOKE_PORTS)"
for _port in $OT_SMOKE_PORTS; do
  if ! wait_for_port "$_port"; then
    kubectl -n ot-sim get pods -o wide || true
    for _d in $OT_SMOKE_DEPLOYS; do
      kubectl -n ot-sim logs "deploy/$_d" --tail=20 2>&1 | tail -n 20 || true
    done
    die "nothing answers on :$_port - refusing to bake a dead image"
  fi
done

fi

# ── 5c. Seed the FUXA project (best-effort) ──────────────────────────────────
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

The PLC's address comes from FUXA_PLC_ADDRESS, because the two runtimes reach it
differently: `plc` is the compose service name on docker, and 127.0.0.1 on KubeSolo,
where the pods share the node's network namespace.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:1881"
DEVICE_ID = "ot_sim_plc"
DEVICE_NAME = "PLC"
PLC_ADDRESS = os.environ.get("FUXA_PLC_ADDRESS") or "plc"
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
        # Reachable only from inside the cell either way: a compose service name on
        # the cell's own docker network, or the node's loopback under KubeSolo.
        "property": {"address": PLC_ADDRESS, "port": "502", "slaveid": "1",
                     "options": {}},
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

# Run on the HOST with stdlib python3, not inside a container: FUXA answers on the
# node's :1881 under either runtime, and the KubeSolo one has no docker left to run
# the seed with.
log "seeding the FUXA project (device + holding-register tags, PLC at $FUXA_PLC_ADDRESS)"
if FUXA_PLC_ADDRESS="$FUXA_PLC_ADDRESS" python3 /opt/ot-sim/plc-sim/fuxa_seed.py; then
  log "FUXA project seeded - the cell opens on a wired PLC connection"
else
  log "WARNING: FUXA project NOT seeded (see the error above). The image is still"
  log "         good: wire the connection by hand once per cell, as described in"
  log "         provisioners/ot/README.md (FUXA project seeding)."
fi

if [ "$OT_RUNTIME" = "docker" ]; then
  docker compose -f /opt/ot-sim/docker-compose.yml down
fi

log "installing the ot-sim systemd unit ($OT_RUNTIME runtime)"
if [ "$OT_RUNTIME" = "docker" ]; then
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
else
cat > /etc/systemd/system/ot-sim.service <<'EOF'
[Unit]
Description=OT demo cell workloads on KubeSolo (PLC simulators + FUXA HMI)
After=kubesolo.service
Requires=kubesolo.service

[Service]
Type=oneshot
RemainAfterExit=yes
# A first boot mints the cluster's CA and node and loads ~a gigabyte of baked image
# tarballs, because there is no registry to pull from; the default 90s start timeout
# would kill that half way through and leave the plant dark.
TimeoutStartSec=1200
ExecStart=/opt/ot-sim/kubesolo/apply.sh

[Install]
WantedBy=multi-user.target
EOF
fi
systemctl daemon-reload
systemctl enable ot-sim.service

else

# ── 5D. Role: the plant's DMZ broker ─────────────────────────────────────────
# One machine, one job: run the BeyondTrust Entitle agent inside the plant, so the
# thing that brokers access to plant resources sits in the plant rather than reaching
# in from a cluster somewhere else. It is the only host in the demo with a way out,
# and that way out is two ports to one destination — see the zoning rules the cell
# deploy applies (services/ot_service.py) and docs/profiles/demo/ot-demo-cell.md.
#
# Same KubeSolo as the cell, on purpose: what a customer would put on a plant IPC.
# No simulators, also on purpose.
log "baking the plant's DMZ broker (KubeSolo + the Entitle agent's chart)"
install_k8s_clients
install_kubesolo

# The chart is baked because the agent install must not need the Helm repo at run
# time: anycred.github.io is a CDN, and a CDN cannot be named honestly in the narrow
# egress allow-list a plant boundary is built from. The agent's IMAGES still come from
# Entitle at run time — that is what the 443 hole is for, and on a v1-routing tenant
# that same host proxies the registry.
log "pulling the Entitle agent chart from $OT_ENTITLE_CHART_REPO"
mkdir -p "$OT_ENTITLE_CHART_DIR"
if [ -n "$OT_ENTITLE_CHART_VERSION" ]; then
  helm pull "$OT_ENTITLE_CHART" --repo "$OT_ENTITLE_CHART_REPO" \
    --version "$OT_ENTITLE_CHART_VERSION" --destination "$OT_ENTITLE_CHART_DIR" \
    || die "could not pull $OT_ENTITLE_CHART $OT_ENTITLE_CHART_VERSION from $OT_ENTITLE_CHART_REPO"
else
  helm pull "$OT_ENTITLE_CHART" --repo "$OT_ENTITLE_CHART_REPO" \
    --destination "$OT_ENTITLE_CHART_DIR" \
    || die "could not pull $OT_ENTITLE_CHART from $OT_ENTITLE_CHART_REPO"
fi
_chart_tgz="$(ls -1 "$OT_ENTITLE_CHART_DIR"/*.tgz 2>/dev/null | head -n 1)"
if [ -z "$_chart_tgz" ]; then
  die "helm pull left no chart archive in $OT_ENTITLE_CHART_DIR"
fi
# A stable filename the play can name without knowing the version, beside the
# versioned archive helm wrote — which stays, because "which version is on this
# broker" must be answerable from the host.
cp -f "$_chart_tgz" "$OT_ENTITLE_CHART_DIR/entitle-agent.tgz"
helm show chart "$OT_ENTITLE_CHART_DIR/entitle-agent.tgz" > "$OT_ENTITLE_CHART_DIR/CHART.txt" \
  || die "helm cannot read the chart it just pulled — refusing to bake a broker that \
cannot install the agent"
log "baked chart: $(basename "$_chart_tgz")"

# The egress probe's image, pulled through the cluster so it lands in the same
# containerd the probe pod will run from. The probe exists because the agent runs as a
# POD: whether its traffic leaves with the node's address is a property of the CNI, not
# an assumption to discover in front of a customer — and a pod is the only place that
# question can be asked honestly.
log "pre-pulling the egress probe image ($OT_PROBE_IMAGE)"
kubectl run ot-probe-warm --image="$OT_PROBE_IMAGE" --restart=Never \
  --command -- /bin/true >/dev/null 2>&1 || true
_waited=0
while [ "$_waited" -lt 36 ]; do
  case "$(kubectl get pod ot-probe-warm -o jsonpath='{.status.phase}' 2>/dev/null)" in
    Succeeded|Failed) break ;;
  esac
  _waited=$((_waited + 1))
  sleep 5
done
kubectl delete pod ot-probe-warm --ignore-not-found >/dev/null 2>&1 || true
if [ "$_waited" -ge 36 ]; then
  log "WARNING: the probe image did not pull in time. The agent install will still"
  log "         run, but its pre-flight egress probe will report the image missing"
  log "         rather than proving the plant boundary — see ot-demo-cell.md."
fi

log "the broker is ready: KubeSolo up, chart at $OT_ENTITLE_CHART_DIR/entitle-agent.tgz"

fi

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
  if [ "$OT_ROLE" = "broker" ] || [ "$OT_RUNTIME" = "kubesolo" ]; then
    # Same reasoning as the ssh host keys above, one layer up: a baked cluster would
    # hand every cell the same CA and admin credential, and its Node object still
    # carries the BAKE VM's hostname — which no cell will ever have, so the pods bound
    # to it would sit there unscheduled while Kubernetes reported itself healthy.
    log "resetting the cluster's identity (each cell mints its own CA, node and state)"
    systemctl stop ot-sim.service >/dev/null 2>&1 || true
    systemctl stop kubesolo >/dev/null 2>&1 || true
    for _dir in /var/lib/kubesolo/*; do
      [ -e "$_dir" ] || continue
      # Everything but the image store: those layers cannot be re-pulled inside the
      # plant network. (Losing them is survivable, not fatal — apply.sh re-imports
      # from /var/lib/ot-sim/images on boot — but it costs minutes on every cell.)
      case "$_dir" in */containerd) continue ;; esac
      rm -rf "$_dir"
    done
    # Written by the bake's own run of apply.sh, and describing the BUILD VM: its CA
    # and address, both wrong on a cell. Each cell writes its own at boot.
    rm -f /var/lib/ot-sim/kubeconfig-via-tunnel.yaml
  fi
fi

if [ "$OT_ROLE" = "broker" ]; then
  log "ot-broker bake complete — KubeSolo on :6443, Entitle agent chart baked, \
no simulators, PS account '$OT_ADMIN_USER'"
elif [ "$OT_RUNTIME" = "kubesolo" ]; then
  log "ot-sim bake complete — KubeSolo runtime, sims [$OT_SIMS], FUXA HMI on :1881, \
Kubernetes API on :6443, PS account '$OT_ADMIN_USER'"
else
  log "ot-sim bake complete — docker runtime, sims [$OT_SIMS], FUXA HMI on :1881, \
PS account '$OT_ADMIN_USER'"
fi
