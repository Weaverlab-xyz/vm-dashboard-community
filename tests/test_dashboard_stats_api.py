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
    from web_dashboard.database import (Base, CloudFunction, DashboardStatCache,
                                      SessionLocal, engine)
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

    def __init__(self, username="alice", is_admin=False, effective=None, workgroups=(),
                 pov_env_ids=None, permissions=None):
        self.username = username
        self.is_admin = is_admin
        self.is_effective_admin = is_admin if effective is None else effective
        self.workgroups_list = list(workgroups)
        # The two the POV tiles read. `pov_env_ids_list` is the per-instance grant and
        # `effective_permissions_dict` the feature-area one; an empty dict means
        # unrestricted, which is the pre-OIDC compatibility clause has_permission keeps.
        self.pov_env_ids_list = list(pov_env_ids or [])
        self.effective_permissions_dict = permissions or {}


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
              "k8s_runner_service", "dashboard_collect_fetchers",
              # The POV tiles are DB reads. These three describe the same rows and each
              # dials -- an enrolled agent, the PRA appliance, the lab platform -- so
              # reaching for one to enrich a tile is the plausible next mistake.
              "pov_broker", "pov_gateway", "pov_reconcile"}
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


def test_the_cloud_functions_tile_is_creator_scoped_and_counts_the_callable_ones():
    """A cloud_functions row carries no workgroup, so the only honest scope for a
    non-admin is the one they created — the same rule the databases and clusters tiles
    use. And `secondary` is the count with a live endpoint: a row still `deploying`, or
    one whose apply failed, cannot be invoked, so a tile reporting all three as usable
    would be the worse lie."""
    _reset()
    db = SessionLocal()
    try:
        db.query(CloudFunction).delete()
        db.add_all([
            CloudFunction(id="f1", name="a", workload="db_grant", cloud="gcp",
                          status="available", created_by="bob"),
            CloudFunction(id="f2", name="b", workload="db_grant", cloud="gcp",
                          status="deploying", created_by="bob"),
            CloudFunction(id="f3", name="c", workload="db_grant", cloud="gcp",
                          status="available", created_by="alice"),
        ])
        db.commit()

        admin = asyncio.run(api.dashboard_stats(
            db=db, current_user=_User(username="root", is_admin=True)))
        tile = admin["tiles"]["cloud_functions"]
        assert tile["value"] == 3 and tile["secondary"] == 2

        # A JIT-granted admin resolves through the EFFECTIVE rule here, like the other
        # two DB tiles — see test_the_two_admin_rules_are_kept_apart.
        jit = asyncio.run(api.dashboard_stats(
            db=db, current_user=_User(username="carol", is_admin=False, effective=True)))
        assert jit["tiles"]["cloud_functions"]["value"] == 3

        mine = asyncio.run(api.dashboard_stats(
            db=db, current_user=_User(username="bob")))
        tile = mine["tiles"]["cloud_functions"]
        assert tile["value"] == 2 and tile["secondary"] == 1
    finally:
        db.query(CloudFunction).delete()
        db.commit()
        db.close()


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



# ── the POV tiles ─────────────────────────────────────────────────────────────
#
# Two gates and a scope, all borrowed from api/pov.py rather than reinvented here — so
# the three tests below are really one question asked three ways: can this endpoint ever
# report a POV the /pov page would refuse to open?

def _pov_env(env_id, name, *, status="active", vms=()):
    """One POV and its guests. `vms` is [(name, pra_jump_id), ...]."""
    from web_dashboard.database import PovEnvironment, PovEnvironmentVM
    db = SessionLocal()
    try:
        db.add(PovEnvironment(id=env_id, platform="skytap", name=name, status=status,
                              created_by="alice", created_at=datetime.utcnow()))
        for vm_name, jump in vms:
            db.add(PovEnvironmentVM(environment_id=env_id, platform_vm_id=vm_name,
                                    name=vm_name, pra_jump_id=jump or ""))
        db.commit()
    finally:
        db.close()


def _pov_reset():
    from web_dashboard.database import PovEnvironment, PovEnvironmentVM
    db = SessionLocal()
    try:
        db.query(PovEnvironmentVM).delete()
        db.query(PovEnvironment).delete()
        db.commit()
    finally:
        db.close()


def _pov_tiles(user, *, enabled=True):
    """Just the POV half, with the feature flag forced rather than configured.

    The flag is masked off on an estate instance, which is the default this test database
    resolves to — so without forcing it every case below would collapse into the
    "not enabled" one and pass for the wrong reason.
    """
    from web_dashboard.services import feature_flags
    original = feature_flags.enabled
    feature_flags.enabled = lambda name, default=None: (
        enabled if name == "pov_environments_enabled" else original(name, default))
    db = SessionLocal()
    try:
        return api._pov_tiles(db, user)
    finally:
        db.close()
        feature_flags.enabled = original


def test_the_pov_tiles_honour_the_per_instance_grant():
    """A narrowed user must not be told how many POVs exist. `require_pov_env_access`
    closes that one route at a time and answers 404 rather than 403, precisely so an id
    is not worth guessing — a COUNT here would hand back what those 404s withhold."""
    _pov_reset()
    _pov_env("env-a", "alpha", vms=[("vm1", "jump-1"), ("vm2", "")])
    _pov_env("env-b", "bravo", vms=[("vm3", "jump-3")])
    _pov_env("env-c", "charlie")

    everything = _pov_tiles(_User(is_admin=True))
    assert everything["pov_active"]["value"] == 3
    assert everything["pov_guests"]["value"] == 3
    assert everything["pov_guests"]["secondary"] == 2, "wired counts the jump items"

    narrowed = _pov_tiles(_User(pov_env_ids=["env-a"]))
    assert narrowed["pov_active"]["value"] == 1, (
        "a user granted one POV is counting all of them")
    assert narrowed["pov_guests"]["value"] == 2, (
        "the guest tile counts guests of POVs this user cannot open")


def test_a_destroyed_pov_is_not_counted():
    """The /pov list filters them out, and so must the tile above it — otherwise the
    number on the home page and the rows on the page it links to disagree."""
    _pov_reset()
    _pov_env("env-a", "alpha")
    _pov_env("env-gone", "gone", status="destroyed")
    assert _pov_tiles(_User(is_admin=True))["pov_active"]["value"] == 1


def test_without_pov_read_every_pov_tile_is_forbidden_rather_than_a_number():
    """One missing permission must never blank the page, and must never leak a count."""
    _pov_reset()
    _pov_env("env-a", "alpha", vms=[("vm1", "jump-1")])
    tiles = _pov_tiles(_User(permissions={"aws": ["read"]}))
    for key in ("pov_active", "pov_guests", "pov_coverage"):
        assert tiles[key]["status"] == "forbidden", (
            f"{key} answered {tiles[key]['status']!r} for a user without pov:read")
        assert tiles[key]["value"] == api.UNAVAILABLE


def test_an_empty_permission_map_is_still_unrestricted():
    """The pre-OIDC compatibility clause in has_permission. Being stricter here than the
    rest of the app would hide POVs from users who can already open them."""
    _pov_reset()
    _pov_env("env-a", "alpha")
    assert _pov_tiles(_User(permissions={}))["pov_active"]["value"] == 1


def test_an_instance_that_runs_no_povs_reports_unavailable_never_zero():
    """0 is a plausible number and renders as one. "This instance does not do POVs" and
    "this POV instance has none right now" are different facts, and the tile has to be
    able to say which."""
    _pov_reset()
    tiles = _pov_tiles(_User(is_admin=True), enabled=False)
    for key in ("pov_active", "pov_guests", "pov_coverage"):
        assert tiles[key]["value"] == api.UNAVAILABLE, (
            f"{key} reports {tiles[key]['value']!r} on an instance with the feature off")
        assert tiles[key]["status"] == "unavailable"

    # ...and with the feature ON and no POVs yet, zero IS the honest answer.
    live = _pov_tiles(_User(is_admin=True))
    assert live["pov_active"]["value"] == 0
    assert live["pov_active"]["status"] == "ok"


def test_a_failing_pov_source_degrades_to_its_own_tile():
    """Same rule as every other source here: one failure is one unavailable tile, never a
    500 that blanks a page whose other twenty tiles were fine."""
    _pov_reset()
    _pov_env("env-a", "alpha", vms=[("vm1", "jump-1")])
    from web_dashboard.services import pov_use_cases
    original = pov_use_cases.summary_for
    pov_use_cases.summary_for = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        tiles = _pov_tiles(_User(is_admin=True))
    finally:
        pov_use_cases.summary_for = original

    assert tiles["pov_coverage"]["value"] == api.UNAVAILABLE
    assert tiles["pov_active"]["value"] == 1, (
        "the coverage tile failing took the environment count with it")
    assert tiles["pov_guests"]["value"] == 1


def test_the_coverage_denominator_counts_only_what_this_pov_can_run():
    """A POV wired into one product has most of the catalog out of scope. Reporting
    "3 of 32" against it would read as an evaluation going badly rather than a scoped one,
    which is why the denominator travels as text rather than as a share of the catalog."""
    _pov_reset()
    _pov_env("env-a", "alpha", vms=[("vm1", "jump-1")])
    tile = _pov_tiles(_User(is_admin=True))["pov_coverage"]
    assert isinstance(tile["secondary"], str) and "in scope" in tile["secondary"], (
        "the coverage secondary is not the free-form in-scope denominator")

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
