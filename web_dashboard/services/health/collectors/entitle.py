"""Entitle: is just-in-time access reconciling, and did what the dashboard built apply?

* **The JIT reconciliation sweep** (``cloud_identity_sweeper_service``): its last stored
  result. A cloud leg that errored, grants Entitle no longer knows about (orphans), or a
  sweep that has stopped running are the failures that matter -- each means time-boxed
  access may not be ending when it should.
* **Virtual applications and persona bundle sets** the dashboard applied through
  Terraform: one that is ``failed`` is access somebody expects to be requestable and is
  not.

No grant, requester or role is listed here, only whether the machinery is working.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .. import core
from ....utils.clock import utcnow

_APP_STATUS = {"active": core.OK, "failed": core.FAIL, "applying": core.UNKNOWN,
               "destroying": core.UNKNOWN}


def sweep_item(result: dict, *, gate_on: bool, interval_s: int,
               now: Optional[datetime] = None) -> Optional[core.Item]:
    """The sweep's health from its stored result, or None when JIT is not in use. Pure."""
    now = now or utcnow()
    if not gate_on:
        return None
    checks = []
    if result.get("never_run"):
        checks.append(core.check("ran", "Last sweep", core.UNKNOWN,
                                 "The reconciliation sweep has not run yet."))
    elif result.get("corrupted"):
        checks.append(core.check("ran", "Last sweep", core.UNKNOWN,
                                 "The stored sweep result could not be read."))
    elif result.get("skipped"):
        checks.append(core.check("ran", "Last sweep", core.WARN,
                                 "The sweep is switched off, so expired grants are not "
                                 "being reconciled."))
    else:
        ended = core.parse_when(result.get("ended_at"))
        late = ended is None or now - ended > timedelta(seconds=max(interval_s, 60) * 3)
        checks.append(core.check(
            "ran", "Last sweep", core.WARN if late else core.OK,
            f"Ran {core.ago(ended, now)}, reconciled {int(result.get('processed') or 0)} "
            f"grant(s)." + (" It should have run again by now." if late else "")))
        for cloud, leg in sorted((result.get("by_cloud") or {}).items()):
            if not isinstance(leg, dict):
                continue
            if leg.get("error"):
                checks.append(core.check(f"cloud:{cloud}", f"{cloud.upper()} leg", core.FAIL,
                                         core.text(leg.get("error"), 300)))
        orphans = result.get("orphans") or []
        if orphans:
            checks.append(core.check(
                "orphans", "Orphaned grants", core.WARN,
                f"{len(orphans)} grant(s) the dashboard recorded that Entitle no longer "
                f"knows about. See the cloud identity orphans view."))
    return core.Item(object_id="jit-sweep", name="Just-in-time cloud access",
                     kind="Reconciliation sweep", href="/settings", checks=checks)


def applied_items(rows, *, kind: str, name_of) -> list:
    """Items for virtual apps or bundle sets. Pure."""
    out = []
    for row in rows:
        if row.status == "deleted":
            continue
        status = _APP_STATUS.get(row.status, core.UNKNOWN)
        detail = row.status.capitalize()
        if row.error and status != core.OK:
            detail += f": {core.text(row.error, 300)}"
        out.append(core.Item(object_id=f"{kind}:{row.id}", name=name_of(row),
                             kind=kind.replace("_", " ").capitalize(), href="/settings",
                             checks=[core.check("applied", "Applied", status, detail)]))
    return out


async def collect(db) -> list:
    from ... import cloud_identity_sweeper_service as sweeper, config_service
    from ....database import EntitleBundleSet, EntitleVirtualApp
    items = []
    sweep = sweep_item(sweeper.get_last_sweep_result(),
                       gate_on=config_service.get_bool("cloud_identity_gate_enabled",
                                                       default=False),
                       interval_s=sweeper.sweep_interval_seconds())
    if sweep:
        items.append(sweep)
    items += applied_items(db.query(EntitleVirtualApp).all(), kind="virtual_app",
                           name_of=lambda r: r.name)
    items += applied_items(db.query(EntitleBundleSet).all(), kind="bundle_set",
                           name_of=lambda r: f"Persona bundles ({r.created_at:%Y-%m-%d})")
    return items
