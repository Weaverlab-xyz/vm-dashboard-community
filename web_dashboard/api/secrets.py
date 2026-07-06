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

from fastapi import APIRouter, Depends, HTTPException, Request
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


def _require_admin_dep(request: Request) -> None:
    """FastAPI Depends-compatible wrapper around `_require_admin` — secret
    read/update/delete are admin-only. (There is no approval gate; secret
    access is not mediated by an Entitle request.)"""
    _require_admin(request)


# ── Secret registry ───────────────────────────────────────────────────────────
# All secrets the dashboard manages, regardless of which feature they belong to.

_SECRET_REGISTRY: list[tuple[str, str]] = [
    # (config_service key, human-readable description)
    ("aws_secret_access_key",     "AWS Secret Access Key"),
    ("azure_client_secret",       "Azure Service Principal Client Secret"),
    ("azure_oauth_client_secret", "Azure OAuth App Client Secret"),
    ("gcp_service_account_json",  "GCP Service Account JSON Key"),
    ("pscli_client_secret",       "BeyondTrust ps-cli Client Secret"),
    ("bt_client_secret",          "BeyondTrust Privileged Remote Access Client Secret"),
    ("epml_pat",                  "BeyondTrust EPM-L Personal Access Token"),
    ("entitle_api_token",         "Entitle API Token"),
    ("entitle_api_key",           "Entitle Terraform Provider API Key"),
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

# Credentials each backend reads to authenticate. Migrating one of these to the
# same backend would brick config resolution on next restart (the dashboard
# can't read its own auth credential through the backend that auth credential
# unlocks), so the migration loop refuses to move them.
_BOOTSTRAP_BLOCKLIST: dict[str, frozenset[str]] = {
    "aws_sm":          frozenset({"aws_secret_access_key"}),
    "azure_kv":        frozenset({"azure_client_secret"}),
    "gcp_sm":          frozenset({"gcp_service_account_json"}),
    "bt_secrets_safe": frozenset({"pscli_client_secret"}),
}


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
    secrets_bt_owner: str = ""
    secret_max_age_days: int = 0   # flag secrets older than this; 0 = disabled


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
        "secrets_bt_owner":    cs.get("secrets_bt_owner", ""),
        "secret_max_age_days": int(cs.get("secret_max_age_days") or 0),
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
        "secrets_bt_owner":     payload.secrets_bt_owner,
        "secret_max_age_days":  str(max(0, payload.secret_max_age_days)),
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
        # Read the raw stored value (without external resolution) to show where
        # it lives. get_raw() reads the correctly-keyed global row; reaching into
        # _cache directly here would miss it — the cache is keyed on (key, None)
        # tuples, not bare key strings.
        raw = cs.get_raw(key)

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


@router.get("/staleness")
async def secret_staleness(request: Request):
    """Per-secret age + staleness for the config-secret registry.

    For external-vault references the age comes from the backend's own
    last-changed date (so a secret rotated in the vault reads as fresh); DB-stored
    secrets — and refs the vault can't date — use the dashboard's
    ``AppConfig.updated_at``. A secret is flagged ``stale`` when its age reaches
    ``secret_max_age_days`` (0 = disabled). Admin-only, read-only.
    """
    _require_admin(request)
    from ..services import config_service as cs, secret_hygiene
    from ..config import settings
    from ..database import SessionLocal, AppConfig

    try:
        max_age = int(cs.get("secret_max_age_days")
                      or getattr(settings, "secret_max_age_days", 0) or 0)
    except (TypeError, ValueError):
        max_age = 0

    cs._ensure_loaded()
    keys = [k for k, _ in _SECRET_REGISTRY]
    db = SessionLocal()
    try:
        updated = {
            r.key: r.updated_at
            for r in db.query(AppConfig).filter(
                AppConfig.key.in_(keys), AppConfig.workgroup.is_(None)).all()
        }
    finally:
        db.close()

    items = []
    for key in keys:
        raw = cs.get_raw(key)
        if not raw:
            continue  # unset → nothing to age
        source = "database"
        changed_at = updated.get(key)
        for b_id, prefix in _BACKEND_PREFIXES.items():
            if prefix and raw.startswith(prefix):
                source = b_id
                # Prefer the vault's real last-changed date; fall back to when the
                # reference was configured in the dashboard.
                changed_at = cs.describe_reference(raw) or updated.get(key)
                break
        items.append({"key": key, "source": source, "changed_at": changed_at})

    return secret_hygiene.summarize(items, max_age)


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
    blocked = _BOOTSTRAP_BLOCKLIST.get(payload.target_backend, frozenset())

    migrated: list[dict] = []
    skipped:  list[dict] = []
    errors:   list[dict] = []

    for key, description in _SECRET_REGISTRY:
        if key in blocked:
            skipped.append({
                "key": key,
                "reason": (
                    f"bootstrap credential — {payload.target_backend} reads this "
                    f"to authenticate; migrating it would brick the dashboard"
                ),
                "bootstrap": True,
            })
            continue

        # Raw stored value (external refs left unresolved so we can detect
        # their backend prefix below). Read via get_raw() — the cache is keyed
        # on (key, None) tuples, so a bare-key _cache.get(key) never matches.
        raw = cs.get_raw(key)

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


# ── Per-secret CRUD ───────────────────────────────────────────────────────────
#
# All secret values are constrained to JSON (validate_json_value in
# secrets_backend_service) so the UI can ship one editor that works across
# every backend. The endpoints below mirror the existing /list/test/migrate
# admin guard.
#
# BSS (BeyondTrust Secrets Safe) gets two additional read-only endpoints to
# enumerate Safes and Folders so the frontend can render a tree. Write/delete
# on safes and folders is intentionally NOT exposed — ps-cli doesn't provide
# those operations; operators manage Safe/Folder lifecycle in BeyondInsight.

class SecretCreateRequest(BaseModel):
    backend: str
    key:     str
    value:   str  # must be valid JSON


class SecretUpdateRequest(BaseModel):
    backend: str
    value:   str  # must be valid JSON


@router.get("/items")
async def list_secret_items(request: Request, backend: str, folder: str = ""):
    """List secrets in the named backend. For bt_secrets_safe, pass ?folder=
    to scope the result to a single Folder; omitted means the configured
    default folder."""
    _require_admin(request)
    from ..services import secrets_backend_service as sbs
    try:
        items = await asyncio.to_thread(sbs.list_sync, backend, folder=folder)
    except (ValueError, Exception) as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return {"backend": backend, "items": items, "count": len(items)}


# Secret read/update/delete are admin-only (`_require_admin_dep`). Create is
# likewise admin-gated below via `_require_admin`. There is no approval gate.
@router.get("/items/{backend}/{ref:path}", dependencies=[Depends(_require_admin_dep)])
async def get_secret_item(backend: str, ref: str):
    """Return the value of one secret. Value is whatever string is stored —
    the editor on the frontend treats it as JSON."""
    from ..services import secrets_backend_service as sbs
    try:
        value = await asyncio.to_thread(sbs.read_sync, backend, ref)
    except (ValueError, Exception) as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return {"backend": backend, "ref": ref, "value": value}


@router.post("/items", status_code=201)
async def create_secret_item(payload: SecretCreateRequest, request: Request):
    """Create a new secret. Value must parse as JSON. Admin-only."""
    _require_admin(request)
    from ..services import secrets_backend_service as sbs
    try:
        ref = await asyncio.to_thread(
            sbs.write_sync_validated, payload.backend, payload.key, payload.value,
        )
    except (ValueError, Exception) as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return {"backend": payload.backend, "key": payload.key, "ref": ref}


@router.patch("/items/{backend}/{ref:path}", dependencies=[Depends(_require_admin_dep)])
async def update_secret_item(backend: str, ref: str, payload: SecretUpdateRequest):
    """Update the value of an existing secret. New value must parse as JSON.
    The path's `backend` and the body's `backend` must agree."""
    if payload.backend != backend:
        raise HTTPException(status_code=400, detail="backend in URL and body must match")
    from ..services import secrets_backend_service as sbs
    try:
        new_ref = await asyncio.to_thread(
            sbs.write_sync_validated, backend, ref, payload.value,
        )
    except (ValueError, Exception) as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return {"backend": backend, "ref": new_ref}


@router.delete("/items/{backend}/{ref:path}", dependencies=[Depends(_require_admin_dep)])
async def delete_secret_item(backend: str, ref: str):
    """Delete a secret."""
    from ..services import secrets_backend_service as sbs
    try:
        await asyncio.to_thread(sbs.delete_sync, backend, ref)
    except (ValueError, Exception) as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "backend": backend, "ref": ref}


# ── BSS hierarchy CRUD ───────────────────────────────────────────────────────
#
# ps-cli exposes create-safe / update-safe / delete-safe and create / delete
# for folders, so the dashboard ships the full hierarchy CRUD. Operations
# below thin-wrap the secrets_backend_service helpers.

class BssSafeCreate(BaseModel):
    name: str
    description: str = ""


class BssSafeUpdate(BaseModel):
    name: str  # new name


class BssFolderCreate(BaseModel):
    parent_id: str  # Safe GUID for top-level folders; Folder GUID for nested
    name: str


@router.get("/bss/safes")
async def list_bss_safes(request: Request):
    """List BeyondTrust Safes (top-level containers)."""
    _require_admin(request)
    from ..services import secrets_backend_service as sbs
    try:
        safes = await asyncio.to_thread(sbs.list_bt_safes)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return {"safes": safes, "count": len(safes)}


@router.post("/bss/safes", status_code=201)
async def create_bss_safe(payload: BssSafeCreate, request: Request):
    """Create a new BeyondTrust Safe via `ps-cli create-safe`."""
    _require_admin(request)
    from ..services import secrets_backend_service as sbs
    try:
        safe = await asyncio.to_thread(sbs.create_bt_safe, payload.name, payload.description)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return safe


@router.patch("/bss/safes/{safe_id}")
async def update_bss_safe(safe_id: str, payload: BssSafeUpdate, request: Request):
    """Rename a BeyondTrust Safe via `ps-cli update-safe`."""
    _require_admin(request)
    from ..services import secrets_backend_service as sbs
    try:
        safe = await asyncio.to_thread(sbs.update_bt_safe, safe_id, payload.name)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return safe


@router.delete("/bss/safes/{safe_id}")
async def delete_bss_safe(safe_id: str, request: Request):
    """Delete a BeyondTrust Safe via `ps-cli delete-safe`."""
    _require_admin(request)
    from ..services import secrets_backend_service as sbs
    try:
        await asyncio.to_thread(sbs.delete_bt_safe, safe_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "id": safe_id}


@router.get("/bss/folders")
async def list_bss_folders(request: Request, safe: str = ""):
    """List BeyondTrust Folders. Pass ?safe= to scope to one Safe."""
    _require_admin(request)
    from ..services import secrets_backend_service as sbs
    try:
        folders = await asyncio.to_thread(sbs.list_bt_folders, safe)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return {"folders": folders, "count": len(folders), "safe": safe}


@router.post("/bss/folders", status_code=201)
async def create_bss_folder(payload: BssFolderCreate, request: Request):
    """Create a new BeyondTrust Folder via `ps-cli create`."""
    _require_admin(request)
    from ..services import secrets_backend_service as sbs
    try:
        folder = await asyncio.to_thread(sbs.create_bt_folder, payload.parent_id, payload.name)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return folder


@router.delete("/bss/folders/{folder_id}")
async def delete_bss_folder(folder_id: str, request: Request):
    """Delete a BeyondTrust Folder via `ps-cli delete`. ps-cli refuses to
    delete a folder that still contains child folders or secrets — the
    error is surfaced to the caller as-is."""
    _require_admin(request)
    from ..services import secrets_backend_service as sbs
    try:
        await asyncio.to_thread(sbs.delete_bt_folder, folder_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "id": folder_id}
