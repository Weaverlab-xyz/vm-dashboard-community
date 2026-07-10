#!/bin/sh
# Arch-selecting launcher for the Google Compute Engine guest agent.
#
# The promote runner bakes BOTH the amd64 and arm64 guest_agent binaries into
# the image offline (it can't know the guest's arch at bake time), then installs
# this launcher as /usr/bin/google_guest_agent — the path the systemd unit's
# ExecStart points at. At guest boot `uname -m` is the real arch, so we exec the
# matching binary. `exec` replaces this shell, so systemd's Type=notify handshake
# (NOTIFY_SOCKET) is inherited by the real binary unchanged.
case "$(uname -m)" in
  x86_64 | amd64)
    exec /usr/lib/google-guest-agent/amd64/google_guest_agent "$@"
    ;;
  aarch64 | arm64)
    exec /usr/lib/google-guest-agent/arm64/google_guest_agent "$@"
    ;;
  *)
    echo "google_guest_agent: no bundled binary for arch $(uname -m)" >&2
    exit 1
    ;;
esac
