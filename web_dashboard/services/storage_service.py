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

_ASSET_EXTENSIONS = {".yml", ".yaml", ".sh", ".rpm", ".deb"}
_TYPE_MAP = {".yml": "playbook", ".yaml": "playbook", ".sh": "script", ".rpm": "rpm", ".deb": "deb"}

BACKENDS = ("s3", "azure_blob", "gcs")


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


# ── Backend dispatch table ────────────────────────────────────────────────────

_BACKEND_OPS = {
    "s3":         {"list": _s3_list_sync,    "fetch": _s3_fetch_sync,    "upload": _s3_upload_sync,    "delete": _s3_delete_sync},
    "azure_blob": {"list": _azure_list_sync, "fetch": _azure_fetch_sync, "upload": _azure_upload_sync, "delete": _azure_delete_sync},
    "gcs":        {"list": _gcs_list_sync,   "fetch": _gcs_fetch_sync,   "upload": _gcs_upload_sync,   "delete": _gcs_delete_sync},
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


async def test_backend(backend: str) -> dict:
    """Probe a backend for reachability — list assets and report success/error.
    Used by /api/storage/test for the page's "Test connection" button."""
    _validate_backend(backend)
    try:
        items = await asyncio.to_thread(_BACKEND_OPS[backend]["list"])
        return {"ok": True, "count": len(items)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
