#!/usr/bin/env bash
# Consolidated onboarding: provision the chosen cloud sandbox(es) and push the
# resulting config straight into the dashboard's setup API — so you skip the
# /setup wizard entirely.
#
# It runs the existing per-cloud bootstrappers (which use your local
# aws/az/gcloud SSO), reads the config.json each one writes, merges them, and
# POSTs to /api/setup/import (creating the admin + marking setup complete on a
# fresh stack, or merging with admin auth if setup is already done).
#
# Usage: ./scripts/sandbox/Linux/onboard-sandbox.sh [options]
#   --cloud LIST          aws,azure,gcp or "all"        (prompted if omitted)
#   --dashboard-url URL   dashboard base URL            (default http://localhost:8001)
#   --admin-user NAME     admin username to create/login (prompted if needed)
#   --admin-pass PASS     admin password                 (prompted, hidden, if needed)
#   --token TOKEN         admin JWT for re-runs when setup is already complete
#   --push-only           skip provisioning; just push cached config.json files
#   --no-push             provision + write config.json, but don't call the API
#   -h, --help            show this help

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

DASHBOARD_URL="http://localhost:8001"
CLOUDS="" ADMIN_USER="" ADMIN_PASS="" TOKEN="" PUSH_ONLY=0 NO_PUSH=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cloud)         CLOUDS="$2"; shift 2 ;;
    --dashboard-url) DASHBOARD_URL="${2%/}"; shift 2 ;;
    --admin-user)    ADMIN_USER="$2"; shift 2 ;;
    --admin-pass)    ADMIN_PASS="$2"; shift 2 ;;
    --token)         TOKEN="$2"; shift 2 ;;
    --push-only)     PUSH_ONLY=1; shift ;;
    --no-push)       NO_PUSH=1; shift ;;
    -h|--help)       usage 0 ;;
    *) err "unknown arg: $1"; usage 2 ;;
  esac
done

require_cmd jq
(( NO_PUSH )) || require_cmd curl

# ── Resolve cloud list ──────────────────────────────────────────────────────
if [[ -z "$CLOUDS" ]]; then
  read -r -p "Which clouds to provision? [all] (comma list of aws,azure,gcp): " CLOUDS
  CLOUDS="${CLOUDS:-all}"
fi
[[ "$CLOUDS" == "all" ]] && CLOUDS="aws,azure,gcp"
IFS=',' read -r -a CLOUD_ARR <<<"$CLOUDS"
for c in "${CLOUD_ARR[@]}"; do
  case "$c" in aws|azure|gcp) ;; *) die "unknown cloud: '$c' (expected aws|azure|gcp|all)";; esac
done

# ── 1. Provision (unless --push-only) ───────────────────────────────────────
if (( ! PUSH_ONLY )); then
  for c in "${CLOUD_ARR[@]}"; do
    section "Provisioning $c sandbox"
    "$SCRIPT_DIR/setup-$c.sh" || die "setup-$c.sh failed for '$c'."
  done
fi

# ── 2. Merge each cloud's config.json ───────────────────────────────────────
cfgfiles=()
for c in "${CLOUD_ARR[@]}"; do
  f="$(state_dir "$c")/config.json"
  if [[ -f "$f" ]]; then cfgfiles+=("$f"); else warn "no config.json for $c (skipped)"; fi
done
(( ${#cfgfiles[@]} )) || die "No config.json found. Provision first (drop --push-only)."
CONFIG_JSON="$(jq -s 'reduce .[] as $o ({}; . + $o)' "${cfgfiles[@]}")"
ok "Merged $(jq 'length' <<<"$CONFIG_JSON") config keys from ${#cfgfiles[@]} cloud(s)."

if (( NO_PUSH )); then
  ok "Skipping API push (--no-push). Cached config: ${SANDBOX_STATE_DIR:-$HOME/.dashboard-sandbox}/<cloud>/config.json"
  exit 0
fi

# ── 3. Push to the dashboard setup API ──────────────────────────────────────
section "Pushing config to $DASHBOARD_URL"
status="$(curl -fsS "$DASHBOARD_URL/api/setup/status" 2>/dev/null || true)"
[[ -n "$status" ]] || die "Cannot reach $DASHBOARD_URL/api/setup/status — is the dashboard running and reachable?"
complete="$(jq -r '.complete // false' <<<"$status")"

post_import() {  # $1 = JSON payload, $2 = optional bearer token
  local payload="$1" tok="${2:-}" args
  args=(-fsS -X POST "$DASHBOARD_URL/api/setup/import" -H "Content-Type: application/json" --data-binary @-)
  [[ -n "$tok" ]] && args+=(-H "Authorization: Bearer $tok")
  printf '%s' "$payload" | curl "${args[@]}"
}

if [[ "$complete" == "true" ]]; then
  info "Dashboard is already set up — merging config (admin auth required)."
  if [[ -z "$TOKEN" ]]; then
    [[ -n "$ADMIN_USER" ]] || read -r -p "Admin username: " ADMIN_USER
    [[ -n "$ADMIN_PASS" ]] || { read -rs -p "Admin password: " ADMIN_PASS; echo; }
    TOKEN="$(curl -fsS -X POST "$DASHBOARD_URL/api/auth/login" \
              --data-urlencode "username=$ADMIN_USER" \
              --data-urlencode "password=$ADMIN_PASS" | jq -r '.access_token // empty')"
    [[ -n "$TOKEN" ]] || die "Login failed (no access_token). Check the admin credentials or pass --token."
  fi
  payload="$(jq -n --argjson cfg "$CONFIG_JSON" '{config:$cfg}')"
  resp="$(post_import "$payload" "$TOKEN")" || die "Import failed."
else
  info "First-run setup — creating the admin and applying config."
  [[ -n "$ADMIN_USER" ]] || { read -r -p "New admin username [admin]: " ADMIN_USER; ADMIN_USER="${ADMIN_USER:-admin}"; }
  [[ -n "$ADMIN_PASS" ]] || { read -rs -p "New admin password: " ADMIN_PASS; echo; }
  [[ -n "$ADMIN_PASS" ]] || die "Admin password is required for first-run setup."
  payload="$(jq -n --argjson cfg "$CONFIG_JSON" --arg u "$ADMIN_USER" --arg p "$ADMIN_PASS" \
              '{admin_username:$u, admin_password:$p, config:$cfg}')"
  resp="$(post_import "$payload")" || die "Import failed (already set up? re-run with --token)."
fi

ok "Config imported ($(jq -r '.keys_written // "?"' <<<"$resp") keys written)."
ok "Done — open $DASHBOARD_URL and log in. No wizard needed."
