#!/usr/bin/env bash
# Run the tests/ suite the way CI's `tests` job does, and refuse a silent skip.
#
# Each file runs as its own process via its `if __name__ == "__main__"` runner, not
# through one pytest session: several files install fakes into sys.modules with no
# teardown, and separate processes keep one file's stubs out of the next.
#
# THE SKIP GUARD. A test that cannot run prints a skip line ("SKIP: ...",
# "(skipped: ...)") and exits 0, so a file that skips everything reads exactly like one
# that passed. tests/test_gcp_jumpoint_modes.py did that in CI for five days: a stub was
# missing GCPError, every test skipped on the import, and the job stayed green. So a file
# whose output carries a skip line FAILS here, unless scripts/ci/skip_allowlist.txt names
# it with the reason the skip is expected (usually: another CI job runs it with the tool
# this job does not have).
#
# Usage:
#   scripts/ci/run_tests.sh                    # every tests/test_*.py
#   scripts/ci/run_tests.sh tests/test_x.py    # just these
#
# Run it with the interpreter CI uses (Python 3.12, web_dashboard/requirements.txt, and
# NOT pytest: a skip calls pytest.skip() when pytest is importable, which a standalone
# runner reports as an error).

set -uo pipefail

cd "$(dirname "$0")/../.."
PY="${PYTHON:-python}"
ALLOWLIST=scripts/ci/skip_allowlist.txt

# A line that STARTS (after indentation) with one of the skip spellings the suite uses.
# Anchored so a test that merely mentions the word in a message is not caught.
SKIP_RE='^[[:space:]]*(SKIP\b|\(skipped\b|skip \()'

# File names on the allowlist, comments and blank lines dropped.
mapfile -t allowed < <(sed -e 's/#.*//' -e 's/[[:space:]]*$//' "$ALLOWLIST" | grep -v '^$')
is_allowed() {
  local name; name=$(basename "$1")
  for a in "${allowed[@]}"; do [ "$a" = "$name" ] && return 0; done
  return 1
}

if [ $# -gt 0 ]; then files=("$@"); else files=(tests/test_*.py); fi

# An allowlist entry for a file that no longer exists is a stale exemption waiting to
# cover a new file of the same name.
stale=0
for a in "${allowed[@]}"; do
  if [ ! -f "tests/$a" ]; then
    echo "::error::$ALLOWLIST names tests/$a, which does not exist; remove the entry"
    stale=1
  fi
done

fail=0
failed=()
skipped=()
out=$(mktemp)
trap 'rm -f "$out"' EXIT

for f in "${files[@]}"; do
  echo "::group::$f"
  "$PY" "$f" 2>&1 | tee "$out"
  rc=${PIPESTATUS[0]}
  echo "::endgroup::"
  if [ "$rc" -ne 0 ]; then
    failed+=("$f")
    fail=1
  elif grep -qE "$SKIP_RE" "$out" && ! is_allowed "$f"; then
    echo "::error file=$f::$f skipped tests in CI:"
    grep -E "$SKIP_RE" "$out" | head -5 | sed 's/^/    /'
    skipped+=("$f")
    fail=1
  fi
done

if [ ${#failed[@]} -gt 0 ]; then
  echo "::error::${#failed[@]} test file(s) failed:"
  printf '  %s\n' "${failed[@]}"
fi
if [ ${#skipped[@]} -gt 0 ]; then
  echo "::error::${#skipped[@]} test file(s) passed only by skipping:"
  printf '  %s\n' "${skipped[@]}"
  echo "Fix what makes them skip (a missing stub, a missing dependency). If the skip is"
  echo "expected here because another job runs the file, add it to $ALLOWLIST with why."
fi
[ "$stale" -eq 0 ] || fail=1
exit $fail
