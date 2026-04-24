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
    ec2_ssh_key_secret: str = ""
    ec2_ssm_instance_profile: str = ""


class AzureSetup(BaseModel):
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_tenant_id: str = ""
    azure_subscription_id: str = ""
    azure_resource_group: str = "dashboard-rg"
    azure_location: str = "centralus"
    # VNet/NSG lookup (defaults to azure_resource_group if blank)
    azure_vnet_resource_group: str = ""
    # Shared Image Gallery for private images (optional)
    azure_shared_image_gallery: str = ""
    azure_gallery_resource_group: str = ""
    # Key Vault for SSH key retrieval (optional)
    azure_key_vault_url: str = ""
    azure_ssh_key_secret_name: str = ""
    # Optional: separate app registration for "Sign in with Microsoft"
    azure_oauth_client_id: str = ""
    azure_oauth_client_secret: str = ""
    azure_oauth_tenant_id: str = ""


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


_WIZARD_SECRET_FIELDS = frozenset({
    "aws_secret_access_key",
    "azure_client_secret",
    "azure_oauth_client_secret",
})


def _apply_config(payload: SetupPayload) -> None:
    """Write all wizard values to the config store and invalidate service caches."""
    from ..services import config_service

    pairs: dict = {}

    for field, value in payload.aws.model_dump().items():
        # Skip empty secret fields on reconfigure so existing DB values aren't blanked.
        if field in _WIZARD_SECRET_FIELDS and not value:
            continue
        pairs[field] = value

    # Azure
    for field, value in payload.azure.model_dump().items():
        if field in _WIZARD_SECRET_FIELDS and not value:
            continue
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


# ── Per-feature configuration ─────────────────────────────────────────────────
#
# Each feature has its own typed model so the UI can GET current values and
# PATCH only the keys relevant to that integration.  All require admin JWT.

class ChatFeatureConfig(BaseModel):
    enabled: bool = False
    chat_model: str = "llama3.1:8b-instruct-q4_K_M"
    ollama_base_url: str = "http://ollama:11434"

class VMwareFeatureConfig(BaseModel):
    enabled: bool = False

class BeyondTrustFeatureConfig(BaseModel):
    enabled: bool = False
    pscli_api_url: str = ""
    pscli_client_id: str = ""
    pscli_client_secret: str = ""   # encrypted at rest
    bt_api_host: str = ""
    bt_client_id: str = ""
    bt_client_secret: str = ""      # encrypted at rest

class PortainerFeatureConfig(BaseModel):
    enabled: bool = False
    portainer_url: str = ""
    portainer_verify_ssl: bool = True

class AnsibleFeatureConfig(BaseModel):
    enabled: bool = False
    ansible_s3_bucket: str = ""
    ansible_s3_region: str = ""
    ansible_ecs_cluster: str = "bt-jumpoint"
    ansible_ecs_task_family: str = "ansible-config-mgmt"

class EntitleFeatureConfig(BaseModel):
    enabled: bool = False
    entitle_api_url: str = ""
    entitle_api_token: str = ""         # encrypted at rest
    entitle_webhook_secret: str = ""    # encrypted at rest
    approval_gate_enabled: bool = False


_FEATURE_MODELS = {
    "chat":         ChatFeatureConfig,
    "vmware":       VMwareFeatureConfig,
    "beyondtrust":  BeyondTrustFeatureConfig,
    "portainer":    PortainerFeatureConfig,
    "ansible":      AnsibleFeatureConfig,
    "entitle":      EntitleFeatureConfig,
}

_SECRET_FEATURE_KEYS = frozenset({
    "pscli_client_secret", "bt_client_secret",
    "entitle_api_token", "entitle_webhook_secret",
})


def _feature_to_cfg_key(feature: str) -> str:
    return f"{feature}_enabled"


def _read_feature(feature: str, model_cls) -> dict:
    """Build a response dict for a feature by reading config_service."""
    from ..services import config_service
    enabled_key = _feature_to_cfg_key(feature)
    data = {"enabled": config_service.get_bool(enabled_key)}
    for field in model_cls.model_fields:
        if field == "enabled":
            continue
        val = config_service.get(field)
        # Redact secrets for display
        data[field] = "••••••••" if (field in _SECRET_FEATURE_KEYS and val) else val
    return data


def _write_feature(feature: str, payload_dict: dict) -> None:
    """Persist a feature's config to config_service."""
    from ..services import config_service
    pairs: dict = {}
    enabled_key = _feature_to_cfg_key(feature)
    enabled = payload_dict.pop("enabled", False)
    pairs[enabled_key] = "1" if enabled else "0"
    for key, value in payload_dict.items():
        # Skip placeholder values (user left secret field as bullets)
        if isinstance(value, str) and value.startswith("••"):
            continue
        pairs[key] = str(value) if not isinstance(value, str) else value
    config_service.set_many(pairs)

    if feature == "azure":
        try:
            from ..services import azure_service
            azure_service.invalidate_credentials()
        except Exception:
            pass


@router.get("/feature/{feature_name}")
def get_feature_config(feature_name: str, request: Request):
    """Return current config for a single feature (secrets redacted). Admin JWT required."""
    _require_admin(request)
    model_cls = _FEATURE_MODELS.get(feature_name)
    if model_cls is None:
        raise HTTPException(status_code=404, detail=f"Unknown feature: {feature_name}")
    return _read_feature(feature_name, model_cls)


@router.patch("/feature/{feature_name}")
def patch_feature_config(feature_name: str, payload: dict, request: Request):
    """Save config for a single feature. Admin JWT required."""
    _require_admin(request)
    model_cls = _FEATURE_MODELS.get(feature_name)
    if model_cls is None:
        raise HTTPException(status_code=404, detail=f"Unknown feature: {feature_name}")
    # Validate through the model for type safety, but only persist keys that were
    # explicitly sent — this prevents a toggle-only call ({enabled: true}) from
    # blanking out credential fields that weren't included in the payload.
    validated = model_cls(**payload).model_dump()
    filtered = {k: v for k, v in validated.items() if k in payload}
    _write_feature(feature_name, filtered)
    logger.info("Feature '%s' configuration updated.", feature_name)
    return {"ok": True}
