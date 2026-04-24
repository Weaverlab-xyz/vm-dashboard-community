"""
Setup wizard API.

POST /api/setup/complete   — initial setup, no auth required, errors if already done
PUT  /api/setup/config     — reconfigure, admin JWT required
GET  /api/setup/status     — {complete: bool, configured_keys: list}
GET  /api/setup/config     — current config with secrets redacted (admin JWT if setup done)
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/setup", tags=["setup"])


# ── Pydantic models ───────────────────────────────────────────────────────────

class AdminSetup(BaseModel):
    username: str
    password: str


class AWSSetup(BaseModel):
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-2"


class AzureSetup(BaseModel):
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_tenant_id: str = ""
    azure_subscription_id: str = ""
    azure_resource_group: str = "dashboard-rg"
    azure_location: str = "centralus"


class FeaturesSetup(BaseModel):
    chat_enabled: bool = False
    vmware_enabled: bool = False
    beyondtrust_enabled: bool = False
    portainer_enabled: bool = False
    ansible_enabled: bool = False
    entitle_enabled: bool = False


class SetupPayload(BaseModel):
    admin: AdminSetup
    aws: AWSSetup
    azure: AzureSetup
    features: FeaturesSetup


# ── Internal helpers ──────────────────────────────────────────────────────────

def _require_admin(request: Request) -> None:
    """Verify the request carries a valid admin JWT. Raises 401/403 on failure."""
    from jose import JWTError, jwt
    from ..config import settings
    from ..database import SessionLocal, User

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    token = auth[7:]
    try:
        payload = jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
        username: str = payload.get("sub", "")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(User.username == username, User.is_active == True)
            .first()
        )
        if not user or not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")
    finally:
        db.close()


def _upsert_admin(username: str, password: str) -> None:
    from ..database import SessionLocal, User, get_password_hash
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            existing.hashed_password = get_password_hash(password)
            existing.is_admin = True
            existing.is_active = True
        else:
            user = User(
                username=username,
                hashed_password=get_password_hash(password),
                full_name="Administrator",
                is_admin=True,
                is_active=True,
            )
            user.workgroups_list = []
            db.add(user)
        db.commit()
    finally:
        db.close()


def _apply_config(payload: SetupPayload) -> None:
    """Write all wizard values to the config store and invalidate service caches."""
    from ..services import config_service

    pairs: dict = {}

    # AWS — only store non-empty values so we don't blank out existing creds
    # when the user skips a section (empty string = not configured)
    for field, value in payload.aws.model_dump().items():
        pairs[field] = value

    # Azure
    for field, value in payload.azure.model_dump().items():
        pairs[field] = value

    # Feature flags — always store (explicit true/false is meaningful)
    pairs.update({
        "chat_enabled":         "1" if payload.features.chat_enabled else "0",
        "vmware_enabled":       "1" if payload.features.vmware_enabled else "0",
        "beyondtrust_enabled":  "1" if payload.features.beyondtrust_enabled else "0",
        "portainer_enabled":    "1" if payload.features.portainer_enabled else "0",
        "ansible_enabled":      "1" if payload.features.ansible_enabled else "0",
        "entitle_enabled":      "1" if payload.features.entitle_enabled else "0",
    })

    config_service.set_many(pairs)

    # Invalidate Azure credential cache so the service picks up new values.
    try:
        from ..services import azure_service
        azure_service.invalidate_credentials()
    except Exception:
        pass


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/status")
def setup_status():
    from ..services import config_service
    complete = config_service.is_setup_complete()
    configured = list(config_service.get_all_public().keys()) if complete else []
    return {"complete": complete, "configured_keys": configured}


@router.get("/config")
def get_config(request: Request):
    """Return current config with secrets redacted. Admin JWT required once setup is done."""
    from ..services import config_service
    if config_service.is_setup_complete():
        _require_admin(request)
    return config_service.get_all_public()


@router.post("/complete", status_code=201)
def complete_setup(payload: SetupPayload, request: Request):
    """
    Initial wizard submission — no auth required.
    Returns 409 if setup has already been completed (use PUT /api/setup/config instead).
    """
    from ..services import config_service
    if config_service.is_setup_complete():
        raise HTTPException(
            status_code=409,
            detail="Setup is already complete. Use PUT /api/setup/config to reconfigure.",
        )
    _upsert_admin(payload.admin.username, payload.admin.password)
    _apply_config(payload)
    config_service.mark_setup_complete()
    logger.info("Setup wizard completed by first-run submission.")
    return {"ok": True}


@router.put("/config")
def reconfigure(payload: SetupPayload, request: Request):
    """
    Reconfigure credentials and feature flags. Admin JWT required.
    Admin account password is updated only when payload.admin.password is non-empty.
    """
    _require_admin(request)
    if payload.admin.password:
        _upsert_admin(payload.admin.username, payload.admin.password)
    _apply_config(payload)
    logger.info("Configuration updated via reconfigure endpoint.")
    return {"ok": True}
