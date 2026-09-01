#!/usr/bin/env bash
# Shared helpers for the dashboard sandbox bootstrappers.
# Source this from each setup-*.sh and rollback.sh.

set -Eeuo pipefail

# ── Tagging convention ─────────────────────────────────────────────────────────
# Every resource the sandbox creates carries this tag. Rollback enumerates by
# tag rather than relying on a state file, so a lost ./.state file or a re-run
# never strands resources.
SANDBOX_TAG_KEY="managed-by"
SANDBOX_TAG_VALUE="dashboard-sandbox"
# Overridable per run, because a second REGION needs a second sandbox: nothing the
# setup scripts create is region-scoped by name (RG, VNet, VPC, cluster, …), and
# re-running with a different region against the same prefix quietly reuses the
# first region's network — see the location guard in setup-azure.sh. Pair it with
# SANDBOX_STATE_DIR, which is keyed on cloud alone and would otherwise be shared.
# The TAG deliberately does not follow: rollback enumerates by tag, and one
# `managed-by` value should still find every region's resources.
SANDBOX_NAME_PREFIX="${SANDBOX_NAME_PREFIX:-dashboard-sandbox}"

# ── Logging ────────────────────────────────────────────────────────────────────
_now() { date -u +"%H:%M:%S"; }
info()   { printf "\033[0;36m[%s]\033[0m %s\n"      "$(_now)" "$*" >&2; }
ok()     { printf "\033[0;32m[%s] ✓\033[0m %s\n"    "$(_now)" "$*" >&2; }
warn()   { printf "\033[0;33m[%s] !\033[0m %s\n"    "$(_now)" "$*" >&2; }
err()    { printf "\033[0;31m[%s] ✗\033[0m %s\n"    "$(_now)" "$*" >&2; }
section(){ printf "\n\033[1;35m── %s\033[0m\n"      "$*" >&2; }

die() { err "$*"; exit 1; }

# ── Retry with backoff ──────────────────────────────────────────────────────────
# Re-run a command that fails transiently. Cloud control planes are eventually
# consistent: a resource a create call just returned can be briefly invisible to
# the next call. Cases this guards, one per provider:
#   • GCP   — a fresh service account isn't resolvable as an IAM policy member,
#             so `add-iam-policy-binding` fails "Service account … does not exist".
#   • AWS   — a fresh IAM user/role isn't ready, so `attach-*-policy` /
#             `put-role-policy` fails "NoSuchEntity".
#   • Azure — a fresh service principal isn't visible to ARM RBAC, so
#             `role assignment create` fails "PrincipalNotFound".
#
# ONLY wrap idempotent ops — attach/put/binding/assignment grants and read-only
# lookups, where a re-run is a harmless no-op. NEVER wrap a non-idempotent create
# (create-access-key, create-for-rbac, …): a retry after a lost response would
# mint a duplicate. Such creates are instead gated by a wrapped op that touches
# the same principal first (e.g. attach-user-policy gates create-access-key).
#
# stdout streams through untouched, so `VALUE="$(retry … cmd)"` still captures it
# (callers that don't want it redirect with >/dev/null as usual). The command's
# stderr is buffered and printed only if every attempt fails, so the transient
# errors don't scare anyone — but a genuinely fatal final error is preserved.
#   retry <attempts> <delay_seconds> <command> [args…]
retry() {
  local attempts="$1" delay="$2"; shift 2
  local n=1 rc errfile
  errfile="$(mktemp)"
  while :; do
    # `|| rc=$?` captures the real exit code under set -e (reading $? after an
    # `if cmd; then…fi` would see 0, since a no-branch-taken if exits 0).
    rc=0
    "$@" 2>"$errfile" || rc=$?
    if (( rc == 0 )); then
      rm -f "$errfile"
      return 0
    fi
    if (( n >= attempts )); then
      cat "$errfile" >&2
      rm -f "$errfile"
      return "$rc"
    fi
    warn "$1: attempt $n/$attempts failed; retrying in ${delay}s…"
    sleep "$delay"
    n=$((n + 1))
  done
}

# ── Prereq checks ──────────────────────────────────────────────────────────────
require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$1 not found on PATH. Run scripts/sandbox/00-prereqs.sh first."
}

require_supported_os() {
  # WSL/Linux/macOS are all supported — the cloud CLIs and helpers used
  # here are portable. The /proc/version probe is kept for diagnostics only.
  local os; os="$(uname -s)"
  case "$os" in
    Linux|Darwin) return 0 ;;
    *) die "These scripts target Linux, WSL, or macOS. Detected: $os." ;;
  esac
}

# Confirms (and caches) the cloud CLI is logged in. Each CLI has its own
# auth check; wrap the per-cloud probe inside the setup script.
ensure_logged_in() {
  local cli="$1" probe_cmd="$2" hint="$3"
  if ! eval "$probe_cmd" >/dev/null 2>&1; then
    die "$cli is installed but not authenticated. $hint"
  fi
}

# ── Output: dashboard config block ─────────────────────────────────────────────
# At the end of each setup script, print a block of key=value pairs the user
# pastes into the /setup wizard or Settings → Integrations panels. Keys
# correspond to web_dashboard/config.py / api/setup.py field names.
print_dashboard_config() {
  local title="$1"; shift
  printf "\n\033[1;32m═══════════════════════════════════════════════════════════════\033[0m\n" >&2
  printf "\033[1;32m  %s — paste into /setup or Settings → Integrations\033[0m\n" "$title" >&2
  printf "\033[1;32m═══════════════════════════════════════════════════════════════\033[0m\n\n" >&2
  for kv in "$@"; do
    printf "%s\n" "$kv"
  done
  printf "\n"
}

# Machine-readable twin of print_dashboard_config: write the same key=value
# pairs to $(state_dir <cloud>)/config.json so the consolidated onboarder
# (onboard-sandbox.sh) can merge them and POST to /api/setup/import. JSON (not
# a .env) keeps values that contain '=' or embedded JSON
# (gcp_service_account_json) intact. Splits each pair on the FIRST '='.
write_config_json() {
  local cloud="$1"; shift
  command -v jq >/dev/null 2>&1 || { warn "jq not found — skipping config.json for $cloud"; return 0; }
  local d obj kv key val
  d="$(state_dir "$cloud")"
  obj='{}'
  for kv in "$@"; do
    [[ "$kv" == *"="* ]] || continue            # skip blank / comment-only lines
    key="${kv%%=*}"
    val="${kv#*=}"
    # Strip a trailing "   # human comment" (only when whitespace precedes '#').
    if [[ "$val" =~ ^(.*[^[:space:]])[[:space:]]+#.*$ ]]; then val="${BASH_REMATCH[1]}"; fi
    # Trim whitespace from key (config keys never contain spaces) and value.
    key="$(printf '%s' "$key" | tr -d '[:space:]')"
    val="${val#"${val%%[![:space:]]*}"}"; val="${val%"${val##*[![:space:]]}"}"
    [[ -n "$key" ]] || continue
    [[ "$val" == "…" ]] && continue             # skip "paste manually" placeholders
    obj="$(jq -c --arg k "$key" --arg v "$val" '. + {($k): $v}' <<<"$obj")"
  done
  printf '%s\n' "$obj" > "$d/config.json"
  chmod 600 "$d/config.json" 2>/dev/null || true
  info "Wrote $d/config.json ($(jq 'length' <<<"$obj") keys)"
}

# ── State file (optional cache) ────────────────────────────────────────────────
# Tag-based rollback is the source of truth, but we also drop a state file as
# a fast-path hint for users who want to know what was created.
state_dir() {
  local cloud="$1"
  local d="${SANDBOX_STATE_DIR:-$HOME/.dashboard-sandbox}/$cloud"
  mkdir -p "$d"
  printf "%s" "$d"
}

state_write() {
  local cloud="$1" key="$2" value="$3"
  local d; d="$(state_dir "$cloud")"
  printf "%s\n" "$value" > "$d/$key"
}

state_read() {
  local cloud="$1" key="$2"
  local d; d="$(state_dir "$cloud")"
  [[ -f "$d/$key" ]] && cat "$d/$key" || true
}

state_clear() {
  local cloud="$1"
  rm -rf "${SANDBOX_STATE_DIR:-$HOME/.dashboard-sandbox}/$cloud"
}

# ── Confirm prompt (for destructive ops) ───────────────────────────────────────
confirm() {
  local prompt="$1" reply
  read -r -p "$prompt [y/N]: " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}
