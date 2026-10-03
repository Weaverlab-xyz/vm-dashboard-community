"""People reach on-prem clusters through Dex, and only through Dex.

For a `cloud=local` cluster the API-tunnel download is a `kubectl oidc-login`
kubeconfig against Dex — never the stored kubeconfig, which for k3s is the admin one.
It needs the Dex settings AND the per-cluster "Trusts Dex" flag; without either it
refuses and names the missing step. Design: docs/design/agent-and-human-identity.md,
"On-prem: the one path that changes".

Run: python tests/test_k8s_onprem_dex_kubeconfig.py   (or under pytest)
"""
import base64
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-k8s-onprem-dex")
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
from web_dashboard.services.k8s_service import K8sCredentialRefused  # noqa: E402

_SECRET = "LS0tLS1CRUdJTiBFQyBQUklWQVRFIEtFWS0tLS0tU0VDUkVULUtFWS1NQVRFUklBTA=="
_ISSUER = "https://dex.lab.test:5554"
_CA = "-----BEGIN CERTIFICATE-----\nTEFCQ0E=\n-----END CERTIFICATE-----"

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


class _Row:
    def __init__(self, cloud):
        self.cloud = cloud


class _DB:
    def __init__(self, cloud):
        self._row = _Row(cloud)

    def query(self, *_a, **_k):
        return self

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._row


def _build(cfg: dict, cloud: str = "local", stored: str = _K3S_ADMIN) -> str:
    real_resolve, real_cfg = k8s_service.stored_kubeconfig, k8s_service._cfg
    k8s_service.stored_kubeconfig = lambda db, cid: stored
    k8s_service._cfg = lambda key, default="": cfg.get(key, default)
    try:
        return k8s_service.build_api_tunnel_kubeconfig(_DB(cloud), "c1")
    finally:
        k8s_service.stored_kubeconfig, k8s_service._cfg = real_resolve, real_cfg


def _refusal(cfg: dict) -> str:
    try:
        _build(cfg)
    except K8sCredentialRefused as exc:
        assert _SECRET not in str(exc), "the refusal echoed key material"
        return str(exc)
    raise AssertionError("an on-prem kubeconfig was handed out without Dex ready")


_READY = {"dex_issuer_url": _ISSUER, "dex_k8s_client_id": "kubernetes",
          "k8s_dex_trusted_c1": "1"}


# ── refusals name the missing step ──────────────────────────────────────────

def test_no_dex_issuer_refuses_and_says_where_to_set_it():
    msg = _refusal({})
    assert "Settings → Kubernetes → Dex" in msg and "dex-helm.yml" in msg, msg
    assert "k3s-dex-auth.yml" in msg, msg


def test_an_untrusted_cluster_refuses_and_says_how_to_trust_it():
    msg = _refusal({"dex_issuer_url": _ISSUER})
    assert "Trusts Dex" in msg and "k3s-dex-auth.yml" in msg, msg


# ── the kubeconfig a person gets ────────────────────────────────────────────

def _dex_user(out: str) -> dict:
    doc = yaml.safe_load(out)
    assert len(doc["users"]) == 1
    return doc["users"][0]["user"]


def test_a_ready_cluster_gets_an_oidc_login_kubeconfig_and_no_credential():
    out = _build(_READY)
    assert _SECRET not in out, "the admin key reached the person's kubeconfig"
    assert k8s_service._embedded_credential_keys(out) == []
    user = _dex_user(out)
    assert set(user) == {"exec"}, f"the user entry carries more than an exec block: {set(user)}"
    args = user["exec"]["args"]
    assert args[:2] == ["oidc-login", "get-token"]
    assert f"--oidc-issuer-url={_ISSUER}" in args
    assert "--oidc-client-id=kubernetes" in args
    assert "--oidc-extra-scope=groups" in args, "without the groups scope RBAC by group fails"
    assert not any(a.startswith("--certificate-authority-data=") for a in args), (
        "no CA was configured, so none may be embedded")


def test_the_kubeconfig_goes_through_the_tunnel_and_keeps_the_cluster_ca():
    doc = yaml.safe_load(_build(_READY))
    cluster = doc["clusters"][0]["cluster"]
    assert cluster["server"] == "https://127.0.0.1:6443"
    assert cluster["tls-server-name"] == "10.0.0.5"
    assert cluster["certificate-authority-data"] == "Q0E="


def test_a_lab_ca_is_embedded_for_oidc_login():
    args = _dex_user(_build({**_READY, "dex_ca_pem": _CA}))["exec"]["args"]
    embedded = [a.split("=", 1)[1] for a in args if a.startswith("--certificate-authority-data=")]
    assert embedded and base64.b64decode(embedded[0]).decode() == _CA


def test_a_trailing_slash_on_the_issuer_is_dropped():
    """The API server compares the issuer string; a stray slash rejects every token."""
    args = _dex_user(_build({**_READY, "dex_issuer_url": _ISSUER + "/"}))["exec"]["args"]
    assert f"--oidc-issuer-url={_ISSUER}" in args


def test_on_prem_always_goes_through_dex_even_without_an_embedded_credential():
    """Dex is the only way on-prem, not merely the fallback when the file is unsafe."""
    exec_stored = _K3S_ADMIN.replace(
        f"    client-certificate-data: Q0VSVA==\n    client-key-data: {_SECRET}\n",
        "    exec: {apiVersion: client.authentication.k8s.io/v1beta1, command: something}\n")
    assert "client-key-data" not in exec_stored
    try:
        _build({}, stored=exec_stored)
    except K8sCredentialRefused:
        pass
    else:
        raise AssertionError("an on-prem cluster's stored kubeconfig was served without Dex")


def test_managed_clusters_are_unaffected():
    """A managed cluster's exec-plugin kubeconfig is served exactly as before, Dex or not."""
    eks = _K3S_ADMIN.replace(
        f"    client-certificate-data: Q0VSVA==\n    client-key-data: {_SECRET}\n",
        "    exec: {apiVersion: client.authentication.k8s.io/v1beta1, command: aws, "
        "args: [eks, get-token, --cluster-name, demo]}\n")
    out = yaml.safe_load(_build(_READY, cloud="aws", stored=eks))
    assert out["users"][0]["user"]["exec"]["command"] == "aws"


# ── the flag and its route ──────────────────────────────────────────────────

def _src(*parts):
    return open(os.path.join(_ROOT, *parts), encoding="utf-8").read()


def test_the_trust_route_is_guarded_and_needs_write():
    src = _src("web_dashboard", "api", "k8s.py")
    body = src.split("def set_dex_trust(", 1)[1].split("\n@router.", 1)[0]
    assert 'require_permission("k8s", "write")' in body
    assert body.index("_visible_or_404(") < body.index("k8s_service.set_dex_trust("), (
        "ownership must be checked before the flag changes")


def test_the_trust_flag_is_on_prem_only():
    svc = _src("web_dashboard", "services", "k8s_service.py")
    body = svc.split("def set_dex_trust(", 1)[1].split("\ndef ", 1)[0]
    assert 'row.cloud != "local"' in body, "managed clusters must not take the on-prem flag"
    assert '"dex_trusted":' in svc and 'r.cloud == "local"' in svc.split('"dex_trusted":', 1)[1][:80]


def test_the_settings_fields_exist_and_are_bound_in_the_panel():
    from web_dashboard.api.setup import K8sManagementFeatureConfig
    panel = _src("web_dashboard", "templates", "settings.html")
    for key in ("dex_issuer_url", "dex_k8s_client_id", "dex_ca_pem"):
        assert key in K8sManagementFeatureConfig.model_fields, f"{key} missing from the panel model"
        assert f"panelCfg.{key}" in panel, f"{key} is unbound, so a save discards it"


def test_the_row_offers_the_toggle_only_for_on_prem_clusters():
    page = _src("web_dashboard", "templates", "k8s", "index.html")
    button = page.split('@click="toggleDexTrust(c)"', 1)[0].rsplit("<button", 1)[1]
    assert "c.cloud === 'local'" in button


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
