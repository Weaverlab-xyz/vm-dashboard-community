"""BeyondTrust Gateway API.

  GET    /api/gateways              — every gateway the dashboard deployed
  GET    /api/gateways/suggest-name — a free, cloud-legal default for the form
  POST   /api/gateways/deploy       — stand up another gateway host
  DELETE /api/gateways/{id}         — remove a requested gateway

The dashboard has always ensured one shared gateway per cloud and torn it down when
idle. That stays. These endpoints add gateways an operator asks for, to carry session
load — no cap, no reference counting, and removed only on request.

Enqueue-only, like every other long operation: the handler validates, creates the
job, and returns. ``services.gateway_service`` runs it on the job runner, so a
gunicorn recycle mid-deploy can't strand a half-built host.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import gateway_service, job_service, region_catalog, region_config
from ..services.gateway_service import GatewayError
from .auth import get_current_user, require_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/gateways", tags=["gateways"])


class DeployGatewayRequest(BaseModel):
    cloud: str
    region: str = ""
    zone: str = ""
    name: str = ""


def _placement(req: "DeployGatewayRequest") -> tuple[str, str]:
    """Validate and normalise the requested (region, zone) for ``req.cloud``.

    The region only reaches the cloud SDKs by way of the per-region config lookup, so
    a malformed one used to surface as a confusing resource error much later. The
    zone/region cross-check matters more than it looks: the GCP gateway's subnet is
    derived from its *zone*, so a zone in another region would quietly relocate the
    host and ignore the chosen region.

    Blank stays blank — that means "the configured default", resolved at deploy time.
    """
    region = ""
    if req.region.strip():
        if not region_catalog.validate(req.cloud, req.region):
            raise HTTPException(status_code=400,
                                detail=f"Invalid {req.cloud} region '{req.region}'.")
        region = region_catalog.normalize(req.cloud, req.region)

    # Only GCP places the host in a zone; the other clouds have no use for one.
    if req.cloud != "gcp" or not req.zone.strip():
        return region, ""
    if not region_catalog.validate_zone(req.zone):
        raise HTTPException(status_code=400, detail=f"Invalid GCP zone '{req.zone}'.")
    zone = region_catalog.normalize("gcp", req.zone)
    if region and not region_config.zone_in_region(zone, region):
        raise HTTPException(
            status_code=400,
            detail=(f"Zone '{zone}' is not in region '{region}'. The gateway's subnet "
                    f"follows its zone, so it would come up outside '{region}'."),
        )
    return region, zone


@router.get("")
async def list_gateways(
    cloud: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Every gateway, managed and requested. Read-only, so any authenticated user may
    see what exists — deploying and removing need admin."""
    return {"gateways": gateway_service.list_gateways(db, cloud)}


@router.get("/suggest-name")
async def suggest_name(
    cloud: str,
    region: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("admin", "write")),
):
    if cloud not in gateway_service.CLOUDS:
        raise HTTPException(status_code=400, detail=f"Unknown cloud '{cloud}'.")
    try:
        return {"name": gateway_service.suggest_name(db, cloud, region)}
    except GatewayError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/deploy", status_code=202)
async def deploy_gateway(
    req: DeployGatewayRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("admin", "write")),
):
    """Queue another gateway host. Deliberately uncapped — the right number is a
    function of session load, which the dashboard can't see."""
    if req.cloud not in gateway_service.CLOUDS:
        raise HTTPException(status_code=400, detail=f"Unknown cloud '{req.cloud}'.")
    region, zone = _placement(req)
    try:
        name = gateway_service.validate_name(db, req.cloud, req.name)
    except GatewayError as e:
        raise HTTPException(status_code=400, detail=str(e))

    from ..database import Gateway
    import uuid as _uuid
    row = Gateway(
        id=str(_uuid.uuid4()), cloud=req.cloud, region=region or None,
        zone=zone or None, name=name, status="provisioning", managed=False,
        created_by=current_user.username,
    )
    db.add(row)
    db.commit()

    job = job_service.create_job(
        db,
        job_type="gateway_deploy",
        created_by=current_user.username,
        metadata={
            "job_type": "gateway_deploy",
            "gateway_id": row.id,
            "cloud": req.cloud,
            "region": region,
            "zone": zone,
            "name": name,
        },
    )
    row.deploy_job_id = job.id
    db.commit()

    job_service.log_audit(
        db, current_user.username, "gateway_deploy",
        details={"cloud": req.cloud, "region": region, "name": name},
    )
    return {"job_id": job.id, "gateway_id": row.id, "name": name, "status": "pending"}


@router.delete("/{gateway_id}", status_code=202)
async def destroy_gateway(
    gateway_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("admin", "delete")),
):
    """Remove a requested gateway. The managed one is refused: its lifecycle belongs
    to the reference-counted ensure/idle pair, and deleting it here would leave the
    auto-ensure to recreate it while the row said it was gone."""
    row = gateway_service.get_gateway(db, gateway_id)
    if row is None or row.status == "deleted":
        raise HTTPException(status_code=404, detail=f"Gateway {gateway_id} not found.")
    if row.managed:
        raise HTTPException(
            status_code=400,
            detail=("This is the auto-managed gateway. It is removed automatically "
                    "once nothing is using it, and recreated when something needs "
                    "one — it cannot be deleted directly."),
        )

    row.status = "deleting"
    db.commit()

    job = job_service.create_job(
        db,
        job_type="gateway_teardown",
        created_by=current_user.username,
        metadata={
            "job_type": "gateway_teardown",
            "gateway_id": row.id,
            "cloud": row.cloud,
            "region": row.region or "",
            "zone": row.zone or "",
            "name": row.name,
        },
    )
    job_service.log_audit(
        db, current_user.username, "gateway_teardown",
        details={"cloud": row.cloud, "name": row.name},
    )
    return {"job_id": job.id, "gateway_id": row.id, "status": "pending"}
