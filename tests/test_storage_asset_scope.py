"""Stored assets limited to one workstream.

What these pin:

- an untagged asset is everyone's; a tagged one is its workstream's members' and
  administrators', and every listing filters it — /storage/list-all, /list, /list/{b},
  /config-mgmt/assets and /playbooks;
- to anyone else it is MISSING: fetch, run, bulk run, a Compose deploy and delete all
  answer 404, and an upload of the same name is refused, since the scope would stay on
  the file and the team would run the uploader's version;
- only an administrator scopes, it is audited, an unknown workstream is a 400 and null
  makes the asset global again;
- the scope follows the file: move carries it, migrate copies it, delete drops it;
- a workstream's post-deploy runs can only use global assets or its own, and a
  workstream that still scopes assets cannot be deleted.

Uses a real temp SQLite database; the storage backends are stubbed. Runs standalone:
    python tests/test_storage_asset_scope.py
"""
import asyncio
import os
import re
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="asset-scope-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "asset-scope-test-secret")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web_dashboard.api import auth  # noqa: E402
from web_dashboard.api import config_mgmt as cm_api  # noqa: E402
from web_dashboard.api import storage as storage_api  # noqa: E402
from web_dashboard.api.auth import full_access_map  # noqa: E402
from web_dashboard.database import (AuditLog, Base, SessionLocal, User,  # noqa: E402
                                    Workgroup, engine)
from web_dashboard.services import asset_scope_service as scope  # noqa: E402
from web_dashboard.services import storage_service  # noqa: E402
from web_dashboard.services import workgroup_service as ws  # noqa: E402
from web_dashboard.services.workgroup_service import WorkgroupError  # noqa: E402

Base.metadata.create_all(bind=engine)


# ── A fake two-backend store ──────────────────────────────────────────────────

STORE = {}


def _reset_store():
    STORE.clear()
    STORE.update({
        "s3": {"base.yml": b"- hosts: all", "team-a.yml": b"- hosts: all", "agent.sh": b"#!/bin/sh"},
        "gcs": {"team-a.yml": b"- hosts: all"},
    })


def _items(backend):
    return [{"name": n, "type": "playbook" if n.endswith(".yml") else "script", "size": len(d)}
            for n, d in STORE[backend].items()]


async def _list_assets_in(backend):
    return _items(backend)


async def _list_all():
    return [{**i, "backend": b} for b in STORE for i in _items(b)]


async def _list_assets():
    return _items("s3")


async def _list_playbooks():
    return [i["name"] for i in _items("s3") if i["type"] == "playbook"]


async def _fetch_in(backend, name):
    if name not in STORE.get(backend, {}):
        raise storage_service.StorageError("missing")
    return STORE[backend][name]


async def _delete_in(backend, name):
    STORE[backend].pop(name, None)


async def _delete(name):
    STORE["s3"].pop(name, None)


async def _upload(name, data):
    STORE["s3"][name] = data


async def _upload_to(backend, name, data):
    STORE.setdefault(backend, {})[name] = data


async def _move(name, src, dst):
    STORE[dst][name] = STORE[src].pop(name)


storage_service.active_backend = lambda: "s3"
storage_service.list_assets_in = _list_assets_in
storage_service.list_all_assets = _list_all
storage_service.list_assets = _list_assets
storage_service.list_playbooks = _list_playbooks
storage_service.fetch_asset_in = _fetch_in
storage_service.delete_asset_in = _delete_in
storage_service.delete_asset = _delete
storage_service.upload_asset = _upload
storage_service.upload_asset_to = _upload_to
storage_service.move_asset = _move
_reset_store()


# ── People ────────────────────────────────────────────────────────────────────

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
for _n in ("team-a", "team-b", "team-gone"):
    _db.add(Workgroup(name=_n, display_name=_n))
_db.commit()
_db.close()


def _client(user):
    app = FastAPI()
    app.include_router(storage_api.router)
    app.include_router(cm_api.router)

    def _me():
        db = SessionLocal()
        try:
            return db.query(User).filter(User.id == user.id).first()
        finally:
            db.close()
    app.dependency_overrides[auth.get_current_user] = _me
    return TestClient(app)


def _scope(backend, name, wg):
    db = SessionLocal()
    try:
        return scope.set_scope(db, backend, name, wg)
    finally:
        db.close()


def _scope_of(backend, name):
    db = SessionLocal()
    try:
        return scope.scope_of(db, backend, name)
    finally:
        db.close()


class _Tagged:
    """team-a.yml on s3 scoped to team-a for the duration of a test."""

    def __enter__(self):
        _reset_store()
        _scope("s3", "team-a.yml", "team-a")

    def __exit__(self, *exc):
        db = SessionLocal()
        try:
            from web_dashboard.database import StorageAssetScope
            db.query(StorageAssetScope).delete()
            db.commit()
        finally:
            db.close()
        _reset_store()


def _names(items, backend="s3"):
    return {i["name"] for i in items if i.get("backend", backend) == backend}


# ── Seeing it ─────────────────────────────────────────────────────────────────

def test_untagged_is_everyones_and_tagged_is_its_workstreams():
    with _Tagged():
        for user, sees in ((ALICE, True), (ROOT, True), (BOB, False)):
            c = _client(user)
            all_items = c.get("/api/storage/list-all").json()["items"]
            assert ("team-a.yml" in _names(all_items)) is sees, user.username
            assert "base.yml" in _names(all_items)
            # The gcs copy was never scoped: scope is per backend + name.
            assert "team-a.yml" in _names(all_items, "gcs")
            assert ("team-a.yml" in _names(c.get("/api/storage/list").json()["items"])) is sees
            assert ("team-a.yml" in _names(
                c.get("/api/storage/list/s3").json()["items"])) is sees
            assert ("team-a.yml" in _names(c.get("/api/config-mgmt/assets").json())) is sees
            assert ("team-a.yml" in c.get("/api/config-mgmt/playbooks").json()) is sees
        tagged = [i for i in _client(ALICE).get("/api/storage/list-all").json()["items"]
                  if i["name"] == "team-a.yml" and i["backend"] == "s3"][0]
        assert tagged["workgroup"] == "team-a", "the page shows whose it is"


# ── Using it ──────────────────────────────────────────────────────────────────

def test_an_outsider_finds_it_missing_everywhere():
    with _Tagged():
        bob, alice = _client(BOB), _client(ALICE)
        assert bob.get("/api/storage/fetch/s3/team-a.yml").status_code == 404
        assert alice.get("/api/storage/fetch/s3/team-a.yml").status_code == 200
        run = {"asset": "team-a.yml", "target": "no-such-group"}
        assert bob.post("/api/config-mgmt/run", json=run).status_code == 404
        # A blank backend is the active one, where the scope is.
        assert bob.post("/api/config-mgmt/run",
                        json={**run, "asset_backend": ""}).status_code == 404
        r = alice.post("/api/config-mgmt/run", json=run)
        assert r.status_code != 404, "a member's run gets past the scope (to the target check)"
        assert bob.post("/api/config-mgmt/run-bulk",
                        json={"asset": "team-a.yml", "inventory_ids": ["job:x"]}).status_code == 404
        assert bob.delete("/api/storage/asset-in/s3/team-a.yml").status_code == 404
        assert bob.delete("/api/storage/asset/team-a.yml").status_code == 404
        out = bob.post("/api/storage/bulk-delete",
                       json={"items": [{"backend": "s3", "name": "team-a.yml"}]}).json()
        assert out["failed"] and not out["deleted"]
        assert "team-a.yml" in STORE["s3"], "nothing an outsider did removed it"


def test_an_outsider_cannot_replace_it():
    import base64
    with _Tagged():
        body = {"filename": "team-a.yml", "content_b64": base64.b64encode(b"evil").decode()}
        assert _client(BOB).post("/api/storage/upload", json=body).status_code == 409
        assert _client(BOB).post("/api/config-mgmt/upload", json=body).status_code == 409
        assert STORE["s3"]["team-a.yml"] != b"evil"
        assert _client(BOB).post("/api/storage/upload/begin",
                                 json={"filename": "team-a.yml", "size": 10}).status_code == 409
        assert _client(ALICE).post("/api/storage/upload", json=body).status_code == 201


def test_a_compose_deploy_checks_it():
    src = _src("api", "containers.py")
    i = src.index("def deploy_compose(")
    window = src[i:src.index("job_service.create_job(", i)]
    assert "asset_scope_service.check(db, current_user, req.compose_backend, req.compose_file)" in window


# ── Setting it ────────────────────────────────────────────────────────────────

def _audited():
    db = SessionLocal()
    try:
        return db.query(AuditLog).filter(AuditLog.action == "storage_asset_scope_update").count()
    finally:
        db.close()


def test_only_an_admin_scopes_and_it_is_audited():
    body = {"backend": "s3", "name": "base.yml", "workgroup": "team-a"}
    assert _client(ALICE).put("/api/storage/scope", json=body).status_code == 403
    assert _scope_of("s3", "base.yml") is None
    before = _audited()
    r = _client(ROOT).put("/api/storage/scope", json={**body, "workgroup": "TEAM-A"})
    assert r.status_code == 200 and r.json()["workgroup"] == "team-a", r.text
    assert _scope_of("s3", "base.yml") == "team-a"
    assert _client(ROOT).put("/api/storage/scope",
                             json={**body, "workgroup": "nope"}).status_code == 400
    assert _client(ROOT).put("/api/storage/scope",
                             json={**body, "name": "../x.yml"}).status_code == 400
    assert _client(ROOT).put("/api/storage/scope",
                             json={**body, "workgroup": None}).status_code == 200
    assert _scope_of("s3", "base.yml") is None
    assert _audited() == before + 2


def test_the_scope_follows_the_file():
    with _Tagged():
        root = _client(ROOT)
        assert root.post("/api/storage/move", json={
            "name": "team-a.yml", "from_backend": "s3", "to_backend": "gcs"}).status_code == 200
        assert _scope_of("s3", "team-a.yml") is None and _scope_of("gcs", "team-a.yml") == "team-a"
        root.post("/api/storage/migrate", json={"source": "gcs", "target": "s3"})
        assert _scope_of("s3", "team-a.yml") == "team-a", "a migrated copy must not go global"
        assert _client(ALICE).delete("/api/storage/asset-in/gcs/team-a.yml").status_code == 200
        assert _scope_of("gcs", "team-a.yml") is None


# ── Workstreams ───────────────────────────────────────────────────────────────

def test_post_deploy_runs_only_use_global_or_own_assets():
    with _Tagged():
        db = SessionLocal()
        try:
            try:
                ws.set_post_deploy_runs(db, "team-b", [{"asset": "team-a.yml"}])
                raise AssertionError("team-b must not run team-a's asset")
            except WorkgroupError as e:
                assert "scoped to workstream 'team-a'" in str(e)
            assert ws.set_post_deploy_runs(db, "team-a", [{"asset": "team-a.yml"},
                                                          {"asset": "base.yml"}])
            assert ws.set_post_deploy_runs(db, "team-b", [{"asset": "team-a.yml",
                                                           "asset_backend": "gcs"}])
            ws.set_post_deploy_runs(db, "team-a", [])
            ws.set_post_deploy_runs(db, "team-b", [])
        finally:
            db.close()


def test_a_workstream_that_scopes_assets_cannot_be_deleted():
    _scope("s3", "agent.sh", "team-gone")
    db = SessionLocal()
    try:
        try:
            ws.delete(db, "team-gone")
            raise AssertionError("should refuse")
        except WorkgroupError as e:
            assert "stored asset" in str(e)
    finally:
        db.close()
    _scope("s3", "agent.sh", None)
    db = SessionLocal()
    try:
        ws.delete(db, "team-gone")
    finally:
        db.close()


# ── Every route ───────────────────────────────────────────────────────────────

def _src(*parts):
    with open(os.path.join(_ROOT, "web_dashboard", *parts), encoding="utf-8") as fh:
        return fh.read()


def _route_bodies(src):
    starts = [m.start() for m in re.finditer(r"^@router\.", src, re.M)] + [len(src)]
    return [(re.match(r'@router\.\w+\("([^"]*)"', src[a:]).group(1), src[a:b])
            for a, b in zip(starts, starts[1:])]


def test_every_route_that_names_an_asset_applies_the_scope():
    need = {
        "storage.py": {"/list", "/list/{backend}", "/list-all", "/fetch/{backend}/{name:path}",
                       "/asset/{name:path}", "/asset-in/{backend}/{name:path}", "/bulk-delete",
                       "/upload", "/upload/begin", "/move", "/migrate"},
        "config_mgmt.py": {"/assets", "/playbooks", "/upload", "/run", "/run-bulk"},
    }
    for fname, paths in need.items():
        bodies = dict(_route_bodies(_src("api", fname)))
        for path in paths:
            assert path in bodies, f"{fname}: route {path} moved — update this test"
            assert ("asset_scope_service" in bodies[path] or "_scope_check(" in bodies[path]
                    or "_replace_check(" in bodies[path]), (
                f"{fname} {path} names an asset without consulting its workstream scope")


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
