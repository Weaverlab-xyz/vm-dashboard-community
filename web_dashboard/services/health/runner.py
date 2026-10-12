"""Running health collectors and storing what they find.

One loop in the jobs worker (one process, so a provider is asked once per interval, not
once per gunicorn worker) runs whichever areas are due. Each run replaces that area's
rows: objects the collector no longer reports are removed, so a deleted tenant stops
showing. A collector that raises records the error against the AREA and leaves its rows
untouched -- "Password Safe is down" must not read as "every account failed".
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ...database import HealthArea, HealthCheck
from .. import feature_flags
from . import core
from .registry import COLLECTORS, get

logger = logging.getLogger(__name__)

LOOP_SLEEP_SECONDS = 60
# Run now refuses inside this window and returns what is stored, so a double click (or
# a script) is not a burst of provider calls.
RUN_NOW_MIN_SECONDS = 60


def enabled(collector: core.Collector) -> bool:
    return not collector.flags or any(feature_flags.enabled(f) for f in collector.flags)


def _area_row(db: Session, area: str) -> HealthArea:
    row = db.get(HealthArea, area)
    if row is None:
        row = HealthArea(area=area)
        db.add(row)
    return row


def store(db: Session, area: str, items: list, *, now: Optional[datetime] = None) -> int:
    """Replace ``area``'s rows with ``items``. Returns how many were stored."""
    now = now or datetime.utcnow()
    seen = set()
    for item in items:
        oid = core.text(item.object_id, 160)
        if not oid or oid in seen:
            continue
        seen.add(oid)
        row = db.get(HealthCheck, (area, oid))
        if row is None:
            row = HealthCheck(area=area, object_id=oid)
            db.add(row)
        row.name = core.text(item.name, 255) or oid
        row.kind = core.text(item.kind, 64) or None
        row.href = core.text(item.href, 255) or None
        row.checks = json.dumps(item.checks)
        row.status = item.status
        row.checked_at = now
    stale = db.query(HealthCheck).filter(HealthCheck.area == area)
    if seen:
        stale = stale.filter(HealthCheck.object_id.notin_(seen))
    stale.delete(synchronize_session=False)
    db.commit()
    return len(seen)


async def run_area(db: Session, collector: core.Collector) -> dict:
    """Run one collector now and store the result. Never raises."""
    started = time.monotonic()
    area = _area_row(db, collector.area)
    area.last_run_at = datetime.utcnow()
    try:
        items = await collector.collect(db)
    except Exception as exc:  # noqa: BLE001 - a collector failing is a result, not a crash
        logger.warning("health: %s collector failed", collector.area, exc_info=True)
        db.rollback()
        area = _area_row(db, collector.area)
        area.last_run_at = datetime.utcnow()
        # The type only: an exception's text can carry a URL, a path or a chained cause
        # from somewhere unrelated, and this is shown on a page. Collectors that know
        # what went wrong return a failing Item with a written message instead.
        area.last_error = (f"The check could not run ({type(exc).__name__}). "
                           f"See the dashboard log for details.")
        area.duration_ms = int((time.monotonic() - started) * 1000)
        db.commit()
        return {"area": collector.area, "ok": False}
    count = store(db, collector.area, items)
    area = _area_row(db, collector.area)
    area.last_ok_at = area.last_run_at
    area.last_error = None
    area.duration_ms = int((time.monotonic() - started) * 1000)
    db.commit()
    return {"area": collector.area, "ok": True, "items": count}


def _due(db: Session, collector: core.Collector, now: datetime) -> bool:
    row = db.get(HealthArea, collector.area)
    return row is None or row.last_run_at is None or \
        now - row.last_run_at >= timedelta(minutes=collector.interval_minutes)


async def run_due(db: Session) -> int:
    now = datetime.utcnow()
    ran = 0
    for collector in COLLECTORS:
        if enabled(collector) and _due(db, collector, now):
            await run_area(db, collector)
            ran += 1
    return ran


async def run_now(db: Session, area: str) -> dict:
    """Run one area on request, unless it ran within the last minute."""
    collector = get(area)
    if collector is None:
        raise KeyError(area)
    row = db.get(HealthArea, area)
    if row and row.last_run_at and \
            (datetime.utcnow() - row.last_run_at).total_seconds() < RUN_NOW_MIN_SECONDS:
        return {"area": area, "ok": row.last_error is None, "throttled": True}
    return await run_area(db, collector)


async def run_loop() -> None:
    """Forever: run what is due, sleep a minute. A peer of the job runner, like
    ``dashboard_collect.collect_loop`` and ``directory_health_service.check_loop``."""
    from ...database import SessionLocal
    while True:
        try:
            db = SessionLocal()
            try:
                ran = await run_due(db)
                if ran:
                    logger.info("health: ran %d area(s)", ran)
            finally:
                db.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # noqa: BLE001
            logger.warning("health pass failed: %s", exc)
        await asyncio.sleep(LOOP_SLEEP_SECONDS)


def snapshot(db: Session, *, persona: str = "") -> dict:
    """What the Health page shows: every enabled area, worst objects first.

    Ordered by the active persona when one is given -- areas that list it come first --
    but never filtered by it.
    """
    rank = {core.FAIL: 0, core.WARN: 1, core.UNKNOWN: 2, core.OK: 3}
    rows_by_area: dict = {}
    for row in db.query(HealthCheck).all():
        rows_by_area.setdefault(row.area, []).append(row)
    areas, order = [], []
    for index, collector in enumerate(COLLECTORS):
        rows = rows_by_area.get(collector.area, [])
        if not enabled(collector) and not rows:
            continue
        run = db.get(HealthArea, collector.area)
        items = sorted(({
            "object_id": r.object_id, "name": r.name, "kind": r.kind or "",
            "href": r.href or "", "status": r.status,
            "checks": _loads(r.checks),
            "checked_at": r.checked_at.isoformat() + "Z" if r.checked_at else None,
        } for r in rows), key=lambda i: (rank.get(i["status"], 2), i["name"].lower()))
        counts = {s: sum(1 for i in items if i["status"] == s) for s in core.STATUSES}
        areas.append({
            "area": collector.area, "label": collector.label,
            "description": collector.description,
            "personas": list(collector.personas),
            "interval_minutes": collector.interval_minutes,
            "enabled": enabled(collector),
            "last_run_at": run.last_run_at.isoformat() + "Z" if run and run.last_run_at else None,
            "last_error": run.last_error if run else None,
            "counts": counts, "items": items,
        })
        order.append((0 if persona and persona in collector.personas else 1, index))
    areas = [a for _o, a in sorted(zip(order, areas), key=lambda pair: pair[0])]
    totals = {s: sum(a["counts"][s] for a in areas) for s in core.STATUSES}
    return {"areas": areas, "counts": totals}


def _loads(raw) -> list:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []
