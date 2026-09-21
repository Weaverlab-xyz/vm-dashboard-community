"""Who can see an agent cell, exercised against a real listing rather than its source.

**The rule is workgroup OR creator**, and the second term is not decoration. It is the
Certificate and SPIRE labs' rule, and this cell needs it for a reason those two do not
share with the cloud pages: the workgroup is OPTIONAL here, where a cloud deploy form
requires one. Filtering on workgroup alone therefore made a blank field mean
"administrators only" — so a non-admin who left it empty minted an agent that vanished
from their own listing, on the one screen that had just shown them its token for the
only time. The Revoke button for a credential they had personally issued was then out of
reach, which is the exact inversion of what this cell exists to demonstrate.

Behavioural, not AST, and deliberately so. Every other agent-cell suite reads source
because it is asserting the ABSENCE of something a refactor could add back. This one
asserts a positive: three identities, three answers. A source check would have passed
against the broken version — the filter it now needs was one `and` clause away from the
one that was there, and both read fine.

Runs under pytest, or standalone:
    python tests/test_agentcell_visibility.py
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# A file-backed SQLite DB, set before web_dashboard.database is imported so the engine
# binds to it. Not :memory: — the app opens more than one connection.
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="agentcell-vis-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-agentcell-visibility")

# The third-party deps are probed BY NAME and the first-party imports are unguarded,
# which is the rule tests/test_import_guard_narrowness.py enforces. A wider handler
# cannot tell "this machine has no fastapi" (ModuleNotFoundError, skipping is right)
# from "AgentCell no longer exists" (plain ImportError, the file is broken) — and CI runs
# each file standalone, so the second would print SKIP and exit 0 forever.
try:
    import fastapi  # noqa: F401
    import fastapi.testclient  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web_dashboard.database import (  # noqa: E402
    AgentCell, Base, SessionLocal, engine, get_db)
from web_dashboard.api import agentcell as agentcell_api  # noqa: E402
from web_dashboard.api.auth import get_current_user  # noqa: E402

Base.metadata.create_all(bind=engine)


class _User:
    """Enough of a principal for the listing. `_accessible_workgroups` reads `is_admin`
    and `workgroups_list`; the creator term reads `username`."""

    def __init__(self, username, workgroups=(), is_admin=False):
        self.username = username
        self.workgroups_list = list(workgroups)
        self.is_admin = is_admin
        self.is_effective_admin = is_admin


_CALLER = {"user": None}


def _client() -> TestClient:
    """Only the agentcell router, so this does not depend on app startup."""
    app = FastAPI()
    app.include_router(agentcell_api.router)

    def _db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    # Overrides the BASE dependency rather than the permission factory:
    # `require_permission` returns a fresh function per call, so an override keyed on it
    # matches nothing and every route answers "Not authenticated". The listing takes
    # `get_current_user` directly anyway, which is the identity this file is about.
    app.dependency_overrides[get_current_user] = lambda: _CALLER["user"]
    return TestClient(app)


CLIENT = _client()


def _seed(name, created_by, workgroup):
    db = SessionLocal()
    try:
        row = AgentCell(name=name, status="provisioning", created_by=created_by,
                        workgroup=workgroup)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _names_seen_by(user):
    _CALLER["user"] = user
    resp = CLIENT.get("/api/agentcell/agents")
    assert resp.status_code == 200, resp.text
    return {a["name"] for a in resp.json()["agents"]}


# The fixtures, seeded once: an untagged agent, one tagged to a workgroup, and one
# belonging to somebody else entirely.
_UNTAGGED = "vis-untagged"
_TAGGED = "vis-tagged"
_OTHERS = "vis-someone-elses"
_seed(_UNTAGGED, created_by="minter", workgroup=None)
_seed(_TAGGED, created_by="minter", workgroup="platform")
_seed(_OTHERS, created_by="stranger", workgroup="finance")


def test_the_minter_sees_their_own_untagged_agent():
    """THE REGRESSION. Blank workgroup, non-admin creator — this returned nothing, and
    the row it hid was one whose token the caller had just been shown once."""
    seen = _names_seen_by(_User("minter", workgroups=[]))
    assert _UNTAGGED in seen, (
        "the person who minted an untagged agent cannot see it. Its token was shown "
        "exactly once and its Revoke button is on that row — this is a credential they "
        "issued and can no longer reach")


def test_a_stranger_does_not_see_an_untagged_agent():
    """The creator term restores a row to ONE person. It must not open it to everybody,
    or "leave the workgroup blank" would quietly mean "share with the instance"."""
    seen = _names_seen_by(_User("nobody", workgroups=[]))
    assert _UNTAGGED not in seen and _TAGGED not in seen and _OTHERS not in seen, (
        f"a user who neither minted nor shares a workgroup sees {sorted(seen)}")


def test_a_workgroup_member_sees_an_agent_they_did_not_mint():
    """Tagging is still how an agent is shared — the creator term did not replace it."""
    seen = _names_seen_by(_User("colleague", workgroups=["platform"]))
    assert _TAGGED in seen, "workgroup scoping stopped working"
    assert _UNTAGGED not in seen, (
        "an untagged agent leaked to a workgroup member who did not mint it")


def test_the_workgroup_match_is_case_insensitive():
    """`_accessible_workgroups` lowercases and the row may not be, which is why the
    comparison lowercases the row too. A case-sensitive match would hide a shared agent
    from the group it was shared with."""
    assert _TAGGED in _names_seen_by(_User("colleague", workgroups=["PLATFORM"]))


def test_an_admin_sees_everything():
    seen = _names_seen_by(_User("root", workgroups=[], is_admin=True))
    for name in (_UNTAGGED, _TAGGED, _OTHERS):
        assert name in seen, f"an admin cannot see {name}"


def test_the_creator_term_does_not_widen_another_workgroup():
    """Minting does not grant a workgroup. Somebody who created NOTHING in `finance`
    must not reach `finance` rows just because they created something else."""
    seen = _names_seen_by(_User("minter", workgroups=[]))
    assert _OTHERS not in seen, "creating one agent granted sight of another user's"


# ── the home-page tile counts what its own link will show ────────────────────

def test_the_tile_and_the_listing_agree_for_every_identity():
    """A tile that counts rows its own link will not show reads as data disappearing.

    Both go through ``agentcell_service.visible_to`` for exactly this reason, so this is
    the assertion that keeps them one function rather than two that happen to match
    today. Checked per identity, because the shapes only diverge for non-admins — an
    admin sees everything either way, which is how a scoping bug hides from whoever is
    most likely to be looking.
    """
    from web_dashboard.services import agentcell_service
    from web_dashboard.api.gcp import _accessible_workgroups
    from web_dashboard.database import AgentCell

    for user in (_User("minter", workgroups=[]),
                 _User("colleague", workgroups=["platform"]),
                 _User("nobody", workgroups=[]),
                 _User("root", workgroups=[], is_admin=True)):
        listed = _names_seen_by(user)
        db = SessionLocal()
        try:
            accessible = _accessible_workgroups(user)
            counted = {r.name for r in db.query(AgentCell).all()
                       if agentcell_service.visible_to(r, accessible, user.username)}
        finally:
            db.close()
        assert counted == listed, (
            f"{user.username}: the tile would count {sorted(counted)} and the page it "
            f"links to lists {sorted(listed)}")


def test_the_tiles_secondary_counts_authorization_not_installation():
    """`authorized` is the count of tokens that would still be accepted.

    NOT `wired`, which is what the OT tile's secondary means and what the obvious
    copy-paste would have used here. `is_wired` reads `stages_done`, and nothing writes
    it — the two install playbooks are runs the operator makes, so it would report 0
    forever beside a working agent. `token_live` is the narrower thing the row actually
    knows, and total-minus-it is how many of these principals have stopped.
    """
    from datetime import datetime, timedelta
    from web_dashboard.services import agentcell_service as A

    now = datetime.utcnow()

    class _Row:
        def __init__(self, **kw):
            self.pat_revoked_at = kw.get("revoked")
            self.pat_expires_at = kw.get("expires")

    assert A.token_live(_Row(expires=now + timedelta(hours=1)), now) is True
    assert A.token_live(_Row(expires=now - timedelta(hours=1)), now) is False, \
        "a lapsed token still counts as authorized"
    assert A.token_live(_Row(expires=now + timedelta(hours=1),
                             revoked=now - timedelta(minutes=1)), now) is False, \
        "a REVOKED token still counts as authorized — which is the demo's closing beat, " \
        "so the tile would contradict the thing it is there to show"
    assert A.token_live(_Row(expires=None), now) is False, \
        "a row with no expiry counts as live; the generous reading is the wrong one here"
    # And the tile must not be tempted back to the field that cannot work.
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "dashboard.py"),
               encoding="utf-8").read()
    body = src.split("def _agent_cells():", 1)[1].split('_safe("agent_cells"', 1)[0]
    body = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert "is_wired" not in body and "stages_done" not in body, (
        "the agent_cells tile reads a stage field nothing writes, so its secondary "
        "would be 0 forever")


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
