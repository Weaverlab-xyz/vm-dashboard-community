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
    "local":      ["storage_local_path"],
}

# All editable per-backend config keys, in canonical order.
_BACKEND_KEYS = {
    "s3":         ["storage_s3_bucket",       "storage_s3_region",       "storage_s3_prefix"],
    "azure_blob": ["storage_azure_account",   "storage_azure_container", "storage_azure_prefix"],
    "gcs":        ["storage_gcs_bucket",      "storage_gcs_prefix"],
    "local":      ["storage_local_path",      "storage_local_username",  "storage_local_password",
                   "storage_local_domain"],
}

# Backends that only make sense for the local Ansible runner (no cloud
# runner has a network path back to a corporate file server).
_LOCAL_RUNNER_ONLY_BACKENDS = {"local"}


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
    runner = _cfg_get("ansible_runner") or "local"
    labels = {
        "s3":         "AWS S3",
        "azure_blob": "Azure Blob Storage",
        "gcs":        "Google Cloud Storage",
        "local":      "Local Filesystem / UNC",
    }
    return {
        "backends": [
            {
                "id":            b,
                "label":         labels[b],
                "configured":    b in cfgd,
                "active":        b == active,
                # Whether this backend is selectable given the current runner.
                # Local-runner-only backends (UNC) refuse to activate when a
                # cloud runner is selected; surface that to the UI so the
                # radio can disable with a useful tooltip.
                "selectable":    b not in _LOCAL_RUNNER_ONLY_BACKENDS or runner == "local",
                "runner_locked": b in _LOCAL_RUNNER_ONLY_BACKENDS,
            }
            for b in BACKENDS
        ],
        "active":         active,
        "any_active":     bool(active),
        "ansible_runner": runner,
    }


# ── GET /api/storage/config ──────────────────────────────────────────────────

@router.get("/config")
async def get_config(current_user: User = Depends(require_permission("admin", "read"))):
    """Return all per-backend config values. Admin-only because the field
    list overlaps with cloud account scoping."""
    out: dict = {
        "storage_active_backend": _cfg_get("storage_active_backend"),
        "storage_hub_backend":    _cfg_get("storage_hub_backend"),
    }
    for keys in _BACKEND_KEYS.values():
        for k in keys:
            out[k] = _cfg_get(k)
    # Promote-runner config — kept on /storage because it shares the hub
    # backend's lifecycle (runner reads hub via presigned URL).
    for k in (
        "promote_runner_image",
        "promote_runner_ecs_cluster",
        "promote_runner_ecs_task_family",
        "promote_runner_ecs_cpu",
        "promote_runner_ecs_memory",
        "promote_runner_ecs_subnet_id",
        "promote_runner_ecs_security_group_ids",
        "promote_runner_ecs_execution_role_arn",
        "promote_runner_ecs_task_role_arn",
        "promote_runner_aws_staging_bucket",
        "promote_runner_aws_staging_prefix",
        "promote_runner_azure_resource_group",
        "promote_runner_azure_location",
        "promote_runner_azure_subnet_id",
        "promote_runner_azure_cpu",
        "promote_runner_azure_memory_gb",
        "promote_runner_azure_staging_account",
        "promote_runner_azure_staging_container",
        "promote_runner_azure_staging_prefix",
        "promote_runner_azure_target_resource_group",
        "promote_runner_azure_target_storage_account_id",
        "promote_runner_gcp_region",
        "promote_runner_gcp_cpu",
        "promote_runner_gcp_memory",
        "promote_runner_gcp_vpc_connector",
        "promote_runner_gcp_service_account",
        "promote_runner_gcp_staging_bucket",
        "promote_runner_gcp_staging_prefix",
        "promote_runner_gcp_image_family",
        "promote_runner_oci_compartment",
        "promote_runner_oci_availability_domain",
        "promote_runner_oci_subnet_ocid",
        "promote_runner_oci_ocpus",
        "promote_runner_oci_memory_gbs",
        "promote_runner_oci_staging_bucket",
        "promote_runner_oci_staging_prefix",
    ):
        out[k] = _cfg_get(k)
    return out


# ── PATCH /api/storage/config ─────────────────────────────────────────────────

class StorageConfigPatch(BaseModel):
    storage_active_backend: str | None = None
    storage_hub_backend:    str | None = None
    storage_s3_bucket:      str | None = None
    storage_s3_region:      str | None = None
    storage_s3_prefix:      str | None = None
    storage_azure_account:    str | None = None
    storage_azure_container:  str | None = None
    storage_azure_prefix:     str | None = None
    storage_gcs_bucket:     str | None = None
    storage_gcs_prefix:     str | None = None
    storage_local_path:     str | None = None
    storage_local_username: str | None = None
    storage_local_password: str | None = None
    storage_local_domain:   str | None = None
    # Control flag (NOT persisted): when switching storage_active_backend while
    # live Terraform state exists in the current backend, set this true to copy
    # the state to the new backend first instead of being blocked.
    migrate_terraform_state: bool = False
    # Promote-runner overrides — operators only set these when overriding the
    # public image or pinning the ECS task to specific network plumbing.
    promote_runner_image:                  str | None = None
    promote_runner_ecs_cluster:            str | None = None
    promote_runner_ecs_task_family:        str | None = None
    promote_runner_ecs_cpu:                str | None = None
    promote_runner_ecs_memory:             str | None = None
    promote_runner_ecs_subnet_id:          str | None = None
    promote_runner_ecs_security_group_ids: str | None = None
    promote_runner_ecs_execution_role_arn: str | None = None
    promote_runner_ecs_task_role_arn:      str | None = None
    promote_runner_aws_staging_bucket:     str | None = None
    promote_runner_aws_staging_prefix:     str | None = None
    # Azure-target promote runner
    promote_runner_azure_resource_group:            str | None = None
    promote_runner_azure_location:                  str | None = None
    promote_runner_azure_subnet_id:                 str | None = None
    promote_runner_azure_cpu:                       str | None = None
    promote_runner_azure_memory_gb:                 str | None = None
    promote_runner_azure_staging_account:           str | None = None
    promote_runner_azure_staging_container:         str | None = None
    promote_runner_azure_staging_prefix:            str | None = None
    promote_runner_azure_target_resource_group:     str | None = None
    promote_runner_azure_target_storage_account_id: str | None = None
    # GCP-target promote runner
    promote_runner_gcp_region:           str | None = None
    promote_runner_gcp_cpu:              str | None = None
    promote_runner_gcp_memory:           str | None = None
    promote_runner_gcp_vpc_connector:    str | None = None
    promote_runner_gcp_service_account:  str | None = None
    promote_runner_gcp_staging_bucket:   str | None = None
    promote_runner_gcp_staging_prefix:   str | None = None
    promote_runner_gcp_image_family:     str | None = None

    promote_runner_oci_compartment:          str | None = None
    promote_runner_oci_availability_domain:  str | None = None
    promote_runner_oci_subnet_ocid:          str | None = None
    promote_runner_oci_ocpus:                str | None = None
    promote_runner_oci_memory_gbs:           str | None = None
    promote_runner_oci_staging_bucket:       str | None = None
    promote_runner_oci_staging_prefix:       str | None = None


@router.patch("/config")
async def patch_config(
    payload: StorageConfigPatch,
    current_user: User = Depends(require_permission("admin", "write")),
):
    """Partial update — only fields explicitly supplied (non-None) are written.
    Validates that the active backend (if changed) is configured before flipping."""
    raw = payload.model_dump(exclude_unset=True, exclude_none=True)
    # migrate_terraform_state is a control flag, never a stored config key.
    do_migrate_state = bool(raw.pop("migrate_terraform_state", False))
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
            # Local-runner-only backends (e.g. UNC) won't work with cloud
            # runners — the runner has no network path back to the file
            # server. Reject the activation explicitly so users don't get
            # mysterious "tcp 445 timed out" errors at job time.
            if chosen in _LOCAL_RUNNER_ONLY_BACKENDS:
                runner = _cfg_get("ansible_runner") or "local"
                if runner != "local":
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Backend '{chosen}' only works with the local "
                            f"Ansible runner. Settings → Ansible currently "
                            f"selects '{runner}'. Switch the runner to "
                            f"'local' before activating this backend."
                        ),
                    )
    if "storage_hub_backend" in raw:
        chosen_hub = raw["storage_hub_backend"]
        # Empty string means "fall back to active backend" — that's valid.
        if chosen_hub and chosen_hub not in BACKENDS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid hub backend '{chosen_hub}'. Valid: {', '.join(BACKENDS)}.",
            )
        # Hub holds VHD artefacts the promote runners read via HTTPS, so the
        # local/UNC backend can't be a hub (no presigned URL surface).
        if chosen_hub == "local":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Local/SMB backend can't host the image-registry hub — promote "
                    "runners need a cloud-native URL. Pick s3, azure_blob, or gcs."
                ),
            )
        if chosen_hub:
            required = _REQUIRED_FIELDS[chosen_hub]
            for k in required:
                if not (raw.get(k) or _cfg_get(k)):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Cannot set hub to '{chosen_hub}' — missing required field "
                            f"'{k}'. Configure that backend before pointing the hub at it."
                        ),
                    )
    # ── Terraform state migration guard ──────────────────────────────────────
    # Don't let the active storage backend change out from under live Terraform
    # state (it would strand it — the new backend has no state, so destroy can't
    # run). Block the swap, or migrate the state first when explicitly asked.
    migrated_state = 0
    if "storage_active_backend" in raw:
        chosen = raw["storage_active_backend"]
        current = storage_service.active_backend()
        if chosen and current and chosen != current:
            if await storage_service.has_terraform_state(current):
                if not do_migrate_state:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Live Terraform state exists in '{current}'. Switching the "
                            f"active backend to '{chosen}' would strand it. Re-send with "
                            f"migrate_terraform_state=true to copy the state to '{chosen}' "
                            f"first (the '{current}' copy is kept as a backup)."
                        ),
                    )
                try:
                    migrated_state = await storage_service.migrate_terraform_state(current, chosen)
                except StorageError as e:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Terraform state migration {current}→{chosen} failed: {e}. "
                               f"Active backend NOT changed.",
                    )
                logger.info("Migrated %d Terraform state object(s) %s→%s before backend swap",
                            migrated_state, current, chosen)

    _cfg_set_many(raw)
    return {"ok": True, "updated": list(raw.keys()), "terraform_state_migrated": migrated_state}


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
    # Advisory secret scan (never blocks the upload — a heads-up only).
    findings = []
    from ..services import config_service as cs, secret_scan
    if cs.get_bool("secret_scan_enabled", True):
        findings = secret_scan.scan_bytes(data, req.filename)

    try:
        await storage_service.upload_asset(req.filename, data)
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "filename": req.filename, "size": len(data),
            "secret_findings": findings}


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


# ── GET /api/storage/list-all ────────────────────────────────────────────────

@router.get("/list-all")
async def list_all(current_user: User = Depends(get_current_user)):
    """Aggregated asset list across every *configured* backend. Each item is
    tagged with the backend it lives on so the Storage page can render
    per-backend rows and the Config Mgmt page can warn when a local-only asset
    is paired with a cloud target."""
    items = await storage_service.list_all_assets()
    return {"items": items, "count": len(items)}


# ── POST /api/storage/move ───────────────────────────────────────────────────

class MoveRequest(BaseModel):
    name: str
    from_backend: str
    to_backend: str


@router.post("/move")
async def move_asset(
    req: MoveRequest,
    current_user: User = Depends(require_permission("admin", "write")),
):
    """Move a single asset from one backend to another (copy + delete source).
    Used to relocate playbooks from local filesystem to a cloud backend so a
    cloud-side ansible runner can fetch them. Atomicity: if the copy succeeds
    but the source delete fails, the asset ends up duplicated and the response
    error message tells the operator to clean the source up by hand."""
    try:
        await storage_service.move_asset(req.name, req.from_backend, req.to_backend)
    except StorageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "moved": req.name, "from": req.from_backend, "to": req.to_backend}


# ── GET /api/storage/fetch/{backend}/{name} ──────────────────────────────────

@router.get("/fetch/{backend}/{name:path}")
async def fetch_asset(
    backend: str,
    name: str,
    current_user: User = Depends(get_current_user),
):
    """Return the raw bytes of a stored asset as base64. Used by the Packer
    image builder forms to load a stored .sh script into the provisioner
    textarea instead of forcing the operator to copy-paste from elsewhere."""
    try:
        data = await storage_service.fetch_asset_in(backend, name)
    except StorageError as e:
        raise HTTPException(status_code=404, detail=str(e))
    import base64
    return {
        "name": name,
        "backend": backend,
        "content_b64": base64.b64encode(data).decode(),
        "size": len(data),
    }


# ── DELETE /api/storage/asset-in/{backend}/{name} ────────────────────────────

@router.delete("/asset-in/{backend}/{name:path}")
async def delete_asset_in(
    backend: str,
    name: str,
    current_user: User = Depends(require_permission("admin", "delete")),
):
    """Delete an asset from a *specific* backend (sibling of /asset/{name}
    which targets the active backend). Needed once the UI surfaces assets
    from multiple backends side by side."""
    try:
        await storage_service.delete_asset_in(backend, name)
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "deleted": name, "backend": backend}


# ── POST /api/storage/bulk-delete ────────────────────────────────────────────

class BulkDeleteItem(BaseModel):
    backend: str
    name: str


class BulkDeleteRequest(BaseModel):
    items: list[BulkDeleteItem]


@router.post("/bulk-delete")
async def bulk_delete(
    req: BulkDeleteRequest,
    current_user: User = Depends(require_permission("admin", "delete")),
):
    """Delete many assets in one call. Each item names its source backend so
    the UI can mix assets from different backends in a single bulk action
    (issue #13). Continues on per-item failure and returns a per-item
    success/error report — the user gets to see which ones worked."""
    if not req.items:
        raise HTTPException(status_code=400, detail="No items to delete.")
    deleted: list[dict] = []
    failed:  list[dict] = []
    for item in req.items:
        try:
            await storage_service.delete_asset_in(item.backend, item.name)
            deleted.append({"name": item.name, "backend": item.backend})
        except StorageError as e:
            failed.append({"name": item.name, "backend": item.backend, "error": str(e)})
    return {
        "deleted": deleted,
        "failed":  failed,
        "summary": f"{len(deleted)} deleted, {len(failed)} failed",
    }
