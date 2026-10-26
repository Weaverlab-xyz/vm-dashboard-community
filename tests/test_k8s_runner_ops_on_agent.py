"""Kubernetes runner operations on a cluster's owned agent (phase 3 part B of
docs/design/remote-worker-and-agent.md).

  * **Same callers, same shapes.** With an executor, each wrapper hands its work to the
    agent as `agent_k8s` steps; without one it runs the worker command it always ran.
  * **The step set stays closed.** A helm argv the agent's step has no field for, a `--set`
    it would refuse, or a chart named through a repo alias it does not have stays on the
    worker rather than failing on the agent.
  * **What moves:** ESO, the Entitle agent, the Entra binding, the impersonator binding,
    the PS rotator RBAC, the legacy PRA token delete, workload RBAC.
  * **What does not:** the token mints (they hand a secret back) and a URL manifest (the
    Rancher import).

Runs under pytest, or standalone:  python tests/test_k8s_runner_ops_on_agent.py
"""
import asyncio
import inspect
import os
import sys
import tempfile
import types
from datetime import datetime, timezone

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="k8s-ops-agent-"), "t.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    import fastapi  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — no app deps installed
    print(f"SKIP: {exc}")
    sys.exit(0)

from web_dashboard.database import Base, Job, K8sCluster, SessionLocal, engine  # noqa: E402
from web_dashboard.services import (config_service, k8s_agent_runner,  # noqa: E402
                                    k8s_executor_service, k8s_runner_service, k8s_service)

Base.metadata.create_all(bind=engine)
AGENT = types.SimpleNamespace(id="agent-owned", name="owned")


def _cluster(db, cid="cl-ops"):
    db.query(K8sCluster).filter(K8sCluster.id == cid).delete()
    db.add(K8sCluster(id=cid, cloud="aws", name="prod-eks", status="registered",
                      source="registered", api_server="https://api.example.com",
                      kubeconfig_ref="ref", executor_agent_id="agent-owned",
                      created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    db.commit()
    return db.query(K8sCluster).filter(K8sCluster.id == cid).first()


class _Scene:
    """An executor (or none), a recording bridge, and a recording worker runner."""
    def __init__(self, *, agent=True):
        self.agent = agent
        self.steps, self.worker = [], []

    def __enter__(self):
        self.saved = (k8s_executor_service.executor_for, k8s_agent_runner.run_steps,
                      k8s_runner_service.run, k8s_runner_service.mode,
                      k8s_service.resolve_kubeconfig, k8s_service._runner_kubeconfig)
        k8s_executor_service.executor_for = lambda db, c: AGENT if self.agent else None

        async def run_steps(db, cluster, steps, *, parent_job_id="", legacy=None,
                            created_by="system"):
            if not self.agent:            # what the real bridge does with no executor
                return await legacy()
            self.steps.append(steps)
            return "\n".join(f"--- step {i}: {s['kind']}" for i, s in enumerate(steps, 1))

        async def worker(*, kubeconfig, command, target_cloud="", stdin_text=None, job_id=""):
            self.worker.append({"command": command, "stdin": stdin_text})
            return "ENTRA_BOUND_OK" if "create clusterrolebinding" in command else "ok"
        k8s_agent_runner.run_steps = run_steps
        k8s_runner_service.run = worker
        k8s_runner_service.mode = lambda cloud="": "ecs"
        k8s_service.resolve_kubeconfig = lambda db, cid: "kubeconfig"
        k8s_service._runner_kubeconfig = lambda kc: kc
        return self

    def __exit__(self, *exc):
        (k8s_executor_service.executor_for, k8s_agent_runner.run_steps,
         k8s_runner_service.run, k8s_runner_service.mode,
         k8s_service.resolve_kubeconfig, k8s_service._runner_kubeconfig) = self.saved

    def kinds(self):
        return [[s["kind"] for s in steps] for steps in self.steps]


def _run(coro):
    return asyncio.run(coro)


# ── the helm step ─────────────────────────────────────────────────────────────

def test_the_eso_install_becomes_one_helm_step_with_its_repo_by_url():
    step = k8s_service._helm_step(
        ["upgrade", "--install", "external-secrets", "external-secrets/external-secrets",
         "--namespace", "external-secrets", "--create-namespace", "--wait",
         "--set", "installCRDs=true", "--version", "0.10.0"], None, add_eso_repo=True)
    assert step == {"kind": "helm_upgrade", "release": "external-secrets",
                    "chart": "external-secrets", "repo": "https://charts.external-secrets.io",
                    "namespace": "external-secrets", "create_namespace": True, "wait": True,
                    "set": ["installCRDs=true"], "version": "0.10.0"}
    assert k8s_service._helm_step(["uninstall", "eso", "-n", "ns"], None, False) == {
        "kind": "helm_uninstall", "release": "eso", "namespace": "ns"}
    values = k8s_service._helm_step(["upgrade", "--install", "a", "a", "--repo",
                                     "https://charts.example.com", "-n", "x", "-f", "-"],
                                    "token: t", False)
    assert values["values"] == "token: t"


def test_anything_the_agent_cannot_carry_stays_on_the_worker():
    base = ["upgrade", "--install", "r", "c", "-n", "ns"]
    for args, values in ((base + ["--post-renderer", "/bin/sh"], None),
                         (base + ["--set", "--kubeconfig=/x"], None),
                         (base + ["-f", "-"], None),
                         (["upgrade", "--install", "r", "other/chart", "-n", "ns"], None),
                         (["upgrade", "--install", "r", "c"], None),
                         (["rollback", "r"], None)):
        try:
            k8s_service._helm_step(args, values, add_eso_repo=True)
        except ValueError:
            continue
        raise AssertionError(f"{args} became a step")


# ── the wrappers ──────────────────────────────────────────────────────────────

def test_with_an_executor_the_wrappers_send_steps():
    db = SessionLocal()
    row = _cluster(db)
    with _Scene() as scene:
        _run(k8s_service._apply_manifest_via_runner("kc", "kind: Namespace", "aws",
                                                    db=db, cluster=row))
        _run(k8s_service._delete_manifest_via_runner("kc", "kind: Secret", "aws",
                                                     db=db, cluster=row))
        _run(k8s_service._helm_via_runner("kc", ["uninstall", "x", "-n", "y"], False,
                                          target_cloud="aws", db=db, cluster=row))
    assert scene.kinds() == [["apply"], ["delete"], ["helm_uninstall"]]
    assert scene.worker == []


def test_without_one_they_run_the_worker_command_unchanged():
    db = SessionLocal()
    row = _cluster(db)
    with _Scene(agent=False) as scene:
        _run(k8s_service._apply_manifest_via_runner("kc", "kind: Namespace", "aws",
                                                    db=db, cluster=row))
        _run(k8s_service._helm_via_runner("kc", ["uninstall", "x", "-n", "y"], False,
                                          target_cloud="aws", db=db, cluster=row))
    assert scene.steps == []
    assert [w["command"] for w in scene.worker] == ["kubectl apply -f -",
                                                    "helm uninstall x -n y"]


def test_a_url_manifest_and_an_uncarriable_helm_call_stay_on_the_worker():
    db = SessionLocal()
    row = _cluster(db)
    with _Scene() as scene:
        _run(k8s_service._apply_manifest_via_runner(
            "kc", "https://rancher.example.com/v3/import/abc.yaml", "aws", db=db, cluster=row))
        _run(k8s_service._helm_via_runner(
            "kc", ["upgrade", "--install", "r", "c", "-n", "ns", "--atomic"], False,
            target_cloud="aws", db=db, cluster=row))
    assert scene.steps == [] and len(scene.worker) == 2


# ── the callers ───────────────────────────────────────────────────────────────

def test_eso_install_and_removal_run_on_the_agent():
    db = SessionLocal()
    row = _cluster(db)
    for key, value in (("pscli_client_id", "cid"), ("pscli_client_secret", "csecret"),
                       ("pscli_api_url", "https://ps.example.com")):
        config_service.set(key, value)
    with _Scene() as scene:
        _run(k8s_service.setup_secret_delivery(row.id, "eso"))
        _run(k8s_service.setup_secret_delivery(row.id, "none"))
    assert scene.kinds() == [["helm_upgrade"], ["apply"], ["apply"], ["delete"],
                             ["helm_uninstall"]]
    assert scene.steps[0][0]["repo"] == "https://charts.external-secrets.io"
    assert "csecret" in scene.steps[1][0]["manifest"]
    assert scene.worker == []


def test_the_entitle_install_steps_carry_the_one_shots_sequence():
    helm_args = ["upgrade", "--install", "entitle-agent", "entitle-agent", "--repo",
                 "https://charts.entitle.io", "--namespace", "entitle", "--create-namespace",
                 "--wait", "--set", "kmsType=kubernetes_secret_manager", "-f", "-"]
    steps = k8s_service._entitle_agent_install_steps(
        helm_args, "agent:\n  token: t\n", None, "kind: ClusterRoleBinding",
        "kind: ConfigMap")
    assert [s["kind"] for s in steps] == ["helm_upgrade", "apply", "apply"]
    assert steps[0]["values"] == "agent:\n  token: t\n" and steps[0]["wait"] is True
    assert steps[2]["best_effort"] is True and "best_effort" not in steps[1]
    with_secret = k8s_service._entitle_agent_install_steps(
        helm_args[:-2], None, "kind: Secret", "kind: ClusterRoleBinding", None)
    assert [s["kind"] for s in with_secret] == ["apply", "helm_upgrade", "apply"]
    # An operator --set the agent would refuse keeps the whole install on the worker.
    assert k8s_service._entitle_agent_install_steps(
        helm_args + ["--set", "bad key=x"], "v: 1", None, "crb", None) == []


def test_the_entra_bind_is_a_delete_and_apply_of_the_same_binding():
    db = SessionLocal()
    row = _cluster(db)
    with _Scene() as scene:
        out = _run(k8s_service.bind_entra_group(db, row.id, group_id="grp-1",
                                                role="view"))
    assert out["role"] == "view" and scene.kinds() == [["delete", "apply"]]
    crb = yaml.safe_load(scene.steps[0][1]["manifest"])
    assert crb["kind"] == "ClusterRoleBinding"
    assert crb["metadata"]["name"] == "entra-group-binding"
    assert crb["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io",
                              "kind": "ClusterRole", "name": "view"}
    assert crb["subjects"][0]["kind"] == "Group" and crb["subjects"][0]["name"] == "grp-1"
    with _Scene(agent=False) as scene:
        _run(k8s_service.bind_entra_group(db, row.id, group_id="grp-1", role="view"))
    assert scene.steps == [] and "create clusterrolebinding" in scene.worker[0]["command"]


def test_rbac_bindings_and_the_legacy_delete_run_on_the_agent():
    db = SessionLocal()
    row = _cluster(db)
    with _Scene() as scene:
        _run(k8s_service.apply_impersonator_binding(db, row.id, group_id="grp-1"))
        _run(k8s_service.remove_impersonator_binding(db, row.id))
        _run(k8s_service.apply_ps_rotator_rbac(db, row.id, mode="rotate"))
        _run(k8s_service.delete_legacy_pra_token_secret(db, row.id))
    assert scene.kinds() == [["apply"], ["delete"], ["apply"], ["delete"]]
    secret = yaml.safe_load(scene.steps[3][0]["manifest"])
    assert secret["kind"] == "Secret" and secret["metadata"]["name"].endswith("-token")
    assert scene.worker == []


def test_the_token_mints_cannot_reach_an_agent():
    """They hand a secret BACK to the dashboard, so they take no cluster to route by."""
    for fn in (k8s_service._mint_entitle_sa_token, k8s_service._run_sa_token_oneshot):
        params = inspect.signature(fn).parameters
        assert "cluster" not in params and "db" not in params, fn.__name__


# ── the bridge's own fallback ─────────────────────────────────────────────────

def test_an_oversized_manifest_runs_on_the_worker_before_any_job_exists():
    db = SessionLocal()
    row = _cluster(db)
    db.add(Job(id="parent-ops", job_type="k8s_secret_delivery", status="running",
               created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    db.commit()
    saved = k8s_executor_service.executor_for, k8s_agent_runner._usable_now
    k8s_executor_service.executor_for = lambda db, c: AGENT
    k8s_agent_runner._usable_now = lambda db, agent: True
    called = []

    async def legacy():
        called.append(1)
        return "worker"
    try:
        out = _run(k8s_agent_runner.run_steps(
            db, row, [{"kind": "apply", "manifest": "x" * (300 * 1024)}],
            parent_job_id="parent-ops", legacy=legacy))
    finally:
        k8s_executor_service.executor_for, k8s_agent_runner._usable_now = saved
    assert out == "worker" and called == [1]
    assert db.query(Job).filter(Job.job_type == "agent_k8s").count() == 0
    from web_dashboard.services import job_service
    lines = [line for _, line in job_service.get_job_logs(db, "parent-ops")]
    assert any("over" in line and "remote worker" in line for line in lines), lines


if __name__ == "__main__":
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
