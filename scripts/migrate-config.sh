#!/usr/bin/env bash
# Move dashboard Settings configuration from one instance to another — every key
# in the encrypted app_config store plus the notification endpoints edited in
# Settings → Notifications.
#
# A pg_dump of app_config does NOT work: values are Fernet-encrypted with a key
# derived from JWT_SECRET_KEY, so a restore into a second instance yields
# ciphertext it cannot read — and _decrypt swallows the failure, so the app
# serves the ciphertext with no error and no log line. This moves plaintext and
# lets the target re-encrypt, through POST /api/setup/import.
#
# Usage: ./scripts/migrate-config.sh <command> [options]
#   export  --source URL  [--out PATH] [--via http|docker] [--include-on-prem]
#   diff    --bundle PATH --target URL [--only PREFIX]
#   import  --bundle PATH --target URL --apply [--only PREFIX]
#
#   --admin-user NAME     admin username        (env DASHBOARD_ADMIN_USER)
#   --admin-password PW   admin password        (env DASHBOARD_ADMIN_PASSWORD; prompted)
#   --token JWT           existing admin JWT, instead of logging in
#   --ca-bundle PATH      PEM bundle for a TLS-inspecting corporate proxy
#   --regions merge|replace   merge (default) keeps region sets the target has
#   -h, --help            show this help
#
# `--via docker` execs into the local container. It is the only way to capture
# the four values GET /api/setup/config redacts and the only way to read webhook
# URLs at all, so prefer it when migrating away from a Compose instance.
#
# Examples:
#   ./scripts/migrate-config.sh export --source http://localhost:8001 --via docker
#   ./scripts/migrate-config.sh diff   --bundle ~/.dashboard-migrate/bundle-*.json \
#                                      --target https://dash.example.com
#   ./scripts/migrate-config.sh import --bundle ~/.dashboard-migrate/bundle-*.json \
#                                      --target https://dash.example.com --apply

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() { sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# Colour-free when not a TTY, matching scripts/onboard.sh.
if [[ -t 2 ]]; then
  err() { printf "\033[0;31m✗\033[0m %s\n" "$*" >&2; }
else
  err() { printf "✗ %s\n" "$*" >&2; }
fi
die() { err "$*"; exit 1; }

[[ $# -gt 0 ]] || usage 2
case "$1" in
  -h|--help) usage 0 ;;
  export|diff|import|export-local) ;;
  *) err "unknown command: $1"; usage 2 ;;
esac

# Prefer python3; fall back to python (some distros ship only the unsuffixed name).
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
[[ -n "$PY" ]] || die "python3 not found on PATH."

# The tool is stdlib-only by design, so no virtualenv is needed — but it does
# import web_dashboard.services.region_config for the region field names, which
# means the repo root has to be importable.
cd "$REPO_ROOT"
exec "$PY" -m web_dashboard.scripts.config_migrate "$@"
