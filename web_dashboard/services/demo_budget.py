"""A demo presenter's monthly budget: what their cells cost this month, and whether a new
booking fits.

The Demo hub prices each cell at list price (services/demo_cell_cost). This adds the limit
an admin sets on that spending, and holds it at the one moment it can be held without
breaking a demo already in progress: **when a cell is booked.**

* **Refuse, never stop.** A booking that would take the presenter past their budget is
  refused with every figure in the message. A running cell is never stopped by the budget:
  the number is an estimate, and an estimate that ends a live customer demo would be the
  last one anybody trusted.
* **Admins are never limited**, and nothing changes until an admin sets a budget: the
  instance default (``demo_monthly_budget_usd``) is 0, and 0 means none.
* **A user's own budget wins** (``User.demo_budget_usd``): NULL means the default, 0 means
  no budget for that user, more than 0 is their limit.

The arithmetic, per presenter, for the current calendar month (UTC):

* ``spent``: what their cells' VMs and cluster nodes have cost since the 1st, including a
  VM destroyed this month, up to when it was destroyed. Running and stopped time are split
  on the dashboard's own power jobs, the same as the hub.
* ``committed``: each live resource's rate now, held until its cell's end or the end of the
  month, whichever is first. A cell with no end runs to the end of the month.
* ``estimate``: the new cell's rate times its lifetime.

A booking is refused when ``spent + committed + estimate`` passes the limit.

**What cannot be priced is not counted, and is said.** A VM in a region the price list
cannot reach, a database, a cold price list that times out: none of it is counted as $0,
none of it refuses a booking on its own, and the message names it.

Price lookups are blocking HTTP on a cold cache. Callers on the event loop go through
:func:`check_booking_async`, which runs this on ``cloud_executor`` with its own session.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from ..database import Job
from . import demo_cell_cost as dcc

logger = logging.getLogger(__name__)

#: Metadata that marks a deploy row as part of a demo cell.
_CELL_MARKERS = ("ot_cell", "ot_broker", "netcell", "custom_cell_id")


class BudgetExceeded(Exception):
    """A booking that would take the presenter past their monthly budget."""


# ── Whose limit ─────────────────────────────────────────────────────────────────

def instance_default() -> float:
    try:
        from . import config_service
        return max(0.0, float(config_service.get("demo_monthly_budget_usd", "") or 0))
    except (TypeError, ValueError):
        return 0.0


def limit_for(user) -> tuple:
    """``(limit_usd or None, source)``. Source: admin | personal | default | none."""
    if getattr(user, "is_effective_admin", False) or getattr(user, "is_admin", False):
        return None, "admin"
    own = getattr(user, "demo_budget_usd", None)
    if own is not None:
        return (float(own), "personal") if own > 0 else (None, "none")
    default = instance_default()
    return (default, "default") if default > 0 else (None, "none")


# ── What they are spending ──────────────────────────────────────────────────────

def month_bounds(now: datetime) -> tuple:
    now = dcc._naive(now)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt


def _cell_ends(db: Session, username: str) -> dict:
    """``{custom_cell_id: end}`` for this presenter's custom cells."""
    from ..database import CustomCell
    return {c.id: dcc._naive(c.expires_at)
            for c in db.query(CustomCell).filter(CustomCell.created_by == username)}


def _vm_rows(db: Session, username: str, since: datetime) -> list:
    from . import suspend_sweeper
    rows = []
    for row in (db.query(Job)
                .filter(Job.job_type.in_(tuple(suspend_sweeper._DEPLOY_TYPES)),
                        Job.created_by == username, Job.status == "completed")):
        m = row.metadata_dict
        if not any(m.get(k) for k in _CELL_MARKERS):
            continue
        gone = dcc._naive(row.updated_at)
        if m.get("destroyed") and (gone is None or gone < since):
            continue
        rows.append(row)
    return rows


def _clusters(db: Session, username: str) -> list:
    """``[(cluster, cell_end)]`` in this presenter's custom cells, not destroyed."""
    from ..database import CustomCell, CustomCellMember, K8sCluster
    out = []
    cells = {c.id: c for c in db.query(CustomCell).filter(CustomCell.created_by == username)}
    if not cells:
        return out
    for m in db.query(CustomCellMember).filter(CustomCellMember.cell_id.in_(list(cells)),
                                               CustomCellMember.kind == "k8s"):
        if m.status == "destroyed" or not m.ref_id:
            continue
        cluster = db.query(K8sCluster).filter(K8sCluster.id == m.ref_id).first()
        if cluster is not None:
            out.append((cluster, dcc._naive(cells[m.cell_id].expires_at)))
    return out


def standing(db: Session, username: str, *, now: Optional[datetime] = None,
             rate_for=None, rate_spec=None) -> dict:
    """``{spent_usd, committed_usd, unpriced}`` for this presenter, this month."""
    from . import spend_sweeper
    rate_for = rate_for or spend_sweeper.rate_for
    rate_spec = rate_spec or spend_sweeper.rate_for_spec
    now = dcc._naive(now or datetime.now(timezone.utc))
    month_start, month_end = month_bounds(now)
    spent, committed, unpriced = 0.0, 0.0, []

    ends = _cell_ends(db, username)
    rows = _vm_rows(db, username, month_start)
    events = dcc.power_events(db, [r.id for r in rows])
    for row in rows:
        m = row.metadata_dict
        on = rate_for(row, m, running=True)
        off = rate_for(row, m, running=False)
        if on is None or off is None:
            unpriced.append(dcc._vm_name(row))
            continue
        history = events.get(row.id) or []
        spent += dcc.span_cost(row.completed_at or row.created_at, dcc._vm_end(row, now),
                               history, rate_running=on, rate_stopped=off,
                               since=month_start)
        if m.get("destroyed"):
            continue
        running = (history[-1][1] if history else "start") != "stop"
        end = (ends.get(m.get("custom_cell_id")) if m.get("custom_cell_id")
               else dcc._naive(m.get("demo_end_at"))) or dcc._naive(row.expires_at)
        committed += (on if running else off) * dcc._hours(now, min(end or month_end,
                                                                    month_end))

    clusters = _clusters(db, username)
    cluster_events = dcc.cluster_power_events(db, [c.id for c, _ in clusters])
    for cluster, end in clusters:
        on, off, _reason = dcc.cluster_rates(db, cluster, rate_spec)
        if on is None or off is None:
            unpriced.append(cluster.name)
            continue
        history = cluster_events.get(cluster.id) or []
        spent += dcc.span_cost(cluster.created_at, now, history, rate_running=on,
                               rate_stopped=off, since=month_start)
        rate = on if dcc._cluster_running(cluster, history) else off
        committed += rate * dcc._hours(now, min(end or month_end, month_end))

    return {"spent_usd": round(spent, 2), "committed_usd": round(committed, 2),
            "unpriced": unpriced}


def summary(db: Session, user, *, now: Optional[datetime] = None) -> Optional[dict]:
    """The hub's budget line for ``user``, or None when they have no budget."""
    limit, source = limit_for(user)
    return summary_for(db, user.username, limit, source, now=now)


def summary_for(db: Session, username: str, limit, source: str, *,
                now: Optional[datetime] = None) -> Optional[dict]:
    """:func:`summary` from plain values, for a caller on another thread."""
    if limit is None:
        return None
    st = standing(db, username, now=now)
    return {"limit_usd": round(limit, 2), "source": source, **st}


# ── A new booking ───────────────────────────────────────────────────────────────

def estimate(specs, *, start: Optional[datetime], end: Optional[datetime],
             now: Optional[datetime] = None, rate_spec=None) -> tuple:
    """``(usd or None, unpriced)`` for a cell of ``specs`` -- ``[(cloud, meta, count)]``,
    each a VM described the way a deploy row is -- from ``start`` (None: now) to ``end``
    (None: the end of the month)."""
    from . import spend_sweeper
    rate_spec = rate_spec or spend_sweeper.rate_for_spec
    now = dcc._naive(now or datetime.now(timezone.utc))
    begin = dcc._naive(start) or now
    finish = dcc._naive(end) or month_bounds(begin)[1]
    hours = dcc._hours(begin, finish)
    total, priced, unpriced = 0.0, 0, []
    for cloud, meta, count in specs:
        rate = rate_spec(cloud, meta, running=True)
        if rate is None:
            unpriced.append(f"{cloud} {meta.get('instance_type') or meta.get('vm_size') or meta.get('machine_type') or 'VM'}")
            continue
        priced += 1
        total += rate * max(0, int(count or 1)) * hours
    return (round(total, 2) if priced else None), unpriced


def check_booking(db: Session, user, specs, *, start=None, end=None,
                  now: Optional[datetime] = None, rate_for=None, rate_spec=None) -> None:
    """Raise :class:`BudgetExceeded` if this booking would pass the user's budget."""
    limit, _source = limit_for(user)
    if limit is None:
        return
    _check(db, user.username, limit, specs, start=start, end=end, now=now,
           rate_for=rate_for, rate_spec=rate_spec)


def _check(db: Session, username: str, limit: float, specs, *, start, end, now=None,
           rate_for=None, rate_spec=None) -> None:
    st = standing(db, username, now=now, rate_for=rate_for, rate_spec=rate_spec)
    est, est_unpriced = estimate(specs, start=start, end=end, now=now, rate_spec=rate_spec)
    total = st["spent_usd"] + st["committed_usd"] + (est or 0.0)
    if total <= limit:
        return
    parts = [f"${st['spent_usd']:,.2f} spent this month",
             f"${st['committed_usd']:,.2f} committed by running cells",
             f"${(est or 0.0):,.2f} for this cell"]
    msg = (f"This booking would take your demo spend to ${total:,.2f}, over your monthly "
           f"budget of ${limit:,.2f} ({', '.join(parts)}). Destroy or shorten a running "
           f"cell, book a shorter one, or ask an administrator to raise your budget. "
           f"These are list-price estimates.")
    missed = st["unpriced"] + est_unpriced
    if missed:
        msg += f" Not priced, so not counted: {', '.join(missed)}."
    raise BudgetExceeded(msg)


async def check_booking_async(user, specs, *, start=None, end=None) -> None:
    """:func:`check_booking` off the event loop, with its own session. A busy pool or a
    deadline lets the booking through and logs it: a cold price list never refuses on its
    own."""
    limit, _ = limit_for(user)
    if limit is None:
        return
    # Plain values across the thread boundary, never the request's ORM row.
    username = user.username
    from . import cloud_executor

    def _run():
        from ..database import SessionLocal
        session = SessionLocal()
        try:
            _check(session, username, limit, specs, start=start, end=end)
        finally:
            session.close()

    try:
        await cloud_executor.run("pricing", _run, deadline=30)
    except cloud_executor.CloudCallError:
        logger.warning("demo budget: could not price a booking for %s; let through",
                       getattr(user, "username", "?"), exc_info=True)


def check_booking_sync(db: Session, user, specs, *, start=None, end=None) -> None:
    """For a sync route, which FastAPI already runs in a worker thread. A failure to price
    lets the booking through, as the async form does; BudgetExceeded is raised."""
    try:
        check_booking(db, user, specs, start=start, end=end)
    except BudgetExceeded:
        raise
    except Exception:  # noqa: BLE001 -- pricing trouble never refuses on its own
        logger.warning("demo budget: could not price a booking for %s; let through",
                       getattr(user, "username", "?"), exc_info=True)


def specs_for_items(items) -> tuple:
    """``(specs, unpriced)`` for a custom cell's blueprint items: each VM item at its
    count, each cluster as its node pool. Databases cannot be priced yet."""
    specs, unpriced = [], []
    for it in items:
        cloud, spec = it["cloud"], dict(it.get("spec") or {})
        if it["kind"] == "vm":
            meta = {"region": it.get("region") or "", **spec}
            specs.append((cloud, meta, int(it.get("count") or 1)))
        elif it["kind"] == "k8s":
            pool = dcc.pool_for_request(cloud, it.get("region") or "", spec)
            if pool is None:
                unpriced.append(f"{it.get('label') or 'cluster'} ({cloud})")
                continue
            specs.append((cloud, {"region": pool["region"], "instance_type": pool["size"],
                                  "disk_gb": pool["disk_gb"]}, pool["count"]))
        else:
            unpriced.append(f"{it.get('label') or it['kind']} ({it['kind']})")
    return specs, unpriced


# ── The routes' one-liners ──────────────────────────────────────────────────────
#
# A cell route resolves its schedule with demo_schedule.plan (which already speaks
# HTTPException), then calls one of these before it creates a single row.

def _refuse(exc: BudgetExceeded):
    from fastapi import HTTPException
    return HTTPException(status_code=409, detail=str(exc))


def enforce_sync(db: Session, user, specs, sp) -> None:
    try:
        check_booking_sync(db, user, specs, start=sp.start, end=sp.end or sp.expires_at)
    except BudgetExceeded as exc:
        raise _refuse(exc) from exc


async def enforce_async(user, specs, sp) -> None:
    try:
        await check_booking_async(user, specs, start=sp.start, end=sp.end or sp.expires_at)
    except BudgetExceeded as exc:
        raise _refuse(exc) from exc
