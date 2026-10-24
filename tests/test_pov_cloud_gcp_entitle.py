"""GCP for the POV cloud provider: the SPIFFE identity, and Entitle for its writes.

docs/profiles/pov/public-cloud.md → Keyless GCP. Workload Credentials mints no GCP
credentials, so the privileged half is an Entitle grant: the service account holds a read
role, and each write asks Entitle for the provision role on behalf of the machine user the
account is assigned to. Pinned:

  * the machine-request client speaks Entitle's published API — duration in SECONDS,
    ``behalfOf: {id}``, ``target: {type, role|bundle: {id}}``, no doubled ``/v1`` — none
    of which it did before, so no machine elevation could have succeeded live;
  * Entitle's own status names classify correctly ("approved" is NOT granted);
  * ``elevate(gated=False, target=…)`` serves a caller with its own switch, and a live
    grant is REUSED instead of re-requested; a grant with no expiry gets one;
  * every GCP write runs inside the elevation, no read does, and a fresh grant waits for
    IAM before the write while a reused one does not;
  * Test connection never files a request (an auto-approved one IS a grant) and calls a
    standing write role a problem;
  * the permission set the job waits on equals the Terraform provision role's additions;
  * the panel never clears the stored key on a radio change.

No cloud and no Entitle are reachable: HTTP, the thread hop and IAM are stubbed.

Run: python tests/test_pov_cloud_gcp_entitle.py   (or under pytest)
"""
import asyncio
import ast
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-gcp-entitle-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-pov-gcp-entitle")

try:
    import httpx  # noqa: F401
    import pydantic  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

# First-party imports UNGUARDED: a broken module must fail this file, not skip it.
from web_dashboard.api.setup import PovCloudFeatureConfig  # noqa: E402
from web_dashboard.config import Settings  # noqa: E402
from web_dashboard.database import Base, EntitleActivation, SessionLocal, engine  # noqa: E402
from web_dashboard.services import (cloud_federation, cloud_identity_service,  # noqa: E402
                                    config_service, entitle_public_api, entitle_service,
                                    gcp_service, pov_cloud_gcp)
from _settings_src import settings_source

Base.metadata.create_all(bind=engine)

_DRIVER = os.path.join(_ROOT, "web_dashboard", "services", "pov_cloud_gcp.py")
_TF = os.path.join(_ROOT, "terraform", "pov_cloud_gcp_role", "main.tf")


class _Patch:
    def __init__(self):
        self._undo = []

    def set(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, value in reversed(self._undo):
            setattr(obj, name, value)


def _cfg_patch(patch, values):
    real = config_service.get
    patch.set(config_service, "get",
              lambda k, *a, **kw: values[k] if k in values else real(k, *a, **kw))


def _clear_activations():
    db = SessionLocal()
    try:
        db.query(EntitleActivation).delete()
        db.commit()
    finally:
        db.close()


# ── the request client ────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = str(body)

    def json(self):
        return self._body


def _fake_httpx(patch, sent, response):
    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            sent.append(("POST", url, json))
            return _Resp(200, response)

        async def get(self, url, headers=None, params=None):
            sent.append(("GET", url, None))
            return _Resp(200, response)
    patch.set(httpx, "AsyncClient", _Client)


def test_a_machine_request_speaks_entitles_published_api():
    patch, sent = _Patch(), []
    try:
        patch.set(entitle_public_api, "auth",
                  lambda cfg=None: ("https://api.us.entitle.io", {"Authorization": "Bearer t"}))
        _fake_httpx(patch, sent, {"result": [{"id": "req-1", "status": "waitingForApproval"}]})
        rid = asyncio.run(entitle_service.submit_machine_request(
            operation="gcp:pov:provision", target={"role_id": "r-9"}, duration_minutes=60,
            payload_hash="ab" * 32, behalf_of_email="vm-dashboard-pov@example.com"))
    finally:
        patch.undo()
    assert rid == "req-1"
    method, url, body = sent[0]
    assert url == "https://api.us.entitle.io/public/v1/accessRequests", url
    assert body["duration"] == 3600, "Entitle's duration is SECONDS"
    assert body["behalfOf"] == {"id": "vm-dashboard-pov@example.com"}
    assert body["target"] == {"type": "role", "role": {"id": "r-9"}}


def test_target_shapes():
    assert entitle_service.request_target({"bundle_id": "b"}) == {"type": "bundle", "bundle": {"id": "b"}}
    shaped = {"type": "role", "role": {"id": "x"}}
    assert entitle_service.request_target(shaped) == shaped
    try:
        entitle_service.request_target({"nothing": 1})
    except entitle_service.EntitleError:
        return
    raise AssertionError("a target naming no id was accepted")


def test_entitles_own_status_names_classify():
    pending = ("approved", "permissionInProgress", "waitingForIT", "waitingForApproval")
    for status in pending:
        assert entitle_service.classify_machine_status({"status": status})[0] == "pending", status
    assert entitle_service.classify_machine_status({"status": "granted"})[0] == "granted"
    for status in ("rejected", "failed", "revoked", "cancelled"):
        assert entitle_service.classify_machine_status({"status": status})[0] == "denied", status


# ── elevate: own switch, explicit target, reuse ───────────────────────────────

def _stub_entitle(patch, calls, status="granted"):
    async def _submit(**kw):
        calls.append(("submit", kw))
        return f"req-{len(calls)}"

    async def _get(rid):
        calls.append(("get", rid))
        return {"id": rid, "status": status}
    patch.set(entitle_service, "submit_machine_request", _submit)
    patch.set(entitle_service, "get_machine_request", _get)


def _elevate(**over):
    kw = dict(duration_minutes=60, payload_hash="cd" * 32, target={"role_id": "r-1"},
              gated=False, reuse_min_remaining_minutes=20)
    kw.update(over)

    async def run():
        async with cloud_identity_service.elevate("gcp", "gcp:pov:provision", **kw) as h:
            return h
    return asyncio.run(run())


def test_an_ungated_elevation_ignores_the_console_gate_and_the_matrix():
    _clear_activations()
    patch, calls = _Patch(), []
    try:
        _cfg_patch(patch, {"cloud_identity_gate_enabled": "false", "cloud_identity_matrix": "",
                           "entitle_machine_identity_email": "m@example.com",
                           "entitle_machine_poll_interval_ms": "100"})
        _stub_entitle(patch, calls)
        handle = _elevate()
    finally:
        patch.undo()
    assert not handle.is_noop and handle.entitle_request_id == "req-1"
    assert calls[0][1]["target"] == {"role_id": "r-1"}
    assert calls[0][1]["behalf_of_email"] == "m@example.com"
    assert handle.expires_at is not None, "a grant with no expiry from Entitle got none"


def test_a_live_grant_is_reused_not_re_requested():
    _clear_activations()
    patch, calls = _Patch(), []
    try:
        _cfg_patch(patch, {"entitle_machine_identity_email": "m@example.com",
                           "entitle_machine_poll_interval_ms": "100"})
        _stub_entitle(patch, calls)
        first = _elevate()
        second = _elevate()
    finally:
        patch.undo()
    assert [c for c in calls if c[0] == "submit"].__len__() == 1, calls
    assert second.extra.get("reused") and second.entitle_request_id == first.entitle_request_id


def test_a_grant_near_expiry_is_not_reused():
    _clear_activations()
    db = SessionLocal()
    try:
        db.add(EntitleActivation(
            id="old", cloud="gcp", operation="gcp:pov:provision", payload_hash="x",
            status="completed", entitle_request_id="req-old",
            requested_at=datetime.now(timezone.utc).replace(tzinfo=None),
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5)))
        db.commit()
    finally:
        db.close()
    patch, calls = _Patch(), []
    try:
        _cfg_patch(patch, {"entitle_machine_identity_email": "m@example.com",
                           "entitle_machine_poll_interval_ms": "100"})
        _stub_entitle(patch, calls)
        handle = _elevate()
    finally:
        patch.undo()
    assert not handle.extra.get("reused") and handle.entitle_request_id != "req-old"


# ── the driver: writes elevate, reads do not ──────────────────────────────────

_WRITES = ("create_network", "create_vms", "power", "delete_environment", "remove_vms")
_READS = ("read_environment", "list_environments", "read_network", "verify")


def test_every_write_runs_inside_the_elevation_and_no_read_does():
    tree = ast.parse(open(_DRIVER, encoding="utf-8").read())
    bodies = {n.name: ast.unparse(n) for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
    for name in _WRITES:
        assert "_privileged(" in bodies[name], f"{name} writes without the Entitle grant"
    for name in _READS:
        assert "_privileged(" not in bodies.get(name, ""), f"{name} requests write privilege"


def _privileged_run(patch, calls, waits, *, source="entitle"):
    _cfg_patch(patch, {"pov_gcp_provision_source": source,
                       "pov_gcp_entitle_target_id": "r-1",
                       "pov_gcp_entitle_target_type": "role",
                       "pov_gcp_entitle_grant_minutes": "60",
                       "entitle_machine_identity_email": "m@example.com",
                       "entitle_machine_poll_interval_ms": "100"})
    _stub_entitle(patch, calls)
    patch.set(pov_cloud_gcp, "_wait_for_permissions", lambda project: waits.append(project))

    async def _to_thread(fn, *args):
        return fn(*args)
    patch.set(gcp_service, "_to_thread", _to_thread)

    async def run():
        async with pov_cloud_gcp._privileged("pov-proj", "create_vms:povenv-x"):
            pass
        async with pov_cloud_gcp._privileged("pov-proj", "power:povenv-x:stop"):
            pass
    asyncio.run(run())


def test_a_fresh_grant_waits_for_iam_and_a_reused_one_does_not():
    _clear_activations()
    patch, calls, waits = _Patch(), [], []
    try:
        _privileged_run(patch, calls, waits)
    finally:
        patch.undo()
    assert len([c for c in calls if c[0] == "submit"]) == 1, calls
    assert waits == ["pov-proj"], waits


def test_without_entitle_the_writes_ask_nobody():
    _clear_activations()
    patch, calls, waits = _Patch(), [], []
    try:
        _privileged_run(patch, calls, waits, source="")
    finally:
        patch.undo()
    assert not calls and not waits


def test_a_refusal_fails_the_write_closed():
    _clear_activations()
    patch, calls = _Patch(), []
    try:
        _cfg_patch(patch, {"pov_gcp_provision_source": "entitle",
                           "pov_gcp_entitle_target_id": "r-1",
                           "entitle_machine_identity_email": "m@example.com",
                           "entitle_machine_poll_interval_ms": "100"})
        _stub_entitle(patch, calls, status="rejected")

        async def run():
            async with pov_cloud_gcp._privileged("pov-proj", "delete:povenv-x"):
                raise AssertionError("the write ran without a grant")
        try:
            asyncio.run(run())
        except pov_cloud_gcp.env.CloudEnvError as exc:
            assert "Entitle did not grant" in str(exc), exc
            return
    finally:
        patch.undo()
    raise AssertionError("a rejected request did not fail the write")


# ── Test connection ───────────────────────────────────────────────────────────

def _verify(patch, *, user=True, standing=(), submits=None):
    _cfg_patch(patch, {"pov_gcp_provision_source": "entitle",
                       "pov_gcp_entitle_target_id": "r-1",
                       "entitle_machine_identity_email": "m@example.com",
                       "gcp_federation_service_account": "pov@p.iam.gserviceaccount.com"})
    patch.set(pov_cloud_gcp, "configured", lambda: True)
    patch.set(pov_cloud_gcp, "_project", lambda env_id="": "pov-proj")
    patch.set(cloud_federation, "source", lambda cloud: cloud_federation.SOURCE)
    patch.set(cloud_federation, "active", lambda cloud: True)
    patch.set(pov_cloud_gcp, "_verify_sync", lambda project, region: "us-central1-a")
    patch.set(pov_cloud_gcp, "_held_permissions", lambda project: set(standing))

    async def _to_thread(fn, *args):
        return fn(*args)
    patch.set(gcp_service, "_to_thread", _to_thread)

    async def _find(email):
        return {"email": email} if user else None
    patch.set(entitle_service, "find_user", _find)

    async def _submit(**kw):
        submits.append(kw)
        return "req"
    patch.set(entitle_service, "submit_machine_request", _submit)
    return asyncio.run(pov_cloud_gcp.verify())


def test_verify_proves_both_halves_and_requests_nothing():
    patch, submits = _Patch(), []
    try:
        ok, msg = _verify(patch, submits=submits)
    finally:
        patch.undo()
    assert ok, msg
    assert "pov@p.iam.gserviceaccount.com" in msg and "nothing requested" in msg, msg
    assert "no standing write privilege" in msg, msg
    assert not submits, "Test connection filed an Entitle request — that is a grant"


def test_verify_calls_a_standing_write_role_a_problem():
    patch, submits = _Patch(), []
    try:
        ok, msg = _verify(patch, standing={"compute.instances.create"}, submits=submits)
    finally:
        patch.undo()
    assert "ALREADY holds" in msg and "compute.instances.create" in msg, msg


def test_verify_names_a_missing_machine_user():
    patch, submits = _Patch(), []
    try:
        ok, msg = _verify(patch, user=False, submits=submits)
    finally:
        patch.undo()
    assert not ok and "no user with that email" in msg, msg


# ── one permission set, two copies ────────────────────────────────────────────

def _tf_list(name):
    src = open(_TF, encoding="utf-8").read()
    block = src.split(f"{name} = [", 1)[1].split("]", 1)[0]
    return re.findall(r'"([a-zA-Z.]+)"', block)


def test_the_wait_set_equals_the_terraform_provision_additions():
    assert set(_tf_list("provision_additions")) == set(pov_cloud_gcp.PROVISION_PERMISSIONS)


def test_the_read_role_holds_no_write():
    assert not set(_tf_list("read_permissions")) & set(pov_cloud_gcp.PROVISION_PERMISSIONS)


# ── settings ──────────────────────────────────────────────────────────────────

def test_the_new_settings_are_declared_in_both_places():
    fields = PovCloudFeatureConfig.model_fields
    for name in ("dashboard_spiffe_aud_gcp", "gcp_federation_service_account",
                 "pov_gcp_provision_source", "pov_gcp_entitle_target_type",
                 "pov_gcp_entitle_target_id", "pov_gcp_entitle_grant_minutes",
                 "entitle_machine_identity_email"):
        assert name in fields, f"PovCloudFeatureConfig does not declare {name}"
        assert name in Settings.model_fields, f"config.Settings does not declare {name}"


def _method(src, name):
    start = src.index(f"    {name}(")
    return src[start:src.index("\n    },", start)]


def test_choosing_a_gcp_source_never_touches_the_key():
    body = _method(settings_source(), "choosePovGcpSource")
    assert "pov_gcp_provision_source = src === 'spiffe_entitle' ? 'entitle' : ''" in body
    assert "gcp_service_account_json" not in body


def test_removing_the_stored_key_marks_it_touched():
    body = _method(settings_source(), "removePovGcpStoredKey")
    assert "panelSecretsTouched.push('gcp_service_account_json')" in body, body


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
