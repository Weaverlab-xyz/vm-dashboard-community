"""Secrets bound to a workstream: references an administrator picks for its deploys.

What these pin:

- only an administrator may bind (a member who could would point their team's deploys at
  any secret the dashboard reads); a member may READ the names, never a value;
- the stored shape is checked on the way in and filtered on the way out;
- a deploy that names no SSH key secret or PRA credential gets the bound one; a
  non-admin naming a different one is refused; an administrator may; a workstream with
  nothing bound is untouched;
- every deploy route, and the post-deploy PRA action, applies it before validating and
  fanning out; GCP and OCI now carry the PRA credential to their jumps.

Uses a real temp SQLite database. Runs standalone:
    python tests/test_workstream_secret_bindings.py
"""
import os
import re
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="workstream-secrets-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "workstream-secrets-test-secret")

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


def _workstream(name):
    db = SessionLocal()
    try:
        db.add(Workgroup(name=name, display_name=name))
        db.commit()
    finally:
        db.close()


ALICE = _person("alice", ["team-a"])
BOB = _person("bob", ["team-b"])
ROOT = _person("root", admin=True)
for _n in ("team-a", "team-b", "team-c"):
    _workstream(_n)


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


def _bind(name, values):
    db = SessionLocal()
    try:
        return ws.set_bindings(db, name, values)
    finally:
        db.close()


def _fresh_user(user):
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user.id).first()
        db.expunge(u)
        return u
    finally:
        db.close()


class _Req:
    def __init__(self, ssh=None, pra=None, with_pra=True):
        self.ssh_key_secret_override = ssh
        if with_pra:
            self.pra_credential_ref = pra


# ── The service ───────────────────────────────────────────────────────────────

def test_shape_is_checked_and_null_unbinds():
    for bad in ({"bt_client_secret": "x"}, {"ssh_key_secret": {"vmware": "k"}},
                {"ssh_key_secret": "not-a-map"}, {"pra_credential_ref": 5},
                {"ssh_key_secret": {"aws": "x" * 2000}}):
        try:
            _bind("team-c", bad)
            raise AssertionError(f"{bad} should be refused")
        except WorkgroupError:
            pass
    out = _bind("team-c", {"ssh_key_secret": {"aws": " team-c/ssh ", "gcp": "gk"},
                           "pra_credential_ref": "aws_sm://pra/team-c"})
    assert out == {"ssh_key_secret": {"aws": "team-c/ssh", "gcp": "gk"},
                   "pra_credential_ref": "aws_sm://pra/team-c"}
    # Only the named clouds change.
    _bind("team-c", {"ssh_key_secret": {"gcp": None}})
    assert ws.bindings("team-c")["ssh_key_secret"] == {"aws": "team-c/ssh"}
    _bind("team-c", {"ssh_key_secret": {"aws": ""}, "pra_credential_ref": None})
    db = SessionLocal()
    try:
        assert ws.get(db, "team-c").secret_bindings is None, "nothing bound is stored as NULL"
    finally:
        db.close()


def test_a_hand_edited_row_is_filtered_on_read():
    db = SessionLocal()
    try:
        ws.get(db, "team-c").secret_bindings = (
            '{"ssh_key_secret": {"aws": "ok", "vmware": "no", "gcp": 3}, '
            '"pra_credential_ref": ["x"], "extra": "y"}')
        db.commit()
        assert ws.bindings("team-c", db) == {"ssh_key_secret": {"aws": "ok"},
                                              "pra_credential_ref": None}
        ws.get(db, "team-c").secret_bindings = None
        db.commit()
    finally:
        db.close()


def test_a_blank_request_gets_the_bound_references():
    _bind("team-a", {"ssh_key_secret": {"aws": "team-a/ssh"},
                     "pra_credential_ref": "aws_sm://pra/team-a"})
    try:
        req = _Req()
        ws.apply_secret_bindings("aws", req, "team-a", _fresh_user(ALICE))
        assert req.ssh_key_secret_override == "team-a/ssh"
        assert req.pra_credential_ref == "aws_sm://pra/team-a"
        # Another cloud has no SSH binding: left alone; the PRA one still applies.
        req = _Req()
        ws.apply_secret_bindings("gcp", req, "team-a", _fresh_user(ALICE))
        assert req.ssh_key_secret_override is None
        assert req.pra_credential_ref == "aws_sm://pra/team-a"
        # A model with no pra_credential_ref field is not given one.
        req = _Req(with_pra=False)
        ws.apply_secret_bindings("aws", req, "team-a", _fresh_user(ALICE))
        assert not hasattr(req, "pra_credential_ref")
    finally:
        _bind("team-a", {"ssh_key_secret": {"aws": None}, "pra_credential_ref": None})


def test_a_member_cannot_name_another_secret_but_an_admin_can():
    _bind("team-a", {"ssh_key_secret": {"aws": "team-a/ssh"},
                     "pra_credential_ref": "aws_sm://pra/team-a"})
    try:
        for req in (_Req(ssh="team-b/ssh"), _Req(pra="aws_sm://pra/team-b")):
            try:
                ws.apply_secret_bindings("aws", req, "team-a", _fresh_user(ALICE))
                raise AssertionError("a member's different secret should be refused")
            except ws.SecretBindingDenied as e:
                assert "set by an administrator" in str(e)
        # Naming the bound one is fine.
        ws.apply_secret_bindings("aws", _Req(ssh="team-a/ssh"), "team-a", _fresh_user(ALICE))
        req = _Req(ssh="admin/ssh")
        ws.apply_secret_bindings("aws", req, "team-a", _fresh_user(ROOT))
        assert req.ssh_key_secret_override == "admin/ssh"
    finally:
        _bind("team-a", {"ssh_key_secret": {"aws": None}, "pra_credential_ref": None})


def test_nothing_bound_changes_nothing():
    req = _Req(ssh="anything/at/all", pra="aws_sm://x")
    ws.apply_secret_bindings("aws", req, "team-b", _fresh_user(BOB))
    assert req.ssh_key_secret_override == "anything/at/all"
    assert req.pra_credential_ref == "aws_sm://x"
    req = _Req()
    ws.apply_secret_bindings("aws", req, None, _fresh_user(BOB))
    assert req.ssh_key_secret_override is None


# ── The API ───────────────────────────────────────────────────────────────────

def _audited(action):
    db = SessionLocal()
    try:
        return db.query(AuditLog).filter(AuditLog.action == action).count()
    finally:
        db.close()


def test_only_an_admin_binds_and_it_is_audited():
    body = {"ssh_key_secret": {"aws": "mine"}}
    assert _client(ALICE).put("/api/workgroups/team-a/secret-bindings", json=body).status_code == 403
    assert ws.bindings("team-a")["ssh_key_secret"] == {}
    before = _audited("workstream_secret_bindings_update")
    r = _client(ROOT).put("/api/workgroups/team-a/secret-bindings", json=body)
    assert r.status_code == 200, r.text
    assert _audited("workstream_secret_bindings_update") == before + 1
    _bind("team-a", {"ssh_key_secret": {"aws": None}})


def test_a_member_reads_the_names_and_an_outsider_does_not():
    _bind("team-a", {"pra_credential_ref": "aws_sm://pra/team-a"})
    try:
        r = _client(ALICE).get("/api/workgroups/team-a/secret-bindings")
        assert r.status_code == 200 and r.json()["pra_credential_ref"] == "aws_sm://pra/team-a"
        assert _client(BOB).get("/api/workgroups/team-a/secret-bindings").status_code == 403
    finally:
        _bind("team-a", {"pra_credential_ref": None})


def test_a_refusal_is_a_403_at_the_deploy_edge():
    from fastapi import HTTPException
    _bind("team-a", {"ssh_key_secret": {"aws": "team-a/ssh"}})
    try:
        try:
            api.enforce_secret_bindings("aws", _Req(ssh="other"), "team-a", _fresh_user(ALICE))
            raise AssertionError("should raise")
        except HTTPException as e:
            assert e.status_code == 403
    finally:
        _bind("team-a", {"ssh_key_secret": {"aws": None}})


# ── Every route applies it ────────────────────────────────────────────────────

def _src(*parts):
    with open(os.path.join(_ROOT, "web_dashboard", *parts), encoding="utf-8") as fh:
        return fh.read()


def test_every_deploy_route_applies_the_bindings_before_validating():
    for cloud in ("aws", "azure", "gcp", "oci"):
        src = _src("api", f"{cloud}.py")
        resolves = [m.start() for m in re.finditer(
            r"workgroup = _validate_workgroup\(db, current_user", src)]
        assert len(resolves) == 2, f"{cloud}: expected the single and bulk deploy routes"
        for i in resolves:
            window = src[i:i + 400]
            assert f'enforce_secret_bindings("{cloud}"' in window, (
                f"{cloud}: a deploy route resolves its workstream without applying its "
                "bound secrets")
            j = window.index("enforce_secret_bindings")
            for later in ("_validate_ssh_key_override(", "_validate_gcp_ssh_override(",
                          "_fan_out_batch("):
                k = window.find(later)
                assert k == -1 or k > j, f"{cloud}: {later} runs before the bindings"
        assert f'enforce_secret_bindings("{cloud}", payload, deploy_job.workgroup' in src, (
            f"{cloud}: the post-deploy PRA action ignores the bound PRA credential")


def test_gcp_and_oci_carry_the_pra_credential_to_their_jumps():
    from web_dashboard.models import gcp, oci
    for model in (gcp.GCPDeployRequest, gcp.GCPBulkDeployRequest,
                  oci.OCIDeployRequest, oci.OCIBulkDeployRequest):
        assert "pra_credential_ref" in model.model_fields, model.__name__
    for cloud in ("gcp", "oci"):
        assert "pra_credential_ref=req.pra_credential_ref" in _src("api", f"{cloud}.py"), (
            f"{cloud}: bulk children drop the PRA credential")
        assert "client_secret=vm_pra_service.client_secret_for(" in _src(
            "services", f"{cloud}_vm_service.py"), f"{cloud}: the jump ignores the credential"


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
