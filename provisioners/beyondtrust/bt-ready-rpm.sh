#!/bin/sh
# bt-ready-rpm.sh — prepare an RPM-family cloud image for BeyondTrust management.
#
# Self-elevates to root via sudo -E (AWS/GCP Packer templates invoke the shell
# provisioner as the cloud-default user, not root). POSIX sh
# only — Azure's builder forces /bin/sh regardless of shebang, so no [[ ]], no
# arrays, no <<<, no $'...'.
#
# Scope: PRA Shell Jump connectivity prereqs + a Password-Safe / Entitle SSH
# bootstrap account (adminuser) + optional EPM-L package install + conservative
# baseline hygiene. EPM-L *activation* (pbactivate) is done post-deploy with a
# short-lived installation token from the dashboard's EPM-L integration — not
# baked into the image. No host firewall. See provisioners/beyondtrust/README.md.
#
# Targets: RHEL, Rocky, CentOS Stream, AlmaLinux, Amazon Linux 2 / 2023.
#
# Operator-overridable via Packer build env:
#   BT_TARGET_USER     force sudoers-target user (default: autodetect ec2-user/rocky/centos/almalinux/cloud-user)
#   BT_ADMIN_USER      Password-Safe-managed bootstrap account name (default: adminuser)
#   BT_SEED_ADMIN_KEY=1 seed adminuser's authorized_keys with a throwaway key so the
#                      AWS Systems Manager Custom Plugin has one to rotate (private half discarded)
#   BT_EPML_URL        presigned URL to the EPM-L .rpm; set to install (activation is Ansible's job)
#   BT_AUTOPATCH=1     enable dnf-automatic on the built image
#   BT_SKIP_UPDATES=1  skip security upgrade (faster iteration builds)
#   BT_SKIP_CLEANUP=1  skip image-reuse cleanup (keep host keys, machine-id, logs)
#   BT_APPLY_CIS=1     run OpenSCAP remediation with a CIS/STIG profile
#   BT_CIS_PROFILE     override the default profile id (defaults to CIS L1 Server)

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
is_rpm_family() {
  [ -f /etc/redhat-release ] && return 0
  [ -f /etc/system-release ] && return 0
  if [ -f /etc/os-release ]; then
    . /etc/os-release
    case "${ID:-}${ID_LIKE:-}" in
      *rhel*|*rocky*|*centos*|*almalinux*|*amzn*|*fedora*) return 0 ;;
    esac
  fi
  return 1
}
is_rpm_family || die "not an RPM-family system — use bt-ready-debian.sh"
. /etc/os-release 2>/dev/null || true
log "starting bt-ready on ${PRETTY_NAME:-unknown} ($(uname -m))"

# Detect the package manager up front; some recipes diverge dnf vs yum.
if command -v dnf >/dev/null 2>&1; then
  PKG=dnf
elif command -v yum >/dev/null 2>&1; then
  PKG=yum
else
  die "neither dnf nor yum found — cannot manage packages"
fi
log "package manager: $PKG"

# ── 2. Resolve the BT target user ────────────────────────────────────────────
resolve_user() {
  if [ -n "${BT_TARGET_USER:-}" ]; then
    if id -u "$BT_TARGET_USER" >/dev/null 2>&1; then
      echo "$BT_TARGET_USER"; return 0
    fi
    die "BT_TARGET_USER='$BT_TARGET_USER' does not exist on this image"
  fi
  for candidate in ec2-user rocky centos almalinux cloud-user; do
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
  log "BT_SKIP_UPDATES=1 — skipping security upgrade"
else
  log "applying security updates (this may take a while)"
  # --security on RHEL/Rocky requires the security metadata; if it's missing
  # (Amazon Linux 2023 didn't ship it for a while) fall through to full upgrade.
  if ! $PKG -y --security upgrade 2>/dev/null; then
    log "warn: --security upgrade unavailable, running full upgrade"
    $PKG -y upgrade
  fi
  $PKG -y autoremove >/dev/null 2>&1 || true
  $PKG clean all >/dev/null 2>&1 || true
fi

# ── 4. Optional CIS/STIG remediation via OpenSCAP ────────────────────────────
# Runs BEFORE the BT sshd drop-in so our settings (in 00-bt-ready.conf, loaded
# lex-first by sshd) still win for the directives we care about. Profile
# defaults per-distro; override with BT_CIS_PROFILE (e.g. the STIG profile
# xccdf_org.ssgproject.content_profile_stig).
if [ "${BT_APPLY_CIS:-0}" = "1" ]; then
  CIS_DS=""
  PROFILE="${BT_CIS_PROFILE:-}"
  case "${ID:-}" in
    rhel|rocky|almalinux|centos)
      [ -n "$PROFILE" ] || PROFILE=xccdf_org.ssgproject.content_profile_cis_server_l1
      $PKG -y install openscap-scanner scap-security-guide
      # ssg-rhelN-ds.xml on RHEL; rocky/alma also ship under their own id but
      # SSG falls back to the RHEL data stream — try the distro first, then RHEL.
      for c in /usr/share/xml/scap/ssg/content/ssg-${ID}*-ds.xml \
               /usr/share/xml/scap/ssg/content/ssg-rhel${VERSION_ID%%.*}-ds.xml \
               /usr/share/xml/scap/ssg/content/ssg-rhel*-ds.xml; do
        [ -f "$c" ] && CIS_DS="$c" && break
      done
      ;;
    amzn)
      # Amazon Linux 2 has a single 'cis' profile; AL2023 lacks SSG coverage.
      case "${VERSION_ID:-}" in
        2) [ -n "$PROFILE" ] || PROFILE=xccdf_org.ssgproject.content_profile_cis ;;
        *) log "warn: SSG coverage on Amazon Linux ${VERSION_ID} is incomplete; skipping CIS"; PROFILE="" ;;
      esac
      if [ -n "$PROFILE" ]; then
        $PKG -y install openscap-scanner scap-security-guide || \
          log "warn: scap-security-guide not available on this image; skipping"
        for c in /usr/share/xml/scap/ssg/content/ssg-amazon_linux*-ds.xml; do
          [ -f "$c" ] && CIS_DS="$c" && break
        done
      fi
      ;;
    *)
      log "warn: BT_APPLY_CIS=1 on unsupported ID=${ID:-unknown}; skipping"
      ;;
  esac
  if [ -n "$CIS_DS" ] && [ -n "$PROFILE" ]; then
    # Accept short names (e.g. "stig", "cis_server_l2") by prepending the SSG namespace.
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
systemctl enable sshd >/dev/null 2>&1 || true

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

# ── adminuser — Password Safe bootstrap account ───────────────────────────────
# A dedicated account Password Safe manages (onboarded + key/password rotated
# out-of-band). The scoped NOPASSWD sudo below is the least-privilege command set
# for SSH "ephemeral accounts" style management.
#
# NOTE: the dashboard's Entitle SSH-ephemeral-accounts integration no longer uses
# this account. Entitle connects as the **cloud-default user** with the VM's own
# launch keypair (the key cloud-init injects at boot), so no separate Entitle
# public key is baked into the image. Point entitle_ssh_sudo_user at that user.
BT_ADMIN_USER="${BT_ADMIN_USER:-adminuser}"
log "creating Password-Safe bootstrap user: $BT_ADMIN_USER"
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

# adminuser SSH key seed (opt-in: BT_SEED_ADMIN_KEY=1).
# Password Safe's AWS Systems Manager Custom Plugin ROTATES an existing key in
# place — it does NOT bootstrap ~/.ssh/authorized_keys. For that model the image
# must ship a placeholder key the plugin can rotate on its first Change Password;
# with nothing seeded, the plugin has nothing to replace and the account never
# becomes SSH-reachable. Generate a throwaway keypair and DISCARD the private half
# immediately (it never leaves the build), so the seeded key grants no standing
# access — it exists only to give the plugin something to rotate. Off by default
# so Entitle / cloud-default-user images are unaffected.
if [ "${BT_SEED_ADMIN_KEY:-0}" = "1" ]; then
  if ! command -v ssh-keygen >/dev/null 2>&1; then
    log "BT_SEED_ADMIN_KEY=1 but ssh-keygen missing — installing openssh"
    { command -v dnf >/dev/null 2>&1 && dnf install -y -q openssh; } || \
    { command -v yum >/dev/null 2>&1 && yum install -y -q openssh; } || true
  fi
  if command -v ssh-keygen >/dev/null 2>&1; then
    ADMIN_HOME="$(getent passwd "$BT_ADMIN_USER" | cut -d: -f6)"
    [ -n "$ADMIN_HOME" ] || ADMIN_HOME="/home/$BT_ADMIN_USER"
    SEED_DIR="$(mktemp -d)"
    ssh-keygen -t ed25519 -N "" -C "bt-ready-seed (rotate me)" -f "$SEED_DIR/seed" >/dev/null
    install -d -m 700 -o "$BT_ADMIN_USER" -g "$BT_ADMIN_USER" "$ADMIN_HOME/.ssh"
    install -m 600 -o "$BT_ADMIN_USER" -g "$BT_ADMIN_USER" \
      "$SEED_DIR/seed.pub" "$ADMIN_HOME/.ssh/authorized_keys"
    rm -rf "$SEED_DIR"   # discard the throwaway private key — never leaves the build
    log "seeded $ADMIN_HOME/.ssh/authorized_keys with a throwaway key (private half discarded) — Password Safe rotates it on first Change Password"
  else
    log "warn: ssh-keygen unavailable — could not seed $BT_ADMIN_USER authorized_keys"
  fi
else
  # Not seeded (default): Password Safe manages the key out-of-band. If you use the
  # AWS Systems Manager Custom Plugin (rotate-in-place), set BT_SEED_ADMIN_KEY=1 so
  # the image ships a placeholder key for it to rotate.
  log "not seeding $BT_ADMIN_USER SSH key (set BT_SEED_ADMIN_KEY=1 for the AWS SSM Custom Plugin)"
fi

# ── EPM-L package install (opt-in via BT_EPML_URL) ───────────────────────────
# Install ONLY. EPM-L activation (pbactivate -t <token>) is performed post-deploy
# using a short-lived installation token from the dashboard's EPM-L integration
# (/api/epml/token); tokens must not be baked into the image.
if [ -n "${BT_EPML_URL:-}" ]; then
  log "downloading + installing EPM-L package (RPM) from BT_EPML_URL"
  command -v curl >/dev/null 2>&1 || $PKG -y install curl
  curl -fsSL -o /tmp/epml.rpm "$BT_EPML_URL" || die "EPM-L package download failed from BT_EPML_URL"
  $PKG -y install /tmp/epml.rpm || rpm -i /tmp/epml.rpm
  # EPM-L needs libcrypt.so.1 on newer RHEL/Rocky/Alma (shipped by libxcrypt-compat).
  ldconfig -p | grep -q 'libcrypt.so.1' || $PKG -y install libxcrypt-compat || \
    log "warn: libxcrypt-compat unavailable; install it if EPM-L reports a missing libcrypt.so.1"
  rm -f /tmp/epml.rpm
  log "EPM-L installed (NOT activated) — activate post-deploy with a token from the EPM-L integration"
else
  log "BT_EPML_URL unset — skipping EPM-L install (the EPM-L integration handles activation separately)"
fi

# ── 6. Time sync ─────────────────────────────────────────────────────────────
log "ensuring chrony is installed and running"
if ! command -v chronyd >/dev/null 2>&1; then
  $PKG -y install chrony
fi
systemctl enable --now chronyd >/dev/null 2>&1 || \
  log "warn: chronyd would not start; clock sync left to image defaults"

# ── 7. Baseline hygiene ──────────────────────────────────────────────────────
if [ "${BT_AUTOPATCH:-0}" = "1" ]; then
  log "BT_AUTOPATCH=1 — installing dnf-automatic"
  if [ "$PKG" = "dnf" ]; then
    $PKG -y install dnf-automatic
    AUTO_CONF=/etc/dnf/automatic.conf
    if [ -f "$AUTO_CONF" ]; then
      sed -i 's/^upgrade_type *=.*/upgrade_type = security/' "$AUTO_CONF"
      sed -i 's/^apply_updates *=.*/apply_updates = yes/' "$AUTO_CONF"
    fi
    systemctl enable --now dnf-automatic.timer >/dev/null 2>&1 || true
  else
    log "warn: BT_AUTOPATCH=1 set but $PKG has no dnf-automatic equivalent — install yum-cron manually if needed"
  fi
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
  # Amazon Linux 2023 (and minimal RHEL/Rocky images) don't ship
  # /var/lib/dbus — dbus-daemon reads /etc/machine-id directly. Only
  # recreate the legacy symlink where the dir actually exists, otherwise
  # `ln -sf` aborts the build under set -e.
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
