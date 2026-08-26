"""One call for every dashboard tile.

The home page used to fan out to ~33 endpoints, ~22 of them simultaneously, and every one
held a pooled connection for its whole duration against ``pool_size=5 + max_overflow=5``.
That is the ``QueuePool limit of size 5 overflow 5 reached`` failure of 26.8.5, and caching
could not fix it: a cache hit is still a request, still a connection, still a slot. This
endpoint is the page asking one question instead.

**It makes no cloud call. Not "usually none" — none.** There is no ``allow_fetch``
parameter, no refresh path, and no import of any dialing service; the only writer of the
cloud-backed numbers is ``services/dashboard_collect``, running in dash-worker. A tile the
collector has not reached yet reports ``value: -1``, which is exactly what the client
already renders as "unavailable". ``tests/test_dashboard_stats_api`` asserts the no-dialing
property by source scan, because a promise like that is worth enforcing structurally rather
than describing.

TWO CLASSES OF TILE, AND WHY THEY ARE NOT TREATED ALIKE
-------------------------------------------------------
*Cloud-backed* tiles come from ``dashboard_stat_cache``. Their payload is either a minimal
per-row projection (``{workgroup, state, region}``) or a plain count, and the row form is
fed straight to ``cloud_stats.summarize_instances`` — the same helper the live endpoints
use, so the filtering rules exist in exactly one place.

*DB-backed* tiles are counted here, inline, in the session this request already holds.
Their cost was never the query — it was being one of 22 separate requests. Snapshotting
them would add writes to buy nothing and would move their RBAC somewhere new.

A third group is **absent from the response entirely**: the five hypervisor tiles. They are
a live WinRM/pyVmomi/XAPI call unless the connection is agent-backed, and counting them here
would put exactly the kind of call this endpoint exists to remove back on the request path.
Collecting them needs the per-connection ``scope`` the table reserves but nothing writes
yet. The contract is therefore "tiles I can answer for": the client keeps its own fetcher
for any key this response omits.

RBAC IS PER TILE, DELIBERATELY NOT UNIFIED
------------------------------------------
The four cloud modules decide admin with ``user.is_admin``. Inventory, databases and k8s use
``user.is_effective_admin``, which is a **superset** — it also honours a session-permissions
row and a live Entitle JIT grant. So a JIT-admin already sees everything on ``/inventory``
and only their own workgroups on ``/api/aws/instances``.

That inconsistency predates this endpoint. Reproducing it tile by tile is correct;
"tidying" it here would silently widen or narrow somebody's access, in a place nobody would
think to look. Each tile therefore borrows its own module's accessor rather than a shared
one, and the choice is recorded on ``TileSpec.rbac``.
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import Job, User, get_db
from ..services import cloud_stats, dashboard_collect, dashboard_stat_cache as store
from .auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# The client's existing sentinels: -1 renders as an en dash plus "unavailable", None means
# still loading. A tile that was never collected must never report 0 — 0 is a plausible
# number and renders as one, which is how five hypervisor tiles reported zero VMs for months.
UNAVAILABLE = -1


def _accessible_for(rbac: str, user: User):
    """The workgroup list this tile's own api module would use.

    Borrowed rather than reimplemented, and per module rather than shared — see the module
    docstring on why the two admin rules must not be unified here.
    """
    if rbac == "aws":
        from . import aws as mod
    elif rbac == "azure":
        from . import azure as mod
    elif rbac == "gcp":
        from . import gcp as mod
    elif rbac == "oci":
        from . import oci as mod
    else:
        return None
    return mod._accessible_workgroups(user)


def _tile(value, *, secondary=None, by_region=None, as_of=None, stale=False, note="",
          status="ok", href=None) -> dict:
    """``href`` overrides the tile's static link in dashboard.html for tiles whose
    destination depends on the data — an OT cell count spans three clouds, so the one
    hardcoded cloud page was wrong for anyone whose cells live elsewhere. None (the
    default, and every other tile) leaves the static href alone."""
    return {"value": value, "secondary": secondary, "by_region": by_region,
            "as_of": as_of, "stale": stale, "note": note, "status": status,
            "href": href}


def _unavailable(note: str) -> dict:
    return _tile(UNAVAILABLE, note=note, status="unavailable")


def _forbidden() -> dict:
    """Renders identically to today's 403-caught-into-{value:-1}. One missing permission
    must not blank the whole page, so this is never an HTTP error."""
    return _tile(UNAVAILABLE, note="not permitted", status="forbidden")


# ── cloud-backed tiles ───────────────────────────────────────────────────────

def _from_snapshot(spec, snaps: list, user: User, now) -> dict:
    """One cloud-backed tile, from its snapshot rows, RBAC applied for this caller.

    ``snaps`` is every scope of this tile. They are summed, and ``as_of`` is the OLDEST
    among them: a tile built from three connections is only as fresh as its stalest one.
    That is the correction ``api/vms.py`` documents for its own ``cached_at``, which used to
    be ``datetime.now()`` — true of the response and silent about the data.
    """
    usable = [s for s in snaps if store.payload_of(s) is not None]
    if not usable:
        return _unavailable(store.note(snaps[0], now) if snaps else "not collected yet")

    accessible = _accessible_for(spec.rbac, user) if spec.rbac else None

    total = running = 0
    by_region: dict = {}
    has_running = False
    for snap in usable:
        payload = store.payload_of(snap)
        if "rows" in payload:
            has_running = True
            rows = payload["rows"]
            summary = cloud_stats.summarize_instances(rows, accessible, "state")
            total += summary["total"]
            running += summary["running"]
            for region, counts in cloud_stats.summarize_by_region(
                    rows, accessible, "state", "region").items():
                bucket = by_region.setdefault(region, {"total": 0, "running": 0})
                bucket["total"] += counts["total"]
                bucket["running"] += counts["running"]
        else:
            total += int(payload.get("count") or 0)
            if payload.get("running") is not None:
                has_running = True
                running += int(payload["running"])

    oldest = min((s["fetched_at"] for s in usable if s["fetched_at"]), default=None)
    notes = [n for n in (store.note(s, now) for s in usable) if n]
    return _tile(total,
                 secondary=running if has_running else None,
                 by_region=by_region or None,
                 as_of=store._iso(oldest),
                 stale=bool(notes),
                 note="; ".join(notes))


# ── DB-backed tiles ──────────────────────────────────────────────────────────

def _db_tiles(db: Session, user: User) -> dict:
    """The tiles that are indexed reads against the dashboard's own database.

    Each one delegates to the same service call its live endpoint uses, and applies that
    endpoint's own RBAC. Every one is wrapped: a single failing source must degrade to one
    unavailable tile, never a 500 that blanks the page.
    """
    out: dict = {}

    def _safe(key, fn):
        try:
            out[key] = fn()
        except Exception as exc:                       # noqa: BLE001
            logger.warning("dashboard stats: %s failed: %s", key, exc)
            out[key] = _unavailable(f"{type(exc).__name__}")

    # Active jobs. Kept live rather than snapshotted: one indexed COUNT, and the number an
    # operator actually watches move — a value lagging up to a minute would be a regression
    # in the one place the page is meant to be current.
    def _active_jobs():
        q = db.query(Job).filter(Job.status.in_(["pending", "queued", "running"]))
        if not user.is_effective_admin:
            q = q.filter(Job.created_by == user.username)
        return _tile(q.count())
    _safe("active_jobs", _active_jobs)

    def _deployed():
        from ..services import inventory_service
        accessible = inventory_service.accessible_workgroups(user)
        items = [i for i in inventory_service.collect(db)
                 if inventory_service.visible_to(i, accessible, user.username)]
        return _tile(len(items))
    _safe("deployed_resources", _deployed)

    def _registry():
        from ..services import image_registry_service
        images = image_registry_service.list_images(db)
        # Green "N promoted" must count only promotions that LANDED — a running, pending,
        # manual or failed entry is not one. Same predicate as the tile it replaces.
        promoted = sum(1 for i in images
                       if any(p and p.get("status") == "completed"
                              for p in (i.get("promotions") or {}).values()))
        return _tile(len(images), secondary=promoted)
    _safe("registered_images", _registry)

    def _databases():
        from ..services import cloud_database_service
        rows = cloud_database_service.list_databases(db)
        # Creator-scoped, not workgroup-scoped: these rows carry no workgroup.
        if not user.is_effective_admin:
            rows = [r for r in rows if r.get("created_by") == user.username]
        return _tile(len(rows))
    _safe("cloud_databases", _databases)

    def _clusters():
        from ..services import k8s_service
        rows = k8s_service.list_clusters(db)
        if not user.is_effective_admin:
            rows = [r for r in rows if r.get("created_by") == user.username]
        return _tile(len(rows))
    _safe("k8s_clusters", _clusters)

    def _workstation():
        from . import vms as vms_api
        rows = vms_api._agent_workstation_vms(db, vms_api._accessible(user))
        running = sum(1 for v in rows if getattr(v, "is_running", False))
        # Oldest sync among the rows shown, never now() — api/vms.py's own rule.
        stamps = [v.synced_at for v in rows if getattr(v, "synced_at", None)]
        tile = _tile(len(rows), secondary=running, as_of=min(stamps) if stamps else None)
        # Per-workgroup counts for the page's workgroup badges. They used to come from the
        # client looping the full /api/vms payload; computing them from the same rows here
        # is one fewer request and keeps the badges consistent with the tile above them.
        counts: dict = {}
        for vm in rows:
            wg = getattr(vm, "workgroup", None)
            if wg:
                counts[wg] = counts.get(wg, 0) + 1
        tile["by_workgroup"] = counts
        return tile
    _safe("workstation_vms", _workstation)

    def _gateways():
        from ..services import gateway_service
        rows = gateway_service.list_gateways(db)
        # reconcile is deliberately NOT run here: it dials every cloud, and this endpoint
        # does not make cloud calls. The Gateways tab reconciles on open; this reports what
        # that last found, exactly as the tile's `?reconcile=false` did.
        return _tile(len(rows),
                     secondary=sum(1 for g in rows if g.get("status") == "running"))
    _safe("gateways", _gateways)

    def _ot_cells():
        # OT demo cells across all three clouds. A cell's VM-deploy child row IS
        # its inventory record (metadata ot_cell=True), so this is a Job-table
        # read — never a cloud call, which is the whole contract of this endpoint.
        # Same filters and workgroup scoping as GET /api/ot/cells, summed over the
        # clouds this caller may read; secondary counts fully wired cells.
        from ..services import ot_service
        perms = user.effective_permissions_dict
        readable = [
            job_type for cloud, job_type in ot_service.CELL_CHILD_JOB_TYPE.items()
            if user.is_effective_admin or not perms or "read" in perms.get(cloud, [])
        ]
        if not readable:
            return _forbidden()
        accessible = _accessible_for("gcp", user)   # same workgroup rule on every cloud module
        per_cloud = {}
        total = wired = 0
        for row in db.query(Job).filter(Job.job_type.in_(readable)).all():
            meta = row.metadata_dict
            if not meta.get("ot_cell") or meta.get("destroyed") or row.status == "cancelled":
                continue
            if accessible is not None and (row.workgroup or "").lower() not in accessible:
                continue
            total += 1
            cloud = ot_service.cell_cloud_for_job_type(row.job_type)
            if cloud:
                per_cloud[cloud] = per_cloud.get(cloud, 0) + 1
            # ot_service owns "is this cell fully wired" — this tile and
            # GET /api/ot/cells used to carry a copy each, which disagreed the
            # moment a cell had more than one protocol tunnel (both went green
            # on the first of several).
            if row.status == "completed" and ot_service.cell_wiring_complete(meta):
                wired += 1
        # The count spans three clouds but the tile is one link, so send the caller
        # where their cells actually are: the cloud holding the most. With no cells
        # (or none readable) fall back to a cloud this caller may READ — linking to
        # /gcp unconditionally handed a GCP-less or GCP-forbidden operator a dead end.
        busiest = max(per_cloud, key=lambda c: (per_cloud[c], c)) if per_cloud else ""
        if not busiest:
            readable_clouds = [c for c, jt in ot_service.CELL_CHILD_JOB_TYPE.items()
                               if jt in readable]
            busiest = "gcp" if "gcp" in readable_clouds else (
                readable_clouds[0] if readable_clouds else "gcp")
        return _tile(total, secondary=wired, href=f"/{busiest}#ot")
    _safe("ot_cells", _ot_cells)

    return out


async def _cost_tile(user: User) -> dict:
    """Month-to-date spend, from the cost cache's last known good figure.

    ``allow_fetch=False`` is the load-bearing argument. ``/api/costs/summary`` may claim and
    query; this endpoint may not, so it reads whatever is already stored and says so if that
    is stale. Admin-only, because ``/api/costs`` is.
    """
    if not user.is_effective_admin:
        return _forbidden()
    try:
        from ..services import cost_cache
        payload = await cost_cache.get_summary(refresh=False, allow_fetch=False)
    except Exception as exc:                           # noqa: BLE001
        logger.warning("dashboard stats: cost tile failed: %s", exc)
        return _unavailable(f"{type(exc).__name__}")

    if not payload or payload.get("total_mtd") is None:
        return _unavailable("no spend figure yet")
    currency = payload.get("currency") or "USD"
    symbol = "$" if currency == "USD" else currency + " "
    parts = [f"{c['cloud']} {symbol}{round(c['amount']):,}"
             for c in (payload.get("clouds") or [])
             if c.get("status") == "ok" and c.get("amount") is not None]
    return _tile(f"{symbol}{round(payload['total_mtd']):,}",
                 secondary=" · ".join(parts),
                 as_of=payload.get("oldest_as_of"),
                 stale=bool(payload.get("stale")),
                 note=payload.get("note") or "")


# ── the endpoint ─────────────────────────────────────────────────────────────

@router.get("/stats")
async def dashboard_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Every dashboard tile's numbers in one call.

    ``db`` is declared and reused rather than opening a ``SessionLocal`` here:
    ``get_current_user`` already depends on ``get_db`` and FastAPI caches that within a
    request, so a second session would take a SECOND connection out of a pool of ten while
    the first is still held.
    """
    now = store._utcnow()
    snaps = store.read_all(db)      # ONE query — the whole DB cost of the snapshot half
    tiles: dict = {}

    for spec in dashboard_collect.TILES:
        tiles[spec.key] = _from_snapshot(spec, snaps.get(spec.key, []), current_user, now)

    tiles.update(_db_tiles(db, current_user))
    tiles["cloud_cost"] = await _cost_tile(current_user)

    as_ofs = [t["as_of"] for t in tiles.values() if t.get("as_of")]
    return {
        "tiles": tiles,
        # Oldest contributor, never now(): the page is only as fresh as its stalest tile.
        "oldest_as_of": min(as_ofs) if as_ofs else None,
        "stale": any(t.get("stale") for t in tiles.values()),
        "generated_at": store._iso(now),
    }


@router.post("/refresh", status_code=202)
async def dashboard_refresh(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Ask the collector for a fresh pass. Returns immediately.

    **Refresh triggers the collector; it does NOT read the live endpoints.** Reverting to a
    live fan-out here would rebuild the ~22-request burst this whole change removes, fired
    from the one button an operator presses when the page already looks wrong. That is the
    cost-cache incident in miniature: the page shows an error, which makes the admin click
    Refresh, which issues more queries into the window already rejecting them.

    It also does not fetch synchronously in THIS process. The app serves requests on two
    gunicorn workers against a ten-connection pool and eight threads per provider; doing
    thirty tiles' worth of cloud calls inside a request handler is the 2026-08-12 outage
    with a button on it.

    ``mark_stale`` rather than a delete, for the same reason ``cost_cache`` uses it: the
    current numbers keep serving while the next pass re-collects. Deleting first trades a
    working value for a maybe — which is exactly what made one throttle into a blank page.

    ``cooldown_until`` is deliberately NOT cleared. A saturated provider stays left alone no
    matter how many times the button is pressed.
    """
    now = store._utcnow()
    newest = max((s["fetched_at"] for snaps in store.read_all(db).values()
                  for s in snaps if s["fetched_at"]), default=None)
    floor = store.min_refresh_interval_seconds()
    if newest and (now - newest).total_seconds() < floor:
        # Say so rather than appearing to do nothing.
        waited = int((now - newest).total_seconds())
        return {"ok": True, "queued": False,
                "reason": f"collected {waited}s ago; minimum interval is {floor}s"}

    store.mark_stale(db)
    # Fire and forget: the response is already composed, and whichever process wins the
    # claim does the work once — the other's next pass claims nothing.
    import asyncio
    asyncio.create_task(_forced_pass())
    return {"ok": True, "queued": True}


async def _forced_pass() -> None:
    """Run one forced collection, swallowing everything.

    Detached from the request, so it must never raise into the event loop — and it holds no
    session: ``collect_once`` opens its own, per tile, around each provider call.
    """
    try:
        await dashboard_collect.collect_once(force=True)
    except Exception as exc:                           # noqa: BLE001
        logger.warning("dashboard stats: forced pass failed: %s", exc)
