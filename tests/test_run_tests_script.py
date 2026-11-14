"""scripts/ci/run_tests.sh runs test files in parallel and keeps every rule it had serially.

The script is what CI's `tests` job runs. Running files JOBS at a time is only safe if:

  * **a failure still fails the run,** and the summary names the file;
  * **the skip guard still holds:** a file that passes only by skipping fails the run;
  * **every file gets its own directory**: its database, the files the app writes beside it,
    and its TMPDIR. Two files never see each other's files;
  * **a group is whole:** one file's output is never split by another's;
  * **a shared database means serial:** the postgres job sets DATABASE_URL, and parallel
    files would collide in it;
  * **JOBS=1 is the old run,** files in the order given.

Each test drives the real script on throwaway test files written to a temp directory.

Runs under pytest, or standalone:
    python tests/test_run_tests_script.py
"""
import os
import subprocess
import sys
import tempfile
import textwrap

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "scripts", "ci", "run_tests.sh")


def _files(**bodies):
    d = tempfile.mkdtemp(prefix="run-tests-script-")
    paths = []
    for name, body in sorted(bodies.items()):
        p = os.path.join(d, f"test_{name}.py")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(body))
        paths.append(p)
    return d, paths


def _run(paths, *, jobs=None, database_url=None):
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "JOBS")}
    env["PYTHON"] = sys.executable
    if jobs is not None:
        env["JOBS"] = str(jobs)
    if database_url:
        env["DATABASE_URL"] = database_url
    proc = subprocess.run(["bash", _SCRIPT, *paths], capture_output=True, text=True,
                          env=env, timeout=120)
    return proc.returncode, proc.stdout + proc.stderr


_PASS = 'print("ok   test_fine")\n'

# Each records when it ran, in a shared journal, for the overlap tests.
_TIMED = """
import os, time
j = os.environ["JOURNAL"]
open(j, "a").write("start {n}\\n")
time.sleep(0.6)
open(j, "a").write("end {n}\\n")
print("ok   test_{n}")
"""


def test_a_failure_still_fails_a_parallel_run():
    _, paths = _files(good=_PASS, bad='import sys\nprint("FAIL test_x")\nsys.exit(1)\n')
    rc, out = _run(paths, jobs=4)
    assert rc == 1, out
    assert "1 test file(s) failed" in out and "test_bad.py" in out.split("failed:")[1], out


def test_the_skip_guard_still_holds_in_parallel():
    _, paths = _files(good=_PASS, skips='print("SKIP: no boto3 here")\n')
    rc, out = _run(paths, jobs=4)
    assert rc == 1, out
    assert "passed only by skipping" in out and "test_skips.py" in out, out


def test_every_file_has_a_directory_of_its_own():
    """The app writes a setup_token beside its database: a shared directory would race."""
    body = """
    import os, tempfile, time
    d = os.path.dirname(os.environ["DATABASE_URL"].replace("sqlite:///", ""))
    open(os.path.join(d, "setup_token"), "w").write("{n}")
    assert tempfile.gettempdir().startswith(d), "TMPDIR is not the file's own"
    time.sleep(0.5)
    others = [f for f in os.listdir(d) if f.startswith("marker-")]
    open(os.path.join(d, "marker-{n}"), "w").close()
    assert open(os.path.join(d, "setup_token")).read() == "{n}", "another file's token"
    assert not others, others
    print("ok   test_{n}")
    """
    _, paths = _files(**{n: body.replace("{n}", n) for n in ("a", "b", "c", "d")})
    rc, out = _run(paths, jobs=4)
    assert rc == 0, out


def test_a_files_output_is_never_split():
    body = 'import time\nfor i in range(40):\n    print("{n}", i, flush=True)\n    time.sleep(0.01)\n'
    _, paths = _files(**{n: body.replace("{n}", n) for n in ("p", "q", "r")})
    rc, out = _run(paths, jobs=3)
    assert rc == 0, out
    current = None
    for line in out.splitlines():
        if line.startswith("::group::"):
            current = os.path.basename(line[len("::group::"):])[len("test_"):-len(".py")]
        elif line == "::endgroup::":
            current = None
        elif current and line and line.split()[0] in ("p", "q", "r"):
            assert line.split()[0] == current, f"{line!r} inside {current}'s group"


def _journal(paths, **kw):
    j = os.path.join(os.path.dirname(paths[0]), "journal")
    os.environ["JOURNAL"] = j
    try:
        rc, out = _run(paths, **kw)
    finally:
        del os.environ["JOURNAL"]
    assert rc == 0, out
    return open(j).read().split("\n")[:-1]


def _overlapped(events):
    live, most = 0, 0
    for e in events:
        live += 1 if e.startswith("start") else -1
        most = max(most, live)
    return most > 1


def test_files_do_run_at_the_same_time():
    _, paths = _files(**{n: _TIMED.replace("{n}", n) for n in ("a", "b")})
    assert _overlapped(_journal(paths, jobs=2)), "JOBS=2 ran the files one after another"


def test_a_shared_database_runs_one_at_a_time():
    d, paths = _files(**{n: _TIMED.replace("{n}", n) for n in ("a", "b")})
    events = _journal(paths, jobs=4, database_url=f"sqlite:///{d}/shared.db")
    assert not _overlapped(events), f"files shared one database at once: {events}"


def test_jobs_1_is_the_old_serial_run_in_the_given_order():
    _, paths = _files(**{n: _TIMED.replace("{n}", n) for n in ("b", "a")})
    events = _journal(list(reversed(paths)), jobs=1)
    assert events == ["start b", "end b", "start a", "end a"], events


def test_a_nonsense_jobs_value_is_refused():
    _, paths = _files(good=_PASS)
    rc, out = _run(paths, jobs="many")
    assert rc == 2 and "JOBS must be a positive number" in out, out


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
