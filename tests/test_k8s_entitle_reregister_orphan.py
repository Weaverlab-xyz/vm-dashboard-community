"""A second Entitle register of a cluster must not orphan the first integration.

Live case (gke-east, 2026-09-30): Register in Entitle ran at 15:31 while integration
a1d7dca9 was already recorded. The register path overwrote the cluster's single
state slot, so a1d7dca9 stayed in the tenant with nothing left to delete it. It was a
private (agent-brokered) integration, so it also pinned the agent token: Entitle
refused to delete the token ("This token is used by integrations, in order to delete
it please remove all integrations first") and every k8s_decommission of the agent's
host cluster failed on that 400.

Pinned here, by running the real register_cluster_in_entitle against stubs:
  * a register with recorded state deregisters THAT integration first;
  * a failed deregister raises, keeps the state and registers nothing new;
  * a recorded id with no state fails loudly, naming the id, instead of orphaning it;
  * a first-time register touches nothing extra.

Pure source extraction + stubs, no app imports. Runs under pytest, or standalone:
    python tests/test_k8s_entitle_reregister_orphan.py
"""
import ast
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_K8S_PATH = os.path.join(_ROOT, "web_dashboard", "services", "k8s_service.py")
with open(_K8S_PATH, encoding="utf-8") as fh:
    _K8S_SRC = fh.read()

CID = "e2c3240d"


def _fn_code(name: str) -> str:
    tree = ast.parse(_K8S_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(_K8S_SRC, node) or ""
    raise AssertionError(f"{name}() not found")


class K8sError(Exception):
    pass


class _Row:
    id = CID
    name = "gke-east"
    cloud = "gcp"


class _Query:
    def filter(self, *_a):
        return self

    def first(self):
        return _Row()


class _Session:
    def query(self, _model):
        return _Query()

    def close(self):
        pass


class _Col:
    def __eq__(self, _other):
        return True


class _K8sCluster:
    id = _Col()


def _harness(conf: dict, deregister_fails: bool = False):
    calls = []

    async def deregister(state, ctx=None):
        calls.append(("deregister", state))
        if deregister_fails:
            raise RuntimeError("Entitle said no")

    async def register_kubernetes(**kw):
        calls.append(("register", kw.get("private")))
        return {"integration_id": "new-id", "tf_state_json": "new-state"}

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda k, *d: conf.get(k, "")
    cfg.set = lambda k, v: conf.__setitem__(k, v)
    ent = types.ModuleType("web_dashboard.services.entitle_registration_service")
    ent.deregister = deregister
    ent.register_kubernetes = register_kubernetes
    dbm = types.ModuleType("web_dashboard.database")
    dbm.SessionLocal = _Session
    pkg = types.ModuleType("web_dashboard")
    pkg.__path__ = []
    svc = types.ModuleType("web_dashboard.services")
    svc.__path__ = []
    svc.config_service = cfg
    svc.entitle_registration_service = ent
    saved = {k: sys.modules.get(k) for k in (
        "web_dashboard", "web_dashboard.services", "web_dashboard.database",
        "web_dashboard.services.config_service",
        "web_dashboard.services.entitle_registration_service")}
    sys.modules.update({
        "web_dashboard": pkg, "web_dashboard.services": svc, "web_dashboard.database": dbm,
        "web_dashboard.services.config_service": cfg,
        "web_dashboard.services.entitle_registration_service": ent})

    log = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    ns = {"__package__": "web_dashboard.services", "__name__": "web_dashboard.services.k8s_service",
          "K8sCluster": _K8sCluster, "K8sError": K8sError, "logger": log,
          "_cfg": lambda k, d="": d, "resolve_kubeconfig": lambda db, cid: "kubeconfig",
          "config_service": cfg}
    exec(compile(_fn_code("register_cluster_in_entitle"), "<extracted>", "exec"), ns)  # noqa: S102

    def restore():
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return ns["register_cluster_in_entitle"], calls, restore


def _in_cluster_conf(**extra):
    return {"entitle_agent_cluster_id": CID, **extra}


def test_a_second_register_removes_the_recorded_integration_first():
    conf = _in_cluster_conf(**{f"entitle_k8s_tfstate_{CID}": "old-state",
                               f"entitle_k8s_integration_id_{CID}": "a1d7dca9"})
    fn, calls, restore = _harness(conf)
    try:
        asyncio.run(fn(CID))
    finally:
        restore()
    assert calls == [("deregister", "old-state"), ("register", True)], calls
    assert conf[f"entitle_k8s_tfstate_{CID}"] == "new-state"
    assert conf[f"entitle_k8s_integration_id_{CID}"] == "new-id"


def test_a_failed_deregister_keeps_the_state_and_registers_nothing():
    conf = _in_cluster_conf(**{f"entitle_k8s_tfstate_{CID}": "old-state",
                               f"entitle_k8s_integration_id_{CID}": "a1d7dca9"})
    fn, calls, restore = _harness(conf, deregister_fails=True)
    try:
        try:
            asyncio.run(fn(CID))
            raise AssertionError("a failed deregister must raise")
        except RuntimeError:
            pass
    finally:
        restore()
    assert calls == [("deregister", "old-state")], "a second integration was created"
    assert conf[f"entitle_k8s_tfstate_{CID}"] == "old-state", "the only handle was dropped"


def test_a_recorded_id_without_state_fails_loudly_naming_it():
    conf = _in_cluster_conf(**{f"entitle_k8s_integration_id_{CID}": "a1d7dca9"})
    fn, calls, restore = _harness(conf)
    try:
        try:
            asyncio.run(fn(CID))
            raise AssertionError("an unremovable integration must not be silently replaced")
        except K8sError as exc:
            assert "a1d7dca9" in str(exc) and "Entitle console" in str(exc)
    finally:
        restore()
    assert calls == [], calls


def test_a_first_register_touches_nothing_else():
    conf = _in_cluster_conf()
    fn, calls, restore = _harness(conf)
    try:
        asyncio.run(fn(CID))
    finally:
        restore()
    assert calls == [("register", True)], calls


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    sys.exit(1 if failures else 0)
