"""Adding VMs to a POV that already exists — the endpoint, and the copy job.

Use case 18 of BeyondTrust's Skytap Password Safe POC is the runbook's strongest demo:
add two guests and a batch of AD users to a *running* environment and show the existing
Smart Rules onboard all of it with nothing edited. Use case 15 needs the same call for a
Password Safe Cache guest that template Part 1 does not carry. Neither was possible — the
adapter had no VM-add at all.

What is pinned here is mostly about the call being the RIGHT one, because Skytap gives two
plausible wrong answers and only one of them is loud:

  * **The merge is v1 and a PUT**, ``PUT /configurations/{id}.json``. The v2 form of that
    same verb at that same resource exists and is what ``update_environment`` uses; send
    ``template_id`` to it and Skytap answers **200 with the environment unchanged**. No
    error, no VMs, nothing to investigate. The two creates at least 404.
  * **An empty ``vm_ids`` is refused, not passed through.** The API reads an omitted list
    as "merge the whole template", which against a live POV means silently doubling it.
  * **A 409 has one documented cause** — a running Power VM among the ones being copied,
    which cannot be suspended for the copy — and nothing about "conflict" says so.

And about the job doing the two things an operator would otherwise be caught by:

  * the copies arrive **stopped** even when the environment is running, so the job powers
    it on;
  * the new guests are **not Config-Management targets** until the POV is re-brokered, so
    the job says so rather than re-enrolling the agent behind an SE's back.

No network: the adapter is faked at the module boundary.

Runs under pytest, or standalone:
    python tests/test_pov_add_vms.py
"""
import asyncio
import os
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-add-vms")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (job_service, lab_platforms,  # noqa: E402
                                    pov_env_service, skytap_service)


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _env(db, **kw):
    env = d.PovEnvironment(platform="skytap", name=_name("poc"),
                           platform_environment_id="sky-1",
                           status=pov_env_service.STATUS_ACTIVE,
                           runstate="running", **kw)
    db.add(env)
    db.commit()
    return env


class _FakeAdapter:
    """Stands in for the Skytap adapter at the boundary `pov_env_service` uses."""

    def __init__(self, **behaviour):
        self.b = behaviour
        self.added = []
        self.runstates = []

    async def add_vms(self, env_id, template_id, vm_ids):
        self.added.append((env_id, template_id, list(vm_ids)))
        if self.b.get("add_raises"):
            raise RuntimeError(self.b["add_raises"])
        return {"id": env_id, "runstate": "busy"}

    async def get_environment(self, env_id):
        return {"id": env_id, "runstate": self.b.get("after", "running"),
                "vms": self.b.get("vms", [
                    {"id": "vm-1", "name": "app01", "os_family": "windows",
                     "runstate": "running", "private_ip": "10.0.0.3",
                     "published_services": []},
                    {"id": "vm-2", "name": "app02", "os_family": "windows",
                     "runstate": "stopped", "private_ip": "10.0.0.13",
                     "published_services": []},
                ])}

    async def wait_for_runstate(self, env_id, target, **kw):
        if self.b.get("wait_raises"):
            raise RuntimeError(self.b["wait_raises"])
        return {"id": env_id, "runstate": target}

    async def set_runstate(self, env_id, runstate):
        self.runstates.append(runstate)
        if self.b.get("power_raises"):
            raise RuntimeError(self.b["power_raises"])
        return {"id": env_id, "runstate": runstate}


def _install(fake):
    original = pov_env_service._adapter
    pov_env_service._adapter = lambda env: fake
    return original


def _restore(original):
    pov_env_service._adapter = original


def _logs(db, job_id):
    """The Live Output lines for a job, joined. They live in `JobLog`, not on the row."""
    rows = (db.query(d.JobLog).filter(d.JobLog.job_id == job_id)
              .order_by(d.JobLog.seq).all())
    return chr(10).join(r.line or "" for r in rows)


def _run(db, env, **meta):
    job = job_service.create_job(
        db, job_type="pov_env_add_vms", created_by="se",
        metadata={"environment_id": env.id, "template_id": "tpl-part2",
                  "vm_ids": ["v-app02", "v-lin02"], **meta})
    asyncio.run(pov_env_service.run_env_add_vms(job.id, job.metadata_dict))
    db.expire_all()
    return db.query(d.Job).filter(d.Job.id == job.id).first()


# ── the adapter call is the right one ────────────────────────────────────────

def test_the_merge_is_the_v1_put_and_not_the_v2_one():
    """The trap. Both exist, both are a PUT at the same resource, and the v2 one answers
    200 with nothing changed -- so a test that only checked the method would pass against
    the call that silently does nothing."""
    import ast
    import inspect
    src = inspect.getsource(skytap_service.add_vms)
    v1 = '"PUT", f"/configurations/{env_id}.json"'
    assert v1 in src, "add_vms does not PUT the v1 configuration path"
    # The docstring names the v2 path to EXPLAIN the trap, so this reads the code with
    # the docstring removed rather than the whole source.
    # `getsource` on a module-level function is already unindented; `cleandoc` is for
    # docstrings and would strip the body's own indentation with it.
    fn = ast.parse(src).body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]
    wrong = "add_vms reaches the v2 path, which ignores template_id and reports success"
    assert "/v2/configurations" not in ast.unparse(fn), wrong


def test_the_merge_body_carries_the_template_and_the_chosen_vms():
    import inspect
    src = inspect.getsource(skytap_service.add_vms)
    assert '"template_id": template_id' in src and '"vm_ids": ids' in src


def test_an_empty_vm_list_is_refused_rather_than_merging_the_whole_template():
    for bad in ([], None, ["", "  "]):
        try:
            asyncio.run(skytap_service.add_vms("sky-1", "tpl-1", bad))
            raise AssertionError(f"an empty selection {bad!r} was accepted")
        except skytap_service.SkytapError as exc:
            assert "whole template" in str(exc)


def test_a_missing_environment_or_template_id_is_refused():
    try:
        asyncio.run(skytap_service.add_vms("", "tpl-1", ["v-1"]))
        raise AssertionError("no environment id was accepted")
    except skytap_service.SkytapError as exc:
        assert "environment id" in str(exc)
    try:
        asyncio.run(skytap_service.add_vms("sky-1", "", ["v-1"]))
        raise AssertionError("no template id was accepted")
    except skytap_service.SkytapError as exc:
        assert "template id" in str(exc)


def test_a_409_names_the_running_power_vm():
    """"Conflict" against an environment that is plainly fine reads as a Skytap fault. It
    has one documented cause and the message does not mention it."""
    import inspect
    src = inspect.getsource(skytap_service.add_vms)
    assert "409" in src and "Power VM" in src


def test_add_vms_is_exported():
    assert "add_vms" in skytap_service.__all__


# ── the capability ───────────────────────────────────────────────────────────

def test_only_skytap_can_add_vms_to_a_live_environment():
    assert lab_platforms.supports("skytap", "vm_add") is True
    for cloud in lab_platforms.CLOUD_PLATFORMS:
        assert lab_platforms.supports(cloud, "vm_add") is False, \
            f"{cloud} claims it can add VMs to an existing environment"


def test_every_platform_answers_the_capability_explicitly():
    """Omitting it reads as "cannot", which would be right for the clouds and wrong for
    Skytap — so the table is explicit and a new platform cannot forget."""
    for name in lab_platforms.VALID_PLATFORMS:
        assert "vm_add" in lab_platforms.CAPABILITIES[name]


# ── the job ──────────────────────────────────────────────────────────────────

def test_the_job_copies_then_re_reads_then_powers_on():
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakeAdapter()
    original = _install(fake)
    try:
        job = _run(db, env, power_on=True)
    finally:
        _restore(original)

    assert job.status == "completed", job.error_message
    assert fake.added == [("sky-1", "tpl-part2", ["v-app02", "v-lin02"])]
    assert "running" in fake.runstates, "the copies arrive stopped and were not powered on"
    # The re-read landed both guests as rows, which is what later slices join on.
    names = sorted(v.name for v in db.query(d.PovEnvironmentVM)
                   .filter(d.PovEnvironmentVM.environment_id == env.id).all())
    assert names == ["app01", "app02"]
    db.close()


def test_power_on_is_skippable():
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakeAdapter()
    original = _install(fake)
    try:
        job = _run(db, env, power_on=False)
    finally:
        _restore(original)
    assert job.status == "completed"
    assert "running" not in fake.runstates
    db.close()


def test_the_completion_says_the_new_guests_are_not_config_targets_yet():
    """The job deliberately does not re-broker: policy.yaml is written at enrolment, and
    re-enrolling the agent mid-POV is a bigger side effect than the one it would save."""
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakeAdapter()
    original = _install(fake)
    try:
        job = _run(db, env)
    finally:
        _restore(original)
    log = _logs(db, job.id) + (job.error_message or "")
    assert "Press Broker" in log, "the completion note does not name the re-broker"
    db.close()


def test_a_refused_copy_fails_the_job_and_adds_nothing():
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakeAdapter(add_raises="409 conflict")
    original = _install(fake)
    try:
        job = _run(db, env)
    finally:
        _restore(original)
    assert job.status == "failed"
    assert "refused the copy" in (job.error_message or "")
    db.close()


def test_an_environment_that_will_not_settle_is_a_note_not_a_failure():
    """The VMs are copied either way. Failing here would report a completed copy as a
    failure and invite somebody to run it twice."""
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakeAdapter(wait_raises="timed out")
    original = _install(fake)
    try:
        job = _run(db, env, power_on=False)
    finally:
        _restore(original)
    assert job.status == "completed", job.error_message
    assert "did not settle" in _logs(db, job.id)
    db.close()


def test_a_failed_power_on_is_a_note_not_a_failure():
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakeAdapter(power_raises="refused")
    original = _install(fake)
    try:
        job = _run(db, env, power_on=True)
    finally:
        _restore(original)
    assert job.status == "completed", job.error_message
    assert "did not power on" in _logs(db, job.id)
    db.close()


def test_an_environment_never_created_on_the_platform_is_refused():
    db = d.SessionLocal()
    env = _env(db)
    env.platform_environment_id = ""
    db.commit()
    fake = _FakeAdapter()
    original = _install(fake)
    try:
        job = _run(db, env)
    finally:
        _restore(original)
    assert job.status == "failed"
    assert "never created" in (job.error_message or "")
    assert fake.added == []
    db.close()


def test_a_platform_that_cannot_add_vms_is_refused_before_the_call():
    db = d.SessionLocal()
    env = _env(db)
    env.platform = "aws"
    db.commit()
    fake = _FakeAdapter()
    original = _install(fake)
    try:
        job = _run(db, env)
    finally:
        _restore(original)
    assert job.status == "failed"
    assert "cannot add VMs" in (job.error_message or "")
    assert fake.added == [], "the adapter was called for a platform that cannot do it"
    db.close()


def test_an_empty_vm_read_after_the_copy_never_prunes_the_existing_rows():
    """`refresh_vms` already owns this rule and it matters most here: a transient empty
    read right after a copy would delete every row, PAM artifact columns and all, and
    record the copy as a success."""
    db = d.SessionLocal()
    env = _env(db)
    db.add(d.PovEnvironmentVM(environment_id=env.id, platform_vm_id="vm-1",
                              name="app01", os_family="windows",
                              private_ip="10.0.0.3", pra_jump_id="j1"))
    db.commit()
    fake = _FakeAdapter(vms=[])
    original = _install(fake)
    try:
        job = _run(db, env, power_on=False)
    finally:
        _restore(original)
    assert job.status == "completed", job.error_message
    rows = db.query(d.PovEnvironmentVM).filter(
        d.PovEnvironmentVM.environment_id == env.id).all()
    assert [r.name for r in rows] == ["app01"]
    assert rows[0].pra_jump_id == "j1"
    db.close()


# ── the worker knows the job type ────────────────────────────────────────────

def test_the_worker_handles_and_tiers_the_job_type():
    from web_dashboard import jobs_worker as w
    assert "pov_env_add_vms" in w.HANDLED_TYPES
    tiered = set(w.MEDIUM_TYPES) | set(w.LIGHT_TYPES)
    assert "pov_env_add_vms" in tiered, "the job type has no concurrency tier"


def test_the_dispatch_names_the_handler():
    src = open(os.path.join(_ROOT, "web_dashboard", "jobs_worker.py"),
               encoding="utf-8").read()
    assert '"pov_env_add_vms": pov_env_service.run_env_add_vms,' in src


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
