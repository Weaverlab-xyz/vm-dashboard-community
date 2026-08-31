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


# force-unlock failure output in the s3/azurerm shape: those backends COMPARE the
# id and hand back a statemgr.LockError with Info attached. The ID here is still a
# GCS generation number rather than the UUID inside the .tflock, because that is
# why the id is read back from terraform instead of from the object.
#
# GCS's own wording differs and is pinned separately in REAL_GCS_LOCKED_PROBE
# below -- this fixture originally claimed to BE the GCS output, and that fiction
# is what let a non-numeric sentinel ship green.
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


# ── the sentinel's FORMAT (the bug that shipped) ───────────────────────────────
#
# All three of the following were captured live on 2026-08-31 (terraform 1.10.5,
# gcs backend) while probing the real lock left behind by a wedged k8s_decommission.

# A state that IS locked. Note there is no "does not match" phrasing anywhere: the
# gcs backend uses the generation as a delete PRECONDITION instead of comparing
# ids, and precondition 0 is refused client-side before any API call -- which is
# what keeps the probe non-destructive while still rendering the block.
REAL_GCS_LOCKED_PROBE = """Failed to unlock state: storage: Delete: empty conditions
Lock Info:
  ID:        1788210674129164
  Path:      gs://bucket/terraform-state/38fd3bb8-e404-4813-ab54-8a73ec72d657/default.tflock
  Operation: OperationTypeApply
  Who:       root@dash-worker--0000023-7bc784f647-49pvx
  Version:   1.10.5
  Created:   2026-08-31 21:11:14.036246578 +0000 UTC
  Info:
"""

# What gcs says when the sentinel is not numeric. It is a BARE error, not a
# statemgr.LockError, so it carries no Lock Info block no matter what holds the
# lock -- which silently blinded both the cancel release and the operator panel on
# every GCS deployment.
REAL_GCS_NON_NUMERIC_REJECTION = (
    "Failed to unlock state: Lock ID should be numerical value, "
    "got 'dashboard-probe-not-a-lock-id'\n")

# A state with no .tflock at all.
REAL_GCS_ABSENT_LOCK = ("Failed to unlock state: storage: Delete: empty conditions\n"
                        "storage: object doesn't exist\n")


def test_the_sentinel_is_numeric():
    # gcs parses the lock id as an int64 BEFORE it reads the lock, so a non-numeric
    # sentinel can never render a Lock Info block on that backend.
    try:
        int(tf._LOCK_PROBE_ID)
    except (TypeError, ValueError):
        raise AssertionError(
            "_LOCK_PROBE_ID must be numeric or the GCS backend rejects it on format "
            "and every lock reads as absent; got %r" % (tf._LOCK_PROBE_ID,))


def test_the_sentinel_cannot_be_a_real_gcs_generation():
    # Generations are positive int64s, so 0 matches nothing that can exist.
    assert int(tf._LOCK_PROBE_ID) <= 0, "the sentinel must not be a plausible generation"


def test_real_gcs_locked_output_is_classified_locked():
    verdict, info = tf._classify_lock_probe(REAL_GCS_LOCKED_PROBE)
    assert verdict == "locked", verdict
    assert info["ID"] == "1788210674129164", info
    assert info["Who"] == "root@dash-worker--0000023-7bc784f647-49pvx", info


def test_real_gcs_absent_lock_is_classified_unlocked():
    assert tf._classify_lock_probe(REAL_GCS_ABSENT_LOCK) == ("unlocked", {})


def test_a_format_rejection_is_unknown_never_unlocked():
    # THE regression. Classifying this as "unlocked" is what told an operator the
    # lock had cleared itself while it was provably still held, and sent them off to
    # retry a run that could only fail in exactly the same way.
    verdict, _info = tf._classify_lock_probe(REAL_GCS_NON_NUMERIC_REJECTION)
    assert verdict == "unknown", verdict


def test_an_unreadable_probe_is_unknown_not_unlocked():
    for text in ("", None, "Error: Backend initialization required",
                 "Error: googleapi: Error 403: caller lacks storage.objects.get",
                 "tls: failed to verify certificate: x509: unknown authority"):
        verdict, _info = tf._classify_lock_probe(text)
        assert verdict == "unknown", (text, verdict)


def _read_lock(probe_out):
    """Drive _read_lock_sync with a stubbed _run."""
    def fake_run(cmd, cwd, timeout=600, env=None):
        return subprocess.CompletedProcess(cmd, 1, "", probe_out)
    orig = tf._run
    tf._run = fake_run
    try:
        with tempfile.TemporaryDirectory() as tmp:
            return tf._read_lock_sync(tmp, None)
    finally:
        tf._run = orig


def test_read_lock_sync_returns_the_lock_when_one_is_held():
    assert _read_lock(REAL_GCS_LOCKED_PROBE)["ID"] == "1788210674129164"


def test_read_lock_sync_reports_absence_only_when_confirmed():
    assert _read_lock(REAL_GCS_ABSENT_LOCK) == {}


def test_read_lock_sync_raises_rather_than_inventing_unlocked():
    # The panel renders "the lock is already gone" off a falsy result, so an
    # unreadable probe has to raise and surface as "could not read the lock".
    try:
        _read_lock(REAL_GCS_NON_NUMERIC_REJECTION)
    except tf.TerraformError as exc:
        assert "Could not determine" in str(exc), str(exc)
    else:
        raise AssertionError("an unreadable probe must not be reported as 'no lock'")


def test_cancel_never_reports_none_held_when_it_cannot_read_the_lock():
    status, cmds = _release(REAL_GCS_NON_NUMERIC_REJECTION, BEFORE, who=ME)
    assert status != "none held", "an unreadable probe must not read as 'nothing orphaned'"
    assert "could not read the lock" in status, status
    assert len(cmds) == 1, "nothing may be force-unlocked off an unreadable probe"


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
