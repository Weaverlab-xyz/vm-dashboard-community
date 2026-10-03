"""The dashboard as a SPIFFE workload (services/dashboard_identity, api/spiffe_oidc).

docs/design/dashboard-workload-identity.md, Slice 1. No SPIRE image is available to the
test run, so ``docker exec … spire-server`` is a fake that answers in SPIRE 1.15's shapes
— ``jwt mint -output json`` is ``{"svid": {"token": …}}`` (cmd/spire-server/cli/jwt/mint.go
prints the MintJWTSVIDResponse) — and signs real ES256 tokens, so the served JWKS can be
checked against what was minted. Pinned:

  * the mint asks for the dashboard's own ID, one audience, a Go-duration TTL;
  * one file per audience, written atomically, re-minted only at half its life, and a
    second worker finds it fresh (the flock) instead of minting again;
  * a failed mint keeps the old file and records the error; an issuer that does not match
    what this side publishes is refused, not written;
  * switching an audience off removes its file;
  * discovery and keys are 404 until switched on; the issuer defaults to the agent
    audience plus /spiffe; keys come live from the server, public members only, fall back
    to the stored bundle, and verify the tokens actually minted;
  * the trust-domain sync runs for the dashboard's identity even with agent attestation
    off.

Run: python tests/test_dashboard_spiffe_identity.py   (or under pytest)
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="dash-identity-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-dashboard-identity-tests")

try:
    import cryptography  # noqa: F401
    import fastapi  # noqa: F401
    import jose  # noqa: F401
    import sqlalchemy  # noqa: F401
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
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from jose import jwk, jwt  # noqa: E402
from web_dashboard.database import (Base, SessionLocal, SpiffeTrustDomain,  # noqa: E402
                                    engine)
from web_dashboard.api import spiffe_oidc  # noqa: E402
from web_dashboard.services import (agent_service, config_service,  # noqa: E402
                                    dashboard_identity, dashboard_spire)

Base.metadata.create_all(bind=engine)

TD = "dash.example"
AGENT_AUDIENCE = "https://agents.example.com"
ISSUER = AGENT_AUDIENCE + "/spiffe"
DASH_ID = f"spiffe://{TD}/dashboard"

_KEY = ec.generate_private_key(ec.SECP256R1())
_PRIV = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()
_PUB_JWK = jwk.construct(_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
    "ES256").to_dict()
_PUB_JWK.pop("alg", None)
_JWT_KEY = dict(_PUB_JWK, kid="jwt-1", use="jwt-svid")
# Things a bundle may carry that must never be published as a JWT signing key.
_X509_KEY = {"kty": "EC", "crv": "P-256", "x": "AA", "y": "BB", "use": "x509-svid",
             "x5c": ["MIIB"]}
_PRIVATE_MEMBER_KEY = dict(_PUB_JWK, kid="jwt-2", use="jwt-svid", d="SECRET-D")


class FakeSpire:
    """docker exec <container> /opt/spire/bin/spire-server …, as SPIRE 1.15 answers it."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.calls = []
        self.down = False
        self.iss = ISSUER
        self.bundle_keys = [_JWT_KEY, _X509_KEY, _PRIVATE_MEMBER_KEY]

    def mints(self):
        return [c for c in self.calls if c[4:6] == ["jwt", "mint"]]

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        assert argv[:2] == ["docker", "exec"] and argv[3] == "/opt/spire/bin/spire-server"
        if self.down:
            return subprocess.CompletedProcess(argv, 1, "", "Error response from daemon: "
                                               "No such container: vmdash-spire-server\n")
        a = argv[4:]
        if a[:2] == ["bundle", "show"]:
            if "-format" in a:
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({"keys": self.bundle_keys}), "")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"trust_domain": TD}), "")
        if a[:2] == ["jwt", "mint"]:
            sid = a[a.index("-spiffeID") + 1]
            aud = a[a.index("-audience") + 1]
            ttl = a[a.index("-ttl") + 1]
            assert ttl.endswith("s") and ttl[:-1].isdigit(), f"-ttl must be a Go duration: {ttl}"
            now = int(time.time())
            claims = {"sub": sid, "aud": [aud], "iat": now, "exp": now + int(ttl[:-1])}
            if self.iss:
                claims["iss"] = self.iss
            token = jwt.encode(claims, _PRIV, algorithm="ES256", headers={"kid": "jwt-1"})
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"svid": {"token": token, "id": {"trust_domain": TD, "path": "/dashboard"},
                          "expires_at": str(claims["exp"]), "issued_at": str(now)}}), "")
        raise AssertionError(f"unexpected spire-server call {a}")


SPIRE = FakeSpire()
dashboard_spire._run = SPIRE

_KEYS = ("dashboard_spiffe_identity_enabled", "dashboard_spiffe_issuer",
         "dashboard_spiffe_aud_aws", "dashboard_spiffe_aud_azure",
         "dashboard_spiffe_aud_gcp", "dashboard_spiffe_aud_wlc",
         "wlc_identity_audience", "spire_attest_enabled")


def _fresh(**settings):
    """A clean slate: a new token directory, no cache, these settings and no others."""
    SPIRE.reset()
    dashboard_identity.clear_cache()
    os.environ["SPIFFE_TOKEN_DIR"] = tempfile.mkdtemp(prefix="spiffe-tokens-")
    for key in _KEYS:
        config_service.set(key, "")
    config_service.set(agent_service.AUDIENCE_CONFIG, AGENT_AUDIENCE)
    for key, value in settings.items():
        config_service.set(key, "1" if value is True else str(value))
    return os.environ["SPIFFE_TOKEN_DIR"]


def _token(directory, name):
    with open(os.path.join(directory, f"{name}.jwt"), encoding="ascii") as fh:
        return fh.read()


class _Admin:
    username = "tester"
    is_admin = True
    is_effective_admin = True


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(spiffe_oidc.router)
    app.include_router(spiffe_oidc.admin_router)
    from web_dashboard.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _Admin()
    return TestClient(app)


CLIENT = _client()


# ── minting and the token files ───────────────────────────────────────────────

def test_the_mint_asks_for_the_dashboards_own_id_and_one_audience():
    _fresh()
    token = dashboard_spire.mint_jwt("sts.amazonaws.com", 900, TD)
    (call,) = SPIRE.mints()
    assert call[4:] == ["jwt", "mint", "-spiffeID", DASH_ID, "-audience",
                        "sts.amazonaws.com", "-ttl", "900s", "-output", "json"]
    assert dashboard_identity.claims(token)["sub"] == DASH_ID


def test_one_file_per_audience_written_with_the_right_claims_and_mode():
    d = _fresh(dashboard_spiffe_identity_enabled=True, dashboard_spiffe_aud_aws=True,
               dashboard_spiffe_aud_azure=True)
    status = dashboard_identity.refresh()
    assert set(status["tokens"]) == {"aws", "azure"}, status
    for name, aud in (("aws", "sts.amazonaws.com"), ("azure", "api://AzureADTokenExchange")):
        c = dashboard_identity.claims(_token(d, name))
        assert c["sub"] == DASH_ID and c["aud"] == [aud] and c["iss"] == ISSUER
        assert stat.S_IMODE(os.stat(os.path.join(d, f"{name}.jwt")).st_mode) == 0o640
    # Nothing half-written left behind.
    assert not [f for f in os.listdir(d) if f.startswith(".") and f != ".lock"]


def test_a_fresh_file_is_not_minted_again_until_half_its_life_is_gone():
    _fresh(dashboard_spiffe_identity_enabled=True, dashboard_spiffe_aud_aws=True)
    now = time.time()
    dashboard_identity.refresh(now)
    dashboard_identity.refresh(now + 60)
    assert len(SPIRE.mints()) == 1
    dashboard_identity.refresh(now + dashboard_identity.TOKEN_TTL_S / 2 + 1)
    assert len(SPIRE.mints()) == 2


def test_two_workers_at_once_mint_once():
    """Both gunicorn workers run the loop. The flock makes the second find a fresh file."""
    _fresh(dashboard_spiffe_identity_enabled=True, dashboard_spiffe_aud_aws=True)
    threads = [threading.Thread(target=dashboard_identity.refresh) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(SPIRE.mints()) == 1


def test_a_failed_mint_keeps_the_old_file_and_records_why():
    d = _fresh(dashboard_spiffe_identity_enabled=True, dashboard_spiffe_aud_aws=True)
    now = time.time()
    dashboard_identity.refresh(now)
    before = _token(d, "aws")
    SPIRE.down = True
    status = dashboard_identity.refresh(now + dashboard_identity.TOKEN_TTL_S)
    assert _token(d, "aws") == before
    assert "not running" in status["tokens"]["aws"]["error"]
    shown = dashboard_identity.status()["tokens"]["aws"]
    assert shown["present"] and "not running" in shown["error"]


def test_an_issuer_that_does_not_match_is_refused_not_written():
    d = _fresh(dashboard_spiffe_identity_enabled=True, dashboard_spiffe_aud_aws=True)
    SPIRE.iss = ""          # SPIRE_JWT_ISSUER left blank on the server
    status = dashboard_identity.refresh()
    assert not os.path.exists(os.path.join(d, "aws.jwt"))
    assert "SPIRE_JWT_ISSUER" in status["tokens"]["aws"]["error"]


def test_switching_an_audience_off_removes_its_file():
    d = _fresh(dashboard_spiffe_identity_enabled=True, dashboard_spiffe_aud_aws=True)
    dashboard_identity.refresh()
    assert os.path.exists(os.path.join(d, "aws.jwt"))
    config_service.set("dashboard_spiffe_aud_aws", "")
    dashboard_identity.refresh()
    assert not os.path.exists(os.path.join(d, "aws.jwt"))


def test_off_means_no_calls_and_no_files():
    d = _fresh(dashboard_spiffe_aud_aws=True)
    assert dashboard_identity.refresh() == {"enabled": False}
    assert SPIRE.calls == [] and os.listdir(d) == []


def test_audiences_that_cannot_be_resolved_say_which_setting_to_fix():
    _fresh(dashboard_spiffe_identity_enabled=True, dashboard_spiffe_aud_gcp="projects/1/x")
    assert "full resource name" in dashboard_identity.refresh()["error"]
    _fresh(dashboard_spiffe_identity_enabled=True, dashboard_spiffe_aud_wlc=True)
    assert "Workload Credentials" in dashboard_identity.refresh()["error"]
    d = _fresh(dashboard_spiffe_identity_enabled=True, dashboard_spiffe_aud_wlc=True,
               wlc_identity_audience="api://wlc-registration")
    dashboard_identity.refresh()
    assert dashboard_identity.claims(_token(d, "wlc"))["aud"] == ["api://wlc-registration"]


def test_a_plain_http_issuer_is_refused():
    _fresh(dashboard_spiffe_identity_enabled=True, dashboard_spiffe_aud_aws=True,
           dashboard_spiffe_issuer="http://agents.example.com/spiffe")
    assert "not https" in dashboard_identity.refresh()["error"]
    assert SPIRE.mints() == []


# ── discovery ─────────────────────────────────────────────────────────────────

def test_discovery_and_keys_are_404_until_switched_on():
    _fresh()
    assert CLIENT.get("/spiffe/.well-known/openid-configuration").status_code == 404
    assert CLIENT.get("/spiffe/keys").status_code == 404


def test_the_issuer_defaults_to_the_agent_audience_and_the_setting_wins():
    _fresh(dashboard_spiffe_identity_enabled=True)
    doc = CLIENT.get("/spiffe/.well-known/openid-configuration").json()
    assert doc["issuer"] == ISSUER and doc["jwks_uri"] == ISSUER + "/keys"
    config_service.set("dashboard_spiffe_issuer", "https://id.example.com/spiffe/")
    doc = CLIENT.get("/spiffe/.well-known/openid-configuration").json()
    assert doc["issuer"] == "https://id.example.com/spiffe"


def test_keys_are_the_jwt_keys_only_with_public_members_only():
    _fresh(dashboard_spiffe_identity_enabled=True)
    resp = CLIENT.get("/spiffe/keys")
    assert resp.status_code == 200
    assert "max-age=300" in resp.headers["cache-control"]
    keys = resp.json()["keys"]
    assert {k["kid"] for k in keys} == {"jwt-1", "jwt-2"}
    assert "SECRET-D" not in resp.text and "x5c" not in resp.text
    for k in keys:
        assert set(k) <= {"kty", "kid", "crv", "x", "y", "n", "e", "alg", "use"}
        assert k["use"] == "sig" and k["alg"] == "ES256"


def test_a_minted_token_verifies_against_the_served_keys():
    """End to end, the way a cloud's STS does it: discovery → jwks_uri → verify."""
    d = _fresh(dashboard_spiffe_identity_enabled=True, dashboard_spiffe_aud_aws=True)
    dashboard_identity.refresh()
    token = _token(d, "aws")
    keys = CLIENT.get("/spiffe/keys").json()["keys"]
    kid = jwt.get_unverified_header(token)["kid"]
    key = next(k for k in keys if k["kid"] == kid)
    claims = jwt.decode(token, key, algorithms=["ES256"], audience="sts.amazonaws.com",
                        issuer=ISSUER)
    assert claims["sub"] == DASH_ID


def test_keys_fall_back_to_the_stored_bundle_and_503_when_there_is_none():
    _fresh(dashboard_spiffe_identity_enabled=True)
    SPIRE.down = True
    assert CLIENT.get("/spiffe/keys").status_code == 503
    db = SessionLocal()
    try:
        db.add(SpiffeTrustDomain(trust_domain=TD, created_by=dashboard_spire.OWNER,
                                 bundle_json=json.dumps({"keys": [_JWT_KEY]})))
        db.commit()
        resp = CLIENT.get("/spiffe/keys")
        assert resp.status_code == 200
        assert [k["kid"] for k in resp.json()["keys"]] == ["jwt-1"]
    finally:
        db.query(SpiffeTrustDomain).delete()
        db.commit()
        db.close()


def test_the_status_route_shows_the_files():
    _fresh(dashboard_spiffe_identity_enabled=True, dashboard_spiffe_aud_aws=True)
    dashboard_identity.refresh()
    body = CLIENT.get("/api/spiffe-identity").json()
    assert body["enabled"] and body["issuer"] == ISSUER
    assert body["tokens"]["aws"]["subject"] == DASH_ID
    assert "token" not in json.dumps(body["tokens"]).replace("tokens", "")


# ── the trust domain stays registered for the dashboard's own identity ────────

def test_the_trust_domain_sync_runs_for_the_identity_alone():
    _fresh()
    assert not dashboard_spire.server_in_use()
    config_service.set("dashboard_spiffe_identity_enabled", "1")
    assert dashboard_spire.server_in_use()
    db = SessionLocal()
    try:
        assert dashboard_spire.sync_if_due(db) is True
        rec = db.query(SpiffeTrustDomain).filter(SpiffeTrustDomain.trust_domain == TD).first()
        assert rec and rec.created_by == dashboard_spire.OWNER
    finally:
        db.query(SpiffeTrustDomain).delete()
        db.commit()
        db.close()


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
