"""Stop / start for Kubernetes clusters (services/k8s_power).

Against the real models on SQLite, with the cloud calls replaced. Pins:

  * the state machine, and that a start returns the row to the status it was stopped
    FROM (registered / managed / awaiting_agent), not a hard-coded one;
  * the restore round trip: the node pool sizes and autoscaling recorded at stop are
    exactly what start puts back, and they are committed BEFORE the first pool is
    touched, so a stop that dies halfway still knows them;
  * merge_restore: a re-stop after a partial failure never records a 0 over a size;
  * the GKE and EKS backends' API calls (autoscaling off before setSize 0; EKS keeps
    maxSize >= 1) against fake HTTP / boto3 clients;
  * refusals while provisioning / decommissioning / mid-transition, and for registered
    and OKE clusters;
  * decommission from stopped: allowed, and only AKS is started first;
  * the Terraform modules ignore the node-count drift a stop creates;
  * worker registration and the route's 404 / 400 / 409.

Runs under pytest, or standalone:  python tests/test_k8s_power.py
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
from _schema import build_schema  # noqa: E402

build_schema("k8s-power")

from web_dashboard.database import Job, K8sCluster, SessionLocal  # noqa: E402
from web_dashboard.services import job_service  # noqa: E402
from web_dashboard.services import k8s_power as kp  # noqa: E402
from web_dashboard.services import k8s_service  # noqa: E402

kp.POLL_INTERVAL_S = 0.0


class FakeBackend:
    """Pools as a dict of {name: entry}; stop/start mutate it like the cloud would."""

    def __init__(self, pools, fail_stop_after=None):
        self.pools = {k: dict(v) for k, v in pools.items()}
        self.calls = []
        self.fail_stop_after = fail_stop_after

    def snapshot(self):
        self.calls.append("snapshot")
        return {k: dict(v) for k, v in self.pools.items()}

    def stop(self, restore):
        self.calls.append(("stop", sorted(restore)))
        for i, name in enumerate(sorted(restore)):
            if self.fail_stop_after is not None and i >= self.fail_stop_after:
                raise RuntimeError("GKE: operation already in progress")
            self.pools[name]["size"] = 0
            self.pools[name]["autoscaling"] = None

    def start(self, restore, fallback):
        self.calls.append(("start", json.loads(json.dumps(restore)), fallback))
        for name, entry in restore.items():
            self.pools[name] = dict(entry)


def _db():
    return SessionLocal()


def _row(db, **kw):
    deploy = Job(job_type="k8s_provision", created_by="alice", status="completed")
    deploy.metadata_dict = {"tf_variables": {"cluster_name": "k8s-demo", "node_count": 2}}
    db.add(deploy)
    db.commit()
    fields = dict(cloud="gcp", name="demo", status="registered", source="provisioned",
                  region="us-central1", deploy_job_id=deploy.id, created_by="alice")
    fields.update(kw)
    row = K8sCluster(**fields)
    db.add(row)
    db.commit()
    return row


def _run(db, row, job_id, action, backend):
    kp._BACKEND_FACTORY = lambda _r: backend
    try:
        asyncio.run(kp.run_power(db, cluster_id=row.id, job_id=job_id, action=action))
    finally:
        kp._BACKEND_FACTORY = None
    db.expire_all()
    return db.query(K8sCluster).filter(K8sCluster.id == row.id).first()


def _job(db, job_id):
    db.expire_all()
    return db.query(Job).filter(Job.id == job_id).first()


_POOLS = {
    "k8s-demo-ng": {"size": 3, "autoscaling": {"enabled": True, "minNodeCount": 1,
                                               "maxNodeCount": 5}},
    "gpu": {"size": 1, "autoscaling": None},
}


# ── state machine + restore round trip ───────────────────────────────────────

def test_stop_start_restores_exact_sizes_and_status():
    db = _db()
    row = _row(db, status="managed")
    out = k8s_service.start_power(db, row.id, "stop", "alice")
    assert out["status"] == "stopping"
    assert _job(db, out["job_id"]).job_type == "k8s_power"
    backend = FakeBackend(_POOLS)
    row = _run(db, row, out["job_id"], "stop", backend)
    assert row.status == "stopped"
    st = kp.read_state(row)
    assert st["restore"] == _POOLS and st["resume_status"] == "managed"
    assert all(p["size"] == 0 for p in backend.pools.values())

    out = k8s_service.start_power(db, row.id, "start", "alice")
    row = _run(db, row, out["job_id"], "start", backend)
    assert row.status == "managed", "start returns the row to where it was stopped from"
    assert backend.pools == _POOLS, "start must put back exactly what stop recorded"
    st = kp.read_state(row)
    assert "restore" not in st and st["state"] == "running"
    assert _job(db, out["job_id"]).status == "completed"
    db.close()


def test_restore_is_committed_before_the_stop_and_survives_a_partial_failure():
    db = _db()
    row = _row(db)
    out = k8s_service.start_power(db, row.id, "stop")
    backend = FakeBackend(_POOLS, fail_stop_after=1)     # first pool scaled, then error
    row = _run(db, row, out["job_id"], "stop", backend)
    assert row.status == "registered", "a failed stop puts the row back"
    st = kp.read_state(row)
    assert st["restore"] == _POOLS, "the sizes must be on the row even though stop failed"
    assert "operation already in progress" in st["last_error"]
    assert _job(db, out["job_id"]).status == "failed"
    # Retry: one pool is now at 0 in the cloud; its recorded size must survive.
    out = k8s_service.start_power(db, row.id, "stop")
    backend.fail_stop_after = None
    row = _run(db, row, out["job_id"], "stop", backend)
    assert row.status == "stopped"
    assert kp.read_state(row)["restore"] == _POOLS
    db.close()


def test_merge_restore():
    old = {"a": {"size": 3}, "b": {"size": 2}}
    new = {"a": {"size": 0}, "b": {"size": 4}, "c": {"size": 1}}
    assert kp.merge_restore(old, new) == {"a": {"size": 3}, "b": {"size": 4},
                                          "c": {"size": 1}}
    assert kp.merge_restore({}, {"a": {"size": 0}}) == {"a": {"size": 0}}


def test_start_without_restore_uses_the_terraform_size():
    db = _db()
    row = _row(db, status="stopped", power_state=json.dumps({"state": "stopped"}))
    out = k8s_service.start_power(db, row.id, "start")
    backend = FakeBackend({})
    _run(db, row, out["job_id"], "start", backend)
    assert backend.calls[-1] == ("start", {}, 2), backend.calls


def test_noops_dedupe_and_refusals():
    db = _db()
    row = _row(db)
    assert k8s_service.start_power(db, row.id, "start")["noop"] is True
    first = k8s_service.start_power(db, row.id, "stop")
    assert k8s_service.start_power(db, row.id, "stop")["job_id"] == first["job_id"]
    try:
        k8s_service.start_power(db, row.id, "start")
        raise AssertionError("opposite action while one runs must be refused")
    except kp.PowerRefused:
        pass
    for status in ("provisioning", "deploying", "decommissioning", "failed"):
        r = _row(db, status=status)
        for action in ("stop", "start"):
            try:
                k8s_service.start_power(db, r.id, action)
                raise AssertionError(f"{action} from {status} must be refused")
            except kp.PowerRefused:
                pass
    for kw in ({"source": "registered"}, {"cloud": "oci"}, {"cloud": "local"},
               {"deploy_job_id": None}):
        r = _row(db, **kw)
        try:
            k8s_service.start_power(db, r.id, "stop")
            raise AssertionError(f"{kw} must be unsupported")
        except kp.PowerUnsupported:
            pass
    try:
        k8s_service.start_power(db, "nope", "stop")
        raise AssertionError("unknown id")
    except k8s_service.K8sError:
        pass
    db.close()


def test_stale_job_reverts_the_row():
    db = _db()
    row = _row(db, status="awaiting_agent")
    out = k8s_service.start_power(db, row.id, "stop")
    job = _job(db, out["job_id"])
    job.status = "failed"
    db.commit()
    job_service._flip_resource_row(db, job)
    db.commit()
    db.refresh(row)
    assert row.status == "awaiting_agent"
    db.close()


# ── the cloud backends' calls ────────────────────────────────────────────────

class _Resp:
    def __init__(self, body, code=200):
        self._body, self.status_code = body, code

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _GkeSession:
    def __init__(self):
        self.posts = []

    def get(self, url):
        if url.endswith("/nodePools"):
            return _Resp({"nodePools": [
                {"name": "np1", "initialNodeCount": 1,
                 "instanceGroupUrls": ["https://x/zones/z/instanceGroupManagers/ig1"],
                 "autoscaling": {"enabled": True, "minNodeCount": 1, "maxNodeCount": 4}},
                {"name": "np2", "initialNodeCount": 2, "instanceGroupUrls": []}]})
        if "instanceGroupManagers" in url:
            return _Resp({"targetSize": 3})
        if "/operations/" in url:
            return _Resp({"status": "DONE"})
        raise AssertionError(url)

    def post(self, url, json=None):
        self.posts.append((url.rsplit("/", 1)[-1], json))
        return _Resp({"name": "op-1"})


def test_gke_backend_calls():
    s = _GkeSession()
    b = kp._GkeBackend("proj", "k8s-demo", "us-central1-a")
    b._s = lambda: s
    snap = b.snapshot()
    assert snap == {"np1": {"size": 3, "autoscaling": {"enabled": True, "minNodeCount": 1,
                                                        "maxNodeCount": 4}},
                    "np2": {"size": 2, "autoscaling": None}}
    b.stop(snap)
    assert s.posts == [
        ("np1:setAutoscaling", {"autoscaling": {"enabled": False}}),   # off FIRST
        ("np1:setSize", {"nodeCount": 0}),
        ("np2:setSize", {"nodeCount": 0}),
    ], s.posts
    s.posts.clear()
    b.start(snap, 1)
    assert s.posts == [
        ("np1:setSize", {"nodeCount": 3}),
        ("np1:setAutoscaling", {"autoscaling": snap["np1"]["autoscaling"]}),
        ("np2:setSize", {"nodeCount": 2}),
    ], s.posts


class _Eks:
    def __init__(self):
        self.updates = []
        self.cfg = {"ng1": {"minSize": 1, "maxSize": 3, "desiredSize": 2}}

    def list_nodegroups(self, **kw):
        return {"nodegroups": list(self.cfg)}

    def describe_nodegroup(self, clusterName, nodegroupName):
        return {"nodegroup": {"scalingConfig": self.cfg[nodegroupName]}}

    def update_nodegroup_config(self, clusterName, nodegroupName, scalingConfig):
        self.updates.append((nodegroupName, scalingConfig))

    def get_waiter(self, name):
        assert name == "nodegroup_active"
        return SimpleNamespace(wait=lambda **kw: None)


def test_eks_backend_calls():
    c = _Eks()
    b = kp._EksBackend("k8s-demo", "us-east-2")
    b._c = lambda: c
    snap = b.snapshot()
    assert snap == {"ng1": {"size": 2, "min": 1, "max": 3}}
    b.stop(snap)
    assert c.updates[-1] == ("ng1", {"minSize": 0, "desiredSize": 0, "maxSize": 3})
    b.start(snap, 1)
    assert c.updates[-1] == ("ng1", {"minSize": 1, "desiredSize": 2, "maxSize": 3})


# ── decommission from stopped ────────────────────────────────────────────────

def test_decommission_from_stopped_and_aks_start_first():
    db = _db()
    row = _row(db, status="stopped", power_state=json.dumps({"state": "stopped"}))
    out = k8s_service.start_decommission(db, row.id, created_by="alice")
    assert out["job_id"]
    db.refresh(row)
    assert row.status == "decommissioning"
    busy = _row(db, status="starting")
    try:
        k8s_service.start_decommission(db, busy.id)
        raise AssertionError("mid-transition decommission must be refused")
    except kp.PowerRefused:
        pass
    for cloud, want in (("azure", True), ("gcp", False), ("aws", False)):
        r = _row(db, cloud=cloud, status="decommissioning",
                 power_state=json.dumps({"state": "stopped"}))
        backend = FakeBackend({})
        kp._BACKEND_FACTORY = lambda _r, b=backend: b
        try:
            assert asyncio.run(kp.ensure_running_for_teardown(db, r, "job")) is None
        finally:
            kp._BACKEND_FACTORY = None
        assert any(c[0] == "start" for c in backend.calls) is want, (cloud, backend.calls)
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "k8s_service.py"),
               encoding="utf-8").read()
    body = src[src.index("async def run_decommission"):]
    assert body.index("ensure_running_for_teardown") < body.index("ps_k8s_token_service.deregister")
    db.close()


def test_stopped_cluster_is_reapable():
    from web_dashboard.services import expiry_policy
    assert "stopped" in expiry_policy._REAPABLE_STATES["k8s"]
    assert not kp.TRANSITIONAL & expiry_policy._REAPABLE_STATES["k8s"]


def test_terraform_ignores_node_count_drift():
    gke = open(os.path.join(_ROOT, "terraform", "k8s_cluster", "gcp_gke", "main.tf"),
               encoding="utf-8").read()
    eks = open(os.path.join(_ROOT, "terraform", "k8s_cluster", "aws_eks", "main.tf"),
               encoding="utf-8").read()
    pool = gke[gke.index('resource "google_container_node_pool"'):]
    pool = pool[:pool.index("\n}\n")]
    assert "ignore_changes = [node_count, autoscaling]" in pool
    ng = eks[eks.index('resource "aws_eks_node_group"'):]
    ng = ng[:ng.index("\n}\n")]
    assert "scaling_config[0].desired_size" in ng and "scaling_config[0].min_size" in ng


# ── worker + route ───────────────────────────────────────────────────────────

def test_worker_registration():
    from web_dashboard import jobs_worker as jw
    assert "k8s_power" in jw.HANDLED_TYPES and "k8s_power" in jw.MEDIUM_TYPES
    src = open(jw.__file__, encoding="utf-8").read()
    assert 'job_type == "k8s_power"' in src and "k8s_service.run_power" in src


def test_route():
    from fastapi import HTTPException
    from web_dashboard.api import k8s as api
    db = _db()
    alice = SimpleNamespace(username="alice", is_effective_admin=False, workgroups_list=[])
    mallory = SimpleNamespace(username="mallory", is_effective_admin=False, workgroups_list=[])
    row = _row(db)
    reg = _row(db, source="registered")

    def call(r, user, action):
        return api.power_cluster(r.id, api.PowerRequest(action=action), db=db,
                                 current_user=user)

    for r, user, action, code in ((row, mallory, "stop", 404), (reg, alice, "stop", 400),
                                  (row, alice, "reboot", 400)):
        try:
            call(r, user, action)
            raise AssertionError(f"expected {code}")
        except HTTPException as exc:
            assert exc.status_code == code, (exc.status_code, exc.detail)
    assert call(row, alice, "stop")["status"] == "stopping"
    try:
        call(row, alice, "start")
        raise AssertionError("expected 409")
    except HTTPException as exc:
        assert exc.status_code == 409
    import inspect
    assert 'require_permission("k8s", "write")' in inspect.getsource(api.power_cluster)
    db.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
