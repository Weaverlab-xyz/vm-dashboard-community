"""
Ansible asset storage abstraction.

Supports three backends — S3, Azure Blob Storage, and GCS — so any cloud
user can store playbooks and provisioning assets without requiring an AWS
account.

Backend selection is automatic: whichever of the three storage configs is
populated is used. If more than one is set, priority is S3 > Azure Blob > GCS.

Supported asset types:
    .yml / .yaml  — Ansible playbooks (run as-is)
    .sh           — Bash scripts (wrapped in an auto-generated playbook)
    .rpm          — RPM packages   (wrapped: copy + dnf install)
    .deb          — DEB packages   (wrapped: copy + apt install)

Public API:
    list_assets()         → list[dict]  — [{name, type}] sorted by name
    list_playbooks()      → list[str]   — .yml/.yaml names only (back-compat)
    fetch_asset_b64()     → str         — asset bytes, base64-encoded
    fetch_playbook_b64()  → str         — alias for fetch_asset_b64
"""
import asyncio
import base64
import logging

logger = logging.getLogger(__name__)

_ASSET_EXTENSIONS = {".yml", ".yaml", ".sh", ".rpm", ".deb"}
_TYPE_MAP = {".yml": "playbook", ".yaml": "playbook", ".sh": "script", ".rpm": "rpm", ".deb": "deb"}


def _asset_type(name: str) -> str:
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    return _TYPE_MAP.get(ext, "playbook")


class AnsibleStorageError(Exception):
    pass


def _cfg(key: str) -> str:
    from . import config_service
    return config_service.get(key) or ""


def _active_backend() -> str:
    """Return the first configured backend: 's3', 'azure_blob', or 'gcs'."""
    if _cfg("ansible_s3_bucket"):
        return "s3"
    if _cfg("ansible_azure_storage_account"):
        return "azure_blob"
    if _cfg("ansible_gcs_bucket"):
        return "gcs"
    return ""


# ── S3 backend ────────────────────────────────────────────────────────────────

def _s3_client():
    try:
        import boto3  # noqa: F401
    except ImportError:
        raise AnsibleStorageError("boto3 is not installed")
    from .aws_service import _aws_kwargs
    region = _cfg("ansible_s3_region") or _cfg("aws_region")
    import boto3
    return boto3.client("s3", **_aws_kwargs(region))


def _s3_list_sync() -> list[dict]:
    bucket = _cfg("ansible_s3_bucket")
    prefix = (_cfg("ansible_s3_prefix") or "config-mgmt").rstrip("/")
    client = _s3_client()
    paginator = client.get_paginator("list_objects_v2")
    assets = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix + "/"):
        for obj in page.get("Contents", []):
            key = obj["Key"][len(prefix) + 1:]  # strip "prefix/"
            if key and any(key.endswith(ext) for ext in _ASSET_EXTENSIONS):
                assets.append({"name": key, "type": _asset_type(key)})
    return sorted(assets, key=lambda x: x["name"])


def _s3_fetch_sync(name: str) -> bytes:
    bucket = _cfg("ansible_s3_bucket")
    prefix = (_cfg("ansible_s3_prefix") or "config-mgmt").rstrip("/")
    client = _s3_client()
    resp = client.get_object(Bucket=bucket, Key=f"{prefix}/{name}")
    return resp["Body"].read()


# ── Azure Blob Storage backend ────────────────────────────────────────────────

def _azure_blob_client():
    try:
        from azure.storage.blob import BlobServiceClient  # noqa: F401
    except ImportError:
        raise AnsibleStorageError("azure-storage-blob or azure-identity is not installed")
    from azure.identity import ClientSecretCredential
    from azure.storage.blob import BlobServiceClient
    account = _cfg("ansible_azure_storage_account")
    cred = ClientSecretCredential(
        tenant_id=_cfg("azure_tenant_id"),
        client_id=_cfg("azure_client_id"),
        client_secret=_cfg("azure_client_secret"),
    )
    return BlobServiceClient(account_url=f"https://{account}.blob.core.windows.net", credential=cred)


def _azure_list_sync() -> list[dict]:
    svc = _azure_blob_client()
    container = _cfg("ansible_azure_container") or "playbooks"
    prefix = (_cfg("ansible_azure_prefix") or "config-mgmt").rstrip("/")
    cc = svc.get_container_client(container)
    assets = []
    for blob in cc.list_blobs(name_starts_with=prefix + "/"):
        name = blob.name[len(prefix) + 1:]
        if name and any(name.endswith(ext) for ext in _ASSET_EXTENSIONS):
            assets.append({"name": name, "type": _asset_type(name)})
    return sorted(assets, key=lambda x: x["name"])


def _azure_fetch_sync(name: str) -> bytes:
    svc = _azure_blob_client()
    container = _cfg("ansible_azure_container") or "playbooks"
    prefix = (_cfg("ansible_azure_prefix") or "config-mgmt").rstrip("/")
    blob_client = svc.get_blob_client(container=container, blob=f"{prefix}/{name}")
    return blob_client.download_blob().readall()


# ── GCS backend ───────────────────────────────────────────────────────────────

def _gcs_client():
    try:
        from google.cloud import storage as gcs  # noqa: F401
    except ImportError:
        raise AnsibleStorageError("google-cloud-storage is not installed")
    from .gcp_service import _gcp_creds
    from google.cloud import storage as gcs
    return gcs.Client(credentials=_gcp_creds(), project=_cfg("gcp_project_id"))


def _gcs_list_sync() -> list[dict]:
    client = _gcs_client()
    bucket_name = _cfg("ansible_gcs_bucket")
    prefix = (_cfg("ansible_gcs_prefix") or "config-mgmt").rstrip("/")
    assets = []
    for blob in client.list_blobs(bucket_name, prefix=prefix + "/"):
        name = blob.name[len(prefix) + 1:]
        if name and any(name.endswith(ext) for ext in _ASSET_EXTENSIONS):
            assets.append({"name": name, "type": _asset_type(name)})
    return sorted(assets, key=lambda x: x["name"])


def _gcs_fetch_sync(name: str) -> bytes:
    client = _gcs_client()
    bucket_name = _cfg("ansible_gcs_bucket")
    prefix = (_cfg("ansible_gcs_prefix") or "config-mgmt").rstrip("/")
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(f"{prefix}/{name}")
    return blob.download_as_bytes()


# ── Public API ────────────────────────────────────────────────────────────────

async def list_assets() -> list[dict]:
    """List all assets (.yml, .sh, .deb, .rpm) from the configured storage backend."""
    backend = _active_backend()
    if not backend:
        raise AnsibleStorageError(
            "No asset storage configured. Set ANSIBLE_S3_BUCKET, "
            "ANSIBLE_AZURE_STORAGE_ACCOUNT, or ANSIBLE_GCS_BUCKET."
        )
    try:
        if backend == "s3":
            return await asyncio.to_thread(_s3_list_sync)
        elif backend == "azure_blob":
            return await asyncio.to_thread(_azure_list_sync)
        else:
            return await asyncio.to_thread(_gcs_list_sync)
    except AnsibleStorageError:
        raise
    except Exception as e:
        raise AnsibleStorageError(f"Failed to list assets from {backend}: {e}") from e


async def list_playbooks() -> list[str]:
    """List playbook names (.yml/.yaml) only — back-compat alias."""
    assets = await list_assets()
    return [a["name"] for a in assets if a["type"] == "playbook"]


async def fetch_asset_b64(name: str) -> str:
    """Download an asset by name and return it base64-encoded."""
    backend = _active_backend()
    if not backend:
        raise AnsibleStorageError("No asset storage configured.")
    try:
        if backend == "s3":
            data = await asyncio.to_thread(_s3_fetch_sync, name)
        elif backend == "azure_blob":
            data = await asyncio.to_thread(_azure_fetch_sync, name)
        else:
            data = await asyncio.to_thread(_gcs_fetch_sync, name)
        return base64.b64encode(data).decode()
    except AnsibleStorageError:
        raise
    except Exception as e:
        raise AnsibleStorageError(f"Failed to fetch asset '{name}' from {backend}: {e}") from e


async def fetch_playbook_b64(name: str) -> str:
    """Alias for fetch_asset_b64 — kept for back-compat."""
    return await fetch_asset_b64(name)
