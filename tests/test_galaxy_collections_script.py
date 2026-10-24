"""scripts/ci/galaxy_collections.sh: Galaxy first, the cached set only when Galaxy is down.

The ansible-syntax job installs the BeyondTrust collections from Galaxy, which on
2026-10-24 answered 500s for over forty minutes. The driver upgrades from Galaxy with four
tries, so the check normally runs against the latest releases, and falls back to a full
set restored from an earlier run on main only when every try fails. These pin it with a
fake ``ansible-galaxy`` on PATH:

  * Galaxy works -> fresh=true, so the workflow saves the cache,
  * Galaxy fails, then works -> it retried, fresh=true,
  * Galaxy is down and every collection is cached -> it passes, with a warning, and
    fresh=false, so the stale set is never re-saved as if it were new,
  * Galaxy is down and the cache is partial or empty -> it fails, as before.

Run: python tests/test_galaxy_collections_script.py   (or under pytest)
"""
import os
import shutil
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "scripts", "ci", "galaxy_collections.sh")
_COLLECTIONS = ("beyondtrust.secrets_safe", "beyondtrust.password_safe")

# Fails `install` the first $FAILS times it is called, then succeeds; `list` always works.
_FAKE = """#!/bin/sh
state="$FAKE_STATE"
if [ "$2" = "list" ]; then echo "listed"; exit 0; fi
n=$(cat "$state" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$state"
if [ "$n" -le "$FAILS" ]; then echo "HTTP Error 500: Internal Server Error"; exit 1; fi
echo "installed $*"
"""


def _run(fails, cached=()):
    work = tempfile.mkdtemp(prefix="galaxy-")
    try:
        bindir = os.path.join(work, "bin")
        os.makedirs(bindir)
        fake = os.path.join(bindir, "ansible-galaxy")
        with open(fake, "w") as fh:
            fh.write(_FAKE)
        os.chmod(fake, 0o755)
        dest = os.path.join(work, "collections")
        for c in cached:
            ns, name = c.split(".")
            os.makedirs(os.path.join(dest, "ansible_collections", ns, name))
        out = os.path.join(work, "github_output")
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""),
                   FAILS=str(fails), FAKE_STATE=os.path.join(work, "calls"),
                   ANSIBLE_COLLECTIONS_DIR=dest, GALAXY_RETRY_SECONDS="0",
                   GITHUB_OUTPUT=out)
        proc = subprocess.run(["bash", _SCRIPT, *_COLLECTIONS], env=env,
                              capture_output=True, text=True, timeout=60)
        calls = int(open(env["FAKE_STATE"]).read()) if os.path.exists(env["FAKE_STATE"]) else 0
        output = open(out).read() if os.path.exists(out) else ""
        return proc.returncode, proc.stdout + proc.stderr, calls, output
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _skip_without_bash():
    if shutil.which("bash") is None:
        print("SKIP: bash not available")
        return True
    return False


def test_galaxy_up_is_a_fresh_install():
    if _skip_without_bash():
        return
    rc, log, calls, out = _run(fails=0)
    assert rc == 0 and calls == 1, log
    assert "fresh=true" in out, out


def test_a_failed_try_is_retried():
    if _skip_without_bash():
        return
    rc, log, calls, out = _run(fails=2)
    assert rc == 0 and calls == 3, log
    assert log.count("::warning::Galaxy collection install failed") == 2, log
    assert "fresh=true" in out


def test_galaxy_down_with_a_full_cache_passes_on_the_cache():
    if _skip_without_bash():
        return
    rc, log, calls, out = _run(fails=99, cached=_COLLECTIONS)
    assert rc == 0, log
    assert calls == 4, f"gave up after {calls} tries, not 4"
    assert "checking against the collections cached" in log, log
    assert "fresh=false" in out and "fresh=true" not in out, (
        "a run that used the cache must not mark it fresh, or main re-saves a stale set")


def test_galaxy_down_with_a_partial_cache_fails():
    if _skip_without_bash():
        return
    rc, log, _calls, out = _run(fails=99, cached=_COLLECTIONS[:1])
    assert rc == 1, log
    assert "::error::" in log and "fresh=" not in out


def test_galaxy_down_with_no_cache_fails():
    if _skip_without_bash():
        return
    rc, log, _calls, _out = _run(fails=99)
    assert rc == 1, log
    assert "cached: none" in log, log


def test_the_workflow_saves_only_a_fresh_download_on_main():
    """The other half of fresh=false: the save step's condition."""
    src = open(os.path.join(_ROOT, ".github", "workflows", "tests.yml"), encoding="utf-8").read()
    job = src.split("\n  ansible-syntax:", 1)[1].split("\n  # ──", 1)[0]
    assert "scripts/ci/galaxy_collections.sh" in job
    assert "actions/cache/restore@" in job and "actions/cache/save@" in job
    save = job.split("actions/cache/save@", 1)[0].rsplit("- name:", 1)[1]
    assert "github.ref == 'refs/heads/main'" in save, save
    assert "steps.galaxy.outputs.fresh == 'true'" in save, save


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
