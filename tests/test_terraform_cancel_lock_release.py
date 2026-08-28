"""Unit tests for releasing the state lock a cancelled terraform run orphans.

Cancelling a job kills the terraform subprocess, and a killed terraform never
releases its state lock. The lock then outlives the job forever and wedges every
later run against that state — including the destroy that would clean the
cancelled deployment up. Live case: a cancelled clouddb_provision left the GCS
default.tflock held by the very worker that killed it, the follow-up
clouddb_decommission failed with "Error acquiring the state lock", and the lock
object had to be deleted by hand before the teardown could run.

The cancel path now breaks that lock — but ONLY the one it can prove it orphaned.
This pins both halves: that our own lock is released, and that the guards refuse
every lock we cannot prove is ours (another replica's Who, a lock created before
we spawned, an unparseable or absent stamp). A guard that fails open here would
let one worker yank a lock out from under another worker's live apply.

Terraform itself is never invoked — `_run` is stubbed.
Runs under pytest, or standalone:  python tests/test_terraform_cancel_lock_release.py
"""
import asyncio
import os
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _Settings:
    """Stand-in for the pydantic Settings: any unknown key resolves to ""."""
    terraform_executable = "terraform"

    def __getattr__(self, _key):
        return ""


def _install_stubs():
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    st = types.ModuleType("web_dashboard.services.storage_service")
    st.active_backend = lambda: "local"
    sys.modules["web_dashboard.services.storage_service"] = st

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: ""
    sys.modules["web_dashboard.services.config_service"] = cfg


_install_stubs()
try:
    from web_dashboard.services import terraform as tf
except Exception as exc:  # pragma: no cover — skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"terraform service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


# The real force-unlock failure output, GCS backend. Note the ID: it is the lock
# OBJECT's generation number, not the UUID stored inside the .tflock — which is
# exactly why the id is read back from terraform instead of from the object.
def _probe_output(who, created="2026-08-28 14:49:48.17098413 +0000 UTC",
                  lock_id="1787928588291305"):
    return f"""Failed to unlock state: lock id "{tf._LOCK_PROBE_ID}" does not match existing lock

Lock Info:
  ID:        {lock_id}
  Path:      gs://bucket/terraform-state/eda7e624/default.tflock
  Operation: OperationTypeApply
  Who:       {who}
  Version:   1.10.5
  Created:   {created}
  Info:
"""


NO_LOCK_OUTPUT = """Failed to unlock state: failed to retrieve lock info: storage: object doesn't exist
"""


# ── the Lock Info parser ──────────────────────────────────────────────────────

def test_parses_the_lock_info_block():
    info = tf._parse_lock_info(_probe_output("root@dash-worker--0000020-bd48bfb77-22q8c"))
    assert info["ID"] == "1787928588291305"
    assert info["Who"] == "root@dash-worker--0000020-bd48bfb77-22q8c"
    assert info["Operation"] == "OperationTypeApply"
    assert info["Created"] == "2026-08-28 14:49:48.17098413 +0000 UTC"


def test_no_lock_block_parses_empty():
    # "no lock held", "backend does not lock", and "dir not initialised" all land
    # here, and every one of them must read as nothing to release.
    assert tf._parse_lock_info(NO_LOCK_OUTPUT) == {}
    assert tf._parse_lock_info("") == {}
    assert tf._parse_lock_info(None) == {}


# ── the Created stamp ─────────────────────────────────────────────────────────

def test_parses_go_default_time_layout():
    # terraform renders Created with Go's time.Time String(), NOT RFC3339, and with
    # 8 fractional digits — two more than datetime accepts.
    t = tf._parse_lock_time("2026-08-28 14:49:48.17098413 +0000 UTC")
    assert t is not None, "the live stamp format must parse"
    assert (t.year, t.month, t.day, t.hour, t.minute, t.second) == (2026, 8, 28, 14, 49, 48)
    assert t.microsecond == 170984
    assert t.utcoffset() == timedelta(0)


def test_parses_rfc3339_from_the_lock_object():
    t = tf._parse_lock_time("2026-08-28T14:49:48.17098413Z")
    assert t is not None and t.utcoffset() == timedelta(0)


def test_parses_a_non_utc_offset():
    t = tf._parse_lock_time("2026-08-28 09:49:48.5 -0500 EST")
    assert t is not None and t.utcoffset() == timedelta(hours=-5)


def test_unparseable_time_is_none_not_a_guess():
    assert tf._parse_lock_time("") is None
    assert tf._parse_lock_time("whenever") is None
    assert tf._parse_lock_time(None) is None


# ── the release decision ──────────────────────────────────────────────────────

def _release(probe_out, started_at, unlock_rc=0, who=None):
    """Drive _release_own_lock_sync with a stubbed _run; returns (status, cmds)."""
    cmds = []

    def fake_run(cmd, cwd, timeout=600, env=None):
        cmds.append(list(cmd))
        if cmd[-1] == tf._LOCK_PROBE_ID:
            return subprocess.CompletedProcess(cmd, 1, "", probe_out)
        return subprocess.CompletedProcess(cmd, unlock_rc, "", "boom" if unlock_rc else "")

    orig_run, orig_owner = tf._run, tf._self_lock_owner
    tf._run = fake_run
    if who is not None:
        tf._self_lock_owner = lambda: who
    try:
        with tempfile.TemporaryDirectory() as tmp:
            status = tf._release_own_lock_sync(tmp, None, started_at)
    finally:
        tf._run, tf._self_lock_owner = orig_run, orig_owner
    return status, cmds


ME = "root@dash-worker--0000020-bd48bfb77-22q8c"
BEFORE = datetime(2026, 8, 28, 14, 49, 40, tzinfo=timezone.utc)   # we spawned first
AFTER = datetime(2026, 8, 28, 14, 55, 0, tzinfo=timezone.utc)     # lock predates us


def test_releases_our_own_lock():
    status, cmds = _release(_probe_output(ME), BEFORE, who=ME)
    assert status == "released 1787928588291305", status
    assert len(cmds) == 2, f"expected a probe then an unlock, got {cmds}"
    assert cmds[1] == ["force-unlock", "-force", "1787928588291305"], cmds[1]


def test_refuses_a_lock_held_by_another_replica():
    # The OOM-killed / rolled-replica case. Nothing here can prove that holder is
    # gone, so it must be left for a deliberate operator action.
    other = "root@dash-worker--0000019-aaaaaaaaa-zzzzz"
    status, cmds = _release(_probe_output(other), BEFORE, who=ME)
    assert "left alone" in status and other in status, status
    assert len(cmds) == 1, "the probe must be the ONLY command run"


def test_refuses_a_lock_taken_before_we_spawned():
    # Same host, same deploy dir, but a DIFFERENT terraform that is still alive:
    # Who matches and would pass on its own, so the timestamp guard is what saves it.
    status, cmds = _release(_probe_output(ME), AFTER, who=ME)
    assert "left alone" in status and "before this run started" in status, status
    assert len(cmds) == 1, "a live sibling's lock must never be force-unlocked"


def test_refuses_when_created_is_unparseable():
    status, cmds = _release(_probe_output(ME, created="???"), BEFORE, who=ME)
    assert "left alone" in status and "unparseable" in status, status
    assert len(cmds) == 1


def test_no_lock_is_a_clean_noop():
    status, cmds = _release(NO_LOCK_OUTPUT, BEFORE, who=ME)
    assert status == "none held", status
    assert len(cmds) == 1


def test_a_failed_unlock_reports_instead_of_raising():
    status, _cmds = _release(_probe_output(ME), BEFORE, unlock_rc=1, who=ME)
    assert "failed" in status, status


def test_never_raises_even_if_terraform_blows_up():
    # The caller is unwinding a cancel; an exception here would replace JobCancelled.
    def boom(*_a, **_k):
        raise OSError("terraform not found")

    orig = tf._run
    tf._run = boom
    try:
        status = tf._release_own_lock_sync("/nonexistent", None, BEFORE)
    finally:
        tf._run = orig
    assert status.startswith("release failed:"), status


def test_self_owner_is_user_at_hostname():
    owner = tf._self_lock_owner()
    assert "@" in owner and not owner.endswith("@"), owner


# ── the cancel path wiring ────────────────────────────────────────────────────

def _run_cancelled_stream(kill_hangs=False):
    """Drive _stream to a JobCancelled and report how the lock release was called."""
    calls = []

    class _Proc:
        returncode = -15

        def __init__(self):
            self.stdout = self
            self._lines = [b"google_sql_database_instance.this: Still creating...\n"]

        async def readline(self):
            return self._lines.pop(0) if self._lines else b""

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")

        async def wait(self):
            calls.append("wait")
            if kill_hangs and calls.count("wait") == 1:
                await asyncio.sleep(3600)   # force the terminate path to time out
            return -15

    async def fake_exec(*_a, **_k):
        return _Proc()

    async def on_line(_line):
        raise tf.JobCancelled()

    released = {}

    def fake_release(deploy_dir, env, started_at):
        released["args"] = (deploy_dir, env, started_at)
        calls.append("release")
        return "released 123"

    orig_exec = asyncio.create_subprocess_exec
    orig_release, orig_wait_for = tf._release_own_lock_sync, asyncio.wait_for
    asyncio.create_subprocess_exec = fake_exec
    tf._release_own_lock_sync = fake_release

    async def fast_wait_for(aw, timeout=None):
        return await orig_wait_for(aw, timeout=0.05 if kill_hangs else timeout)

    if kill_hangs:
        asyncio.wait_for = fast_wait_for
    try:
        cancelled = False
        try:
            asyncio.run(tf._stream(["apply"], "/deploy/eda7e624", {"K": "v"}, on_line))
        except tf.JobCancelled:
            cancelled = True
    finally:
        asyncio.create_subprocess_exec = orig_exec
        tf._release_own_lock_sync = orig_release
        asyncio.wait_for = orig_wait_for
    return cancelled, calls, released


def test_cancel_kills_terraform_then_releases_the_lock():
    cancelled, calls, released = _run_cancelled_stream()
    assert cancelled, "_stream must still re-raise JobCancelled so the job finalizes"
    assert "release" in calls, "the orphaned lock must be released on cancel"
    assert calls.index("terminate") < calls.index("release"), \
        "release must come AFTER the holder is stopped, never before"
    deploy_dir, env, started_at = released["args"]
    assert deploy_dir == "/deploy/eda7e624", "the lock is released in the job's deploy dir"
    assert env == {"K": "v"}, "backend credentials must reach the unlock subprocess"
    assert started_at.tzinfo is not None, "the spawn stamp must be timezone-aware"
    assert started_at <= datetime.now(timezone.utc)


def test_a_hung_terminate_is_killed_and_reaped_before_the_release():
    cancelled, calls, _released = _run_cancelled_stream(kill_hangs=True)
    assert cancelled
    assert "kill" in calls, "a terraform that ignores SIGTERM must be SIGKILLed"
    assert calls.index("kill") < calls.index("release"), \
        "force-unlock is only sound once the holder is provably dead"
    assert calls.count("wait") >= 2, "the killed process must be reaped, not just signalled"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
