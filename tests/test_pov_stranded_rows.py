"""A POV row stranded in ``provisioning``/``destroying`` is flipped to ``failed``.

``reconcile_stale_jobs`` fails a job whose worker died, but a POV row only ever left
``provisioning`` through the provision job itself. After an app restart mid-provision the
row said ``provisioning`` forever, and ``may_act_on`` refuses Destroy on that status: no
way to clear it out. The sweep keys off the JOBS, not the job it just reconciled, so a row
whose job was failed by an earlier pass is recovered too.

What these pin:

- a stranded ``provisioning`` row with no live job becomes ``failed``, with a reason;
- a row whose provision job is still pending or running is left alone;
- a row younger than the cutoff is left alone (the row is committed before its job);
- a stranded ``destroying`` row becomes ``failed``, so Destroy can be re-run.

Run: python tests/test_pov_stranded_rows.py   (or under pytest)
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

_DB_DIR = tempfile.mkdtemp(prefix="pov_stranded_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_DB_DIR, 'pov.db')}".replace("\\", "/")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-pov-stranded-rows")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard.database import (Base, Job, PovEnvironment, SessionLocal,  # noqa: E402
                                    engine)
from web_dashboard.utils.clock import utcnow  # noqa: E402
from web_dashboard.services import job_service                                 # noqa: E402

Base.metadata.create_all(bind=engine)

_OLD = utcnow() - timedelta(hours=1)


def _reset():
    db = SessionLocal()
    try:
        db.query(Job).delete()
        db.query(PovEnvironment).delete()
        db.commit()
    finally:
        db.close()


def _env(env_id, status, created_at=_OLD):
    db = SessionLocal()
    try:
        db.add(PovEnvironment(id=env_id, platform="aws", name=env_id, status=status,
                              created_at=created_at))
        db.commit()
    finally:
        db.close()


def _job(job_id, job_type, status, env_id):
    db = SessionLocal()
    try:
        db.add(Job(id=job_id, job_type=job_type, status=status,
                   extra_data=json.dumps({"environment_id": env_id}),
                   created_at=utcnow(), updated_at=utcnow()))
        db.commit()
    finally:
        db.close()


def _row(env_id):
    db = SessionLocal()
    try:
        env = db.query(PovEnvironment).filter(PovEnvironment.id == env_id).first()
        return env.status, env.error_message
    finally:
        db.close()


def _sweep():
    db = SessionLocal()
    try:
        job_service.reconcile_stale_jobs(db)
    finally:
        db.close()


def test_stranded_provisioning_row_becomes_failed():
    _reset()
    _env("e1", "provisioning")
    _job("j1", "pov_env_provision", "failed", "e1")  # failed by an earlier pass
    _sweep()
    status, why = _row("e1")
    assert status == "failed", "a provisioning row with no live job stayed provisioning"
    assert why and "Destroy" in why


def test_live_provision_job_is_left_alone():
    _reset()
    _env("e1", "provisioning")
    _env("e2", "provisioning")
    _job("j1", "pov_env_provision", "running", "e1")
    _job("j2", "pov_env_provision", "pending", "e2")
    _sweep()
    assert _row("e1")[0] == "provisioning"
    assert _row("e2")[0] == "provisioning"


def test_a_fresh_row_is_left_alone():
    _reset()
    _env("e1", "provisioning", created_at=utcnow())
    _sweep()
    assert _row("e1")[0] == "provisioning", (
        "a row created moments ago was failed before its job could be queued")


def test_stranded_destroying_row_becomes_failed():
    _reset()
    _env("e1", "destroying")
    _sweep()
    assert _row("e1")[0] == "failed"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
