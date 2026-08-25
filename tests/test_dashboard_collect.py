"""The collector's spec table must match the dashboard's tile catalog, in both directions.

A tile with no spec is never collected, so it reads `–` / "unavailable" forever — and that
is a *designed* state, indistinguishable from a throttled cloud API. This repo has already
shipped one tile pointed at an endpoint nobody wrote; it rendered "unavailable" from the
day it shipped and nobody reported it, because there is no server-side error, no log line
and no failing test. The parity scan below is the cheap guard against the same class.

The other direction matters too: a spec for a tile the page does not show is a provider
call nobody looks at, made on a schedule, forever.

Also pinned:
  * only CLOUD-BACKED tiles are collected. The DB-backed ones (jobs, inventory, gateways,
    databases, k8s clusters, workstation, registry) are indexed reads the read endpoint
    does inline in the session it already has; collecting them would add writes to buy
    nothing and would force this module to re-encode their RBAC — databases and k8s
    clusters are creator-scoped, not workgroup-scoped.
  * every spec's `feature` is a real key in the map /api/features serves. A gate on a
    misspelled key is permanently false, so that tile silently never collects. This repo
    has that bug class on record (`get_bool` against a nonexistent config field).
  * providers are collected concurrently but their tiles sequentially. GCP owns five specs
    against an 8-thread pool, so a fully-gathered pass would hand itself CloudProviderBusy.
  * no fetcher returns an empty payload to signal failure — an empty count is a number, and
    the store would happily write it over a working one.

Reads the template as text and the collector by import. No DOM, no app, no cloud.

Run: python tests/test_dashboard_collect.py   (or under pytest)
"""
import ast
import inspect
import os
import re
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# A FILE, not ":memory:". database.py gives SQLite a NullPool — a fresh connection per
# Session — so an in-memory URL hands every caller its own empty database and anything that
# reads app_config (which feature_map does, via config_service) fails with "no such table".
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="dash-collect-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-dashboard-collect-tests")

try:
    from web_dashboard.database import Base, engine
    from web_dashboard.services import dashboard_collect as dc
    Base.metadata.create_all(bind=engine)
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


DASHBOARD = _read("web_dashboard", "templates", "dashboard.html")

# Tiles the page shows that are NOT collected, and why. Every one is either an indexed read
# against the dashboard's own database or already has its own durable store. Listing them
# explicitly is what makes the parity scan below meaningful — otherwise "not in TILES" has
# two possible meanings and the test cannot tell them apart.
NOT_COLLECTED = {
    "active_jobs":        "indexed COUNT on jobs; also the one number an operator watches move",
    "deployed_resources": "inventory_service.collect — dashboard DB only",
    "registered_images":  "registered_images table",
    "cloud_databases":    "dashboard DB, creator-scoped",
    "k8s_clusters":       "dashboard DB, creator-scoped",
    "workstation_vms":    "HypervisorVMCache rows an agent synced",
    "gateways":           "gateway registry table (reconcile=false on the tile)",
    "ot_cells":           "Job rows (the cell's deploy child IS its inventory record)",
    "cloud_cost":         "already durable — services/cost_cache",
    # The hypervisor listings are agent-synced DB reads when conn.via_agent, and a live
    # call otherwise. Collecting them needs the per-connection `scope` the table reserves
    # but nothing writes yet, so they stay on the read endpoint's inline path for now.
    "proxmox_vms":        "per-connection; see the scope column",
    "vsphere_vms":        "per-connection; see the scope column",
    "hyperv_vms":         "per-connection; see the scope column",
    "nutanix_vms":        "per-connection; see the scope column",
    "xcpng_vms":          "per-connection; see the scope column",
}


def _tile_keys():
    """Every tile key declared in dashboard.html's tileSections."""
    start = DASHBOARD.index("tileSections:")
    end = DASHBOARD.index("\n      ],", start)
    block = DASHBOARD[start:end]
    return {m.group(1) for m in re.finditer(r"\{\s*key:\s*'([a-z_0-9]+)'", block)}


def test_every_collected_tile_is_a_real_tile():
    page = _tile_keys()
    assert page, "no tiles parsed out of dashboard.html — the scan is broken"
    orphans = {t.key for t in dc.TILES} - page
    assert not orphans, (
        f"collecting tiles the dashboard does not show: {sorted(orphans)} — that is a "
        "provider call on a schedule that nobody ever looks at")


def test_every_cloud_backed_tile_is_collected():
    page = _tile_keys()
    specced = {t.key for t in dc.TILES}
    unaccounted = page - specced - set(NOT_COLLECTED)
    assert not unaccounted, (
        f"tiles with neither a collector spec nor an entry in NOT_COLLECTED: "
        f"{sorted(unaccounted)}. A tile with no spec is never collected, so it renders "
        "'unavailable' forever — and that is a designed state, indistinguishable from a "
        "throttled cloud API. Add a spec, or say here why it does not need one")


def test_the_not_collected_list_stays_honest():
    page = _tile_keys()
    stale = set(NOT_COLLECTED) - page
    assert not stale, (
        f"NOT_COLLECTED names tiles the page no longer has: {sorted(stale)} — a stale "
        "exemption list is how the parity scan above stops catching anything")
    both = set(NOT_COLLECTED) & {t.key for t in dc.TILES}
    assert not both, f"listed as not-collected but has a spec: {sorted(both)}"


def test_every_feature_gate_is_a_real_feature_key():
    from web_dashboard.services import feature_flags
    keys = set(feature_flags.feature_map())
    bad = {t.key: t.feature for t in dc.TILES if t.feature and t.feature not in keys}
    assert not bad, (
        f"tiles gating on a key /api/features does not serve: {bad}. A gate on a "
        "nonexistent key is permanently false, so that tile silently never collects — the "
        "same shape as a get_bool against a config field that was never declared")


def test_each_tile_has_a_lock_key_for_its_provider():
    from web_dashboard.services import dashboard_stat_cache as store
    missing = {t.key: t.provider for t in dc.TILES
               if t.provider not in store._PROVIDER_LOCK_KEYS}
    assert not missing, (
        f"providers with no advisory-lock key: {missing}. _try_provider_lock refuses an "
        "unknown provider, so those tiles would never be claimed by anyone")


def test_tile_keys_are_unique():
    keys = [t.key for t in dc.TILES]
    assert len(keys) == len(set(keys)), (
        "duplicate tile keys — the second spec would overwrite the first's row every pass")


# ── payload shape ─────────────────────────────────────────────────────────────

def test_rows_payload_normalises_running_state():
    # The collector knows which field this provider uses; the reader must not have to.
    aws = dc.rows_payload([{"workgroup": "hydra", "state": "running", "region": "us-east-2"}],
                          state_field="state", region_field="region")
    gcp = dc.rows_payload([{"workgroup": "hydra", "status": "RUNNING", "region": "us-east1"}],
                          state_field="status", region_field="region")
    oci = dc.rows_payload([{"workgroup": "hydra", "lifecycle_state": "RUNNING"}],
                          state_field="lifecycle_state")
    for name, p in (("aws", aws), ("gcp", gcp), ("oci", oci)):
        assert p["rows"][0]["state"] == "running", (
            f"{name} running-state did not normalise: {p['rows'][0]['state']!r}. The read "
            "endpoint calls cloud_stats.summarize_instances with ONE field name; if the "
            "collector does not normalise, that becomes a per-provider predicate in a "
            "second place")


def test_rows_payload_keeps_the_rbac_dimensions_and_drops_everything_else():
    p = dc.rows_payload(
        [{"workgroup": "hydra", "state": "running", "region": "us-east-2",
          "instance_id": "i-123", "key_name": "k", "public_ip": "1.2.3.4"}],
        state_field="state", region_field="region")
    row = p["rows"][0]
    assert set(row) == {"workgroup", "state", "region"}, (
        f"projection carries {sorted(row)} — workgroup and region are what RBAC and the "
        "by-region line need; anything else is payload nobody reads, stored per row")
    assert row["workgroup"] == "hydra", "workgroup is what non-admin filtering matches on"


def test_a_missing_workgroup_survives_as_none():
    # summarize_instances treats a None workgroup as admin-only, by never matching it
    # against a caller's list. Coercing it to "" here would make it match a blank entry.
    p = dc.rows_payload([{"state": "running"}], state_field="state")
    assert p["rows"][0]["workgroup"] is None, (
        "an ownerless row must stay ownerless — it is admin-only, and a '' would compare "
        "equal to a blank workgroup name")


def test_count_payload_counts_running_only_when_asked():
    assert dc.count_payload([1, 2, 3]) == {"count": 3}
    p = dc.count_payload([{"s": "RUNNING"}, {"s": "STOPPED"}],
                         running=dc._upper_running("s"))
    assert p == {"count": 2, "running": 1}


# ── structure ─────────────────────────────────────────────────────────────────

def test_no_fetcher_signals_failure_with_an_empty_payload():
    # An empty count is a NUMBER. If a fetcher returned {"count": 0} on an error, the store
    # would write it as a success over a working value — which is the exact tile bug class
    # this whole change exists to remove.
    for spec in dc.TILES:
        src = inspect.getsource(spec.fetch)
        tree = ast.parse(src.lstrip())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            if isinstance(node.value, ast.Dict) and not node.value.keys:
                raise AssertionError(
                    f"{spec.key}'s fetcher returns a bare {{}} — a failure must raise "
                    "TileUnavailable, never return an empty payload")
            if isinstance(node.value, ast.Constant) and node.value.value is None:
                raise AssertionError(
                    f"{spec.key}'s fetcher returns None — the store reads None as 'this "
                    "attempt failed', so returning it deliberately hides a real answer")


def test_fetchers_import_the_api_layer_lazily():
    # A services -> api import at module scope is both a cycle risk and a cloud-SDK import
    # in the worker's startup path. It is also how a collector ends up with its own copy of
    # a fetch instead of the route's.
    src = _read("web_dashboard", "services", "dashboard_collect.py")
    module_level = [ln for ln in src.splitlines()
                    if ln.startswith("from ..api") or ln.startswith("import ..api")]
    assert not module_level, (
        f"module-scope api imports: {module_level} — keep them inside the fetchers")
    assert "from ..api" in src, (
        "no fetcher delegates to the api layer at all; a collector holding its own copy of "
        "a fetch drifts silently — see main.py's warmer comment block")


def test_the_collector_never_holds_a_session_across_a_provider_call():
    # Three phases, three short sessions. Holding a pooled connection across a cloud call
    # on a 5+5 pool is the exhaustion failure this whole change exists to fix; a collector
    # that did it would be indistinguishable from the bug.
    src = inspect.getsource(dc._collect_tile)
    body = src[src.index('"""', src.index('"""') + 3) + 3:]
    assert body.count("SessionLocal()") == 2, (
        "expected exactly two sessions in _collect_tile — one to claim, one to record — "
        f"found {body.count('SessionLocal()')}")
    claim_at, fetch_at = body.index("store.claim"), body.index("spec.fetch()")
    close_at = body.index("db.close()")
    assert claim_at < close_at < fetch_at, (
        "the claim session is still open when the provider is called — that is a pooled "
        "connection held across a network call")


def test_providers_run_concurrently_but_their_tiles_sequentially():
    src = inspect.getsource(dc.collect_once)
    assert "asyncio.gather" in src, "providers must not be collected one after another"
    # The per-provider body must be a sequential comprehension, not another gather.
    inner = inspect.getsource(dc.collect_once)
    inner = inner[inner.index("async def _one_provider"):]
    inner = inner[:inner.index("\n\n")] if "\n\n" in inner else inner
    assert "gather" not in inner, (
        "tiles within one provider are gathered — GCP owns five specs against an 8-thread "
        "pool, so a fully-gathered pass hands itself CloudProviderBusy and marks its own "
        "tiles failed")


def test_the_tile_deadline_is_bounded_and_under_the_lease():
    from web_dashboard.services import dashboard_stat_cache as store
    d = dc._tile_deadline()
    assert 5 <= d <= 45, f"tile deadline {d}s is outside the sane band"
    assert d < store.lease_seconds(), (
        "the per-tile deadline must be under the lease, or a hung provider holds its claim "
        "until the lease expires rather than releasing it cleanly")


def test_the_collector_is_not_a_job_type():
    worker = _read("web_dashboard", "jobs_worker.py")
    assert "dashboard_collect.collect_loop()" in worker, (
        "the worker no longer starts the collector — nothing writes the snapshot")
    assert "dash-stats" in worker, "the peer task should be named for the logs"
    # Every HANDLED_TYPES entry must be in exactly one tier tuple; an untiered type raises
    # in the supervisor and takes the loop down for every job.
    for bad in ('"dashboard_stats"', '"dashboard_collect"', '"stats_collect"'):
        assert bad not in worker, (
            f"{bad} looks like a job type. This must stay a PEER of the job runner: a job "
            "would need a tier, would spend a LIGHT slot belonging to real work, and would "
            "write a jobs row every 60s")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
