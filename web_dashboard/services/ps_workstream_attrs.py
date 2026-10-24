"""Stamping a workstream's Password Safe attributes onto the managed systems it onboards.

A workstream (``Workgroup`` in the code) can name attributes — ``{"Workstream": "team-a"}``
— that every managed system onboarded from it should carry
(``workgroup_service.ps_attributes``). This is the half that writes them, so a customer
can write a Smart Rule on ``Workstream = team-a`` and have Password Safe access policy
follow the dashboard's workstreams instead of a second list kept by hand.

Three callers, one function:

  * onboarding — ``ps_vm_hook`` once a managed system id is known;
  * reassign — the cloud workstream-reassign routes, which SWAP the old workstream's
    values for the new one's;
  * backfill — ``api/workgroups`` "apply to existing systems".

**It never creates anything in the tenant.** A type or value the vocabulary does not hold
is a ``failed`` outcome naming it; creating one is an administrator's explicit act at save
time (``workgroup_service.set_ps_attributes(create_missing=True)``). A deploy that minted
attribute types as a side effect would be the silent tenant change this tree refuses.

**Every attribute has its own outcome** — ``ok``, ``skipped`` or ``failed`` — because each
is its own Password Safe call and a partial apply is the normal failure. A skip is never
counted as success: ``state`` is ``ok`` only when every entry was written.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

OK = "ok"
SKIPPED = "skipped"
FAILED = "failed"

# The overall verdict. NONE = the workstreams name no attributes, so nothing was asked.
STATE_NONE = "none"
STATE_OK = "ok"
STATE_PARTIAL = "partial"
STATE_FAILED = "failed"
STATE_SKIPPED = "skipped"


def _same(a, b) -> bool:
    return str(a or "").strip().lower() == str(b or "").strip().lower()


def resolve(vocabulary, type_name: str, value: str) -> tuple:
    """``(type_row, value_row)`` for ``type_name = value`` in a built vocabulary
    (``ps_attribute_catalog.build_vocabulary``), or raise :class:`LookupError` naming
    which half is missing. Case-insensitive, like ``ensure_managed_system_attribute``.

    Read-only is NOT judged here — ``ps_attribute_catalog.assert_assignable`` is the one
    place that rule lives, and callers pass the id on to it.
    """
    type_row = next((t for t in vocabulary or [] if _same(t.get("name"), type_name)), None)
    if type_row is None:
        raise LookupError(f"Password Safe has no attribute type '{type_name}'")
    value_row = next((v for v in type_row.get("values", [])
                      if _same(v.get("value"), value)), None)
    if value_row is None:
        raise LookupError(
            f"the attribute type '{type_row['name']}' has no value '{value}' in Password Safe")
    return type_row, value_row


def unavailable_reason() -> str:
    """"" when Password Safe can be called, else why not."""
    from ..config import settings
    from . import config_service, ps_api_service
    if not config_service.get_bool("password_safe_enabled", settings.password_safe_enabled):
        return "Password Safe is not enabled on this instance"
    if not ps_api_service.configured():
        return "Password Safe has no API credentials"
    return ""


def _verdict(outcomes) -> str:
    if not outcomes:
        return STATE_NONE
    statuses = {o["status"] for o in outcomes}
    if statuses == {OK}:
        return STATE_OK
    if statuses == {SKIPPED}:
        return STATE_SKIPPED
    if OK not in statuses and FAILED in statuses:
        return STATE_FAILED
    return STATE_PARTIAL


def summary(report: dict) -> str:
    """One line for a job's progress message or a log."""
    counts = {s: 0 for s in (OK, SKIPPED, FAILED)}
    for o in report.get("outcomes") or []:
        counts[o["status"]] += 1
    if report.get("state") == STATE_NONE:
        return "no workstream attributes to apply"
    return (f"workstream attributes: {counts[OK]} applied, {counts[SKIPPED]} skipped, "
            f"{counts[FAILED]} failed")


def _outcome(action, type_name, value, status, detail="") -> dict:
    return {"action": action, "type": type_name, "value": value,
            "status": status, "detail": detail}


async def apply(db, workgroup: Optional[str], managed_system_id,
                previous_workgroup: Optional[str] = None, *, vocabulary=None) -> dict:
    """Write ``workgroup``'s attributes onto one managed system. Never raises.

    With ``previous_workgroup`` (a reassign), that workstream's values are removed first —
    except one the new workstream also sets, which is left alone and reported ``skipped``
    rather than removed and re-added. The same workstream on both sides is a plain apply.

    ``vocabulary`` lets a caller applying to many systems read it once.

    Returns ``{"state", "managed_system_id", "outcomes": [{action, type, value, status,
    detail}]}``.
    """
    from . import ps_api_service, ps_attribute_catalog as pac, workgroup_service

    sid = str(managed_system_id or "").strip()
    wanted = workgroup_service.ps_attributes(workgroup, db) if workgroup else {}
    previous = {}
    if previous_workgroup and not _same(previous_workgroup, workgroup):
        previous = workgroup_service.ps_attributes(previous_workgroup, db)
    report = {"state": STATE_NONE, "managed_system_id": sid, "outcomes": []}

    plan = []   # (action, type, value)
    for type_name, value in previous.items():
        if not _same(wanted.get(type_name), value):
            plan.append(("remove", type_name, value))
    for type_name, value in wanted.items():
        plan.append(("assign", type_name, value))
    if not plan:
        return report

    outcomes = report["outcomes"]
    why_not = "" if sid else "this VM has no Password Safe managed system"
    why_not = why_not or unavailable_reason()
    if not why_not and vocabulary is None:
        try:
            raw = await ps_api_service.read_attribute_vocabulary()
            if raw.get("state") != ps_api_service.PROBE_OK:
                raise ps_api_service.PSApiError(
                    raw.get("detail") or "the attribute vocabulary could not be read")
            vocabulary = pac.build_vocabulary(raw["types"], raw["values_by_type"])
        except Exception as exc:  # noqa: BLE001 — reported per attribute below
            logger.warning("workstream attributes: vocabulary read failed", exc_info=True)
            detail = (str(exc) if isinstance(exc, ps_api_service.PSApiError)
                      else "the Password Safe read failed — see the dashboard log")
            for action, type_name, value in plan:
                outcomes.append(_outcome(action, type_name, value, FAILED, detail))
            report["state"] = _verdict(outcomes)
            return report

    for action, type_name, value in plan:
        if why_not:
            outcomes.append(_outcome(action, type_name, value, SKIPPED, why_not))
            continue
        if action == "assign" and _same(previous.get(type_name), value):
            outcomes.append(_outcome(action, type_name, value, SKIPPED,
                                     f"unchanged from '{previous_workgroup}'; left as it was"))
            continue
        try:
            _type_row, value_row = resolve(vocabulary, type_name, value)
        except LookupError as exc:
            if action == "remove":
                # Nothing in the tenant to remove: the value itself is gone, so no system
                # can still carry it. Not a success — the call was never made.
                outcomes.append(_outcome(action, type_name, value, SKIPPED, str(exc)))
            else:
                outcomes.append(_outcome(action, type_name, value, FAILED,
                                         f"{exc} — re-save the workstream's attributes"))
            continue
        if action == "assign":
            try:
                pac.assert_assignable(vocabulary, value_row["attribute_id"])
            except ValueError as exc:
                outcomes.append(_outcome(action, type_name, value, FAILED, str(exc)))
                continue
        try:
            await ps_api_service.set_managed_system_attribute(
                sid, value_row["attribute_id"], assign=(action == "assign"))
        except Exception as exc:  # noqa: BLE001 — one attribute must not end the run
            logger.warning("workstream attribute %s %s=%s on system %s failed",
                           action, type_name, value, sid, exc_info=True)
            outcomes.append(_outcome(
                action, type_name, value, FAILED,
                str(exc) if isinstance(exc, ps_api_service.PSApiError)
                else "the attribute change failed — see the dashboard log"))
            continue
        outcomes.append(_outcome(action, type_name, value, OK))

    report["state"] = _verdict(outcomes)
    return report


async def on_reassign(db, job, previous_workgroup: Optional[str]) -> Optional[dict]:
    """The reassign routes' call: swap ``previous_workgroup``'s attributes for the job's
    current workstream's on the VM's managed system. ``None`` when there is nothing to do
    — no deploy row, never onboarded, or neither workstream names attributes — so the
    route's response only carries a report when Password Safe was actually involved."""
    if job is None:
        return None
    from . import workgroup_service
    sid = str(job.metadata_dict.get("ps_managed_system_id") or "").strip()
    if not sid:
        return None
    if not (workgroup_service.ps_attributes(job.workgroup, db)
            or workgroup_service.ps_attributes(previous_workgroup, db)):
        return None
    report = await apply(db, job.workgroup, sid, previous_workgroup=previous_workgroup)
    if any(o["status"] == OK for o in report["outcomes"]):
        await invalidate_cache()
    return report


async def invalidate_cache() -> None:
    """Drop the inventory page's cached attribute snapshot (api/inventory._ps_snapshot),
    so a change this module wrote shows on the next load instead of up to 15 min later.
    By prefix: the key is parameterised by the configured Password Safe workgroup."""
    from . import cache_service
    try:
        await cache_service.invalidate_prefix("ps_attributes")
    except Exception:  # noqa: BLE001 — a stale cache is not worth failing a write over
        logger.warning("could not invalidate the ps_attributes cache", exc_info=True)


BACKFILL_MAX = 50


async def backfill(db, workgroup: str, *, offset: int = 0) -> dict:
    """Apply ``workgroup``'s attributes to the managed systems its EXISTING resources
    match — the systems onboarded before the workstream named any attributes.

    Matched the way /inventory matches (``ps_attribute_catalog.build_index`` /
    ``match``): the recorded system id first, then address. An ambiguous row is listed
    and left alone rather than written to both candidates. At most :data:`BACKFILL_MAX`
    systems per call, in a stable order; ``next_offset`` is where the next call starts,
    ``None`` when this was the last page.

    Raises :class:`RuntimeError` when Password Safe cannot be asked at all.
    """
    from . import (config_service, inventory_service, ps_api_service,
                   ps_attribute_catalog as pac)

    why_not = unavailable_reason()
    if why_not:
        raise RuntimeError(why_not)
    wanted = (workgroup or "").strip().lower()
    rows = [r for r in inventory_service.collect(db)
            if (r.get("workgroup") or "").lower() == wanted]

    inventory = await ps_api_service.read_attribute_inventory(
        workgroup=ps_api_service.configured_workgroup())
    if not inventory.get("reachable"):
        raise RuntimeError(inventory.get("detail") or "Password Safe could not be read")
    index = pac.build_index(inventory["assets"]["rows"], inventory["managed_systems"]["rows"])

    systems, ambiguous, unmatched = {}, [], 0
    for row in rows:
        m = pac.match(row, index)
        if m["state"] == pac.STATE_AMBIGUOUS:
            ambiguous.append({"name": row.get("name") or row.get("id") or "",
                              "detail": m["detail"]})
            continue
        found = [o["object_id"] for o in m["objects"] if o["kind"] == pac.KIND_SYSTEM]
        if not found:
            unmatched += 1
        for sid in found:
            systems.setdefault(sid, row.get("name") or "")

    ordered = sorted(systems, key=lambda s: (len(s), s))
    offset = max(0, int(offset or 0))
    page = ordered[offset:offset + BACKFILL_MAX]

    raw = await ps_api_service.read_attribute_vocabulary()
    if raw.get("state") != ps_api_service.PROBE_OK:
        raise RuntimeError(raw.get("detail") or "the attribute vocabulary could not be read")
    vocabulary = pac.build_vocabulary(raw["types"], raw["values_by_type"])

    targets = []
    for sid in page:
        report = await apply(db, workgroup, sid, vocabulary=vocabulary)
        targets.append({**report, "name": systems[sid]})
    if any(o["status"] == OK for t in targets for o in t["outcomes"]):
        await invalidate_cache()
    end = offset + len(page)
    return {"matched": len(ordered), "offset": offset, "targets": targets,
            "next_offset": end if end < len(ordered) else None,
            "ambiguous": ambiguous, "unmatched": unmatched}
