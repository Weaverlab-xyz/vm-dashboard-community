"""Workgroups on cloud databases and Kubernetes clusters.

Both used to be **creator-scoped only**: `cloud_databases` and `k8s_clusters` carried a
`created_by` and no `workgroup`, so `inventory_service.visible_to` always took its
ownerless branch and a team could not share a database or a cluster the way it shares a
VM. Adding the column is the whole feature; the rule it feeds already existed.

What this file pins, in order of how badly it would hurt to lose:

1. **A tagged row reaches its workgroup, not only its creator.** Without this assertion
   the feature can ship as a no-op — every other visibility test in this suite exercises
   the creator branch, which is what the rule already did before the column existed.
2. **An untagged row is still creator-only.** Every row predating the column is NULL, so
   this is the property that made the change safe to deploy: nobody gained or lost access
   on upgrade. If it ever breaks, an install's entire existing estate changes visibility
   in one deploy.
3. **The workgroup branch OUTRANKS the creator branch.** Tagging a row into a workgroup
   you are not in hides it from you. That is why `resolve_for_tagging` refuses to do it,
   and this records the consequence rather than just the guard.
4. **A row with no `workgroup` key at all behaves exactly as before.** `cloud_functions`
   projections have no such key and must not change.
5. **Canonicalisation.** A workgroup stored as `"Team-A"` would be invisible to every
   member of `team-a`, including whoever typed it, because the comparison is lowercase.

Real throwaway SQLite for the service-layer half; plain dicts for the predicate.

Runs under pytest, or standalone:  python tests/test_workgroup_resource_tagging.py
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(tempfile.mkdtemp(), "wgtag.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

# Probe the optional third-party dep by NAME, then import first-party unguarded. A
# missing symbol must surface as a plain ImportError and fail this file loudly rather
# than print SKIP and exit 0 — see tests/test_import_guard_narrowness.py.
try:
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover - environmental
    print(f"SKIP: {exc}")
    sys.exit(0)

from web_dashboard.database import (Base, CloudDatabase, K8sCluster,  # noqa: E402
                                    SessionLocal, Workgroup, engine)
from web_dashboard.services import inventory_service as inv  # noqa: E402
from web_dashboard.services import workgroup_service as wgs  # noqa: E402

Base.metadata.create_all(bind=engine)


class _User:
    """Enough of a User for the two attributes the scoping helpers read."""

    def __init__(self, username, workgroups=(), effective_admin=False):
        self.username = username
        self.workgroups_list = list(workgroups)
        self.is_effective_admin = effective_admin


ALICE = _User("alice", ["team-a"])
BOB = _User("bob", ["team-b"])
CAROL = _User("carol", ["team-a", "team-b"])
ADMIN = _User("root", effective_admin=True)


# ── The predicate ─────────────────────────────────────────────────────────────

def _acc(user):
    return inv.accessible_workgroups(user)


def test_a_tagged_row_reaches_its_workgroup_not_only_its_creator():
    """THE test. Everything else here is a guard around this one sentence."""
    row = {"created_by": "alice", "workgroup": "team-a"}
    assert inv.row_visible_to(row, _acc(BOB), BOB.username) is False, (
        "bob is in team-b — he must not see a team-a row")
    assert inv.row_visible_to(row, _acc(CAROL), CAROL.username) is True, (
        "carol is in team-a and created nothing: this is the capability being added")


def test_an_untagged_row_is_still_creator_only():
    """The upgrade-safety property. Every row predating the column is NULL."""
    row = {"created_by": "alice", "workgroup": None}
    assert inv.row_visible_to(row, _acc(ALICE), ALICE.username) is True
    assert inv.row_visible_to(row, _acc(BOB), BOB.username) is False
    assert inv.row_visible_to(row, _acc(CAROL), CAROL.username) is False, (
        "membership of any workgroup must not widen an UNTAGGED row")


def test_the_workgroup_branch_outranks_the_creator_branch():
    """Tagging into a workgroup you are not in hides the row FROM YOU.

    This is the reason workgroup_service.resolve_for_tagging refuses that assignment
    rather than allowing it — see test_tagging_into_a_workgroup_you_are_not_in_is_refused.
    Recorded here as behaviour so that removing the guard shows up as a surprise, not a
    convenience."""
    row = {"created_by": "alice", "workgroup": "team-b"}
    assert inv.row_visible_to(row, _acc(ALICE), ALICE.username) is False, (
        "alice created this row but is not in team-b, so she cannot see it")


def test_a_row_with_no_workgroup_key_is_unchanged():
    """cloud_functions rows have no `workgroup` key. They must behave exactly as the old
    creator-only filter did — this is what lets the dashboard tiles and the MCP tools
    share one helper without widening functions."""
    rows = [{"created_by": "alice"}, {"created_by": "bob"}]
    for user, expected in ((ALICE, [rows[0]]), (BOB, [rows[1]]), (ADMIN, rows)):
        got = [r for r in rows if inv.row_visible_to(r, _acc(user), user.username)]
        assert got == expected, f"{user.username}: {got}"


def test_an_effective_admin_sees_everything():
    for wg in ("team-a", "team-b", None):
        row = {"created_by": "alice", "workgroup": wg}
        assert inv.row_visible_to(row, _acc(ADMIN), ADMIN.username) is True


def test_a_stored_workgroup_is_compared_case_insensitively():
    """A row written as "Team-A" must still reach team-a.

    `accessible_workgroups` lowercases the user's side; nothing lowercases the row's.
    A stored TitleCase value would therefore be invisible to the workgroup it names —
    including to whoever typed it. resolve_for_tagging prevents it being stored that way
    (see below), and row_visible_to normalises anyway, because old rows exist."""
    row = {"created_by": "alice", "workgroup": "Team-A"}
    assert inv.row_visible_to(row, _acc(CAROL), CAROL.username) is True


def test_row_visible_to_does_not_require_a_deployed_by_key():
    """The whole reason this helper exists rather than reusing visible_to directly: an
    inventory item spells the creator `deployed_by`, a service projection `created_by`.
    Feeding visible_to a service row would silently compare None to the username."""
    row = {"created_by": "alice", "workgroup": None}
    assert "deployed_by" not in row
    assert inv.row_visible_to(row, _acc(ALICE), ALICE.username) is True
    assert row == {"created_by": "alice", "workgroup": None}, "must not mutate its input"


# ── Tagging authorization ─────────────────────────────────────────────────────

def _seed_workgroups(db):
    db.query(Workgroup).delete()
    for name in ("team-a", "team-b", "default"):
        db.add(Workgroup(id=f"wg-{name}", name=name, display_name=name.title()))
    db.commit()


def test_blank_is_legal_and_means_untagged():
    """Decision on record: the workgroup is OPTIONAL on create. A POST without one must
    succeed and leave NULL, so every pre-existing programmatic caller (POV wiring, the
    Entitle adapters, this suite) keeps working without a 400."""
    db = SessionLocal()
    try:
        _seed_workgroups(db)
        for blank in ("", "   ", None):
            assert wgs.resolve_for_tagging(db, blank, user=ALICE) is None
            assert wgs.canonical_or_none(db, blank) is None
    finally:
        db.close()


def test_an_unknown_workgroup_is_refused():
    db = SessionLocal()
    try:
        _seed_workgroups(db)
        try:
            wgs.resolve_for_tagging(db, "nope", user=ADMIN)
        except wgs.WorkgroupError:
            pass
        else:
            raise AssertionError("an unknown workgroup must not be stored")
    finally:
        db.close()


def test_tagging_into_a_workgroup_you_are_not_in_is_refused():
    """And refused with the DISTINGUISHABLE error, so the route can answer 403 rather
    than 400 — a membership problem is not a malformed request."""
    db = SessionLocal()
    try:
        _seed_workgroups(db)
        try:
            wgs.resolve_for_tagging(db, "team-b", user=ALICE)
        except wgs.WorkgroupAccessError as e:
            assert "team-b" in str(e)
        else:
            raise AssertionError("alice is not in team-b and must be refused")
        # ...but the subclass still satisfies a caller that only catches the base type.
        assert issubclass(wgs.WorkgroupAccessError, wgs.WorkgroupError)
        # Her own workgroup is fine, and an admin may tag into any of them.
        assert wgs.resolve_for_tagging(db, "team-a", user=ALICE) == "team-a"
        assert wgs.resolve_for_tagging(db, "team-b", user=ADMIN) == "team-b"
    finally:
        db.close()


def test_the_canonical_lowercase_name_is_what_gets_stored():
    """Never the caller's string. See test_a_stored_workgroup_is_compared_case_
    insensitively for what a stored "Team-A" would cost."""
    db = SessionLocal()
    try:
        _seed_workgroups(db)
        assert wgs.resolve_for_tagging(db, "TEAM-A", user=ADMIN) == "team-a"
        assert wgs.canonical_or_none(db, "  Team-A  ") == "team-a"
    finally:
        db.close()


# ── The column reaches the row, and the row reaches the projection ────────────

def test_the_services_store_and_project_the_workgroup():
    """A round trip through the real model and the real projections, because a field that
    validates but never lands is the failure mode a request-model test cannot see."""
    from web_dashboard.services import cloud_database_service as cds
    from web_dashboard.services import k8s_service as k8s

    db = SessionLocal()
    try:
        _seed_workgroups(db)
        db.query(CloudDatabase).delete()
        db.query(K8sCluster).delete()
        db.add(CloudDatabase(id="d1", engine="postgres", cloud="aws",
                             status="available", created_by="alice",
                             workgroup="team-a"))
        db.add(K8sCluster(id="c1", cloud="gcp", name="prod", status="registered",
                          source="registered", created_by="alice",
                          workgroup="team-a"))
        db.commit()

        d = cds._serialize(db.query(CloudDatabase).filter(CloudDatabase.id == "d1").first())
        c = k8s._serialize(db.query(K8sCluster).filter(K8sCluster.id == "c1").first())
        assert d["workgroup"] == "team-a", d
        assert c["workgroup"] == "team-a", c

        # And an untagged row projects "" — falsy, which the predicate reads as untagged.
        db.add(CloudDatabase(id="d2", engine="mysql", cloud="gcp",
                             status="available", created_by="alice"))
        db.commit()
        d2 = cds._serialize(db.query(CloudDatabase).filter(CloudDatabase.id == "d2").first())
        assert d2["workgroup"] == "", d2
        assert inv.row_visible_to(d2, _acc(CAROL), CAROL.username) is False
        assert inv.row_visible_to(d, _acc(CAROL), CAROL.username) is True
    finally:
        db.close()


def test_the_request_models_declare_the_field():
    """Pydantic drops an undeclared key WITHOUT error, so a select the UI binds and the
    server never receives looks like a working form that quietly stores nothing. Four
    models take a workgroup; all four must declare it, and none may require it."""
    try:
        import pydantic  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover - environmental
        print(f"   (skipped: {exc})")
        return
    from web_dashboard.api.cloud_databases import (ProvisionRequest,
                                                   RegisterDatabaseRequest)
    from web_dashboard.models.k8s import (ClusterProvisionRequest,
                                          ClusterRegisterRequest)

    for model in (ProvisionRequest, RegisterDatabaseRequest,
                  ClusterProvisionRequest, ClusterRegisterRequest):
        fields = getattr(model, "model_fields", None) or getattr(model, "__fields__")
        assert "workgroup" in fields, f"{model.__name__} would silently drop it"
        field = fields["workgroup"]
        required = getattr(field, "is_required", None)
        if callable(required):
            assert required() is False, f"{model.__name__}.workgroup must be optional"


def test_expiry_stamping_honours_an_exempt_workgroup():
    """Threaded so an exempt resource is never STAMPED, not merely skipped at reap time.
    Stamping and then skipping leaves a countdown the sweeper intends to ignore, and an
    operator reading "deletes in 6h" beside "exempt" cannot tell which wins."""
    from web_dashboard.services import expiry_policy as ep

    real_enabled, real_hours, real_exempt = ep.enabled, ep.default_hours, ep.exempt_workgroups
    ep.enabled = lambda: True
    ep.default_hours = lambda: 8
    ep.exempt_workgroups = lambda: frozenset({"platform"})
    try:
        for kind in ("database", "k8s"):
            assert ep.default_expiry_for_kind(kind, workgroup="platform") is None, kind
            assert ep.default_expiry_for_kind(kind, workgroup="PLATFORM") is None, kind
            assert ep.default_expiry_for_kind(kind, workgroup="team-a") is not None, kind
            # The no-workgroup call every pre-existing caller makes is unchanged.
            assert ep.default_expiry_for_kind(kind) is not None, kind
    finally:
        ep.enabled, ep.default_hours, ep.exempt_workgroups = real_enabled, real_hours, real_exempt


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
