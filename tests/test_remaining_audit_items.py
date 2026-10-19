"""The smaller audit findings, one test group each.

  * EPML installation token: minting one needs ``epml:write`` (it was ``epml:read``), its
    lifetime is capped at a day (it was a year), and every mint is audited.
  * ``/api/cache/status`` needs an administrator; ``/api/setup/status`` still says whether
    setup is complete to anyone, but lists the configured keys to an administrator only.
  * ``api/setup`` and ``api/secrets`` checked "admin" by decoding the JWT themselves, so
    any token signed with the key that carried a ``sub`` passed -- a ``pre_auth`` token
    from the first half of an MFA login included -- and an admin's PAT did not. They now
    resolve through ``auth.resolve_bearer`` like every other route.
  * A new PAT expires in 90 days unless told otherwise (it was never), and no later than
    ``pat_max_days`` (365 by default; 0 lifts the cap).
  * ``/api/config-mgmt/cloud-targets`` lists only the machines in the caller's
    workstreams, as the cloud tabs do; it listed every deployed VM to anyone signed in.

Runs standalone:
    python tests/test_remaining_audit_items.py
"""
import os
import sys
import tempfile
import uuid
from datetime import timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="remaining-audit-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "remaining-audit-test-secret")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web_dashboard.api import auth, config_mgmt, epml, tokens  # noqa: E402
from web_dashboard.api import secrets as secrets_api  # noqa: E402
from web_dashboard.api import setup as setup_api  # noqa: E402
from web_dashboard.api.auth import full_access_map  # noqa: E402
from web_dashboard.database import (AuditLog, Base, Job, PersonalAccessToken,  # noqa: E402
                                    SessionLocal, User, engine)
from web_dashboard.services import config_service, epml_service  # noqa: E402
from web_dashboard.utils.clock import utcnow  # noqa: E402

Base.metadata.create_all(bind=engine)


def _person(name, workgroups=(), admin=False, perms=None):
    db = SessionLocal()
    try:
        u = User(id=str(uuid.uuid4()), username=name, hashed_password="x", is_admin=admin)
        u.workgroups_list = list(workgroups)
        u.permissions_dict = full_access_map() if perms is None else perms
        db.add(u)
        db.commit()
        db.refresh(u)
        db.expunge(u)
        return u
    finally:
        db.close()


ROOT = _person("root", admin=True)
ALICE = _person("alice", ["team-a"])
READER = _person("reader", perms={"epml": ["read"]})


def _client(user, *routers):
    app = FastAPI()
    for r in routers:
        app.include_router(r)

    def _me():
        db = SessionLocal()
        try:
            return db.query(User).filter(User.id == user.id).first()
        finally:
            db.close()
    app.dependency_overrides[auth.get_current_user] = _me
    return TestClient(app)


def _audited(action):
    db = SessionLocal()
    try:
        return db.query(AuditLog).filter(AuditLog.action == action).count()
    finally:
        db.close()


# ── EPML installation token ──────────────────────────────────────────────────

_minted = []


async def _fake_mint(expiry_minutes=480):
    _minted.append(expiry_minutes)
    return "epml-token"


epml_service.get_installation_token = _fake_mint


def test_minting_an_epml_token_needs_write_and_is_audited():
    assert _client(READER, epml.router).get("/api/epml/token").status_code == 403
    before = _audited("epml_installation_token_issued")
    r = _client(ALICE, epml.router).get("/api/epml/token")
    assert r.status_code == 200 and r.json()["token"] == "epml-token", r.text
    assert _audited("epml_installation_token_issued") == before + 1


def test_the_epml_token_lives_at_most_a_day():
    c = _client(ALICE, epml.router)
    assert c.get("/api/epml/token?expiry_minutes=525600").json()["expiry_minutes"] == 1440
    assert _minted[-1] == 1440
    assert c.get("/api/epml/token?expiry_minutes=5").json()["expiry_minutes"] == 30
    assert c.get("/api/epml/token").json()["expiry_minutes"] == 480
    assert epml_service.clamp_token_expiry(10 ** 6) == 1440


# ── status routes ────────────────────────────────────────────────────────────

def _main_client():
    from web_dashboard import main
    config_service.is_setup_complete = lambda: True
    return TestClient(main.app)


def _bearer(user):
    return {"Authorization": "Bearer " + auth.create_access_token({"sub": user.username})}


def test_cache_status_is_for_administrators():
    c = _main_client()
    assert c.get("/api/cache/status").status_code == 401
    assert c.get("/api/cache/status", headers=_bearer(ALICE)).status_code == 403
    assert c.get("/api/cache/status", headers=_bearer(ROOT)).status_code == 200


def test_setup_status_lists_keys_to_administrators_only():
    config_service.set("aws_default_region", "eu-west-1")
    c = _main_client()
    for headers in ({}, _bearer(ALICE), {"Authorization": "Bearer junk"}):
        body = c.get("/api/setup/status", headers=headers).json()
        assert body == {"complete": True, "configured_keys": []}, (headers, body)
    body = c.get("/api/setup/status", headers=_bearer(ROOT)).json()
    assert "aws_default_region" in body["configured_keys"], body


# ── the hand-rolled admin checks ─────────────────────────────────────────────

class _Req:
    def __init__(self, token):
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}


def _status(fn, token):
    from fastapi import HTTPException
    try:
        fn(_Req(token))
    except HTTPException as exc:
        return exc.status_code
    return 200


def _admin_pat():
    raw = tokens._generate_raw()
    db = SessionLocal()
    try:
        db.add(PersonalAccessToken(user_id=ROOT.id, name="ci", token_hash=tokens.hash_pat(raw)))
        db.commit()
    finally:
        db.close()
    return raw


def test_setup_and_secrets_admin_checks_resolve_like_every_route():
    pre_auth = auth._create_pre_auth_token(ROOT.username)   # MFA's first half, not a login
    login = auth.create_access_token({"sub": ROOT.username})
    pat = _admin_pat()
    for fn in (setup_api._require_admin, secrets_api._require_admin):
        assert _status(fn, None) == 401
        assert _status(fn, pre_auth) == 401, f"{fn.__module__} accepted a pre_auth token"
        assert _status(fn, auth.create_access_token({"sub": ALICE.username})) == 403
        assert _status(fn, login) == 200
        assert _status(fn, pat) == 200, f"{fn.__module__} refused an administrator's PAT"
    assert setup_api._require_admin(_Req(pat)) == "root"


# ── PAT lifetime ─────────────────────────────────────────────────────────────

def _days_left(resp):
    from datetime import datetime
    exp = resp.json()["expires_at"]
    if exp is None:
        return None
    return round((datetime.fromisoformat(exp) - utcnow()) / timedelta(days=1))


def test_a_new_pat_defaults_to_90_days_and_respects_the_cap():
    c = _client(ALICE, tokens.router)
    config_service.delete("pat_max_days")
    assert _days_left(c.post("/api/tokens", json={"name": "a"})) == 90
    assert _days_left(c.post("/api/tokens", json={"name": "b", "expires_days": 365})) == 365
    for bad in (366, 0, -1):
        r = c.post("/api/tokens", json={"name": "c", "expires_days": bad})
        assert r.status_code == 422, (bad, r.text)
    assert "365" in c.post("/api/tokens", json={"name": "d", "expires_days": 0}).text
    config_service.set("pat_max_days", "30")
    try:
        assert _days_left(c.post("/api/tokens", json={"name": "e"})) == 30
        config_service.set("pat_max_days", "0")
        assert _days_left(c.post("/api/tokens", json={"name": "f", "expires_days": 0})) is None
        assert _days_left(c.post("/api/tokens", json={"name": "g", "expires_days": 5000})) == 5000
    finally:
        config_service.delete("pat_max_days")


def test_an_admin_minted_pat_follows_the_same_rule():
    from web_dashboard.api import users
    c = _client(ROOT, users.router)
    r = c.post(f"/api/users/{ALICE.id}/tokens", json={"name": "x"})
    assert r.status_code == 201 and _days_left(r) == 90, r.text
    assert c.post(f"/api/users/{ALICE.id}/tokens",
                  json={"name": "y", "expires_days": 0}).status_code == 422


# ── cloud targets ────────────────────────────────────────────────────────────

def _deploy(workgroup, name, ip):
    db = SessionLocal()
    try:
        j = Job(id=str(uuid.uuid4()), job_type="ec2_deploy", status="completed",
                workgroup=workgroup, created_by="someone")
        j.metadata_dict = {"instance_id": f"i-{name}", "instance_name": name, "public_ip": ip}
        db.add(j)
        db.commit()
    finally:
        db.close()


_deploy("team-a", "vm-a", "198.51.100.1")
_deploy("team-b", "vm-b", "198.51.100.2")
_deploy(None, "vm-untagged", "198.51.100.3")


def _aws_targets(user):
    r = _client(user, config_mgmt.router).get("/api/config-mgmt/cloud-targets")
    assert r.status_code == 200, r.text
    return {t["name"] for t in r.json()["aws"]}


def test_cloud_targets_follow_workstreams():
    assert _aws_targets(ALICE) == {"vm-a"}
    assert _aws_targets(ROOT) >= {"vm-a", "vm-b", "vm-untagged"}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
