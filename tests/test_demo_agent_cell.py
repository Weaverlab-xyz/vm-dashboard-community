"""A Demo Presenter's agent cell: the agent runs as a service account the cell creates.

What this file holds in place (api/agentcell, api/demo_gates):

  * **A presenter picks no token user.** The cell creates a service account with the
    built-in Demo Agent role, in the cell's workstream, and records that it owns it.
  * **The agent never reaches past the presenter.** The workstream must be the caller's,
    the host must be a VM in the caller's workstreams, the SPIRE lab must be one the caller
    can see, and the caller must hold every grant the Demo Agent role carries.
  * **Options disclose no roster** to a presenter, and say linking is off.
  * **Revoke** is scoped to cells the presenter can see, and disables the owned account.
  * **Linking and cluster-access episodes** stay on `config_mgmt:write`.
  * **A `config_mgmt:write` holder** keeps the free choice of token user.

The outside world is stubbed -- the MCP flag, the host lookup, the SVID check -- and
nothing else: the database, the role, the service account and its OAuth client are real.

Runs under pytest, or standalone:
    python tests/test_demo_agent_cell.py
"""
import ast
import asyncio
import json
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="demo-agent-cell-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-demo-agent-cell")

from fastapi import HTTPException  # noqa: E402

from web_dashboard.api import agentcell, demo_gates  # noqa: E402
from web_dashboard.database import (AgentCell, Base, Job, OAuthClient,  # noqa: E402
                                    SessionLocal, SpireLab, User, Workgroup, engine)
from web_dashboard.models.agentcell import AgentCellCreateRequest  # noqa: E402
from web_dashboard.services import (agentcell_service, feature_flags,  # noqa: E402
                                    role_service, spire_lab_service)

Base.metadata.create_all(bind=engine)

PRESENTER_MAP = next(r["permissions"] for r in role_service._BUILTIN_ROLES
                     if r["slug"] == "demo-presenter")


# ── fixtures ─────────────────────────────────────────────────────────────────

def _fresh():
    db = SessionLocal()
    for model in (AgentCell, OAuthClient, Job, SpireLab, User, Workgroup):
        db.query(model).delete()
    db.commit()
    role_service.seed_builtins(db)
    for name in ("demo", "ops"):
        db.add(Workgroup(id=str(uuid.uuid4()), name=name, display_name=name))
    db.commit()
    return db


def _user(db, perms, *, workgroups=("demo",), name=None, admin=False):
    u = User(id=str(uuid.uuid4()), username=name or "u-" + uuid.uuid4().hex[:8],
             is_active=True, is_admin=admin)
    u.permissions = json.dumps(perms)
    u.workgroups = json.dumps(list(workgroups))
    db.add(u)
    db.commit()
    return u


def _host(db, workgroup="demo", name="worker-1"):
    j = Job(id=str(uuid.uuid4()), job_type="gce_deploy", workgroup=workgroup,
            status="completed", created_by="someone")
    j.metadata_dict = {"instance_name": name, "private_ip": "10.0.0.5"}
    db.add(j)
    db.commit()
    return j


def _lab(db, created_by):
    lab = SpireLab(id=str(uuid.uuid4()), name="lab", trust_domain="lab.example",
                   status="available", created_by=created_by, cloud="gcp")
    db.add(lab)
    db.commit()
    return lab


class _World:
    """Stub the three things that reach outside the database."""

    def __init__(self, host_job):
        self.host_job = host_job

    def __enter__(self):
        self._saved = (feature_flags.enabled, spire_lab_service.resolve_host,
                       agentcell_service.svid_client_available)
        orig = feature_flags.enabled
        feature_flags.enabled = (lambda f, d=False:
                                 True if f == "mcp_server_enabled" else orig(f, d))
        job = self.host_job
        spire_lab_service.resolve_host = lambda db, cloud, ref: {
            "name": ref, "private_ip": "10.0.0.5", "public_ip": "",
            "meta": {}, "deploy_job_id": job.id}
        agentcell_service.svid_client_available = lambda db, td, sid: ""
        return self

    def __exit__(self, *exc):
        (feature_flags.enabled, spire_lab_service.resolve_host,
         agentcell_service.svid_client_available) = self._saved


def _mint(db, user, **over):
    body = {"name": "watcher", "host_ref": "worker-1", "cloud": "gcp",
            "trust_source": "lab", "spire_lab_id": over.pop("lab_id"),
            "pat_hours": 4, "workgroup": "demo"}
    body.update(over)
    return agentcell.create_agent(AgentCellCreateRequest(**body), db=db, current_user=user)


def _status(fn):
    try:
        fn()
        return 200
    except HTTPException as exc:
        return exc.status_code


# ── minting ──────────────────────────────────────────────────────────────────

def test_a_presenter_mint_creates_a_bounded_service_account():
    db = _fresh()
    try:
        p = _user(db, PRESENTER_MAP)
        lab = _lab(db, p.username)
        with _World(_host(db)):
            out = _mint(db, p, lab_id=lab.id)
        row = db.query(AgentCell).filter(AgentCell.id == out.id).first()
        assert row.owns_agent_account is True
        sa = db.query(User).filter(User.id == row.pat_user_id).first()
        assert sa.is_service_account and not sa.is_effective_admin
        assert sa.workgroups_list == ["demo"]
        role = role_service.get_by_slug(db, "demo-agent")
        assert sa.role_id == role.id
        assert sa.effective_permissions_dict == {"vms": ["read"], "inventory": ["read"]}, (
            sa.effective_permissions_dict)
        assert out.client_id and out.token, "no OAuth client was minted for the account"
        assert row.workgroup == "demo"
    finally:
        db.close()


def test_a_presenter_cannot_choose_the_token_user():
    db = _fresh()
    try:
        p = _user(db, PRESENTER_MAP)
        victim = _user(db, {"aws": ["read", "write", "delete"]}, name="cloud-op")
        lab = _lab(db, p.username)
        with _World(_host(db)):
            assert _status(lambda: _mint(db, p, lab_id=lab.id, pat_user_id=victim.id)) == 400
        assert db.query(AgentCell).count() == 0
    finally:
        db.close()


def test_the_workstream_host_and_lab_must_be_the_presenters():
    db = _fresh()
    try:
        p = _user(db, PRESENTER_MAP)
        mine = _lab(db, p.username)
        theirs = _lab(db, "someone-else")
        with _World(_host(db)):
            assert _status(lambda: _mint(db, p, lab_id=mine.id, workgroup="")) == 400
            assert _status(lambda: _mint(db, p, lab_id=mine.id, workgroup="ops")) == 403
            assert _status(lambda: _mint(db, p, lab_id=theirs.id)) == 404
        with _World(_host(db, workgroup="ops", name="their-vm")):
            assert _status(lambda: _mint(db, p, lab_id=mine.id, host_ref="their-vm")) == 400
        assert db.query(AgentCell).count() == 0
        assert db.query(User).filter(User.is_service_account == True).count() == 0  # noqa: E712
    finally:
        db.close()


def test_the_agent_can_never_hold_more_than_the_presenter():
    db = _fresh()
    try:
        thin = _user(db, {"demo": ["read", "write", "delete"]})   # no vms/inventory read
        lab = _lab(db, thin.username)
        with _World(_host(db)):
            assert _status(lambda: _mint(db, thin, lab_id=lab.id)) == 403
        assert db.query(User).filter(User.is_service_account == True).count() == 0, (  # noqa: E712
            "a refused mint left a service account behind")
    finally:
        db.close()


def test_an_operator_still_picks_the_token_user():
    db = _fresh()
    try:
        op = _user(db, {"config_mgmt": ["read", "write"]})
        target = _user(db, {"vms": ["read"]}, name="narrow-user")
        lab = _lab(db, op.username)
        with _World(_host(db)):
            out = _mint(db, op, lab_id=lab.id, pat_user_id=target.id)
            assert _status(lambda: _mint(db, op, lab_id=lab.id)) == 400, (
                "an operator mint with no token user should be refused, not defaulted")
        row = db.query(AgentCell).filter(AgentCell.id == out.id).first()
        assert row.pat_user_id == target.id and not row.owns_agent_account
    finally:
        db.close()


# ── options ──────────────────────────────────────────────────────────────────

def test_options_show_a_presenter_no_roster():
    db = _fresh()
    try:
        p = _user(db, PRESENTER_MAP)
        _user(db, {"vms": ["read"]}, name="someone")
        op = _user(db, {"config_mgmt": ["read", "write"]})
        out = agentcell.build_options(db=db, current_user=p)
        assert out["users"] == [] and out["mint_as"] == "cell_service_account"
        assert out["can_link"] is False
        assert not any("administrator" in m for m in out["missing"]), (
            "a presenter is told no user can be minted against -- they pick none")
        out = agentcell.build_options(db=db, current_user=op)
        assert out["mint_as"] == "pick" and out["can_link"] is True and out["users"]
    finally:
        db.close()


# ── revoke ───────────────────────────────────────────────────────────────────

def test_a_presenter_revokes_their_own_agent_and_its_account():
    db = _fresh()
    try:
        p = _user(db, PRESENTER_MAP)
        lab = _lab(db, p.username)
        with _World(_host(db)):
            out = _mint(db, p, lab_id=lab.id)
        agentcell.revoke_agent(out.id, db=db, current_user=p)
        row = db.query(AgentCell).filter(AgentCell.id == out.id).first()
        assert row.status == "revoked"
        sa = db.query(User).filter(User.id == row.pat_user_id).first()
        assert sa.is_active is False, "revoking left the cell's service account active"
    finally:
        db.close()


def test_a_presenter_cannot_revoke_someone_elses_agent():
    db = _fresh()
    try:
        owner = _user(db, PRESENTER_MAP, workgroups=("ops",))
        lab = _lab(db, owner.username)
        with _World(_host(db, workgroup="ops")):
            out = _mint(db, owner, lab_id=lab.id, workgroup="ops")
        other = _user(db, PRESENTER_MAP, workgroups=("demo",))
        assert _status(lambda: agentcell.revoke_agent(out.id, db=db, current_user=other)) == 404
        assert _status(lambda: agentcell.remove_spire_identity(
            out.id, db=db, current_user=other)) == 404
    finally:
        db.close()


def _passes(dep, user):
    try:
        asyncio.run(dep(current_user=user))
        return True
    except HTTPException as exc:
        assert exc.status_code == 403
        return False


def test_revoke_needs_demo_delete_or_config_mgmt_write():
    db = _fresh()
    try:
        dep = demo_gates.require_cell("config_mgmt", "write", demo_level="delete")
        assert _passes(dep, _user(db, {"demo": ["delete"]}))
        assert _passes(dep, _user(db, {"config_mgmt": ["write"]}))
        assert not _passes(dep, _user(db, {"demo": ["read", "write"]}))
    finally:
        db.close()


# ── what stays operator-only ─────────────────────────────────────────────────

def test_linking_and_episodes_stay_on_config_mgmt_write():
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "agentcell.py"),
               encoding="utf-8").read()
    gates = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef):
            gates[node.name] = ast.get_source_segment(src, node).split(":\n", 1)[0]
    for fn in ("link_agent", "unlink_agent", "request_cluster_access"):
        assert 'require_permission("config_mgmt", "write")' in gates[fn], fn
        assert "require_cell" not in gates[fn], f"{fn} accepts demo"
    for fn in ("create_agent", "build_options"):
        assert 'require_cell("config_mgmt", "write")' in gates[fn], fn
    for fn in ("revoke_agent", "remove_spire_identity"):
        assert 'require_cell("config_mgmt", "write", demo_level="delete")' in gates[fn], fn


def test_the_demo_agent_role_is_narrow_and_valid():
    spec = next(r for r in role_service._BUILTIN_ROLES if r["slug"] == "demo-agent")
    assert spec["permissions"] == {"vms": ["read"], "inventory": ["read"]}
    role_service.validate_role_permissions(spec["permissions"], slug=spec["slug"])
    # The subset guarantee only holds while the presenter role carries what this does.
    for scope, levels in spec["permissions"].items():
        for level in levels:
            assert level in PRESENTER_MAP.get(scope, []), (
                f"Demo Presenter lacks {scope}:{level}, so no presenter could mint an agent")


# ── every by-id route, for everyone ───────────────────────────────────────────

OPERATOR = {"config_mgmt": ["read", "write"], "vms": ["read"]}


def _operator_cell(db):
    """An agent cell in `ops`, minted by an operator there against a narrow user."""
    owner = _user(db, OPERATOR, workgroups=("ops",), name="ops-operator")
    target = _user(db, {"vms": ["read"]}, workgroups=("ops",), name="ops-agent-user")
    lab = _lab(db, owner.username)
    with _World(_host(db, workgroup="ops")):
        out = _mint(db, owner, lab_id=lab.id, pat_user_id=target.id, workgroup="ops")
    return out.id


def test_an_operator_cannot_act_on_a_cell_outside_their_workstreams():
    """config_mgmt:write used to reach every agent cell by id -- revoke, link, unlink,
    open or close an episode -- whoever owned it. The row is resolved with the list's own
    visibility rule now (workgroup or creator), for every caller."""
    from web_dashboard.models.agentcell import AgentCellEpisodeRequest, AgentCellLinkRequest
    db = _fresh()
    try:
        cid = _operator_cell(db)
        other = _user(db, OPERATOR, workgroups=("demo",), name="demo-operator")
        calls = {
            "revoke": lambda: agentcell.revoke_agent(cid, db=db, current_user=other),
            "spire": lambda: agentcell.remove_spire_identity(cid, db=db, current_user=other),
            "link": lambda: agentcell.link_agent(
                cid, AgentCellLinkRequest(mechanism="cloud", credential_id="x"),
                db=db, current_user=other),
            "unlink": lambda: agentcell.unlink_agent(cid, db=db, current_user=other),
            "episode": lambda: agentcell.request_cluster_access(
                cid, AgentCellEpisodeRequest(), db=db, current_user=other),
            "release": lambda: agentcell.release_cluster_access(cid, db=db, current_user=other),
        }
        for name, call in calls.items():
            assert _status(call) == 404, f"{name}: an operator reached another team's agent"
        assert db.query(AgentCell).filter(AgentCell.id == cid).first().status != "revoked"
    finally:
        db.close()


def test_an_operator_in_the_workstream_and_an_admin_still_can():
    db = _fresh()
    try:
        cid = _operator_cell(db)
        teammate = _user(db, OPERATOR, workgroups=("ops",), name="ops-teammate")
        assert _status(lambda: agentcell.revoke_agent(cid, db=db, current_user=teammate)) == 200
        cid2 = _operator_cell_again(db)
        admin = _user(db, {}, workgroups=(), name="root", admin=True)
        assert _status(lambda: agentcell.revoke_agent(cid2, db=db, current_user=admin)) == 200
    finally:
        db.close()


def _operator_cell_again(db):
    owner = db.query(User).filter(User.username == "ops-operator").first()
    target = db.query(User).filter(User.username == "ops-agent-user").first()
    lab = _lab(db, owner.username)
    with _World(_host(db, workgroup="ops", name="worker-2")):
        return _mint(db, owner, lab_id=lab.id, pat_user_id=target.id, workgroup="ops",
                     name="watcher-2", host_ref="worker-2").id


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
