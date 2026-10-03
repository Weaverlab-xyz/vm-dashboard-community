"""The API-tunnel kubeconfig download never hands a person an embedded credential.

`GET /api/k8s/clusters/{id}/api-tunnel-kubeconfig` (k8s:read) returns the cluster's
STORED kubeconfig with the server repointed at the tunnel. That is token-free only when
the stored file authenticates through a cloud exec plugin. An on-prem k3s cluster is
registered with its ADMIN kubeconfig (client certificate and key inline), and an imported
cluster can carry a static token — returning either gave every k8s:read user the same
unrevocable cluster-admin identity. These pin the refusal.

Run: python tests/test_k8s_tunnel_kubeconfig_creds.py   (or under pytest)
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-k8s-tunnel-creds")
os.environ["DATABASE_URL"] = "sqlite://"

try:
    import yaml
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover -- bare interpreter
    try:
        import pytest
        pytest.skip(f"dependency unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from web_dashboard.services import k8s_service  # noqa: E402
from web_dashboard.services.k8s_service import K8sCredentialRefused, K8sError  # noqa: E402

# Recognisable fake key material, so a test can prove it never reaches the message.
_SECRET = "LS0tLS1CRUdJTiBFQyBQUklWQVRFIEtFWS0tLS0tU0VDUkVULUtFWS1NQVRFUklBTA=="

_K3S_ADMIN = f"""
apiVersion: v1
kind: Config
clusters:
- name: default
  cluster:
    server: https://10.0.0.5:6443
    certificate-authority-data: Q0E=
contexts:
- name: default
  context: {{cluster: default, user: default}}
current-context: default
users:
- name: default
  user:
    client-certificate-data: Q0VSVA==
    client-key-data: {_SECRET}
"""

_EKS_EXEC = """
apiVersion: v1
kind: Config
clusters:
- name: eks
  cluster:
    server: https://abcdef.gr7.eu-west-1.eks.amazonaws.com
    certificate-authority-data: Q0E=
contexts:
- name: eks
  context: {cluster: eks, user: eks}
current-context: eks
users:
- name: eks
  user:
    exec:
      apiVersion: client.authentication.k8s.io/v1beta1
      command: aws
      args: [eks, get-token, --cluster-name, demo]
"""


def _with_user(user: dict) -> str:
    doc = yaml.safe_load(_EKS_EXEC)
    doc["users"][0]["user"] = user
    return yaml.safe_dump(doc)


# ── the helper ──────────────────────────────────────────────────────────────

def test_the_k3s_admin_shape_is_flagged():
    keys = k8s_service._embedded_credential_keys(_K3S_ADMIN)
    assert "client-key-data" in keys and "client-certificate-data" in keys, keys


def test_tokens_files_and_passwords_are_flagged():
    for user, key in (({"token": "abc"}, "token"),
                      ({"tokenFile": "/var/run/token"}, "tokenFile"),
                      ({"username": "admin", "password": "pw"}, "password"),
                      ({"client-key": "/etc/k.pem"}, "client-key"),
                      ({"auth-provider": {"name": "oidc"}}, "auth-provider")):
        assert key in k8s_service._embedded_credential_keys(_with_user(user)), user


def test_an_exec_plugin_kubeconfig_is_clean():
    assert k8s_service._embedded_credential_keys(_EKS_EXEC) == []


def test_no_users_is_clean_and_empty_values_do_not_count():
    assert k8s_service._embedded_credential_keys("apiVersion: v1\nkind: Config\n") == []
    assert k8s_service._embedded_credential_keys(_with_user({"token": ""})) == []


def test_the_helper_returns_names_never_values():
    keys = k8s_service._embedded_credential_keys(_K3S_ADMIN)
    assert not any(_SECRET in k for k in keys)


# ── the builder ─────────────────────────────────────────────────────────────

class _Row:
    def __init__(self, cloud):
        self.cloud = cloud


class _DB:
    """Just enough Session for build_api_tunnel_kubeconfig's one row lookup."""

    def __init__(self, cloud):
        self._row = _Row(cloud)

    def query(self, *_a, **_k):
        return self

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._row


def _build(stored: str, cloud: str) -> str:
    real_resolve, real_cfg = k8s_service.resolve_kubeconfig, k8s_service._cfg
    k8s_service.resolve_kubeconfig = lambda db, cid: stored
    k8s_service._cfg = lambda key, default="": default
    try:
        return k8s_service.build_api_tunnel_kubeconfig(_DB(cloud), "c1")
    finally:
        k8s_service.resolve_kubeconfig, k8s_service._cfg = real_resolve, real_cfg


def _refusal(stored: str, cloud: str) -> str:
    try:
        _build(stored, cloud)
    except K8sCredentialRefused as exc:
        return str(exc)
    raise AssertionError(f"a {cloud} cluster's embedded credential was handed out")


def test_an_on_prem_admin_kubeconfig_is_refused_and_points_at_dex():
    msg = _refusal(_K3S_ADMIN, "local")
    assert "Dex" in msg and "k3s-dex-auth.yml" in msg, msg
    assert _SECRET not in msg, "the refusal echoed key material"


def test_an_imported_cluster_with_a_static_token_is_refused():
    msg = _refusal(_with_user({"token": _SECRET}), "aws")
    assert "exec plugin" in msg, msg
    assert _SECRET not in msg, "the refusal echoed the token"


def test_the_refusal_is_a_k8s_error_so_old_callers_still_catch_it():
    assert issubclass(K8sCredentialRefused, K8sError)


def test_an_exec_plugin_kubeconfig_is_still_served_repointed():
    out = yaml.safe_load(_build(_EKS_EXEC, "aws"))
    cluster = out["clusters"][0]["cluster"]
    assert cluster["server"] == "https://127.0.0.1:6443"
    assert cluster["tls-server-name"] == "abcdef.gr7.eu-west-1.eks.amazonaws.com"
    assert out["users"][0]["user"]["exec"]["command"] == "aws"


# ── the route ───────────────────────────────────────────────────────────────

def test_the_route_answers_409_for_a_refusal_and_keeps_its_guard_first():
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "k8s.py"), encoding="utf-8").read()
    body = src.split("def api_tunnel_kubeconfig(", 1)[1].split("\n@router.", 1)[0]
    refused, generic = body.index("except K8sCredentialRefused"), body.index("except K8sError")
    assert refused < generic, "K8sError is caught first, so the refusal reads as a 404"
    assert "status_code=409" in body[refused:generic]
    assert body.index("_visible_or_404(") < body.index("build_api_tunnel_kubeconfig("), (
        "ownership must be checked before the kubeconfig is built")


def test_the_page_shows_the_reason_not_just_the_status():
    src = open(os.path.join(_ROOT, "web_dashboard", "templates", "k8s", "index.html"),
               encoding="utf-8").read()
    fn = src.split("async downloadApiKubeconfig()", 1)[1].split("async removeApiTunnel()", 1)[0]
    assert ".detail" in fn, "a refused download flashes only an HTTP status"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
