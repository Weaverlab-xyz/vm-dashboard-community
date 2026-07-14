"""Unit tests for the direct k8s API TCP tunnel + token-free kubeconfig feature.

Two independent parts (each stubs the minimum so the target module imports without
an app/DB, mirroring test_pra_k8s_vault.py / test_entitle_agent_rbac.py):

A. terraform_pra_service._generate_api_tunnel_hcl emits a generic tunnel_type="tcp"
   sra_protocol_tunnel_jump with tunnel_definitions + tunnel_listen_address, and NO
   url/ca_certificates (k8s-tunnel-only) and NO Vault account.
B. k8s_service._repoint_kubeconfig_to_tunnel repoints server → 127.0.0.1:<port>,
   adds tls-server-name, keeps the CA + the cloud-native exec user, and leaves NO
   embedded token/client-key in the output.

Runs under pytest or standalone:  python tests/test_pra_api_tunnel.py
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

# k8s_service imports ..database + sqlalchemy.orm at module load — stub both so it
# imports without a real DB engine (same approach as test_entitle_agent_rbac.py).
sys.modules.setdefault("sqlalchemy", types.ModuleType("sqlalchemy"))
_orm_stub = types.ModuleType("sqlalchemy.orm")
_orm_stub.Session = object
sys.modules.setdefault("sqlalchemy.orm", _orm_stub)
_db_stub = types.ModuleType("web_dashboard.database")
_db_stub.Job = type("Job", (), {})
_db_stub.K8sCluster = type("K8sCluster", (), {})
sys.modules.setdefault("web_dashboard.database", _db_stub)

import yaml  # noqa: E402

from web_dashboard.services import terraform_pra_service as pra  # noqa: E402
from web_dashboard.services import k8s_service as k  # noqa: E402


# ── Part A: HCL emission ──────────────────────────────────────────────────────

def test_api_tunnel_hcl_is_generic_tcp_with_pinned_local_port():
    hcl = pra._generate_api_tunnel_hcl(
        name="k8s-gke-api", hostname="35.238.242.201",
        jump_group_name="Cloud", jumpoint_name="GCP Run",
        tunnel_definitions="6443;443")
    assert 'resource "sra_protocol_tunnel_jump"' in hcl
    assert 'tunnel_type           = "tcp"' in hcl
    # local;remote pair — semicolon format confirmed against a live appliance jump.
    assert 'tunnel_definitions    = "6443;443"' in hcl
    assert 'tunnel_listen_address = "127.0.0.1"' in hcl
    # Name→id lookups for the pre-existing Jump Group + Jumpoint.
    assert 'data "sra_jump_group_list" "jg"' in hcl and '"Cloud"' in hcl
    assert 'data "sra_jumpoint_list" "jp"' in hcl and '"GCP Run"' in hcl
    assert 'output "tunnel_jump_id"' in hcl
    # k8s-tunnel-only fields + credential injection must NOT be present.
    assert "ca_certificates" not in hcl
    assert "\n  url" not in hcl and "url  " not in hcl
    assert "sra_vault" not in hcl
    assert "tunnel_type           = \"k8s\"" not in hcl


def test_api_tunnel_definitions_built_from_ports():
    # provision_api_tunnel composes tunnel_definitions as "<local>;<remote>".
    import asyncio
    calls = {}

    def _fake_sync(name, hostname, jgn, jpn, tunnel_definitions, tag="Kubernetes", client_secret=""):
        calls["tunnel_definitions"] = tunnel_definitions
        return {"tunnel_jump_id": "1", "jump_group_name": jgn, "tf_state_json": None}

    orig = pra._provision_api_tunnel_sync
    pra._provision_api_tunnel_sync = _fake_sync
    try:
        asyncio.run(pra.provision_api_tunnel(
            name="x", hostname="h", jump_group_name="jg", jumpoint_name="jp",
            local_port=6443, remote_port=443))
    finally:
        pra._provision_api_tunnel_sync = orig
    assert calls["tunnel_definitions"] == "6443;443"


# ── Part B: kubeconfig transform ──────────────────────────────────────────────

_GKE_KUBECONFIG = """\
apiVersion: v1
kind: Config
current-context: gke_proj_us-central1-a_k8s-gke
clusters:
- name: gke_proj_us-central1-a_k8s-gke
  cluster:
    server: https://35.238.242.201
    certificate-authority-data: QUJDREVG
contexts:
- name: gke_proj_us-central1-a_k8s-gke
  context:
    cluster: gke_proj_us-central1-a_k8s-gke
    user: gke_proj_us-central1-a_k8s-gke
users:
- name: gke_proj_us-central1-a_k8s-gke
  user:
    exec:
      apiVersion: client.authentication.k8s.io/v1beta1
      command: gke-gcloud-auth-plugin
      provideClusterInfo: true
"""


def test_repoint_kubeconfig_swaps_server_and_keeps_exec_auth():
    out = k._repoint_kubeconfig_to_tunnel(_GKE_KUBECONFIG, 6443)
    cfg = yaml.safe_load(out)
    cl = cfg["clusters"][0]["cluster"]
    assert cl["server"] == "https://127.0.0.1:6443"
    assert cl["tls-server-name"] == "35.238.242.201"   # so the real cert SAN validates
    assert cl["certificate-authority-data"] == "QUJDREVG"   # CA preserved
    # Auth is untouched: the cloud-native exec plugin, NO embedded static credential.
    user = cfg["users"][0]["user"]
    assert user["exec"]["command"] == "gke-gcloud-auth-plugin"
    assert "token" not in user and "client-key-data" not in user and "client-certificate-data" not in user
    assert "token" not in out and "client-key-data" not in out


def test_repoint_honors_local_port():
    out = k._repoint_kubeconfig_to_tunnel(_GKE_KUBECONFIG, 7000)
    cfg = yaml.safe_load(out)
    assert cfg["clusters"][0]["cluster"]["server"] == "https://127.0.0.1:7000"


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
