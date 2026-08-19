"""Collects the dashboard's cloud-backed tile counts into ``dashboard_stat_cache``.

One spec table and one pass. ``dashboard_stat_cache`` owns *whether we may ask*; this
module owns *what to ask and how to shape the answer*.

WHICH TILES ARE HERE
--------------------
Only the ones that cost a **provider call**. The dashboard's other tiles — active jobs,
deployed resources, the image registry, gateways, databases, k8s clusters, workstation VMs
— are indexed reads against the dashboard's own database, and their cost was never the
query: it was being one of ~22 separate HTTP requests, each holding its own pooled
connection. Those get counted inline by the read endpoint, in the one session it already
has. Collecting them here would add DB writes to buy nothing, and would force this module
to re-encode their RBAC (databases and k8s clusters are creator-scoped, not
workgroup-scoped) instead of leaving it where it lives.

Cost is not here either: ``services/cost_cache`` is already exactly this, for spend.

PAYLOAD SHAPES
--------------
Two, and the choice per tile is about RBAC rather than size.

``{"rows": [{"workgroup": …, "state": …, "region": …}]}`` for anything the caller's
workgroups filter. The read endpoint feeds those rows straight to
``cloud_stats.summarize_instances`` / ``summarize_by_region`` — the same helpers the live
endpoints use today — so the filtering rules are not copied anywhere. A pre-aggregated
count would be smaller, but it would mean a second implementation of "which rows may this
user see", and that is the drift ``tests/test_cache_warmer_parity`` exists to prevent.

``{"count": n, "running": m}`` for tiles with no per-row RBAC — the container tiles, whose
only gate is ``require_permission("containers", "read")``.

The collector NORMALISES running-state into one ``state`` field as it writes, because it
already knows which field this provider uses (``state`` for AWS/Azure, ``status`` for GCP,
``lifecycle_state`` for OCI, ``power_state`` for the hypervisors). That is what keeps the
reader on one uniform call instead of re-deriving per-provider predicates.

DELEGATION IS THE RULE
----------------------
Every fetcher imports the api module and calls **its** fetcher. A collector holding its own
copy of a fetch drifts silently — no exception, no log, just a differently-shaped payload —
and this repo has shipped that bug twice (see ``main.py``'s warmer comment block). The
imports are function-local, both to avoid a services→api import at module scope and because
several of these api modules pull in cloud SDKs.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import dashboard_stat_cache as store

logger = logging.getLogger(__name__)


class TileUnavailable(Exception):
    """This tile could not be collected. Carries whether it was a saturation refusal
    (retry soon) rather than a real failure (back off)."""

    def __init__(self, detail: str, *, busy: bool = False):
        self.detail, self.busy = detail, busy
        super().__init__(detail)


@dataclass(frozen=True)
class TileSpec:
    key: str
    provider: str
    fetch: Callable
    # A key from /api/features. A tile whose integration is switched off is skipped rather
    # than collected-and-failed: an unconfigured cloud is not an error, and letting it earn
    # a cooldown would fill the operator's status page with noise.
    feature: str = ""
    # Which api module's `_accessible_workgroups` governs this tile's rows, or "" for a tile
    # with no per-row RBAC. It lives on the spec so the read endpoint needs no second table
    # that could drift from this one.
    #
    # NOT unified on purpose. The four cloud modules key on `user.is_admin`; inventory,
    # databases and k8s key on `user.is_effective_admin`, which is a SUPERSET (it also
    # honours a session-permission row and a live Entitle JIT grant). A JIT-admin therefore
    # sees everything on /inventory and only their own workgroups on /api/aws/instances.
    # That inconsistency predates this table; reproducing it per tile is correct, and
    # "fixing" it here would silently widen or narrow somebody's access.
    rbac: str = ""


# ── payload builders ─────────────────────────────────────────────────────────

def rows_payload(rows, *, state_field: str, region_field: str = "",
                 running_value: str = "running") -> dict:
    """Minimal per-row projection for a workgroup-filtered tile.

    Three short strings a row. Everything else the live endpoint returns is dropped: the
    read endpoint only ever counts these.
    """
    out = []
    for r in (rows or []):
        raw = str(r.get(state_field, "") or "")
        out.append({
            "workgroup": r.get("workgroup"),
            # Normalised here, where the provider's field name is already known.
            "state": "running" if raw.lower() == running_value.lower() else raw.lower(),
            "region": (str(r.get(region_field) or "").strip() if region_field else ""),
        })
    return {"rows": out}


def count_payload(items, *, running=None) -> dict:
    """Plain totals for a tile with no per-row RBAC."""
    items = list(items or [])
    out = {"count": len(items)}
    if running is not None:
        out["running"] = sum(1 for i in items if running(i))
    return out


def _upper_running(field: str):
    return lambda i: str(i.get(field, "") or "").upper() == "RUNNING"


# ── fetchers ─────────────────────────────────────────────────────────────────
#
# Each returns a payload dict, or raises TileUnavailable. They must never return an empty
# payload to mean "it failed" — a 0 is a plausible number and the store would write it over
# a working count.

async def _aws_instances():
    from ..api import aws as aws_api
    return rows_payload(await aws_api._fetch_instances_fresh(),
                        state_field="state", region_field="region")


async def _aws_amis():
    from ..api import aws as aws_api
    return count_payload(await aws_api._fetch_amis(aws_api._aws_region()))


async def _azure_vms():
    from ..api import azure as azure_api
    # Azure keys region as "location" on the VM rows.
    return rows_payload(await azure_api._fetch_vms_fresh(),
                        state_field="state", region_field="location")


async def _azure_images():
    from ..api import azure as azure_api
    payload = await azure_api._fetch_private_images(azure_api._loc())
    return count_payload((payload or {}).get("images") or [])


async def _gcp_instances():
    from ..api import gcp as gcp_api
    project_id = gcp_api._gcp_project()
    if not project_id:
        raise TileUnavailable("no GCP project configured")
    return rows_payload(await _with_session(gcp_api._gcp_instances_unfiltered, project_id),
                        state_field="status", region_field="region")


async def _gcp_images():
    from ..api import gcp as gcp_api
    from . import gcp_service
    project_id = gcp_api._gcp_project()
    if not project_id:
        raise TileUnavailable("no GCP project configured")
    return count_payload(await gcp_service.list_custom_images(project_id=project_id))


async def _oci_instances():
    from ..api import oci as oci_api
    return rows_payload(
        await _with_session(oci_api._oci_instances_unfiltered, oci_api._compartment()),
        state_field="lifecycle_state")


async def _oci_images():
    from ..api import oci as oci_api
    from . import oci_service
    imgs = await oci_service.list_images(oci_api._compartment())
    return count_payload([i for i in (imgs or []) if i.get("source") == "custom"])


async def _ecs_tasks():
    from ..config import settings
    from . import aws_service
    return count_payload(await aws_service.list_ecs_tasks(
        settings.aws_region, settings.bt_ecs_cluster, False))


async def _aci_containers():
    from ..api import containers as containers_api
    from . import azure_service
    raw = await azure_service.list_aci_container_instances(containers_api._aci_rg())
    return count_payload(raw, running=lambda c: str(
        c.get("state", "") or "").lower() == "running")


async def _gce_containers():
    """GCE COS instances = gateways + compose, mirroring the merged table the Containers
    page shows and the tile's own two-call fetcher."""
    from ..api import containers as containers_api
    from . import gcp_service
    project_id = containers_api._gcp_project_id()
    if not project_id:
        raise TileUnavailable("no GCP project configured")
    jumpoints, compose = await asyncio.gather(
        gcp_service.list_gce_jumpoints(project_id),
        gcp_service.list_gce_compose(project_id))
    return count_payload(list(jumpoints or []) + list(compose or []),
                         running=_upper_running("status"))


async def _cloud_run_jobs():
    from ..api import containers as containers_api
    from . import gcp_service
    project_id = containers_api._gcp_project_id()
    if not project_id:
        raise TileUnavailable("no GCP project configured")
    return count_payload(await gcp_service.list_cloud_run_jobs(project_id, limit=20))


async def _rancher_nodes():
    from ..api import containers as containers_api
    from . import gcp_service
    project_id = containers_api._gcp_project_id()
    if not project_id:
        raise TileUnavailable("no GCP project configured")
    return count_payload(await gcp_service.list_gce_rancher(project_id),
                         running=_upper_running("status"))


async def _portainer_node():
    from ..api import containers as containers_api
    from . import gcp_service
    project_id = containers_api._gcp_project_id()
    if not project_id:
        raise TileUnavailable("no GCP project configured")
    return count_payload(await gcp_service.list_gce_portainer(project_id),
                         running=_upper_running("status"))


async def _portainer_endpoints():
    from . import portainer_service
    # ONE call. The tile used to make one per workgroup and sum them, but this endpoint has
    # no workgroup parameter and containers.py has no per-row workgroup filtering at all.
    raw = await portainer_service.list_endpoints()
    return count_payload(raw, running=lambda e: e.get("Status") == 1
                         or e.get("status") == 1)


# ── the spec table ───────────────────────────────────────────────────────────
#
# tests/test_dashboard_collect.py asserts this covers exactly the cloud-backed tile keys in
# templates/dashboard.html, in BOTH directions. A tile with no spec is permanently
# "unavailable" with nothing in any log — that failure mode has shipped here before.

TILES = (
    TileSpec("aws_instances",       "aws",       _aws_instances,       feature="aws",
             rbac="aws"),
    TileSpec("aws_amis",            "aws",       _aws_amis,            feature="aws"),
    TileSpec("ecs_tasks",           "aws",       _ecs_tasks,           feature="aws"),
    TileSpec("azure_vms",           "azure",     _azure_vms,           feature="azure",
             rbac="azure"),
    TileSpec("azure_images",        "azure",     _azure_images,        feature="azure"),
    TileSpec("aci_containers",      "azure",     _aci_containers,      feature="azure"),
    TileSpec("gcp_instances",       "gcp",       _gcp_instances,       feature="gcp",
             rbac="gcp"),
    TileSpec("gcp_images",          "gcp",       _gcp_images,          feature="gcp"),
    TileSpec("gce_containers",      "gcp",       _gce_containers,      feature="gcp"),
    TileSpec("cloud_run_jobs",      "gcp",       _cloud_run_jobs,      feature="gcp"),
    TileSpec("portainer_node",      "gcp",       _portainer_node,      feature="gcp"),
    TileSpec("rancher_nodes",       "gcp",       _rancher_nodes,       feature="k8s_management"),
    TileSpec("oci_instances",       "oci",       _oci_instances,       feature="oci",
             rbac="oci"),
    TileSpec("oci_images",          "oci",       _oci_images,          feature="oci"),
    TileSpec("portainer_endpoints", "portainer", _portainer_endpoints,
             feature="portainer_configured"),
)


def tile_providers() -> dict:
    return {t.key: t.provider for t in TILES}


# ── plumbing ─────────────────────────────────────────────────────────────────

async def _with_session(fn, *args):
    """Run ``fn(db, *args)`` against a session this collector owns.

    Several api fetchers still take a Session. The collector has no request to borrow one
    from, and must not hold one across the provider call anyway — so it opens one, and the
    fetcher's own DB work happens inside. This is the ``api/inventory.py`` shape.
    """
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        return await fn(db, *args)
    finally:
        db.close()


def _features() -> dict:
    """The same map /api/features serves, so a tile is collected exactly when the page
    would show it.

    Via services/feature_flags rather than main: dash-worker runs
    ``python -m web_dashboard.jobs_worker`` and importing main there would construct the
    whole FastAPI app in a process that serves no requests. Sharing the function is also
    what stops the collector growing a second, drifting copy of the gating rules.
    """
    try:
        from . import feature_flags
        return feature_flags.feature_map() or {}
    except Exception:                                  # pragma: no cover - defensive
        logger.debug("dashboard collect: feature map unavailable", exc_info=True)
        return {}


def _enabled(spec: TileSpec, features: dict) -> bool:
    return not spec.feature or bool(features.get(spec.feature))


async def _collect_tile(spec: TileSpec, *, force: bool, min_age_s: int) -> str:
    """Claim → fetch with NO session held → record. Returns the outcome, for logging.

    The three phases use three separate short-lived sessions on purpose. Holding a pooled
    connection across a provider call is the exhaustion failure database.py's pool-sizing
    comment is written about, and on a 5+5 pool a collector that did it would be
    indistinguishable from the bug this whole change exists to fix.
    """
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        claimed, _snap, reason = store.claim(db, spec.key, provider=spec.provider,
                                             force=force, min_age_s=min_age_s)
    finally:
        db.close()
    if not claimed:
        return reason

    payload, error, busy = None, "", False
    try:
        # Deadline is explicit: jobs_worker calls cloud_executor.use_worker_defaults(),
        # which sets a 14400s budget that is right for an image export and absurd for a
        # dashboard tile. Inherit it and one wedged provider parks a collector thread for
        # four hours.
        payload = await asyncio.wait_for(spec.fetch(), timeout=_tile_deadline())
    except asyncio.TimeoutError:
        error, busy = f"{spec.provider} did not answer within {_tile_deadline():g}s", True
    except TileUnavailable as exc:
        error, busy = exc.detail, exc.busy
    except Exception as exc:                           # noqa: BLE001
        from . import cloud_executor
        busy = isinstance(exc, cloud_executor.CloudCallError)
        error = f"{type(exc).__name__}: {exc}"

    db = SessionLocal()
    try:
        store.finish(db, spec.key, payload, error=error, busy=busy)
    finally:
        db.close()
    return "collected" if payload is not None else "failed"


def _tile_deadline() -> float:
    """Per-tile budget. Comfortably under the lease, so a hung provider releases its claim
    by expiry rather than blocking that tile until the process restarts."""
    return max(5.0, min(float(store.lease_seconds()) - 10.0, 45.0))


async def collect_once(*, force: bool = False, min_age_s: int = 0) -> dict:
    """One collection pass over every enabled tile.

    Providers run CONCURRENTLY; tiles within a provider run SEQUENTIALLY. GCP owns five
    tiles here against an 8-thread pool (services/cloud_executor), so a fully-gathered pass
    would hand itself CloudProviderBusy and mark its own tiles failed. Same reasoning as
    cost_cache.warm's "sequential, not gathered: the two views hit the same subscription".
    """
    from ..database import SessionLocal

    features = _features()
    specs = [t for t in TILES if _enabled(t, features)]
    if not specs:
        return {"collected": 0, "skipped": 0, "failed": 0}

    # Rows first: pacing is a bulk UPDATE and cannot reach a row that does not exist yet.
    db = SessionLocal()
    try:
        store.ensure_tiles(db, [(t.key, "", t.provider) for t in specs])
    finally:
        db.close()

    by_provider: dict = {}
    for spec in specs:
        by_provider.setdefault(spec.provider, []).append(spec)

    async def _one_provider(group):
        return [await _collect_tile(s, force=force, min_age_s=min_age_s) for s in group]

    results = await asyncio.gather(*(_one_provider(g) for g in by_provider.values()),
                                   return_exceptions=True)

    tally = {"collected": 0, "skipped": 0, "failed": 0}
    for group in results:
        if isinstance(group, BaseException):           # pragma: no cover - defensive
            logger.warning("dashboard collect: provider group failed: %s", group)
            tally["failed"] += 1
            continue
        for outcome in group:
            key = outcome if outcome in tally else "skipped"
            tally[key] += 1
    return tally


async def collect_loop() -> None:
    """Forever: collect, sleep, repeat. Started as a PEER of the job runner.

    Not a job type. Every entry in jobs_worker.HANDLED_TYPES must appear in exactly one
    tier tuple (statically pinned by tests/test_worker_tiers.py), an untiered type raises
    in the supervisor and takes the loop down for every job, and a pass every 60s would
    flood the jobs table. The right precedent is notification_service.drain_loop: launched
    unconditionally, owns a try/except per pass, re-reads its interval each time so a
    settings change lands without a restart, and its failures are its own.
    """
    import random
    # De-burst: the app's two gunicorn workers start within milliseconds of each other, and
    # while the claim lock makes a simultaneous pass correct, it makes the losers wait for
    # nothing.
    await asyncio.sleep(random.uniform(0, 5))
    while True:
        try:
            tally = await collect_once()
            if tally["collected"] or tally["failed"]:
                logger.info("dashboard stats: %s", tally)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # noqa: BLE001
            logger.warning("dashboard stats collector pass failed: %s", exc)
        await asyncio.sleep(store.collect_interval_seconds())
