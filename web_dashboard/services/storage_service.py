"""
Cloud object storage abstraction.

Three backends — AWS S3, Azure Blob Storage, Google Cloud Storage — exposed
through a single per-backend operation set so callers don't care which is
active. Originally introduced for Ansible playbook/asset hosting; intended
to grow to other features (image manifests, log archives, anything where
a small object store fits).

Backend selection comes from the explicit `storage_active_backend` config
key. A backend is considered *configured* if its required fields are
populated, regardless of whether it's the active one — this matters for
the migrate flow, which copies between any two configured backends.

Supported asset types:
    .yml / .yaml  — Ansible playbooks
    .sh           — shell scripts
    .rpm / .deb   — packages

Public API:
    list_assets()                          — assets in the active backend
    fetch_asset_b64(name)                  — base64 of a single asset
    upload_asset(name, data)               — write to the active backend
    delete_asset(name)                     — remove from the active backend
    configured_backends()                  — list of backend names with required config set
    active_backend()                       — the configured-active backend, or "" if none
    list_assets_in(backend)                — list contents of a specific backend (for migration)
    fetch_asset_in(backend, name)          — fetch from a specific backend (for migration)
    upload_asset_to(backend, name, data)   — write to a specific backend (for migration)
"""
import asyncio
import base64
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_ASSET_EXTENSIONS = {".yml", ".yaml", ".sh", ".ps1", ".rpm", ".deb"}
_TYPE_MAP = {
    ".yml":  "playbook",
    ".yaml": "playbook",
    ".sh":   "script",
    ".ps1":  "powershell",
    ".rpm":  "rpm",
    ".deb":  "deb",
}

BACKENDS = ("s3", "azure_blob", "gcs", "local")


def _asset_type(name: str) -> str:
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    return _TYPE_MAP.get(ext, "playbook")


def asset_type(name: str) -> str:
    """Public alias — used by api/config_mgmt.py."""
    return _asset_type(name)


class StorageError(Exception):
    pass


# Back-compat alias — older callers imported AnsibleStorageError from this
# module under its previous name. Keep the symbol so they don't break on
# import while the rename rolls through downstream code.
AnsibleStorageError = StorageError


def _cfg(key: str) -> str:
    from . import config_service
    return config_service.get(key) or ""


# ── Per-backend "configured?" probe ───────────────────────────────────────────

def _backend_configured(backend: str) -> bool:
    """A backend is configured iff its primary identifier is set. The other
    fields all have safe defaults so we don't gate on them."""
    if backend == "s3":
        return bool(_cfg("storage_s3_bucket"))
    if backend == "azure_blob":
        return bool(_cfg("storage_azure_account"))
    if backend == "gcs":
        return bool(_cfg("storage_gcs_bucket"))
    if backend == "local":
        return bool(_cfg("storage_local_path"))
    return False


def configured_backends() -> list[str]:
    return [b for b in BACKENDS if _backend_configured(b)]


def active_backend() -> str:
    """Return the user-selected active backend, validated against the
    `configured_backends()` list. If the selection is missing required
    config or unset, returns "" so callers can render a useful error."""
    chosen = _cfg("storage_active_backend")
    if chosen and chosen in BACKENDS and _backend_configured(chosen):
        return chosen
    # Fall back to the first configured backend so existing setups that
    # never set storage_active_backend still work.
    cfgd = configured_backends()
    return cfgd[0] if cfgd else ""


def hub_backend() -> str:
    """The image-registry hub backend — single source of truth for the
    canonical VHD/raw artefact across builds. Resolves explicit
    `storage_hub_backend` config; falls back to `active_backend()` so
    single-backend installs Just Work without operator intervention.
    Returns "" if no backend is usable."""
    chosen = _cfg("storage_hub_backend")
    if chosen and chosen in BACKENDS and _backend_configured(chosen):
        return chosen
    return active_backend()


# ── S3 backend ────────────────────────────────────────────────────────────────

def _s3_client():
    try:
        import boto3  # noqa: F401
    except ImportError:
        raise StorageError("boto3 is not installed")
    from .aws_service import _aws_kwargs
    region = _cfg("storage_s3_region") or _cfg("aws_region")
    import boto3
    return boto3.client("s3", **_aws_kwargs(region))


def _s3_prefix() -> str:
    return (_cfg("storage_s3_prefix") or "config-mgmt").rstrip("/")


def _s3_list_sync() -> list[dict]:
    bucket = _cfg("storage_s3_bucket")
    prefix = _s3_prefix()
    client = _s3_client()
    paginator = client.get_paginator("list_objects_v2")
    assets = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix + "/"):
        for obj in page.get("Contents", []):
            key = obj["Key"][len(prefix) + 1:]
            if key and any(key.endswith(ext) for ext in _ASSET_EXTENSIONS):
                assets.append({"name": key, "type": _asset_type(key), "size": obj.get("Size", 0)})
    return sorted(assets, key=lambda x: x["name"])


def _s3_fetch_sync(name: str) -> bytes:
    bucket = _cfg("storage_s3_bucket")
    client = _s3_client()
    resp = client.get_object(Bucket=bucket, Key=f"{_s3_prefix()}/{name}")
    return resp["Body"].read()


def _s3_upload_sync(name: str, data: bytes) -> None:
    import io
    bucket = _cfg("storage_s3_bucket")
    client = _s3_client()
    client.upload_fileobj(io.BytesIO(data), bucket, f"{_s3_prefix()}/{name}")


def _s3_delete_sync(name: str) -> None:
    bucket = _cfg("storage_s3_bucket")
    client = _s3_client()
    client.delete_object(Bucket=bucket, Key=f"{_s3_prefix()}/{name}")


# ── Azure Blob Storage backend ────────────────────────────────────────────────

def _azure_blob_client():
    try:
        from azure.storage.blob import BlobServiceClient  # noqa: F401
    except ImportError:
        raise StorageError("azure-storage-blob or azure-identity is not installed")
    from azure.identity import ClientSecretCredential
    from azure.storage.blob import BlobServiceClient
    account = _cfg("storage_azure_account")
    cred = ClientSecretCredential(
        tenant_id=_cfg("azure_tenant_id"),
        client_id=_cfg("azure_client_id"),
        client_secret=_cfg("azure_client_secret"),
    )
    return BlobServiceClient(account_url=f"https://{account}.blob.core.windows.net", credential=cred)


def _azure_container() -> str:
    return _cfg("storage_azure_container") or "playbooks"


def _azure_prefix() -> str:
    return (_cfg("storage_azure_prefix") or "config-mgmt").rstrip("/")


def _azure_list_sync() -> list[dict]:
    svc = _azure_blob_client()
    container = _azure_container()
    prefix = _azure_prefix()
    cc = svc.get_container_client(container)
    assets = []
    for blob in cc.list_blobs(name_starts_with=prefix + "/"):
        name = blob.name[len(prefix) + 1:]
        if name and any(name.endswith(ext) for ext in _ASSET_EXTENSIONS):
            assets.append({"name": name, "type": _asset_type(name), "size": getattr(blob, "size", 0)})
    return sorted(assets, key=lambda x: x["name"])


def _azure_fetch_sync(name: str) -> bytes:
    svc = _azure_blob_client()
    blob_client = svc.get_blob_client(container=_azure_container(), blob=f"{_azure_prefix()}/{name}")
    return blob_client.download_blob().readall()


def _azure_upload_sync(name: str, data: bytes) -> None:
    svc = _azure_blob_client()
    blob_client = svc.get_blob_client(container=_azure_container(), blob=f"{_azure_prefix()}/{name}")
    blob_client.upload_blob(data, overwrite=True)


def _azure_delete_sync(name: str) -> None:
    svc = _azure_blob_client()
    blob_client = svc.get_blob_client(container=_azure_container(), blob=f"{_azure_prefix()}/{name}")
    blob_client.delete_blob()


# ── GCS backend ───────────────────────────────────────────────────────────────

def _gcs_client():
    try:
        from google.cloud import storage as gcs  # noqa: F401
    except ImportError:
        raise StorageError("google-cloud-storage is not installed")
    from .gcp_service import _gcp_creds
    from google.cloud import storage as gcs
    return gcs.Client(credentials=_gcp_creds(), project=_cfg("gcp_project_id"))


def _gcs_prefix() -> str:
    return (_cfg("storage_gcs_prefix") or "config-mgmt").rstrip("/")


def _gcs_list_sync() -> list[dict]:
    client = _gcs_client()
    bucket_name = _cfg("storage_gcs_bucket")
    prefix = _gcs_prefix()
    assets = []
    for blob in client.list_blobs(bucket_name, prefix=prefix + "/"):
        name = blob.name[len(prefix) + 1:]
        if name and any(name.endswith(ext) for ext in _ASSET_EXTENSIONS):
            assets.append({"name": name, "type": _asset_type(name), "size": blob.size or 0})
    return sorted(assets, key=lambda x: x["name"])


def _gcs_fetch_sync(name: str) -> bytes:
    client = _gcs_client()
    bucket = client.bucket(_cfg("storage_gcs_bucket"))
    blob = bucket.blob(f"{_gcs_prefix()}/{name}")
    return blob.download_as_bytes()


def _gcs_upload_sync(name: str, data: bytes) -> None:
    client = _gcs_client()
    bucket = client.bucket(_cfg("storage_gcs_bucket"))
    blob = bucket.blob(f"{_gcs_prefix()}/{name}")
    blob.upload_from_string(data)


def _gcs_delete_sync(name: str) -> None:
    client = _gcs_client()
    bucket = client.bucket(_cfg("storage_gcs_bucket"))
    blob = bucket.blob(f"{_gcs_prefix()}/{name}")
    blob.delete()


# ── Local filesystem / SMB UNC backend ───────────────────────────────────────
# Path may be either a normal filesystem path (anything not starting with
# `\\` or `//`) or a UNC `\\server\share[\subpath]`. UNC paths are read via
# the smbprotocol library — no host-side mount required. Credentials only
# apply to UNC. Only useful for on-prem hypervisor targets running off the
# local Ansible runner; cloud runners (ECS / ACI / Cloud Run) have no path
# back to a corporate file server.


def _is_unc(path: str) -> bool:
    return path.startswith("\\\\") or path.startswith("//")


def _local_smb_register():
    """Configure smbclient session with the optional credentials. Idempotent
    — registering the same server twice replaces the previous registration."""
    import smbclient
    user = _cfg("storage_local_username")
    pwd  = _cfg("storage_local_password")
    dom  = _cfg("storage_local_domain")
    if user:
        # Pull the server name out of the UNC path so we can register
        # per-server credentials. smbclient uses these on subsequent ops.
        path = _cfg("storage_local_path").replace("\\", "/").lstrip("/")
        server = path.split("/", 1)[0] if path else ""
        if server:
            smbclient.register_session(
                server,
                username=(f"{dom}\\{user}" if dom else user),
                password=pwd or None,
            )


def _local_normalise(path: str) -> str:
    """Smbclient accepts either back- or forward-slash. We normalise to
    backslashes for UNC and forward-slashes for filesystem to match each
    OS's idiom in error messages."""
    return path.rstrip("/\\")


def _local_path_for(name: str) -> str:
    base = _local_normalise(_cfg("storage_local_path"))
    sep = "\\" if _is_unc(base) else "/"
    return f"{base}{sep}{name}"


def _local_list_sync() -> list[dict]:
    base = _local_normalise(_cfg("storage_local_path"))
    if not base:
        raise StorageError("storage_local_path is not set.")
    if _is_unc(base):
        import smbclient
        _local_smb_register()
        out = []
        try:
            for entry in smbclient.scandir(base):
                if not entry.is_file():
                    continue
                if any(entry.name.endswith(ext) for ext in _ASSET_EXTENSIONS):
                    try:
                        size = entry.stat().st_size
                    except Exception:
                        size = 0
                    out.append({"name": entry.name, "type": _asset_type(entry.name), "size": size})
        except Exception as e:
            raise StorageError(f"SMB list failed for {base}: {e}") from e
    else:
        import os
        if not os.path.isdir(base):
            raise StorageError(f"Path '{base}' does not exist or is not a directory.")
        out = []
        for name in os.listdir(base):
            full = os.path.join(base, name)
            if not os.path.isfile(full):
                continue
            if any(name.endswith(ext) for ext in _ASSET_EXTENSIONS):
                out.append({"name": name, "type": _asset_type(name), "size": os.path.getsize(full)})
    return sorted(out, key=lambda x: x["name"])


def _local_fetch_sync(name: str) -> bytes:
    target = _local_path_for(name)
    if _is_unc(target):
        import smbclient
        _local_smb_register()
        try:
            with smbclient.open_file(target, mode="rb") as f:
                return f.read()
        except Exception as e:
            raise StorageError(f"SMB read failed for {target}: {e}") from e
    else:
        try:
            with open(target, "rb") as f:
                return f.read()
        except FileNotFoundError:
            raise StorageError(f"Asset '{name}' not found at {target}.")
        except Exception as e:
            raise StorageError(f"Local read failed for {target}: {e}") from e


def _local_upload_sync(name: str, data: bytes) -> None:
    target = _local_path_for(name)
    if _is_unc(target):
        import smbclient
        _local_smb_register()
        try:
            with smbclient.open_file(target, mode="wb") as f:
                f.write(data)
        except Exception as e:
            raise StorageError(f"SMB write failed for {target}: {e}") from e
    else:
        import os
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        try:
            with open(target, "wb") as f:
                f.write(data)
        except Exception as e:
            raise StorageError(f"Local write failed for {target}: {e}") from e


def _local_delete_sync(name: str) -> None:
    target = _local_path_for(name)
    if _is_unc(target):
        import smbclient
        _local_smb_register()
        try:
            smbclient.remove(target)
        except Exception as e:
            raise StorageError(f"SMB delete failed for {target}: {e}") from e
    else:
        import os
        try:
            os.remove(target)
        except FileNotFoundError:
            pass
        except Exception as e:
            raise StorageError(f"Local delete failed for {target}: {e}") from e


# ── Backend dispatch table ────────────────────────────────────────────────────

_BACKEND_OPS = {
    "s3":         {"list": _s3_list_sync,    "fetch": _s3_fetch_sync,    "upload": _s3_upload_sync,    "delete": _s3_delete_sync},
    "azure_blob": {"list": _azure_list_sync, "fetch": _azure_fetch_sync, "upload": _azure_upload_sync, "delete": _azure_delete_sync},
    "gcs":        {"list": _gcs_list_sync,   "fetch": _gcs_fetch_sync,   "upload": _gcs_upload_sync,   "delete": _gcs_delete_sync},
    "local":      {"list": _local_list_sync, "fetch": _local_fetch_sync, "upload": _local_upload_sync, "delete": _local_delete_sync},
}


def _require_active() -> str:
    backend = active_backend()
    if not backend:
        raise StorageError(
            "No active storage backend. Configure one on /storage and select it as active."
        )
    return backend


def _validate_backend(backend: str) -> None:
    if backend not in BACKENDS:
        raise StorageError(f"Unknown backend '{backend}'. Valid: {', '.join(BACKENDS)}.")
    if not _backend_configured(backend):
        raise StorageError(f"Backend '{backend}' is not configured.")


# ── Public API: active-backend operations ────────────────────────────────────

async def list_assets() -> list[dict]:
    backend = _require_active()
    try:
        return await asyncio.to_thread(_BACKEND_OPS[backend]["list"])
    except StorageError:
        raise
    except Exception as e:
        raise StorageError(f"Failed to list assets from {backend}: {e}") from e


async def list_playbooks() -> list[str]:
    """Playbook names only — back-compat shim."""
    return [a["name"] for a in await list_assets() if a["type"] == "playbook"]


async def fetch_asset_b64(name: str) -> str:
    backend = _require_active()
    try:
        data = await asyncio.to_thread(_BACKEND_OPS[backend]["fetch"], name)
        return base64.b64encode(data).decode()
    except StorageError:
        raise
    except Exception as e:
        raise StorageError(f"Failed to fetch asset '{name}' from {backend}: {e}") from e


async def fetch_playbook_b64(name: str) -> str:
    return await fetch_asset_b64(name)


async def upload_asset(name: str, data: bytes) -> None:
    backend = _require_active()
    if not any(name.endswith(ext) for ext in _ASSET_EXTENSIONS):
        raise StorageError(
            f"Unsupported file type for '{name}'. "
            f"Allowed extensions: {', '.join(sorted(_ASSET_EXTENSIONS))}"
        )
    try:
        await asyncio.to_thread(_BACKEND_OPS[backend]["upload"], name, data)
    except StorageError:
        raise
    except Exception as e:
        raise StorageError(f"Failed to upload '{name}' to {backend}: {e}") from e


async def delete_asset(name: str) -> None:
    backend = _require_active()
    try:
        await asyncio.to_thread(_BACKEND_OPS[backend]["delete"], name)
    except StorageError:
        raise
    except Exception as e:
        raise StorageError(f"Failed to delete '{name}' from {backend}: {e}") from e


# ── Public API: explicit-backend operations (used by /storage and migrate) ───

async def list_assets_in(backend: str) -> list[dict]:
    _validate_backend(backend)
    try:
        return await asyncio.to_thread(_BACKEND_OPS[backend]["list"])
    except StorageError:
        raise
    except Exception as e:
        raise StorageError(f"Failed to list assets from {backend}: {e}") from e


async def fetch_asset_in(backend: str, name: str) -> bytes:
    _validate_backend(backend)
    try:
        return await asyncio.to_thread(_BACKEND_OPS[backend]["fetch"], name)
    except StorageError:
        raise
    except Exception as e:
        raise StorageError(f"Failed to fetch asset '{name}' from {backend}: {e}") from e


async def upload_asset_to(backend: str, name: str, data: bytes) -> None:
    _validate_backend(backend)
    if not any(name.endswith(ext) for ext in _ASSET_EXTENSIONS):
        raise StorageError(
            f"Unsupported file type for '{name}'. "
            f"Allowed extensions: {', '.join(sorted(_ASSET_EXTENSIONS))}"
        )
    try:
        await asyncio.to_thread(_BACKEND_OPS[backend]["upload"], name, data)
    except StorageError:
        raise
    except Exception as e:
        raise StorageError(f"Failed to upload '{name}' to {backend}: {e}") from e


async def delete_asset_in(backend: str, name: str) -> None:
    """Delete an asset from a specific backend (sibling of delete_asset which
    targets the active backend)."""
    _validate_backend(backend)
    try:
        await asyncio.to_thread(_BACKEND_OPS[backend]["delete"], name)
    except StorageError:
        raise
    except Exception as e:
        raise StorageError(f"Failed to delete '{name}' from {backend}: {e}") from e


# ── Public API: multi-backend views (config-mgmt + /storage page) ────────────

async def list_all_assets() -> list[dict]:
    """Return assets across every configured backend, tagged with the
    backend they live on. Duplicate names (same asset on multiple backends)
    appear once per backend so the UI can show where each copy lives.
    Used by the Config Mgmt asset picker and the Storage page asset table.
    """
    out: list[dict] = []
    for backend in configured_backends():
        try:
            items = await list_assets_in(backend)
        except StorageError as exc:
            logger.warning("list_all_assets: %s skipped (%s)", backend, exc)
            continue
        for item in items:
            out.append({**item, "backend": backend})
    return out


# Cloud-eligible backends — these are reachable from a containerised ansible
# runner (ECS task, ACI, Cloud Run). The `local` backend is the filesystem
# of the dashboard host and is NOT reachable from cloud runners — the
# Config Mgmt page surfaces a warning when a user picks a local asset for
# a cloud target. See issue #16.
CLOUD_BACKENDS = ("s3", "azure_blob", "gcs")


def is_cloud_backend(backend: str) -> bool:
    return backend in CLOUD_BACKENDS


async def move_asset(name: str, from_backend: str, to_backend: str) -> None:
    """Copy an asset from one backend to another, then delete the source.
    Used by the Storage page's "Move" action — operators typically use it
    to relocate a playbook from local filesystem to a cloud backend so a
    cloud-side ansible runner can fetch it.
    """
    _validate_backend(from_backend)
    _validate_backend(to_backend)
    if from_backend == to_backend:
        raise StorageError(f"Source and target backend are the same ({from_backend}).")
    if not any(name.endswith(ext) for ext in _ASSET_EXTENSIONS):
        raise StorageError(
            f"Unsupported file type for '{name}'. "
            f"Allowed extensions: {', '.join(sorted(_ASSET_EXTENSIONS))}"
        )
    # Copy first; only delete the source if the copy succeeded so a failure
    # mid-flight doesn't lose data.
    try:
        data = await asyncio.to_thread(_BACKEND_OPS[from_backend]["fetch"], name)
    except Exception as e:
        raise StorageError(f"Failed to read '{name}' from {from_backend}: {e}") from e
    try:
        await asyncio.to_thread(_BACKEND_OPS[to_backend]["upload"], name, data)
    except Exception as e:
        raise StorageError(f"Failed to write '{name}' to {to_backend}: {e}") from e
    try:
        await asyncio.to_thread(_BACKEND_OPS[from_backend]["delete"], name)
    except Exception as e:
        # Copy succeeded but delete didn't — the asset is now duplicated. Surface
        # the warning rather than failing the whole operation so the user can
        # manually clean up the source.
        raise StorageError(
            f"Asset copied to {to_backend} but source delete from {from_backend} "
            f"failed: {e}. The asset is now present on both backends; remove the "
            f"source copy manually if needed."
        ) from e


async def test_backend(backend: str) -> dict:
    """Probe a backend for reachability — list assets and report success/error.
    Used by /api/storage/test for the page's "Test connection" button."""
    _validate_backend(backend)
    try:
        items = await asyncio.to_thread(_BACKEND_OPS[backend]["list"])
        return {"ok": True, "count": len(items)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Image-path I/O ───────────────────────────────────────────────────────────
#
# Distinct from the asset functions above for three reasons:
#   1. Asset I/O is gated by _ASSET_EXTENSIONS (playbooks/scripts/packages).
#      Image blobs use VM disk formats (.vhd, .raw, .tar.gz, ...) which aren't
#      on that list and have a deliberately separate allowlist.
#   2. Image transfers can be multi-GB. These helpers stream through SDK-
#      native multipart / chunked I/O so callers don't have to load the
#      whole blob into a Python bytes object.
#   3. Callers provide full keys (no _<backend>_prefix() mangling). The
#      image-registry artefact URL is the source of truth for paths and the
#      registry already stores them as full-bucket-key strings.
#
# Used by the hub-backed image registry (cross-backend artefact copy + the
# per-target-cloud promote runners).

_IMAGE_EXTENSIONS = {".vhd", ".raw", ".tar.gz", ".vmdk", ".qcow2"}


def _is_image_filename(name: str) -> bool:
    """True if `name` ends with a supported VM disk-image extension. Handles
    multi-dot extensions like `.tar.gz` (which a naive rsplit doesn't)."""
    lower = (name or "").lower()
    return any(lower.endswith(ext) for ext in _IMAGE_EXTENSIONS)


def image_key(backend: str, image_name: str, ext: str = "vhd", ts: Optional[str] = None) -> str:
    """Build the canonical full key for an image artefact on `backend`.

    S3 prepends `storage_s3_prefix` because the same bucket is also the asset
    store; Azure and GCS use the configured container/bucket directly so just
    the `images/` sub-prefix is enough. `ts` is auto-generated if omitted so
    repeated builds of the same image name produce distinct keys.
    """
    if ts is None:
        from datetime import datetime
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    blob = f"images/{image_name}-{ts}.{ext}"
    if backend == "s3":
        prefix = (_cfg("storage_s3_prefix") or "config-mgmt").rstrip("/")
        return f"{prefix}/{blob}" if prefix else blob
    return blob


def image_url(backend: str, key: str) -> str:
    """Canonical URL for an image-path key on `backend`. Used to populate
    `RegisteredImage.artefact_url` — the same shape the cloud SDKs accept
    as an import source."""
    if backend == "s3":
        return f"s3://{_cfg('storage_s3_bucket')}/{key}"
    if backend == "azure_blob":
        account = _cfg("storage_azure_account")
        container = _cfg("storage_azure_container") or "playbooks"
        return f"https://{account}.blob.core.windows.net/{container}/{key}"
    if backend == "gcs":
        return f"gs://{_cfg('storage_gcs_bucket')}/{key}"
    if backend == "local":
        base = _cfg("storage_local_path").rstrip("/").rstrip("\\")
        return f"file://{base}/{key.lstrip('/')}"
    raise StorageError(f"Unknown backend '{backend}'")


# ── Per-backend image sync helpers ───────────────────────────────────────────

def _s3_upload_image_sync(key: str, fileobj) -> None:
    bucket = _cfg("storage_s3_bucket")
    client = _s3_client()
    # boto3.upload_fileobj auto-switches to multipart at ~8 MiB so this stays
    # bounded in memory regardless of the source size.
    client.upload_fileobj(fileobj, bucket, key)


def _s3_download_image_sync(key: str, fileobj) -> None:
    bucket = _cfg("storage_s3_bucket")
    client = _s3_client()
    # Symmetric to upload_fileobj — streams to the given writable file-like.
    client.download_fileobj(bucket, key, fileobj)


def _s3_delete_image_sync(key: str) -> None:
    bucket = _cfg("storage_s3_bucket")
    client = _s3_client()
    client.delete_object(Bucket=bucket, Key=key)


def _s3_head_image_sync(key: str) -> Optional[dict]:
    from botocore.exceptions import ClientError
    bucket = _cfg("storage_s3_bucket")
    client = _s3_client()
    try:
        resp = client.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    last_modified = resp.get("LastModified")
    return {
        "size": resp.get("ContentLength", 0),
        "etag": (resp.get("ETag") or "").strip('"'),
        "content_type": resp.get("ContentType", ""),
        "last_modified": last_modified.isoformat() if last_modified else None,
    }


def _s3_copy_same_sync(src_key: str, dst_key: str) -> None:
    bucket = _cfg("storage_s3_bucket")
    client = _s3_client()
    # Server-side copy within the same bucket — no bytes transit through this
    # process and the operation works for any object size.
    client.copy_object(Bucket=bucket, Key=dst_key, CopySource={"Bucket": bucket, "Key": src_key})


def _s3_presigned_url_sync(key: str, expiry_seconds: int, method: str) -> str:
    bucket = _cfg("storage_s3_bucket")
    client = _s3_client()
    aws_op = {"GET": "get_object", "PUT": "put_object"}.get(method.upper())
    if not aws_op:
        raise StorageError(f"Unsupported presigned URL method '{method}' for S3.")
    return client.generate_presigned_url(
        aws_op,
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expiry_seconds,
    )


def _azure_upload_image_sync(key: str, fileobj) -> None:
    svc = _azure_blob_client()
    blob_client = svc.get_blob_client(container=_azure_container(), blob=key)
    blob_client.upload_blob(fileobj, overwrite=True)


def _azure_download_image_sync(key: str, fileobj) -> None:
    svc = _azure_blob_client()
    blob_client = svc.get_blob_client(container=_azure_container(), blob=key)
    downloader = blob_client.download_blob()
    downloader.readinto(fileobj)


def _azure_delete_image_sync(key: str) -> None:
    svc = _azure_blob_client()
    blob_client = svc.get_blob_client(container=_azure_container(), blob=key)
    blob_client.delete_blob()


def _azure_head_image_sync(key: str) -> Optional[dict]:
    from azure.core.exceptions import ResourceNotFoundError
    svc = _azure_blob_client()
    blob_client = svc.get_blob_client(container=_azure_container(), blob=key)
    try:
        props = blob_client.get_blob_properties()
    except ResourceNotFoundError:
        return None
    return {
        "size": props.size,
        "etag": (props.etag or "").strip('"'),
        "content_type": (props.content_settings.content_type if props.content_settings else "") or "",
        "last_modified": props.last_modified.isoformat() if props.last_modified else None,
    }


def _azure_copy_same_sync(src_key: str, dst_key: str) -> None:
    # Azure server-side copy: ask the destination blob to pull from a URL
    # pointing at the source blob in the same account. The fastest path uses
    # a same-account URL with a short-lived SAS; user-delegation key works
    # with the AAD credential we already have.
    from datetime import datetime, timedelta, timezone
    from azure.storage.blob import BlobSasPermissions, generate_blob_sas
    svc = _azure_blob_client()
    account = _cfg("storage_azure_account")
    container = _azure_container()
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    udk = svc.get_user_delegation_key(start, expiry)
    sas = generate_blob_sas(
        account_name=account,
        container_name=container,
        blob_name=src_key,
        user_delegation_key=udk,
        permission=BlobSasPermissions(read=True),
        expiry=expiry,
        start=start,
    )
    src_url = f"https://{account}.blob.core.windows.net/{container}/{src_key}?{sas}"
    dst_client = svc.get_blob_client(container=container, blob=dst_key)
    dst_client.start_copy_from_url(src_url)


def _azure_presigned_url_sync(key: str, expiry_seconds: int, method: str) -> str:
    from datetime import datetime, timedelta, timezone
    from azure.storage.blob import BlobSasPermissions, generate_blob_sas
    svc = _azure_blob_client()
    account = _cfg("storage_azure_account")
    container = _azure_container()
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    expiry = datetime.now(timezone.utc) + timedelta(seconds=expiry_seconds)
    # AAD credential → must mint a user-delegation key (no account key needed).
    udk = svc.get_user_delegation_key(start, expiry)
    m = method.upper()
    if m == "GET":
        perms = BlobSasPermissions(read=True)
    elif m == "PUT":
        perms = BlobSasPermissions(write=True, create=True)
    else:
        raise StorageError(f"Unsupported presigned URL method '{method}' for Azure Blob.")
    sas = generate_blob_sas(
        account_name=account,
        container_name=container,
        blob_name=key,
        user_delegation_key=udk,
        permission=perms,
        expiry=expiry,
        start=start,
    )
    return f"https://{account}.blob.core.windows.net/{container}/{key}?{sas}"


def _gcs_upload_image_sync(key: str, fileobj) -> None:
    client = _gcs_client()
    bucket = client.bucket(_cfg("storage_gcs_bucket"))
    blob = bucket.blob(key)
    # upload_from_file streams in chunks for files > resumable threshold
    # (8 MiB by default). rewind() to make this re-runnable on a SpooledFile.
    if hasattr(fileobj, "seek"):
        try:
            fileobj.seek(0)
        except OSError:
            pass
    blob.upload_from_file(fileobj, rewind=False)


def _gcs_download_image_sync(key: str, fileobj) -> None:
    client = _gcs_client()
    bucket = client.bucket(_cfg("storage_gcs_bucket"))
    blob = bucket.blob(key)
    blob.download_to_file(fileobj)


def _gcs_delete_image_sync(key: str) -> None:
    client = _gcs_client()
    bucket = client.bucket(_cfg("storage_gcs_bucket"))
    blob = bucket.blob(key)
    blob.delete()


def _gcs_head_image_sync(key: str) -> Optional[dict]:
    from google.cloud.exceptions import NotFound
    client = _gcs_client()
    bucket = client.bucket(_cfg("storage_gcs_bucket"))
    blob = bucket.blob(key)
    try:
        blob.reload()
    except NotFound:
        return None
    return {
        "size": blob.size or 0,
        "etag": (blob.etag or "").strip('"'),
        "content_type": blob.content_type or "",
        "last_modified": blob.updated.isoformat() if blob.updated else None,
    }


def _gcs_copy_same_sync(src_key: str, dst_key: str) -> None:
    client = _gcs_client()
    bucket = client.bucket(_cfg("storage_gcs_bucket"))
    src_blob = bucket.blob(src_key)
    # rewrite() is GCS's server-side copy primitive — handles any object size
    # without bytes flowing through this process.
    new_blob = bucket.blob(dst_key)
    token = None
    while True:
        token, _written, _total = new_blob.rewrite(src_blob, token=token)
        if token is None:
            break


def _gcs_presigned_url_sync(key: str, expiry_seconds: int, method: str) -> str:
    from datetime import timedelta
    client = _gcs_client()
    bucket = client.bucket(_cfg("storage_gcs_bucket"))
    blob = bucket.blob(key)
    m = method.upper()
    if m not in ("GET", "PUT"):
        raise StorageError(f"Unsupported presigned URL method '{method}' for GCS.")
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=expiry_seconds),
        method=m,
    )


def _local_upload_image_sync(key: str, fileobj) -> None:
    target = _cfg("storage_local_path").rstrip("/").rstrip("\\") + "/" + key.lstrip("/")
    if _is_unc(target):
        import smbclient
        _local_smb_register()
        with smbclient.open_file(target, mode="wb") as f:
            while True:
                chunk = fileobj.read(8 * 1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    else:
        import os
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "wb") as f:
            while True:
                chunk = fileobj.read(8 * 1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)


def _local_download_image_sync(key: str, fileobj) -> None:
    target = _cfg("storage_local_path").rstrip("/").rstrip("\\") + "/" + key.lstrip("/")
    if _is_unc(target):
        import smbclient
        _local_smb_register()
        with smbclient.open_file(target, mode="rb") as f:
            while True:
                chunk = f.read(8 * 1024 * 1024)
                if not chunk:
                    break
                fileobj.write(chunk)
    else:
        with open(target, "rb") as f:
            while True:
                chunk = f.read(8 * 1024 * 1024)
                if not chunk:
                    break
                fileobj.write(chunk)


def _local_delete_image_sync(key: str) -> None:
    target = _cfg("storage_local_path").rstrip("/").rstrip("\\") + "/" + key.lstrip("/")
    if _is_unc(target):
        import smbclient
        _local_smb_register()
        try:
            smbclient.remove(target)
        except Exception:
            pass
    else:
        import os
        try:
            os.remove(target)
        except FileNotFoundError:
            pass


def _local_head_image_sync(key: str) -> Optional[dict]:
    target = _cfg("storage_local_path").rstrip("/").rstrip("\\") + "/" + key.lstrip("/")
    if _is_unc(target):
        import smbclient
        _local_smb_register()
        try:
            st = smbclient.stat(target)
        except Exception:
            return None
        return {"size": st.st_size, "etag": "", "content_type": "", "last_modified": None}
    else:
        import os
        try:
            st = os.stat(target)
        except FileNotFoundError:
            return None
        from datetime import datetime, timezone
        return {
            "size": st.st_size,
            "etag": "",
            "content_type": "",
            "last_modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        }


def _local_copy_same_sync(src_key: str, dst_key: str) -> None:
    # No server-side primitive — copy via a small buffer.
    import io
    buf = io.BytesIO()
    _local_download_image_sync(src_key, buf)
    buf.seek(0)
    _local_upload_image_sync(dst_key, buf)


def _local_presigned_url_unsupported(*_args, **_kwargs):
    raise StorageError(
        "Local/SMB backend doesn't support presigned URLs — cloud SDKs need an "
        "HTTPS URL they can pull from. Use s3/azure_blob/gcs for promote artefacts."
    )


_IMAGE_OPS = {
    "s3": {
        "upload":   _s3_upload_image_sync,
        "download": _s3_download_image_sync,
        "delete":   _s3_delete_image_sync,
        "head":     _s3_head_image_sync,
        "copy":     _s3_copy_same_sync,
        "presign":  _s3_presigned_url_sync,
    },
    "azure_blob": {
        "upload":   _azure_upload_image_sync,
        "download": _azure_download_image_sync,
        "delete":   _azure_delete_image_sync,
        "head":     _azure_head_image_sync,
        "copy":     _azure_copy_same_sync,
        "presign":  _azure_presigned_url_sync,
    },
    "gcs": {
        "upload":   _gcs_upload_image_sync,
        "download": _gcs_download_image_sync,
        "delete":   _gcs_delete_image_sync,
        "head":     _gcs_head_image_sync,
        "copy":     _gcs_copy_same_sync,
        "presign":  _gcs_presigned_url_sync,
    },
    "local": {
        "upload":   _local_upload_image_sync,
        "download": _local_download_image_sync,
        "delete":   _local_delete_image_sync,
        "head":     _local_head_image_sync,
        "copy":     _local_copy_same_sync,
        "presign":  _local_presigned_url_unsupported,
    },
}


# ── Public image-path API ────────────────────────────────────────────────────

async def upload_image_to(backend: str, key: str, fileobj) -> None:
    """Stream `fileobj` into `backend` at the full key `key`. The fileobj must
    be a binary, seekable file-like (open("rb"), io.BytesIO, etc). Multi-GB
    safe — each backend uses its SDK's chunked/multipart path."""
    _validate_backend(backend)
    if not _is_image_filename(key):
        raise StorageError(
            f"'{key}' isn't a supported image format. Allowed extensions: "
            f"{', '.join(sorted(_IMAGE_EXTENSIONS))}."
        )
    try:
        await asyncio.to_thread(_IMAGE_OPS[backend]["upload"], key, fileobj)
    except StorageError:
        raise
    except Exception as e:
        raise StorageError(f"Failed to upload image '{key}' to {backend}: {e}") from e


async def download_image_to(backend: str, key: str, fileobj) -> None:
    """Stream the image at `key` from `backend` into the writable binary
    fileobj. Multi-GB safe."""
    _validate_backend(backend)
    try:
        await asyncio.to_thread(_IMAGE_OPS[backend]["download"], key, fileobj)
    except StorageError:
        raise
    except Exception as e:
        raise StorageError(f"Failed to download image '{key}' from {backend}: {e}") from e


async def delete_image_in(backend: str, key: str) -> None:
    """Delete an image-path blob from `backend`. Used by the promote flow to
    clean up the target-cloud staged copy after a successful cloud-side
    import."""
    _validate_backend(backend)
    try:
        await asyncio.to_thread(_IMAGE_OPS[backend]["delete"], key)
    except StorageError:
        raise
    except Exception as e:
        raise StorageError(f"Failed to delete image '{key}' from {backend}: {e}") from e


async def head_image_in(backend: str, key: str) -> Optional[dict]:
    """Return `{size, etag, content_type, last_modified}` for the blob at
    `key`, or None if it doesn't exist. Used to verify a copy succeeded
    before relying on the dest blob."""
    _validate_backend(backend)
    try:
        return await asyncio.to_thread(_IMAGE_OPS[backend]["head"], key)
    except StorageError:
        raise
    except Exception as e:
        raise StorageError(f"Failed to head image '{key}' on {backend}: {e}") from e


async def presigned_url(
    backend: str,
    key: str,
    expiry_seconds: int = 3600,
    method: str = "GET",
) -> str:
    """Mint a short-lived HTTPS URL the cloud SDKs can fetch directly. S3 uses
    a SigV4 presigned URL, Azure a user-delegation SAS, GCS a v4 signed URL.
    `local` is not supported."""
    _validate_backend(backend)
    if expiry_seconds <= 0:
        raise StorageError("presigned_url expiry_seconds must be > 0")
    try:
        return await asyncio.to_thread(_IMAGE_OPS[backend]["presign"], key, expiry_seconds, method)
    except StorageError:
        raise
    except Exception as e:
        raise StorageError(f"Failed to mint presigned URL for {backend}/{key}: {e}") from e


async def copy(src_backend: str, src_key: str, dst_backend: str, dst_key: str) -> None:
    """Copy an image-path blob. Same-backend uses the SDK's server-side copy
    (S3 CopyObject, Azure start_copy_from_url, GCS rewrite) and is cheap for
    any size. Cross-backend streams through a temp file on disk — fine for
    administrative / small-file moves but the heavy promote-time transfers
    are routed through per-target-cloud runners in later PRs, not this
    function."""
    _validate_backend(src_backend)
    _validate_backend(dst_backend)
    if not _is_image_filename(dst_key):
        raise StorageError(
            f"'{dst_key}' isn't a supported image format. Allowed extensions: "
            f"{', '.join(sorted(_IMAGE_EXTENSIONS))}."
        )

    if src_backend == dst_backend:
        try:
            await asyncio.to_thread(_IMAGE_OPS[src_backend]["copy"], src_key, dst_key)
            return
        except StorageError:
            raise
        except Exception as e:
            raise StorageError(
                f"Failed to copy {src_backend}/{src_key} → {dst_backend}/{dst_key}: {e}"
            ) from e

    # Cross-backend: stream through a temp file so memory stays bounded for
    # multi-GB images. Heavy promote transfers in later PRs use cloud-native
    # runners with presigned URLs; this fallback covers admin moves.
    import os
    import tempfile
    fd, tmp_path = tempfile.mkstemp(prefix="storage_copy_")
    os.close(fd)
    try:
        with open(tmp_path, "wb") as out:
            await asyncio.to_thread(_IMAGE_OPS[src_backend]["download"], src_key, out)
        with open(tmp_path, "rb") as inp:
            await asyncio.to_thread(_IMAGE_OPS[dst_backend]["upload"], dst_key, inp)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
