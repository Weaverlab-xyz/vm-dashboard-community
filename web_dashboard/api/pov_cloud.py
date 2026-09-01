"""The POV's own read-only view of its cloud provider.

    GET /api/pov/cloud/overview   — what this instance has running, and what is orphaned

A POV-owned route, deliberately, rather than re-opening ``/aws`` on a POV instance. The
demo cloud consoles stay 404 there because their deploy paths resolve the GLOBAL
BeyondTrust tenant singletons — a VM built through them would onboard into the demo tenant
rather than into this POV's, silently, because both paths "work". So this page shows and
never creates: there is no deploy button here and no endpoint behind one.

The half worth having is the **orphan sweep**. ``pov_reconcile`` compares each POV row
against the platform and can tell you a row's environment has gone. It cannot tell you the
reverse — that the cloud is holding an environment no row remembers — and that is the
direction cost leaks in: a provision that died before its row was written, a POV destroyed
from the console leaving its network behind, a row deleted by hand. Every resource carries
``povManagedBy=vm-dashboard``, so the question is answerable, and answering it is the whole
reason to render a cloud page on a POV instance at all.
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import PovEnvironment, User, get_db
from ..services import (lab_platforms, pov_cloud_cost, pov_cloud_env, pov_env_service)
from .auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pov/cloud", tags=["pov"])


@router.get("/overview")
async def overview(db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    """Everything the cloud view renders, in one call.

    Answers 200 with ``configured: false`` rather than refusing when no cloud is selected
    or its credentials are missing. The page exists to explain the state of the cloud
    integration, and a 409 would leave it unable to explain the most common state of all.
    """
    cloud = lab_platforms.selected_cloud()
    out = {
        "cloud": cloud,
        "configured": False,
        "reason": "",
        "region": "",
        "environments": [],
        "orphans": [],
        "footprint": pov_cloud_cost.footprint([]),
        "estimate": {"available": False, "reason": ""},
    }
    if not cloud:
        out["reason"] = ("No POV cloud provider is selected. Choose one in "
                         "Settings → Integrations → POV cloud provider.")
        return out

    mod = lab_platforms.adapter(cloud)
    label = lab_platforms.capabilities(cloud)["label"]
    if not mod.configured():
        out["reason"] = (f"{label} is selected but has no credentials. Add them in "
                         f"Settings → Integrations → POV cloud provider.")
        return out
    out["configured"] = True
    out["region"] = pov_cloud_env.driver(cloud).default_region()

    try:
        live = await mod.list_environments()
    except Exception as exc:  # noqa: BLE001
        # Reported, not raised. A page whose whole job is to say what is running should
        # say "I could not ask" rather than 500 — and the answer names the remedy, which a
        # stack trace would not. Keeping it a return value also keeps a caught exception
        # out of a response body, which is the shape CodeQL fires on.
        logger.warning("POV cloud overview: listing %s failed", cloud, exc_info=True)
        out["reason"] = (f"{label} did not answer a read: {type(exc).__name__}. Check the "
                         f"credentials with Test connection in Settings.")
        return out

    # Rows this dashboard owns, by the environment id it recorded. Destroyed rows are
    # excluded so a finished POV whose teardown left something behind reads as an ORPHAN
    # rather than as a live environment — which is the honest description and the one that
    # gets it cleaned up.
    rows = {
        (r.platform_environment_id or ""): r
        for r in db.query(PovEnvironment)
                   .filter(PovEnvironment.platform == cloud,
                           PovEnvironment.status != pov_env_service.STATUS_DESTROYED)
                   .all()
        if r.platform_environment_id
    }

    known, orphans = [], []
    for e in live:
        row = rows.get(e.get("id") or "")
        entry = dict(e)
        entry["pov_id"] = row.id if row else ""
        entry["pov_name"] = row.name if row else ""
        entry["expires_at"] = (row.expires_at.isoformat()
                               if row is not None and row.expires_at else "")
        (known if row else orphans).append(entry)

    out["environments"] = known
    out["orphans"] = orphans
    # The footprint covers BOTH. An orphan costs exactly what a live POV costs, and a
    # total that quietly excluded it would understate the bill by the part nobody is
    # watching.
    out["footprint"] = pov_cloud_cost.footprint(known + orphans)
    out["estimate"] = pov_cloud_cost.estimate(known + orphans, out["region"],
                                              cloud)
    return out
