"""A workstream's own vault: registered vaults, and the scope that limits one to a team.

What these pin:

- only an administrator registers, changes or deletes a vault, and every change is
  audited; ids and backends are checked, a backend never changes, and an unknown
  workstream is refused;
- a reference into a vault limited to workstream X resolves only when it is resolved
  FOR X — not for another workstream, and not for a caller that names none (a global
  Settings key, a Config-Management run); an unlimited vault and a plain reference into
  the configured store resolve for anyone, as before;
- changing a scope applies at once, not after the resolved-value cache expires;
- a workstream cannot bind a PRA credential from another workstream's vault, and a vault
  still bound somewhere cannot be deleted;
- every per-deploy resolution passes the VM's workstream.

Uses a real temp SQLite database; the secret backends are stubbed. Runs standalone:
    python tests/test_workstream_vaults.py
"""
import os
import re
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="workstream-vaults-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "workstream-vaults-test-secret")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web_dashboard.api import auth  # noqa: E402
from web_dashboard.api import secrets as secrets_api  # noqa: E402
from web_dashboard.database import (AuditLog, Base, SessionLocal, User,  # noqa: E402
                                    Workgroup, engine)
from web_dashboard.services import config_service  # noqa: E402
from web_dashboard.services import secret_vault_service as svs  # noqa: E402
from web_dashboard.services import secrets_backend_service  # noqa: E402
from web_dashboard.services import workgroup_service as ws  # noqa: E402

Base.metadata.create_all(bind=engine)

READS = []


def _read_sync(backend, ref, vault_id=None):
    READS.append((backend, ref, vault_id))
    return f"value-of-{vault_id or 'default'}-{ref}"


secrets_backend_service.read_sync = _read_sync

_db = SessionLocal()
for _n in ("team-a", "team-b"):
    _db.add(Workgroup(name=_n, display_name=_n))
_db.add(User(id=str(uuid.uuid4()), username="root", hashed_password="x", is_admin=True))
_db.commit()
_db.close()


def _client(admin=True):
    def _who(request):
        if not admin:
            raise HTTPException(status_code=403, detail="Admin only")
        return "root"
    auth.admin_username_from_request = _who
    app = FastAPI()
    app.include_router(secrets_api.router)
    return TestClient(app)


def _upsert(vault_id, workgroup=None, backend="azure_kv"):
    db = SessionLocal()
    try:
        return svs.upsert(db, vault_id, backend, "https://kv.example", workgroup)
    finally:
        db.close()


def _delete(vault_id):
    db = SessionLocal()
    try:
        svs.delete(db, vault_id)
    finally:
        db.close()


def _audited(action):
    db = SessionLocal()
    try:
        return db.query(AuditLog).filter(AuditLog.action == action).count()
    finally:
        db.close()


# ── The registry ──────────────────────────────────────────────────────────────

def test_only_an_admin_manages_vaults_and_it_is_audited():
    body = {"backend": "azure_kv", "endpoint": "https://kv.example", "workgroup": "team-a"}
    assert _client(admin=False).put("/api/secrets/vaults/x-kv", json=body).status_code == 403
    assert _client(admin=False).get("/api/secrets/vaults").status_code == 403
    before = _audited("secret_vault_update")
    c = _client()
    r = c.put("/api/secrets/vaults/x-kv", json={**body, "workgroup": "TEAM-A"})
    assert r.status_code == 200 and r.json()["workgroup"] == "team-a", r.text
    assert any(v["id"] == "x-kv" for v in c.get("/api/secrets/vaults").json()["vaults"])
    assert _audited("secret_vault_update") == before + 1
    assert c.delete("/api/secrets/vaults/x-kv").status_code == 200
    assert c.delete("/api/secrets/vaults/x-kv").status_code == 404


def test_bad_input_is_refused():
    c = _client()
    for vid, body in (("Bad Id", {"backend": "azure_kv", "endpoint": "e"}),
                      ("ok", {"backend": "vmware", "endpoint": "e"}),
                      ("ok", {"backend": "azure_kv", "endpoint": ""}),
                      ("ok", {"backend": "azure_kv", "endpoint": "e", "workgroup": "nope"})):
        assert c.put(f"/api/secrets/vaults/{vid}", json=body).status_code == 400, (vid, body)
    _upsert("fixed")
    try:
        assert c.put("/api/secrets/vaults/fixed",
                     json={"backend": "aws_sm", "endpoint": "us-east-1"}).status_code == 400, (
            "references name the backend; changing it would point them somewhere else")
    finally:
        _delete("fixed")


# ── Resolution ────────────────────────────────────────────────────────────────

def test_a_team_vault_resolves_only_for_its_team():
    _upsert("team-a-kv", "team-a")
    _upsert("shared-kv")
    try:
        ref = "azure_kv://team-a-kv/pra-client"
        assert config_service.resolve_reference(ref, workgroup="team-a") == \
            "value-of-team-a-kv-pra-client"
        assert config_service.resolve_reference(ref, workgroup="team-b") == ""
        assert config_service.resolve_reference(ref) == "", (
            "a caller that names no workstream must not read a team's vault")
        assert config_service.resolve_reference(
            "azure_kv://shared-kv/x", workgroup="team-b") == "value-of-shared-kv-x"
        # A plain reference into the configured store is untouched.
        assert config_service.resolve_reference("azure_kv://plain-name") == \
            "value-of-default-plain-name"
    finally:
        _delete("team-a-kv")
        _delete("shared-kv")


def test_a_scope_change_applies_at_once():
    _upsert("moving-kv")
    try:
        ref = "azure_kv://moving-kv/s"
        assert config_service.resolve_reference(ref, workgroup="team-b")  # cached now
        _upsert("moving-kv", "team-a")
        assert config_service.resolve_reference(ref, workgroup="team-b") == ""
    finally:
        _delete("moving-kv")


# ── Bindings ──────────────────────────────────────────────────────────────────

def test_a_workstream_cannot_bind_another_teams_vault_and_a_bound_vault_stays():
    _upsert("team-a-kv", "team-a")
    db = SessionLocal()
    try:
        try:
            ws.set_bindings(db, "team-b", {"pra_credential_ref": "azure_kv://team-a-kv/p"})
            raise AssertionError("team-b must not bind team-a's vault")
        except ws.WorkgroupError as e:
            assert "belongs to workstream 'team-a'" in str(e)
        ws.set_bindings(db, "team-a", {"pra_credential_ref": "azure_kv://team-a-kv/p"})
        try:
            svs.delete(db, "team-a-kv")
            raise AssertionError("a bound vault must not be deleted")
        except svs.VaultError as e:
            assert "team-a" in str(e)
        ws.set_bindings(db, "team-a", {"pra_credential_ref": None})
    finally:
        db.close()
    _delete("team-a-kv")


# ── Every per-deploy resolution names the VM's workstream ─────────────────────

def _src(*parts):
    with open(os.path.join(_ROOT, "web_dashboard", *parts), encoding="utf-8") as fh:
        return fh.read()


def test_every_per_deploy_resolution_passes_the_workstream():
    for cloud in ("aws", "azure", "gcp", "oci"):
        src = _src("services", f"{cloud}_vm_service.py")
        for m in re.finditer(r"client_secret_for\(", src):
            call = src[m.end():src.index(")", src.index(")", m.end()) + 1) + 1]
            assert "workgroup" in call, f"{cloud}: {src[m.start():m.start() + 120]!r}"
        for m in re.finditer(r"resolve_reference\(", src):
            assert "workgroup=" in src[m.end():m.end() + 160], (
                f"{cloud}: {src[m.start():m.start() + 120]!r} resolves without the workstream")
    assert "client_secret_for(pra_credential_ref, deploy_job.workgroup)" in _src(
        "services", "vm_pra_service.py")


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
