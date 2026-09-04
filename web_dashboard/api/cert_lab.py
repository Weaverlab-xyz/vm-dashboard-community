"""
Certificate Lab API — preview (gated by the ``cert_lab_enabled`` flag).

  GET    /api/cert-lab                    — list dashboard-built certificate authorities
  POST   /api/cert-lab                    — build a CA (record + schedule apply)
  GET    /api/cert-lab/options            — locations, tiers and what config is missing
  GET    /api/cert-lab/{id}               — one CA
  GET    /api/cert-lab/{id}/chain         — the CA chain PEM, for the mTLS endpoint
  POST   /api/cert-lab/{id}/identities    — onboard a certificate identity onto it
  DELETE /api/cert-lab/{id}/identities    — remove the Password Safe objects
  DELETE /api/cert-lab/{id}               — destroy the CA (terraform destroy)
  POST   /api/cert-lab/preview-address    — compose + validate a profile without saving

``preview-address`` exists because the managed system's address is the ENTIRE
configuration surface for this plugin — a Password Safe Cloud tenant cannot edit the
appsettings.json inside the .psplugin — and Password Safe's address column is 255
characters. The fully spelled-out ADCS profile from the plugin's own documentation is 269
characters, so an operator needs to see the length while the field is still editable
rather than after a rotation fails.

Permission-gated via the ``cloud_function``-style pattern: list results are scoped to the
caller's own rows for non-admins, mirroring the cloud-database and functions pages.

The chain PEM is deliberately NOT admin-only. A CA certificate is a public document by
construction — it is what every client has to trust — and the whole lab depends on
pasting it into an nginx ``ssl_client_certificate``.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, get_db
from ..services import cert_lab_service, cert_ps_service, config_service
from ..services.cert_lab_service import CertLabError
from ..services.cert_ps_service import CertPSError
from .auth import require_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cert-lab", tags=["cert-lab"])


def _require_enabled() -> None:
    if not config_service.get_bool("cert_lab_enabled", settings.cert_lab_enabled):
        raise HTTPException(status_code=403, detail="the Certificate Lab is disabled")


def _row_or_404(db: Session, lab_id: str):
    row = cert_lab_service.get_lab(db, lab_id)
    if not row:
        raise HTTPException(status_code=404, detail="certificate authority not found")
    return row


def _visible(row, user: User) -> bool:
    """Creator-scoped for non-admins, exactly like the databases and functions pages."""
    return bool(getattr(user, "is_admin", False)) or row.created_by == user.username


def _shape(row) -> dict:
    return {"id": row.id, "name": row.name, "cloud": row.cloud, "backend": row.backend,
            "project": row.project, "location": row.location, "pool_id": row.pool_id,
            "status": row.status, "error_message": row.error_message,
            # The enrollment identity's EMAIL. Its KEY is never returned by any route
            # here: it goes straight into the Password Safe functional account, which is
            # the protected field built for it.
            "enroll_account": row.enroll_account,
            "has_chain": bool(row.ca_chain_pem),
            "ps_system_id": row.ps_system_id, "ps_account_id": row.ps_account_id,
            "ps_address": row.ps_address,
            "deploy_job_id": row.deploy_job_id,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None}


class BuildRequest(BaseModel):
    name: str
    project: str
    cloud: str = "gcp"
    location: str = ""
    pool_id: str = ""


class IdentityRequest(BaseModel):
    account_name: str
    # Per-identity profile overrides — lifetime, key, subject, dns, eku. Each overrides
    # the CA's default for this identity alone, which is what removes the need for a
    # separate platform instance per SAN set.
    overrides: Optional[dict] = None


class AddressPreviewRequest(BaseModel):
    backend: str
    backend_options: dict = {}
    overrides: Optional[dict] = None


# ── read ──────────────────────────────────────────────────────────────────────

@router.get("")
def list_authorities(db: Session = Depends(get_db),
                     user: User = Depends(require_permission("cloud_function", "read"))):
    _require_enabled()
    rows = cert_lab_service.list_labs(db)
    return [_shape(r) for r in rows if r.status != "deleted" and _visible(r, user)]


@router.get("/options")
def build_options(user: User = Depends(require_permission("cloud_function", "read"))):
    """What the build form needs, plus an honest list of what is not configured yet.

    ``missing`` is the point of this route. Every item on it produces a failure that
    surfaces hours later inside the plugin rather than at the click, so naming them up
    front is the difference between a five-minute setup and an afternoon."""
    _require_enabled()
    missing = []
    if not cert_ps_service.default_biurl():
        missing.append("cert_ps_biurl (or pscli_api_url) — the plugin has nowhere to "
                       "write the bundle, and a Cloud tenant cannot supply it from "
                       "appsettings.json")
    if not config_service.get("cert_ps_owner_group_id"):
        missing.append("cert_ps_owner_group_id — Secrets Safe requires an owner for "
                       "created secrets, and it is a GROUP id here")
    if not config_service.get("cert_ps_functional_account"):
        missing.append("cert_ps_functional_account — one account carries BOTH the CA "
                       "enrollment credential and the BeyondInsight API user, split on "
                       "the last colon")
    return {"clouds": ["gcp"],
            "locations": ["us-central1", "us-east1", "europe-west1", "asia-east1"],
            "tiers": ["DEVOPS", "ENTERPRISE"],
            "default_location": config_service.get("cert_gcp_cas_location") or "us-central1",
            "biurl": cert_ps_service.default_biurl(),
            "folder": config_service.get("cert_ps_folder") or settings.cert_ps_folder,
            "address_limit": 255,
            "missing": missing}


@router.get("/{lab_id}")
def get_authority(lab_id: str, db: Session = Depends(get_db),
                  user: User = Depends(require_permission("cloud_function", "read"))):
    _require_enabled()
    row = _row_or_404(db, lab_id)
    if not _visible(row, user):
        raise HTTPException(status_code=404, detail="certificate authority not found")
    return _shape(row)


@router.get("/{lab_id}/chain")
def get_chain(lab_id: str, db: Session = Depends(get_db),
              user: User = Depends(require_permission("cloud_function", "read"))):
    """The CA chain PEM — feed it to the mTLS endpoint playbook as ``ca_chain_pem``."""
    _require_enabled()
    row = _row_or_404(db, lab_id)
    if not _visible(row, user):
        raise HTTPException(status_code=404, detail="certificate authority not found")
    if not row.ca_chain_pem:
        raise HTTPException(
            status_code=409,
            detail=f"{row.name} has no chain yet — it is {row.status}")
    return {"id": row.id, "name": row.name, "ca_chain_pem": row.ca_chain_pem}


# ── write ─────────────────────────────────────────────────────────────────────

@router.post("")
def build_authority(req: BuildRequest, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("cloud_function", "write"))):
    _require_enabled()
    try:
        return cert_lab_service.provision(
            db, name=req.name, project=req.project, cloud=req.cloud,
            location=req.location, pool_id=req.pool_id, created_by=user.username)
    except CertLabError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/preview-address")
def preview_address(req: AddressPreviewRequest,
                    user: User = Depends(require_permission("cloud_function", "read"))):
    """Compose and validate a profile without saving anything. Never 400s on an invalid
    profile — it returns the address it would have built plus the reason it is refused,
    so the field can show both while it is still being edited."""
    _require_enabled()
    return cert_ps_service.address_preview(req.backend, req.backend_options, req.overrides)


@router.post("/{lab_id}/identities")
def add_identity(lab_id: str, req: IdentityRequest, db: Session = Depends(get_db),
                 user: User = Depends(require_permission("cloud_function", "write"))):
    """Onboard one certificate identity onto this CA.

    One managed account per identity — and with an Entra publisher, one per app
    registration. Graph's PATCH replaces the whole keyCredentials collection, so two
    rotations against the same registration can clobber each other's key; Password Safe
    serialises per managed account, which is what makes that mapping safe."""
    _require_enabled()
    row = _row_or_404(db, lab_id)
    if not _visible(row, user):
        raise HTTPException(status_code=404, detail="certificate authority not found")
    try:
        return cert_lab_service.start_ps_register(
            db, lab_id=lab_id, account_name=req.account_name,
            created_by=user.username, overrides=req.overrides)
    except (CertLabError, CertPSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{lab_id}/identities")
def remove_identity(lab_id: str, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("cloud_function", "write"))):
    _require_enabled()
    row = _row_or_404(db, lab_id)
    if not _visible(row, user):
        raise HTTPException(status_code=404, detail="certificate authority not found")
    try:
        return cert_lab_service.start_ps_register(
            db, lab_id=lab_id, account_name="", created_by=user.username,
            action="deregister")
    except CertLabError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{lab_id}")
def destroy_authority(lab_id: str, db: Session = Depends(get_db),
                      user: User = Depends(require_permission("cloud_function", "write"))):
    """Destroy the CA pool and everything in it. The same teardown the auto-delete timer
    runs, so there is exactly one path and it is exercised both ways."""
    _require_enabled()
    row = _row_or_404(db, lab_id)
    if not _visible(row, user):
        raise HTTPException(status_code=404, detail="certificate authority not found")
    try:
        return cert_lab_service.start_decommission(
            db, lab_id=lab_id, created_by=user.username)
    except CertLabError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
