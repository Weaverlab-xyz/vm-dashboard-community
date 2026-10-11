"""Persona × cloud access bundles in Entitle — Settings → Integrations → Entitle.

  GET    /api/entitle/persona-bundles                    — catalog, cloud pins, applied sets
  GET    /api/entitle/persona-bundles/cloud-candidates   — integrations to pin per cloud
  PUT    /api/entitle/persona-bundles/clouds             — pin a cloud's integration/resource
  POST   /api/entitle/persona-bundles/preview            — resolve the selected bundles
  POST   /api/entitle/persona-bundles/apply              — resolve again, apply the ready ones
  DELETE /api/entitle/persona-bundles/sets/{id}          — remove an applied set

Administrator-only, like the rest of the Entitle panel: a bundle is standing
infrastructure in the tenant that decides what people can request. ``apply`` resolves the
keys afresh rather than trusting a preview the browser sends back, so what is created is
what Entitle holds at apply time.
"""
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import EntitleBundleSet, User, get_db
from ..services import entitle_persona_bundles as bundles
from ..services import entitle_public_api as api
from ..services import entitle_tiers, job_service
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/entitle/persona-bundles", tags=["entitle"])

# Application-name fragments that plausibly govern each cloud. Ordering hints only.
_CLOUD_HINTS = {"aws": ("aws", "amazon"), "azure": ("azure",),
                "gcp": ("gcp", "google"), "oci": ("oci", "oracle cloud")}


class KeysRequest(BaseModel):
    keys: List[str]


class CloudPin(BaseModel):
    cloud: str
    integration_id: str = ""
    resource: str = ""


@router.get("")
def overview(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    flows = entitle_tiers.resolve_workflows()
    return {
        "configured": api.configured(),
        "catalog": bundles.catalog(),
        "clouds": bundles.cloud_settings(),
        "sets": bundles.list_sets(db),
        "tiers": [{"id": t, "label": entitle_tiers.TIER_LABELS[t],
                   "workflow_set": bool(flows[t]["id"]),
                   "workflow_fallback": flows[t]["fallback"]} for t in entitle_tiers.TIERS],
    }


@router.get("/cloud-candidates")
async def cloud_candidates(cloud: str, user: User = Depends(require_admin)):
    if cloud not in bundles.CLOUDS:
        raise HTTPException(status_code=400, detail="unknown cloud")
    try:
        rows = await api.get_paged("/public/v1/integrations", what="integrations")
    except api.EntitleApiError as e:
        return {"integrations": [], "error": str(e)}
    out = []
    for r in rows:
        app = r.get("application")
        app = str((app.get("name") if isinstance(app, dict) else app) or "")
        out.append({"id": str(r.get("id") or ""), "name": str(r.get("name") or ""),
                    "application": app,
                    "suggested": any(h in app.lower() for h in _CLOUD_HINTS[cloud])})
    out = [o for o in out if o["id"]]
    out.sort(key=lambda o: (not o["suggested"], o["name"].lower()))
    return {"integrations": out}


@router.put("/clouds")
async def pin_cloud(req: CloudPin, db: Session = Depends(get_db),
                    user: User = Depends(require_admin)):
    from ..services import config_service
    if req.cloud not in bundles.CLOUDS:
        raise HTTPException(status_code=400, detail="unknown cloud")
    iid = req.integration_id.strip()
    if iid:
        try:
            rows = await api.get_paged("/public/v1/integrations", what="integrations")
        except api.EntitleApiError as e:
            raise HTTPException(status_code=503, detail=str(e))
        if not any(str(r.get("id")) == iid for r in rows):
            raise HTTPException(status_code=400,
                                detail="That integration is not in this Entitle tenant.")
    config_service.set(f"entitle_cloud_integration_{req.cloud}", iid)
    config_service.set(f"entitle_cloud_resource_{req.cloud}", req.resource.strip()[:255])
    job_service.log_audit(db, user.username, "entitle_cloud_pin", details={
        "cloud": req.cloud, "integration_id": iid, "resource": req.resource.strip()})
    return {"clouds": bundles.cloud_settings()}


async def _resolve(db: Session, keys: List[str]) -> list:
    if not keys:
        raise HTTPException(status_code=400, detail="Pick at least one bundle.")
    try:
        return await bundles.resolve(db, keys)
    except api.EntitleApiError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/preview")
async def preview(req: KeysRequest, db: Session = Depends(get_db),
                  user: User = Depends(require_admin)):
    return {"bundles": await _resolve(db, req.keys)}


@router.post("/apply", status_code=202)
async def apply(req: KeysRequest, db: Session = Depends(get_db),
                user: User = Depends(require_admin)):
    rows = await _resolve(db, req.keys)
    try:
        bset, job = bundles.start_apply(db, rows, created_by=user.username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    skipped = [{"key": r["key"], "blockers": r["blockers"]} for r in rows if not r["ready"]]
    job_service.log_audit(db, user.username, "entitle_bundles_apply", details={
        "set_id": bset.id,
        "bundles": [{"key": r["key"], "roles": len(r["role_ids"])} for r in rows if r["ready"]],
        "skipped": skipped})
    return {"set": bundles.set_to_dict(bset), "job_id": job.id, "skipped": skipped}


@router.delete("/sets/{set_id}", status_code=202)
def delete_set(set_id: str, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    bset = db.query(EntitleBundleSet).filter(EntitleBundleSet.id == set_id).first()
    if not bset or bset.status == "deleted":
        raise HTTPException(status_code=404, detail="bundle set not found")
    try:
        job = bundles.start_delete(db, bset, created_by=user.username)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    job_service.log_audit(db, user.username, "entitle_bundles_delete",
                          details={"set_id": bset.id})
    return {"set": bundles.set_to_dict(bset), "job_id": job.id}
