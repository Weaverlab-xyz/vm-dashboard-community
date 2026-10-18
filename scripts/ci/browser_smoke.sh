#!/usr/bin/env bash
# Browser smoke test: load every page of the dashboard in Chromium and fail on any
# uncaught JavaScript error. See tests/browser_smoke.js for what it does on each page and
# why the text-level suite cannot replace it.
#
# Two instances, because some pages exist on only one install profile:
#   demo  -- every integration and preview flag on that the demo profile allows
#   pov   -- the POV profile, for /pov and its sub-pages (masked on demo)
# Each is SQLite in a temp dir, set up through the same /api/setup/complete the wizard
# uses. The page list is every GET HTMLResponse route without a path parameter (read from
# the app, so a new page is covered the day it is added), kept if it answers 200 on that
# instance -- a 404 is a page this profile does not have.
#
# Usage:
#   scripts/ci/browser_smoke.sh            # offline: external hosts blocked (Alpine is vendored)
#   REQUIRE_TAILWIND=1 scripts/ci/browser_smoke.sh   # CI: also check the pinned Tailwind loads
#
# Needs: python with web_dashboard/requirements.txt, node, and playwright (npm) with a
# Chromium it can launch. CHROMIUM_PATH overrides the browser binary.
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT="$PWD"
PY="${PYTHON:-python}"
WORK="$(mktemp -d)"
PIDS=()
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done; rm -rf "$WORK"; }
trap cleanup EXIT
PASSWORD="Smoke-Test-Passw0rd-$(date +%s)"

ROUTES="$("$PY" - <<'EOF'
import json, os, sys, tempfile
sys.path.insert(0, ".")
os.environ.setdefault("JWT_SECRET_KEY", "smoke-routes-" + "0" * 48)
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "r.db"))
from fastapi.responses import HTMLResponse
from web_dashboard.main import app
paths = sorted({r.path for r in app.routes
                if "GET" in (getattr(r, "methods", None) or ())
                and getattr(r, "response_class", None) is HTMLResponse
                and "{" not in r.path})
# One path-parameter page with a dummy id: the job detail page renders its shell for any
# id, and its state-lock card is one of the panels this test exists for.
paths.append("/jobs/00000000-0000-0000-0000-000000000000")
print(json.dumps(paths))
EOF
)"

start() {  # start NAME PORT PROFILE FEATURES_JSON [ENV=VALUE...]
  local name="$1" port="$2" profile="$3" features="$4"; shift 4
  env "$@" JWT_SECRET_KEY="smoke-$name-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')" \
      DATABASE_URL="sqlite:///$WORK/$name.db" \
      "$PY" -m uvicorn web_dashboard.main:app --port "$port" >"$WORK/$name.log" 2>&1 &
  PIDS+=($!)
  for _ in $(seq 1 90); do
    curl -s -o /dev/null "http://127.0.0.1:$port/login" && break
    sleep 1
  done
  curl -sf -X POST "http://127.0.0.1:$port/api/setup/complete" -H 'content-type: application/json' \
       -d "{\"admin\":{\"username\":\"admin\",\"password\":\"$PASSWORD\"},\"profile\":{\"install_profile\":\"$profile\"},\"aws\":{},\"azure\":{},\"features\":$features}" \
       >/dev/null || { echo "setup failed on $name"; tail -30 "$WORK/$name.log"; exit 1; }
}

token() {
  curl -sf -X POST "http://127.0.0.1:$1/api/auth/login" -d "username=admin&password=$PASSWORD" \
    | "$PY" -c "import sys, json; print(json.load(sys.stdin)['access_token'])"
}

pages_for() {  # the routes that answer 200 on this instance
  local port="$1" tok="$2" out=()
  for p in $("$PY" -c "import json,sys; print(' '.join(json.loads(sys.argv[1])))" "$ROUTES"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $tok" "http://127.0.0.1:$port$p")"
    [ "$code" = "200" ] && out+=("$p")
  done
  "$PY" -c "import json,sys; print(json.dumps(sys.argv[1:]))" "${out[@]}"
}

DEMO_FEATURES='{"remote_agents_enabled":true,"ansible_enabled":true,"proxmox_enabled":true,
  "vsphere_enabled":true,"hyperv_enabled":true,"nutanix_enabled":true,"xcpng_enabled":true,
  "password_safe_enabled":true,"pra_enabled":true,"epml_enabled":true,"portainer_enabled":true,
  "entitle_enabled":true,"cost_explorer_enabled":true,"cloud_database_enabled":true,
  "k8s_management_enabled":true,"cloud_functions_enabled":true}'
start demo 18731 demo "$(echo "$DEMO_FEATURES" | tr -d '\n ')" \
  CERT_LAB_ENABLED=true SPIRE_LAB_ENABLED=true WORKLOAD_CREDENTIALS_ENABLED=true \
  K8S_MANAGEMENT_ENABLED=true PRA_ENABLED=true NETCELL_ENABLED=true
start pov 18732 pov '{}' POV_ENVIRONMENTS_ENABLED=true

fail=0
for inst in "demo 18731" "pov 18732"; do
  set -- $inst
  tok="$(token "$2")"
  pages="$(pages_for "$2" "$tok")"
  if [ "$1" = pov ]; then  # the POV pass covers only what the demo pass cannot reach
    pages="$("$PY" -c "import json,sys; print(json.dumps([p for p in json.loads(sys.argv[1]) if p.startswith('/pov')]))" "$pages")"
  fi
  echo "== $1 pages: $pages"
  echo "== $1: $("$PY" -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$pages") pages"
  BASE="http://127.0.0.1:$2" TOKEN="$tok" PAGES="$pages" LABEL="$1" \
    ALLOW="$ROOT/tests/browser_smoke_allow.json" node "$ROOT/tests/browser_smoke.js" || fail=1
done
exit $fail
