#!/usr/bin/env bash
# pip-audit: fail when an installed Python package has a published vulnerability.
#
# Audits the INSTALLED environment, not requirements.txt as text: several requirements are
# ranges or floors, and only the resolved versions can be matched against advisories. Run it
# with the interpreter whose packages you want audited (CI: the job's venv, built from
# web_dashboard/requirements.txt exactly as the tests job builds it).
#
# Accepted advisories live in .pip-audit-ignore, each with the reason it does not apply.
# An entry with no reason, or for an advisory no installed package has any more, fails the
# run, so the list cannot quietly grow or go stale.
#
# Usage:
#   scripts/ci/pip_audit.sh                       # audits `python` on PATH
#   PYTHON=/path/to/venv/bin/python scripts/ci/pip_audit.sh

set -uo pipefail
cd "$(dirname "$0")/../.."
PY="${PYTHON:-$(command -v python)}"
IGNORE_FILE=.pip-audit-ignore

ignores=()
bad=0
while IFS= read -r line; do
  [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
  id=$(awk '{print $1}' <<<"$line")
  reason=$(sed -n 's/^[^#]*#[[:space:]]*//p' <<<"$line")
  if [ -z "$reason" ]; then
    echo "::error file=$IGNORE_FILE::$id has no reason; say why it does not apply here"
    bad=1
  fi
  ignores+=("$id")
done < "$IGNORE_FILE"

args=()
for id in "${ignores[@]}"; do args+=(--ignore-vuln "$id"); done

# pip-audit itself may live in another environment; PIPAPI_PYTHON_LOCATION points it at the
# interpreter being audited.
export PIPAPI_PYTHON_LOCATION="$PY"
echo "auditing the packages installed for $PY"
pip-audit --progress-spinner off --desc on "${args[@]}"
rc=$?

# Stale entries: an ignored advisory that no installed package carries any more.
present=$(pip-audit --progress-spinner off -f json 2>/dev/null \
  | python3 -c 'import json,sys
d=json.load(sys.stdin)
for p in d["dependencies"]:
    for v in p.get("vulns", []):
        print(v["id"])
        print("\n".join(v.get("aliases", [])))')
for id in "${ignores[@]}"; do
  if ! grep -qxF "$id" <<<"$present"; then
    echo "::error file=$IGNORE_FILE::$id is ignored but no installed package has it any more; remove the entry"
    bad=1
  fi
done

[ "$rc" -eq 0 ] && [ "$bad" -eq 0 ]
