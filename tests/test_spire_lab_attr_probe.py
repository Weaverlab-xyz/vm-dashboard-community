"""The SPIRE lab's attribute probe — what unblocks L3.

docs/runbooks/spire-lab-standup.md §5; docs/design/dashboard-workload-identity.md, L3. The
SPIFFE plugin takes its configuration from BeyondInsight attributes, and whether the
gateway populates them for a plugin action has never been observed. Pinned here:

  * **Prepare** (``ps_api_service.ensure_managed_system_attribute``, against a fake
    Password Safe over httpx's MockTransport): finds an existing type and value and only
    assigns; creates a missing type, then its value, in that order, and assigns the new
    id; refuses a read-only type before any write; a refused create stops before any
    assignment and keeps every call's status; a POST that answers 200 but does not read
    back is a failure; both list shapes Password Safe returns are read.
  * **The job**: refused on a lab that is not governed, not available, or already being
    prepared; stores the setup and audits the write; a failure stores the calls and names
    the console remedy; it never touches the functional account.
  * **The answer**: only the runbook's three outcomes and the side observations' three
    values; the pasted line is capped; who and when are recorded; the view carries the
    runbook row; the latest answer across visible labs wins.
  * ``onboarding_gaps`` drops each step once it is done; the routes, the worker and the
    row payload are wired.

Run: python tests/test_spire_lab_attr_probe.py   (or under pytest)
"""
import asyncio
import json
import os
import sys
import tempfile
import types
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="spire-attr-probe-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-spire-attr-probe")

try:
    import httpx
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover -- app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

# First-party imports UNGUARDED: a broken module must fail this file, not skip it.
from web_dashboard.database import AuditLog, Base, Job, SessionLocal, SpireLab, engine  # noqa: E402
from web_dashboard.services import job_service, ps_api_service  # noqa: E402
from web_dashboard.services import spire_lab_service as svc  # noqa: E402

Base.metadata.create_all(bind=engine)


# ── a fake Password Safe ──────────────────────────────────────────────────────

class FakePS:
    """The tenant: attribute types, their values, and each managed system's attributes.
    ``refuse`` maps (METHOD, path) to a status; ``envelope`` serves lists the
    ``{"TotalCount", "Data"}`` way; ``drop_assign`` answers 200 to an assignment and keeps
    nothing (the case the read-back exists for)."""

    def __init__(self, types=None, values=None, *, refuse=None, envelope=False,
                 drop_assign=False):
        self.types = list(types or [])
        self.values = {k: list(v) for k, v in (values or {}).items()}
        self.assigned = {}
        self.refuse = refuse or {}
        self.envelope = envelope
        self.drop_assign = drop_assign
        self.calls = []
        self.next_id = 500

    def _list(self, rows):
        return httpx.Response(200, json={"TotalCount": len(rows), "Data": rows}
                              if self.envelope else rows)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/v3/", 1)[-1] if "/v3/" in request.url.path \
            else request.url.path.lstrip("/")
        method = request.method
        self.calls.append((method, path))
        if (method, path) in self.refuse:
            return httpx.Response(self.refuse[(method, path)], text="no")
        if path == "Auth/Connect/Token":
            return httpx.Response(200, json={"access_token": "t"})
        if path in ("Auth/SignAppIn", "Auth/Signout"):
            return httpx.Response(200, json={})
        parts = path.split("/")
        if path == "AttributeTypes" and method == "GET":
            return self._list(self.types)
        if path == "AttributeTypes" and method == "POST":
            self.next_id += 1
            row = {"AttributeTypeID": self.next_id, "Name": json.loads(request.content)["Name"]}
            self.types.append(row)
            return httpx.Response(201, json=row)
        if parts[0] == "AttributeTypes" and parts[2:] == ["Attributes"]:
            if method == "GET":
                return self._list(self.values.get(parts[1], []))
            body = json.loads(request.content)
            self.next_id += 1
            row = {"AttributeID": self.next_id, "AttributeTypeID": int(parts[1]),
                   "ShortName": body["ShortName"], "LongName": body.get("LongName")}
            self.values.setdefault(parts[1], []).append(row)
            return httpx.Response(201, json=row)
        if parts[0] == "ManagedSystems" and len(parts) == 4 and method == "POST":
            if not self.drop_assign:
                self.assigned.setdefault(parts[1], []).append(int(parts[3]))
            return httpx.Response(200, json={})
        if parts[0] == "ManagedSystems" and len(parts) == 3 and method == "GET":
            return self._list([{"AttributeID": a} for a in self.assigned.get(parts[1], [])])
        raise AssertionError(f"unexpected call {method} {path}")


def _use(fake):
    ps_api_service._client = lambda tenant=None: httpx.AsyncClient(
        base_url="https://ps.test/BeyondTrust/api/public/v3/",
        transport=httpx.MockTransport(fake))


_real_client = ps_api_service._client


def _ensure(fake, system_id=7, value="lab.probe.test"):
    _use(fake)
    try:
        return asyncio.run(ps_api_service.ensure_managed_system_attribute(
            system_id, svc.ATTR_PROBE_TYPE, value))
    finally:
        ps_api_service._client = _real_client


def _writes(fake):
    return [c for c in fake.calls if c[0] in ("POST", "DELETE") and not c[1].startswith("Auth/")]


# ── Prepare, against the fake tenant ──────────────────────────────────────────

def test_an_existing_type_and_value_are_only_assigned():
    fake = FakePS(types=[{"AttributeTypeID": 9, "Name": "SpiffeTrustDomain"}],
                  values={"9": [{"AttributeID": 41, "ShortName": "Lab.Probe.Test"}]})
    out = _ensure(fake)
    assert _writes(fake) == [("POST", "ManagedSystems/7/Attributes/41")], fake.calls
    assert out["type_id"] == 9 and out["attribute_id"] == 41
    assert not out["created_type"] and not out["created_value"]
    assert out["assigned"] and out["read_back"]
    assert [c["status"] for c in out["calls"]] == [200, 200, 200, 200]


def test_a_missing_type_is_created_then_its_value_then_assigned():
    fake = FakePS(types=[{"AttributeTypeID": 1, "Name": "Geography"}])
    out = _ensure(fake)
    writes = _writes(fake)
    assert writes[0] == ("POST", "AttributeTypes")
    assert writes[1] == ("POST", f"AttributeTypes/{out['type_id']}/Attributes")
    assert writes[2] == ("POST", f"ManagedSystems/7/Attributes/{out['attribute_id']}")
    assert out["created_type"] and out["created_value"] and out["read_back"]
    assert fake.values[str(out["type_id"])][0]["ShortName"] == "lab.probe.test"


def test_a_read_only_type_is_refused_before_any_write():
    fake = FakePS(types=[{"AttributeTypeID": 9, "Name": "SpiffeTrustDomain",
                          "IsReadOnly": True}])
    try:
        _ensure(fake)
        raise AssertionError("a read-only type must be refused")
    except ps_api_service.AttributeProbeError as exc:
        assert "read-only" in str(exc)
    assert _writes(fake) == []


def test_a_refused_create_stops_before_any_assignment_and_keeps_the_calls():
    fake = FakePS(refuse={("POST", "AttributeTypes"): 403})
    try:
        _ensure(fake)
        raise AssertionError("a refused create must fail")
    except ps_api_service.AttributeProbeError as exc:
        assert exc.calls[-1] == {"method": "POST", "path": "AttributeTypes", "status": 403}
        assert "no" not in str(exc).split("with")[-1], "the body is never carried outward"
    assert not any(p.startswith("ManagedSystems") for _m, p in fake.calls)


def test_an_assignment_that_does_not_read_back_is_a_failure():
    fake = FakePS(types=[{"AttributeTypeID": 9, "Name": "SpiffeTrustDomain"}],
                  values={"9": [{"AttributeID": 41, "ShortName": "lab.probe.test"}]},
                  drop_assign=True)
    try:
        _ensure(fake)
        raise AssertionError("a POST that answers 200 is not proof")
    except ps_api_service.AttributeProbeError as exc:
        assert "read back" in str(exc)
        assert exc.calls[-1]["path"] == "ManagedSystems/7/Attributes"


def test_both_list_shapes_are_read():
    fake = FakePS(types=[{"AttributeTypeID": 9, "Name": "SpiffeTrustDomain"}],
                  values={"9": [{"AttributeID": 41, "ShortName": "lab.probe.test"}]},
                  envelope=True)
    out = _ensure(fake)
    assert out["attribute_id"] == 41 and out["read_back"]
    assert ("POST", "AttributeTypes") not in fake.calls, "the enveloped type was found"


def test_the_inputs_are_checked_before_signing_in():
    for sid, name, value in (("x", "T", "v"), (7, "", "v"), (7, "T", " ")):
        try:
            asyncio.run(ps_api_service.ensure_managed_system_attribute(sid, name, value))
            raise AssertionError((sid, name, value))
        except ps_api_service.PSApiError:
            pass


# ── the job ───────────────────────────────────────────────────────────────────

def _lab(**kw) -> SpireLab:
    base = dict(id=str(uuid.uuid4()), name="lab", trust_domain=f"{uuid.uuid4().hex[:6]}.probe.test",
                cloud="azure", bind_port=8081, status="available", vm_name="spire-01",
                private_ip="10.0.0.5", deployment_mode="vm", ps_system_id="7",
                admin_spiffe_id="spiffe://x/password-safe/admin", created_by="tester")
    base.update(kw)
    return SpireLab(**base)


def _save(row):
    db = SessionLocal()
    db.add(row)
    db.commit()
    db.refresh(row)
    return db, row


def _run_probe(db, row, fake, *, user="prober"):
    job = svc.start_attr_probe(db, lab_id=row.id, created_by=user)
    ws = types.ModuleType("web_dashboard.api.websocket")

    async def broadcast_progress(*a, **kw):
        pass
    ws.broadcast_progress = broadcast_progress
    prev = sys.modules.get("web_dashboard.api.websocket")
    sys.modules["web_dashboard.api.websocket"] = ws
    saved = (ps_api_service.configured, ps_api_service.get_functional_account)

    async def no_fa(*a, **kw):
        raise AssertionError("the probe must never touch the functional account")
    ps_api_service.configured = lambda: True
    ps_api_service.get_functional_account = no_fa
    _use(fake)
    try:
        asyncio.run(svc.run_attr_probe(db, lab_id=row.id, job_id=job["job_id"]))
    finally:
        ps_api_service.configured, ps_api_service.get_functional_account = saved
        ps_api_service._client = _real_client
        if prev is not None:
            sys.modules["web_dashboard.api.websocket"] = prev
    db.expire_all()
    return db.query(Job).filter(Job.id == job["job_id"]).one()


def test_prepare_is_refused_where_there_is_nothing_to_probe():
    for kw, why in ((dict(ps_system_id=None), "Govern first"),
                    (dict(status="building"), "not available")):
        db, row = _save(_lab(**kw))
        try:
            svc.start_attr_probe(db, lab_id=row.id, created_by="u")
            raise AssertionError(kw)
        except svc.SpireLabError as exc:
            assert why in str(exc), (kw, exc)
        finally:
            db.close()
    db, row = _save(_lab())
    try:
        svc.start_attr_probe(db, lab_id=row.id, created_by="u")
        try:
            svc.start_attr_probe(db, lab_id=row.id, created_by="u")
            raise AssertionError("a second prepare while one runs")
        except svc.SpireLabError as exc:
            assert "already" in str(exc)
    finally:
        db.close()


def test_a_prepared_probe_is_stored_and_its_write_audited():
    fake = FakePS()
    db, row = _save(_lab())
    try:
        job = _run_probe(db, row, fake)
        assert job.status == "completed", job.error_message
        row = db.query(SpireLab).filter(SpireLab.id == row.id).one()
        data = svc.attr_probe(row)
        assert data["prepared_by"] == "prober", "the job's creator, not the lab's"
        assert data["setup"]["read_back"] and data["setup"]["created_type"]
        assert fake.assigned["7"] == [data["setup"]["attribute_id"]]
        audit = (db.query(AuditLog).filter(AuditLog.action == "spire_lab.attr_probe_prepare")
                 .order_by(AuditLog.id.desc()).first())
        assert audit and audit.username == "prober" and audit.target_vm == "ManagedSystem:7"
    finally:
        db.close()


def test_a_failed_prepare_keeps_the_calls_and_names_the_console_remedy():
    fake = FakePS(refuse={("POST", "AttributeTypes"): 405})
    db, row = _save(_lab())
    try:
        job = _run_probe(db, row, fake)
        assert job.status == "failed"
        assert "Configuration → Attributes" in job.error_message
        data = svc.attr_probe(db.query(SpireLab).filter(SpireLab.id == row.id).one())
        assert data["setup"]["read_back"] is False
        assert data["setup"]["calls"][-1]["status"] == 405
        assert not db.query(AuditLog).filter(
            AuditLog.action == "spire_lab.attr_probe_prepare",
            AuditLog.details.contains(row.id)).first(), "nothing was written, so no audit"
    finally:
        db.close()


# ── the answer ────────────────────────────────────────────────────────────────

def test_only_the_runbooks_outcomes_and_values_are_accepted():
    db, row = _save(_lab())
    try:
        bad = (dict(outcome="maybe"),
               dict(outcome="empty", side={"pem_fits": "sort of"}),
               dict(outcome="empty", side={"other": "yes"}),
               dict(outcome="empty", line="x" * (svc.ATTR_PROBE_LINE_MAX + 1)))
        for kw in bad:
            try:
                svc.record_attr_probe_answer(db, lab_id=row.id, answered_by="u", **kw)
                raise AssertionError(kw)
            except svc.SpireLabError:
                pass
        assert set(svc.ATTR_PROBE_OUTCOMES) == {"populated", "truncated", "empty"}
    finally:
        db.close()


def test_an_answer_is_stored_with_who_and_when_and_its_runbook_row():
    db, row = _save(_lab())
    try:
        out = svc.record_attr_probe_answer(
            db, lab_id=row.id, outcome="empty", answered_by="op",
            line="Attributes received: system=[] account=[]", side={"pem_fits": "no"})
        answer = out["answer"]
        assert answer["outcome"] == "empty" and answer["answered_by"] == "op"
        assert answer["answered_at"] and answer["line"].startswith("Attributes received")
        assert answer["pem_fits"] == "no" and answer["tilde_in_account_name"] == "not_tried"
        assert "plugin change" in out["meaning"]["next"], (
            "system=[] means the configuration moves onto the address — the runbook's row")
        assert "attribute writer" in svc.ATTR_PROBE_OUTCOMES["populated"]["next"]
    finally:
        db.close()


def test_an_ungoverned_lab_has_no_answer_to_record():
    db, row = _save(_lab(ps_system_id=None))
    try:
        svc.record_attr_probe_answer(db, lab_id=row.id, outcome="empty", answered_by="u")
        raise AssertionError("no managed system, no Verify Functional Account")
    except svc.SpireLabError as exc:
        assert "not governed" in str(exc)
    finally:
        db.close()


def test_the_latest_answer_across_visible_labs_wins():
    import time
    db, a = _save(_lab(name="first"))
    _db2, b = _save(_lab(name="second", created_by="someone-else"))
    try:
        svc.record_attr_probe_answer(db, lab_id=a.id, outcome="empty", answered_by="u")
        time.sleep(0.01)
        svc.record_attr_probe_answer(db, lab_id=b.id, outcome="populated", answered_by="u")
        latest = svc.latest_attr_probe(db)
        assert latest["lab_id"] == b.id and latest["outcome"] == "populated"
        assert "next" in latest and "means" in latest
        mine = svc.latest_attr_probe(db, visible=lambda r: r.created_by == "tester")
        assert mine["lab_id"] != b.id, "a lab the user cannot see is never cited"
    finally:
        db.close()
        _db2.close()


def test_preparing_again_keeps_a_recorded_answer():
    fake = FakePS()
    db, row = _save(_lab())
    try:
        svc.record_attr_probe_answer(db, lab_id=row.id, outcome="truncated", answered_by="u")
        _run_probe(db, row, fake)
        data = svc.attr_probe(db.query(SpireLab).filter(SpireLab.id == row.id).one())
        assert data["answer"]["outcome"] == "truncated" and data["setup"]["read_back"]
    finally:
        db.close()


# ── the gaps, the routes, the worker ──────────────────────────────────────────

def test_each_gap_goes_once_its_step_is_done():
    row = _lab()
    whats = lambda: " ".join(g["what"] for g in svc.onboarding_gaps(row))  # noqa: E731
    assert "SpiffeTrustDomain" in whats() and "answer" in whats()
    row.attr_probe = json.dumps({"setup": {"read_back": True}})
    assert "SpiffeTrustDomain" not in whats() and "answer" in whats()
    row.attr_probe = json.dumps({"setup": {"read_back": True},
                                 "answer": {"outcome": "empty"}})
    assert "SpiffeTrustDomain" not in whats() and "answer" not in whats()
    assert "functional account" in whats(), "the PKCS#12 stays with a human, always"


def test_the_routes_are_wired_with_write_permission_and_the_right_order():
    import inspect
    from web_dashboard.api import spire_lab as api
    for fn in (api.prepare_attr_probe, api.record_attr_probe_answer):
        assert 'require_permission("cloud_function",' in inspect.getsource(fn)
        assert '"write"' in inspect.getsource(fn), fn.__name__
    assert set(inspect.signature(api.prepare_attr_probe).parameters) == {"lab_id", "db", "user"}
    assert "spire_lab.attr_probe_answer" in inspect.getsource(api.record_attr_probe_answer)
    paths = [r.path for r in api.router.routes]
    assert paths.index("/api/spire-lab/attr-probe") < paths.index("/api/spire-lab/{lab_id}"), (
        "declared after /{lab_id}, the GET would be read as a lab id")
    shape = api._shape(_lab())
    assert "attr_probe" in shape and set(shape["attr_probe"]["outcomes"]) == set(
        svc.ATTR_PROBE_OUTCOMES)


def test_the_worker_runs_the_job_light():
    from web_dashboard import jobs_worker as w
    assert "spirelab_attr_probe" in w.HANDLED_TYPES
    assert "spirelab_attr_probe" in w.LIGHT_TYPES
    src = open(w.__file__, encoding="utf-8").read()
    assert 'job_type == "spirelab_attr_probe"' in src and "run_attr_probe(" in src


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
