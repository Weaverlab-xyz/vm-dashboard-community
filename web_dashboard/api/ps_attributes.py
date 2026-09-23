"""Assigning Password Safe attributes to assets, and re-running the Smart Rules they feed.

The write half of the attributes feature. ``/inventory`` shows what an asset carries;
this changes it, and then lets an operator re-run the rule that acts on it.

**Its own router rather than a POST on /inventory.** That module's docstring is explicit
that it is read-only by design — "acting on a resource is the owning cloud scope's job" —
and a write bolted onto it would be the first exception to a rule the next person would
then reasonably break again.

**Admin-only.** There is no ``password_safe`` permission scope and this does not add one:
``api/auth.py`` records that adding a scope silently revokes it for everyone, because
nothing is stored against a key nobody has been granted. Writing into a customer's PAM
tenant is also not a capability to hand out by default. If a narrower grant is wanted
later, it should be a deliberate scope addition with the revocation understood.

**An attribute is not free text**, which is what makes this simpler than the cloud tag
editor. A type owns a fixed set of values, each with its own ``AttributeID``, and
assigning one names that id — so there is no charset to police, no key to invent, and a
typo is not expressible. What CAN go wrong is naming an id this tenant does not have, or
one belonging to a read-only type, and ``ps_attribute_catalog.assert_assignable`` refuses
both by name.

**The Smart Rule re-run is a separate action, never automatic.** Applying an attribute and
watching a rule act on it are two things an operator wants to see happen in order — and
firing a rule per target in a bulk apply would be both surprising and expensive. Password
Safe processes asynchronously anyway, so "automatic" would not even mean "finished".
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, get_db
from ..services import (config_service, job_service, ps_api_service,
                        ps_attribute_catalog as pac)
from .auth import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/password-safe", tags=["password-safe"])

# Dotted, so api/audit.py's PREFIX filter groups every attribute mutation — and groups
# them apart from `tags.` rather than under it. These change a customer's PAM tenant, not
# a cloud resource, and an operator asking "what did the dashboard write into Password
# Safe" should get exactly those rows.
AUDIT_ASSIGN = "attributes.assign"
AUDIT_REMOVE = "attributes.remove"

MAX_TARGETS = 50


def _enabled() -> bool:
    return config_service.get_bool("password_safe_enabled",
                                   settings.password_safe_enabled)


def _require_ready() -> None:
    """Refuse before any call when the feature is off or has no credentials.

    Two different answers on purpose: "off" is a decision somebody made, "unconfigured" is
    a setup step nobody finished, and sending an operator to the wrong one of those wastes
    their afternoon.
    """
    if not _enabled():
        raise HTTPException(status_code=404,
                            detail="Password Safe is not enabled on this instance.")
    if not ps_api_service.configured():
        raise HTTPException(
            status_code=409,
            detail=("Password Safe has no API credentials yet — set them in "
                    "Settings → Integrations."))


async def _vocabulary() -> list:
    raw = await ps_api_service.read_attribute_vocabulary()
    if raw["state"] != ps_api_service.PROBE_OK:
        raise HTTPException(
            status_code=502,
            # `_probe` already withholds the tenant's response body; this passes its
            # fixed string through and adds nothing of its own (CodeQL
            # py/stack-trace-exposure).
            detail=raw["detail"] or "the attribute vocabulary could not be read")
    return pac.build_vocabulary(raw["types"], raw["values_by_type"])


@router.get("/attribute-vocabulary", summary="Attribute types and their values")
async def attribute_vocabulary(_: User = Depends(require_admin)):
    """The picker. Read-only types are returned WITH their `read_only` flag rather than
    filtered out — an operator looking for `Criticality` should find it and be told why
    it is not offered, not be left wondering whether the dashboard can see it."""
    _require_ready()
    return {"types": await _vocabulary()}


class AssetAttributeRequest(BaseModel):
    """One attribute, applied to or removed from every target."""
    asset_ids: List[int]
    attribute_id: int
    assign: bool = True


@router.post("/asset-attributes", summary="Assign or remove one attribute across assets")
async def set_asset_attributes(
    payload: AssetAttributeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Apply one attribute change to one or many assets.

    Per-asset outcomes are reported BY NAME, never as a count: a bulk apply across a
    selection is N independent calls, and "3 failed" means checking all of them.
    """
    _require_ready()
    vocabulary = await _vocabulary()

    try:
        type_row, value = pac.assert_assignable(vocabulary, payload.attribute_id)
    except ValueError as exc:
        # 409 rather than 400: the request is well formed and the caller is entitled to
        # make it — this particular attribute is not assignable. 400 reads as a typo.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    targets = list(dict.fromkeys(payload.asset_ids or []))   # de-duped, order kept
    if not targets:
        raise HTTPException(status_code=400, detail="No assets selected.")
    if len(targets) > MAX_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=f"{len(targets)} assets selected; the limit for one change is "
                   f"{MAX_TARGETS}.")

    updated, failed = [], []
    for asset_id in targets:
        try:
            await ps_api_service.set_asset_attribute(
                asset_id, payload.attribute_id, assign=payload.assign)
        except Exception as exc:  # noqa: BLE001 — one asset must not end the run
            logger.warning("Password Safe attribute change failed for asset %s",
                           asset_id, exc_info=True)
            failed.append({"name": str(asset_id), "error": str(exc)})
            continue
        updated.append({"name": str(asset_id),
                        "type": type_row["name"], "value": value["value"]})
        # After the call, never before: a row claiming a change the tenant refused is
        # worse than no row. One per asset, so /audit's target filter answers "what has
        # this dashboard written about this asset".
        job_service.log_audit(
            db, current_user.username,
            AUDIT_ASSIGN if payload.assign else AUDIT_REMOVE,
            target_vm=f"asset:{asset_id}",
            details={"attribute_id": payload.attribute_id,
                     "type": type_row["name"], "value": value["value"]})

    return {"count": len(updated), "updated": updated, "failed": failed,
            "type": type_row["name"], "value": value["value"],
            "assigned": bool(payload.assign)}


@router.get("/smart-rules", summary="Smart Rules, for the re-run picker")
async def smart_rules(_: User = Depends(require_admin)):
    """The rules an operator might re-run after changing an attribute.

    Sorted by title because the Password Safe POC runbook numbers its rules ("1 - List",
    "5 - Map and Access") and that numbering is how an SE finds the one they were just
    talking about — the same ordering `pov_ps_config.rules` settled on.
    """
    _require_ready()
    inventory = await ps_api_service.read_config_inventory()
    probe = inventory.get("smart_rules") or {}
    if probe.get("state") != ps_api_service.PROBE_OK:
        raise HTTPException(
            status_code=502,
            detail=probe.get("detail") or "the Smart Rules could not be read")
    out = []
    for row in probe.get("rows") or []:
        rule_id = row.get("SmartRuleID") or row.get("ID")
        if not rule_id:
            continue
        out.append({"id": int(rule_id),
                    "title": str(row.get("Title") or ""),
                    "last_processed": str(row.get("LastProcessedDate") or "")})
    return {"rules": sorted(out, key=lambda r: r["title"].lower())}


@router.post("/smart-rules/{rule_id}/process", summary="Re-run one Smart Rule")
async def process_smart_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Queue a Smart Rule re-run. Returns as soon as Password Safe accepts it.

    Deliberately not chained onto the write above. Password Safe processes
    asynchronously, so chaining would neither wait nor report — and in front of a
    customer the value is in watching the attribute land and *then* the rule act.
    """
    _require_ready()
    try:
        result = await ps_api_service.process_smart_rule(rule_id)
    except ps_api_service.PSApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    job_service.log_audit(db, current_user.username, "attributes.smart_rule_processed",
                          details={"rule_id": rule_id})
    return result
