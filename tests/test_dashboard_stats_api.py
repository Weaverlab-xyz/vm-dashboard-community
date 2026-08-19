"""GET /api/dashboard/stats — the page's one read. RBAC must be byte-identical per tile.

This endpoint replaces ~33 per-tile requests with one, so it now owns the filtering that was
spread across a dozen list endpoints. Getting that wrong is not a performance bug: it either
shows an operator resources they may not see, or hides ones they own.

The subtle part, and the reason this file exists: **the app has two different admin rules,
and they must not be unified here.** The four cloud modules key on `user.is_admin`;
inventory, databases and k8s key on `user.is_effective_admin`, which is a SUPERSET — it also
honours a session-permissions row and a live Entitle JIT grant. A JIT-granted admin
therefore already sees everything on /inventory and only their own workgroups on
/api/aws/instances. That predates this endpoint. Reproducing it tile by tile is correct;
"tidying" it would silently change somebody's access in a place nobody would look.

Also pinned:
  * the endpoint makes NO cloud call — asserted structurally, by scanning its imports,
    because a promise like that is worth enforcing rather than describing
  * a never-collected tile reports -1, never 0. 0 is a plausible number and renders as one
  * `as_of` is the OLDEST contributor and never now() — the api/vms.py correction
  * the five hypervisor tiles are deliberately ABSENT, so the client keeps its own fetcher
    rather than being handed a wrong number
  * one failing source degrades to one unavailable tile, not a 500 that blanks the page

Uses a temporary SQLite file and the real ORM, and calls the handler directly rather than
through TestClient: what is under test is the aggregation and the RBAC, not FastAPI.

Run: python tests/test_dashboard_stats_api.py   (or under pytest)
"""
import asyncio
import ast
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="dash-stats-api-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-dashboard-stats-api-tests")

try:
    from web_dashboard.database import (Base, DashboardStatCache, SessionLocal, engine)
    from web_dashboard.api import dashboard as api
    from web_dashboard.services import dashboard_stat_cache as store
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)


class _User:
    """Enough of a User for the accessors. `is_admin` and `is_effective_admin` are set
    independently on purpose — that difference is the point of several tests below."""

    def __init__(self, username="alice", is_admin=False, effective=None, workgroups=()):
        self.username = username
        self.is_admin = is_admin
        self.is_effective_admin = is_admin if effective is None else effective
        self.workgroups_list = list(workgroups)


def _reset():
    db = SessionLocal()
    try:
        db.query(DashboardStatCache).delete()
        db.commit()
    finally:
        db.close()


def _write(tile_key, payload, *, provider="aws", scope="", fetched_at=None):
    db = SessionLocal()
    try:
        row = DashboardStatCache(
            tile_key=tile_key, scope=scope, provider=provider,
            payload=json.dumps(payload), payload_version=store.PAYLOAD_VERSION,
            fetched_at=fetched_at or datetime.utcnow(), stale=False,
            consecutive_failures=0)
        db.add(row)
        db.commit()
    finally:
        db.close()


def _rows(*specs):
    """[(workgroup, state, region), ...] -> the collector's projection."""
    return {"rows": [{"workgroup": w, "state": s, "region": r} for w, s, r in specs]}


def _spec(key):
    from web_dashboard.services import dashboard_collect
    return next(t for t in dashboard_collect.TILES if t.key == key)


def _tile(key, user):
    """Run just the snapshot half for one tile — no DB tiles, no cost, no event loop."""
    db = SessionLocal()
    try:
        snaps = store.read_all(db)
        return api._from_snapshot(_spec(key), snaps.get(key, []), user, store._utcnow())
    finally:
        db.close()


# ── RBAC ──────────────────────────────────────────────────────────────────────

def test_a_non_admin_sees_only_their_own_workgroups():
    _reset()
    _write("aws_instances", _rows(("hydra", "running", "us-east-2"),
                                 ("hydra", "stopped", "us-east-2"),
                                 ("weaverlab", "running", "eu-west-1")))
    t = _tile("aws_instances", _User(workgroups=["hydra"]))
    assert t["value"] == 2, f"expected 2 hydra rows, got {t['value']}"
    assert t["secondary"] == 1, f"expected 1 running, got {t['secondary']}"
    assert set(t["by_region"]) == {"us-east-2"}, (
        f"the by-region line leaked another workgroup's region: {t['by_region']}")


def test_an_admin_sees_everything():
    _reset()
    _write("aws_instances", _rows(("hydra", "running", "us-east-2"),
                                 ("weaverlab", "running", "eu-west-1")))
    t = _tile("aws_instances", _User(is_admin=True))
    assert t["value"] == 2
    assert set(t["by_region"]) == {"us-east-2", "eu-west-1"}


def test_an_ownerless_row_is_admin_only():
    _reset()
    _write("aws_instances", _rows((None, "running", "us-east-2"),
                                  ("hydra", "running", "us-east-2")))
    assert _tile("aws_instances", _User(workgroups=["hydra"]))["value"] == 1, (
        "a row with no workgroup was shown to a non-admin — summarize_instances treats it "
        "as admin-only, and the aggregate must not widen that")
    assert _tile("aws_instances", _User(is_admin=True))["value"] == 2


def test_a_blank_workgroup_does_not_match_an_ownerless_row():
    # If the collector coerced a missing workgroup to "", a user in a workgroup named ""
    # would match it. It stores None for exactly this reason.
    _reset()
    _write("aws_instances", _rows((None, "running", "us-east-2")))
    assert _tile("aws_instances", _User(workgroups=[""]))["value"] == 0


def test_the_two_admin_rules_are_kept_apart():
    """A JIT-granted admin (is_effective_admin, not is_admin) must see the CLOUD tiles as a
    non-admin and the DB tiles as an admin — which is what the live endpoints already do."""
    _reset()
    _write("aws_instances", _rows(("hydra", "running", "us-east-2"),
                                  ("weaverlab", "running", "eu-west-1")))
    jit = _User(is_admin=False, effective=True, workgroups=["hydra"])

    assert _tile("aws_instances", jit)["value"] == 1, (
        "the cloud tile used is_effective_admin. api/aws.py::_accessible_workgroups keys on "
        "the raw is_admin column, so a JIT admin sees only their workgroups there — "
        "unifying the two rules here silently widens their access")

    # And the DB-tile side of the same user resolves through the effective rule.
    from web_dashboard.services import inventory_service
    assert inventory_service.accessible_workgroups(jit) is None, (
        "inventory_service.accessible_workgroups no longer honours is_effective_admin; the "
        "DB tiles in this endpoint would then narrow a JIT admin's view")


def test_every_rbac_tag_names_a_module_with_that_accessor():
    from web_dashboard.services import dashboard_collect
    for spec in dashboard_collect.TILES:
        if not spec.rbac:
            continue
        # A typo'd rbac tag falls through _accessible_for's else branch to None, which is
        # ADMIN — i.e. it fails open. That is the one failure mode worth a static check.
        assert api._accessible_for(spec.rbac, _User(workgroups=["x"])) == ["x"], (
            f"tile {spec.key!r} tags rbac={spec.rbac!r}, which _accessible_for does not "
            "know — it returns None for an unknown tag, and None means ADMIN. A typo here "
            "fails OPEN")


# ── unavailable vs zero ───────────────────────────────────────────────────────

def test_a_never_collected_tile_is_unavailable_not_zero():
    _reset()
    t = _tile("aws_instances", _User(is_admin=True))
    assert t["value"] == api.UNAVAILABLE, (
        f"a tile with no snapshot reported {t['value']!r}. It must be -1: 0 is a plausible "
        "number, renders as one, and is how five hypervisor tiles reported zero VMs for "
        "months without anyone noticing")
    assert t["status"] == "unavailable"


def test_a_genuinely_empty_cloud_reports_zero():
    _reset()
    _write("aws_instances", _rows())
    t = _tile("aws_instances", _User(is_admin=True))
    assert t["value"] == 0 and t["status"] == "ok", (
        "an account with no instances must read 0, not unavailable — otherwise a healthy "
        "empty install looks broken")


def test_a_stale_payload_version_reads_as_uncollected():
    _reset()
    _write("aws_instances", _rows(("hydra", "running", "us-east-2")))
    db = SessionLocal()
    try:
        db.query(DashboardStatCache).update({DashboardStatCache.payload_version: 999})
        db.commit()
    finally:
        db.close()
    assert _tile("aws_instances", _User(is_admin=True))["value"] == api.UNAVAILABLE, (
        "a payload written under an older shape was counted anyway")


# ── freshness ─────────────────────────────────────────────────────────────────

def test_as_of_is_the_oldest_contributor_and_never_now():
    _reset()
    old = datetime.utcnow() - timedelta(hours=3)
    new = datetime.utcnow() - timedelta(minutes=1)
    _write("hyperv_vms", _rows(("hydra", "running", "")), provider="hyperv",
           scope="conn-a", fetched_at=old)
    _write("hyperv_vms", _rows(("hydra", "running", "")), provider="hyperv",
           scope="conn-b", fetched_at=new)

    db = SessionLocal()
    try:
        snaps = store.read_all(db)
    finally:
        db.close()

    from web_dashboard.services.dashboard_collect import TileSpec
    fake = TileSpec("hyperv_vms", "hyperv", lambda: None, rbac="")
    t = api._from_snapshot(fake, snaps["hyperv_vms"], _User(is_admin=True), store._utcnow())

    assert t["value"] == 2, "per-scope rows must be summed, not overwritten"
    assert t["as_of"].startswith(old.replace(microsecond=old.microsecond).isoformat()[:16]), (
        f"as_of {t['as_of']} is not the OLDEST scope. A tile built from several connections "
        "is only as fresh as its stalest one — api/vms.py documents this exact correction, "
        "where cached_at used to be datetime.now(): true of the response, silent about the "
        "data")


# ── structural guarantees ─────────────────────────────────────────────────────

def test_the_endpoint_imports_nothing_that_can_dial_out():
    """'No cloud call' is a promise worth enforcing structurally.

    A future edit adding `from ..services import aws_service` to build "just one more tile"
    would put a live call back on the request path, and nothing else would catch it — the
    page would simply get slow again, which is exactly the regression this endpoint exists
    to prevent.
    """
    path = os.path.join(_ROOT, "web_dashboard", "api", "dashboard.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    banned = {"aws_service", "azure_service", "gcp_service", "oci_service",
              "proxmox_service", "nutanix_service", "vsphere_service", "hyperv_service",
              "xcpng_service", "portainer_service", "cost_service", "storage_service",
              "k8s_runner_service", "dashboard_collect_fetchers"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found |= {a.name for a in node.names} & banned
        elif isinstance(node, ast.Import):
            found |= {a.name.rsplit(".", 1)[-1] for a in node.names} & banned
    assert not found, (
        f"api/dashboard.py imports {sorted(found)} — those dial out. Every cloud number "
        "must come from dashboard_stat_cache, which the worker fills; there is no "
        "allow_fetch path here on purpose")


def test_the_cost_tile_never_triggers_a_query():
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "dashboard.py"),
               encoding="utf-8").read()
    assert "allow_fetch=False" in src, (
        "the cost tile must call cost_cache.get_summary(allow_fetch=False). The default is "
        "True, which lets it claim and query Cost Management from a page load — the "
        "throttle loop cost_cache was built to end")


def test_the_hypervisor_tiles_are_deliberately_absent():
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "dashboard.py"),
               encoding="utf-8").read()
    # Counting them here means a live WinRM/pyVmomi call in the request path, which is the
    # thing this endpoint removes. They wait for the per-connection scope.
    for key in ("proxmox_vms", "vsphere_vms", "hyperv_vms", "nutanix_vms", "xcpng_vms"):
        assert f'"{key}"' not in src, (
            f"{key} is answered here. Unless the connection is agent-backed that is a live "
            "hypervisor call on the request path — the client keeps its own fetcher for "
            "these until the collector writes per-connection scopes")


def test_one_failing_source_does_not_blank_the_page():
    _reset()
    from web_dashboard.services import inventory_service
    original = inventory_service.collect
    inventory_service.collect = lambda db: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        db = SessionLocal()
        try:
            tiles = api._db_tiles(db, _User(is_admin=True))
        finally:
            db.close()
    finally:
        inventory_service.collect = original

    assert tiles["deployed_resources"]["value"] == api.UNAVAILABLE, (
        "a failing source must degrade to ONE unavailable tile")
    assert tiles["deployed_resources"]["status"] == "unavailable"
    assert tiles["active_jobs"]["value"] == 0, (
        "one broken source took its neighbours with it — each is wrapped separately so the "
        "rest of the page still paints")


def test_the_response_shape_matches_what_the_client_renders():
    _reset()
    _write("aws_instances", _rows(("hydra", "running", "us-east-2")))
    db = SessionLocal()
    try:
        out = asyncio.run(api.dashboard_stats(db=db, current_user=_User(is_admin=True)))
    finally:
        db.close()

    assert set(out) >= {"tiles", "oldest_as_of", "stale", "generated_at"}
    for key in ("value", "secondary", "by_region", "as_of", "stale", "note", "status"):
        assert key in out["tiles"]["aws_instances"], f"tile is missing {key!r}"
    assert out["tiles"]["aws_instances"]["value"] == 1
    # oldest_as_of feeds the page's one "as of" label.
    assert out["oldest_as_of"], "no page-level as_of to render"


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
