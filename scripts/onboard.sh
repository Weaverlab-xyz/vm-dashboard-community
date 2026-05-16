#!/usr/bin/env bash
# One-command onboarding for the Infrastructure Management Dashboard
# (Community Edition) on macOS and Linux.
#
# Mirrors scripts/Onboard-Dashboard.ps1 for Windows: preflight, .env
# bootstrap, secret auto-gen, `docker compose up`, health poll, browser open.
#
# Usage:
#   ./scripts/onboard.sh            # normal run
#   ./scripts/onboard.sh --build    # force image rebuild
#   ./scripts/onboard.sh --no-open  # skip opening the browser

set -euo pipefail

BUILD=0
NO_OPEN=0
for arg in "$@"; do
    case "$arg" in
        --build) BUILD=1 ;;
        --no-open) NO_OPEN=1 ;;
        -h|--help)
            sed -n '2,12p' "$0"
            exit 0
            ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# Repo root is the parent of the directory holding this script.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
ENV_EXAMPLE="$REPO_ROOT/.env.example"
COMPOSE_FILE="$REPO_ROOT/docker-compose.yml"
HEALTH_URL="http://localhost:8001/api/health"
DASHBOARD_URL="http://localhost:8001"

# ANSI color helpers (no-op when stdout is not a TTY).
if [[ -t 1 ]]; then
    C_STEP=$'\033[36m'; C_OK=$'\033[32m'; C_WARN=$'\033[33m'
    C_FAIL=$'\033[31m'; C_RESET=$'\033[0m'
else
    C_STEP=""; C_OK=""; C_WARN=""; C_FAIL=""; C_RESET=""
fi
step()  { printf "%s==>%s %s\n" "$C_STEP" "$C_RESET" "$1"; }
ok()    { printf "    %s%s%s\n" "$C_OK"   "$1" "$C_RESET"; }
warn()  { printf "    %s%s%s\n" "$C_WARN" "$1" "$C_RESET"; }
fail()  { printf "    %s%s%s\n" "$C_FAIL" "$1" "$C_RESET" >&2; }

# ── 1. Preflight ────────────────────────────────────────────────────────
step "Checking prerequisites"

# Detect WSL early — used for daemon hint and browser open.
_is_wsl=0
if grep -qEi "(microsoft|wsl)" /proc/version 2>/dev/null; then _is_wsl=1; fi

for cmd in git docker curl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        fail "'$cmd' not found on PATH."
        case "$cmd" in
            docker)
                if (( _is_wsl )); then
                    fail "Install Docker in WSL (pick one):"
                    fail "  Option A — distro package (no Cloudflare/CDN required):"
                    fail "    sudo apt update && sudo apt install -y docker.io docker-compose-plugin"
                    fail "    sudo usermod -aG docker \$USER && newgrp docker"
                    fail "  Option B — Docker Engine (official upstream):"
                    fail "    https://docs.docker.com/engine/install/ubuntu/"
                else
                    fail "Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
                fi
                ;;
            git)  fail "Install git (macOS: 'xcode-select --install'; Linux: use your package manager)" ;;
            curl) fail "Install curl via your package manager." ;;
        esac
        exit 1
    fi
    ok "$cmd found"
done

if ! docker info --format '{{.ServerVersion}}' >/dev/null 2>&1; then
    fail "Docker daemon is not responding."
    if (( _is_wsl )); then
        fail "Start the Docker daemon with one of:"
        fail "  sudo service docker start        (WSL without systemd)"
        fail "  sudo systemctl start docker      (WSL with systemd enabled)"
        fail "Then rerun this script."
    else
        fail "Is Docker Desktop running? Start it, wait for the whale icon to settle, then rerun."
    fi
    exit 1
fi
ok "Docker daemon responding"

# Detect which Compose variant is available.
# docker.io (distro package) ships the compose plugin separately as docker-compose-plugin;
# standalone docker-compose (v1) also works.
if docker compose version >/dev/null 2>&1; then
    _compose_cmd="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    _compose_cmd="docker-compose"
    warn "Using standalone docker-compose (v1). Consider installing docker-compose-plugin for v2."
else
    fail "Compose not found. Install it with one of:"
    fail "  sudo apt install -y docker-compose-plugin   (v2 plugin, recommended)"
    fail "  sudo apt install -y docker-compose          (v1 standalone)"
    exit 1
fi
ok "Compose: $_compose_cmd"

if [[ ! -f "$COMPOSE_FILE" ]]; then
    fail "docker-compose.yml not found at $COMPOSE_FILE."
    fail "Run this script from a clone of the dashboard repository."
    exit 1
fi

# ── 2. Bootstrap .env ───────────────────────────────────────────────────
step "Checking .env"

if [[ ! -f "$ENV_FILE" ]]; then
    if [[ ! -f "$ENV_EXAMPLE" ]]; then
        fail ".env.example is missing from the repo — cannot bootstrap .env."
        exit 1
    fi
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    ok "Created .env from .env.example"
fi
ok ".env exists"

# Read a value from .env (ignores comments and blanks). Empty if unset.
read_env() {
    local key="$1"
    awk -F= -v k="$key" '
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*$/ { next }
        $1 == k { sub(/^[^=]*=/, ""); print; exit }
    ' "$ENV_FILE"
}

# Replace or append KEY=VALUE in .env. Uses a temp file + mv for atomicity.
set_env() {
    local key="$1" value="$2"
    local tmp; tmp="$(mktemp)"
    awk -F= -v k="$key" -v v="$value" '
        BEGIN { done = 0 }
        $1 == k && !/^[[:space:]]*#/ { print k "=" v; done = 1; next }
        { print }
        END { if (!done) print k "=" v }
    ' "$ENV_FILE" > "$tmp"
    mv "$tmp" "$ENV_FILE"
}

new_hex() {
    # $1 = number of bytes. Falls back to /dev/urandom if openssl missing.
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex "$1"
    else
        head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'
    fi
}

# ── 3. Generate JWT secret key file ────────────────────────────────────
# The JWT key is the root of trust for DB-encrypted integration credentials.
# It lives in .jwt_secret_key (Docker secret mount) rather than .env so
# the rest of .env is safe to inspect or share for debugging.
step "Checking JWT secret key"

JWT_KEY_FILE="$REPO_ROOT/.jwt_secret_key"
if [[ ! -f "$JWT_KEY_FILE" ]]; then
    new_hex 32 > "$JWT_KEY_FILE"
    chmod 600 "$JWT_KEY_FILE"
    ok "Generated .jwt_secret_key (chmod 600)"
else
    ok ".jwt_secret_key already exists"
fi

# ── 4. Auto-generate remaining bootstrap secrets ────────────────────────
step "Auto-generating bootstrap secrets"

changed=0
if [[ "$(read_env POSTGRES_PASSWORD)" == "REPLACE_ME_WITH_STRONG_PASSWORD" || -z "$(read_env POSTGRES_PASSWORD)" ]]; then
    set_env POSTGRES_PASSWORD "$(new_hex 16)"; ok "Generated POSTGRES_PASSWORD"; changed=1
fi
(( changed )) || ok "Bootstrap secrets already set"

# ── 4. Bring up the stack ───────────────────────────────────────────────
step "Starting Docker Compose stack"

compose_args=(-f "$COMPOSE_FILE" up -d)
(( BUILD )) && compose_args+=(--build)

if ! $_compose_cmd "${compose_args[@]}"; then
    fail "$_compose_cmd up failed."
    fail "Recent logs:"
    $_compose_cmd -f "$COMPOSE_FILE" logs --tail 50 app || true
    exit 1
fi
ok "Containers started"

# ── 6. Wait for health endpoint ─────────────────────────────────────────
step "Waiting for health endpoint ($HEALTH_URL)"

ready=0
for i in $(seq 1 90); do
    sleep 1
    if curl -fsS -o /dev/null --max-time 2 "$HEALTH_URL"; then
        ready=1
        ok "Healthy after ${i}s"
        break
    fi
done

if ! (( ready )); then
    fail "Health endpoint did not respond within 90 seconds."
    fail "Recent app logs:"
    $_compose_cmd -f "$COMPOSE_FILE" logs --tail 50 app || true
    echo
    warn "Common causes:"
    warn "  - Invalid AWS or Azure credentials (app crashes at startup)"
    warn "  - Port 8001 already in use by another process"
    warn "  - Database container still initializing — try rerunning in ~30s"
    exit 1
fi

# ── 7. Open the browser ─────────────────────────────────────────────────
echo
ok "Dashboard is up at $DASHBOARD_URL"
if ! (( NO_OPEN )); then
    if (( _is_wsl )); then
        # Open in the Windows-side browser. Try wslu (wslview) first,
        # then fall back to cmd.exe /c start which always works on WSL2.
        if command -v wslview >/dev/null 2>&1; then
            wslview "$DASHBOARD_URL" 2>/dev/null || true
        else
            /mnt/c/Windows/System32/cmd.exe /c "start $DASHBOARD_URL" 2>/dev/null || \
                warn "Could not open browser automatically. Navigate to $DASHBOARD_URL in your Windows browser."
        fi
    elif command -v open >/dev/null 2>&1; then
        open "$DASHBOARD_URL" || true           # macOS
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$DASHBOARD_URL" >/dev/null 2>&1 || true  # Linux with X11/Wayland
    fi
fi

echo
printf "%sNext steps:%s\n" "$C_STEP" "$C_RESET"
echo "  - The browser setup wizard will open automatically on first launch."
echo "    Complete it to create your admin account and enter cloud credentials."
echo "  - See docs/ONBOARDING.md for the full feature-test checklist."
echo "  - Stop the stack with:  $_compose_cmd -f docker-compose.yml down"
