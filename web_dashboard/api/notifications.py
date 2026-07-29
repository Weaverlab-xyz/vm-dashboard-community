"""Outbound notification API — manage webhook endpoints and read the delivery log.

Admin-only throughout, for two reasons: a webhook endpoint is a place dashboard data
gets sent, and the delivery log holds the rendered body of every alert, which names
resources the reader might not otherwise be able to see.

Endpoint URLs and HMAC secrets are **never returned**. A Slack or Teams webhook URL is
itself a bearer credential — whoever holds it can post to the channel — so responses
carry a scheme+host hint and a ``has_secret`` boolean instead.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import job_service, notification_service, notify_policy, notify_transports
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/notifications", tags=["notifications"])

MAX_ENDPOINTS = 20


class EndpointCreate(BaseModel):
    name: str = Field(default="", max_length=100)
    url: str = ""
    fmt: str = "custom"
    secret: str = ""
    event_types: str = ""
    enabled: bool = True


class EndpointPatch(BaseModel):
    """Every field optional: a toggle-only PATCH must not blank the URL or secret.

    Same contract ``api/setup.patch_feature_config`` gives the feature panels — only
    keys the caller actually sent are applied.
    """
    name: Optional[str] = None
    url: Optional[str] = None
    fmt: Optional[str] = None
    secret: Optional[str] = None
    event_types: Optional[str] = None
    enabled: Optional[bool] = None


def _validate(url: str, fmt: str) -> None:
    if not (url or "").strip():
        raise HTTPException(status_code=400, detail="A webhook URL is required.")
    if not url.strip().lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400,
                            detail="The webhook URL must start with http:// or https://")
    if fmt not in notify_transports.FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown format {fmt!r}. Expected one of: "
                   f"{', '.join(notify_transports.FORMATS)}.")


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/endpoints")
async def list_endpoints(db: Session = Depends(get_db),
                         current_user: User = Depends(require_admin)):
    return {"endpoints": [notification_service.endpoint_public(e)
                          for e in notification_service.list_endpoints(db)],
            "formats": list(notify_transports.FORMATS),
            "known_events": sorted(notify_policy.EVENT_SEVERITY)}


@router.post("/endpoints")
async def create_endpoint(payload: EndpointCreate,
                          db: Session = Depends(get_db),
                          current_user: User = Depends(require_admin)):
    _validate(payload.url, payload.fmt)
    if len(notification_service.list_endpoints(db)) >= MAX_ENDPOINTS:
        raise HTTPException(status_code=400,
                            detail=f"At most {MAX_ENDPOINTS} endpoints are supported.")
    ep = notification_service.create_endpoint(
        db, name=payload.name or payload.fmt, url=payload.url, fmt=payload.fmt,
        secret=payload.secret, event_types=payload.event_types,
        enabled=payload.enabled, actor=current_user.username)
    # Worth a hash-chain entry: an admin has just told the dashboard to start sending
    # its data somewhere. The URL is not recorded — only that one was set.
    _audit(db, current_user, "notification_endpoint_created",
           {"endpoint_id": ep.id, "name": ep.name, "fmt": ep.fmt})
    return notification_service.endpoint_public(ep)


@router.patch("/endpoints/{endpoint_id}")
async def patch_endpoint(endpoint_id: str, payload: EndpointPatch,
                         db: Session = Depends(get_db),
                         current_user: User = Depends(require_admin)):
    fields = payload.model_dump(exclude_unset=True)
    if "url" in fields and fields["url"]:
        _validate(fields["url"], fields.get("fmt") or "custom")
    if "fmt" in fields and fields["fmt"] not in notify_transports.FORMATS:
        raise HTTPException(status_code=400, detail=f"Unknown format {fields['fmt']!r}.")
    ep = notification_service.update_endpoint(db, endpoint_id, **fields)
    if ep is None:
        raise HTTPException(status_code=404, detail="No such notification endpoint.")
    _audit(db, current_user, "notification_endpoint_updated",
           {"endpoint_id": ep.id, "name": ep.name, "fields": sorted(fields)})
    return notification_service.endpoint_public(ep)


@router.delete("/endpoints/{endpoint_id}")
async def delete_endpoint(endpoint_id: str,
                          db: Session = Depends(get_db),
                          current_user: User = Depends(require_admin)):
    if not notification_service.delete_endpoint(db, endpoint_id):
        raise HTTPException(status_code=404, detail="No such notification endpoint.")
    _audit(db, current_user, "notification_endpoint_deleted", {"endpoint_id": endpoint_id})
    return {"deleted": endpoint_id}


@router.post("/endpoints/{endpoint_id}/test")
async def test_endpoint(endpoint_id: str,
                        db: Session = Depends(get_db),
                        current_user: User = Depends(require_admin)):
    """Send one real message now and return the verbatim outcome.

    Runs inline rather than through the worker's drain loop, and ignores dry-run: the
    entire value of the button is the immediate ``CERTIFICATE_VERIFY_FAILED`` /
    ``invalid_payload`` / ``HTTP 403`` text. It reads the *stored* configuration, so
    what it tests is what a real notification would use — the same contract
    ``POST /api/setup/oidc/test`` has.
    """
    result = await notification_service.test_send(db, endpoint_id,
                                                  actor=current_user.username)
    if result.get("error") == "no such endpoint":
        raise HTTPException(status_code=404, detail="No such notification endpoint.")
    _audit(db, current_user, "notification_test_sent",
           {"endpoint_id": endpoint_id, "ok": result.get("ok")})
    return result


# ── Delivery log ─────────────────────────────────────────────────────────────

@router.get("/deliveries")
async def list_deliveries(page: int = Query(1, ge=1),
                          page_size: int = Query(20, ge=1, le=200),
                          status: str = "", channel: str = "",
                          event_type: str = "", resource_id: str = "",
                          db: Session = Depends(get_db),
                          current_user: User = Depends(require_admin)):
    rows, total = notification_service.list_deliveries(
        db, page=page, page_size=page_size, status=status, channel=channel,
        event_type=event_type, resource_id=resource_id)
    return {"deliveries": [notification_service.delivery_public(r) for r in rows],
            "total": total, "page": page, "page_size": page_size}


@router.get("/summary")
async def delivery_summary(hours: int = Query(24, ge=1, le=720),
                           db: Session = Depends(get_db),
                           current_user: User = Depends(require_admin)):
    out = notification_service.summary(db, hours=hours)
    out["enabled"] = notify_policy.enabled()
    out["dry_run"] = notify_policy.dry_run()
    out["endpoints"] = len(notification_service.list_endpoints(db, enabled_only=True))
    # Blank means deep links are omitted from every message — worth surfacing, because
    # the symptom (alerts with no link) looks like a bug rather than a setting.
    out["base_url_set"] = bool(notify_policy.base_url())
    return out


def _audit(db: Session, user: User, action: str, details: dict) -> None:
    """Best-effort. Deliberately NOT called per delivery — a hash-chain append takes an
    advisory lock, and an entry per notification would bury the entries that matter."""
    try:
        job_service.log_audit(db, user.username, action, details=details)
    except Exception:
        logger.warning("could not audit %s", action, exc_info=True)
