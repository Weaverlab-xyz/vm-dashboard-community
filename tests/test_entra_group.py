"""Unit tests for the Entra-group → cluster-RBAC bind commands (real-identity JIT).

Binding an Entra (AAD) group to a ClusterRole lets its members authenticate as
themselves and be authorized by their AAD token's group claim — the real-identity
alternative to the k8s (agent) connector's synthetic `entitle:` impersonation
subject. These lock in the kubectl commands `bind_entra_group`/`unbind_entra_group`
run via the cloud runner.

Stubs the DB / sqlalchemy imports so k8s_service loads without an app/DB (same as
test_entitle_agent_rbac / test_pra_api_tunnel). Runs under pytest or standalone.
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)
sys.modules.setdefault("sqlalchemy", types.ModuleType("sqlalchemy"))
_orm = types.ModuleType("sqlalchemy.orm")
_orm.Session = object
sys.modules.setdefault("sqlalchemy.orm", _orm)
_db = types.ModuleType("web_dashboard.database")
_db.Job = type("Job", (), {})
_db.K8sCluster = type("K8sCluster", (), {})
sys.modules.setdefault("web_dashboard.database", _db)

from web_dashboard.services import k8s_service as k  # noqa: E402


def test_bind_command_creates_group_clusterrolebinding():
    cmd = k._entra_group_bind_command("cluster-admin", "1051c7ab-6284-4865-979f-55f55766e437")
    assert "kubectl create clusterrolebinding entra-group-binding" in cmd
    assert "--clusterrole=cluster-admin" in cmd
    # Subject is a GROUP = the Entra Object ID (kubectl create ... --group=).
    assert "--group=1051c7ab-6284-4865-979f-55f55766e437" in cmd
    # Delete-then-create so a role change applies (roleRef is immutable under apply).
    assert "kubectl delete clusterrolebinding entra-group-binding --ignore-not-found" in cmd
    assert cmd.index("delete clusterrolebinding") < cmd.index("create clusterrolebinding")
    assert "ENTRA_BOUND_OK" in cmd


def test_bind_command_shell_quotes_untrusted_values():
    cmd = k._entra_group_bind_command("my custom role", "gid")
    assert "--clusterrole='my custom role'" in cmd   # shlex-quoted, no injection


def test_unbind_command_deletes_binding_idempotently():
    cmd = k._entra_group_unbind_command()
    assert "kubectl delete clusterrolebinding entra-group-binding --ignore-not-found" in cmd
    assert "ENTRA_UNBOUND_OK" in cmd


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
    sys.exit(1 if failures else 0)
