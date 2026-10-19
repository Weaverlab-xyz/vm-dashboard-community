"""A workstream's network defaults: the subnet / security groups its deploy forms start from.

What these pin:

- the shape is an allowlist per cloud, kept per region (a subnet exists in one region),
  checked on write and filtered on read; an empty value clears that region only;
- a member (or the creator, or an administrator) sets and reads them — they only pre-fill
  a form — anyone else is refused, and every write is audited;
- each deploy form fills its network fields from them after its region's options load,
  only where the operator has not picked, and can save its current network as one.

Uses a real temp SQLite database. Runs standalone:
    python tests/test_workstream_network_defaults.py
"""
import os
import re
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="network-defaults-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "network-defaults-test-secret")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web_dashboard.api import auth  # noqa: E402
from web_dashboard.api import workgroups as api  # noqa: E402
from web_dashboard.api.auth import full_access_map  # noqa: E402
from web_dashboard.database import (AuditLog, Base, SessionLocal, User,  # noqa: E402
                                    Workgroup, engine)
from web_dashboard.services import workgroup_service as ws  # noqa: E402
from web_dashboard.services.workgroup_service import WorkgroupError  # noqa: E402

Base.metadata.create_all(bind=engine)


def _person(name, workgroups=(), admin=False):
    db = SessionLocal()
    try:
        u = User(id=str(uuid.uuid4()), username=name, hashed_password="x", is_admin=admin)
        u.workgroups_list = list(workgroups)
        u.permissions_dict = full_access_map()
        db.add(u)
        db.commit()
        db.refresh(u)
        db.expunge(u)
        return u
    finally:
        db.close()


ALICE = _person("alice", ["team-a"])
BOB = _person("bob", ["team-b"])
ROOT = _person("root", admin=True)
_db = SessionLocal()
for _n in ("team-a", "team-b", "team-c"):
    _db.add(Workgroup(name=_n, display_name=_n))
_db.commit()
_db.close()


def _client(user):
    app = FastAPI()
    app.include_router(api.router)

    def _me():
        db = SessionLocal()
        try:
            return db.query(User).filter(User.id == user.id).first()
        finally:
            db.close()
    app.dependency_overrides[auth.get_current_user] = _me
    return TestClient(app)


def _set(name, cloud, region, values):
    db = SessionLocal()
    try:
        return ws.set_network_defaults(db, name, cloud, region, values)
    finally:
        db.close()


SUB = "subnet-0abc"
SG = ["sg-1", "sg-2"]


def test_the_shape_is_checked_per_cloud_and_region():
    for cloud, region, bad in (("vmware", "x", {}), ("aws", "US EAST", {"subnet_id": SUB}),
                               ("aws", "us-east-1", {"nsg_ids": ["x"]}),
                               ("aws", "us-east-1", {"security_group_ids": "sg-1"}),
                               ("aws", "us-east-1", {"subnet_id": "x" * 600}),
                               ("gcp", "us-central1", {"subnetwork": ["a"]})):
        try:
            _set("team-c", cloud, region, bad)
            raise AssertionError(f"{cloud}/{region} {bad} should be refused")
        except WorkgroupError:
            pass
    out = _set("team-c", "aws", "us-east-1", {"subnet_id": f" {SUB} ", "security_group_ids": SG})
    assert out == {"aws": {"us-east-1": {"subnet_id": SUB, "security_group_ids": SG}}}
    _set("team-c", "aws", "eu-west-1", {"subnet_id": "subnet-eu"})
    # Clearing one region leaves the other.
    out = _set("team-c", "aws", "us-east-1", None)
    assert out == {"aws": {"eu-west-1": {"subnet_id": "subnet-eu"}}}
    _set("team-c", "aws", "eu-west-1", {})
    db = SessionLocal()
    try:
        assert ws.get(db, "team-c").network_defaults is None, "nothing set is stored as NULL"
        ws.get(db, "team-c").network_defaults = (
            '{"aws": {"us-east-1": {"subnet_id": "ok", "evil": "x"}, "BAD REGION": '
            '{"subnet_id": "y"}}, "vmware": {"r": {"x": 1}}}')
        db.commit()
        assert ws.network_defaults("team-c", db) == {"aws": {"us-east-1": {"subnet_id": "ok"}}}
        ws.get(db, "team-c").network_defaults = None
        db.commit()
    finally:
        db.close()


def test_a_member_sets_them_an_outsider_does_not_and_it_is_audited():
    body = {"cloud": "azure", "region": "eastus", "values": {"subnet_id": "/subs/x/sn"}}
    assert _client(BOB).put("/api/workgroups/team-a/network-defaults", json=body).status_code == 403
    assert _client(BOB).get("/api/workgroups/team-a/network-defaults").status_code == 403
    db = SessionLocal()
    try:
        before = db.query(AuditLog).filter(
            AuditLog.action == "workstream_network_defaults_update").count()
    finally:
        db.close()
    r = _client(ALICE).put("/api/workgroups/team-a/network-defaults", json=body)
    assert r.status_code == 200, r.text
    assert r.json() == {"azure": {"eastus": {"subnet_id": "/subs/x/sn"}}}
    assert _client(ALICE).get("/api/workgroups/team-a/network-defaults").json() == r.json()
    bad = {**body, "values": {"nsg_ids": "not-a-list"}}
    assert _client(ALICE).put("/api/workgroups/team-a/network-defaults", json=bad).status_code == 400
    db = SessionLocal()
    try:
        assert db.query(AuditLog).filter(
            AuditLog.action == "workstream_network_defaults_update").count() == before + 1
    finally:
        db.close()
    _set("team-a", "azure", "eastus", None)


def _src(*parts):
    with open(os.path.join(_ROOT, "web_dashboard", *parts), encoding="utf-8") as fh:
        return fh.read()


def test_each_form_fills_after_its_region_loads_and_can_save():
    pages = {
        "aws": ("async loadNetworkOptsFor(", "subnet_id"),
        "azure": ("async onDeployLocationChange(", "subnetId"),
        "gcp": ("async onZoneChange(", "subnetwork"),
    }
    for cloud, (handler, field) in pages.items():
        src = _src("templates", cloud, "index.html")
        i = src.index(handler)
        body = src[i:i + 1500]
        assert "this.applyNetworkDefaults()" in body, f"{cloud}: {handler} never fills"
        assert "saveNetworkDefault()" in src, f"{cloud}: no way to save a default"
    assert "saveNetworkDefault()" in _src("templates", "oci", "index.html")
    js = _src("static", "js", "app.js")
    assert "await this.loadNetworkDefaults(wg);" in js and "this.applyNetworkDefaults();" in js
    # Only an empty field, or one still holding what was filled automatically, changes.
    i = js.index("applyNetworkDefaults() {")
    assert "same(cur, this.wsNetFilled[field])" in js[i:i + 1200]


def test_aws_marks_its_own_default_as_automatic():
    src = _src("templates", "aws", "index.html")
    i = src.index("async loadNetworkOptsFor(")
    body = src[i:i + 2000]
    assert body.index("this.applyNetworkDefaults()") < body.index("o.default_subnet_id"), (
        "the workstream's network must come before the server's default fills the field")
    assert "markNetworkAutoFilled('subnet_id'" in body


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
