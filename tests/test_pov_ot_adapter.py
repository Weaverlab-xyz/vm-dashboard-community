"""The OT DMZ broker as a POV component -- the FUXA HMI's Entitle adapter.

What is pinned here is mostly where things go and in what order:

  * **The host is the POV's Entitle agent host, and it must be an ot-broker guest**: the
    adapter's address resolves only inside that guest's k3s, beside the agent.
  * **Rotation is queued before the deploy**, both on the POV's broker agent, so the
    agent (one job at a time, oldest first) runs them in that order.
  * **Credentials travel by reference**: the job rows carry config-key NAMES.
  * **The grant names the HMI Web Jump the wire-up built**, and dry run is the default.
  * **The integration is agent-brokered, in the POV's own tenant**, registered once.
  * **Teardown deregisters with the same tenant, keeps the state on failure**, and runs
    before the agent token's teardown in the POV destroy.

Uses a real SQLite database and fakes for the tenant, Entitle and storage. No network.

Runs under pytest, or standalone:
    python tests/test_pov_ot_adapter.py
"""
import asyncio
import os
import sys
import uuid
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-ot-adapter")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (config_service, entitle_registration_service,  # noqa: E402
                                    ot_faas_service, pov_cell_roles as cr,
                                    pov_entitle_agent, pov_env_service,
                                    pov_ot_adapter as ad, pov_wireup, storage_service)

_STATE = '{"resources":[]}'


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _env(db):
    env = d.PovEnvironment(platform="aws", name=_name("poc"), platform_environment_id="e",
                           status=pov_env_service.STATUS_ACTIVE, gateway_name="gw",
                           entitle_tenant_id="t-ent")
    db.add(env)
    db.commit()
    return env


def _vm(db, env, name, role, ip, *, hmi_wired=True):
    vm = d.PovEnvironmentVM(environment_id=env.id, platform_vm_id=_name("vm"), name=name,
                            os_family="linux", private_ip=ip, cell_role=role)
    if role == cr.OT_SIM and hmi_wired:
        vm.cell_artifacts_dict = {"hmi": {"kind": "web_jump", "id": "1", "tf_state": _STATE}}
    db.add(vm)
    db.commit()
    return vm


def _pov(db, *, broker_role=cr.OT_BROKER, hmi_wired=True, installed=True):
    env = _env(db)
    host = _vm(db, env, "entitle", broker_role, "10.9.0.5")
    hmi = _vm(db, env, "plc01", cr.OT_SIM, "10.9.0.20", hmi_wired=hmi_wired)
    if installed:
        config_service.set(pov_entitle_agent.token_config_key(env.id), "tok")
        meta = env.metadata_dict
        meta["entitle_agent_token_name"] = "pov-agent"
        env.metadata_dict = meta
        db.commit()
    return env, host, hmi


class _Fakes:
    """Swap the outside world for recorders, and put it back."""

    def __init__(self, host_getter, *, register_raises=None, deregister_raises=None,
                 backend="local"):
        self.host_getter = host_getter
        self.registered = []
        self.deregistered = []
        self.b = {"register_raises": register_raises,
                  "deregister_raises": deregister_raises, "backend": backend}
        self.ctx = {"api_key": "k", "agent_token_name": "pov-agent"}

    def __enter__(self):
        fakes = self

        def preflight(db, env):
            return SimpleNamespace(id="agent-1", name="broker"), fakes.host_getter(), None

        async def tenant_ctx(db, env):
            return {"ctx": fakes.ctx, "label": "acme-entitle", "tenant": None}

        async def register_rest(**kw):
            fakes.registered.append(kw)
            if fakes.b["register_raises"]:
                raise RuntimeError(fakes.b["register_raises"])
            return {"integration_id": "int-7", "tf_state_json": _STATE}

        async def deregister(state, ctx=None):
            fakes.deregistered.append(ctx)
            if fakes.b["deregister_raises"]:
                raise RuntimeError(fakes.b["deregister_raises"])

        def build_package(workload=""):
            return "UEsFBg==", "a" * 64, workload or ot_faas_service.DEFAULT_WORKLOAD

        self.saved = [
            (pov_entitle_agent, "preflight", preflight),
            (pov_wireup, "entitle_tenant_ctx", tenant_ctx),
            (entitle_registration_service, "register_rest", register_rest),
            (entitle_registration_service, "deregister", deregister),
            (ot_faas_service, "build_package", build_package),
            (storage_service, "active_backend", lambda: fakes.b["backend"]),
        ]
        self.saved = [(mod, attr, getattr(mod, attr), new) for mod, attr, new in self.saved]
        for mod, attr, _old, new in self.saved:
            setattr(mod, attr, new)
        return self

    def __exit__(self, *exc):
        for mod, attr, old, _new in self.saved:
            setattr(mod, attr, old)


def _jobs(env_id):
    db = d.SessionLocal()
    rows = (db.query(d.Job).filter(d.Job.job_type == "agent_ansible").all())
    out = [j for j in rows if (j.metadata_dict or {}).get("pov_environment_id") == env_id]
    out.sort(key=lambda j: j.created_at)
    db.close()
    return out


# ── where it runs, and the refusals ──────────────────────────────────────────

def test_the_host_must_be_an_ot_broker_guest():
    db = d.SessionLocal()
    env, host, _hmi = _pov(db, broker_role=None)
    with _Fakes(lambda: host):
        try:
            ad.preflight(db, env)
            raise AssertionError("an adapter was allowed onto a guest with no OpenFaaS")
        except ad.OTAdapterError as exc:
            assert "ot-broker" in str(exc)
    db.close()


def test_the_entitle_agent_must_be_installed_first():
    db = d.SessionLocal()
    env, host, _hmi = _pov(db, installed=False)
    with _Fakes(lambda: host):
        try:
            ad.preflight(db, env)
            raise AssertionError("an agent-brokered integration without an agent")
        except ad.OTAdapterError as exc:
            assert "Entitle agent" in str(exc)
    db.close()


def test_the_hmi_web_jump_must_exist():
    db = d.SessionLocal()
    env, host, _hmi = _pov(db, hmi_wired=False)
    with _Fakes(lambda: host):
        try:
            ad.preflight(db, env)
            raise AssertionError("a grant would name a Web Jump that does not exist")
        except ad.OTAdapterError as exc:
            assert "Wire up" in str(exc)
    db.close()


def test_exactly_one_ot_simulator_is_required():
    db = d.SessionLocal()
    env, host, _hmi = _pov(db)
    _vm(db, env, "plc02", cr.OT_SIM, "10.9.0.21")
    with _Fakes(lambda: host):
        try:
            ad.preflight(db, env)
            raise AssertionError("two HMIs and one adapter")
        except ad.OTAdapterError as exc:
            assert "plc01" in str(exc) and "plc02" in str(exc)
    db.close()


def test_no_storage_backend_is_refused_naming_the_plays():
    db = d.SessionLocal()
    env, host, _hmi = _pov(db)
    with _Fakes(lambda: host, backend=""):
        try:
            ad.preflight(db, env)
            raise AssertionError("queued plays nobody can fetch")
        except ad.OTAdapterError as exc:
            assert ot_faas_service.FAAS_DEPLOY_PLAYBOOK in str(exc)
    db.close()


# ── the jobs ─────────────────────────────────────────────────────────────────

def test_rotation_is_queued_before_the_deploy_on_the_broker_agent():
    db = d.SessionLocal()
    env, host, hmi = _pov(db)
    eid, ename = env.id, env.name
    with _Fakes(lambda: host) as fakes:
        note = asyncio.run(ad.queue(db, env, created_by="se"))
    jobs = _jobs(eid)
    assert [j.metadata_dict["asset"] for j in jobs] == [
        ot_faas_service.FUXA_ROTATE_PLAYBOOK, ot_faas_service.FAAS_DEPLOY_PLAYBOOK]
    for job in jobs:
        meta = job.metadata_dict
        assert job.agent_id == "agent-1"
        assert meta["target_host"] == "10.9.0.5", "the plays must run on the broker guest"
        assert meta["pov_vm_id"] == host.platform_vm_id
        # Names of config keys, never values.
        for ref in meta["secret_vars"].values():
            assert ref.startswith(f"ot/{eid}/"), ref

    rotate, deploy = (j.metadata_dict for j in jobs)
    assert rotate["extra_vars"]["fuxa_url"] == "http://10.9.0.20:1881"
    env_vars = deploy["extra_vars"]["otfn_env"]
    assert env_vars["FN_FUXA_JUMP_ITEM"] == f"{ename}-plc01-hmi"
    assert env_vars["FN_FUXA_URL"] == "http://10.9.0.20:1881"
    assert env_vars["FN_FUXA_DRY_RUN"] == "1", "dry run is the default"
    assert "dry-run" in note

    assert len(fakes.registered) == 1
    reg = fakes.registered[0]
    assert reg["private"] is True and reg["ctx"] is fakes.ctx
    assert reg["base_url"] == ot_faas_service.base_url()
    db.refresh(env)
    assert config_service.get(ad.state_config_key(eid)) == _STATE
    assert ad.describe(env)["ot_adapter_registered"] is True
    db.close()


def test_going_live_redeploys_without_registering_twice():
    db = d.SessionLocal()
    env, host, _hmi = _pov(db)
    with _Fakes(lambda: host) as fakes:
        asyncio.run(ad.queue(db, env))
        note = asyncio.run(ad.queue(db, env, dry_run=False))
    deploy = _jobs(env.id)[-1].metadata_dict
    assert deploy["extra_vars"]["otfn_env"]["FN_FUXA_DRY_RUN"] == "0"
    assert len(fakes.registered) == 1, "a second press registered a second integration"
    assert "Already registered" in note
    db.refresh(env)
    assert ad.describe(env)["ot_adapter_dry_run"] is False
    db.close()


def test_a_failed_registration_is_a_refusal_with_the_tenant_named():
    db = d.SessionLocal()
    env, host, _hmi = _pov(db)
    with _Fakes(lambda: host, register_raises="403 forbidden"):
        try:
            asyncio.run(ad.queue(db, env))
            raise AssertionError("a failed registration was reported as success")
        except ad.OTAdapterError as exc:
            assert "acme-entitle" in str(exc) and "403" in str(exc)
    assert not config_service.get(ad.state_config_key(env.id))
    db.close()


# ── teardown ─────────────────────────────────────────────────────────────────

def test_teardown_deregisters_with_the_povs_tenant_and_clears_the_secrets():
    db = d.SessionLocal()
    env, host, _hmi = _pov(db)
    with _Fakes(lambda: host) as fakes:
        asyncio.run(ad.queue(db, env))
        line = asyncio.run(ad.teardown(db, env))
    assert fakes.deregistered == [fakes.ctx]
    assert "Removed" in line
    for key in (ad.state_config_key(env.id), ot_faas_service.bearer_config_key(env.id),
                ot_faas_service.fuxa_admin_config_key(env.id)):
        assert not config_service.get(key), f"{key} survived the teardown"
    db.refresh(env)
    assert ad.describe(env)["ot_adapter_registered"] is False
    db.close()


def test_a_failed_deregister_keeps_the_state_and_the_bearer():
    db = d.SessionLocal()
    env, host, _hmi = _pov(db)
    with _Fakes(lambda: host, deregister_raises="503") as _fakes:
        asyncio.run(ad.queue(db, env))
        line = asyncio.run(ad.teardown(db, env))
    assert "by hand" in line
    assert config_service.get(ad.state_config_key(env.id)), "the only handle was dropped"
    assert config_service.get(ot_faas_service.bearer_config_key(env.id))
    db.close()


def test_the_adapter_comes_out_before_the_agent_token_in_a_pov_destroy():
    """The integration is agent-brokered and its teardown reads the agent's name off the
    row, which the token teardown clears."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "pov_env_service.py"),
               encoding="utf-8").read()
    body = src.split("async def run_env_destroy", 1)[1]
    assert body.index("pov_ot_adapter.teardown") < body.index("pov_entitle_agent.teardown")


# ── the shared spec ──────────────────────────────────────────────────────────

def test_deploy_spec_dry_run_overrides_only_when_asked():
    original = ot_faas_service.build_package
    ot_faas_service.build_package = lambda workload="": ("UEsFBg==", "a" * 64,
                                                         "fuxa_hmi_access")
    try:
        record = {"instance_name": "x", "ot_hmi_url": "http://h:1881"}
        owner = _name("owner")
        live = ot_faas_service.deploy_spec(owner, record, dry_run=False)
        assert live["extra_vars"]["otfn_env"]["FN_FUXA_DRY_RUN"] == "0"
        default = ot_faas_service.deploy_spec(owner, record)
        assert default["extra_vars"]["otfn_env"]["FN_FUXA_DRY_RUN"] in ("0", "1")
        assert live["secret_vars"]["otfn_bearer"] == ot_faas_service.bearer_config_key(owner)
    finally:
        ot_faas_service.build_package = original


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
