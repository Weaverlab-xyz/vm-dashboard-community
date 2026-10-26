"""Kubernetes work on dashboard-owned agents (phase 3 of
docs/design/remote-worker-and-agent.md), dashboard side.

  * **Only an owned agent qualifies.** Deployed by this dashboard (an agent_hosts row),
    SPIRE-attested, Kubernetes work allowed on its host, agent 2.12+, granted
    `agent_k8s`, in the cluster's cloud. Every rule is read from the dashboard's rows.
  * **Checked at every use.** An executor that stops qualifying sends the work back to the
    remote worker; `lease_one` never hands `agent_k8s` to an agent that does not qualify.
  * **Secrets stay sealed.** Steps are stored encrypted and wiped when the job ends; the
    envelope carries the endpoint and step kinds only; the kubeconfig is minted at lease.
  * **Plays** against a cluster run on its executor, and fall back when it is offline.

Runs under pytest, or standalone:  python tests/test_k8s_owned_agents.py
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="k8s-owned-"), "t.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from fastapi import HTTPException
except ModuleNotFoundError as exc:  # pragma: no cover — no app deps installed
    print(f"SKIP: {exc}")
    sys.exit(0)

from web_dashboard.api import config_mgmt as cm  # noqa: E402
from web_dashboard.database import (AgentHost, Base, Job, JobLog, K8sCluster,  # noqa: E402
                                    RemoteAgent, SessionLocal, engine)
from web_dashboard.services import (agent_host_service, agent_k8s_meta,  # noqa: E402
                                    agent_sealing, agent_service, inventory_service,
                                    job_service, k8s_agent_runner, k8s_executor_service as kex,
                                    k8s_service)
from web_dashboard.services.k8s_runner_service import K8sRunnerError  # noqa: E402

Base.metadata.create_all(bind=engine)

API = "https://ABC123.gr7.us-east-2.eks.amazonaws.com"
KUBECONFIG = ("apiVersion: v1\nusers:\n- name: u\n  user:\n    token: eks-token-abcdefgh\n"
              "clusters:\n- name: c\n  cluster:\n    server: " + API + "\n")
DIGEST_IMG = "chrweav/ansible-cloud@sha256:" + "a" * 64


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _admin():
    return types.SimpleNamespace(username="tester", is_effective_admin=True,
                                 workgroups_list=[], effective_permissions_dict={})


def _fresh():
    db = SessionLocal()
    for model in (Job, JobLog, K8sCluster, AgentHost, RemoteAgent):
        db.query(model).delete()
    db.commit()
    return db


def _agent(db, name="owned", *, deployed=True, spiffe=True, k8s=True, version="2.12.0",
           cloud="aws", online=True, grants=None):
    db.add(RemoteAgent(
        id=f"agent-{name}", name=name, is_active=True, agent_version=version,
        public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + name,
        auth_mode="spiffe" if spiffe else None,
        last_seen_at=_utcnow() if online else _utcnow() - timedelta(hours=2),
        allowed_job_types=json.dumps(grants) if grants is not None else None,
        created_at=_utcnow()))
    if deployed:
        db.add(AgentHost(agent_id=f"agent-{name}", name=name, cloud=cloud, region="us-east-2",
                         ansible_cidrs="[]", max_concurrent=1, k8s_enabled=k8s or None,
                         status="running"))
    db.commit()
    return db.query(RemoteAgent).filter(RemoteAgent.id == f"agent-{name}").first()


def _cluster(db, *, cloud="aws", executor="agent-owned", workgroup=None):
    db.add(K8sCluster(id="cl-1", cloud=cloud, name="prod-eks", status="registered",
                      source="registered", api_server=API, kubeconfig_ref="ref",
                      executor_agent_id=executor, workgroup=workgroup, created_by="tester",
                      created_at=_utcnow()))
    db.commit()
    return db.query(K8sCluster).filter(K8sCluster.id == "cl-1").first()


class _Kubeconfig:
    """Stand in for the stored kubeconfig and the token mint."""
    def __enter__(self):
        self.saved = k8s_service.resolve_kubeconfig, k8s_service._runner_kubeconfig
        k8s_service.resolve_kubeconfig = lambda db, cid: KUBECONFIG
        k8s_service._runner_kubeconfig = lambda kc: kc
        return self

    def __exit__(self, *exc):
        k8s_service.resolve_kubeconfig, k8s_service._runner_kubeconfig = self.saved


# ── the trust rules ───────────────────────────────────────────────────────────

def test_an_owned_spire_agent_with_kubernetes_work_qualifies():
    db = _fresh()
    agent = _agent(db)
    cluster = _cluster(db)
    assert kex.agent_refusal(db, agent, cluster) == ""
    assert kex.executor_for(db, cluster).id == agent.id


def test_every_rule_refuses_by_name():
    cases = [({"deployed": False}, "not deployed by this dashboard"),
             ({"spiffe": False}, "not SPIRE-attested"),
             ({"k8s": False}, "Kubernetes work is not allowed"),
             ({"version": "2.11.0"}, "needs agent 2.12"),
             ({"grants": ["agent_ansible"]}, "not granted Kubernetes work"),
             ({"cloud": "gcp"}, "runs in gcp")]
    for kw, words in cases:
        db = _fresh()
        agent = _agent(db, **kw)
        cluster = _cluster(db)
        why = kex.agent_refusal(db, agent, cluster)
        assert words in why, (kw, why)
        assert kex.executor_for(db, cluster) is None, kw
        try:
            kex.set_executor(db, cluster, agent.id)
        except kex.ExecutorError as exc:
            assert words in str(exc)
        else:
            raise AssertionError(f"{kw} was accepted as an executor")


def test_an_on_premises_cluster_keeps_the_dashboards_runner():
    db = _fresh()
    agent = _agent(db)
    cluster = _cluster(db, cloud="local", executor=None)
    assert "on-premises" in kex.agent_refusal(db, agent, cluster)


def test_a_revoked_executor_sends_the_work_back_to_the_worker():
    db = _fresh()
    agent = _agent(db)
    cluster = _cluster(db)
    agent.is_active = False
    db.commit()
    assert kex.executor_for(db, cluster) is None


def test_the_picker_lists_the_clouds_deployed_agents_with_reasons():
    db = _fresh()
    _agent(db, "ok")
    _agent(db, "no-spire", spiffe=False)
    _agent(db, "elsewhere", cloud="azure")
    _agent(db, "byo", deployed=False)
    cluster = _cluster(db, executor=None)
    rows = {r["name"]: r["reason"] for r in kex.candidates(db, cluster)}
    assert set(rows) == {"ok", "no-spire"}
    assert rows["ok"] == "" and "SPIRE" in rows["no-spire"]


def test_deleting_the_agent_clears_the_clusters_choice():
    db = _fresh()
    agent = _agent(db)
    _cluster(db)
    agent.is_active = False
    db.query(AgentHost).delete()
    db.commit()
    agent_service.delete_agent(db, agent)
    db.expire_all()
    assert db.query(K8sCluster).first().executor_agent_id is None


# ── the lease ─────────────────────────────────────────────────────────────────

def _queue(db, agent, cluster, steps=None):
    steps = steps or [{"kind": "apply", "manifest": "kind: Secret\nstringData: {p: s3cret}"}]
    meta = agent_k8s_meta.run_meta(cluster=cluster, steps=steps)
    return job_service.create_job(db, job_type="agent_k8s", created_by="t", metadata=meta,
                                  agent_id=agent.id)


def test_only_a_qualifying_agent_can_lease_kubernetes_work():
    db = _fresh()
    agent = _agent(db)
    cluster = _cluster(db)
    _queue(db, agent, cluster)
    host = db.query(AgentHost).first()
    host.k8s_enabled = None
    db.commit()
    assert agent_service.lease_one(db, agent) is None
    host.k8s_enabled = True
    db.commit()
    leased = agent_service.lease_one(db, agent)
    assert leased and leased["job_type"] == "agent_k8s"


def test_the_row_holds_the_steps_encrypted_and_the_envelope_holds_no_secret():
    db = _fresh()
    agent = _agent(db)
    cluster = _cluster(db)
    job = _queue(db, agent, cluster)
    raw = job.extra_data
    assert "s3cret" not in raw and "steps_enc" in raw
    env = agent_k8s_meta.envelope_payload(job.metadata_dict)
    assert env == {"cluster_host": "abc123.gr7.us-east-2.eks.amazonaws.com",
                   "cluster_port": 443, "step_kinds": ["apply"]}
    assert "s3cret" not in json.dumps(env)


def test_the_bundle_is_minted_at_lease_for_the_executor_only():
    db = _fresh()
    agent = _agent(db)
    other = _agent(db, "other")
    cluster = _cluster(db)
    job = _queue(db, agent, cluster)
    with _Kubeconfig():
        bundle, scrub = k8s_agent_runner.build_bundle(db, job=job, agent=agent)
        assert bundle["kubeconfig"] == KUBECONFIG
        assert bundle["steps"][0]["kind"] == "apply"
        assert "eks-token-abcdefgh" in scrub
        assert any("s3cret" in s for s in scrub)
        try:
            k8s_agent_runner.build_bundle(db, job=job, agent=other)
        except agent_k8s_meta.K8sMetaError:
            pass
        else:
            raise AssertionError("another agent was handed the kubeconfig")
        agent_k8s_meta.wipe_steps(db, job)
        assert "steps_enc" not in job.metadata_dict
        try:
            k8s_agent_runner.build_bundle(db, job=job, agent=agent)
        except agent_k8s_meta.K8sMetaError as exc:
            assert "already ended" in str(exc)
        else:
            raise AssertionError("an ended job's steps were released")


def test_the_seal_ref_names_the_cluster_endpoint():
    assert agent_sealing.k8s_ref(host="API.Example.COM.", port="443") == \
        "k8s:api.example.com:443"


# ── the bridge ────────────────────────────────────────────────────────────────

def _bridge(db, cluster, *, finish=None, legacy=None):
    """Run run_steps; ``finish(job)`` plays the agent once the job exists."""
    saved = k8s_agent_runner.POLL_S, k8s_agent_runner.WAIT_S
    k8s_agent_runner.POLL_S = 0.01

    async def go():
        task = asyncio.ensure_future(k8s_agent_runner.run_steps(
            db, cluster, [{"kind": "apply", "manifest": "a: 1"}],
            parent_job_id="parent", legacy=legacy))
        for _ in range(200):
            await asyncio.sleep(0.005)
            job = db.query(Job).filter(Job.job_type == "agent_k8s").first()
            if job is not None and finish is not None:
                finish(job)
                break
            if task.done():
                break
        return await task
    try:
        return asyncio.run(go())
    finally:
        k8s_agent_runner.POLL_S, k8s_agent_runner.WAIT_S = saved


def _parent(db):
    db.add(Job(id="parent", job_type="k8s_secret_delivery", status="running",
               created_at=_utcnow()))
    db.commit()


def test_the_bridge_waits_for_the_agent_and_copies_its_output():
    db = _fresh()
    _agent(db)
    cluster = _cluster(db)
    _parent(db)

    def finish(job):
        job_service.append_job_logs(db, job.id, ["--- step 1: apply", "secret/x created"])
        job.status = "completed"
        db.commit()
    out = _bridge(db, cluster, finish=finish)
    assert "secret/x created" in out
    lines = [line for _, line in job_service.get_job_logs(db, "parent")]
    assert any("on agent 'owned'" in line for line in lines)
    assert "secret/x created" in lines
    child = db.query(Job).filter(Job.job_type == "agent_k8s").first()
    assert "steps_enc" not in child.metadata_dict


def test_a_failed_step_raises_the_runner_error_with_its_output():
    db = _fresh()
    _agent(db)
    cluster = _cluster(db)
    _parent(db)

    def finish(job):
        job_service.append_job_logs(db, job.id, ["error: forbidden"])
        job.status, job.error_message = "failed", "a Kubernetes step exited 1."
        db.commit()
    try:
        _bridge(db, cluster, finish=finish)
    except K8sRunnerError as exc:
        assert "forbidden" in exc.log_output and "exited 1" in str(exc)
    else:
        raise AssertionError("a failed step returned")


def test_no_executor_or_an_offline_one_runs_on_the_worker():
    called = []

    async def legacy():
        called.append(1)
        return "worker output"
    db = _fresh()
    _agent(db)
    cluster = _cluster(db, executor=None)
    _parent(db)
    assert _bridge(db, cluster, legacy=legacy) == "worker output"
    db = _fresh()
    _agent(db, online=False)
    cluster = _cluster(db)
    _parent(db)
    saved = agent_host_service.booking_refusal
    agent_host_service.booking_refusal = lambda db, agent, at: "parked"
    try:
        assert _bridge(db, cluster, legacy=legacy) == "worker output"
    finally:
        agent_host_service.booking_refusal = saved
    assert called == [1, 1]
    lines = [line for _, line in job_service.get_job_logs(db, "parent")]
    assert any("not online" in line for line in lines)
    assert db.query(Job).filter(Job.job_type == "agent_k8s").count() == 0


def test_an_agent_that_never_finishes_is_cancelled():
    db = _fresh()
    _agent(db)
    cluster = _cluster(db)
    _parent(db)
    saved = k8s_agent_runner.WAIT_S
    k8s_agent_runner.WAIT_S = 0
    try:
        _bridge(db, cluster)
    except K8sRunnerError as exc:
        assert "cancelled" in str(exc)
    else:
        raise AssertionError("a stuck job returned")
    finally:
        k8s_agent_runner.WAIT_S = saved
    child = db.query(Job).filter(Job.job_type == "agent_k8s").first()
    assert child.status == "cancelled" and "steps_enc" not in child.metadata_dict


# ── the deployed host ─────────────────────────────────────────────────────────

def test_a_host_allowed_kubernetes_work_gets_the_block_by_digest():
    import yaml
    doc = yaml.safe_load(agent_host_service.policy_yaml(["10.40.0.0/16"], 1,
                                                        k8s_image=DIGEST_IMG))
    assert "agent_k8s" in doc["job_types"]
    assert doc["k8s"]["image"] == DIGEST_IMG and doc["k8s"]["enabled"] is True
    assert {t["cidr"] for t in doc["k8s"]["targets"]} == {"0.0.0.0/0", "::/0"}
    plain = yaml.safe_load(agent_host_service.policy_yaml(["10.40.0.0/16"], 1))
    assert "k8s" not in plain and plain["job_types"] == ["agent_ansible"]
    try:
        agent_host_service.policy_yaml(["10.40.0.0/16"], 1, k8s_image="x/y:latest")
    except agent_host_service.AgentHostError:
        pass
    else:
        raise AssertionError("a tag was accepted for the Kubernetes image")


def test_the_image_is_resolved_to_a_digest_or_not_used():
    from web_dashboard.services import agent_release_service
    saved = agent_release_service.read_registry
    try:
        agent_release_service.read_registry = lambda repo, tag, session=None: {
            "digest": "sha256:" + "b" * 64, "version": ""}
        assert agent_host_service.k8s_image() == "chrweav/ansible-cloud@sha256:" + "b" * 64

        def boom(*a, **k):
            raise RuntimeError("no route to Docker Hub")
        agent_release_service.read_registry = boom
        assert agent_host_service.k8s_image() == ""
    finally:
        agent_release_service.read_registry = saved


def test_the_bootstrap_pre_pulls_the_pinned_image():
    script = agent_host_service.bootstrap_script(
        url="https://dash.example.com", policy="job_types: []\n",
        material={"mode": "code", "code": "C"}, k8s_image=DIGEST_IMG)
    assert f"docker pull {DIGEST_IMG}" in script


def test_the_migration_is_recorded():
    from web_dashboard import schema_migrations as sm
    mig = [m for m in sm.MIGRATIONS if m.name == "0009_k8s_on_owned_agents"]
    assert len(mig) == 1 and len(mig[0].steps) == 2


# ── plays ─────────────────────────────────────────────────────────────────────

def _request(**over):
    base = dict(asset="site.yml", asset_backend="s3", agent_id="agent-owned",
                target_kind="k8s", target_id="cl-1")
    base.update(over)
    return cm.RunRequest(**base)


def test_a_play_runs_on_the_clusters_executor_against_its_api():
    db = _fresh()
    _agent(db)
    _cluster(db)
    out = cm._resolve_agent_target(_request(), db, _admin())
    assert out["run_kind"] == "k8s" and out["transport"] == "local"
    assert out["target_host"] == "abc123.gr7.us-east-2.eks.amazonaws.com"
    assert out["target_port"] == 443


def test_a_play_on_another_agent_or_a_hidden_cluster_is_refused():
    db = _fresh()
    _agent(db)
    _agent(db, "other")
    _cluster(db)
    for payload, user, code in ((_request(agent_id="agent-other"), _admin(), 400),
                                (_request(), types.SimpleNamespace(
                                    username="x", is_effective_admin=False,
                                    workgroups_list=["team-b"]), 404)):
        try:
            cm._resolve_agent_target(payload, db, user)
        except HTTPException as exc:
            assert exc.status_code == code, (exc.status_code, exc.detail)
        else:
            raise AssertionError("accepted")


def test_the_bundle_of_a_play_carries_the_minted_kubeconfig():
    from web_dashboard.services import agent_ansible_bundle, agent_ansible_meta, storage_service
    db = _fresh()
    agent = _agent(db)
    _cluster(db)
    meta = agent_ansible_meta.normalize(dict(
        run_kind="k8s", transport="local", target_host="ABC123.gr7.us-east-2.eks.amazonaws.com",
        target_port=443, target_id="cl-1", target_label="k8s/prod-eks", asset="site.yml",
        asset_backend="s3"))
    assert agent_ansible_meta.check(meta) == ""
    job = Job(id="run-1", job_type="agent_ansible", status="running", agent_id=agent.id,
              extra_data=json.dumps(meta), created_at=_utcnow())
    db.add(job)
    db.commit()

    async def _asset(backend, name):
        return b"- hosts: localhost\n  tasks: []\n"
    saved = storage_service.fetch_asset_in
    storage_service.fetch_asset_in = _asset
    try:
        with _Kubeconfig():
            bundle, scrub = asyncio.run(agent_ansible_bundle.build(db, job=job, agent=agent))
    finally:
        storage_service.fetch_asset_in = saved
    assert bundle["kubeconfig"] == KUBECONFIG and "eks-token-abcdefgh" in scrub


def test_a_play_falls_back_to_the_worker_while_its_agent_is_offline():
    db = _fresh()
    _agent(db, online=False)
    _cluster(db)
    saved = agent_host_service.booking_refusal
    agent_host_service.booking_refusal = lambda db, agent, at: "parked"
    try:
        fb = cm._worker_fallback(_request(), db, _admin())
    finally:
        agent_host_service.booking_refusal = saved
    assert fb == {"target": "", "agent_name": "owned", "cidr": ""}


def test_the_pickers_and_the_inventory_name_the_executor():
    db = _fresh()
    _agent(db)
    _cluster(db)
    assert cm.get_localhost_targets(_admin(), db)["k8s"] == []
    rows = cm.get_agent_targets(_admin(), db)["clusters"]
    assert [r["target_id"] for r in rows] == ["cl-1"] and rows[0]["agent_name"] == "owned"
    item = next(i for i in inventory_service.collect(db) if i["id"] == "k8s:cl-1")
    assert inventory_service._target_spec(item)["agent_id"] == "agent-owned"


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
