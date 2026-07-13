"""Unit test for the Entitle in-cluster agent's cluster RBAC grant.

In In-Cluster (agent-brokered) mode Entitle drives the agent ServiceAccount to
enumerate the cluster and to create/delete (Cluster)RoleBindings for JIT grants.
The agent Helm chart only grants a namespace-scoped Role for self-management, so
``setup_entitle_agent`` must additionally bind the agent SA to cluster-admin —
otherwise the integration reports "Failed to fetch the resources of <cluster>".
This locks in that ``_entitle_agent_clusterrolebinding_manifest`` emits a valid
ClusterRoleBinding → cluster-admin for the agent ServiceAccount.

Stubs the DB / sqlalchemy imports so k8s_service loads without an app/DB (same
lightweight approach as test_pra_k8s_vault). Runs under pytest or standalone:
    python tests/test_entitle_agent_rbac.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Stub the heavy module-load deps so k8s_service imports without a real DB engine.
_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)

sys.modules.setdefault("sqlalchemy", types.ModuleType("sqlalchemy"))
_orm_stub = types.ModuleType("sqlalchemy.orm")
_orm_stub.Session = object
sys.modules.setdefault("sqlalchemy.orm", _orm_stub)

_db_stub = types.ModuleType("web_dashboard.database")
_db_stub.Job = type("Job", (), {})
_db_stub.K8sCluster = type("K8sCluster", (), {})
sys.modules.setdefault("web_dashboard.database", _db_stub)

import yaml  # noqa: E402

from web_dashboard.services import k8s_service as k  # noqa: E402


def test_agent_clusterrolebinding_binds_sa_to_cluster_admin():
    manifest = k._entitle_agent_clusterrolebinding_manifest("entitle", "entitle-agent-sa")
    docs = [d for d in yaml.safe_load_all(manifest) if d is not None]
    assert len(docs) == 1, "expected exactly one object (the ClusterRoleBinding)"
    crb = docs[0]
    assert crb["kind"] == "ClusterRoleBinding"
    assert crb["metadata"]["name"] == "entitle-agent-cluster-admin"
    # cluster-admin is required by Entitle's k8s integration (resource sync + JIT
    # (Cluster)RoleBinding management), matching the External SA path.
    assert crb["roleRef"]["kind"] == "ClusterRole"
    assert crb["roleRef"]["name"] == "cluster-admin"
    subj = crb["subjects"]
    assert subj == [{"kind": "ServiceAccount", "name": "entitle-agent-sa", "namespace": "entitle"}]


def test_agent_clusterrolebinding_honors_configured_sa_and_namespace():
    manifest = k._entitle_agent_clusterrolebinding_manifest("ent-ns", "custom-agent")
    crb = next(d for d in yaml.safe_load_all(manifest) if d)
    assert crb["subjects"][0]["name"] == "custom-agent"
    assert crb["subjects"][0]["namespace"] == "ent-ns"
    # The binding name is fixed (idempotent apply / clean teardown by name).
    assert crb["metadata"]["name"] == "entitle-agent-cluster-admin"


if __name__ == "__main__":
    fns = [v for k_, v in sorted(globals().items()) if k_.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
