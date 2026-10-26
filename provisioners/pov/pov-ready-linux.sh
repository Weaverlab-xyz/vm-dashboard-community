#!/bin/sh
# pov-ready-linux.sh — the POV half of the Standard Linux image (Ubuntu 24.04).
#
# Runs as a Packer shell provisioner BEFORE bt-ready-debian.sh, which does the
# BeyondTrust account/sshd work and then seals the image (host keys, machine-id,
# cloud-init state). Anything here that must survive into every instance is
# installed, never left running state the seal would have to clean.
#
# What it adds, and why each one is in the IMAGE rather than done at boot:
#   * Docker CE + compose plugin. The control node's bootstrap runs the dashboard
#     agent, the PRA Gateway and the Ansible runner as containers; it can install
#     Docker itself, but that is the slowest step of a first boot and the most
#     likely to fail behind a customer's egress filtering.
#   * cloud-init kept enabled. A control node gets its bootstrap as user-data, and
#     a standard target gets its login the same way (pov_cloud_aws) — nothing else
#     on a POV can reach a guest before the agent is up.
#   * SSH password login for the admin account ONLY. bt-ready turns password auth
#     off globally (PRA Shell Jump prefers keys), but a POV's Password Safe
#     onboarding and Entitle agent install log in with the per-POV password the
#     dashboard sets at launch. A Match block scopes that to the one account.
#   * Optional pre-pulled control-node containers (POV_PREPULL_IMAGES).
#
# Env (set by services/pov_image_recipes):
#   POV_ADMIN_USER      the account allowed password login (default: adminuser)
#   POV_PREPULL_IMAGES  space-separated images to `docker pull` into the image
#
# POSIX sh only (dash on Ubuntu).

if [ "$(id -u)" -ne 0 ]; then
  exec sudo -E sh "$0" "$@"
fi

set -eu

log() { echo "[pov-ready] $*"; }
die() { echo "[pov-ready] ERROR: $*" >&2; exit 1; }

ADMIN_USER="${POV_ADMIN_USER:-adminuser}"
case "$ADMIN_USER" in
  *[!a-z0-9_-]*|'') die "POV_ADMIN_USER '$ADMIN_USER' is not a plain lowercase account name" ;;
esac

[ -f /etc/debian_version ] || die "not a Debian-family system — the standard Linux image is Ubuntu"

export DEBIAN_FRONTEND=noninteractive

# Wait out the first-boot apt run cloud-init starts on a fresh Ubuntu AMI, or every
# apt-get below races it for the dpkg lock.
log "waiting for cloud-init to finish first boot"
cloud-init status --wait >/dev/null 2>&1 || true

# ── 1. Docker CE + compose plugin, from Docker's own repository ───────────────
if ! command -v docker >/dev/null 2>&1; then
  log "installing Docker CE"
  apt-get update -y
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-noble}")"
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' \
    "$(dpkg --print-architecture)" "$CODENAME" > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable docker >/dev/null 2>&1 || true
systemctl start docker
docker version >/dev/null || die "docker installed but the daemon is not answering"

# ── 2. cloud-init stays on ───────────────────────────────────────────────────
# Some hardening guides disable it; a POV cannot work without it. bt-ready's seal
# clears its per-instance state so it runs again on every launch.
rm -f /etc/cloud/cloud-init.disabled
systemctl enable cloud-init >/dev/null 2>&1 || true

# ── 3. Password login for the admin account only ─────────────────────────────
# 99- so it is read after bt-ready's 00- drop-in; a Match block in an included file
# ends at that file's end (OpenSSH >= 8.4; Ubuntu 24.04 ships 9.6), so it cannot
# leak into any other directive. The account itself is created by bt-ready.
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-pov-ready.conf <<EOF
# Managed by pov-ready. Password login for the POV's managed admin account only:
# the dashboard sets its password per POV at launch (cloud-init user-data).
Match User ${ADMIN_USER}
    PasswordAuthentication yes
    KbdInteractiveAuthentication no
EOF
chmod 0644 /etc/ssh/sshd_config.d/99-pov-ready.conf
sshd -t || die "sshd rejected 99-pov-ready.conf"

# ── 4. Optional: pre-pull the control-node containers ────────────────────────
if [ -n "${POV_PREPULL_IMAGES:-}" ]; then
  for IMAGE in $POV_PREPULL_IMAGES; do
    log "pre-pulling $IMAGE"
    # A pull failure is not a build failure: the bootstrap pulls again at boot.
    docker pull "$IMAGE" || log "warn: could not pre-pull $IMAGE; the control node will pull it at boot"
  done
fi

# Docker's own state that must not ship: no containers ran, but the daemon may have
# written an engine id.
rm -f /etc/docker/key.json

log "pov-ready complete — bt-ready-debian.sh runs next and seals the image"
