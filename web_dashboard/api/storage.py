"""
Storage backend management API.

Exposes the cloud object-storage abstraction in services/storage_service.py
to a self-contained `/storage` page that mirrors the shape of `/secrets`:

  GET    /api/storage/backends         — which backends are configured + active
  GET    /api/storage/config           — current per-backend config (non-secret)
  PATCH  /api/storage/config           — update per-backend config + active selection
  POST   /api/storage/test             — probe a backend for reachability
  GET    /api/storage/list             — list assets in the active backend
  GET    /api/storage/list/{backend}   — list assets in a specific backend (for migration)
  POST   /api/storage/migrate          — copy assets from source → target

Storage today stores Ansible playbooks/scripts/packages, but is general-purpose.
Future features that need a small object store can layer on top of it
without re-introducing per-feature backend configuration.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, get_db
from ..services import storage_service
from ..services.storage_service import BACKENDS, StorageError
from .auth import get_current_user, require_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/storage", tags=["storage"])


# Per-backend "configured?" required fields. Mirrors storage_service.
_REQUIRED_FIELDS = {
    "s3":         ["storage_s3_bucket"],
    "azure_blob": ["storage_azure_account"],
    "gcs":        ["storage_gcs_bucket"],
}

# All editable per-backend config keys, in canonical order.
_BACKEND_KEYS = {
    "s3":         ["storage_s3_bucket",       "storage_s3_region",       "storage_s3_prefix"],
    "azure_blob": ["storage_azure_account",   "storage_azure_container", "storage_azure_prefix"],
    "gcs":        ["storage_gcs_bucket",      "storage_gcs_prefix"],
}


def _cfg_get(key: str) -> str:
    from ..services import config_service
    return config_service.get(key) or ""


def _cfg_set_many(values: dict) -> None:
    from ..services import config_service
    for key, value in values.items():
        config_service.set(key, value)


# ── GET /api/storage/backends ────────────────────────────────────────────────

@router.get("/backends")
async def list_backends(current_user: User = Depends(get_current_user)):
    """Return per-backend configured/active state. Used by /storage and the
    Ansible feature-flag prereq gate."""
    cfgd = set(storage_service.configured_backends())
    active = storage_service.active_backend()
    return {
        "backends": [
            {
                "id":         b,
                "label":      {"s3": "AWS S3", "azure_blob": "Azure Blob Storage", "gcs": "Google Cloud Storage"}[b],
                "configured": b in cfgd,
                "active":     b == active,
            }
            for b in BACKENDS
        ],
        "active":     active,
        "any_active": bool(active),
    }


# ── GET /api/storage/config ──────────────────────────────────────────────────

@router.get("/config")
async def get_config(current_user: User = Depends(require_permission("admin", "read"))):
    """Return all per-backend config values. Admin-only because the field
    list overlaps with cloud account scoping."""
    out: dict = {"storage_active_backend": _cfg_get("storage_active_backend")}
    for keys in _BACKEND_KEYS.values():
        for k in keys:
            out[k] = _cfg_get(k)
    return out


# ── PATCH /api/storage/config ─────────────────────────────────────────────────

class StorageConfigPatch(BaseModel):
    storage_active_backend: str | None = None
    storage_s3_bucket:      str | None = None
    storage_s3_region:      str | None = None
    storage_s3_prefix:      str | None = None
    storage_azure_account:    str | None = None
    storage_azure_container:  str | None = None
    storage_azure_prefix:     str | None = None
    storage_gcs_bucket:     str | None = None
    storage_gcs_prefix:     str | None = None


@router.patch("/config")
async def patch_config(
    payload: StorageConfigPatch,
    current_user: User = Depends(require_permission("admin", "write")),
):
    """Partial update — only fields explicitly supplied (non-None) are written.
    Validates that the active backend (if changed) is configured before flipping."""
    raw = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "storage_active_backend" in raw:
        chosen = raw["storage_active_backend"]
        if chosen and chosen not in BACKENDS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid backend '{chosen}'. Valid: {', '.join(BACKENDS)}.",
            )
        # Verify the chosen backend will be configured AFTER this patch lands.
        if chosen:
            required = _REQUIRED_FIELDS[chosen]
            for k in required:
                # Use the patched value if present, else the existing value.
                if not (raw.get(k) or _cfg_get(k)):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Cannot activate '{chosen}' — missing required field "
                            f"'{k}'. Set it before activating this backend."
                        ),
                    )
    _cfg_set_many(raw)
    return {"ok": True, "updated": list(raw.keys())}


# ── POST /api/storage/test ────────────────────────────────────────────────────

class TestRequest(BaseModel):
    backend: str   # "s3" | "azure_blob" | "gcs"


@router.post("/test")
async def test_backend(
    req: TestRequest,
    current_user: User = Depends(require_permission("admin", "read")),
):
    """Probe a backend by listing its assets. Returns ok=true with item count
    on success, ok=false with the error message otherwise. Never raises."""
    if req.backend not in BACKENDS:
        raise HTTPException(status_code=400, detail=f"Invalid backend '{req.backend}'.")
    try:
        result = await storage_service.test_backend(req.backend)
    except StorageError as e:
        return {"ok": False, "error": str(e)}
    return result


# ── GET /api/storage/list ────────────────────────────────────────────────────

@router.get("/list")
async def list_active(current_user: User = Depends(get_current_user)):
    """List assets from the *active* backend."""
    try:
        items = await storage_service.list_assets()
        return {"backend": storage_service.active_backend(), "items": items, "count": len(items)}
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/list/{backend}")
async def list_specific(
    backend: str,
    current_user: User = Depends(require_permission("admin", "read")),
):
    """List assets from a specific backend (used by the migrate UI's source picker)."""
    try:
        items = await storage_service.list_assets_in(backend)
        return {"backend": backend, "items": items, "count": len(items)}
    except StorageError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── POST /api/storage/migrate ────────────────────────────────────────────────

class MigrateRequest(BaseModel):
    source: str    # backend id
    target: str    # backend id
    overwrite: bool = False  # when False, files already in target are skipped


@router.post("/migrate")
async def migrate(
    req: MigrateRequest,
    current_user: User = Depends(require_permission("admin", "write")),
):
    """Copy every asset from `source` to `target`. The source remains
    untouched — operators can verify the target is healthy before deleting
    the source manually. Switch the active backend with PATCH /api/storage/config."""
    if req.source == req.target:
        raise HTTPException(status_code=400, detail="Source and target must differ.")
    for name in (req.source, req.target):
        if name not in BACKENDS:
            raise HTTPException(status_code=400, detail=f"Invalid backend '{name}'.")

    try:
        src_items = await storage_service.list_assets_in(req.source)
    except StorageError as e:
        raise HTTPException(status_code=400, detail=f"Cannot list source: {e}")
    try:
        existing_target = {i["name"] for i in await storage_service.list_assets_in(req.target)}
    except StorageError as e:
        raise HTTPException(status_code=400, detail=f"Cannot list target: {e}")

    copied:  list[str] = []
    skipped: list[str] = []
    failed:  list[dict] = []
    for item in src_items:
        name = item["name"]
        if name in existing_target and not req.overwrite:
            skipped.append(name)
            continue
        try:
            data = await storage_service.fetch_asset_in(req.source, name)
            await storage_service.upload_asset_to(req.target, name, data)
            copied.append(name)
        except StorageError as e:
            failed.append({"name": name, "error": str(e)})

    return {
        "source":  req.source,
        "target":  req.target,
        "copied":  copied,
        "skipped": skipped,
        "failed":  failed,
        "summary": (
            f"{len(copied)} copied, {len(skipped)} skipped (already in target), "
            f"{len(failed)} failed"
        ),
    }


# ── POST /api/storage/upload (active backend only) ───────────────────────────

class UploadAssetRequest(BaseModel):
    filename: str
    content_b64: str


@router.post("/upload", status_code=201)
async def upload_asset(
    req: UploadAssetRequest,
    current_user: User = Depends(get_current_user),
):
    """Upload an asset to the active backend. Open to any logged-in user —
    matches the existing /api/config-mgmt/upload endpoint so the same access
    decision applies. Allowed extensions: .yml/.yaml, .sh, .ps1, .rpm, .deb."""
    import base64
    try:
        data = base64.b64decode(req.content_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="content_b64 is not valid base64.")
    try:
        await storage_service.upload_asset(req.filename, data)
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "filename": req.filename, "size": len(data)}


# ── DELETE /api/storage/asset/{name} (active backend only) ───────────────────

@router.delete("/asset/{name:path}")
async def delete_asset(
    name: str,
    current_user: User = Depends(require_permission("admin", "delete")),
):
    """Delete an asset from the active backend."""
    try:
        await storage_service.delete_asset(name)
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "deleted": name}
