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

# ── The plant's function runtime (broker only) ───────────────────────────────
# Why a function runtime in the plant at all: an Entitle "REST API" integration is an
# HTTP server that Entitle drives, and the target it has to reach — the cell's HMI —
# sits inside the plant boundary. Hosting that server HERE means the Entitle agent
# already on this broker brokers the calls, so the integration needs no inbound hole
# and not one new egress destination. The alternative, a cloud function, would need
# an ingress hole from Entitle's own addresses into the plant zone, which is the
# claim the whole OT demo is built to make. See docs/profiles/demo/ot-demo-cell.md.
#
# `none` bakes the broker every broker was before this section existed: the agent and
# nothing else.
#
# LICENSING, and it is not a footnote. OpenFaaS *Community Edition* limits commercial
# use to ONE installation per company for no more than 60 days, and forbids
# installing it for a client or redistributing it. That is fine for an internal demo
# broker rebuilt inside that window and NOT fine for a customer POV, which is why the
# dashboard treats the runtime as swappable and Nuclio (Apache-2.0) is the planned
# answer for anything shipped to a customer. Do not quietly make CE the only path.
#
# The default is ROLE-AWARE, and it has to be. The guard below refuses the pair
# (cell, openfaas) outright, so a flat `openfaas` default made the script's OWN
# default role — cell — unbakeable: an operator who set no variables at all got a
# die two seconds into the provisioner. Only the UNSET case follows the role; an
# explicit OT_FAAS=openfaas on a cell is still a user error and still refused.
if [ -z "${OT_FAAS:-}" ]; then
  if [ "$OT_ROLE" = "broker" ]; then OT_FAAS="openfaas"; else OT_FAAS="none"; fi
fi
OT_FAAS="$(echo "$OT_FAAS" | tr '[:upper:]' '[:lower:]')"
case "$OT_FAAS" in
  openfaas|none) ;;
  nuclio|deployment)
    die "OT_FAAS=$OT_FAAS is a planned runtime that this script cannot bake yet. \
Use 'openfaas' (read the licensing note in this script first) or 'none'." ;;
  *) die "OT_FAAS must be 'openfaas' or 'none' (got '$OT_FAAS')" ;;
esac
if [ "$OT_ROLE" != "broker" ] && [ "$OT_FAAS" != "none" ]; then
  die "OT_FAAS applies to OT_ROLE=broker only — the cell is the plant floor and runs \
simulators, not the adapters that grant access to them. Set OT_FAAS=none."
fi
OT_OPENFAAS_CHART_REPO="${OT_OPENFAAS_CHART_REPO:-https://openfaas.github.io/faas-netes/}"
OT_OPENFAAS_CHART="${OT_OPENFAAS_CHART:-openfaas}"
OT_OPENFAAS_CHART_VERSION="${OT_OPENFAAS_CHART_VERSION:-14.2.110}"
# Both images are named here AND in the rendered values, and they have to agree: a
# values drift is a pod that tries to pull at boot, on a host with no egress, and
# reports it as ImagePullBackOff — which reads like a blocked firewall rather than
# "that name is not in the local store".
OT_OPENFAAS_GATEWAY_IMAGE="${OT_OPENFAAS_GATEWAY_IMAGE:-ghcr.io/openfaas/gateway:0.27.12}"
OT_OPENFAAS_NETES_IMAGE="${OT_OPENFAAS_NETES_IMAGE:-ghcr.io/openfaas/faas-netes:0.18.12}"
# of-watchdog is a static binary from a GitHub RELEASE asset, not the API, so pinning
# it is about reproducibility rather than the 60-requests-an-hour anonymous API limit
# that bites elsewhere in CI.
OT_OF_WATCHDOG_VERSION="${OT_OF_WATCHDOG_VERSION:-0.10.7}"
OT_FAAS_PYTHON_IMAGE="${OT_FAAS_PYTHON_IMAGE:-docker.io/library/python:3.12-slim}"
# The one image this script BUILDS for the broker. Tagged `:baked` like the cell's
# simulator image, and for the same reason: it exists only in this host's containerd,
# so a manifest that says imagePullPolicy:Never is stating a fact.
OT_FAAS_IMAGE="${OT_FAAS_IMAGE:-ot-faas-python:baked}"
# Only to extract bin/ctr. The cell gets ctr from Docker's containerd.io package and
# then purges Docker around it; the broker has no Docker at all, so it takes the one
# binary out of the upstream release tarball instead — no package, no service, and
# nothing for KubeSolo's pre-flight check to object to.
OT_CONTAINERD_VERSION="${OT_CONTAINERD_VERSION:-1.7.24}"
OT_FAAS_DIR=/opt/ot-faas
OT_FAAS_IMAGE_DIR=/var/lib/ot-faas/images
if [ "$OT_FAAS" != "none" ]; then
  for _img in "$OT_OPENFAAS_GATEWAY_IMAGE" "$OT_OPENFAAS_NETES_IMAGE" \
              "$OT_FAAS_PYTHON_IMAGE" "$OT_FAAS_IMAGE"; do
    case "$_img" in
      *:latest) die "$_img must be pinned to a version tag, not :latest" ;;
      *:*) ;;
      *) die "the function-runtime images must carry explicit tags (got '$_img')" ;;
    esac
  done
  case "$OT_OPENFAAS_CHART_VERSION" in
    "") die "OT_OPENFAAS_CHART_VERSION must be pinned — an unpinned chart can \
re-enable NATS or Prometheus on its own and quietly outgrow a 2-vCPU broker" ;;
  esac
fi

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

install_ctr() {
  # containerd's CLI, and the only way an image gets into a containerd that has no
  # registry behind it. The cell takes it out of Docker's containerd.io package just
  # before purging Docker (section 5b); the broker never installs Docker, so it takes
  # the single binary out of the upstream release tarball. A CLI, not an engine —
  # nothing is installed as a service and nothing appears on the host that KubeSolo's
  # "is Docker here?" pre-flight check could object to.
  if [ -x /usr/local/bin/ctr ]; then
    log "ctr is already present ($(/usr/local/bin/ctr --version 2>/dev/null | head -n 1))"
    return 0
  fi
  log "installing ctr from containerd $OT_CONTAINERD_VERSION (the binary only)"
  curl -fsSL -o /tmp/containerd.tar.gz \
    "https://github.com/containerd/containerd/releases/download/v$OT_CONTAINERD_VERSION/containerd-$OT_CONTAINERD_VERSION-linux-$OT_ARCH.tar.gz" \
    || die "could not download the containerd $OT_CONTAINERD_VERSION release tarball"
  # Only bin/ctr. Extracting the whole archive would drop containerd and
  # containerd-shim into /usr/local/bin, where KubeSolo's own copies belong.
  tar -xzf /tmp/containerd.tar.gz -C /tmp bin/ctr \
    || die "the containerd tarball did not contain bin/ctr"
  install -m 0755 /tmp/bin/ctr /usr/local/bin/ctr
  rm -rf /tmp/containerd.tar.gz /tmp/bin
  /usr/local/bin/ctr --version >/dev/null || die "the extracted ctr does not run"
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
  # `kubectl wait node --all` is not a wait until a Node object EXISTS: with nothing
  # matching it prints "error: no matching resources found" and exits 1 at once,
  # --timeout unread. The installer returns as soon as it has written the kubeconfig,
  # seconds before kubesolo registers its node, so waiting on --all alone turns the
  # very race this is here to absorb into an instant failed bake. Wait for the file,
  # then for the API to serve, then for the object to exist — THEN on Ready.
  _waited=0
  while [ ! -f "$KUBECONFIG" ] \
    || ! kubectl get --raw /readyz >/dev/null 2>&1 \
    || [ -z "$(kubectl get nodes -o name 2>/dev/null)" ]; do
    _waited=$((_waited + 1))
    if [ "$_waited" -gt 60 ]; then
      journalctl -u kubesolo --no-pager -n 40 2>/dev/null || true
      die "KubeSolo's API never came up with a registered node ($KUBECONFIG)"
    fi
    sleep 5
  done
  kubectl wait --for=condition=Ready node --all --timeout=300s || {
    journalctl -u kubesolo --no-pager -n 40 2>/dev/null || true
    die "the KubeSolo node never became Ready (journalctl -u kubesolo)"
  }
}

# ── 1. OS-family gate ────────────────────────────────────────────────────────
[ -f /etc/debian_version ] || die "not a Debian-family system (no /etc/debian_version)"
log "starting ot-sim bake on $(cat /etc/debian_version 2>/dev/null || echo unknown) ($(uname -m))"

# ── 2. System updates ────────────────────────────────────────────────────────
export DEBIAN_FRONTEND=noninteractive

# Ubuntu wires command-not-found's index rebuild into apt as an
# APT::Update::Post-Invoke-Success hook, and `cnf-update-db` exits non-zero whenever a
# `cnf_Commands` index it expects was never fetched — routine on Azure's Ubuntu images,
# and the jammy-backports/multiverse one in particular. The update itself SUCCEEDS; the
# hook fails afterwards and apt-get reports the hook's status as its own, so `set -e`
# kills the whole bake over an interactive shell convenience that has no business in a
# baked server image. The tell is an apt error naming `APT::Update::Post-Invoke-Success`
# and a CommandNotFound/db/creator.py traceback, directly after a clean "Fetched ... MB".
#
# Clear the hook list rather than tolerating a non-zero apt-get: a REAL update failure
# must still stop the build. apt.conf.d is read in lexical order, so 99- lands after the
# 50command-not-found that installs the hook. A no-op on Debian, which does not ship it.
mkdir -p /etc/apt/apt.conf.d
printf '#clear APT::Update::Post-Invoke-Success;\n' \
  > /etc/apt/apt.conf.d/99-ot-sim-no-cnf-hook
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
# it. The broker never installs Docker in the first place — and therefore has nothing
# to purge before that check runs. It does build ONE image (the function runtime's, in
# section 5E), and it uses buildah for it precisely to keep that true: buildah is
# daemonless, so it leaves no docker0, no second containerd and no service for
# KubeSolo's pre-flight check to object to, and section 5E purges it afterwards.
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

# ── The HMI's own authentication ─────────────────────────────────────────────
# FUXA ships with authentication OFF, and that is not "reduced" — it is absent.
# With `secureEnabled` false the server short-circuits twice: the API-key middleware
# returns next() without looking for a token, and verifyGroups hands every anonymous
# caller adminGroups[0]. So on a stock image ANYONE who can reach :1881 can list,
# create and delete HMI users and roles with no credential. Network isolation is the
# only control, and an HMI in that state is not something to show a security buyer.
#
# On by default. It is also the precondition for the Entitle JIT adapter meaning
# anything: a just-in-time account on an HMI that authenticates nobody is theatre.
OT_FUXA_SECURE="${OT_FUXA_SECURE:-1}"
# Short, because this is the REVOCATION WINDOW and not just a login lifetime.
# Deleting a user cuts REST access at once, but FUXA verifies a socket's token once
# at connect and never re-checks, so an already-open browser session survives until
# it reconnects. That interval is this value. 15m keeps the honest claim close to
# the demo's claim; the format is jsonwebtoken's ('15m', '1h', or seconds as a number).
OT_FUXA_TOKEN_EXPIRES="${OT_FUXA_TOKEN_EXPIRES:-15m}"
# FUXA's own seeded account, created only when users.fuxap.db does not yet exist.
# The bake CANNOT usefully change this password: the file it would write ships inside
# the image, so every cell built from it would share one credential. It is rotated
# per-cell at wire time instead (examples/playbooks/kubesolo/fuxa-admin-rotate.yml),
# which is also the only point at which a password can be handed to the adapter.
OT_FUXA_ADMIN_USER="${OT_FUXA_ADMIN_USER:-admin}"
case "$OT_FUXA_SECURE" in
  0|1) ;;
  *) die "OT_FUXA_SECURE must be 0 or 1 (got '$OT_FUXA_SECURE')" ;;
esac
if [ "$OT_FUXA_SECURE" = "1" ] && [ "$OT_RUNTIME" != "kubesolo" ]; then
  # The settings file lives in FUXA's appdata, which the KubeSolo runtime mounts from
  # a hostPath this script primes. The docker fallback uses a named volume that only
  # exists once the container has run, so there is nothing to write into at bake time.
  die "OT_FUXA_SECURE=1 needs OT_RUNTIME=kubesolo (the docker fallback's appdata is a \
named volume that does not exist until first run). Use OT_FUXA_SECURE=0 for a docker \
bake, and know that its HMI applies NO authorization at all."
fi

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

# What has to answer once the stack is up. Only the KubeSolo runtime consumes this:
# it waits on the rollouts and THEN proves each listener, because "the pod is Ready"
# has never been the same claim as "the PLC answers". The docker runtime keeps the
# container-state check it has always had, deliberately — it is the tested path, and
# a new assertion there could only ever fail a bake that used to pass.
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

# FUXA's project data is a hostPath here, not a named volume. This used to try to
# copy the image's own _appdata into it first, on the assumption that a fresh named
# volume had been seeded from the image and the hostPath would not be — but there is
# nothing in the image to seed from: frangoteam/fuxa declares no VOLUME and ships no
# _appdata, the server creates it on first start. So the copy could only ever fail,
# and it did, loudly, on every cell bake ("Could not find the file ... in container"
# plus a WARNING about FUXA starting empty). Empty is the correct starting state; the
# project arrives from the bake-time seed further down, over FUXA's own API. The
# directory itself is made with the image dir above.
# Authentication ON, written BEFORE FUXA ever starts.
#
# That ordering is the whole trick. The other way to set these is POST /api/settings,
# which RESTARTS the FUXA runtime — and the restart lands in the middle of the project
# seed below, whose read-back then 401s and reports a project that seeded fine as "NOT
# seeded". Writing the file while nothing is running has no ordering problem at all.
#
# mysettings.json, not settings.js: FUXA reads both and the JSON one overrides, and it
# is what FUXA's own UI writes. It will not exist yet on a fresh bake — the image
# ships no _appdata at all, per the note above — so this usually CREATES it. The
# read-modify-write is kept anyway: the file is the whole settings document, so an
# image that did ship one must not have keys (uiPort among them) clobbered out of
# it by a writer that only knows about three.
if [ "$OT_FUXA_SECURE" = "1" ]; then
  log "enabling FUXA authentication (secureEnabled, tokenExpiresIn=$OT_FUXA_TOKEN_EXPIRES)"
  OT_FUXA_TOKEN_EXPIRES="$OT_FUXA_TOKEN_EXPIRES" python3 - <<'PYEOF' \
    || die "could not write FUXA's settings — refusing to bake an HMI that authenticates nobody"
import json
import os

path = "/var/lib/ot-sim/fuxa/mysettings.json"
settings = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            settings = loaded
    except (OSError, ValueError):
        # An existing file we cannot parse is not a document to preserve; FUXA
        # falls back to settings.js anyway, so writing a clean one is a repair.
        settings = {}

settings["secureEnabled"] = True
settings["tokenExpiresIn"] = os.environ["OT_FUXA_TOKEN_EXPIRES"]
# A PLACEHOLDER, replaced per-cell on first boot by apply.sh. Baking a real one would
# give every cell built from this image the same JWT signing key, so a token minted
# on one plant would validate on another. Same reasoning as the cluster identity the
# cleanup wipes at the end of this script.
settings["secretCode"] = "REPLACE_ON_FIRST_BOOT"

with open(path, "w", encoding="utf-8") as handle:
    json.dump(settings, handle, indent=2, sort_keys=True)
print("[ot-sim] wrote %s (%d keys)" % (path, len(settings)))
PYEOF
else
  log "WARNING: OT_FUXA_SECURE=0 - this HMI will apply NO authorization at all."
  log "         Anyone who can reach :1881 can create and delete its users."
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
# `command -v` is not a filesystem probe. Both dash and bash answer it out of the
# shell's own command hash before they ever look at PATH, and this script has just
# run docker a dozen times to build and export the images — so the purged
# /usr/bin/docker is still cached and the guard fires on a host where Docker is
# genuinely gone. Drop the cache first, then hold whatever survives against the
# filesystem: the -x is what keeps this honest in a shell whose `hash -r` did
# nothing, and it is the only one of the two tests that cannot be fooled.
hash -r 2>/dev/null || true
_docker_left=$(command -v docker 2>/dev/null || true)
if [ -n "$_docker_left" ] && [ -x "$_docker_left" ]; then
  die "docker is still on PATH after the purge ($_docker_left) — KubeSolo's \
installer would refuse this host"
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
# --local on every import, and it is not decoration. containerd 2.0 changed `ctr
# images import` to hand the tarball to the TRANSFER service, which is served over
# containerd.services.streaming.v1.Streaming — an API KubeSolo's embedded containerd
# does not register. The cell's ctr is whatever Docker's containerd.io package ships
# on the day of the bake, and that is now 2.x, so the default path dies with "unknown
# service containerd.services.streaming.v1.Streaming" against a socket where `images
# ls` answers perfectly: it reads like a broken containerd rather than a client that
# asked for an API this one does not have. --local is the pre-2.0 path — the client
# reads the tarball and writes through the content and images services KubeSolo does
# serve — and it is a no-op on the 1.7 ctr the broker pins, where it already defaults
# to true. Every `images import` in this script and in the apply.sh it writes carries
# it, because the cell re-imports at boot with this same client.
for _pair in "ot-plc-sim:baked|ot-plc-sim.tar" "$OT_FUXA_IMAGE|fuxa.tar"; do
  _ref="$(normalize_ref "${_pair%%|*}")"
  _tarball="${_pair##*|}"
  ctr --address "$KUBESOLO_SOCK" --namespace k8s.io images import --local \
    "$OT_IMAGE_DIR/$_tarball" \
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

# 1. Wait for the API AND for the node to be registered. The bake drops the cluster's
#    identity so that every cell mints its own CA, node and state on first boot -- so
#    this is a real wait, not a formality, and it is the step that takes the time on a
#    cell's first start. The node-exists clause is load-bearing: `kubectl wait node
#    --all` with no Node object yet is not a wait at all, it exits 1 immediately with
#    "error: no matching resources found", and on first boot the object arrives after
#    the API starts serving.
tries=0
while [ ! -f "$KUBECONFIG" ] \
  || ! kubectl get --raw /readyz >/dev/null 2>&1 \
  || [ -z "$(kubectl get nodes -o name 2>/dev/null)" ]; do
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
  # --local because a 2.x ctr would otherwise route the tarball through containerd's
  # transfer service, and KubeSolo does not serve the streaming API that needs; on
  # the 1.7 client it is already the default. See the bake's own import loop.
  # </dev/null so the import cannot consume the image list this loop is reading.
  $CTR images import --local "$IMAGE_DIR/$tarball" </dev/null
done < "$IMAGE_DIR/images.txt"

# 3. Apply, then wait on each Deployment, so a boot that only half-worked says so in
#    `systemctl status ot-sim` instead of in front of a customer.
# 2b. This cell's own JWT signing key, before the HMI can read it.
#     The image ships a placeholder, because a real key baked into it would be the
#     same on every cell built from that image — a token minted on one plant would
#     then validate on another. Same reasoning as the cluster CA the bake wipes.
#     Left unset instead, FUXA generates a fresh key per PROCESS, so every restart
#     silently logs everyone out; a per-cell key that persists is what we want.
FUXA_SETTINGS=/var/lib/ot-sim/fuxa/mysettings.json
if [ -f "$FUXA_SETTINGS" ] && grep -q REPLACE_ON_FIRST_BOOT "$FUXA_SETTINGS"; then
  log "minting this cell's FUXA signing key"
  FUXA_SETTINGS="$FUXA_SETTINGS" python3 - <<'PY'
import json
import os
import secrets

path = os.environ["FUXA_SETTINGS"]
with open(path, encoding="utf-8") as handle:
    settings = json.load(handle)
if settings.get("secretCode") == "REPLACE_ON_FIRST_BOOT":
    settings["secretCode"] = secrets.token_urlsafe(48)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2, sort_keys=True)
PY
  # 0666, matching the 0777 on the directory above and for the same reason: the
  # pinned FUXA image's UID is not known here, and a root-owned 0600 file is one
  # FUXA cannot read at startup — which presents as authentication silently not
  # being on. The cell is a single-purpose appliance whose appdata is already
  # world-readable, so this widens nothing that was narrow.
  chmod 0666 "$FUXA_SETTINGS" 2>/dev/null || true
fi

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


# Set once by _signin() and attached to every call after it. Empty is a valid
# state: a FUXA with secureEnabled off wants no token and rejects nothing.
TOKEN = ""
ADMIN_USER = os.environ.get("FUXA_ADMIN_USER") or "admin"
# The account FUXA seeds itself with, which is what exists at BAKE time. It is
# rotated per-cell at wire time, long after this script has run.
ADMIN_PASSWORD = os.environ.get("FUXA_ADMIN_PASSWORD") or "123456"


def _call(path, payload=None, timeout=20):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        # x-access-token, NOT Authorization: Bearer. No bearer parsing exists
        # anywhere in the FUXA server, so the wrong header is an unauthenticated
        # request that 401s while looking correct.
        headers["x-access-token"] = TOKEN
    req = urllib.request.Request(
        BASE + path, data=body, headers=headers,
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode().strip()
    return json.loads(raw) if raw[:1] in ("{", "[") else {}


def _signin():
    """A session token, or "" when this FUXA needs none.

    Deliberately non-fatal in both directions. An image baked with
    OT_FUXA_SECURE=0 has nothing to sign in to and must still seed; one baked with
    it on must not fail the whole 15-minute bake because the seed could not
    authenticate — the project step has always been best-effort, and the caller
    below reports it either way.
    """
    global TOKEN
    try:
        payload = _call("/api/signin",
                        {"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    except Exception as exc:  # noqa: BLE001
        print("NOTE: FUXA sign-in failed (%s) - continuing unauthenticated" % exc)
        return ""
    token = ""
    if isinstance(payload, dict):
        token = str((payload.get("data") or {}).get("token") or "")
    TOKEN = token
    if token:
        print("signed in to FUXA as %s" % ADMIN_USER)
    return token


def _wait_for_api(seconds):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            # Sign in on every attempt, not once before the loop: on the first of
            # them FUXA may not be listening yet, and a token minted before the
            # runtime finished starting is one it does not know.
            _signin()
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
export FUXA_ADMIN_USER="$OT_FUXA_ADMIN_USER"
if FUXA_PLC_ADDRESS="$FUXA_PLC_ADDRESS" python3 /opt/ot-sim/plc-sim/fuxa_seed.py; then
  log "FUXA project seeded - the cell opens on a wired PLC connection"
else
  log "WARNING: FUXA project NOT seeded (see the error above). The image is still"
  log "         good: wire the connection by hand once per cell, as described in"
  log "         provisioners/ot/README.md (FUXA project seeding)."
fi

# Did the authentication actually take effect? Asked directly rather than assumed.
#
# This is the one check that matters for OT_FUXA_SECURE, and it is not a formality:
# settings.js and mysettings.json are both read and the second overrides, so whether
# a partial file MERGES or is ignored is a property of the pinned FUXA rather than
# something this script can guarantee. And the failure is silent in the worst
# direction — an HMI that looks configured and authorizes everyone.
#
# The probe is exact: with secureEnabled ON, /api/users requires admin and answers
# 401 to an anonymous caller. With it OFF, verifyGroups hands that caller
# adminGroups[0] and the same request returns 200 with the user list. So the status
# code IS the answer.
if [ "$OT_RUNTIME" = "kubesolo" ]; then
  _fuxa_anon_status="$(python3 - <<'PY'
import urllib.error
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:1881/api/users", timeout=15) as resp:
        print(resp.status)
except urllib.error.HTTPError as exc:
    print(exc.code)
except Exception:
    print("0")
PY
)"
  log "anonymous GET /api/users -> HTTP $_fuxa_anon_status"
  if [ "$OT_FUXA_SECURE" = "1" ]; then
    case "$_fuxa_anon_status" in
      401|403) log "FUXA authentication is ON and enforced" ;;
      200) die "OT_FUXA_SECURE=1 but an ANONYMOUS caller listed FUXA's users. The \
settings were not applied - check that $OT_FUXA_IMAGE reads _appdata/mysettings.json \
and that the key is still called secureEnabled. Refusing to bake an HMI that claims \
authentication it does not have." ;;
      *) log "WARNING: could not probe FUXA's authentication (HTTP $_fuxa_anon_status)."
         log "         The image still ships; verify by hand that an anonymous"
         log "         GET /api/users on a deployed cell is refused." ;;
    esac
  elif [ "$_fuxa_anon_status" = "200" ]; then
    log "NOTE: as expected for OT_FUXA_SECURE=0, this HMI authorizes anonymous callers."
  fi
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

if [ "$OT_FAAS" = "openfaas" ]; then

# ── 5E. The plant's function runtime (broker only) ───────────────────────────
# What this gets us: an Entitle "REST API" integration whose HTTP server lives in the
# plant, so Entitle's own agent — a pod on this same cluster — makes the calls and the
# integration's base URL is a name that resolves nowhere else. No inbound hole, no new
# egress destination, and the plant's "two ports to one destination" claim is untouched.
#
# The adapter's CODE is deliberately NOT baked. The bake channel is one file (Packer
# gets a single shell provisioner, so the repo's functions/ tree cannot reach the build
# VM), which would make a baked adapter a heredoc twin of ~45 KB of security-relevant
# Python that drifts from the real thing and needs a re-bake per fix. Instead this
# image is a generic loader: bootstrap.py unpacks a package the dashboard sends at wire
# time in the Function's `environment:`. So an adapter fix ships in the dashboard image
# and a broker baked weeks ago still runs it.
log "baking the plant's function runtime (OpenFaaS CE $OT_OPENFAAS_CHART_VERSION)"
install_ctr
mkdir -p "$OT_FAAS_DIR/image" "$OT_FAAS_DIR/kubesolo" "$OT_FAAS_DIR/charts" \
         "$OT_FAAS_IMAGE_DIR"

log "fetching of-watchdog $OT_OF_WATCHDOG_VERSION"
case "$OT_ARCH" in
  amd64) _watchdog_asset="of-watchdog" ;;
  arm64) _watchdog_asset="of-watchdog-arm64" ;;
  *) die "no of-watchdog release asset for architecture '$OT_ARCH'" ;;
esac
curl -fsSL -o "$OT_FAAS_DIR/image/of-watchdog" \
  "https://github.com/openfaas/of-watchdog/releases/download/$OT_OF_WATCHDOG_VERSION/$_watchdog_asset" \
  || die "could not download of-watchdog $OT_OF_WATCHDOG_VERSION for $OT_ARCH"
chmod 0755 "$OT_FAAS_DIR/image/of-watchdog"

# The ONLY Python this image carries, and it holds no business logic on purpose — see
# the note above. Keep it boring: every string in it is half of a contract with the
# dashboard (tests/test_ot_faas_contract.py holds the two halves together).
cat > "$OT_FAAS_DIR/image/bootstrap.py" <<'PYEOF'
"""Unpack the function package the dashboard sent, then run it.

Baked by provisioners/ot/ot-sim-debian.sh. The adapter, the fnruntime tree and the
HTTP server all arrive at wire time as OTFN_PKG_B64 — the deterministic zip built by
web_dashboard/services/cloud_function_package.py — so this file never has to change
when an adapter does, and a broker baked weeks ago runs today's adapter.

With no package it serves a sentinel on every path instead. That is what lets the
BAKE smoke-test the whole chain (gateway, of-watchdog, this loader, cluster DNS) on
a build VM that still has egress, with no dashboard and no cluster credentials in
the picture — so the only things left to discover live are the ones that genuinely
need the plant.
"""
import base64
import hashlib
import io
import os
import runpy
import sys
import zipfile

SENTINEL = "ot-faas-selftest-ok"
UNPACK_DIR = "/tmp/fn"
PORT = int(os.environ.get("OTFN_PORT") or 5000)


def _unpack(encoded, expected_sha):
    """The package, verified, on disk. Raises rather than running anything unsure.

    The hash is REQUIRED, not optional. This package carries the code that mints
    credentials, and it travels as base64 inside a single --extra-vars argv element
    whose kernel limit (MAX_ARG_STRLEN) is a real ceiling — so a truncated payload is
    a failure mode that actually happens, and a truncated zip can still extract
    something. Refusing is the only safe reading of "I cannot confirm this".
    """
    if not expected_sha:
        raise SystemExit(
            "OTFN_PKG_B64 is set but OTFN_PKG_SHA256 is not: refusing to run an "
            "unverified function package")
    blob = base64.b64decode(encoded)
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected_sha:
        raise SystemExit(
            "the function package does not match OTFN_PKG_SHA256 (got %s, expected "
            "%s): it was altered or truncated in transit -- refusing to run it"
            % (actual, expected_sha))
    os.makedirs(UNPACK_DIR, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        archive.extractall(UNPACK_DIR)
    return UNPACK_DIR


def _serve_sentinel():
    """Answer every path with the sentinel, so the bake can prove the chain."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _reply(self):
            # The path is echoed because the bake asserts on it: an OpenFaaS gateway
            # that did not forward the residual path would make every Entitle route
            # land on the function root, and the adapter would be unroutable.
            body = ('{"status": "%s", "path": "%s"}' % (SENTINEL, self.path)).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _reply
        do_POST = _reply

    sys.stderr.write("no OTFN_PKG_B64: serving the selftest sentinel\n")
    sys.stderr.flush()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def main():
    encoded = (os.environ.get("OTFN_PKG_B64") or "").strip()
    if not encoded:
        _serve_sentinel()
        return
    target = _unpack(encoded, (os.environ.get("OTFN_PKG_SHA256") or "").strip())
    sys.path.insert(0, target)
    # run_name="__main__" so the shim's own `if __name__ == "__main__"` fires and it
    # starts its server. Anything else loads the module and exits immediately, which
    # presents as a pod that never becomes ready.
    runpy.run_module("openfaas_entry", run_name="__main__")


if __name__ == "__main__":
    main()
PYEOF
python3 -c "import ast, sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" \
  "$OT_FAAS_DIR/image/bootstrap.py" \
  || die "the baked bootstrap.py is not valid Python — refusing to build the image"

cat > "$OT_FAAS_DIR/image/Dockerfile" <<EOF
# The plant's generic function image. Built once at bake; the code it runs arrives
# later. of-watchdog in mode=http keeps one long-lived Python process and proxies to
# it, which is what lets the package be unpacked once at start rather than per call.
FROM $OT_FAAS_PYTHON_IMAGE
COPY of-watchdog /usr/bin/fwatchdog
COPY bootstrap.py /app/bootstrap.py
# Non-root, and the port is above 1024 so it needs no capability. /tmp stays writable
# because that is where bootstrap.py unpacks the package.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
USER app
ENV mode=http \\
    upstream_url=http://127.0.0.1:5000 \\
    fprocess="python3 /app/bootstrap.py" \\
    exec_timeout=60s \\
    read_timeout=65s \\
    write_timeout=65s
EXPOSE 8080
CMD ["fwatchdog"]
EOF

# buildah, not Docker. KubeSolo's installer refuses a host that still carries Docker,
# and the cell pays for that with a whole purge dance — apt purge, rm -rf
# /var/lib/docker, ip link delete docker0, reinstalling iptables by name, then
# flushing every chain in filter/nat/mangle — because Docker's chains and its FORWARD
# DROP policy outlive the packages. Repeating all of it for ONE small image build
# doubles the surface where a broker bake can fail. buildah is daemonless: no
# docker0, no containerd, no service, nothing for the pre-flight check to see.
#
# --storage-driver vfs deliberately: overlay wants kernel overlayfs or fuse-overlayfs
# and its availability varies by cloud image. vfs is slower and hungrier, which for
# one small image is a few seconds and a few hundred MB on a build VM, and in exchange
# it works on any kernel. --isolation chroot for the same reason: no user namespaces
# required, and this runs as root on a throwaway VM.
log "installing buildah to build $OT_FAAS_IMAGE (daemonless — no Docker on a broker)"
apt-get -y -q install buildah \
  || die "buildah is not installable here; a broker cannot build its function image"
buildah --storage-driver vfs bud --isolation chroot \
  -t "$OT_FAAS_IMAGE" "$OT_FAAS_DIR/image" \
  || die "buildah could not build $OT_FAAS_IMAGE (its output is above)"
# docker-archive is byte-for-byte what `docker save` writes, so everything downstream
# of here is the cell's already-proven ctr import path, unchanged.
buildah --storage-driver vfs push \
  "$OT_FAAS_IMAGE" "docker-archive:$OT_FAAS_IMAGE_DIR/ot-faas-python.tar:$OT_FAAS_IMAGE" \
  || die "buildah could not export $OT_FAAS_IMAGE to a docker-archive tarball"
log "purging buildah and its layer store (~150 MB that would otherwise ship)"
apt-get -y -q purge buildah >/dev/null 2>&1 || true
apt-get -y -q autoremove >/dev/null 2>&1 || true
rm -rf /var/lib/containers /var/cache/buildah

# The upstream images go straight into the containerd the pods will run from, while
# this VM still has egress. Exported to tarballs as well, and listed in images.txt,
# because the cleanup in section 6 preserves */containerd but that is a convenience
# rather than a contract — apply.sh re-imports from these on boot.
log "loading the runtime's images into KubeSolo's containerd"
for _pair in \
  "$OT_OPENFAAS_GATEWAY_IMAGE|openfaas-gateway.tar|pull" \
  "$OT_OPENFAAS_NETES_IMAGE|openfaas-netes.tar|pull" \
  "$OT_FAAS_IMAGE|ot-faas-python.tar|import" ; do
  _img="${_pair%%|*}"
  _rest="${_pair#*|}"
  _tarball="${_rest%%|*}"
  _how="${_rest##*|}"
  _ref="$(normalize_ref "$_img")"
  if [ "$_how" = "pull" ]; then
    # --local on pull and export for the same reason as on import below: containerd
    # 2.0 routes all three through the transfer service, which KubeSolo does not
    # serve. On the 1.7 client this role pins, all three already default to local.
    ctr --address "$KUBESOLO_SOCK" --namespace k8s.io images pull --local "$_ref" \
      || die "could not pull $_ref — the broker bake needs egress to the registry"
    ctr --address "$KUBESOLO_SOCK" --namespace k8s.io images export --local \
      "$OT_FAAS_IMAGE_DIR/$_tarball" "$_ref" \
      || die "could not export $_ref to $_tarball"
  else
    # --local, exactly as the cell's import loop explains: the transfer service a 2.x
    # ctr would use by default needs a streaming API KubeSolo does not serve. Harmless
    # on the 1.7 client this role pins, where local is already the default — and it is
    # what keeps a bump of OT_CONTAINERD_VERSION to 2.x from breaking the broker.
    ctr --address "$KUBESOLO_SOCK" --namespace k8s.io images import --local \
      "$OT_FAAS_IMAGE_DIR/$_tarball" \
      || die "ctr could not import $_tarball into KubeSolo's containerd"
  fi
  # Proven, not assumed: every manifest below sets imagePullPolicy: Never, so a name
  # containerd does not hold is a pod that dies ErrImageNeverPull on a host with no
  # egress — which reads like a blocked firewall instead of a missing local image.
  if ! ctr --address "$KUBESOLO_SOCK" --namespace k8s.io images ls -q | grep -qx "$_ref"; then
    die "$_tarball is loaded but containerd does not list $_ref — the pods would never start"
  fi
  echo "$_ref $_tarball" >> "$OT_FAAS_IMAGE_DIR/images.txt"
done

# The chart is baked for the same reason the Entitle chart above is: a Helm repo is a
# CDN, and a CDN cannot be named honestly in the narrow egress allow-list a plant
# boundary is built from.
log "pulling the OpenFaaS chart from $OT_OPENFAAS_CHART_REPO"
helm pull "$OT_OPENFAAS_CHART" --repo "$OT_OPENFAAS_CHART_REPO" \
  --version "$OT_OPENFAAS_CHART_VERSION" --destination "$OT_FAAS_DIR/charts" \
  || die "could not pull $OT_OPENFAAS_CHART $OT_OPENFAAS_CHART_VERSION from $OT_OPENFAAS_CHART_REPO"
_faas_chart="$(ls -1 "$OT_FAAS_DIR/charts"/openfaas-*.tgz 2>/dev/null | head -n 1)"
[ -n "$_faas_chart" ] || die "helm pull left no chart archive in $OT_FAAS_DIR/charts"
cp -f "$_faas_chart" "$OT_FAAS_DIR/charts/openfaas.tgz"
helm show chart "$OT_FAAS_DIR/charts/openfaas.tgz" > "$OT_FAAS_DIR/charts/CHART.txt" \
  || die "helm cannot read the chart it just pulled"
log "baked chart: $(basename "$_faas_chart")"

# Trimmed to the two things that are actually needed. Each `false` below is a pod not
# running on a 2-vCPU broker that also carries the Entitle agent (1Gi of requests on
# its own) — and the cost of each is stated, because discovering it in front of a
# customer is worse than reading it here:
#
#   async          drops nats + queue-worker (2 pods). Entitle's calls are synchronous
#                  request/response; nothing here ever queues.
#   prometheus     1 pod. Cost: /system/functions reports zero invocations and the UI
#                  graphs are empty. Say so rather than let someone find it.
#   alertmanager   1 pod. It only drives CE's scale-from-zero, and we pin one replica.
#   basicAuthPlugin 1 pod, and only the browser UI's login flow. So the OpenFaaS web
#                  UI is NOT part of this demo.
#
# basic_auth stays TRUE: it gates /system/*, deploys here are `kubectl apply` so
# nothing needs the credential, and apply.sh mints it from /dev/urandom — which
# leaves the admin API behind a secret no human holds.
#
# These values are not the guarantee. A chart version can rename a key and helm will
# ignore the old one in silence, so the render is checked below (every image must be
# one we pre-loaded) and the running result is checked after apply (exactly one pod).
cat > "$OT_FAAS_DIR/values.yaml" <<EOF
functionNamespace: openfaas-fn
# CRD mode, so a function is deployed with kubectl apply: declarative, idempotent,
# and it needs no gateway credential at deploy time.
operator:
  create: true
async: false
prometheus:
  create: false
alertmanager:
  create: false
basicAuthPlugin:
  enabled: false
basic_auth: true
serviceType: ClusterIP
gateway:
  replicas: 1
  image: $OT_OPENFAAS_GATEWAY_IMAGE
  imagePullPolicy: Never
faasnetes:
  image: $OT_OPENFAAS_NETES_IMAGE
  imagePullPolicy: Never
operator_image: $OT_OPENFAAS_NETES_IMAGE
EOF

# Rendered at bake rather than `helm install`ed at boot: a static manifest can be read
# and diffed on the host, and there is no release state for a half-finished first boot
# to leave behind. Same shape as the cell's ot-sim.yaml.
#
# --include-crds is load-bearing: without it the functions.openfaas.com CRD is absent
# and the operator crash-loops on a resource type that does not exist.
log "rendering the chart to a static manifest"
_rendered="$OT_FAAS_DIR/kubesolo/openfaas.yaml"
# The chart creates neither namespace, and the labels are not decoration: faas-netes
# finds function namespaces by the `openfaas: "1"` label.
cat > "$_rendered" <<'EOF'
# OpenFaaS on the plant's DMZ broker -- rendered at bake time by
# provisioners/ot/ot-sim-debian.sh from the pinned chart in /opt/ot-faas/charts.
# Re-render rather than edit: apply.sh applies this file verbatim on every boot.
---
apiVersion: v1
kind: Namespace
metadata:
  name: openfaas
  labels:
    role: openfaas-system
---
apiVersion: v1
kind: Namespace
metadata:
  name: openfaas-fn
  labels:
    openfaas: "1"
EOF
helm template openfaas "$OT_FAAS_DIR/charts/openfaas.tgz" \
  --namespace openfaas --include-crds -f "$OT_FAAS_DIR/values.yaml" >> "$_rendered" \
  || die "helm template failed on the pinned OpenFaaS chart"
grep -q "kind: CustomResourceDefinition" "$_rendered" \
  || die "the render carries no CRD — --include-crds did not take, and the operator \
would crash-loop on a resource type that does not exist"

# imagePullPolicy is the one setting whose failure the SMOKE TEST CANNOT CATCH: the
# build VM has egress, so `Always` pulls fine here and only fails on a cell, at boot,
# in an egress-less subnet — reported as ImagePullBackOff, which reads like a blocked
# firewall. So it is normalised here, in the render, and loudly: silently rewriting
# would hide a values key that did not apply, so the count is logged either way.
_always="$(grep -c 'imagePullPolicy: *Always' "$_rendered" || true)"
if [ "${_always:-0}" -gt 0 ]; then
  log "NOTE: normalising $_always 'imagePullPolicy: Always' to Never in the render."
  log "      The chart ignored a values key (they get renamed between versions). The"
  log "      images are all local, so Never is correct — but check the key names in"
  log "      $OT_FAAS_DIR/values.yaml against chart $OT_OPENFAAS_CHART_VERSION."
  sed -i 's/imagePullPolicy: *Always/imagePullPolicy: Never/g' "$_rendered"
fi
grep -q 'imagePullPolicy: *Always' "$_rendered" \
  && die "an 'imagePullPolicy: Always' survived normalisation in $_rendered"

# Every image the render names must be one we pre-loaded. This is what catches a
# component that quietly came back — an async queue, a Prometheus — because the tell
# is an image nobody exported, and at boot it would be a pod stuck ErrImageNeverPull
# rather than an obviously-wrong pod count.
log "checking the render names only pre-loaded images"
_unexpected=""
for _img in $(sed -nE 's/^[[:space:]]*image:[[:space:]]*"?([^"[:space:]]+).*/\1/p' \
              "$_rendered" | sort -u); do
  _r="$(normalize_ref "$_img")"
  awk -v want="$_r" '$1 == want { found = 1 } END { exit !found }' \
    "$OT_FAAS_IMAGE_DIR/images.txt" || _unexpected="$_unexpected $_img"
done
if [ -n "$_unexpected" ]; then
  die "the rendered manifest needs images this bake never loaded:$_unexpected — a \
chart component is enabled that values.yaml meant to switch off, or an image pin \
disagrees with the chart's default. Every one of these would be ErrImageNeverPull on \
a plant host."
fi

cat > "$OT_FAAS_DIR/kubesolo/apply.sh" <<'EOF'
#!/bin/sh
# Bring the plant's function runtime up on KubeSolo. Baked by
# provisioners/ot/ot-sim-debian.sh, run at boot by ot-faas.service, and safe to re-run
# by hand: kubectl apply is declarative and an image already in the store is skipped.
set -eu

KUBESOLO_PATH=/var/lib/kubesolo
KUBECONFIG=$KUBESOLO_PATH/pki/admin/admin.kubeconfig
export KUBECONFIG
FAAS_DIR=/opt/ot-faas
IMAGE_DIR=/var/lib/ot-faas/images
CTR="ctr --address $KUBESOLO_PATH/containerd/containerd.sock --namespace k8s.io"

log() { echo "[ot-faas] $*"; }

# 1. Wait for the API AND for a registered node. Deliberately identical to the cell's
#    applier: `kubectl wait node --all` with no Node object yet is not a wait at all —
#    it prints "error: no matching resources found" and exits 1 at once, --timeout
#    unread — and on a first boot the object arrives after the API starts serving. So
#    wait for existence, THEN for the condition. They are two different waits.
tries=0
while [ ! -f "$KUBECONFIG" ] \
  || ! kubectl get --raw /readyz >/dev/null 2>&1 \
  || [ -z "$(kubectl get nodes -o name 2>/dev/null)" ]; do
  tries=$((tries + 1))
  if [ "$tries" -gt 120 ]; then
    echo "[ot-faas] ERROR: KubeSolo's API never came up (journalctl -u kubesolo)" >&2
    exit 1
  fi
  sleep 5
done
kubectl wait --for=condition=Ready node --all --timeout=300s >/dev/null

# 2. Load the images. There is no registry inside the plant network: these tarballs
#    ARE the image source, which is why the manifest says imagePullPolicy: Never.
while read -r ref tarball; do
  [ -n "${ref:-}" ] || continue
  if $CTR images ls -q | grep -qx "$ref"; then continue; fi
  log "importing $ref"
  # --local: a 2.x ctr routes an import through the transfer service, and KubeSolo
  # serves no streaming API for it. Already the default on the 1.7 client.
  # </dev/null so the import cannot consume the image list this loop is reading.
  $CTR images import --local "$IMAGE_DIR/$tarball" </dev/null
done < "$IMAGE_DIR/images.txt"

# 3. The gateway's admin credential. The chart generates this with a Helm HOOK, and
#    hooks do not run through `helm template` — so without this step the gateway comes
#    up with no basic-auth secret to mount and never becomes ready. Minted per host and
#    never printed: deploys here are kubectl apply, so nothing needs to know it, which
#    leaves /system/* behind a password no human holds.
kubectl create namespace openfaas --dry-run=client -o yaml | kubectl apply -f - >/dev/null
if ! kubectl -n openfaas get secret basic-auth >/dev/null 2>&1; then
  log "minting the gateway's basic-auth secret"
  _pw="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  kubectl -n openfaas create secret generic basic-auth \
    --from-literal=basic-auth-user=admin \
    --from-literal=basic-auth-password="$_pw" >/dev/null
  unset _pw
fi

# 4. Apply, then wait, so a boot that only half-worked says so in
#    `systemctl status ot-faas` instead of in front of a customer.
kubectl apply -f "$FAAS_DIR/kubesolo/openfaas.yaml"
kubectl -n openfaas rollout status deploy/gateway --timeout=300s

log "the function runtime is up (kubectl -n openfaas get pods)"
EOF
chmod 0755 "$OT_FAAS_DIR/kubesolo/apply.sh"

log "installing the ot-faas systemd unit"
cat > /etc/systemd/system/ot-faas.service <<'EOF'
[Unit]
Description=OpenFaaS on KubeSolo (the plant's function runtime for Entitle adapters)
After=kubesolo.service
Requires=kubesolo.service

[Service]
Type=oneshot
RemainAfterExit=yes
# A first boot mints the cluster's CA and node and imports the baked image tarballs,
# because there is no registry to pull from; the default 90s start timeout would kill
# that half way through and leave the broker with no runtime.
TimeoutStartSec=1200
ExecStart=/opt/ot-faas/kubesolo/apply.sh

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable ot-faas.service

log "starting the function runtime"
"$OT_FAAS_DIR/kubesolo/apply.sh" \
  || die "the function runtime did not come up — refusing to bake a broker whose \
adapters could never run"

# ── The bake's own smoke test ─────────────────────────────────────────────────
# This is where the questions get answered, on a VM that still has egress and where a
# failure costs a bake instead of a demo. Everything below is checkable HERE; what is
# left for the live run is only what genuinely needs the plant.
kubectl wait --for condition=established --timeout=120s crd/functions.openfaas.com \
  || die "the functions.openfaas.com CRD never established — the operator cannot \
reconcile Function objects, so no adapter could ever be deployed"

# Exactly one pod. A chart upgrade that re-enables NATS or Prometheus is the failure
# that quietly eats a 2-vCPU broker shared with the Entitle agent, and its first
# symptom on a cell is pods stuck Pending with the reason only in `kubectl describe`.
# Better it fails the bake.
_pods="$(kubectl -n openfaas get pods --no-headers 2>/dev/null | wc -l | tr -d ' ')"
if [ "$_pods" != "1" ]; then
  kubectl -n openfaas get pods -o wide || true
  die "the openfaas namespace runs $_pods pods, expected exactly 1 (gateway, with the \
operator as its second CONTAINER). Something values.yaml switches off is enabled — \
see the trimming note above."
fi

# Drive the whole chain through a throwaway Function on the baked image with NO
# package, so bootstrap.py serves its sentinel: gateway routing, of-watchdog, the
# loader, Python starting, and cluster DNS — all without a dashboard.
log "deploying a throwaway selftest function"
cat > /tmp/ot-faas-selftest.yaml <<EOF
apiVersion: openfaas.com/v1
kind: Function
metadata:
  name: ot-faas-selftest
  namespace: openfaas-fn
spec:
  name: ot-faas-selftest
  image: $OT_FAAS_IMAGE
  labels:
    com.openfaas.scale.min: "1"
    com.openfaas.scale.max: "1"
  requests:
    cpu: 100m
    memory: 128Mi
  limits:
    cpu: 500m
    memory: 256Mi
EOF
kubectl apply -f /tmp/ot-faas-selftest.yaml \
  || die "the operator rejected a Function object — check the CRD's apiVersion"
_waited=0
while [ "$_waited" -lt 60 ]; do
  if kubectl -n openfaas-fn get deploy ot-faas-selftest >/dev/null 2>&1; then break; fi
  _waited=$((_waited + 1))
  sleep 5
done
[ "$_waited" -lt 60 ] \
  || die "the operator never created a Deployment for the selftest Function — it is \
running but not reconciling"
kubectl -n openfaas-fn rollout status deploy/ot-faas-selftest --timeout=300s || {
  kubectl -n openfaas-fn get pods -o wide || true
  kubectl -n openfaas-fn describe deploy ot-faas-selftest | tail -n 30 || true
  kubectl -n openfaas-fn logs "deploy/ot-faas-selftest" --all-containers --tail=40 2>&1 | tail -n 40 || true
  die "the selftest function never became ready — if this is ErrImageNeverPull then \
$OT_FAAS_IMAGE is not in containerd, which the import check above should have caught"
}

# Asked from a POD, never from the host. Whether traffic leaves a pod with the node's
# address is a property of the CNI, and the caller here will be the Entitle agent —
# also a pod. The host is a different source and a different answer.
faas_probe() {
  _probe_name="$1"
  _probe_url="$2"
  kubectl -n openfaas-fn delete pod "$_probe_name" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n openfaas-fn run "$_probe_name" --image="$OT_PROBE_IMAGE" \
    --image-pull-policy=Never --restart=Never \
    --command -- sh -c "wget -qO- --timeout=20 '$_probe_url' || echo PROBE-FAILED" \
    >/dev/null 2>&1 || return 1
  _probe_waited=0
  while [ "$_probe_waited" -lt 36 ]; do
    case "$(kubectl -n openfaas-fn get pod "$_probe_name" \
            -o jsonpath='{.status.phase}' 2>/dev/null)" in
      Succeeded|Failed) break ;;
    esac
    _probe_waited=$((_probe_waited + 1))
    sleep 5
  done
  kubectl -n openfaas-fn logs "$_probe_name" 2>/dev/null
  kubectl -n openfaas-fn delete pod "$_probe_name" --ignore-not-found >/dev/null 2>&1 || true
}

log "probing the gateway and the selftest function from inside a pod"
_gw="http://gateway.openfaas.svc.cluster.local:8080"
_health="$(faas_probe ot-faas-probe-health "$_gw/healthz")"
case "$_health" in
  *PROBE-FAILED*|"") die "the gateway does not answer /healthz from a pod — cluster \
DNS or the gateway Service. Probe output: ${_health:-<empty>}" ;;
esac

_root="$(faas_probe ot-faas-probe-root "$_gw/function/ot-faas-selftest")"
case "$_root" in
  *ot-faas-selftest-ok*) log "the function answers through the gateway" ;;
  *) die "the selftest function did not answer through the gateway. Probe output: \
${_root:-<empty>}" ;;
esac

# THE routing question, and the reason it is asked here: an Entitle Remote Adapter
# distinguishes its operations by PATH (/give_access vs /revoke_access). If the
# gateway does not forward the residual path after /function/<name>, every operation
# lands on the function root and the adapter is unroutable — a design problem, not a
# configuration one, so it must surface at bake and not at a customer.
_sub="$(faas_probe ot-faas-probe-subpath "$_gw/function/ot-faas-selftest/get_assets")"
case "$_sub" in
  *'"path": "/get_assets"'*) log "the gateway forwards sub-paths (the adapter is routable)" ;;
  *ot-faas-selftest-ok*)
    die "the gateway reached the function but did NOT forward the sub-path: it saw \
$(echo "$_sub" | sed -n 's/.*\"path\": \"\([^\"]*\)\".*/\1/p'), not /get_assets. An \
Entitle adapter routes on the path, so every operation would land on the root and be \
indistinguishable. Probe output: $_sub" ;;
  *) die "the sub-path probe did not reach the function at all. Probe output: ${_sub:-<empty>}" ;;
esac

# Deleting the Function must take its Deployment with it — otherwise a revoked adapter
# leaves a pod running with the last package it was given.
log "checking the operator reconciles a deletion"
kubectl delete -f /tmp/ot-faas-selftest.yaml --ignore-not-found >/dev/null
_waited=0
while [ "$_waited" -lt 36 ]; do
  if ! kubectl -n openfaas-fn get deploy ot-faas-selftest >/dev/null 2>&1; then break; fi
  _waited=$((_waited + 1))
  sleep 5
done
[ "$_waited" -lt 36 ] \
  || die "the selftest Function was deleted but its Deployment is still there — the \
operator does not reconcile deletions, so a removed adapter would keep running"
rm -f /tmp/ot-faas-selftest.yaml
log "function runtime smoke test passed (CRD, 1 pod, gateway, sub-path routing, GC)"

fi   # OT_FAAS = openfaas

if [ "$OT_FAAS" = "none" ]; then
  log "the broker is ready: KubeSolo up, chart at $OT_ENTITLE_CHART_DIR/entitle-agent.tgz"
  log "         (OT_FAAS=none — no function runtime, so this broker cannot host the"
  log "          Entitle REST adapters; re-bake with OT_FAAS=openfaas if you need them)"
else
  log "the broker is ready: KubeSolo up, Entitle chart at \
$OT_ENTITLE_CHART_DIR/entitle-agent.tgz, OpenFaaS gateway on :8080 in-cluster"
fi

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
    # The broker's function runtime, if this image has one. Stopped BEFORE kubesolo
    # for the same reason its sibling above is: a oneshot unit that is still mid-apply
    # when the API server goes away leaves half-applied objects in the state directory
    # this block is about to delete anyway — but it also writes to the containerd store
    # it must NOT be interrupted in the middle of.
    systemctl stop ot-faas.service >/dev/null 2>&1 || true
    systemctl stop kubesolo >/dev/null 2>&1 || true
    # Stopping kubelet does NOT unmount what kubelet mounted. Every pod that ran
    # leaves a tmpfs at .../volumes/kubernetes.io~projected/kube-api-access-* holding
    # its service-account token, and those survive the process exiting — so the wipe
    # below walks into `rm: cannot remove ...: Device or resource busy` and, under
    # `set -eu`, takes the whole bake down at the last step, nine minutes in, with the
    # image otherwise finished. A mountpoint cannot be removed, only unmounted.
    #
    # Deepest-first, because a parent mount will not release while a child is still
    # mounted: reverse lexical order gives that for free, since a child path is its
    # parent plus more characters. Lazy unmount as the fallback — this host is seconds
    # from being generalized and captured, so detaching a stubborn mount is enough;
    # nothing will read through it again. Unmounting frees no layer data, so the
    # containerd store the loop below deliberately keeps survives this untouched.
    awk -v p="$KUBESOLO_PATH/" 'index($2, p) == 1 {print $2}' /proc/self/mounts \
      | sort -r \
      | while read -r _mnt; do
          umount "$_mnt" 2>/dev/null \
            || umount -l "$_mnt" 2>/dev/null \
            || log "WARNING: could not unmount $_mnt"
        done
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
    # And FUXA's signing key, for exactly the reason above one layer down. The bake
    # RUNS apply.sh to smoke-test the stack, and that mints a real key — so without
    # this reset the key generated on the build VM would ship inside the image and
    # every cell built from it would share one, making a token minted on one plant
    # valid on another. Put the placeholder back so the next first boot mints its own.
    if [ -f /var/lib/ot-sim/fuxa/mysettings.json ]; then
      log "resetting FUXA's signing key (each cell mints its own on first boot)"
      python3 - <<'PY' || log "WARNING: could not reset FUXA's signing key"
import json

path = "/var/lib/ot-sim/fuxa/mysettings.json"
with open(path, encoding="utf-8") as handle:
    settings = json.load(handle)
settings["secretCode"] = "REPLACE_ON_FIRST_BOOT"
with open(path, "w", encoding="utf-8") as handle:
    json.dump(settings, handle, indent=2, sort_keys=True)
PY
    fi
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
