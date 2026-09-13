"""
Workload Lab → Cloud API — a short-lived AWS/Azure credential for a workload.

  GET    /api/workload-cloud              — list the registered identities
  GET    /api/workload-cloud/options      — clouds, dynamic secrets, and what is missing
  POST   /api/workload-cloud              — register one against a dynamic secret
  GET    /api/workload-cloud/{id}         — one identity
  GET    /api/workload-cloud/{id}/lease   — the PROVIDER's view of the current lease
  POST   /api/workload-cloud/{id}/issue   — mint (metered)
  POST   /api/workload-cloud/{id}/revoke  — release early (Azure only)
  DELETE /api/workload-cloud/{id}         — revoke what it can, then retire the identity

**No endpoint returns a credential, and none ever will.** The values a mint produces go to
the caller that asked Workload Credentials for them — a consumer with its own token, which
is what puts *that consumer* in WC's audit log. An endpoint here that proxied them would
make the audit trail say only that the dashboard minted, and would be a second unaudited way
to read a live credential. Same rule as the Kubernetes tab's bearer token.

**Gated on `workload_credentials_enabled`** — the integration's own flag, not a new one. "Do
not change the settings menu" still holds, and this is a capability of an existing
integration rather than a fourth lab.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, get_db
from ..services import config_service, workload_cloud_service
from ..services.workload_cloud_service import WorkloadCloudError
from .auth import require_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workload-cloud", tags=["workload-cloud"])


def _require_enabled() -> None:
    if not config_service.get_bool("workload_credentials_enabled",
                                   getattr(settings, "workload_credentials_enabled", False)):
        raise HTTPException(status_code=403,
                            detail="Workload Credentials is disabled")
    if not workload_cloud_service.enabled():
        raise HTTPException(
            status_code=403,
            detail=("Workload Credentials is enabled but not configured — set wlc_site_id "
                    "and wlc_pat"))


def _visible(row, user: User) -> bool:
    """Creator-scoped for non-admins, exactly like every sibling tab."""
    return bool(getattr(user, "is_admin", False)) or row.created_by == user.username


def _visible_or_404(db: Session, row_id: str, user: User):
    row = workload_cloud_service.get_row(db, row_id)
    if not row or row.status == "deleted" or not _visible(row, user):
        raise HTTPException(status_code=404, detail="workload cloud identity not found")
    return row


def _shape(row) -> dict:
    """The row on the wire. Names, ids and timestamps — never a credential.

    `lease_state` is separate from `status` deliberately: an EXPIRED LEASE IS THE MECHANISM
    WORKING, so it must not render as a fault. `revocable` is read off the row rather than
    recomputed, so the page cannot offer a kill switch the provider does not have.
    """
    return {
        "id": row.id, "name": row.name, "cloud": row.cloud,
        "secret_name": row.secret_name, "secret_folder": row.secret_folder or "",
        "purpose": row.purpose or "",
        "ttl_seconds": row.ttl_seconds or 0,
        "status": row.status, "error_message": row.error_message,
        # The lease. `lease_id` is a correlation handle to a live credential, not a
        # credential — it is what a revoke acts on and what WC's audit log keys on.
        "lease_id": row.lease_id or "",
        "lease_state": workload_cloud_service.lease_state(row),
        "lease_issued_at": (row.lease_issued_at.isoformat()
                            if row.lease_issued_at else None),
        "lease_expires_at": (row.lease_expires_at.isoformat()
                             if row.lease_expires_at else None),
        "revocable": bool(row.revocable),
        # Metered, so the count is a cost figure as much as an audit one.
        "issue_count": row.issue_count or 0,
        # What a consumer will receive, as field NAMES.
        "credential_fields": list(workload_cloud_service.credential_fields(row.cloud)),
        "job_ids": workload_cloud_service.job_ids(row),
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


class RegisterRequest(BaseModel):
    name: str
    cloud: str
    # The Workload Credentials dynamic secret. **This is what decides the scope** — which
    # role is assumed, which subscription, which permissions — and the dashboard cannot
    # widen or narrow it, so there is deliberately no default.
    secret_name: str
    secret_folder: str = ""
    purpose: str = ""
    ttl_seconds: int = 0
    expires_in_hours: Optional[int] = None


# ── read ──────────────────────────────────────────────────────────────────────

@router.get("")
def list_identities(db: Session = Depends(get_db),
                   user: User = Depends(require_permission("cloud_function", "read"))):
    _require_enabled()
    rows = workload_cloud_service.list_rows(db)
    return {"identities": [_shape(r) for r in rows if _visible(r, user)]}


@router.get("/options")
def register_options(db: Session = Depends(get_db),
                     user: User = Depends(require_permission("cloud_function", "read"))):
    """What the form needs, plus an honest list of what is not configured.

    The per-cloud entries carry `revocable`, because that is the single most important thing
    to know before choosing a cloud for a demonstration: on AWS the TTL is the only control
    there is, and an operator who learns that after promising a kill switch has a bad
    afternoon.
    """
    _require_enabled()
    from ..services import workload_credentials_service as wlc

    clouds = []
    for cloud in workload_cloud_service.VALID_CLOUDS:
        clouds.append({
            "key": cloud,
            "enabled": workload_cloud_service.cloud_enabled(cloud),
            "revocable": workload_cloud_service.revocable(cloud),
            "credential_fields": list(workload_cloud_service.credential_fields(cloud)),
            # The dynamic secret the DASHBOARD uses for its own calls, offered only as a
            # hint. Registering against it is allowed but rarely what an operator wants —
            # see the note the form shows.
            "dashboard_secret": config_service.get(f"wlc_{cloud}_secret_name") or "",
            "default_folder": config_service.get(f"wlc_{cloud}_folder") or "",
        })

    missing = []
    if not any(c["enabled"] for c in clouds):
        missing.append("no cloud has Workload Credentials configured — set wlc_aws_enabled "
                       "or wlc_azure_enabled and their dynamic-secret settings")
    if not config_service.get("wlc_site_id"):
        missing.append("wlc_site_id is blank")
    if not config_service.get("wlc_pat"):
        missing.append("wlc_pat is blank")

    # Folders listed live where possible, so an operator picks a real one rather than typing
    # it. Best-effort: the tab is still usable with a typed name if WC is unreachable, and
    # failing the whole options call over a browse would be worse than offering no list.
    folders = []
    try:
        folders = [str(f) for f in (wlc.list_folders() or [])][:100]
    except Exception as exc:                            # noqa: BLE001 — see above
        missing.append(f"could not list dynamic-secret folders: {exc}")

    return {
        "clouds": clouds,
        "folders": folders,
        "missing": missing,
        "note": ("the dynamic secret decides what every credential can do — this dashboard "
                 "names it and cannot change its scope"),
    }


@router.get("/{row_id}")
def get_identity(row_id: str, db: Session = Depends(get_db),
                 user: User = Depends(require_permission("cloud_function", "read"))):
    _require_enabled()
    return _shape(_visible_or_404(db, row_id, user))


@router.get("/{row_id}/lease")
async def get_lease(row_id: str, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("cloud_function", "read"))):
    """The provider's own view of the current lease.

    Read live from Workload Credentials rather than served from the row, because the two
    diverge in exactly the case that matters — somebody released the lease elsewhere — and a
    page reporting the cached view would show a withdrawn credential as live. Carries lease
    metadata only; `generate` is the sole call that returns values.
    """
    _require_enabled()
    _visible_or_404(db, row_id, user)
    try:
        return await workload_cloud_service.inspect_lease(db, row_id=row_id)
    except WorkloadCloudError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── write ─────────────────────────────────────────────────────────────────────

@router.post("")
def register_identity(req: RegisterRequest, db: Session = Depends(get_db),
                      user: User = Depends(require_permission("cloud_function", "write"))):
    """Register a workload identity against a dynamic secret. **Mints nothing.**

    Issuance is billed per call, so registering is deliberately inert — it records that this
    workload draws from that secret and stops. The first credential appears on Issue.
    """
    _require_enabled()
    expires_at = None
    if req.expires_in_hours and req.expires_in_hours > 0:
        expires_at = datetime.utcnow() + timedelta(hours=int(req.expires_in_hours))
    try:
        return workload_cloud_service.register(
            db, name=req.name, cloud=req.cloud, secret_name=req.secret_name,
            secret_folder=req.secret_folder, purpose=req.purpose,
            ttl_seconds=req.ttl_seconds, created_by=user.username,
            expires_at=expires_at)
    except WorkloadCloudError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{row_id}/issue")
def issue_credential(row_id: str, db: Session = Depends(get_db),
                     user: User = Depends(require_permission("cloud_function", "write"))):
    """Mint one credential. **This is the metered call** — one press, one issuance, one charge.

    The credential is not returned here and is not stored. What comes back is the lease id,
    the provider's expiry and the field names a consumer should expect.
    """
    _require_enabled()
    _visible_or_404(db, row_id, user)
    try:
        return workload_cloud_service.start_issue(
            db, row_id=row_id, created_by=user.username)
    except WorkloadCloudError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{row_id}/revoke")
def revoke_credential(row_id: str, db: Session = Depends(get_db),
                      user: User = Depends(require_permission("cloud_function", "write"))):
    """Release the outstanding lease early. **Azure only.**

    A 400 on AWS is the correct answer, not a limitation to work around: STS will not
    withdraw a credential it has already signed. The service refuses at the click rather
    than running a job that would "succeed" while the credential kept working — the
    underlying client swallows the provider's refusal, so a success here would be a lie.
    """
    _require_enabled()
    _visible_or_404(db, row_id, user)
    try:
        return workload_cloud_service.start_revoke(
            db, row_id=row_id, created_by=user.username)
    except WorkloadCloudError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{row_id}")
def delete_identity(row_id: str, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("cloud_function", "write"))):
    """Revoke what can be revoked, then retire the identity.

    **What this reliably stops is minting.** An outstanding AWS lease keeps working until it
    expires whatever happens here; what retiring ends is the identity's ability to draw
    another, which is the thing that would otherwise continue — and keep billing —
    indefinitely.
    """
    _require_enabled()
    _visible_or_404(db, row_id, user)
    try:
        return workload_cloud_service.start_decommission(
            db, row_id=row_id, created_by=user.username)
    except WorkloadCloudError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
