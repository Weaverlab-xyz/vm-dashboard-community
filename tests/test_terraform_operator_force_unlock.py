"""Unit tests for the operator-driven Terraform state-lock force-unlock.

The cancel path releases the locks it orphans itself (see
test_terraform_cancel_lock_release.py). This is the other half: a lock stranded
some other way — worker OOM-killed, replica rolled mid-apply — is held by a
process nothing can prove is dead, so breaking it is an admin's judgement call.

Everything here is a refusal test, because the refusals are the feature. A
force-unlock that fires when something is still using the state corrupts it, and
the failure is silent until a much later run reads back nonsense. So this pins:

  * the state key comes from the LOCK'S PATH, never from the failing job's id —
    they differ in exactly the case this feature exists for (a decommission
    destroys in the provision job's deploy dir);
  * a state key that isn't a job id never reaches the filesystem;
  * the unlock is optimistic — a lock that turned over since the operator looked
    at it is NOT broken;
  * a job that was already running when the lock was taken blocks the unlock,
    and a job that started afterwards provably cannot be the holder.

Terraform itself is never invoked — `_run` / `_init_sync` are stubbed.
Runs under pytest, or standalone:  python tests/test_terraform_operator_force_unlock.py
"""
import os
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _Settings:
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


# The decommission failure from the live incident, trimmed. Note the two different
# ids: the job that FAILED is the decommission, but the lock lives under the
# PROVISION job's state key. Assuming the failing job's own id would look at the
# wrong state and find nothing.
FAILING_JOB_ID = "21471d7d-ac12-4a93-bf95-350357d43e81"
STATE_JOB_ID = "eda7e624-37ce-465c-92b9-7acae54a8031"
LOCK_ID = "1787928588291305"

DECOMMISSION_ERROR = f"""DB destroy: terraform destroy failed:

Error: Error acquiring the state lock

Error message: writing "gs://bucket/terraform-state/{STATE_JOB_ID}/default.tflock"
failed: googleapi: Error 412: At least one of the pre-conditions you specified did
not hold., conditionNotMet
Lock Info:
  ID:        {LOCK_ID}
  Path:      gs://bucket/terraform-state/{STATE_JOB_ID}/default.tflock
  Operation: OperationTypeApply
  Who:       root@dash-worker--0000020-bd48bfb77-22q8c
  Version:   1.10.5
  Created:   2026-08-28 14:49:48.17098413 +0000 UTC
  Info:

Terraform acquires a state lock to protect the state from being written by
multiple users at the same time.
"""


def _probe(lock_id=LOCK_ID, who="root@worker-a"):
    return f"""Failed to unlock state: lock id does not match existing lock

Lock Info:
  ID:        {lock_id}
  Path:      gs://bucket/terraform-state/{STATE_JOB_ID}/default.tflock
  Operation: OperationTypeApply
  Who:       {who}
  Version:   1.10.5
  Created:   2026-08-28 14:49:48.17098413 +0000 UTC
  Info:
"""


NO_LOCK = "Failed to unlock state: failed to retrieve lock info: object doesn't exist\n"


# ── which state a job may act on ──────────────────────────────────────────────

def test_state_key_comes_from_the_lock_path_not_the_failing_job():
    rep = tf.reported_lock(DECOMMISSION_ERROR)
    assert rep, "the live decommission error must be recognised as a lock failure"
    assert rep["state_job_id"] == STATE_JOB_ID
    assert rep["state_job_id"] != FAILING_JOB_ID, \
        "the whole point: the lock is under the PROVISION job's key, not this job's"
    assert rep["ID"] == LOCK_ID
    assert rep["Who"] == "root@dash-worker--0000020-bd48bfb77-22q8c"


def test_a_job_that_reported_no_lock_offers_nothing():
    assert tf.reported_lock("terraform apply failed: quota exceeded") == {}
    assert tf.reported_lock("") == {}


def test_a_lock_block_with_no_usable_path_offers_nothing():
    # Without a terraform-state/<job>/ path there is no state key to act on, and
    # guessing one would be how this endpoint starts touching unrelated state.
    text = _probe().replace(f"gs://bucket/terraform-state/{STATE_JOB_ID}/", "gs://elsewhere/")
    assert tf.reported_lock(text) == {}


def test_two_lock_blocks_never_blend_into_a_third():
    # A job's text can carry the error AND the same failure echoed in captured output.
    # Taking the last value per field would invent a lock that never existed.
    both = _probe(lock_id="111", who="root@worker-a") + _probe(lock_id="222", who="root@worker-b")
    info = tf._parse_lock_info(both)
    assert info["ID"] == "111" and info["Who"] == "root@worker-a", info


# ── the state key never reaches the filesystem unvalidated ────────────────────

def test_a_state_key_that_is_not_a_job_id_is_refused():
    # reported_lock's regex is the first gate, _lock_workdir is the backstop; this
    # pins the backstop, because it is what stands between a terraform error message
    # and os.makedirs.
    for bad in ("../../etc", "..", "/abs/path", "", "a", "x" * 100,
                "eda7e624/../../../etc", "eda7e624;rm -rf /"):
        try:
            with tf._lock_workdir(bad):
                raise AssertionError(f"{bad!r} should have been refused")
        except tf.TerraformError:
            pass


def test_a_real_job_id_yields_a_workdir_named_for_the_deployment():
    # The dir's BASENAME is what _state_key reads, so this is what makes the unlock
    # act on the deployment's own state rather than some scratch key.
    with tf._lock_workdir(STATE_JOB_ID) as work:
        assert os.path.basename(work) == STATE_JOB_ID
        assert os.path.isdir(work)
        assert tf._state_key(work) == f"terraform-state/{STATE_JOB_ID}"
        root = os.path.dirname(work)
    assert not os.path.exists(root), "the scratch dir must not outlive the call"


# ── inspect / unlock ──────────────────────────────────────────────────────────

def _with_backend(backend, probe_out, unlock_rc=0):
    """Stub _backend_settings/_init_sync/_run; returns (fn(*a), recorded cmds)."""
    cmds = []

    def fake_run(cmd, cwd, timeout=600, env=None):
        cmds.append(list(cmd))
        if cmd[-1] == tf._LOCK_PROBE_ID:
            return subprocess.CompletedProcess(cmd, 1, "", probe_out)
        return subprocess.CompletedProcess(cmd, unlock_rc, "", "nope" if unlock_rc else "")

    orig = (tf._backend_settings, tf._init_sync, tf._run)
    tf._backend_settings = lambda d: (backend, {"bucket": "b"}, {"GOOGLE_CREDENTIALS": "x"})
    tf._init_sync = lambda *a, **k: None
    tf._run = fake_run
    return cmds, orig


def _restore(orig):
    tf._backend_settings, tf._init_sync, tf._run = orig


def test_inspect_reports_the_live_lock():
    cmds, orig = _with_backend("gcs", _probe())
    try:
        out = tf._inspect_state_lock_sync(STATE_JOB_ID)
    finally:
        _restore(orig)
    assert out["backend"] == "gcs" and out["supported"] and out["locked"]
    assert out["info"]["ID"] == LOCK_ID
    assert len(cmds) == 1, "inspecting must never run an unlock"


def test_inspect_reports_an_unlocked_state():
    cmds, orig = _with_backend("gcs", NO_LOCK)
    try:
        out = tf._inspect_state_lock_sync(STATE_JOB_ID)
    finally:
        _restore(orig)
    assert out["supported"] and not out["locked"] and out["info"] == {}
    assert len(cmds) == 1


def test_a_local_backend_has_no_remote_lock_to_break():
    cmds, orig = _with_backend("local", _probe())
    try:
        out = tf._inspect_state_lock_sync(STATE_JOB_ID)
        assert not out["supported"] and not out["locked"]
        assert "local" in out["detail"]
        try:
            tf._force_unlock_state_sync(STATE_JOB_ID, LOCK_ID)
            raise AssertionError("a local backend unlock must be refused")
        except tf.TerraformError as exc:
            assert "local" in str(exc)
    finally:
        _restore(orig)
    assert cmds == [], "a local backend must not shell out to terraform at all"


def test_unlock_releases_the_lock_the_operator_confirmed():
    cmds, orig = _with_backend("gcs", _probe())
    try:
        out = tf._force_unlock_state_sync(STATE_JOB_ID, LOCK_ID)
    finally:
        _restore(orig)
    assert out["unlocked"] and out["lock_id"] == LOCK_ID
    assert len(cmds) == 2 and cmds[1] == ["force-unlock", "-force", LOCK_ID], cmds


def test_unlock_refuses_a_lock_that_turned_over_since_the_page_loaded():
    # The operator saw LOCK_ID; a legitimate run has since taken a different one.
    # Breaking THAT lock would pull the state out from under a live apply.
    cmds, orig = _with_backend("gcs", _probe(lock_id="9999999999"))
    try:
        tf._force_unlock_state_sync(STATE_JOB_ID, LOCK_ID)
        raise AssertionError("a changed lock id must be refused")
    except tf.TerraformError as exc:
        assert "no longer the lock you were shown" in str(exc)
        assert "9999999999" in str(exc) and LOCK_ID in str(exc), \
            "the message must name both ids so the operator can tell what happened"
    finally:
        _restore(orig)
    assert len(cmds) == 1, "the unlock must NOT have been attempted"


def test_unlock_refuses_when_nothing_is_locked():
    cmds, orig = _with_backend("gcs", NO_LOCK)
    try:
        tf._force_unlock_state_sync(STATE_JOB_ID, LOCK_ID)
        raise AssertionError("unlocking an unlocked state must be refused")
    except tf.TerraformError as exc:
        assert "nothing to break" in str(exc)
    finally:
        _restore(orig)
    assert len(cmds) == 1


def test_a_failing_force_unlock_raises_rather_than_reporting_success():
    _cmds, orig = _with_backend("gcs", _probe(), unlock_rc=1)
    try:
        tf._force_unlock_state_sync(STATE_JOB_ID, LOCK_ID)
        raise AssertionError("a non-zero force-unlock must raise")
    except tf.TerraformError as exc:
        assert "force-unlock failed" in str(exc)
    finally:
        _restore(orig)


# ── "could something still be holding it?" ────────────────────────────────────

try:
    from web_dashboard.services import job_service as js
except Exception:  # pragma: no cover
    js = None


class _Job:
    def __init__(self, status, started_at, job_type="clouddb_provision", jid="j" * 36):
        self.status, self.started_at, self.job_type, self.id = status, started_at, job_type, jid


LOCK_AT = datetime(2026, 8, 28, 14, 49, 48, tzinfo=timezone.utc)


def _blockers(jobs, created=LOCK_AT):
    return js.lock_blocking_jobs(jobs, created)


def test_a_job_that_started_after_the_lock_cannot_be_holding_it():
    if js is None:
        return
    later = LOCK_AT.replace(tzinfo=None) + timedelta(minutes=5)   # naive UTC, as stored
    assert _blockers([_Job("running", later)]) == []


def test_a_job_already_running_when_the_lock_was_taken_blocks():
    if js is None:
        return
    earlier = LOCK_AT.replace(tzinfo=None) - timedelta(minutes=5)
    assert len(_blockers([_Job("running", earlier)])) == 1


def test_a_running_job_with_no_start_time_blocks():
    if js is None:
        return
    # It cannot be ruled out, and "cannot be ruled out" must never read as safe.
    assert len(_blockers([_Job("running", None)])) == 1


def test_queued_and_pending_jobs_never_block():
    if js is None:
        return
    # They have not run terraform yet, so they cannot hold a lock that already
    # exists. One that starts and locks after we break this one takes a NEW lock.
    earlier = LOCK_AT.replace(tzinfo=None) - timedelta(hours=1)
    jobs = [_Job("pending", earlier), _Job("queued", earlier),
            _Job("failed", earlier), _Job("completed", earlier)]
    assert _blockers(jobs) == []


def test_an_unknown_lock_age_rules_no_running_job_out():
    # An unparseable Created stamp must not read as "nothing can be holding it".
    if js is None:
        return
    jobs = [_Job("running", datetime(2030, 1, 1)), _Job("running", None)]
    assert len(js.lock_blocking_jobs(jobs, None)) == 2
    # ...but a finished job is still not a holder, whatever the stamp says.
    assert js.lock_blocking_jobs([_Job("completed", None)], None) == []


def test_an_aware_started_at_is_compared_correctly():
    if js is None:
        return
    aware_later = LOCK_AT + timedelta(minutes=5)
    aware_earlier = LOCK_AT - timedelta(minutes=5)
    assert _blockers([_Job("running", aware_later)]) == []
    assert len(_blockers([_Job("running", aware_earlier)])) == 1


def test_a_job_started_exactly_at_the_lock_stamp_blocks():
    if js is None:
        return
    # The boundary belongs on the safe side: same instant means it could be ours.
    assert len(_blockers([_Job("running", LOCK_AT.replace(tzinfo=None))])) == 1


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
