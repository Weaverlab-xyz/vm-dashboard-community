"""What a demo cell is costing: a list-price estimate per cell, for the Demo hub.

Presenters build, book and schedule cells on their own (#1325, #1332). The booking-time
policy check (#1335) stops what is not allowed; this is the other half -- what IS being
spent, where the person spending it can see it.

**No new price source.** Every number here comes from machinery the VM spend cap already
uses and documents (``spend_policy``, ``spend_sweeper``):

* the rate is ``spend_sweeper.rate_for`` -- ``pov_cloud_cost.hourly_for_vm`` at list price,
  compute while running, disk always;
* running versus stopped is the dashboard's OWN power jobs, never a live describe, so a VM
  stopped in the cloud console still reads as running. That errs high, which is the
  direction this whole estimate errs in (no reservations, credits, free tier, data
  transfer or snapshots).

Three numbers per cell:

* ``rate_usd_h``  -- what it costs per hour right now;
* ``so_far_usd``  -- the integral of that since each VM came up, split on its power history
  (a VM with a spend cap reports its accrued figure instead, so the hub and the VM's own
  spend panel never disagree);
* ``to_end_usd``  -- the rate now, held until the cell's end time. ``None`` without one.

**What is not priced is said, never counted as zero.** Clusters and managed databases have
no price source in the app yet; a VM in a region the catalogue cannot reach has no rate.
Both land in ``unpriced`` with the reason. An agent cell builds nothing -- it runs on a VM
that is somebody else's line -- so it carries a note and no number.

Prices are fetched by blocking HTTP on a cold cache. Callers run :func:`price_cells` off
the event loop (api/demo uses ``cloud_executor``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy.orm import Session

from ..database import Job
from . import suspend_sweeper, vm_suspend_policy

logger = logging.getLogger(__name__)

CAVEAT = ("Estimated at list price: no Savings Plans, reservations, credits, free tier, "
          "data transfer or snapshots. Errs high.")
AGENT_NOTE = "runs on an existing VM; its cost is that VM's"
UNPRICED_KIND = "clusters and databases are not priced yet"

RateFn = Callable[..., Optional[float]]


def _naive(dt) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _hours(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 3600.0)


# ── What a cell is made of ──────────────────────────────────────────────────────

def billable(db: Session, kind: str, cell: dict) -> tuple:
    """``(vm_rows, unpriced, note)`` for one cell, as the summary lists it."""
    if kind == "agent":
        return [], [], AGENT_NOTE
    if kind in ("ot", "net"):
        row = db.query(Job).filter(Job.id == cell["id"]).first()
        if row is None:
            return [], [], ""
        rows = [row]
        broker_id = row.metadata_dict.get("ot_broker_job_id") if kind == "ot" else ""
        if broker_id:
            broker = db.query(Job).filter(Job.id == broker_id).first()
            if broker is not None:
                rows.append(broker)
        return rows, [], ""
    if kind == "custom":
        from . import custom_cell_service
        rows, unpriced = [], []
        for m in custom_cell_service.members_of(db, cell["id"]):
            if m.status == "destroyed":
                continue
            if m.kind == "vm":
                job = db.query(Job).filter(Job.id == m.ref_id).first() if m.ref_id else None
                if job is not None:
                    rows.append(job)
                continue
            unpriced.append({"name": m.label, "kind": m.kind, "reason": UNPRICED_KIND})
        return rows, unpriced, ""
    return [], [], ""


def power_events(db: Session, job_ids) -> dict:
    """``{deploy_job_id: [(at, "start"|"stop"), ...]}``, oldest first, for these VMs only.

    The same source as ``spend_sweeper.last_power_action`` -- the dashboard's own completed
    ``*_power`` jobs -- but keeping every transition and its time, which "so far" needs.
    One query; the deploy id lives in metadata, so the narrowing is done here.
    """
    wanted = set(job_ids)
    out: dict = {}
    if not wanted:
        return out
    for job in (db.query(Job)
                .filter(Job.job_type.in_(tuple(suspend_sweeper._POWER_JOB.values())),
                        Job.status == "completed")
                .order_by(Job.created_at.asc())
                .all()):
        m = job.metadata_dict
        deploy_id = m.get("deploy_job_id")
        action = m.get("action")
        if deploy_id in wanted and action in ("start", "stop"):
            out.setdefault(deploy_id, []).append(
                (_naive(job.completed_at or job.created_at), action))
    return out


# ── The arithmetic ──────────────────────────────────────────────────────────────

def vm_cost(row, events: list, *, rate_running: float, rate_stopped: float,
            now: datetime) -> float:
    """USD so far for one VM: running spans at ``rate_running``, stopped at ``rate_stopped``.

    From when the deploy completed (the VM existed) to now, or to the row's last update
    once it is destroyed. A capped VM's accrued figure wins -- see the module docstring.
    """
    if getattr(row, "spend_accrued_at", None) is not None:
        return float(row.spend_estimate_usd or 0.0)
    start = _naive(row.completed_at or row.created_at)
    if start is None:
        return 0.0
    end = _naive(now)
    if row.metadata_dict.get("destroyed") and row.updated_at:
        end = min(end, _naive(row.updated_at))
    total, at, running = 0.0, start, True
    for when, action in events or []:
        if when is None or when <= at:
            running = action == "start"
            continue
        if when >= end:
            break
        total += _hours(at, when) * (rate_running if running else rate_stopped)
        at, running = when, action == "start"
    total += _hours(at, end) * (rate_running if running else rate_stopped)
    return total


def _where(row) -> str:
    m = row.metadata_dict
    cloud = vm_suspend_policy.cloud_of(row.job_type) or "?"
    region = m.get("region") or m.get("location") or ""
    zone = m.get("zone") or ""
    if not region and zone:
        # Named as the price lookup keys it: a GCP zone is priced by its region.
        region = zone
        if cloud == "gcp":
            from . import region_catalog
            region = region_catalog.region_from_zone(zone) or zone
    return f"{cloud}/{region or '?'}"


def _vm_name(row) -> str:
    m = row.metadata_dict
    return (m.get("instance_name") or m.get("vm_name") or m.get("instance_id") or row.id)


def cell_cost(db: Session, kind: str, cell: dict, *, now: datetime,
              events: Optional[dict] = None, rate_for: Optional[RateFn] = None) -> dict:
    """The three numbers for one cell, plus what could not be priced and why."""
    if rate_for is None:
        from . import spend_sweeper
        rate_for = spend_sweeper.rate_for
    rows, unpriced, note = billable(db, kind, cell)
    if events is None:
        events = power_events(db, [r.id for r in rows])
    rate_now, so_far, priced = 0.0, 0.0, 0
    for row in rows:
        m = row.metadata_dict
        if m.get("destroyed") or row.status != "completed":
            continue
        history = events.get(row.id) or []
        running_now = (history[-1][1] if history else "start") != "stop"
        on = rate_for(row, m, running=True)
        off = rate_for(row, m, running=False)
        if on is None or off is None:
            unpriced.append({"name": _vm_name(row), "kind": "vm",
                             "reason": f"no price source for {_where(row)}"})
            continue
        priced += 1
        rate_now += on if running_now else off
        so_far += vm_cost(row, history, rate_running=on, rate_stopped=off, now=now)

    end = _naive(cell.get("end_at"))
    to_end = None
    if end is not None and priced:
        to_end = so_far + rate_now * _hours(_naive(now), end)
    return {
        "rate_usd_h": round(rate_now, 4) if priced else None,
        "so_far_usd": round(so_far, 2) if priced else None,
        "to_end_usd": round(to_end, 2) if to_end is not None else None,
        "priced": priced,
        "unpriced": unpriced,
        "note": note,
    }


def price_cells(db: Session, cells_by_kind: dict, *, now: Optional[datetime] = None,
                rate_for: Optional[RateFn] = None) -> dict:
    """``{"cells": {kind: {id: cost}}, "totals": {kind: {...}}, "caveat"}``.

    ``cells_by_kind`` is ``{kind: [cell, ...]}`` exactly as the hub's summary lists them, so
    nothing is priced that the caller cannot see. One cell that fails is that cell's note;
    it never takes the others with it.
    """
    now = now or datetime.now(timezone.utc)
    out: dict = {"cells": {}, "totals": {}, "caveat": CAVEAT}
    for kind, cells in cells_by_kind.items():
        per, rate, so_far, any_priced = {}, 0.0, 0.0, False
        for cell in cells:
            try:
                c = cell_cost(db, kind, cell, now=now, rate_for=rate_for)
            except Exception:  # noqa: BLE001 -- one cell never blanks the rest
                logger.warning("demo cell cost: %s %s", kind, cell.get("id"), exc_info=True)
                c = {"rate_usd_h": None, "so_far_usd": None, "to_end_usd": None,
                     "priced": 0, "unpriced": [], "note": "could not be estimated"}
            per[cell["id"]] = c
            if c["priced"]:
                any_priced = True
                rate += c["rate_usd_h"] or 0.0
                so_far += c["so_far_usd"] or 0.0
        out["cells"][kind] = per
        out["totals"][kind] = {"rate_usd_h": round(rate, 4) if any_priced else None,
                               "so_far_usd": round(so_far, 2) if any_priced else None}
    return out
