#!/bin/sh
# vyos-cell.sh — bake a demo network device (VyOS router/firewall) into a VyOS image.
#
# The network cell is the one demo target in this repo that is not a general-purpose
# host. It exists so the emergency-rule-change story can be told against something
# that really is a firewall: a rep opens a recorded PRA Shell Jump, runs `configure`,
# adds a drop rule, `commit`s and `save`s it, and the rule is live. Nothing is
# simulated — the CLI, the commit and the saved config are VyOS's own.
#
# What the built image carries:
#   * a demo administrator account (VYOS_ADMIN_USER, default `adminuser`) in the
#     `admin` level, so it can enter configuration mode. This is the account the
#     cloud's VM deploy path onboards into Password Safe.
#   * SSH on :22, which is the whole of Layer 1 here — a firewall needs a Shell Jump
#     and nothing else, so the cell provisions no Web Jump and no protocol tunnel.
#   * a baseline firewall posture worth changing: a named ruleset (VYOS_RULESET,
#     default `BLOCKLIST`) attached to forwarded traffic, default-accept and EMPTY.
#     The demo's `set firewall ... rule 10 action drop` is then the first rule in a
#     ruleset that is already wired in, so the change takes effect on commit rather
#     than needing a second, off-story step to attach it.
#
# Everything is written into /config/config.boot, which is a real writable partition
# on VyOS and survives image capture. That is deliberate and not a preference: the
# deploy paths in this repo have NO user-data hook (see provisioners/ot/README.md for
# the same constraint stated from the OT side), so a cell's configuration cannot be
# injected at first boot the way a stock VyOS cloud image expects. It is baked.
#
# Self-elevates to root via sudo -E (Packer invokes the shell provisioner as the
# cloud-default user, which on a VyOS image is `vyos`). POSIX sh only — no [[ ]],
# no arrays, no <<<.
#
# Operator-overridable via Packer build env:
#   VYOS_ADMIN_USER   the Password-Safe-managed demo account (default: adminuser).
#                     Matches the OT image's OT_ADMIN_USER so the two cells onboard
#                     under the same name.
#   VYOS_ADMIN_PUBKEY the SSH public key to bake onto that account, as the whole
#                     "ssh-rsa AAAA... comment" line. STRONGLY RECOMMENDED: VyOS does
#                     not create users from a cloud's key-injection the way a stock
#                     Linux image does, so an account baked with neither a key nor a
#                     password is an account nothing can log into — including the
#                     Shell Jump. Bake the public half of the key pair your PRA
#                     Gateway presents, or set VYOS_ADMIN_PASSWORD instead.
#   VYOS_ADMIN_PASSWORD  initial password for that account. Default: unset. Use it
#                     when your Password Safe functional account rotates passwords
#                     rather than keys; see README.md, "How the credential is managed".
#                     At least one of PUBKEY or PASSWORD should be set — the script
#                     warns loudly when neither is.
#   VYOS_HOSTNAME     the device's hostname (default: vyos-cell). Shows in the prompt,
#                     so it is what the audience reads in the session recording.
#   VYOS_RULESET      name of the baseline ruleset the demo appends to
#                     (default: BLOCKLIST)
#   VYOS_WAN_IF       the cloud-facing interface (default: eth0)
#   VYOS_SYNTAX       firewall syntax to emit: `auto` (default), `1.4` or `1.3`.
#                     VyOS reorganised the firewall tree in 1.4 — `firewall ipv4 name`
#                     and a `forward filter` hook, where 1.3 had `firewall name` and
#                     per-interface `firewall in`. `auto` picks by looking for the 1.4
#                     template directory, which is the only reliable on-box signal
#                     before a config session exists.
#
# Leaves the image's own `vyos` account untouched. That account is the cloud image's
# break-glass path, and removing it would make a failed Password Safe onboarding
# unrecoverable rather than merely broken.

if [ "$(id -u)" -ne 0 ]; then
  exec sudo -E sh "$0" "$@"
fi

set -eu

VYOS_ADMIN_USER="${VYOS_ADMIN_USER:-adminuser}"
VYOS_ADMIN_PUBKEY="${VYOS_ADMIN_PUBKEY:-}"
VYOS_ADMIN_PASSWORD="${VYOS_ADMIN_PASSWORD:-}"
VYOS_HOSTNAME="${VYOS_HOSTNAME:-vyos-cell}"
VYOS_RULESET="${VYOS_RULESET:-BLOCKLIST}"
VYOS_WAN_IF="${VYOS_WAN_IF:-eth0}"
VYOS_SYNTAX="${VYOS_SYNTAX:-auto}"

log() { echo "[vyos-cell] $*"; }

# ── Refuse anything that is not VyOS ─────────────────────────────────────────
# A shell provisioner pointed at the wrong source image would otherwise run to
# completion here and produce a Debian box with a user on it, which the deploy form
# would accept and the demo would fail on in front of someone.
if [ ! -x /opt/vyatta/sbin/my_set ] || [ ! -f /opt/vyatta/etc/functions/script-template ]; then
  echo "[vyos-cell] FATAL: this is not a VyOS image (no /opt/vyatta config tooling)." >&2
  echo "[vyos-cell] Point the build at a VyOS source image — see provisioners/net/README.md." >&2
  exit 1
fi

# ── Which firewall syntax this release speaks ────────────────────────────────
if [ "$VYOS_SYNTAX" = "auto" ]; then
  if [ -d /opt/vyatta/share/vyatta-cfg/templates/firewall/ipv4 ]; then
    VYOS_SYNTAX="1.4"
  else
    VYOS_SYNTAX="1.3"
  fi
fi
log "firewall syntax: $VYOS_SYNTAX"

# ── The configuration session ────────────────────────────────────────────────
# Written out and then run under `sg vyattacfg`, because a VyOS config session needs
# the vyattacfg group even when the caller is root — `my_set` writes into a per-group
# scratch tree and silently has nothing to commit without it.
# Split "ssh-rsa AAAA... comment" into the two fields VyOS stores separately. Done
# here rather than inline so a malformed key fails BEFORE a config session opens --
# a commit that dies halfway leaves the image in a state the bake cannot describe.
PUBKEY_TYPE=""
PUBKEY_BODY=""
if [ -n "$VYOS_ADMIN_PUBKEY" ]; then
  PUBKEY_TYPE=$(echo "$VYOS_ADMIN_PUBKEY" | awk '{print $1}')
  PUBKEY_BODY=$(echo "$VYOS_ADMIN_PUBKEY" | awk '{print $2}')
  if [ -z "$PUBKEY_TYPE" ] || [ -z "$PUBKEY_BODY" ]; then
    echo "[vyos-cell] FATAL: VYOS_ADMIN_PUBKEY is not an 'ssh-... AAAA... [comment]' line." >&2
    exit 1
  fi
  case "$PUBKEY_TYPE" in
    ssh-rsa|ssh-dss|ssh-ed25519|ecdsa-sha2-*) ;;
    *)
      echo "[vyos-cell] FATAL: '$PUBKEY_TYPE' is not an SSH key type VyOS accepts." >&2
      exit 1
      ;;
  esac
fi

if [ -z "$VYOS_ADMIN_PUBKEY" ] && [ -z "$VYOS_ADMIN_PASSWORD" ]; then
  echo "[vyos-cell] WARNING: '$VYOS_ADMIN_USER' is being baked with NO key and NO password." >&2
  echo "[vyos-cell] Nothing will be able to log in as it -- the Shell Jump included." >&2
  echo "[vyos-cell] Set VYOS_ADMIN_PUBKEY (preferred) or VYOS_ADMIN_PASSWORD." >&2
fi

CFG=/tmp/vyos-cell-configure.sh

{
  echo '#!/bin/vbash'
  echo 'source /opt/vyatta/etc/functions/script-template'
  echo 'set -e'
  echo 'configure'
  echo "set system host-name '$VYOS_HOSTNAME'"
  echo 'set service ssh port 22'

  # The demo account. `admin` level is what lets it enter configuration mode; an
  # operator-level account could open a recorded session and change nothing, which
  # would make the recording the only thing the demo proves.
  echo "set system login user '$VYOS_ADMIN_USER' level admin"
  if [ -n "$VYOS_ADMIN_PASSWORD" ]; then
    echo "set system login user '$VYOS_ADMIN_USER' authentication plaintext-password '$VYOS_ADMIN_PASSWORD'"
  fi
  if [ -n "$VYOS_ADMIN_PUBKEY" ]; then
    # VyOS keeps authorized keys IN CONFIGURATION, split into type and body, and
    # regenerates ~/.ssh/authorized_keys from it on commit. Writing the file directly
    # would therefore be undone by the next commit of `system login` -- so the key has
    # to go in as config or not at all.
    echo "set system login user '$VYOS_ADMIN_USER' authentication public-keys 'default' type '$PUBKEY_TYPE'"
    echo "set system login user '$VYOS_ADMIN_USER' authentication public-keys 'default' key '$PUBKEY_BODY'"
  fi

  # The baseline ruleset. Default-accept and empty on purpose: the cell is a demo
  # device reached over SSH through a Gateway, and a default-drop forward policy baked
  # into an image nobody can log into yet is a support call, not a safety feature.
  # What the demo adds is the first DROP rule in a ruleset that is already attached.
  if [ "$VYOS_SYNTAX" = "1.4" ]; then
    echo "set firewall ipv4 name '$VYOS_RULESET' default-action 'accept'"
    echo "set firewall ipv4 name '$VYOS_RULESET' description 'Demo blocklist — the emergency rule change lands here'"
    echo "set firewall ipv4 forward filter rule 10 action 'jump'"
    echo "set firewall ipv4 forward filter rule 10 jump-target '$VYOS_RULESET'"
  else
    echo "set firewall name '$VYOS_RULESET' default-action 'accept'"
    echo "set firewall name '$VYOS_RULESET' description 'Demo blocklist — the emergency rule change lands here'"
    echo "set interfaces ethernet '$VYOS_WAN_IF' firewall in name '$VYOS_RULESET'"
  fi

  echo 'commit'
  echo 'save'
  echo 'exit'
} > "$CFG"

chmod +x "$CFG"

log "applying configuration"
sg vyattacfg -c "/bin/vbash $CFG"
rm -f "$CFG"

# ── What the deploy path and the tests rely on ───────────────────────────────
# A marker file, read by nothing at run time but by a human during triage: it answers
# "is this image actually a network cell, and baked with what?" without a config
# session. tests/test_netcell_provisioner.py pins that the script writes it.
mkdir -p /opt/netcell
{
  echo "admin_user=$VYOS_ADMIN_USER"
  echo "hostname=$VYOS_HOSTNAME"
  echo "ruleset=$VYOS_RULESET"
  echo "auth=$([ -n "$VYOS_ADMIN_PUBKEY" ] && printf pubkey || printf none)$([ -n "$VYOS_ADMIN_PASSWORD" ] && printf +password || printf "")"
  echo "syntax=$VYOS_SYNTAX"
  echo "baked_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > /opt/netcell/IMAGE.txt

log "verifying the ruleset committed"
if ! grep -q "$VYOS_RULESET" /config/config.boot; then
  echo "[vyos-cell] FATAL: '$VYOS_RULESET' is not in /config/config.boot — the commit did not persist." >&2
  exit 1
fi

if ! grep -q "$VYOS_ADMIN_USER" /config/config.boot; then
  echo "[vyos-cell] FATAL: '$VYOS_ADMIN_USER' is not in /config/config.boot — the account did not persist." >&2
  exit 1
fi

log "done — /config/config.boot carries the demo account and the '$VYOS_RULESET' ruleset"
