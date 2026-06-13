#!/bin/sh
# bt-ready-debian.sh — prepare a Debian-family cloud image for BeyondTrust management.
#
# Self-elevates to root via sudo -E (AWS/GCP Packer templates invoke the shell
# provisioner as the cloud-default user, not root). POSIX sh
# only — Azure's builder forces /bin/sh (dash on Debian) regardless of shebang, so
# no [[ ]], no arrays, no <<<, no $'...'.
#
# Scope: PRA Shell Jump connectivity prereqs + a Password-Safe / Entitle SSH
# bootstrap account (adminuser) + optional EPM-L package install + conservative
# baseline hygiene. EPM-L *activation* (pbactivate) is done post-deploy with a
# short-lived installation token from the dashboard's EPM-L integration — not
# baked into the image. No host firewall. See provisioners/beyondtrust/README.md.
#
# Operator-overridable via Packer build env:
#   BT_TARGET_USER     force sudoers-target user (default: autodetect ubuntu/debian/admin)
#   BT_ADMIN_USER      Entitle/Password-Safe bootstrap account name (default: adminuser)
#   BT_ENTITLE_PUBKEY  Entitle integration SSH public key → adminuser authorized_keys
#   BT_EPML_URL        presigned URL to the EPM-L .deb; set to install (activation is Ansible's job)
#   BT_AUTOPATCH=1     enable unattended-upgrades on the built image
#   BT_SKIP_UPDATES=1  skip dist-upgrade (faster iteration builds)
#   BT_SKIP_CLEANUP=1  skip image-reuse cleanup (keep host keys, machine-id, logs)
#   BT_APPLY_CIS=1     run OpenSCAP remediation with a CIS profile (Ubuntu only)
#   BT_CIS_PROFILE     override the default profile id (default: cis_level1_server)

# Self-elevate. AWS and GCP Packer templates invoke the shell provisioner as
# the cloud-default SSH user (ubuntu/ec2-user), not root. Azure's template
# already wraps with `sudo -E sh`, so we end up re-exec'd-as-root there too.
# Re-exec under sudo -E to preserve BT_* env overrides through the elevation.
if [ "$(id -u)" -ne 0 ]; then
  exec sudo -E sh "$0" "$@"
fi

set -eu

log() { echo "[bt-ready] $*"; }
die() { echo "[bt-ready] ERROR: $*" >&2; exit 1; }

# ── 1. OS-family gate ────────────────────────────────────────────────────────
[ -f /etc/debian_version ] || die "not a Debian-family system (no /etc/debian_version) — use bt-ready-rpm.sh"
log "starting bt-ready on $(cat /etc/debian_version 2>/dev/null || echo unknown) ($(uname -m))"

# ── 2. Resolve the BT target user ────────────────────────────────────────────
resolve_user() {
  if [ -n "${BT_TARGET_USER:-}" ]; then
    if id -u "$BT_TARGET_USER" >/dev/null 2>&1; then
      echo "$BT_TARGET_USER"; return 0
    fi
    die "BT_TARGET_USER='$BT_TARGET_USER' does not exist on this image"
  fi
  for candidate in ubuntu debian admin; do
    if id -u "$candidate" >/dev/null 2>&1; then
      echo "$candidate"; return 0
    fi
  done
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ] && id -u "$SUDO_USER" >/dev/null 2>&1; then
    echo "$SUDO_USER"; return 0
  fi
  die "could not resolve a BT target user — set BT_TARGET_USER to an existing username"
}
BT_USER="$(resolve_user)"
log "BT target user: $BT_USER"

# ── 3. System updates ────────────────────────────────────────────────────────
if [ "${BT_SKIP_UPDATES:-0}" = "1" ]; then
  log "BT_SKIP_UPDATES=1 — skipping dist-upgrade"
else
  log "applying security + bugfix updates (this may take a while)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get -y -q -o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef dist-upgrade
  apt-get -y -q autoremove
  apt-get -y -q clean
fi

# ── 4. Optional CIS/STIG remediation via OpenSCAP ────────────────────────────
# Runs BEFORE the BT sshd drop-in so our settings (in 00-bt-ready.conf, loaded
# lex-first by sshd) still win for the directives we care about. Skip on
# Debian proper — SCAP Security Guide ships no CIS profile for Debian, only
# ANSSI; operators who want ANSSI can set BT_CIS_PROFILE explicitly.
if [ "${BT_APPLY_CIS:-0}" = "1" ]; then
  . /etc/os-release 2>/dev/null || true
  CIS_DS=""
  case "${ID:-}" in
    ubuntu)
      PROFILE="${BT_CIS_PROFILE:-xccdf_org.ssgproject.content_profile_cis_level1_server}"
      apt-get -y -q install libopenscap8 ssg-base ssg-debderived 2>/dev/null || \
        apt-get -y -q install libopenscap8 ssg-debderived || \
        apt-get -y -q install libopenscap8 scap-security-guide-ubuntu 2>/dev/null || true
      for c in /usr/share/xml/scap/ssg/content/ssg-ubuntu*-ds.xml; do
        [ -f "$c" ] && CIS_DS="$c" && break
      done
      ;;
    debian)
      if [ -n "${BT_CIS_PROFILE:-}" ]; then
        PROFILE="$BT_CIS_PROFILE"
        apt-get -y -q install libopenscap8 ssg-debian 2>/dev/null || \
          apt-get -y -q install libopenscap8 ssg-debderived || true
        for c in /usr/share/xml/scap/ssg/content/ssg-debian*-ds.xml; do
          [ -f "$c" ] && CIS_DS="$c" && break
        done
      else
        log "warn: SCAP Security Guide has no CIS profile for Debian — set BT_CIS_PROFILE to an ANSSI profile to opt in, or run this on Ubuntu"
      fi
      ;;
    *)
      log "warn: BT_APPLY_CIS=1 on unsupported ID=${ID:-unknown}; skipping"
      ;;
  esac
  if [ -n "$CIS_DS" ] && [ -n "${PROFILE:-}" ]; then
    # Accept short names (e.g. "cis_level2_server") by prepending the SSG namespace.
    case "$PROFILE" in
      xccdf_*) ;;
      *)      PROFILE="xccdf_org.ssgproject.content_profile_$PROFILE" ;;
    esac
    log "applying SCAP profile $PROFILE against $CIS_DS"
    mkdir -p /var/log/bt-ready
    # --remediate exits non-zero when any rule fails to apply; we tolerate that
    # because some rules are environment-specific (e.g. AIDE init) and would
    # otherwise abort the entire build.
    oscap xccdf eval --remediate \
      --profile "$PROFILE" \
      --results-arf /var/log/bt-ready/cis-arf.xml \
      --report /var/log/bt-ready/cis-report.html \
      "$CIS_DS" || log "warn: oscap exited non-zero; see /var/log/bt-ready/cis-report.html on the AMI for the rule-by-rule audit"
  fi
fi

# ── 5. sshd hardening for PRA Shell Jump ─────────────────────────────────────
# Written as 00-bt-ready.conf so it's loaded LEX-FIRST by sshd. sshd uses
# first-occurrence-wins semantics for conflicting directives, so ours win
# even when CIS drops 00-complianceascode-hardening.conf alongside it.
log "writing /etc/ssh/sshd_config.d/00-bt-ready.conf"
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/00-bt-ready.conf <<'EOF'
# Managed by bt-ready provisioner. PRA Shell Jump connectivity prereqs.
# Loaded lex-first so these directives win against any 50-* / 99-* drop-ins.
PasswordAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
ChallengeResponseAuthentication no
KbdInteractiveAuthentication no
UsePAM yes
ClientAliveInterval 60
ClientAliveCountMax 3
EOF
chmod 0644 /etc/ssh/sshd_config.d/00-bt-ready.conf
sshd -t || die "sshd config validation failed after writing 00-bt-ready.conf"
systemctl enable ssh >/dev/null 2>&1 || systemctl enable sshd >/dev/null 2>&1 || true

# ── 5. Sudoers for the BT target user ────────────────────────────────────────
log "writing /etc/sudoers.d/90-bt-ready for $BT_USER"
SUDOERS=/etc/sudoers.d/90-bt-ready
cat > "$SUDOERS" <<EOF
# Managed by bt-ready provisioner. Password-Safe-friendly NOPASSWD sudo.
$BT_USER ALL=(ALL) NOPASSWD: ALL
EOF
chmod 0440 "$SUDOERS"
if ! visudo -c -f "$SUDOERS" >/dev/null; then
  rm -f "$SUDOERS"
  die "visudo rejected 90-bt-ready — sudoers not installed"
fi

# ── adminuser — Password Safe / Entitle SSH bootstrap account ─────────────────
# A dedicated account Password Safe manages (onboarded out-of-band) and that
# serves as the Entitle "SSH ephemeral accounts" bootstrap user: Entitle SSHes in
# as this user (with its private key) and runs useradd/userdel to create + remove
# the temporary per-grant users. See:
#   https://docs.beyondtrust.com/entitle/docs/entitle-integration-ssh_ephemeral_accounts
BT_ADMIN_USER="${BT_ADMIN_USER:-adminuser}"
log "creating Password-Safe / Entitle bootstrap user: $BT_ADMIN_USER"
if ! id -u "$BT_ADMIN_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$BT_ADMIN_USER"
fi

# Scoped NOPASSWD sudo — exactly the commands Entitle ephemeral-accounts needs,
# nothing more. Resolve absolute paths (visudo wants real paths; locations differ
# across distros) via command -v.
ENT_CMDS="cat chmod chown mkdir mv rm sed tee useradd userdel"
CMNDLIST=""
for c in $ENT_CMDS; do
  p="$(command -v "$c" 2>/dev/null || true)"
  if [ -z "$p" ]; then
    log "warn: command '$c' not found on PATH — Entitle ephemeral accounts may need it"
    continue
  fi
  if [ -z "$CMNDLIST" ]; then CMNDLIST="$p"; else CMNDLIST="$CMNDLIST, $p"; fi
done
[ -n "$CMNDLIST" ] || die "could not resolve any Entitle sudo commands — refusing to write an empty sudoers"
ADMIN_SUDOERS=/etc/sudoers.d/91-bt-adminuser
log "writing $ADMIN_SUDOERS (scoped NOPASSWD for Entitle ephemeral accounts)"
cat > "$ADMIN_SUDOERS" <<EOF
# Managed by bt-ready provisioner. Scoped NOPASSWD sudo for Entitle SSH
# ephemeral accounts — least privilege, only the commands Entitle runs.
$BT_ADMIN_USER ALL=(root) NOPASSWD: $CMNDLIST
EOF
chmod 0440 "$ADMIN_SUDOERS"
if ! visudo -c -f "$ADMIN_SUDOERS" >/dev/null; then
  rm -f "$ADMIN_SUDOERS"
  die "visudo rejected 91-bt-adminuser — sudoers not installed"
fi

# Entitle's integration SSH PUBLIC key → adminuser authorized_keys. Entitle holds
# the matching private key in its Connection JSON. (Password Safe onboards +
# rotates this account out-of-band — not done here.)
if [ -n "${BT_ENTITLE_PUBKEY:-}" ]; then
  log "installing Entitle public key into $BT_ADMIN_USER authorized_keys"
  ADMIN_HOME="$(getent passwd "$BT_ADMIN_USER" | cut -d: -f6)"
  [ -n "$ADMIN_HOME" ] || ADMIN_HOME="/home/$BT_ADMIN_USER"
  install -d -m 0700 -o "$BT_ADMIN_USER" -g "$BT_ADMIN_USER" "$ADMIN_HOME/.ssh"
  printf '%s\n' "$BT_ENTITLE_PUBKEY" > "$ADMIN_HOME/.ssh/authorized_keys"
  chown "$BT_ADMIN_USER:$BT_ADMIN_USER" "$ADMIN_HOME/.ssh/authorized_keys"
  chmod 0600 "$ADMIN_HOME/.ssh/authorized_keys"
else
  log "warn: BT_ENTITLE_PUBKEY unset — $BT_ADMIN_USER created but Entitle SSH cannot connect until its public key is installed"
fi

# ── EPM-L package install (opt-in via BT_EPML_URL) ───────────────────────────
# Install ONLY. EPM-L activation (pbactivate -t <token>) is performed post-deploy
# using a short-lived installation token from the dashboard's EPM-L integration
# (/api/epml/token); tokens must not be baked into the image.
if [ -n "${BT_EPML_URL:-}" ]; then
  log "downloading + installing EPM-L package (Debian) from BT_EPML_URL"
  if ! command -v curl >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q && apt-get -y -q install curl
  fi
  curl -fsSL -o /tmp/epml.deb "$BT_EPML_URL" || die "EPM-L package download failed from BT_EPML_URL"
  export DEBIAN_FRONTEND=noninteractive
  apt-get -y -q install /tmp/epml.deb || { dpkg -i /tmp/epml.deb || true; apt-get -y -q -f install; }
  rm -f /tmp/epml.deb
  log "EPM-L installed (NOT activated) — activate post-deploy with a token from the EPM-L integration"
else
  log "BT_EPML_URL unset — skipping EPM-L install (the EPM-L integration handles activation separately)"
fi

# ── 6. Time sync ─────────────────────────────────────────────────────────────
log "enabling systemd-timesyncd"
systemctl enable --now systemd-timesyncd >/dev/null 2>&1 || \
  log "warn: systemd-timesyncd not available; clock sync left to image defaults"

# ── 7. Baseline hygiene ──────────────────────────────────────────────────────
if [ "${BT_AUTOPATCH:-0}" = "1" ]; then
  log "BT_AUTOPATCH=1 — installing unattended-upgrades"
  apt-get -y -q install unattended-upgrades
  cat > /etc/apt/apt.conf.d/52bt-autoupdate <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
Unattended-Upgrade::Allowed-Origins {
        "${distro_id}:${distro_codename}-security";
        "${distro_id}ESMApps:${distro_codename}-apps-security";
        "${distro_id}ESM:${distro_codename}-infra-security";
};
Unattended-Upgrade::Automatic-Reboot "false";
EOF
fi

log "enabling persistent journald"
mkdir -p /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
systemctl restart systemd-journald >/dev/null 2>&1 || true

# ── 8. Image cleanup for re-launch ───────────────────────────────────────────
if [ "${BT_SKIP_CLEANUP:-0}" = "1" ]; then
  log "BT_SKIP_CLEANUP=1 — leaving host keys, machine-id, and logs in place"
else
  log "cleaning ssh host keys, machine-id, cloud-init state, logs"
  rm -f /etc/ssh/ssh_host_*
  truncate -s 0 /etc/machine-id
  # Some distros (Amazon Linux 2023, minimal Ubuntu) don't have /var/lib/dbus
  # populated — dbus-daemon reads /etc/machine-id directly. Only recreate
  # the legacy symlink where the dir actually exists, otherwise `ln -sf`
  # aborts the build under set -e.
  if [ -d /var/lib/dbus ]; then
    rm -f /var/lib/dbus/machine-id
    ln -sf /etc/machine-id /var/lib/dbus/machine-id
  fi
  rm -rf /var/lib/cloud/instances /var/lib/cloud/instance
  find /var/log -type f -name 'cloud-init*.log' -exec truncate -s 0 {} + 2>/dev/null || true
  find /var/log -type f -name '*.log' -exec truncate -s 0 {} + 2>/dev/null || true
  rm -f /root/.bash_history
  find /home -maxdepth 2 -name '.bash_history' -exec rm -f {} + 2>/dev/null || true
fi

log "bt-ready provisioning complete on $(hostname): user=$BT_USER"
