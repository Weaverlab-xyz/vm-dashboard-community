"""Unit tests for Entra OIDC federation (EKS leg — Phase 1).

Federation makes a cluster TRUST Entra as an OIDC identity provider so a user's own
Entra token authenticates and its group Object IDs match the RBAC `Group` binding
(bind_entra_group) — the real-identity JIT story extended from AKS to EKS. These lock
in the pure pieces: the shared-app settings resolver (_entra_oidc_settings), and the
token-free `kubectl oidc-login` kubeconfig transform (_entra_oidc_login_kubeconfig /
build_entra_oidc_kubeconfig).

Stubs the DB / sqlalchemy imports so k8s_service loads without an app/DB (same as
test_entra_group / test_pra_api_tunnel). Runs under pytest or standalone.
"""
import os
import sys
import types

import yaml

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
_db.K8sCluster = type("K8sCluster", (), {"id": None})
sys.modules.setdefault("web_dashboard.database", _db)

from web_dashboard.services import k8s_service as k  # noqa: E402


# A repointed EKS kubeconfig (server already at the tunnel port; cloud-native `aws`
# exec auth), matching what _repoint_kubeconfig_to_tunnel produces before the swap.
_EKS_REPOINTED = """
apiVersion: v1
kind: Config
current-context: k8s-demo
clusters:
- name: k8s-demo
  cluster:
    server: https://127.0.0.1:6443
    tls-server-name: ABCD.gr7.us-east-2.eks.amazonaws.com
    certificate-authority-data: TEST_CA_B64
contexts:
- name: k8s-demo
  context: {cluster: k8s-demo, user: k8s-demo}
users:
- name: k8s-demo
  user:
    exec:
      apiVersion: client.authentication.k8s.io/v1beta1
      command: aws
      args: [eks, get-token, --cluster-name, k8s-demo, --region, us-east-2]
"""

_OIDC = {"issuer": "https://login.microsoftonline.com/TENANT/v2.0",
         "client_id": "CLIENT-GUID", "username_claim": "oid", "groups_claim": "groups"}


def _with_cfg(mapping):
    """Swap k._cfg for a dict-backed stub; returns a restore callable."""
    orig = k._cfg
    k._cfg = lambda key, default="": mapping.get(key, default)
    return lambda: setattr(k, "_cfg", orig)


def test_oidc_login_kubeconfig_swaps_exec_and_carries_no_token():
    out = k._entra_oidc_login_kubeconfig(_EKS_REPOINTED, _OIDC)
    cfg = yaml.safe_load(out)
    exec_blk = cfg["users"][0]["user"]["exec"]
    assert exec_blk["command"] == "kubectl"
    assert exec_blk["args"][0:2] == ["oidc-login", "get-token"]
    assert "--oidc-issuer-url=https://login.microsoftonline.com/TENANT/v2.0" in exec_blk["args"]
    assert "--oidc-client-id=CLIENT-GUID" in exec_blk["args"]
    # The cloud-native `aws eks get-token` auth is gone (now the user's Entra identity).
    assert "aws" not in exec_blk["args"] and "eks" not in exec_blk["args"]
    assert exec_blk["command"] != "aws"
    # Token-free: no static credential leaks into the download.
    assert "token" not in cfg["users"][0]["user"]
    assert "client-key-data" not in out and "k8s-aws-v1." not in out


def test_oidc_login_kubeconfig_preserves_ca_and_server():
    out = k._entra_oidc_login_kubeconfig(_EKS_REPOINTED, _OIDC)
    cl = yaml.safe_load(out)["clusters"][0]["cluster"]
    assert cl["certificate-authority-data"] == "TEST_CA_B64"   # CA kept verbatim
    assert cl["server"] == "https://127.0.0.1:6443"            # repoint untouched
    assert cl["tls-server-name"] == "ABCD.gr7.us-east-2.eks.amazonaws.com"


def test_settings_derive_issuer_from_tenant():
    restore = _with_cfg({"entra_oidc_client_id": "CLIENT-GUID", "azure_tenant_id": "T-123"})
    try:
        s = k._entra_oidc_settings()
        assert s["client_id"] == "CLIENT-GUID"
        assert s["issuer"] == "https://login.microsoftonline.com/T-123/v2.0"
        assert s["username_claim"] == "oid" and s["groups_claim"] == "groups"
    finally:
        restore()


def test_settings_explicit_issuer_wins():
    restore = _with_cfg({"entra_oidc_client_id": "C", "azure_tenant_id": "T",
                         "entra_oidc_issuer_url": "https://issuer.example/v2.0"})
    try:
        assert k._entra_oidc_settings()["issuer"] == "https://issuer.example/v2.0"
    finally:
        restore()


def test_settings_raises_without_client_id():
    restore = _with_cfg({"azure_tenant_id": "T"})
    try:
        try:
            k._entra_oidc_settings()
            raise AssertionError("expected K8sError when client_id unset")
        except k.K8sError:
            pass
    finally:
        restore()


def test_settings_raises_without_issuer_or_tenant():
    restore = _with_cfg({"entra_oidc_client_id": "C"})   # no issuer, no tenant
    try:
        try:
            k._entra_oidc_settings()
            raise AssertionError("expected K8sError when no issuer derivable")
        except k.K8sError:
            pass
    finally:
        restore()


def test_eks_name_region_parsed_from_kubeconfig():
    name, region = k._eks_name_region(_EKS_REPOINTED)
    assert name == "k8s-demo" and region == "us-east-2"


def test_eks_name_region_empty_for_non_eks():
    # An AKS-style kubelogin exec has no --cluster-name/--region.
    aks = _EKS_REPOINTED.replace(
        "command: aws\n      args: [eks, get-token, --cluster-name, k8s-demo, --region, us-east-2]",
        "command: kubelogin\n      args: [get-token, --server-id, SRV, --login, spn]")
    assert k._eks_name_region(aks) == ("", "")


# ── GKE (Workforce Identity Federation) ──────────────────────────────────────

def test_workforce_principalset_wraps_group_oid():
    restore = _with_cfg({"gcp_workforce_pool_id": "bt-entra-pool", "gcp_workforce_location": "global"})
    try:
        ps = k._workforce_principalset("1051c7ab-6284-4865-979f-55f55766e437")
        assert ps == ("principalSet://iam.googleapis.com/locations/global"
                      "/workforcePools/bt-entra-pool/group/1051c7ab-6284-4865-979f-55f55766e437")
    finally:
        restore()


def test_workforce_principalset_raises_without_pool():
    restore = _with_cfg({})   # no gcp_workforce_pool_id
    try:
        try:
            k._workforce_principalset("oid")
            raise AssertionError("expected K8sError when pool unset")
        except k.K8sError:
            pass
    finally:
        restore()


def test_bind_command_accepts_principalset_subject():
    # The GKE subject flows through the same command builder unchanged (safe chars,
    # so shlex adds no quotes) — no injection, and the principalSet is the --group.
    ps = ("principalSet://iam.googleapis.com/locations/global"
          "/workforcePools/bt-entra-pool/group/OID")
    cmd = k._entra_group_bind_command("view", ps)
    assert f"--group={ps}" in cmd
    assert "kubectl create clusterrolebinding entra-group-binding --clusterrole=view" in cmd


def test_connect_gateway_kubeconfig_is_token_free():
    server = ("https://connectgateway.googleapis.com/v1/projects/123456"
              "/locations/global/gkeMemberships/k8s-gke")
    out = k._connect_gateway_kubeconfig("k8s-gke", server)
    cfg = yaml.safe_load(out)
    cl = cfg["clusters"][0]["cluster"]
    assert cl["server"] == server
    assert "certificate-authority-data" not in cl   # gateway serves a public cert
    exec_blk = cfg["users"][0]["user"]["exec"]
    assert exec_blk["command"] == "gke-gcloud-auth-plugin"
    assert exec_blk.get("provideClusterInfo") is True
    assert "token" not in cfg["users"][0]["user"]   # picks up the active workforce identity
    assert "k8s-aws-v1." not in out and "client-key-data" not in out


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
