"""The home page reads one endpoint, and Refresh must not rebuild the fan-out.

`init()` used to fire four un-awaited section refreshes, each a `Promise.all` over its
tiles, putting ~22 requests in flight at once — every one holding a pooled connection for
its whole duration against `pool_size=5 + max_overflow=5`. That is what produced
`QueuePool limit of size 5 overflow 5 reached` on every authenticated page while /login
stayed fast, and no amount of caching fixes it: a cache hit is still a request, still a
connection, still a slot.

Pinned here:
  * `init()` makes ONE snapshot call, not four section fan-outs
  * every tile the aggregate answers has had its client fetcher removed, with no dangling
    reference left behind — two live paths for one tile is how the warmer/reader drift in
    main.py's comment block happened
  * the five hypervisor tiles KEEP their fetchers. They are a live host call unless the
    connection is agent-backed, and the aggregate makes no live calls by construction
  * Refresh triggers the collector; it does not re-fetch the live endpoints. Reverting to a
    per-tile fan-out from the one button an operator presses when the page already looks
    wrong is the cost-cache incident in miniature
  * the freshness label reads the server's as_of, not the time we fetched
  * a failed aggregate leaves tiles on 'unavailable', not spinning on '…' forever
  * the refresh endpoint honours its own minimum interval and never clears a cooldown

Template read as text; the endpoint by import. No DOM, no node — CI runs tests/test_*.py
only, so a .js guard here would never execute.

Run: python tests/test_dashboard_client_switch.py   (or under pytest)
"""
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="dash-client-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-dashboard-client-tests")


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


DASHBOARD = _read("web_dashboard", "templates", "dashboard.html")

# Tiles the aggregate answers, so the client must NOT also fetch them itself.
SERVER_ANSWERED = (
    "aws_instances", "aws_amis", "azure_vms", "azure_images",
    "gcp_instances", "gcp_images", "oci_instances", "oci_images",
    "registered_images", "cloud_cost", "cloud_databases", "k8s_clusters",
    "workstation_vms", "gateways", "ecs_tasks", "aci_containers",
    "gce_containers", "cloud_run_jobs", "rancher_nodes", "portainer_node",
    "portainer_endpoints", "active_jobs", "deployed_resources",
)

# Tiles that keep their own fetcher — a live host call unless agent-backed.
CLIENT_FETCHED = ("proxmox_vms", "vsphere_vms", "hyperv_vms", "nutanix_vms", "xcpng_vms")

# Fetchers the aggregate replaced. A leftover is two live paths for one tile.
REMOVED = ("_fetchCloudStats", "_refreshCloudSection", "_fetchCost", "_fetchOciCustomImages",
           "_fetchGceInstances", "_fetchPortainer", "_fetchWorkstationVms",
           "loadInventoryCount")


def _js(name):
    """A method body lifted out of the template, braces balanced."""
    m = re.search(r"\n[ \t]*(?:async[ \t]+)?" + re.escape(name) + r"\s*\([^)]*\)\s*\{",
                  DASHBOARD)
    assert m, f"dashboard.html: {name} not found"
    open_brace = DASHBOARD.index("{", m.start())
    depth, end = 0, -1
    for j in range(open_brace, len(DASHBOARD)):
        if DASHBOARD[j] == "{":
            depth += 1
        elif DASHBOARD[j] == "}":
            depth -= 1
            if depth == 0:
                end = j
                break
    assert end != -1, f"unbalanced braces in {name}"
    return DASHBOARD[m.start():end + 1]


def _code_only(js):
    """`js` minus // comments and /* */ blocks.

    An absence assertion over raw source also matches the comment explaining why the thing
    was removed, so a correct fix fails its own test. That has bitten this repo repeatedly.
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in js.splitlines())


# ── the switch ────────────────────────────────────────────────────────────────

def test_init_makes_one_snapshot_call_not_four_section_fanouts():
    body = _code_only(_js("init"))

    assert "loadSnapshot()" in body, (
        "init() does not load the snapshot — the page would render every tile as loading")

    # The four un-awaited section refreshes are the ~22-request burst. Only the hypervisor
    # section may still be refreshed directly.
    refreshes = re.findall(r"refreshSection\(([^)]*)\)", body)
    assert len(refreshes) <= 1, (
        f"init() still fans out {len(refreshes)} sections: {refreshes}. That burst against "
        "a ten-connection pool is what produced the QueuePool timeouts")
    if refreshes:
        assert "hypervisors" in refreshes[0], (
            f"the one remaining fan-out is {refreshes[0]!r}, not the hypervisor section — "
            "everything else is answered by the aggregate")


def test_the_replaced_fetchers_are_gone_with_no_dangling_reference():
    code = _code_only(DASHBOARD)
    for name in REMOVED:
        assert name not in code, (
            f"{name} is still in dashboard.html. Leaving both paths live for one tile is "
            "exactly the warmer/reader drift main.py's comment block is about — and the "
            "dead one is the copy that silently rots")


def test_the_aggregate_tiles_have_no_client_fetcher():
    tile_dispatch = _code_only(_js("_fetchTile"))
    for key in SERVER_ANSWERED:
        assert f"'{key}'" not in tile_dispatch, (
            f"_fetchTile still has a branch for {key!r}, which /api/dashboard/stats "
            "answers. Two sources for one number will disagree")


def test_the_hypervisor_tiles_keep_their_fetchers():
    tile_dispatch = _js("_fetchTile")
    for key in CLIENT_FETCHED:
        assert f"'{key}'" in tile_dispatch, (
            f"{key!r} lost its fetcher but the aggregate does not answer it either — the "
            "tile would read 'unavailable' forever. These are a live WinRM/pyVmomi/XAPI "
            "call unless the connection is agent-backed, which is why they are not in the "
            "snapshot")


def test_refresh_triggers_the_collector_rather_than_a_live_fanout():
    body = _code_only(_js("forceRefresh"))
    assert "/api/dashboard/refresh" in body, (
        "Refresh does not ask the collector for a pass")
    # The failure worth guarding: someone "fixes" Refresh by calling the live endpoints.
    for dead in ("/api/aws/", "/api/azure/", "/api/gcp/", "/api/oci/", "/api/containers/"):
        assert dead not in body, (
            f"forceRefresh calls {dead} directly. Reverting to a live fan-out from the one "
            "button an operator presses when the page already looks wrong is the "
            "cost-cache incident in miniature: the tile errors, they click Refresh, and we "
            "issue more calls into the window already refusing them")
    # Its OWN flag, not snapshotLoading: forceRefresh polls loadSnapshot, and loadSnapshot
    # clears snapshotLoading in its finally — so sharing one flag re-enables the button on
    # the first poll iteration, mid-pass.
    assert "this.refreshing" in body, (
        "nothing disables the button while a forced pass runs, so it can be mashed")
    assert "snapshotLoading" not in body, (
        "forceRefresh guards on snapshotLoading, which loadSnapshot clears in its finally — "
        "the button re-enables itself on the first poll iteration")

    dash = _read("web_dashboard", "templates", "dashboard.html")
    assert ':disabled="refreshing"' in dash, "the button is not actually disabled"


def test_the_freshness_label_describes_the_data_not_the_response():
    body = _code_only(_js("sectionFreshness"))
    assert "snapshotAsOf" in body, (
        "the label no longer reads the server's as_of. A timestamp taken when we fetched is "
        "true of the RESPONSE and silent about the data — api/vms.py documents this exact "
        "correction, where cached_at used to be datetime.now()")
    assert "Date.now()" not in body, (
        "sectionFreshness is back to timing the fetch rather than reading the data's age")


def test_a_failed_aggregate_leaves_tiles_unavailable_not_loading():
    body = _js("loadSnapshot")
    catch = body[body.index("catch"):]
    assert "-1" in catch, (
        "a failed aggregate leaves every tile on null, which renders 'loading' forever. -1 "
        "renders 'unavailable', which is at least a state the page has a word for")


def test_the_badges_come_from_the_snapshot():
    body = _code_only(_js("loadSnapshot"))
    assert "by_workgroup" in body and "wg.count" in body, (
        "the workgroup badges have no source. They used to be computed by the client from "
        "the full /api/vms payload; the aggregate now sends per-workgroup counts on the "
        "workstation tile, so the badges cannot disagree with the number above them")


# ── the refresh endpoint ──────────────────────────────────────────────────────

def _api():
    try:
        from web_dashboard.database import Base, engine
        from web_dashboard.api import dashboard as api
        Base.metadata.create_all(bind=engine)
        return api
    except Exception as exc:  # pragma: no cover — app deps missing
        print(f"SKIP endpoint checks: {exc}")
        return None


def test_refresh_honours_its_minimum_interval():
    api = _api()
    if api is None:
        return
    import asyncio
    from web_dashboard.database import DashboardStatCache, SessionLocal
    from web_dashboard.services import dashboard_stat_cache as store

    class _U:
        username, is_admin, is_effective_admin = "a", True, True
        workgroups_list: list = []

    db = SessionLocal()
    try:
        db.query(DashboardStatCache).delete()
        db.add(DashboardStatCache(tile_key="aws_instances", scope="", provider="aws",
                                  payload='{"count": 1}',
                                  payload_version=store.PAYLOAD_VERSION,
                                  fetched_at=datetime.utcnow(), stale=False,
                                  consecutive_failures=0))
        db.commit()
        out = asyncio.run(api.dashboard_refresh(db=db, current_user=_U()))
        assert out["queued"] is False, (
            "a refresh seconds after a collection was queued anyway — the floor is what "
            "stops the button being mashed at a provider")
        assert "reason" in out, (
            "a declined refresh must say why, or the button looks broken and invites more "
            "clicking")

        # …and once the floor has passed, it queues.
        old = datetime.utcnow() - timedelta(seconds=store.min_refresh_interval_seconds() + 5)
        db.query(DashboardStatCache).update({DashboardStatCache.fetched_at: old})
        db.commit()
        out2 = asyncio.run(api.dashboard_refresh(db=db, current_user=_U()))
        assert out2["queued"] is True
    finally:
        db.close()


def test_refresh_marks_stale_rather_than_deleting():
    src = _read("web_dashboard", "api", "dashboard.py")
    body = src[src.index("async def dashboard_refresh("):]
    code = "\n".join(ln.split("#", 1)[0] for ln in body.splitlines())
    assert "mark_stale" in code, (
        "refresh does not mark rows stale, so a forced pass would see them as fresh and "
        "skip")
    for destructive in ("delete()", "invalidate("):
        assert destructive not in code, (
            f"refresh calls {destructive} — deleting first trades a working number for a "
            "maybe, which is what turned one throttle into a blank page in the cost cache")
    assert "clear_cooldowns" not in code, (
        "refresh clears cooldowns, so mashing the button compounds a provider's saturation. "
        "cooldown_until is a hard gate a forced refresh does not cross")


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
