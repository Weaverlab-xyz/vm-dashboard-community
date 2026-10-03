"""On-prem k3s without the dashboard's admin kubeconfig (Slice 4).

docs/design/dashboard-workload-identity.md. A local cluster marked "Trusts dashboard
identity" (k3s-dashboard-auth.yml has run on it) gets the dashboard's own JWT-SVID, minted
for that cluster's audience, as the only credential in the kubeconfig every routine
operation uses. No SPIRE is available to the test run, so ``docker exec … spire-server``
is a fake that signs real ES256 tokens in SPIRE 1.15's ``jwt mint -output json`` shape.
Pinned:

  * untrusted, managed, or never marked: the stored kubeconfig, unchanged;
  * trusted: the stored cluster (server and CA) with the token as the only user — no
    client certificate, no key; the token's subject is the dashboard and its audience is
    THIS cluster's, so two clusters never share one; cached, re-minted near expiry;
  * a token that cannot be minted is an error naming the break-glass — never the admin
    certificate;
  * the people-facing API-tunnel download still reads the stored file;
  * the flag refuses managed clusters and an identity that is off, and the row reports it.

Run: python tests/test_k8s_dashboard_spiffe.py   (or under pytest)
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="k8s-dash-spiffe-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-k8s-dashboard-spiffe-tests")

try:
    import cryptography  # noqa: F401
    import jose  # noqa: F401
    import sqlalchemy  # noqa: F401
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

# First-party imports UNGUARDED: a broken module must fail this file, not skip it.
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from jose import jwt  # noqa: E402
from web_dashboard.database import Base, K8sCluster, SessionLocal, engine  # noqa: E402
from web_dashboard.services import (agent_service, config_service,  # noqa: E402
                                    dashboard_spire, k8s_service)

Base.metadata.create_all(bind=engine)

TD = "dash.example"
AUDIENCE = "https://agents.example.com"
ISSUER = AUDIENCE + "/spiffe"
ADMIN_CERT = "LS0tLS1CRUdJTi1BRE1JTi1DRVJULS0tLS0="
ADMIN_KEY = "LS0tLS1CRUdJTi1BRE1JTi1LRVktLS0tLQ=="

_KEY = ec.generate_private_key(ec.SECP256R1())
_PRIV = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()


class FakeSpire:
    def __init__(self):
        self.mints = []
        self.down = False

    def __call__(self, argv, timeout):
        a = argv[4:]
        if self.down:
            return subprocess.CompletedProcess(argv, 1, "", "No such container: vmdash-spire-server\n")
        if a[:2] == ["bundle", "show"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"trust_domain": TD}), "")
        if a[:2] == ["jwt", "mint"]:
            aud = a[a.index("-audience") + 1]
            ttl = int(a[a.index("-ttl") + 1].rstrip("s"))
            self.mints.append(aud)
            now = int(time.time())
            token = jwt.encode({"sub": a[a.index("-spiffeID") + 1], "aud": [aud], "iss": ISSUER,
                                "iat": now, "exp": now + ttl, "n": len(self.mints)},
                               _PRIV, algorithm="ES256")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"svid": {"token": token}}), "")
        raise AssertionError(f"unexpected spire-server call {a}")


SPIRE = FakeSpire()
dashboard_spire._run = SPIRE


def _stored(server="https://10.0.0.5:6443"):
    return yaml.safe_dump({
        "apiVersion": "v1", "kind": "Config", "current-context": "default",
        "clusters": [{"name": "default", "cluster": {
            "server": server, "certificate-authority-data": "Q0EtREFUQQ=="}}],
        "users": [{"name": "default", "user": {"client-certificate-data": ADMIN_CERT,
                                               "client-key-data": ADMIN_KEY}}],
        "contexts": [{"name": "default", "context": {"cluster": "default", "user": "default"}}],
    })


def _cluster(cloud="local", trusted=False):
    cid = str(uuid.uuid4())
    ref = f"k8s_kubeconfig_{cid}"
    config_service.set(ref, _stored())
    db = SessionLocal()
    try:
        db.add(K8sCluster(id=cid, cloud=cloud, name=f"c-{cid[:6]}", status="registered",
                          kubeconfig_ref=ref))
        db.commit()
    finally:
        db.close()
    config_service.set(k8s_service.spiffe_trust_key(cid), "1" if trusted else "")
    return cid


def _fresh(identity=True):
    SPIRE.mints.clear()
    SPIRE.down = False
    k8s_service._svid_cache.clear()
    config_service.set(agent_service.AUDIENCE_CONFIG, AUDIENCE)
    config_service.set("dashboard_spiffe_issuer", "")
    config_service.set("dashboard_spiffe_identity_enabled", "1" if identity else "")


def _resolve(cid):
    db = SessionLocal()
    try:
        return k8s_service.resolve_kubeconfig(db, cid)
    finally:
        db.close()


def _claims(token):
    return jwt.get_unverified_claims(token)


# ── unchanged unless marked ───────────────────────────────────────────────────

def test_an_unmarked_or_managed_cluster_gets_the_stored_kubeconfig_unchanged():
    _fresh()
    assert _resolve(_cluster()) == _stored()
    assert _resolve(_cluster(cloud="aws", trusted=True)) == _stored(), (
        "a managed cluster already has cloud tokens; the flag means nothing there")
    assert SPIRE.mints == []


# ── trusted: the dashboard's token, and nothing else ──────────────────────────

def test_a_trusted_cluster_gets_the_token_and_no_admin_certificate():
    _fresh()
    cid = _cluster(trusted=True)
    out = _resolve(cid)
    assert ADMIN_CERT not in out and ADMIN_KEY not in out and "client-key" not in out
    cfg = yaml.safe_load(out)
    (cluster,) = cfg["clusters"]
    assert cluster["cluster"] == {"server": "https://10.0.0.5:6443",
                                  "certificate-authority-data": "Q0EtREFUQQ=="}
    (user,) = cfg["users"]
    assert set(user["user"]) == {"token"}
    c = _claims(user["user"]["token"])
    assert c["sub"] == f"spiffe://{TD}/dashboard"
    assert c["aud"] == [f"{ISSUER}/k8s/{cid}"] == [k8s_service.dashboard_audience(cid)]
    assert cfg["contexts"][0]["context"]["user"] == user["name"]


def test_two_clusters_never_share_an_audience():
    _fresh()
    a, b = _cluster(trusted=True), _cluster(trusted=True)
    ta = yaml.safe_load(_resolve(a))["users"][0]["user"]["token"]
    tb = yaml.safe_load(_resolve(b))["users"][0]["user"]["token"]
    assert _claims(ta)["aud"] != _claims(tb)["aud"]


def test_the_token_is_cached_and_re_minted_near_expiry():
    _fresh()
    cid = _cluster(trusted=True)
    now = time.time()
    first = k8s_service._dashboard_svid(cid, now)
    assert k8s_service._dashboard_svid(cid, now + 60) == first
    assert len(SPIRE.mints) == 1
    later = now + k8s_service.SPIFFE_K8S_TTL_S - k8s_service._SPIFFE_K8S_REMINT_S + 1
    k8s_service._dashboard_svid(cid, later)
    assert len(SPIRE.mints) == 2


# ── no silent fallback ────────────────────────────────────────────────────────

def test_a_token_that_cannot_be_minted_is_an_error_never_the_admin_certificate():
    _fresh()
    cid = _cluster(trusted=True)
    SPIRE.down = True
    try:
        out = _resolve(cid)
        raise AssertionError("returned a kubeconfig: " + ("ADMIN CERT" if ADMIN_CERT in out else "?"))
    except k8s_service.K8sError as exc:
        assert "break-glass" in str(exc) and "not running" in str(exc)


def test_an_identity_switched_off_after_marking_is_an_error_too():
    _fresh()
    cid = _cluster(trusted=True)
    config_service.set("dashboard_spiffe_identity_enabled", "")
    try:
        _resolve(cid)
        raise AssertionError("fell back to the admin kubeconfig")
    except k8s_service.K8sError as exc:
        assert "identity is off" in str(exc) and "break-glass" in str(exc)


# ── the people-facing download keeps reading the stored file ──────────────────

def test_the_api_tunnel_download_reads_the_stored_file():
    import inspect
    src = inspect.getsource(k8s_service.build_api_tunnel_kubeconfig)
    assert "stored_kubeconfig(db, cluster_id)" in src and "resolve_kubeconfig(" not in src, (
        "the tunnel download must not be handed the dashboard's own token")


# ── the flag ──────────────────────────────────────────────────────────────────

def test_the_flag_refuses_managed_clusters_and_an_identity_that_is_off():
    _fresh()
    db = SessionLocal()
    try:
        try:
            k8s_service.set_spiffe_trust(db, _cluster(cloud="gcp"), True)
            raise AssertionError("marked a managed cluster")
        except k8s_service.K8sError as exc:
            assert "on-prem" in str(exc)
        config_service.set("dashboard_spiffe_identity_enabled", "")
        try:
            k8s_service.set_spiffe_trust(db, _cluster(), True)
            raise AssertionError("marked a cluster to trust a token nothing mints")
        except k8s_service.K8sError as exc:
            assert "Settings" in str(exc)
        cid = _cluster(trusted=True)
        assert k8s_service.set_spiffe_trust(db, cid, False)["spiffe_trusted"] is False
    finally:
        db.close()


def test_the_row_reports_the_flag_and_the_audience():
    _fresh()
    cid = _cluster(trusted=True)
    db = SessionLocal()
    try:
        row = db.query(K8sCluster).filter(K8sCluster.id == cid).first()
        item = k8s_service._serialize(row)
    finally:
        db.close()
    assert item["spiffe_trusted"] is True
    assert item["dashboard_audience"] == f"{ISSUER}/k8s/{cid}"


def test_the_route_needs_k8s_write():
    import inspect
    from web_dashboard.api import k8s as k8s_api
    src = inspect.getsource(k8s_api.set_spiffe_trust)
    assert 'require_permission("k8s", "write")' in src and "log_audit" in src


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
