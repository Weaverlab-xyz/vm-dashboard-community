"""
Ansible playbook storage abstraction.

Supports three backends — S3, Azure Blob Storage, and GCS — so any cloud
user can store playbooks without requiring an AWS account.

Backend selection is automatic: whichever of the three storage configs is
populated is used. If more than one is set, priority is S3 > Azure Blob > GCS.

Public API:
    list_playbooks()       → list[str]  — playbook names relative to the prefix
    fetch_playbook_b64()   → str        — playbook bytes, base64-encoded
"""
import asyncio
import base64
import logging

logger = logging.getLogger(__name__)


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
        import boto3
    except ImportError:
        raise AnsibleStorageError("boto3 is not installed")
    from .aws_service import _aws_kwargs
    region = _cfg("ansible_s3_region") or _cfg("aws_region")
    return boto3.client("s3", **_aws_kwargs(region))


def _s3_list_sync() -> list[str]:
    bucket = _cfg("ansible_s3_bucket")
    prefix = (_cfg("ansible_s3_prefix") or "config-mgmt").rstrip("/")
    client = _s3_client()
    paginator = client.get_paginator("list_objects_v2")
    names = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix + "/"):
        for obj in page.get("Contents", []):
            key = obj["Key"][len(prefix) + 1:]  # strip "prefix/"
            if key and (key.endswith(".yml") or key.endswith(".yaml")):
                names.append(key)
    return sorted(names)


def _s3_fetch_sync(name: str) -> bytes:
    bucket = _cfg("ansible_s3_bucket")
    prefix = (_cfg("ansible_s3_prefix") or "config-mgmt").rstrip("/")
    client = _s3_client()
    resp = client.get_object(Bucket=bucket, Key=f"{prefix}/{name}")
    return resp["Body"].read()


# ── Azure Blob Storage backend ────────────────────────────────────────────────

def _azure_blob_client():
    try:
        from azure.identity import ClientSecretCredential
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        raise AnsibleStorageError("azure-storage-blob or azure-identity is not installed")
    account = _cfg("ansible_azure_storage_account")
    cred = ClientSecretCredential(
        tenant_id=_cfg("azure_tenant_id"),
        client_id=_cfg("azure_client_id"),
        client_secret=_cfg("azure_client_secret"),
    )
    from azure.storage.blob import BlobServiceClient as _BSC
    return _BSC(account_url=f"https://{account}.blob.core.windows.net", credential=cred)


def _azure_list_sync() -> list[str]:
    svc = _azure_blob_client()
    container = _cfg("ansible_azure_container") or "playbooks"
    prefix = (_cfg("ansible_azure_prefix") or "config-mgmt").rstrip("/")
    cc = svc.get_container_client(container)
    names = []
    for blob in cc.list_blobs(name_starts_with=prefix + "/"):
        name = blob.name[len(prefix) + 1:]
        if name and (name.endswith(".yml") or name.endswith(".yaml")):
            names.append(name)
    return sorted(names)


def _azure_fetch_sync(name: str) -> bytes:
    svc = _azure_blob_client()
    container = _cfg("ansible_azure_container") or "playbooks"
    prefix = (_cfg("ansible_azure_prefix") or "config-mgmt").rstrip("/")
    blob_client = svc.get_blob_client(container=container, blob=f"{prefix}/{name}")
    return blob_client.download_blob().readall()


# ── GCS backend ───────────────────────────────────────────────────────────────

def _gcs_client():
    try:
        from google.cloud import storage as gcs
    except ImportError:
        raise AnsibleStorageError("google-cloud-storage is not installed")
    from .gcp_service import _gcp_creds
    from google.cloud import storage as gcs
    return gcs.Client(credentials=_gcp_creds(), project=_cfg("gcp_project_id"))


def _gcs_list_sync() -> list[str]:
    client = _gcs_client()
    bucket_name = _cfg("ansible_gcs_bucket")
    prefix = (_cfg("ansible_gcs_prefix") or "config-mgmt").rstrip("/")
    names = []
    for blob in client.list_blobs(bucket_name, prefix=prefix + "/"):
        name = blob.name[len(prefix) + 1:]
        if name and (name.endswith(".yml") or name.endswith(".yaml")):
            names.append(name)
    return sorted(names)


def _gcs_fetch_sync(name: str) -> bytes:
    client = _gcs_client()
    bucket_name = _cfg("ansible_gcs_bucket")
    prefix = (_cfg("ansible_gcs_prefix") or "config-mgmt").rstrip("/")
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(f"{prefix}/{name}")
    return blob.download_as_bytes()


# ── Public API ────────────────────────────────────────────────────────────────

async def list_playbooks() -> list[str]:
    """List playbook names from the configured storage backend."""
    backend = _active_backend()
    if not backend:
        raise AnsibleStorageError(
            "No playbook storage configured. Set ANSIBLE_S3_BUCKET, "
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
        raise AnsibleStorageError(f"Failed to list playbooks from {backend}: {e}") from e


async def fetch_playbook_b64(name: str) -> str:
    """Download a playbook by name and return it base64-encoded."""
    backend = _active_backend()
    if not backend:
        raise AnsibleStorageError("No playbook storage configured.")
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
        raise AnsibleStorageError(f"Failed to fetch playbook '{name}' from {backend}: {e}") from e
