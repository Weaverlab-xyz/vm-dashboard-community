"""Behavioral tests for ps_k8s_token_service — the register/deregister orchestration
and the address builder.

The register ORDER is the correctness argument, so it is what these tests pin:

    RBAC → read current token → managed system (seeded) → mirror → rotate →
    PRA push (FATAL) → delete the legacy Secret (only after the push)

Deleting the legacy ``<sa>-token`` Secret any earlier would destroy the credential the
PRA-brokered session is currently using; never deleting it leaves a cluster-admin token
the rotation plugin's label-scoped sweep will never touch. And a rotation whose PRA push
failed must FAIL the job — completing there ships a managed-but-not-mirrored token,
which is exactly the hole the feature exists to close.

Every collaborator module is a stub injected into sys.modules (function-level imports
resolve at call time), so this runs with no app, DB or Password Safe.
Runs under pytest or standalone:  python tests/test_ps_k8s_token_service.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── module scaffolding ────────────────────────────────────────────────────────────

class _Col:
    """Stands in for a SQLAlchemy column in class-level comparisons."""
    def __eq__(self, other):  # noqa: D105
        return True
    def __hash__(self):
        return 0
    def isnot(self, other):
        return True
    def in_(self, other):
        return True


class _Meta(type):
    def __getattr__(cls, name):
        return _Col()


def _load():
    pkg_web = sys.modules.setdefault("web_dashboard", types.ModuleType("web_dashboard"))
    pkg_web.__path__ = [os.path.join(_ROOT, "web_dashboard")]
    pkg_svc = sys.modules.setdefault("web_dashboard.services",
                                     types.ModuleType("web_dashboard.services"))
    pkg_svc.__path__ = [os.path.join(_ROOT, "web_dashboard", "services")]

    sys.modules.setdefault("sqlalchemy", types.ModuleType("sqlalchemy"))
    _orm = types.ModuleType("sqlalchemy.orm")
    _orm.Session = object
    sys.modules.setdefault("sqlalchemy.orm", _orm)

    _dbm = types.ModuleType("web_dashboard.database")
    _dbm.Job = _Meta("Job", (), {})
    _dbm.K8sCluster = _Meta("K8sCluster", (), {})
    sys.modules["web_dashboard.database"] = _dbm

    settings_stub = types.ModuleType("web_dashboard.config")
    settings_stub.settings = types.SimpleNamespace()
    sys.modules["web_dashboard.config"] = settings_stub

    path = os.path.join(_ROOT, "web_dashboard", "services", "ps_k8s_token_service.py")
    spec = importlib.util.spec_from_file_location(
        "web_dashboard.services.ps_k8s_token_service", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod, _dbm


svc, _dbm = _load()


class FakeDB:
    """query(Model).filter(...).first() → the canned row for that model."""
    def __init__(self, rows):
        self.rows = rows          # model-name → object
        self.commits = 0

    def query(self, model):
        db = self

        class _Q:
            def filter(self, *a, **kw):
                return self
            def first(self):
                return db.rows.get(getattr(model, "__name__", str(model)))
        return _Q()

    def commit(self):
        self.commits += 1


class Recorder:
    def __init__(self):
        self.events = []

    def hit(self, name):
        self.events.append(name)

    def index(self, name):
        return self.events.index(name)


def _row(**kw):
    base = dict(id="c-1", cloud="gcp", name="gke-demo", region="us-central1",
                api_server="https://1.2.3.4", deploy_job_id="job-1",
                ps_token_account_id=None, ps_pra_vault_account_id=None,
                pra_vault_account_id="77", status="registered")
    base.update(kw)
    return types.SimpleNamespace(**base)


def _install_stubs(rec, *, cfg=None, push_fails=False, checkout_value="tok"):
    """Inject stub collaborator modules and return (config_store, modules)."""
    store = {
        "k8s_ps_token_rotation_enabled": "1",
        "k8s_ps_functional_account_gcp": "psafe-rotator@p.iam.gserviceaccount.com",
        "k8s_ps_pravault_functional_account": "pra-config-api",
        "k8s_ps_workgroup": "55",
        "bt_api_host": "pra.example.com",
        "gcp_project_id": "proj-1",
    }
    store.update(cfg or {})

    m_cfg = types.ModuleType("web_dashboard.services.config_service")
    m_cfg.get = lambda key, workgroup=None: store.get(key, "")
    m_cfg.get_bool = lambda key, default=False: (
        store[key] not in ("0", "", "false") if key in store else default)
    m_cfg.set = lambda key, value, workgroup=None: store.__setitem__(key, value)
    m_cfg.delete = lambda key, workgroup=None: store.pop(key, None)

    m_k8s = types.ModuleType("web_dashboard.services.k8s_service")
    m_k8s.resolve_kubeconfig = lambda db, cid: "kubeconfig-yaml"

    async def _resolve(db, row, kubeconfig):
        rec.hit("read_current_token")
        return "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzYSJ9.c2ln", "minted"
    m_k8s._resolve_pra_sa_token = _resolve

    async def _rbac(db, cid, *, mode, subject_name=""):
        rec.hit("rbac")
        return "rotator RBAC applied (User x)"
    m_k8s.apply_ps_rotator_rbac = _rbac

    async def _rbac_rm(db, cid, *, mode):
        rec.hit("rbac_removed")
    m_k8s.remove_ps_rotator_rbac = _rbac_rm

    async def _del_secret(db, cid):
        rec.hit("delete_legacy_secret")
    m_k8s.delete_legacy_pra_token_secret = _del_secret

    m_ps = types.ModuleType("web_dashboard.services.ps_api_service")
    m_ps.configured = lambda: True

    async def _fa(name):
        return {"id": 88, "platform_id": 1008, "platform_name": "does not matter here",
                "account_name": name}
    m_ps.get_functional_account = _fa

    async def _pid(name):
        return 1008
    m_ps.get_platform_id = _pid

    async def _wg(name):
        return "55"
    m_ps.get_workgroup_id = _wg

    async def _change(account_id):
        rec.hit("rotate")
    m_ps.change_managed_account_password = _change

    async def _push(**kw):
        rec.hit("pra_push")
        if push_fails:
            raise RuntimeError("PUT ManagedAccounts/9/Credentials failed (422)")
        return {"sha256": "abc123def456", "pushed": True, "skipped": ""}
    m_ps.rotate_pra_vault_token = _push

    async def _checkout(account_id, *, duration_min=15, reason=""):
        rec.hit("checkout")
        return checkout_value
    m_ps.checkout_credential = _checkout

    m_res = types.ModuleType("web_dashboard.services.ps_resource_service")
    m_res._validate_k8ssa_dns_name = lambda addr: None
    _n = {"count": 0}

    async def _register(**kw):
        rec.hit("register_managed_system")
        rec.events.append(("registered", kw))
        _n["count"] += 1
        return {"managed_system_id": str(100 + _n["count"]),
                "managed_account_id": str(200 + _n["count"]),
                "tf_state_json": json.dumps({"n": _n["count"]})}
    m_res.register_managed_system = _register

    async def _deregister(tf_state_json):
        rec.hit(("deregistered", json.loads(tf_state_json)["n"]))
    m_res.deregister = _deregister

    m_hook = types.ModuleType("web_dashboard.services.ps_vm_hook")
    m_hook._platform_name_ok = lambda pname, *tokens: True

    m_job = types.ModuleType("web_dashboard.services.job_service")
    m_job.set_running = lambda db, jid: rec.hit("job_running")
    m_job.set_failed = lambda db, jid, msg: rec.events.append(("job_failed", msg))
    m_job.set_completed = lambda db, jid, result=None: rec.events.append(
        ("job_completed", result))

    pkg_api = types.ModuleType("web_dashboard.api")
    pkg_api.__path__ = []
    m_ws = types.ModuleType("web_dashboard.api.websocket")

    async def _bp(job_id, pct, msg, log_line=None):
        pass
    m_ws.broadcast_progress = _bp

    mods = {
        "web_dashboard.services.config_service": m_cfg,
        "web_dashboard.services.k8s_service": m_k8s,
        "web_dashboard.services.ps_api_service": m_ps,
        "web_dashboard.services.ps_resource_service": m_res,
        "web_dashboard.services.ps_vm_hook": m_hook,
        "web_dashboard.services.job_service": m_job,
        "web_dashboard.api": pkg_api,
        "web_dashboard.api.websocket": m_ws,
    }
    for name, mod in mods.items():
        sys.modules[name] = mod
    return store


def _fake_job(meta):
    return types.SimpleNamespace(metadata_dict=meta)


# ── the address builder ───────────────────────────────────────────────────────────

def test_build_address_per_cloud():
    a = svc.build_address(cloud="aws", region="us-east-1", cluster_name="prod",
                          mode="longlived")
    assert a == "eks;us-east-1;prod;longlived"
    a = svc.build_address(cloud="azure", subscription_id="sub", resource_group="rg",
                          cluster_name="c1", mode="longlived")
    assert a == "aks;sub;rg;c1;longlived"
    a = svc.build_address(cloud="gcp", project_id="p", location="us-central1-a",
                          cluster_name="c1", mode="bound", ttl_seconds=43200)
    assert a == "gke;p;us-central1-a;c1;bound;ttl=43200"
    a = svc.build_address(cloud="local", api_server="https://api.internal:6443",
                          mode="longlived", namespace="pra-access")
    assert a == "k8s;https://api.internal:6443;longlived;ns=pra-access"


def test_build_address_oci_takes_the_generic_path():
    # The plugin has no OCI provider; OKE rides k8s;<apiServerUrl>.
    a = svc.build_address(cloud="oci", api_server="https://oke.example:6443",
                          mode="longlived")
    assert a.startswith("k8s;https://oke.example:6443")


def test_build_address_clamps_the_bound_ttl_to_the_api_server_floor():
    a = svc.build_address(cloud="aws", region="r", cluster_name="c", mode="bound",
                          ttl_seconds=60)
    assert ";ttl=600" in a, "the API server silently caps below 600 — clamp, don't pass through"


def test_build_address_names_the_missing_part():
    try:
        svc.build_address(cloud="azure", subscription_id="sub", cluster_name="c1")
    except svc.PSK8sTokenError as exc:
        assert "resource group" in str(exc)
    else:
        raise AssertionError("a missing AKS resource group must be named")
    try:
        svc.build_address(cloud="local", api_server="")
    except svc.PSK8sTokenError as exc:
        assert "API server" in str(exc)
    else:
        raise AssertionError("a missing generic api_server must be named")


def test_build_address_appends_extra_options_verbatim():
    a = svc.build_address(cloud="gcp", project_id="p", location="l", cluster_name="c",
                          mode="longlived", extra_options="dnsEndpoint=true;serverName=x")
    assert a.endswith(";dnsEndpoint=true;serverName=x")


def test_build_address_rejects_an_unknown_mode():
    try:
        svc.build_address(cloud="aws", region="r", cluster_name="c", mode="forever")
    except svc.PSK8sTokenError:
        pass
    else:
        raise AssertionError("an unknown mode must be rejected")


# ── register: the order is the contract ─────────────────────────────────────────

def _run_register(rec, row, **kw):
    db = FakeDB({"K8sCluster": row,
                 "Job": _fake_job({"tf_variables": {
                     "project": "proj-1", "region": "us-central1",
                     "cluster_name": "k8s-gke-demo"}})})
    return asyncio.run(svc.register(db, "c-1", **kw)), db


def test_register_runs_the_steps_in_the_safe_order():
    rec = Recorder()
    _install_stubs(rec)
    row = _row()
    out, db = _run_register(rec, row)

    assert rec.index("rbac") < rec.index("read_current_token")
    assert rec.index("read_current_token") < rec.index("register_managed_system")
    assert rec.index("rotate") < rec.index("pra_push")
    assert rec.index("pra_push") < rec.index("delete_legacy_secret"), (
        "the legacy Secret is the credential PRA is USING until the push lands — "
        "deleting it earlier breaks every brokered session on a failed push")
    assert out["rotated"] and out["pra_mirrored"] and out["legacy_secret_deleted"]
    assert row.ps_token_account_id == "201"
    assert row.ps_pra_vault_account_id == "202"


def test_register_seeds_password_safe_with_the_live_token():
    rec = Recorder()
    _install_stubs(rec)
    out, _db = _run_register(rec, _row())
    seeded = [kw for e, kw in [ev for ev in rec.events if isinstance(ev, tuple)
                               and ev[0] == "registered"]]
    assert seeded and all(kw["initial_password"].startswith("eyJ") for kw in seeded), (
        "without seeding, Password Safe holds a placeholder while the cluster and PRA "
        "hold the real token — nothing works until the first rotation")


def test_a_failed_pra_push_fails_register_and_keeps_the_legacy_secret():
    rec = Recorder()
    _install_stubs(rec, push_fails=True)
    try:
        _run_register(rec, _row())
    except RuntimeError:
        pass
    else:
        raise AssertionError(
            "a managed-but-not-mirrored token is the exact hole this feature closes — "
            "the register must fail loudly")
    assert "delete_legacy_secret" not in rec.events


def test_no_pra_vault_account_means_no_mirror_and_the_secret_stays():
    rec = Recorder()
    _install_stubs(rec)
    row = _row(pra_vault_account_id=None)
    out, _db = _run_register(rec, row)
    assert "delete_legacy_secret" not in rec.events
    assert out["pra_mirrored"] is False
    assert any("PRA Vault account" in w for w in out["warnings"])


def test_register_is_idempotent_for_an_already_registered_cluster():
    rec = Recorder()
    _install_stubs(rec)
    out, _db = _run_register(rec, _row(ps_token_account_id="201"))
    assert out["already_registered"] is True
    assert "register_managed_system" not in rec.events
    assert "rbac" in rec.events, "re-applying the (idempotent) RBAC is the useful half"


def test_register_respects_the_master_gate():
    rec = Recorder()
    _install_stubs(rec, cfg={"k8s_ps_token_rotation_enabled": "0"})
    try:
        _run_register(rec, _row())
    except svc.PSK8sTokenError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("the master gate must refuse")


def test_current_token_requires_a_registration():
    rec = Recorder()
    _install_stubs(rec)
    db = FakeDB({"K8sCluster": _row(ps_token_account_id=None)})
    try:
        asyncio.run(svc.current_token(db, "c-1"))
    except svc.PSK8sTokenError:
        pass
    else:
        raise AssertionError("current_token without a registration must refuse")
    db = FakeDB({"K8sCluster": _row(ps_token_account_id="201")})
    tok = asyncio.run(svc.current_token(db, "c-1"))
    assert tok == "tok" and "checkout" in rec.events


# ── deregister ───────────────────────────────────────────────────────────────────

def test_deregister_destroys_the_mirror_before_the_token_system():
    rec = Recorder()
    store = _install_stubs(rec)
    row = _row(ps_token_account_id="201", ps_pra_vault_account_id="202")
    db = FakeDB({"K8sCluster": row})
    store["ps_k8s_token_c-1"] = json.dumps(
        {"tf_state": json.dumps({"n": 1}), "pravault_tf_state": json.dumps({"n": 2}),
         "token_mode": "longlived"})
    store["k8s_token_sync_c-1"] = json.dumps({"synced_change": "x"})

    out = asyncio.run(svc.deregister(db, "c-1"))
    assert out["removed"] is True and not out["errors"]
    dereg = [e for e in rec.events if isinstance(e, tuple) and e[0] == "deregistered"]
    assert dereg == [("deregistered", 2), ("deregistered", 1)], (
        "the mirror references the functional account — destroy in reverse "
        "registration order, the same rule the cloud-DB teardown follows")
    assert row.ps_token_account_id is None and row.ps_pra_vault_account_id is None
    assert "ps_k8s_token_c-1" not in store, "the registration state must be dropped"
    assert "k8s_token_sync_c-1" not in store, (
        "a stale watermark would suppress the first sync of a re-registered cluster")
    assert "rbac_removed" in rec.events


def test_deregister_without_a_registration_is_a_noop():
    rec = Recorder()
    _install_stubs(rec)
    db = FakeDB({"K8sCluster": _row()})
    out = asyncio.run(svc.deregister(db, "c-1"))
    assert out == {"ok": True, "removed": False}


# ── the worker entry owns its terminal state ─────────────────────────────────────

def test_run_fails_the_job_instead_of_raising():
    rec = Recorder()
    _install_stubs(rec, push_fails=True)
    db = FakeDB({"K8sCluster": _row(),
                 "Job": _fake_job({"tf_variables": {"project": "p", "region": "r",
                                                    "cluster_name": "c"}})})
    asyncio.run(svc.run(db, cluster_id="c-1", job_id="j-1", action="register"))
    assert any(isinstance(e, tuple) and e[0] == "job_failed" for e in rec.events)


def test_run_rejects_an_unknown_action():
    rec = Recorder()
    _install_stubs(rec)
    db = FakeDB({"K8sCluster": _row()})
    asyncio.run(svc.run(db, cluster_id="c-1", job_id="j-1", action="explode"))
    failed = [e for e in rec.events if isinstance(e, tuple) and e[0] == "job_failed"]
    assert failed and "explode" in failed[0][1]


if __name__ == "__main__":
    fns = [v for name, v in sorted(globals().items()) if name.startswith("test_")]
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
