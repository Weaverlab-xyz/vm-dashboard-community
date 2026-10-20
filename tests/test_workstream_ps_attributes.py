"""Workstream -> Password Safe attributes.

What these pin:

- a workstream's attribute mapping round-trips, is stored as the tenant spells it, and is
  checked against the tenant's vocabulary on save: an unknown type or value and a
  read-only type are refused BY NAME, and nothing is created in the tenant unless
  ``create_missing`` asked for it (and then only on a system the workstream onboarded);
- ``ps_workstream_attrs.apply`` assigns each attribute, swaps the old workstream's values
  for the new one's on a reassign, and reports a partial failure per attribute — a skip
  is never counted as success;
- the onboarding hook calls ``apply`` only when the workstream names attributes, and never
  turns an attribute failure into an onboarding failure;
- the admin routes: GET/PUT are admin-only and audited, a bad entry is a 400.

Password Safe is mocked throughout. Uses a real temp SQLite database. Runs standalone:
    python tests/test_workstream_ps_attributes.py
"""
import asyncio
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="ws-ps-attrs-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "ws-ps-attrs-test-secret")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web_dashboard.api import auth  # noqa: E402
from web_dashboard.api import workgroups as api  # noqa: E402
from web_dashboard.api.auth import full_access_map  # noqa: E402
from web_dashboard.database import (AuditLog, Base, Job, SessionLocal, User,  # noqa: E402
                                    Workgroup, engine)
from web_dashboard.services import ps_api_service as psa  # noqa: E402
from web_dashboard.services import ps_vm_hook  # noqa: E402
from web_dashboard.services import ps_workstream_attrs as pwa  # noqa: E402
from web_dashboard.services import workgroup_service as ws  # noqa: E402
from web_dashboard.services.workgroup_service import WorkgroupError  # noqa: E402

Base.metadata.create_all(bind=engine)

_db = SessionLocal()
for _n in ("team-a", "team-b", "team-c", "team-d"):
    _db.add(Workgroup(name=_n, display_name=_n))
_db.commit()
_db.close()

RAW_VOCAB = {
    "state": psa.PROBE_OK, "detail": "",
    "types": [{"AttributeTypeID": 1, "Name": "Workstream"},
              {"AttributeTypeID": 2, "Name": "Criticality", "IsReadOnly": True},
              {"AttributeTypeID": 3, "Name": "Business Unit"}],
    "values_by_type": {
        "1": [{"AttributeID": 101, "ShortName": "team-a"},
              {"AttributeID": 102, "ShortName": "team-b"}],
        "2": [{"AttributeID": 201, "ShortName": "High"}],
        "3": [{"AttributeID": 301, "ShortName": "Finance"},
              {"AttributeID": 302, "ShortName": "Sales"}],
    },
}


class FakePS:
    """Records every Password Safe write; ``fail`` is a set of attribute ids to refuse."""

    def __init__(self, fail=()):
        self.calls = []
        self.ensured = []
        self.fail = set(fail)
        self.vocab_reads = 0

    async def vocab(self, tenant=None):
        self.vocab_reads += 1
        return RAW_VOCAB

    async def set_attr(self, system_id, attribute_id, *, assign, tenant=None):
        self.calls.append((str(system_id), str(attribute_id), assign))
        if str(attribute_id) in self.fail:
            raise psa.PSApiError("Password Safe refused the attribute change (500)")

    async def ensure(self, system_id, type_name, value, *, tenant=None):
        self.ensured.append((str(system_id), type_name, value))
        return {"created_type": type_name == "Region", "created_value": True}


class patched:
    """Point the PS calls at a FakePS, and make Password Safe look enabled."""

    def __init__(self, fake, ready=""):
        self.fake, self.ready = fake, ready

    def __enter__(self):
        self.saved = (psa.read_attribute_vocabulary, psa.set_managed_system_attribute,
                      psa.ensure_managed_system_attribute, pwa.unavailable_reason)
        psa.read_attribute_vocabulary = self.fake.vocab
        psa.set_managed_system_attribute = self.fake.set_attr
        psa.ensure_managed_system_attribute = self.fake.ensure
        pwa.unavailable_reason = lambda: self.ready
        return self.fake

    def __exit__(self, *exc):
        (psa.read_attribute_vocabulary, psa.set_managed_system_attribute,
         psa.ensure_managed_system_attribute, pwa.unavailable_reason) = self.saved


def _run(coro):
    return asyncio.run(coro)


def _set(name, mapping, **kw):
    db = SessionLocal()
    try:
        return _run(ws.set_ps_attributes(db, name, mapping, **kw))
    finally:
        db.close()


def _apply(workgroup, sid, previous=None):
    db = SessionLocal()
    try:
        return _run(pwa.apply(db, workgroup, sid, previous_workgroup=previous))
    finally:
        db.close()


def _refused(fn, *needles):
    try:
        fn()
    except WorkgroupError as exc:
        for needle in needles:
            assert needle in str(exc), (needle, str(exc))
        return exc
    raise AssertionError("expected a WorkgroupError")


# ── the mapping ───────────────────────────────────────────────────────────────

def test_set_and_get_round_trip_in_the_tenants_spelling():
    with patched(FakePS()) as fake:
        out = _set("team-a", {"workstream": "TEAM-A", "Business Unit": "Finance"})
    assert out == {"attributes": {"Workstream": "team-a", "Business Unit": "Finance"},
                   "created": []}
    assert ws.ps_attributes("team-a") == {"Workstream": "team-a", "Business Unit": "Finance"}
    assert ws.ps_attributes("TEAM-A") == ws.ps_attributes("team-a")
    assert fake.ensured == [] and fake.calls == []   # saving writes nothing to systems
    with patched(FakePS()):
        assert _set("team-a", {}) == {"attributes": {}, "created": []}
    assert ws.ps_attributes("team-a") == {}
    assert ws.ps_attributes("no-such-team") == {}


def test_clearing_needs_no_password_safe():
    # Off/unconfigured must not stop an administrator removing the mapping.
    with patched(FakePS(), ready="Password Safe is not enabled on this instance") as fake:
        assert _set("team-c", {})["attributes"] == {}
    assert fake.vocab_reads == 0


def test_unknown_type_and_value_are_refused_by_name():
    with patched(FakePS()) as fake:
        _refused(lambda: _set("team-a", {"Region": "emea"}), "no attribute type 'Region'")
        _refused(lambda: _set("team-a", {"Workstream": "team-z"}),
                 "'Workstream' has no value 'team-z'", "create missing")
        _refused(lambda: _set("team-a", {"Criticality": "High"}), "read-only")
        _refused(lambda: _set("team-a", {"Workstream": "a", "workstream": "b"}), "named twice")
        _refused(lambda: _set("team-a", {"Workstream": "  "}), "empty")
        _refused(lambda: _set("team-a", {"Work\x1bstream": "x"}), "not a Password Safe")
        _refused(lambda: _set("nope", {"Workstream": "team-a"}), "not found")
    assert fake.ensured == []
    assert ws.ps_attributes("team-a") == {}, "a refused save stored nothing"


def test_password_safe_off_is_its_own_error():
    with patched(FakePS(), ready="Password Safe has no API credentials"):
        exc = _refused(lambda: _set("team-a", {"Workstream": "team-a"}), "no API credentials")
    assert isinstance(exc, ws.PSAttributesUnavailable) and exc.status == 409


def test_create_missing_needs_an_onboarded_system():
    with patched(FakePS()) as fake:
        _refused(lambda: _set("team-c", {"Region": "emea"}, create_missing=True),
                 "Region = emea", "nothing in workstream 'team-c' is onboarded")
        # A read-only type is refused even when creating.
        _refused(lambda: _set("team-c", {"Criticality": "Low"}, create_missing=True),
                 "read-only")
    assert fake.ensured == []


def test_create_missing_creates_on_the_workstreams_oldest_system():
    db = SessionLocal()
    try:
        db.add(Job(id=str(uuid.uuid4()), job_type="ec2_deploy", workgroup="team-d",
                   status="completed", extra_data='{"ps_managed_system_id": 77}'))
        db.add(Job(id=str(uuid.uuid4()), job_type="ec2_deploy", workgroup="team-d",
                   status="completed",
                   extra_data='{"ps_managed_system_id": 66, "destroyed": true}'))
        db.commit()
    finally:
        db.close()
    with patched(FakePS()) as fake:
        out = _set("team-d", {"Region": "emea", "Workstream": "team-b"}, create_missing=True)
    assert fake.ensured == [("77", "Region", "emea")]
    assert out["attributes"] == {"Workstream": "team-b", "Region": "emea"}
    assert out["created"][0]["created_type"] is True
    assert out["created"][0]["managed_system_id"] == "77"


# ── apply ─────────────────────────────────────────────────────────────────────

def test_apply_assigns_each_attribute():
    with patched(FakePS()):
        _set("team-a", {"Workstream": "team-a", "Business Unit": "Finance"})
    with patched(FakePS()) as fake:
        r = _apply("team-a", 500)
    assert r["state"] == pwa.STATE_OK
    assert sorted(fake.calls) == [("500", "101", True), ("500", "301", True)]
    assert [o["status"] for o in r["outcomes"]] == ["ok", "ok"]


def test_apply_with_no_attributes_touches_nothing():
    with patched(FakePS()) as fake:
        r = _apply("team-c", 500)
    assert r == {"state": pwa.STATE_NONE, "managed_system_id": "500", "outcomes": []}
    assert fake.calls == [] and fake.vocab_reads == 0


def test_reassign_swaps_old_values_for_new():
    with patched(FakePS()):
        _set("team-a", {"Workstream": "team-a", "Business Unit": "Finance"})
        _set("team-b", {"Workstream": "team-b", "Business Unit": "Finance"})
    with patched(FakePS()) as fake:
        r = _apply("team-b", 9, previous="team-a")
    # team-a's Workstream value off, team-b's on; the shared Business Unit untouched.
    assert fake.calls == [("9", "101", False), ("9", "102", True)]
    by = {(o["action"], o["type"]): o for o in r["outcomes"]}
    assert by[("remove", "Workstream")]["status"] == "ok"
    assert by[("assign", "Workstream")]["status"] == "ok"
    assert by[("assign", "Business Unit")]["status"] == "skipped"
    assert r["state"] == pwa.STATE_PARTIAL, "a skip must not read as a clean success"


def test_reassign_to_a_workstream_with_none_only_removes():
    with patched(FakePS()):
        _set("team-a", {"Workstream": "team-a"})
    with patched(FakePS()) as fake:
        r = _apply("team-c", 9, previous="team-a")
    assert fake.calls == [("9", "101", False)]
    assert r["state"] == pwa.STATE_OK


def test_partial_failure_is_reported_per_attribute():
    with patched(FakePS()):
        _set("team-a", {"Workstream": "team-a", "Business Unit": "Finance"})
    with patched(FakePS(fail={"301"})) as fake:
        r = _apply("team-a", 500)
    assert len(fake.calls) == 2, "one refusal must not stop the next attribute"
    by = {o["type"]: o for o in r["outcomes"]}
    assert by["Workstream"]["status"] == "ok"
    assert by["Business Unit"]["status"] == "failed"
    assert "refused" in by["Business Unit"]["detail"]
    assert r["state"] == pwa.STATE_PARTIAL


def test_a_value_gone_from_the_tenant_fails_without_a_call():
    db = SessionLocal()
    try:
        ws.get(db, "team-c").ps_attributes = '{"Workstream": "retired"}'
        db.commit()
        with patched(FakePS()) as fake:
            r = _run(pwa.apply(db, "team-c", 5))
        ws.get(db, "team-c").ps_attributes = None
        db.commit()
    finally:
        db.close()
    assert fake.calls == []
    assert r["outcomes"][0]["status"] == "failed"
    assert "no value 'retired'" in r["outcomes"][0]["detail"]
    assert r["state"] == pwa.STATE_FAILED


def test_password_safe_off_skips_and_never_succeeds():
    with patched(FakePS()):
        _set("team-a", {"Workstream": "team-a"})
    with patched(FakePS(), ready="Password Safe is not enabled on this instance") as fake:
        r = _apply("team-a", 500)
    assert fake.calls == []
    assert r["state"] == pwa.STATE_SKIPPED
    assert r["outcomes"][0]["status"] == "skipped"


def test_vocabulary_failure_fails_every_attribute():
    with patched(FakePS()):
        _set("team-a", {"Workstream": "team-a"})
    fake = FakePS()

    async def broken(tenant=None):
        return {"state": psa.PROBE_ERROR, "detail": "could not sign in to Password Safe",
                "types": [], "values_by_type": {}}
    fake.vocab = broken
    with patched(fake):
        r = _apply("team-a", 500)
    assert r["state"] == pwa.STATE_FAILED
    assert r["outcomes"][0]["detail"] == "could not sign in to Password Safe"


# ── the onboarding hook ───────────────────────────────────────────────────────

def _deploy_job(workgroup):
    db = SessionLocal()
    try:
        job = Job(id=str(uuid.uuid4()), job_type="ec2_deploy", workgroup=workgroup,
                  status="running")
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def _hook(job_id, result):
    calls = []
    real = pwa.apply

    async def spy(db, workgroup, sid, previous_workgroup=None, **kw):
        calls.append((workgroup, sid))
        return {"state": pwa.STATE_OK, "managed_system_id": str(sid),
                "outcomes": [{"action": "assign", "type": "Workstream", "value": "x",
                              "status": "ok", "detail": ""}]}
    pwa.apply = spy
    db = SessionLocal()
    try:
        _run(ps_vm_hook.apply_workstream_attributes(db, job_id, result))
        job = db.query(Job).filter(Job.id == job_id).first()
        return calls, job.progress_message
    finally:
        db.close()
        pwa.apply = real


def test_onboarding_applies_only_when_the_workstream_names_attributes():
    with patched(FakePS()):
        _set("team-b", {"Workstream": "team-b"})

    result = {"ps_managed_system_id": 42}
    calls, progress = _hook(_deploy_job("team-b"), result)
    assert calls == [("team-b", 42)]
    assert "1 applied, 0 skipped, 0 failed" in progress
    assert result["ps_workstream_attributes"]["state"] == "ok"

    result = {"ps_managed_system_id": 42}
    calls, _ = _hook(_deploy_job("team-c"), result)
    assert calls == [] and "ps_workstream_attributes" not in result

    # Not onboarded, or onboarding failed: nothing to stamp.
    for result in ({}, {"ps_managed_system_id": 42, "ps_error": "boom"}):
        calls, _ = _hook(_deploy_job("team-b"), result)
        assert calls == []


def test_an_attribute_failure_never_becomes_an_onboarding_failure():
    with patched(FakePS()):
        _set("team-b", {"Workstream": "team-b"})
    real = pwa.apply

    async def boom(*a, **kw):
        raise RuntimeError("kaboom")
    pwa.apply = boom
    result = {"ps_managed_system_id": 42}
    db = SessionLocal()
    try:
        _run(ps_vm_hook.apply_workstream_attributes(db, _deploy_job("team-b"), result))
    finally:
        db.close()
        pwa.apply = real
    assert "ps_error" not in result
    assert result["ps_workstream_attributes"]["state"] == "failed"


def test_every_onboarding_path_calls_the_hook():
    import inspect
    src = inspect.getsource(ps_vm_hook)
    for fn in (ps_vm_hook.register, ps_vm_hook.register_password_managed):
        assert "apply_workstream_attributes(db, job_id, result)" in inspect.getsource(fn), fn
    # register_windows reaches it through register_password_managed.
    assert "register_password_managed(" in inspect.getsource(ps_vm_hook.register_windows)
    assert src.count("apply_workstream_attributes(db, job_id, result)") == 2


# ── the routes ────────────────────────────────────────────────────────────────

def _person(name, admin=False):
    db = SessionLocal()
    try:
        u = User(id=str(uuid.uuid4()), username=name, hashed_password="x", is_admin=admin)
        u.workgroups_list = ["team-a"]
        u.permissions_dict = full_access_map()
        db.add(u)
        db.commit()
        db.refresh(u)
        db.expunge(u)
        return u
    finally:
        db.close()


ROOT = _person("ws-ps-root", admin=True)
ALICE = _person("ws-ps-alice")


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


def test_routes_are_admin_only_and_audited():
    root, alice = _client(ROOT), _client(ALICE)
    url = "/api/workgroups/team-a/ps-attributes"
    with patched(FakePS()):
        # A member of the workstream still may not read or write these.
        assert alice.get(url).status_code == 403
        assert alice.put(url, json={"attributes": {"Workstream": "team-a"}}).status_code == 403
        r = root.put(url, json={"attributes": {"Workstream": "team-a"}})
        assert r.status_code == 200, r.text
        assert r.json()["attributes"] == {"Workstream": "team-a"}
        assert root.get(url).json() == {"attributes": {"Workstream": "team-a"}}
        bad = root.put(url, json={"attributes": {"Workstream": "team-z"}})
        assert bad.status_code == 400 and "team-z" in bad.json()["detail"]
        assert root.get("/api/workgroups/nope/ps-attributes").status_code == 404
    with patched(FakePS(), ready="Password Safe has no API credentials"):
        assert root.put(url, json={"attributes": {"Workstream": "team-a"}}).status_code == 409
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.action == "workstream_ps_attributes_update").all()
    finally:
        db.close()
    assert len(rows) == 1


def test_apply_route_refuses_a_workstream_without_attributes():
    root = _client(ROOT)
    with patched(FakePS()):
        r = root.post("/api/workgroups/team-c/ps-attributes/apply", json={})
    assert r.status_code == 400 and "names no Password Safe attributes" in r.json()["detail"]
    assert _client(ALICE).post("/api/workgroups/team-a/ps-attributes/apply",
                               json={}).status_code == 403


def test_backfill_applies_to_matched_systems_and_pages():
    from web_dashboard.services import inventory_service
    real_collect, real_read = inventory_service.collect, psa.read_attribute_inventory
    rows = [{"name": f"vm{i}", "workgroup": "team-a", "kind": "vm",
             "ps_system_id": str(1000 + i), "ips": []} for i in range(52)]
    rows.append({"name": "elsewhere", "workgroup": "team-b", "kind": "vm",
                 "ps_system_id": "9999", "ips": []})
    rows.append({"name": "unknown", "workgroup": "team-a", "kind": "vm",
                 "ps_system_id": "", "ips": []})

    async def read(**kw):
        systems = [{"ManagedSystemID": 1000 + i, "HostName": f"vm{i}"} for i in range(52)]
        systems.append({"ManagedSystemID": 9999, "HostName": "elsewhere"})
        return {"reachable": True, "assets": {"rows": []},
                "managed_systems": {"rows": systems}}

    inventory_service.collect = lambda db: rows
    psa.read_attribute_inventory = read
    try:
        with patched(FakePS()):
            _set("team-a", {"Workstream": "team-a"})
        db = SessionLocal()
        try:
            with patched(FakePS()) as fake:
                first = _run(pwa.backfill(db, "team-a"))
                second = _run(pwa.backfill(db, "team-a", offset=first["next_offset"]))
        finally:
            db.close()
    finally:
        inventory_service.collect, psa.read_attribute_inventory = real_collect, real_read
    assert first["matched"] == 52 and len(first["targets"]) == pwa.BACKFILL_MAX
    assert first["next_offset"] == 50 and first["unmatched"] == 1
    assert len(second["targets"]) == 2 and second["next_offset"] is None
    written = {c[0] for c in fake.calls}
    assert "9999" not in written, "another workstream's system was written"
    assert len(written) == 52
    assert fake.vocab_reads == 2, "one vocabulary read per call, not per system"


if __name__ == "__main__":
    import inspect as _inspect
    failed = 0
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and _inspect.isfunction(_fn):
            try:
                _fn()
                print("PASS", _name)
            except Exception as _exc:  # noqa: BLE001
                failed += 1
                print("FAIL", _name, repr(_exc))
    sys.exit(1 if failed else 0)
