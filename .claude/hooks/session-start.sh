#!/bin/bash
# Claude Code cloud sessions: give the session the interpreter CI uses, so the suite runs.
#
# CI and the Dockerfile are on Python 3.12. The cloud image's default python3 is newer,
# and on it SQLAlchemy 2.0.27 fails at import and psycopg2-binary has no wheel -- so every
# test that imports the app fails locally while passing in CI. This builds a 3.12 venv from
# web_dashboard/requirements.txt and puts it first on PATH for the session.
#
# Deliberately WITHOUT pytest, because CI has none: it runs each tests/test_*.py as a
# script, and a file whose imports fail takes a different exit path with pytest present
# (pytest.skip -> exit 1) than without (print SKIP -> exit 0). Matching CI means the
# session sees exactly the result CI will.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"
VENV="$CLAUDE_PROJECT_DIR/.venv"

PY=""
for candidate in python3.12 /usr/bin/python3.12 /usr/local/bin/python3.12; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
  echo "session-start: python3.12 not found; tests that import the app may fail" >&2
  exit 0
fi

# Rebuild if a venv exists but on another interpreter.
if [ -x "$VENV/bin/python" ] && ! "$VENV/bin/python" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))'; then
  rm -rf "$VENV"
fi
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"

"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r web_dashboard/requirements.txt

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo "export VIRTUAL_ENV=\"$VENV\""
    echo "export PATH=\"$VENV/bin:\$PATH\""
  } >> "$CLAUDE_ENV_FILE"
fi
