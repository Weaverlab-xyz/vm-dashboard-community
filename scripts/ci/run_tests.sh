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
# ONE DATABASE PER FILE. Unless the caller sets DATABASE_URL (the postgres job does),
# each file gets its own empty SQLite file. Without this, every file that sets no URL of
# its own shared config.py's default ./vm_cli.db in the repo root -- so a file could pass
# only because an earlier file, alphabetically, had created the tables it reads, and
# fail on a fresh checkout or when run alone. A file that sets its own URL keeps it.
#
# COVERAGE. COVERAGE=1 runs each file under `coverage run --parallel-mode
# --source=web_dashboard`, one data file per process, written to $COVERAGE_DIR (default
# .coverage-data/). Nothing else changes: same DBs, same skip guard, same exit codes.
# scripts/ci/coverage_summary.py combines the data files. The `tests` job never sets it;
# only .github/workflows/coverage.yml does (nightly), so a PR's runtime is unchanged.
#
# PARALLEL. Files run JOBS at a time (default: the CPU count), each still its own process
# with its own database, so the isolation above is unchanged -- only the scheduling is.
# Each file gets its own directory, $dbdir/<n>-<name>/, holding its SQLite database, the
# data the app writes beside it (a setup_token file, for one), and its TMPDIR: two files
# sharing a directory could otherwise race on a fixed file name. A file's output is
# printed whole, inside its ::group::, once it finishes, so groups never interleave. The
# biggest files start first, so one long file does not start last and stretch the run.
# JOBS=1 is the old serial run. A caller that sets DATABASE_URL (the postgres job) shares
# one database between files, so the run is serial there whatever JOBS says.
# A file that fails only when run alongside others is sharing something it should not
# (a fixed path, port or temp name): give it its own.
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

RUN=("$PY")
if [ "${COVERAGE:-}" = "1" ]; then
  COVERAGE_DIR="${COVERAGE_DIR:-$PWD/.coverage-data}"
  rm -rf "$COVERAGE_DIR"; mkdir -p "$COVERAGE_DIR"
  # Absolute: a test that chdirs must not scatter data files.
  export COVERAGE_FILE="$COVERAGE_DIR/.coverage"
  RUN=("$PY" -m coverage run --parallel-mode --source=web_dashboard)
fi

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

# A test defined BELOW a file's `if __name__ == "__main__"` runner is not in globals()
# when the runner collects them, so it never runs and the file still passes. Five agent
# lifecycle tests sat there unrun for a release; refuse it.
late=0
for f in "${files[@]}"; do
  n=$(awk '/^if __name__ == "__main__"/{m=1; next} m && /^(async )?def test_/{c++} END{print c+0}' "$f")
  if [ "$n" -gt 0 ]; then
    echo "::error file=$f::$f defines $n test(s) after its __main__ runner; they never run. Move the runner to the end."
    late=1
  fi
done

# How many at once. One shared database (the caller's DATABASE_URL) means one at a time.
if [ -n "${DATABASE_URL:-}" ]; then
  JOBS=1
else
  JOBS="${JOBS:-$(nproc 2>/dev/null || echo 2)}"
fi
case "$JOBS" in ''|*[!0-9]*|0) echo "::error::JOBS must be a positive number, not '$JOBS'"; exit 2;; esac

fail=0
failed=()
skipped=()
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# A directory per file, keyed by its position so two arguments with one basename differ.
declare -A dir_of
i=0
for f in "${files[@]}"; do
  i=$((i + 1))
  dir_of[$f]="$work/$i-$(basename "$f" .py)"
done

run_one() {
  local f="$1" d="$2" t0 rc
  mkdir -p "$d/tmp"
  t0=$EPOCHREALTIME
  if [ -n "${DATABASE_URL:-}" ]; then
    TMPDIR="$d/tmp" "${RUN[@]}" "$f" >"$d/log" 2>&1
  else
    DATABASE_URL="sqlite:///$d/test.db" TMPDIR="$d/tmp" "${RUN[@]}" "$f" >"$d/log" 2>&1
  fi
  rc=$?
  echo "$rc" >"$d/rc"
  awk -v a="$t0" -v b="$EPOCHREALTIME" 'BEGIN { printf "%.1f\n", b - a }' >"$d/secs"
  # Whole, and one at a time: a group is never split by another file's output.
  {
    flock 9
    echo "::group::$f"
    cat "$d/log"
    echo "::endgroup::"
  } 9>"$work/.print-lock"
}

# Biggest first: the nearest cheap stand-in for longest first. Serial keeps the given order.
if [ "$JOBS" -eq 1 ]; then
  order=("${files[@]}")
else
  mapfile -t order < <(for f in "${files[@]}"; do printf '%s\t%s\n' "$(stat -c %s "$f")" "$f"; done \
                       | sort -t$'\t' -k1,1nr | cut -f2-)
fi

running=0
for f in "${order[@]}"; do
  if [ "$running" -ge "$JOBS" ]; then
    wait -n
    running=$((running - 1))
  fi
  run_one "$f" "${dir_of[$f]}" &
  running=$((running + 1))
done
wait

# Judged in the order given, from what each file left behind: the rules are the serial
# run's, read from a saved log instead of a live one.
for f in "${files[@]}"; do
  d="${dir_of[$f]}"
  rc=$(cat "$d/rc" 2>/dev/null || echo 1)
  if [ "$rc" -ne 0 ]; then
    failed+=("$f")
    fail=1
  elif grep -qE "$SKIP_RE" "$d/log" && ! is_allowed "$f"; then
    echo "::error file=$f::$f skipped tests in CI:"
    grep -E "$SKIP_RE" "$d/log" | head -5 | sed 's/^/    /'
    skipped+=("$f")
    fail=1
  fi
done

if [ "${#files[@]}" -gt 1 ]; then
  echo "Slowest files (JOBS=$JOBS):"
  for f in "${files[@]}"; do
    printf '%8.1fs  %s\n' "$(cat "${dir_of[$f]}/secs" 2>/dev/null || echo 0)" "$f"
  done | sort -rn | head -10
fi

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
[ "$late" -eq 0 ] || fail=1
exit $fail
