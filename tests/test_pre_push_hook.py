"""The repo's pre-push hook: a stale tailwind.css is caught before the push, not by CI.

CI's browser-smoke job fails a stale committed stylesheet about 30 minutes into a run
(#1325). ``scripts/hooks/pre-push`` runs the same ``scripts/build_css.sh --check`` in
seconds. These pin that it stays executable, stays wired into cloud sessions, and keeps
calling the same check CI does.

Runs under pytest, or standalone:
    python tests/test_pre_push_hook.py
"""
import os
import stat
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HOOK = os.path.join(_ROOT, "scripts", "hooks", "pre-push")


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_the_hook_is_executable_and_parses():
    assert os.stat(_HOOK).st_mode & stat.S_IXUSR, "scripts/hooks/pre-push lost its +x bit"
    subprocess.run(["bash", "-n", _HOOK], check=True)


def test_the_hook_runs_the_check_ci_runs():
    hook = _read("scripts", "hooks", "pre-push")
    ci = _read(".github", "workflows", "tests.yml")
    assert "scripts/build_css.sh --check" in hook and "scripts/build_css.sh --check" in ci


def test_the_hook_never_blocks_on_a_missing_tool():
    """A hook that blocks pushes when Node is absent is a hook people uninstall."""
    hook = _read("scripts", "hooks", "pre-push")
    assert "command -v npx" in hook
    no_npx = hook.split("command -v npx", 1)[1].split("fi", 1)[0]
    assert "exit 0" in no_npx


def test_cloud_sessions_turn_the_hook_on():
    assert "git config core.hooksPath scripts/hooks" in _read(".claude", "hooks",
                                                              "session-start.sh")


def test_a_push_that_touches_nothing_in_the_app_skips_the_check():
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=_ROOT, capture_output=True,
                          text=True, check=True).stdout.strip()
    # Pushing HEAD over itself changes nothing, so no build may run.
    r = subprocess.run([_HOOK], cwd=_ROOT, input=f"refs/heads/x {head} refs/heads/x {head}\n",
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


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
