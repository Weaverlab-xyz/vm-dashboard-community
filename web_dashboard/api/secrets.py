"""
Secrets backend management API.

GET  /api/secrets/backends     — list supported backend types and descriptions
GET  /api/secrets/config       — active backend + connection config
PATCH /api/secrets/config      — save backend connection config
POST /api/secrets/test         — test the currently configured backend connection
GET  /api/secrets/list         — all known secret keys with backend and value-presence info
POST /api/secrets/migrate      — migrate DB-stored secrets to the configured external backend
"""
import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/secrets", tags=["secrets"])

# ── Admin guard ───────────────────────────────────────────────────────────────

def _require_admin(request: Request) -> None:
    from jose import JWTError, jwt
    from ..config import settings
    from ..database import SessionLocal, User

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    token = auth[7:]
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        username: str = payload.get("sub", "")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username, User.is_active == True).first()
        if not user or not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")
    finally:
        db.close()


# ── Secret registry ───────────────────────────────────────────────────────────
# All secrets the dashboard manages, regardless of which feature they belong to.

_SECRET_REGISTRY: list[tuple[str, str]] = [
    # (config_service key, human-readable description)
    ("aws_secret_access_key",     "AWS Secret Access Key"),
    ("azure_client_secret",       "Azure Service Principal Client Secret"),
    ("azure_oauth_client_secret", "Azure OAuth App Client Secret"),
    ("gcp_service_account_json",  "GCP Service Account JSON Key"),
    ("pscli_client_secret",       "BeyondTrust ps-cli Client Secret"),
    ("bt_client_secret",          "BeyondTrust Password Safe Client Secret"),
    ("epml_pat",                  "BeyondTrust EPM-L Personal Access Token"),
    ("entitle_api_token",         "Entitle API Token"),
    ("entitle_webhook_secret",    "Entitle Webhook Secret"),
    ("proxmox_token_secret",      "Proxmox API Token Secret"),
    ("proxmox_password",          "Proxmox Password"),
    ("vsphere_password",          "vSphere Password"),
    ("hyperv_password",           "Hyper-V Password"),
    ("nutanix_password",          "Nutanix Password"),
    ("xcpng_password",            "XCP-ng Password"),
]

# Prefix → backend id — must match secrets_backend_service._EXT_PREFIXES keys
_BACKEND_PREFIXES: dict[str, str] = {
    "database":        "",
    "aws_sm":          "aws_sm://",
    "azure_kv":        "azure_kv://",
    "gcp_sm":          "gcp_sm://",
    "bt_secrets_safe": "bt_safe://",
}

_VALID_BACKENDS = frozenset(_BACKEND_PREFIXES)
_EXTERNAL_BACKENDS = _VALID_BACKENDS - {"database"}


# ── Pydantic models ───────────────────────────────────────────────────────────

class BackendConfigPayload(BaseModel):
    backend: str = "database"
    secrets_aws_region: str = ""
    secrets_aws_prefix: str = "dashboard"
    secrets_azure_kv_url: str = ""
    secrets_gcp_project: str = ""
    secrets_gcp_prefix: str = "dashboard"
    secrets_bt_host: str = ""
    secrets_bt_folder: str = "Dashboard"


class MigratePayload(BaseModel):
    target_backend: str
    dry_run: bool = False


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/backends")
async def list_backends(request: Request):
    _require_admin(request)
    return [
        {
            "id":          "database",
            "label":       "Database (Fernet-encrypted)",
            "description": "Secrets are encrypted with Fernet and stored in the local PostgreSQL/SQLite database.",
        },
        {
            "id":          "aws_sm",
            "label":       "AWS Secrets Manager",
            "description": "Secrets are stored in AWS Secrets Manager using your configured AWS credentials.",
        },
        {
            "id":          "azure_kv",
            "label":       "Azure Key Vault",
            "description": "Secrets are stored in Azure Key Vault using your configured service principal.",
        },
        {
            "id":          "gcp_sm",
            "label":       "GCP Secret Manager",
            "description": "Secrets are stored in Google Cloud Secret Manager using your configured service account.",
        },
        {
            "id":          "bt_secrets_safe",
            "label":       "BeyondTrust Secrets Safe",
            "description": "Secrets are stored in BeyondTrust Password Safe using your configured ps-cli credentials.",
        },
    ]


@router.get("/config")
async def get_backend_config(request: Request):
    _require_admin(request)
    from ..services import config_service as cs
    return {
        "backend":             cs.get("secrets_backend", "database"),
        "secrets_aws_region":  cs.get("secrets_aws_region", ""),
        "secrets_aws_prefix":  cs.get("secrets_aws_prefix", "dashboard"),
        "secrets_azure_kv_url": cs.get("secrets_azure_kv_url", ""),
        "secrets_gcp_project": cs.get("secrets_gcp_project", ""),
        "secrets_gcp_prefix":  cs.get("secrets_gcp_prefix", "dashboard"),
        "secrets_bt_host":     cs.get("secrets_bt_host", ""),
        "secrets_bt_folder":   cs.get("secrets_bt_folder", "Dashboard"),
    }


@router.patch("/config")
async def update_backend_config(payload: BackendConfigPayload, request: Request):
    _require_admin(request)
    if payload.backend not in _VALID_BACKENDS:
        raise HTTPException(status_code=400, detail=f"Unknown backend: {payload.backend!r}")
    from ..services import config_service as cs
    cs.set_many({
        "secrets_backend":      payload.backend,
        "secrets_aws_region":   payload.secrets_aws_region,
        "secrets_aws_prefix":   payload.secrets_aws_prefix,
        "secrets_azure_kv_url": payload.secrets_azure_kv_url,
        "secrets_gcp_project":  payload.secrets_gcp_project,
        "secrets_gcp_prefix":   payload.secrets_gcp_prefix,
        "secrets_bt_host":      payload.secrets_bt_host,
        "secrets_bt_folder":    payload.secrets_bt_folder,
    })
    logger.info("Secrets backend config updated: backend=%s", payload.backend)
    return {"ok": True}


@router.post("/test")
async def test_backend(request: Request):
    _require_admin(request)
    from ..services import config_service as cs
    from ..services import secrets_backend_service as sbs
    backend = cs.get("secrets_backend", "database")
    if backend == "database":
        return {"ok": True, "message": "Database backend is always available."}
    try:
        result = await asyncio.to_thread(sbs.test_sync, backend)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/list")
async def list_secrets(request: Request):
    _require_admin(request)
    from ..services import config_service as cs

    result = []
    for key, description in _SECRET_REGISTRY:
        # Read raw DB value (without external resolution) to show where it lives
        cs._ensure_loaded()
        with cs._cache_lock:
            raw = cs._cache.get(key, "")

        has_value = bool(raw)
        backend = "database"
        ref = ""
        for b_id, prefix in _BACKEND_PREFIXES.items():
            if prefix and raw.startswith(prefix):
                backend = b_id
                ref = raw[len(prefix):]
                break

        result.append({
            "key":         key,
            "description": description,
            "has_value":   has_value,
            "backend":     backend,
            "ref":         ref,
        })
    return result


@router.post("/migrate")
async def migrate_secrets(payload: MigratePayload, request: Request):
    """
    Read every DB-stored secret, write it to the target external backend,
    then replace the DB value with the backend reference string.

    Secrets already on a different external backend are skipped.
    On dry_run=True, no writes are performed and no DB values are changed.
    The secrets_backend config key is updated to target_backend only when
    all migrations succeed and dry_run is False.
    """
    _require_admin(request)

    if payload.target_backend not in _EXTERNAL_BACKENDS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid target backend {payload.target_backend!r}. "
                   f"Choose one of: {', '.join(sorted(_EXTERNAL_BACKENDS))}",
        )

    from ..services import config_service as cs
    from ..services import secrets_backend_service as sbs

    target_prefix = _BACKEND_PREFIXES[payload.target_backend]

    migrated: list[dict] = []
    skipped:  list[dict] = []
    errors:   list[dict] = []

    for key, description in _SECRET_REGISTRY:
        cs._ensure_loaded()
        with cs._cache_lock:
            raw = cs._cache.get(key, "")

        if not raw:
            skipped.append({"key": key, "reason": "not configured"})
            continue

        # Check if it is already an external reference
        current_backend = "database"
        current_ref = ""
        for b_id, pfx in _BACKEND_PREFIXES.items():
            if pfx and raw.startswith(pfx):
                current_backend = b_id
                current_ref = raw[len(pfx):]
                break

        if current_backend == payload.target_backend:
            skipped.append({"key": key, "reason": "already on target backend", "ref": current_ref})
            continue

        if current_backend != "database":
            skipped.append({
                "key": key,
                "reason": f"stored in {current_backend} — migrate manually or restore to database first",
            })
            continue

        # raw is a Fernet-encrypted DB value — resolve the plaintext
        plaintext = cs.get(key)
        if not plaintext:
            skipped.append({"key": key, "reason": "decryption returned empty value"})
            continue

        if payload.dry_run:
            migrated.append({"key": key, "description": description, "dry_run": True})
            continue

        try:
            ref = await asyncio.to_thread(sbs.write_sync, payload.target_backend, key, plaintext)
            new_db_value = f"{target_prefix}{ref}"
            cs.set(key, new_db_value)
            migrated.append({"key": key, "description": description, "ref": ref})
            logger.info("Migrated secret %s to %s (ref=%s)", key, payload.target_backend, ref)
        except Exception as exc:
            errors.append({"key": key, "error": str(exc)})
            logger.error("Failed to migrate secret %s: %s", key, exc)

    if not payload.dry_run and migrated and not errors:
        cs.set("secrets_backend", payload.target_backend)

    return {
        "target_backend": payload.target_backend,
        "dry_run":        payload.dry_run,
        "migrated":       migrated,
        "skipped":        skipped,
        "errors":         errors,
    }
