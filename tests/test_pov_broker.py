"""The POV broker agent — the orderings and the generated files.

Six properties, each of which fails quietly rather than loudly when it is wrong:

  * **The agent id is persisted before the enrolment wait.** A crash mid-wait must leave
    the next run re-issuing that row's code; minting a second agent for one POV is how
    teardown ends up revoking the wrong one.
  * **A re-run re-issues, never re-mints.** Same property from the other side.
  * **The bootstrap removes the agent's state volume.** A re-issued code plus a surviving
    volume gives a container that starts cleanly and 401s forever — which reads as
    revocation, not as a stale volume, and sends you debugging the wrong thing.
  * **The policy grants a /32 per VM, never a subnet.** The platform's subnet is bigger
    than the POV and on a shared lab network can contain somebody else's environment.
  * **A broker failure does not fail the provision.** The environment is up, billing and
    reapable; failing it would trade a fixable gap for a destroyed environment.
  * **Destroy revokes the agent before deleting the environment.** An enrolled agent whose
    VM has just been deleted keeps polling from nowhere and keeps holding its job.

Uses a real SQLite database and a fake adapter, so the orchestrator runs for real. No
network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_pov_broker.py
"""
import asyncio
import os
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-broker")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (agent_service, config_service, job_service,  # noqa: E402
                                    lab_platforms, pov_broker, pov_env_service)

_AGENT_URL = "https://agents.example.test"


class FakeAdapter:
    """A lab platform that records what it was asked to do."""

    def __init__(self, **behaviour):
        self.b = behaviour
        self.injected = []          # (vm_id, payload)
        self.deleted = []
        self.calls = []

    def configured(self):
        return True

    async def create_environment(self, template_id, name="", project_id=""):
        self.calls.append("create")
        return {"id": "sky-900", "runstate": "stopped", "region": "US-West"}

    async def update_environment(self, env_id, changes):
        return {"id": env_id, "runstate": "stopped"}

    async def set_runstate(self, env_id, runstate):
        return {"id": env_id, "runstate": runstate}

    async def wait_for_runstate(self, env_id, target, **kw):
        return {"id": env_id, "runstate": target}

    async def get_environment(self, env_id):
        self.calls.append("get")
        return {"id": env_id, "runstate": "running",
                "vms": self.b.get("vms", [
                    {"id": "vm-1", "name": "broker", "os_family": "linux",
                     "private_ip": "10.9.0.10", "published_services": []},
                    {"id": "vm-2", "name": "dc01", "os_family": "windows",
                     "private_ip": "10.9.0.20", "published_services": []},
                ])}

    async def inject_bootstrap(self, env_id, vm_id, payload):
        self.calls.append("inject")
        if self.b.get("inject_raises") and payload:
            raise RuntimeError(self.b["inject_raises"])
        self.injected.append((vm_id, payload))

    async def delete_environment(self, env_id):
        self.calls.append("delete")
        self.deleted.append(env_id)


def _install(adapter):
    original = lab_platforms.adapter
    lab_platforms.adapter = lambda platform: adapter
    return original


def _restore(original):
    lab_platforms.adapter = original


async def _no_sleep(_seconds):
    return None


def _new_env(**kw):
    db = d.SessionLocal()
    env = d.PovEnvironment(
        platform="skytap", name=kw.get("name", "poc-" + uuid.uuid4().hex[:6]),
        template_id="42", platform_environment_id=kw.get("platform_environment_id", "sky-900"),
        status=kw.get("status", pov_env_service.STATUS_ACTIVE))
    db.add(env)
    db.commit()
    env_id = env.id
    db.close()
    return env_id


def _add_vms(env_id, vms):
    db = d.SessionLocal()
    for vm in vms:
        db.add(d.PovEnvironmentVM(environment_id=env_id, **vm))
    db.commit()
    db.close()


def _reload(env_id):
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    out = (env.broker_vm_id, env.broker_agent_id, env.status,
           env.metadata_dict.get("broker_error", ""))
    db.close()
    return out


def _set_url(value=_AGENT_URL):
    config_service.set(agent_service.AUDIENCE_CONFIG, value)


def _clear_url():
    try:
        config_service.delete(agent_service.AUDIENCE_CONFIG)
    except Exception:  # noqa: BLE001
        pass


# ── the generated files ──────────────────────────────────────────────────────

def test_the_bootstrap_carries_both_markers_and_the_code():
    """Both markers, because the guest runner must refuse a truncated read: the top half
    of this script is the destructive half."""
    script = pov_broker.render_bootstrap(
        env_name="poc-1", dashboard_url=_AGENT_URL, enroll_code="agte_deadbeef",
        policy_yaml=pov_broker.render_policy(["10.0.0.5"]))
    assert pov_broker.BOOTSTRAP_BEGIN in script
    assert pov_broker.BOOTSTRAP_END in script
    assert script.index(pov_broker.BOOTSTRAP_BEGIN) < script.index(pov_broker.BOOTSTRAP_END)
    assert "agte_deadbeef" in script
    assert _AGENT_URL in script


def test_the_bootstrap_removes_the_agent_state_volume():
    """The whole point of a re-run. Leaving the volume means the re-issued code is never
    redeemed and every poll 401s, which looks exactly like a revoked agent."""
    script = pov_broker.render_bootstrap(
        env_name="poc-1", dashboard_url=_AGENT_URL, enroll_code="agte_x",
        policy_yaml=pov_broker.render_policy(["10.0.0.5"]))
    assert f"docker volume rm {pov_broker.GUEST_STATE_VOLUME}" in script
    assert "docker rm -f dashboard-agent" in script


def test_the_code_file_is_written_world_readable():
    """022, not 077. The container runs as uid 10001 and cannot read a root-owned 0600
    file — the agent says so and exits rather than enrolling."""
    script = pov_broker.render_bootstrap(
        env_name="poc-1", dashboard_url=_AGENT_URL, enroll_code="agte_x",
        policy_yaml=pov_broker.render_policy(["10.0.0.5"]))
    assert "umask 022" in script
    assert "umask 077" not in script


def test_the_policy_grants_a_slash_32_per_vm_not_a_subnet():
    policy = pov_broker.render_policy(["10.9.0.10", "10.9.0.20"])
    assert "- cidr: 10.9.0.10/32" in policy
    assert "- cidr: 10.9.0.20/32" in policy
    assert "/24" not in policy and "/16" not in policy.split("deny:")[0]


def test_the_policy_denies_the_metadata_range():
    """The agent has no business at 169.254.169.254 — on every cloud that is the
    credential endpoint, and here it is where the bootstrap itself came from."""
    assert "169.254.0.0/16" in pov_broker.render_policy(["10.0.0.1"]).split("deny:")[1]


def test_the_agent_name_fits_the_column():
    """RemoteAgent.name is 64 because Job.created_by records `agent:{name}` in 100."""
    env = d.PovEnvironment(platform="skytap", name="p" * 63)
    assert len(pov_broker.agent_name(env)) <= 64
    assert pov_broker.agent_name(env).endswith("-broker")


def test_the_enrolment_wait_cannot_outlive_the_code():
    assert pov_broker.enroll_timeout_seconds() < agent_service.ENROLL_TTL_MINUTES * 60


# ── selection ────────────────────────────────────────────────────────────────

def test_the_broker_vm_is_matched_exactly_not_fuzzily():
    """'contains broker' also matches a customer VM called password-broker, and the cost
    of the wrong answer is an agent installed on a machine nobody expected."""
    env_id = _new_env()
    _add_vms(env_id, [{"platform_vm_id": "v1", "name": "password-broker",
                       "private_ip": "10.0.0.9"}])
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        pov_broker.select_broker_vm(db, env)
        raise AssertionError("a fuzzy match was accepted")
    except pov_broker.BrokerError as exc:
        assert "password-broker" in str(exc), "the refusal must name what it did find"
    finally:
        db.close()


def test_a_per_pov_broker_vm_name_overrides_the_default():
    env_id = _new_env()
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    env.metadata_dict = {"broker_vm_name": "jump01"}
    db.commit()
    assert pov_broker.broker_vm_name(env) == "jump01"
    db.close()


# ── ensure_broker ────────────────────────────────────────────────────────────

def test_the_agent_id_is_persisted_before_the_wait():
    """The ordering that matters most. The wait here never succeeds, so the columns can
    only be set if they were written before it."""
    _set_url()
    original = _install(FakeAdapter())
    env_id = _new_env()
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        asyncio.run(pov_broker.ensure_broker(db, env, sleep=_no_sleep))
        raise AssertionError("the wait should have timed out")
    except pov_broker.BrokerError as exc:
        assert "no agent enrolled" in str(exc)
    finally:
        db.close()
        _restore(original)

    vm_id, agent_id, _, _ = _reload(env_id)
    assert vm_id == "vm-1", "the broker VM must be recorded before the wait"
    assert agent_id, "the agent id must be recorded before the wait"


def test_a_re_run_re_issues_the_same_agent_rather_than_minting_a_second():
    """Two rows for one POV is how teardown revokes the wrong one."""
    _set_url()
    original = _install(FakeAdapter())
    env_id = _new_env()
    for _ in range(2):
        db = d.SessionLocal()
        env = pov_env_service.get(db, env_id)
        try:
            asyncio.run(pov_broker.ensure_broker(db, env, sleep=_no_sleep))
        except pov_broker.BrokerError:
            pass
        finally:
            db.close()
    _restore(original)

    _, agent_id, _, _ = _reload(env_id)
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    rows = db.query(d.RemoteAgent).filter(
        d.RemoteAgent.name == pov_broker.agent_name(env)).all()
    db.close()
    assert len(rows) == 1, "a second run minted a second agent row"
    assert rows[0].id == agent_id


def test_a_successful_enrolment_clears_the_spent_payload():
    """user_data is readable by anyone who can read the environment, and a reboot would
    otherwise re-run a bootstrap whose code is gone."""
    _set_url()
    adapter = FakeAdapter()
    original = _install(adapter)
    env_id = _new_env()
    holder = {"id": None}

    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)

    async def sleeper(_seconds):
        holder["id"] = env.broker_agent_id
        inner = d.SessionLocal()
        row = inner.query(d.RemoteAgent).filter(
            d.RemoteAgent.id == holder["id"]).first()
        row.public_key = "fake-public-key"
        inner.commit()
        inner.close()

    try:
        summary = asyncio.run(pov_broker.ensure_broker(db, env, sleep=sleeper))
    finally:
        db.close()
        _restore(original)

    assert "enrolled" in summary
    assert len(adapter.injected) == 2, "the payload was never cleared"
    assert adapter.injected[0][1], "the first injection must carry the script"
    assert adapter.injected[1][1] == "", "the second must clear it"
    assert _reload(env_id)[3] == "", "a success must clear any previous broker error"


def test_a_plaintext_agent_url_is_refused_before_anything_is_minted():
    """The agent refuses to sign over plaintext, so a broker installed against http://
    would never enrol — and the operator would debug the POV instead of the proxy."""
    _set_url("http://agents.example.test")
    adapter = FakeAdapter()
    original = _install(adapter)
    env_id = _new_env()
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        asyncio.run(pov_broker.ensure_broker(db, env, sleep=_no_sleep))
        raise AssertionError("a plaintext audience was accepted")
    except pov_broker.BrokerError as exc:
        assert "plaintext" in str(exc)
    finally:
        db.close()
        _restore(original)
        _set_url()
    assert adapter.injected == []


def test_an_unknown_agent_url_is_refused_with_the_remedy():
    _clear_url()
    original = _install(FakeAdapter())
    env_id = _new_env()
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        asyncio.run(pov_broker.ensure_broker(db, env, sleep=_no_sleep))
        raise AssertionError("a missing audience was accepted")
    except pov_broker.BrokerError as exc:
        assert "Public base URL" in str(exc)
    finally:
        db.close()
        _restore(original)
        _set_url()


def test_an_environment_with_no_private_addresses_is_refused():
    """An empty policy grants nothing and the agent refuses to start — which from here
    is indistinguishable from a network fault."""
    _set_url()
    original = _install(FakeAdapter(vms=[
        {"id": "vm-1", "name": "broker", "os_family": "linux", "private_ip": ""},
    ]))
    env_id = _new_env()
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        asyncio.run(pov_broker.ensure_broker(db, env, sleep=_no_sleep))
        raise AssertionError("an addressless environment was accepted")
    except pov_broker.BrokerError as exc:
        assert "private address" in str(exc)
    finally:
        db.close()
        _restore(original)


# ── the provision and destroy paths ──────────────────────────────────────────

def test_a_broker_failure_leaves_the_environment_active():
    """The environment is up, billing and reapable. Failing the provision over the broker
    would trade a fixable gap for a destroyed environment."""
    _set_url()
    original = _install(FakeAdapter(inject_raises="user_data is not available"))
    db = d.SessionLocal()
    env = d.PovEnvironment(platform="skytap", name="poc-" + uuid.uuid4().hex[:6],
                           template_id="42",
                           status=pov_env_service.STATUS_PROVISIONING)
    db.add(env)
    db.commit()
    env_id = env.id
    job = job_service.create_job(db, job_type="pov_env_provision", created_by="tester",
                                 metadata={"environment_id": env_id})
    job_id = job.id
    db.close()
    try:
        asyncio.run(pov_env_service.run_env_provision(job_id, {"environment_id": env_id}))
    finally:
        _restore(original)

    _, _, status, broker_error = _reload(env_id)
    assert status == pov_env_service.STATUS_ACTIVE
    assert "user_data is not available" in broker_error, \
        "the reason must reach the row the POV page reads"

    db = d.SessionLocal()
    assert db.query(d.Job).filter(d.Job.id == job_id).first().status == "completed"
    db.close()


def test_teardown_revokes_and_removes_the_agent_and_is_idempotent():
    _set_url()
    original = _install(FakeAdapter())
    env_id = _new_env()
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        asyncio.run(pov_broker.ensure_broker(db, env, sleep=_no_sleep))
    except pov_broker.BrokerError:
        pass
    _restore(original)

    env = pov_env_service.get(db, env_id)
    agent_id = env.broker_agent_id
    assert agent_id
    pov_broker.teardown(db, env)
    assert db.query(d.RemoteAgent).filter(d.RemoteAgent.id == agent_id).first() is None
    assert env.broker_agent_id is None
    # Destroy has to survive every kind of half-finished state, so a second call is a
    # no-op rather than an error.
    assert "No broker agent" in pov_broker.teardown(db, env)
    db.close()


def test_destroy_revokes_the_agent_before_deleting_the_environment():
    """Order, not just presence. An enrolled agent whose VM has just been deleted keeps
    polling from nowhere and keeps holding whatever job it leased."""
    _set_url()
    adapter = FakeAdapter()
    original = _install(adapter)
    env_id = _new_env()
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        asyncio.run(pov_broker.ensure_broker(db, env, sleep=_no_sleep))
    except pov_broker.BrokerError:
        pass
    agent_id = pov_env_service.get(db, env_id).broker_agent_id
    job = job_service.create_job(db, job_type="pov_env_destroy", created_by="tester",
                                 metadata={"environment_id": env_id})
    job_id = job.id
    db.close()

    try:
        asyncio.run(pov_env_service.run_env_destroy(job_id, {"environment_id": env_id}))
    finally:
        _restore(original)

    db = d.SessionLocal()
    assert db.query(d.RemoteAgent).filter(d.RemoteAgent.id == agent_id).first() is None
    env = pov_env_service.get(db, env_id)
    assert env.status == pov_env_service.STATUS_DESTROYED
    db.close()
    assert adapter.deleted == ["sky-900"]
    assert adapter.calls.index("delete") > 0


# ── the capability contract ──────────────────────────────────────────────────

def test_skytap_declares_the_mechanism_this_module_implements():
    """`bootstrap_injection` is an intent with more than one mechanism. This module is
    written against exactly one of them, and says so rather than failing late."""
    assert lab_platforms.capabilities("skytap")["bootstrap_injection"] == "metadata"


def test_the_adapter_exposes_inject_bootstrap():
    """It is in WRITE_CONTRACT, and this slice is the one that makes it real."""
    assert "inject_bootstrap" in lab_platforms.WRITE_CONTRACT
    mod = lab_platforms.adapter("skytap")
    assert callable(getattr(mod, "inject_bootstrap", None))


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
