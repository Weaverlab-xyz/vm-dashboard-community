"""POV provision / power / destroy — the orderings whose absence is silent.

Four properties, each of which produces a resource nobody can reclaim when it is wrong:

  * **The platform id is persisted before anything else can fail.** An environment that
    exists on the platform and not in this database is the one failure mode nothing can
    clean up automatically.
  * **A failure after creation keeps the id.** Failing without it is how an orphan is
    made; the row has to stay pointing at the thing so Destroy can reap it.
  * **Destroy reaches the platform delete even when a step before it fails**, and a
    failed destroy does NOT mark the row destroyed — marking it would hide a live
    environment that is still billing.
  * **An empty VM read never prunes.** A transient error returning zero VMs would
    otherwise delete every row, take the PAM artifact columns with it, and record success.

Uses a real SQLite database and a fake adapter, so the orchestrator runs for real. No
network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_pov_lifecycle.py
"""
import asyncio
import os
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-lifecycle")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import job_service, lab_platforms, pov_env_service  # noqa: E402


class FakeAdapter:
    """A lab platform that does what it is told, and can be told to misbehave."""

    def __init__(self, **behaviour):
        self.b = behaviour
        self.calls = []
        self.created_id = behaviour.get("created_id", "sky-100")
        self.deleted = []

    def configured(self):
        return self.b.get("configured", True)

    async def create_environment(self, template_id, name="", project_id=""):
        self.calls.append(("create", template_id, name))
        if self.b.get("create_raises"):
            raise RuntimeError(self.b["create_raises"])
        return {"id": self.created_id, "runstate": "stopped", "region": "US-West"}

    async def update_environment(self, env_id, changes):
        self.calls.append(("update", env_id, changes))
        if self.b.get("update_raises"):
            raise RuntimeError(self.b["update_raises"])
        return {"id": env_id, "runstate": "stopped"}

    async def set_runstate(self, env_id, runstate):
        self.calls.append(("set_runstate", env_id, runstate))
        if self.b.get("power_raises"):
            raise RuntimeError(self.b["power_raises"])
        return {"id": env_id, "runstate": runstate}

    async def wait_for_runstate(self, env_id, target, **kw):
        self.calls.append(("wait", env_id, target))
        if self.b.get("wait_raises"):
            raise RuntimeError(self.b["wait_raises"])
        return {"id": env_id, "runstate": target}

    async def get_environment(self, env_id):
        self.calls.append(("get", env_id))
        if self.b.get("get_raises"):
            raise RuntimeError(self.b["get_raises"])
        return {"id": env_id, "runstate": "running",
                "vms": self.b.get("vms", [
                    {"id": "v1", "name": "dc01", "os_family": "windows",
                     "private_ip": "10.0.0.4", "published_services": []},
                ])}

    async def delete_environment(self, env_id):
        self.calls.append(("delete", env_id))
        if self.b.get("delete_raises"):
            raise RuntimeError(self.b["delete_raises"])
        self.deleted.append(env_id)


def _install(adapter):
    original = lab_platforms.adapter
    lab_platforms.adapter = lambda platform: adapter
    return original


def _restore(original):
    lab_platforms.adapter = original


def _new_env(**kw):
    db = d.SessionLocal()
    env = d.PovEnvironment(
        platform="skytap", name=kw.get("name", "poc-" + uuid.uuid4().hex[:6]),
        template_id=kw.get("template_id", "42"),
        status=kw.get("status", pov_env_service.STATUS_PROVISIONING))
    db.add(env)
    db.commit()
    env_id = env.id
    db.close()
    return env_id


def _job(job_type, env_id, **meta):
    db = d.SessionLocal()
    job = job_service.create_job(db, job_type=job_type, created_by="tester",
                                 metadata={"environment_id": env_id, **meta})
    jid = job.id
    db.close()
    return jid


def _reload(env_id):
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    db.refresh(env)
    out = (env.status, env.platform_environment_id, env.runstate, env.error_message)
    db.close()
    return out


def _job_status(job_id):
    db = d.SessionLocal()
    j = db.query(d.Job).filter(d.Job.id == job_id).first()
    out = (j.status, j.error_message)
    db.close()
    return out


def _vm_rows(env_id):
    db = d.SessionLocal()
    rows = db.query(d.PovEnvironmentVM).filter(
        d.PovEnvironmentVM.environment_id == env_id).all()
    out = [(r.platform_vm_id, r.name, r.private_ip) for r in rows]
    db.close()
    return out


# ── provision ────────────────────────────────────────────────────────────────

def test_a_successful_provision_records_the_id_state_and_vms():
    a = FakeAdapter()
    o = _install(a)
    try:
        env_id = _new_env()
        asyncio.run(pov_env_service.run_env_provision(
            _job("pov_env_provision", env_id), {"environment_id": env_id}))
    finally:
        _restore(o)
    status, plat_id, runstate, err = _reload(env_id)
    assert status == pov_env_service.STATUS_ACTIVE, err
    assert plat_id == "sky-100"
    assert runstate == "running"
    assert _vm_rows(env_id) == [("v1", "dc01", "10.0.0.4")]


def test_the_platform_id_is_persisted_before_the_power_on():
    """The ordering that makes an orphan impossible: if power-on fails, the row must
    already know what to reap."""
    a = FakeAdapter(power_raises="capacity")
    o = _install(a)
    try:
        env_id = _new_env()
        jid = _job("pov_env_provision", env_id)
        asyncio.run(pov_env_service.run_env_provision(jid, {"environment_id": env_id}))
    finally:
        _restore(o)
    status, plat_id, _rs, err = _reload(env_id)
    assert status == pov_env_service.STATUS_FAILED
    assert plat_id == "sky-100", "the id was lost, so the environment is unreclaimable"
    assert "Destroy" in err, "the failure should tell the operator how to reclaim it"
    assert _job_status(jid)[0] == "failed"


def test_a_create_failure_leaves_no_id_and_says_so():
    a = FakeAdapter(create_raises="template not found")
    o = _install(a)
    try:
        env_id = _new_env()
        jid = _job("pov_env_provision", env_id)
        asyncio.run(pov_env_service.run_env_provision(jid, {"environment_id": env_id}))
    finally:
        _restore(o)
    status, plat_id, _rs, err = _reload(env_id)
    assert status == pov_env_service.STATUS_FAILED
    assert not plat_id
    assert "template not found" in err


def test_an_unconfigured_platform_is_refused_before_anything_is_created():
    a = FakeAdapter(configured=False)
    o = _install(a)
    try:
        env_id = _new_env()
        asyncio.run(pov_env_service.run_env_provision(
            _job("pov_env_provision", env_id), {"environment_id": env_id}))
    finally:
        _restore(o)
    assert a.calls == [], f"a resource was touched during preflight: {a.calls}"
    assert _reload(env_id)[0] == pov_env_service.STATUS_FAILED


def test_a_failed_idle_timer_does_not_fail_the_provision():
    """An environment that is up but will not auto-suspend costs money — worth a loud
    warning, not worth destroying a working environment over."""
    a = FakeAdapter(update_raises="idle not supported here")
    o = _install(a)
    try:
        env_id = _new_env()
        asyncio.run(pov_env_service.run_env_provision(
            _job("pov_env_provision", env_id),
            {"environment_id": env_id, "suspend_on_idle_seconds": 3600}))
    finally:
        _restore(o)
    assert _reload(env_id)[0] == pov_env_service.STATUS_ACTIVE


def test_a_failed_vm_readback_does_not_fail_the_provision():
    a = FakeAdapter(get_raises="rate limited")
    o = _install(a)
    try:
        env_id = _new_env()
        asyncio.run(pov_env_service.run_env_provision(
            _job("pov_env_provision", env_id), {"environment_id": env_id}))
    finally:
        _restore(o)
    status, plat_id, _rs, _e = _reload(env_id)
    assert status == pov_env_service.STATUS_ACTIVE, \
        "the environment is running; a failed read-back must not condemn it"
    assert plat_id == "sky-100"


# ── the empty-read guard ─────────────────────────────────────────────────────

def test_an_empty_vm_read_never_prunes_existing_rows():
    """A zero-VM read is how a whole inventory gets wiped while recording success."""
    a = FakeAdapter()
    o = _install(a)
    try:
        env_id = _new_env()
        asyncio.run(pov_env_service.run_env_provision(
            _job("pov_env_provision", env_id), {"environment_id": env_id}))
        assert len(_vm_rows(env_id)) == 1

        a.b["vms"] = []            # the transient bad read
        db = d.SessionLocal()
        env = pov_env_service.get(db, env_id)
        asyncio.run(pov_env_service.refresh_vms(db, env))
        db.close()
    finally:
        _restore(o)
    assert len(_vm_rows(env_id)) == 1, "an empty read pruned the VM rows"


def test_a_non_empty_read_does_prune_what_is_gone():
    a = FakeAdapter()
    o = _install(a)
    try:
        env_id = _new_env()
        asyncio.run(pov_env_service.run_env_provision(
            _job("pov_env_provision", env_id), {"environment_id": env_id}))
        a.b["vms"] = [{"id": "v2", "name": "web01", "os_family": "linux",
                       "private_ip": "10.0.0.9", "published_services": []}]
        db = d.SessionLocal()
        env = pov_env_service.get(db, env_id)
        asyncio.run(pov_env_service.refresh_vms(db, env))
        db.close()
    finally:
        _restore(o)
    assert [r[0] for r in _vm_rows(env_id)] == ["v2"]


# ── destroy ──────────────────────────────────────────────────────────────────

def test_destroy_reaps_the_platform_and_marks_the_row():
    a = FakeAdapter()
    o = _install(a)
    try:
        env_id = _new_env()
        asyncio.run(pov_env_service.run_env_provision(
            _job("pov_env_provision", env_id), {"environment_id": env_id}))
        asyncio.run(pov_env_service.run_env_destroy(
            _job("pov_env_destroy", env_id), {"environment_id": env_id}))
    finally:
        _restore(o)
    assert a.deleted == ["sky-100"]
    assert _reload(env_id)[0] == pov_env_service.STATUS_DESTROYED
    assert _vm_rows(env_id) == [], "the VM rows outlived their environment"


def test_a_failed_destroy_does_not_mark_the_row_destroyed():
    """Marking it would hide an environment that is still running and still billing."""
    a = FakeAdapter(delete_raises="platform unreachable")
    o = _install(a)
    try:
        env_id = _new_env()
        asyncio.run(pov_env_service.run_env_provision(
            _job("pov_env_provision", env_id), {"environment_id": env_id}))
        jid = _job("pov_env_destroy", env_id)
        asyncio.run(pov_env_service.run_env_destroy(jid, {"environment_id": env_id}))
    finally:
        _restore(o)
    status, plat_id, _rs, err = _reload(env_id)
    assert status != pov_env_service.STATUS_DESTROYED
    assert plat_id == "sky-100", "the id must survive so a re-run can finish the job"
    assert "may still exist" in err
    assert _job_status(jid)[0] == "failed"


def test_destroying_an_environment_that_was_never_created_succeeds():
    """A provision that failed before the create still leaves a row to clean up."""
    a = FakeAdapter()
    o = _install(a)
    try:
        env_id = _new_env(status=pov_env_service.STATUS_FAILED)
        jid = _job("pov_env_destroy", env_id)
        asyncio.run(pov_env_service.run_env_destroy(jid, {"environment_id": env_id}))
    finally:
        _restore(o)
    assert a.deleted == [], "nothing should have been deleted on the platform"
    assert _reload(env_id)[0] == pov_env_service.STATUS_DESTROYED
    assert _job_status(jid)[0] == "completed"


# ── power ────────────────────────────────────────────────────────────────────

def test_power_records_the_settled_state():
    a = FakeAdapter()
    o = _install(a)
    try:
        env_id = _new_env()
        asyncio.run(pov_env_service.run_env_provision(
            _job("pov_env_provision", env_id), {"environment_id": env_id}))
        asyncio.run(pov_env_service.run_env_power(
            _job("pov_env_power", env_id, runstate="suspended"),
            {"environment_id": env_id, "runstate": "suspended"}))
    finally:
        _restore(o)
    assert _reload(env_id)[2] == "suspended"


def test_power_on_an_environment_that_does_not_exist_yet_is_refused():
    a = FakeAdapter()
    o = _install(a)
    try:
        env_id = _new_env(status=pov_env_service.STATUS_FAILED)
        jid = _job("pov_env_power", env_id, runstate="running")
        asyncio.run(pov_env_service.run_env_power(
            jid, {"environment_id": env_id, "runstate": "running"}))
    finally:
        _restore(o)
    status, err = _job_status(jid)
    assert status == "failed"
    assert "never created" in err


# ── the state guard ──────────────────────────────────────────────────────────

def test_an_unrecognised_status_is_refused_rather_than_assumed_safe():
    """The same rule the expiry sweep follows: a state added later can only make this
    do less, never more."""
    db = d.SessionLocal()
    env = d.PovEnvironment(platform="skytap", name="x", status="reticulating")
    ok, why = pov_env_service.may_act_on(env)
    db.close()
    assert ok is False
    assert "unrecognised" in why


def test_a_provisioning_environment_is_not_actionable():
    env = d.PovEnvironment(platform="skytap", name="x",
                           status=pov_env_service.STATUS_PROVISIONING)
    ok, why = pov_env_service.may_act_on(env)
    assert ok is False and "still being provisioned" in why


def test_a_failed_environment_is_actionable_so_it_can_be_reaped():
    """The one that most needs reaping — refusing here would strand what it created."""
    env = d.PovEnvironment(platform="skytap", name="x",
                           status=pov_env_service.STATUS_FAILED)
    assert pov_env_service.may_act_on(env)[0] is True


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
