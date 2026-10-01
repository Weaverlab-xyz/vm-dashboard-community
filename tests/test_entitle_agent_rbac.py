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


def _oneshot(**kw):
    crb = k._entitle_agent_clusterrolebinding_manifest("entitle", "entitle-agent-sa")
    masq = k._gke_ip_masq_configmap(["10.99.1.0/24"])
    args = dict(secret_manifest_from_stdin=False, manifests=[crb], best_effort_manifests=[masq])
    args.update(kw)
    return k._entitle_agent_install_oneshot_command(
        ["upgrade", "--install", "entitle-agent", "entitle-agent", "--set", "a=b c", "-f", "-"], **args)


def test_install_oneshot_is_one_group_in_install_order():
    """The runner pipes stdin into `<command>`, and a pipe binds only to the FIRST
    command of an && list — so the command must be one { …; } group, or helm's
    `-f -` (or the Secret apply) would read nothing."""
    cmd = _oneshot()
    assert cmd.startswith("{ ") and cmd.endswith("; }")
    assert cmd.index("helm upgrade") < cmd.index("ClusterRoleBinding") < cmd.index("ip-masq-agent")
    assert "'a=b c'" in cmd, "helm args must be shell-quoted"
    assert k._ENTITLE_AGENT_BEST_EFFORT_FAILED in cmd


def test_install_oneshot_secret_apply_reads_stdin_before_helm():
    cmd = _oneshot(secret_manifest_from_stdin=True, best_effort_manifests=[])
    assert cmd.startswith("{ kubectl apply -f - 1>&2 && helm ")
    assert k._ENTITLE_AGENT_BEST_EFFORT_FAILED not in cmd


def _run_oneshot_with_stubs(cmd, stdin_text, env_extra):
    """Run ``cmd`` the way the cloud runners do (set -e; decoded stdin piped into it)
    against stub helm/kubectl that log what they received."""
    import base64
    import shutil
    import subprocess
    import tempfile
    tmp = tempfile.mkdtemp()
    try:
        bindir = os.path.join(tmp, "bin")
        os.mkdir(bindir)
        logf = os.path.join(tmp, "log")
        stubs = {
            "helm": '#!/bin/sh\ncase " $* " in *" -f - "*) echo "helm stdin=$(cat)" >> "$LOG";; '
                    '*) echo "helm" >> "$LOG";; esac\n[ "${FAIL_HELM:-}" = 1 ] && exit 1\nexit 0\n',
            "kubectl": '#!/bin/sh\nin=$(cat)\necho "kubectl $1 $(printf %s "$in" | grep -m1 "^kind:")" >> "$LOG"\n'
                       'case "$in" in *ConfigMap*) [ "${FAIL_MASQ:-}" = 1 ] && exit 1;; esac\nexit 0\n',
        }
        for name, body in stubs.items():
            p = os.path.join(bindir, name)
            with open(p, "w", newline="\n") as fh:
                fh.write(body)
            os.chmod(p, 0o755)
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""), LOG=logf,
                   STDIN_B64=base64.b64encode(stdin_text.encode()).decode(), **env_extra)
        full = 'set -e; printf %s "$STDIN_B64" | base64 -d | ' + cmd
        proc = subprocess.run(["sh", "-c", full], env=env, capture_output=True, text=True)
        log = open(logf).read() if os.path.exists(logf) else ""
        return proc.returncode, proc.stderr, log
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _have_posix_sh():
    import shutil
    return os.name == "posix" and shutil.which("sh") and shutil.which("base64")


def test_install_oneshot_executes_under_sh():
    if not _have_posix_sh():
        print("skip (no POSIX sh)")
        return
    rc, _, log = _run_oneshot_with_stubs(_oneshot(), "agent: {token: T}", {})
    assert rc == 0
    assert log.splitlines() == ["helm stdin=agent: {token: T}",
                                "kubectl apply kind: ClusterRoleBinding", "kubectl apply kind: ConfigMap"]
    # A failed best-effort apply flags itself but does not fail the install.
    rc, err, _ = _run_oneshot_with_stubs(_oneshot(), "v", {"FAIL_MASQ": "1"})
    assert rc == 0 and k._ENTITLE_AGENT_BEST_EFFORT_FAILED in err
    # A failed helm stops before the RBAC apply and fails the task.
    rc, _, log = _run_oneshot_with_stubs(_oneshot(), "v", {"FAIL_HELM": "1"})
    assert rc != 0 and "kubectl" not in log
    # Existing-Secret path: the Secret apply consumes stdin, so helm reads nothing.
    rc, _, log = _run_oneshot_with_stubs(
        _oneshot(secret_manifest_from_stdin=True, best_effort_manifests=[]), "kind: Secret\n", {})
    assert rc == 0
    assert log.splitlines() == ["kubectl apply kind: Secret", "helm stdin=",
                                "kubectl apply kind: ClusterRoleBinding"]


def test_remove_oneshot_deletes_crb_then_uninstalls_then_deletes_secret():
    crb = k._entitle_agent_clusterrolebinding_manifest("entitle", "entitle-agent-sa")
    sec = k._entitle_agent_secret_manifest("entitle", "entitle-agent-token", "x")
    cmd = k._entitle_agent_remove_oneshot_command(
        ["uninstall", "entitle-agent", "-n", "entitle"], [crb], [sec])
    assert cmd.startswith("{ ") and cmd.endswith("; }")
    assert cmd.count("kubectl delete --ignore-not-found -f -") == 2
    assert cmd.index("ClusterRoleBinding") < cmd.index("helm uninstall") < cmd.index("kind: Secret")
    if not _have_posix_sh():
        print("skip (no POSIX sh)")
        return
    rc, _, log = _run_oneshot_with_stubs(cmd, "", {})
    assert rc == 0
    assert log.splitlines() == ["kubectl delete kind: ClusterRoleBinding", "helm",
                                "kubectl delete kind: Namespace"]  # Namespace + Secret doc
    # A failed uninstall stops before the Secret delete (same as the separate calls).
    rc, _, log = _run_oneshot_with_stubs(cmd, "", {"FAIL_HELM": "1"})
    assert rc != 0 and log.splitlines() == ["kubectl delete kind: ClusterRoleBinding", "helm"]


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
