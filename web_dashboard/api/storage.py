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
  POST   /api/storage/upload           — upload one asset inline (base64 in JSON)
  POST   /api/storage/upload/begin     — open a chunked upload (large files)
  PUT    /api/storage/upload/part/{n}  — stage one raw part of a chunked upload
  POST   /api/storage/upload/commit    — commit the staged parts into one object
  POST   /api/storage/upload/abort     — discard a chunked upload's staged parts

Storage today stores Ansible playbooks/scripts/packages, but is general-purpose.
Future features that need a small object store can layer on top of it
without re-introducing per-feature backend configuration.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, get_db
from ..services import storage_chunked, storage_service
from ..services.storage_service import BACKENDS, StorageError
from .auth import get_current_user, require_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/storage", tags=["storage"])


# Per-backend "configured?" required fields. Mirrors storage_service.
_REQUIRED_FIELDS = {
    "s3":         ["storage_s3_bucket"],
    "azure_blob": ["storage_azure_account"],
    "gcs":        ["storage_gcs_bucket"],
    "oci_object_storage": ["storage_oci_bucket"],
    "local":      ["storage_local_path"],
    # Both halves of the join. The path is NOT here — it lives in the agent's shares.yaml,
    # which is the point of this backend.
    "agent_local": ["storage_agent_id", "storage_agent_share"],
}

# All editable per-backend config keys, in canonical order.
_BACKEND_KEYS = {
    "s3":         ["storage_s3_bucket",       "storage_s3_region",       "storage_s3_prefix"],
    "azure_blob": ["storage_azure_account",   "storage_azure_container", "storage_azure_prefix"],
    "gcs":        ["storage_gcs_bucket",      "storage_gcs_prefix"],
    "oci_object_storage": ["storage_oci_bucket", "storage_oci_namespace", "storage_oci_prefix"],
    "local":      ["storage_local_path",      "storage_local_username",  "storage_local_password",
                   "storage_local_domain"],
    "agent_local": ["storage_agent_id",       "storage_agent_share",     "storage_agent_subpath"],
}

# Backends that only make sense for the local Ansible runner (no cloud
# runner has a network path back to a corporate file server).
#
# `agent_local` is deliberately NOT here, and the difference is the whole feature. The
# constraint on `local` is that THIS CONTAINER opens the SMB socket; the agent-brokered
# backend never asks anything in a cloud to reach a file server, because the dashboard
# fetches the bytes through the agent first and hands them to the runner exactly as it
# does for S3.
_LOCAL_RUNNER_ONLY_BACKENDS = {"local"}

# Backends that cannot host the image-registry hub. The promote runners read the canonical
# artefact over HTTPS from a presigned URL, and neither of these has a URL surface at all.
_NO_HUB_BACKENDS = {"local", "agent_local"}


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
        "oci_object_storage": "OCI Object Storage",
        "local":      "Local Filesystem / UNC",
        "agent_local": "Remote Filesystem / UNC (via agent)",
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
                "selectable":    (b not in _LOCAL_RUNNER_ONLY_BACKENDS or runner == "local")
                                 and b not in storage_service.ACTIVE_BACKEND_EXCLUSIONS,
                "runner_locked": b in _LOCAL_RUNNER_ONLY_BACKENDS,
                # Brokered by a remote agent rather than reached from this container.
                # The UI badges it, and the fields card asks for an agent instead of a path.
                "via_agent":     b == "agent_local",
                # Hub-capable but not activatable (OCI: no Terraform state backend).
                # The UI disables the "active" radio and shows this as the reason.
                "hub_only":      b in storage_service.ACTIVE_BACKEND_EXCLUSIONS,
                "hub_only_reason": storage_service.ACTIVE_BACKEND_EXCLUSIONS.get(b, ""),
                # Largest file this backend accepts through the upload form, and why. The
                # form needs both BEFORE it reads the file: an oversize pick that gets as
                # far as base64 has already cost the tab more memory than it has.
                #
                # For an object store this is the CHUNKED ceiling, because that is what
                # the form will actually do with a big file — see the three keys below.
                # `max_upload_bytes` is deliberately the number the page refuses on, so
                # there is exactly one ceiling in the UI rather than two the operator has
                # to reconcile.
                "max_upload_bytes":    storage_service.max_form_upload_bytes(b),
                "upload_limit_reason": (
                    storage_chunked.chunked_upload_reason(b)
                    if storage_chunked.supports_chunked_upload(b)
                    else storage_service.upload_ceiling_reason(b)),
                # Whether this backend has the chunked lane at all, the size ABOVE which
                # the form switches into it, and the part size the server will insist on.
                # All three come from here rather than being restated in the template, for
                # the reason the ceiling always has: two readers of one limit drift, and
                # the part size in particular is not a preference — a part of the wrong
                # length is refused at the first part.
                "chunked_upload":     storage_chunked.supports_chunked_upload(b),
                "inline_upload_bytes": storage_service.max_upload_bytes(b),
                "chunk_part_bytes":   storage_chunked.part_bytes(),
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
        "promote_runner_ecs_ephemeral_storage_gib",
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
    storage_oci_bucket:     str | None = None
    storage_oci_namespace:  str | None = None
    storage_oci_prefix:     str | None = None
    storage_local_path:     str | None = None
    storage_local_username: str | None = None
    storage_local_password: str | None = None
    storage_local_domain:   str | None = None
    storage_agent_id:       str | None = None
    storage_agent_share:    str | None = None
    storage_agent_subpath:  str | None = None
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
    promote_runner_ecs_ephemeral_storage_gib: str | None = None
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
        # Some backends can hold the image hub but must never be the ACTIVE
        # backend — the active selection also decides where Terraform state
        # lives, and services/terraform.py silently falls through to local for
        # a backend it has no state mapping for. Reject explicitly rather than
        # let deployment state land on ephemeral container storage.
        if chosen in storage_service.ACTIVE_BACKEND_EXCLUSIONS:
            raise HTTPException(
                status_code=400,
                detail=storage_service.ACTIVE_BACKEND_EXCLUSIONS[chosen],
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
        # Hub holds VHD artefacts the promote runners read via HTTPS, so neither
        # filesystem backend can be a hub (no presigned URL surface).
        if chosen_hub in _NO_HUB_BACKENDS:
            raise HTTPException(
                status_code=400,
                detail=(
                    "A filesystem backend can't host the image-registry hub — promote "
                    "runners need a cloud-native URL, and multi-GB VHDs do not fit a "
                    "job envelope. Pick s3, azure_blob, gcs, or oci_object_storage."
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
    decision applies. Allowed extensions: .yml/.yaml, .sh, .ps1, .rpm, .deb,
    .exe/.msi — the last two are Windows installers, and are what stages a POV's
    Password Safe Resource Broker bootstrapper."""
    import base64
    try:
        data = base64.b64decode(req.content_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="content_b64 is not valid base64.")
    # The inline-transport ceiling, enforced where the base64 body actually arrives. It
    # is not in `storage_service.upload_asset` because server-side writers (EPM-L package
    # sync) reach that without a JSON body and must not inherit a browser's limit.
    try:
        storage_service.check_inline_upload(req.filename, len(data))
    except storage_service.UploadTooLarge as e:
        raise HTTPException(status_code=413, detail=str(e))
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


# ── Chunked upload (active backend, object stores only) ──────────────────────
# The lane above `MAX_INLINE_UPLOAD_BYTES`. Four calls rather than one because the point
# is that no single request — and no single allocation on either side — is the size of the
# file: begin fixes the layout, each part is staged straight into the object store, and
# commit stitches them. See services/storage_chunked.py for why the session is an
# encrypted handle and not a row.
#
# Same access decision as the inline `/upload`: any logged-in user. A bigger file is not a
# more privileged operation, and splitting the permission would mean an operator who can
# upload a playbook cannot upload the installer it runs.

class BeginChunkedUploadRequest(BaseModel):
    filename: str
    size: int


class CommitChunkedUploadPart(BaseModel):
    part: int
    ref: str = ""


class CommitChunkedUploadRequest(BaseModel):
    handle: str
    parts: list[CommitChunkedUploadPart]


class AbortChunkedUploadRequest(BaseModel):
    handle: str


# The handle travels in a header, not in the path or a query string: it is long, and it
# carries the object store's own upload reference — for GCS a resumable session URI, which
# is a write capability. Neither belongs in an access log.
_HANDLE_HEADER = "X-Upload-Handle"


def _upload_handle(request) -> str:
    handle = request.headers.get(_HANDLE_HEADER) or ""
    if not handle:
        raise HTTPException(
            status_code=400,
            detail=f"Missing {_HANDLE_HEADER} — call /api/storage/upload/begin first.")
    return handle


@router.post("/upload/begin", status_code=201)
async def begin_chunked_upload(
    req: BeginChunkedUploadRequest,
    current_user: User = Depends(get_current_user),
):
    """Open a chunked upload against the active backend.

    Returns the handle every later call quotes, plus the part size and part count the
    client must slice to. The layout is the server's to decide — see storage_chunked.
    """
    backend = storage_service.active_backend()
    if not backend:
        raise HTTPException(
            status_code=400,
            detail="No active storage backend. Configure one on /storage and select it "
                   "as active.")
    try:
        session = await storage_chunked.begin_upload(
            backend, req.filename, req.size, username=current_user.username)
    except storage_service.UploadTooLarge as e:
        raise HTTPException(status_code=413, detail=str(e))
    except StorageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return session


@router.put("/upload/part/{part_number}")
async def stage_chunked_upload_part(
    part_number: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Stage one raw part. Body is the bytes themselves — no base64, no JSON envelope.

    Content-Length is checked BEFORE the body is read. `await request.body()` buffers
    whatever arrives, so a client that ignored the part size could otherwise make this
    worker allocate the whole file — the exact failure the chunked lane exists to avoid,
    moved from the browser to the server.
    """
    handle = _upload_handle(request)
    declared = request.headers.get("content-length")
    limit = storage_chunked.part_bytes()
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"A part may not exceed {limit} bytes; this one declares {declared}.")
    data = await request.body()
    try:
        result = await storage_chunked.stage_part(
            handle, part_number, data, username=current_user.username)
    except StorageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to stage part %s", part_number)
        raise HTTPException(status_code=502, detail=f"Failed to stage part: {e}")
    # Advisory secret scan, on the FIRST part only. `secret_scan.scan_bytes` gives up when
    # it finds a NUL in the first 8 KiB, so for the installers this lane exists for it
    # answers from part 1 or not at all — and re-scanning 8 MiB at a time for a file that
    # is by definition too big to hold would cost more than it can find.
    findings = []
    if part_number == 1 and data:
        from ..services import config_service as cs, secret_scan
        if cs.get_bool("secret_scan_enabled", True):
            name = storage_chunked.session_filename(handle, current_user.username) or ""
            findings = secret_scan.scan_bytes(data, name)
    return {"ok": True, **result, "secret_findings": findings}


@router.post("/upload/commit", status_code=201)
async def commit_chunked_upload(
    req: CommitChunkedUploadRequest,
    current_user: User = Depends(get_current_user),
):
    """Stitch the staged parts into the object. Until this returns, nothing is listable."""
    try:
        result = await storage_chunked.commit_upload(
            req.handle,
            [{"part": p.part, "ref": p.ref} for p in req.parts],
            username=current_user.username,
        )
    except StorageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to commit chunked upload")
        raise HTTPException(status_code=502, detail=f"Failed to commit upload: {e}")
    return {"ok": True, **result}


@router.post("/upload/abort")
async def abort_chunked_upload(
    req: AbortChunkedUploadRequest,
    current_user: User = Depends(get_current_user),
):
    """Discard the staged parts — the Cancel button, and what a failed part triggers.

    Never 502s. An abort that fails has cost nothing: every store here expires an
    uncommitted upload on its own, and reporting a failure to clean up as an error would
    leave the page insisting something is wrong when the upload is already gone.
    """
    try:
        return {"ok": True, **await storage_chunked.abort_upload(
            req.handle, username=current_user.username)}
    except StorageError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
