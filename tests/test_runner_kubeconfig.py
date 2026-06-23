"""Unit tests for k8s_service._runner_kubeconfig cloud-exec token minting.

Verifies the transient-runner kubeconfig prep swaps each managed cloud's exec-auth
block — EKS (`aws eks get-token`), AAD-integrated AKS (`kubelogin`), GKE
(`gke-gcloud-auth-plugin`) — for a static bearer token minted server-side, and
leaves non-exec kubeconfigs (client-cert, raw token) untouched. This is what lets a
throwaway kubectl/helm container authenticate to a managed cluster without the
cloud CLIs installed (agent install + Entitle registration run this way).

The heavy app deps (web_dashboard.database → bcrypt, the cloud SDKs) are stubbed in
sys.modules so the test needs only PyYAML + SQLAlchemy. Runs under pytest, or
standalone:  python tests/test_runner_kubeconfig.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _install_stubs():
    # web_dashboard.database pulls bcrypt et al. at import — stub the two names
    # k8s_service imports from it.
    dbmod = types.ModuleType("web_dashboard.database")
    dbmod.Job = type("Job", (), {})
    dbmod.K8sCluster = type("K8sCluster", (), {})
    sys.modules["web_dashboard.database"] = dbmod

    # The cloud minters `_runner_kubeconfig` lazily `from . import`s — stub them so
    # no boto3/azure/google SDK is needed and the minted token is deterministic.
    aws = types.ModuleType("web_dashboard.services.aws_service")
    aws.eks_get_token = lambda cluster_name, region: f"eks-token::{cluster_name}::{region}"
    sys.modules["web_dashboard.services.aws_service"] = aws

    az = types.ModuleType("web_dashboard.services.azure_service")
    az.AKS_AAD_SERVER_APP_ID = "6dae42f8-4368-4678-94ff-3960e28e3630"
    az.aks_get_token = lambda server_id=az.AKS_AAD_SERVER_APP_ID: f"aks-token::{server_id}"
    sys.modules["web_dashboard.services.azure_service"] = az

    gcp = types.ModuleType("web_dashboard.services.gcp_service")
    gcp.gke_get_token = lambda: "gke-token"
    sys.modules["web_dashboard.services.gcp_service"] = gcp


_install_stubs()
try:
    from web_dashboard.services import k8s_service
except Exception as exc:  # pragma: no cover — skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"k8s_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

import yaml


def _kubeconfig(user: dict) -> str:
    return yaml.safe_dump({
        "apiVersion": "v1",
        "current-context": "ctx",
        "contexts": [{"name": "ctx", "context": {"user": "u", "cluster": "c"}}],
        "clusters": [{"name": "c", "cluster": {"server": "https://api.example:443"}}],
        "users": [{"name": "u", "user": user}],
    })


def _user_of(rendered: str) -> dict:
    return yaml.safe_load(rendered)["users"][0]["user"]


def test_eks_exec_swapped_for_token():
    kc = _kubeconfig({"exec": {"command": "aws",
        "args": ["eks", "get-token", "--cluster-name", "demo", "--region", "us-east-2"]}})
    user = _user_of(k8s_service._runner_kubeconfig(kc))
    assert user["token"] == "eks-token::demo::us-east-2"
    assert "exec" not in user


def test_aks_kubelogin_swapped_for_token():
    kc = _kubeconfig({"exec": {"command": "kubelogin",
        "args": ["get-token", "--server-id", "srv-123", "--login", "spn"]}})
    user = _user_of(k8s_service._runner_kubeconfig(kc))
    assert user["token"] == "aks-token::srv-123"
    assert "exec" not in user


def test_aks_kubelogin_default_server_id():
    kc = _kubeconfig({"exec": {"command": "kubelogin", "args": ["get-token", "--login", "msi"]}})
    user = _user_of(k8s_service._runner_kubeconfig(kc))
    assert user["token"] == "aks-token::6dae42f8-4368-4678-94ff-3960e28e3630"


def test_gke_plugin_swapped_for_token():
    kc = _kubeconfig({"exec": {"command": "gke-gcloud-auth-plugin", "args": []}})
    user = _user_of(k8s_service._runner_kubeconfig(kc))
    assert user["token"] == "gke-token"
    assert "exec" not in user


def test_client_cert_left_unchanged():
    kc = _kubeconfig({"client-certificate-data": "AAAA", "client-key-data": "BBBB"})
    user = _user_of(k8s_service._runner_kubeconfig(kc))
    assert user["client-certificate-data"] == "AAAA"
    assert "token" not in user


def test_raw_token_left_unchanged():
    kc = _kubeconfig({"token": "already-a-token"})
    user = _user_of(k8s_service._runner_kubeconfig(kc))
    assert user["token"] == "already-a-token"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
