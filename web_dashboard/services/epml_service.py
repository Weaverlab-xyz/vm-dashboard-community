"""
BeyondTrust EPM for Linux (EPM-L) SaaS API service.

PAT is read from config_service (DB-encrypted) with env var fallback.
Authenticates via Bearer PAT against https://app.beyondtrust.io.
"""
import asyncio
import logging
import time
from typing import Any

import boto3
import httpx

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 10
_DEFAULT_TIMEOUT = 900  # 15 minutes


class EpmlError(Exception):
    pass


def _get_pat() -> str:
    from ..services import config_service
    pat = config_service.get("epml_pat")
    if not pat:
        from ..config import settings
        pat = getattr(settings, "epml_pat", "")
    if not pat:
        raise EpmlError(
            "EPM-L PAT is not configured. "
            "Go to Settings → BeyondTrust to set the Personal Access Token."
        )
    return pat


def _get_base_url() -> str:
    from ..config import settings
    return getattr(settings, "epml_base_url", "https://app.beyondtrust.io").rstrip("/")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=_get_base_url(),
        headers={
            "Authorization": f"Bearer {_get_pat()}",
            "Accept": "application/json",
        },
        timeout=30.0,
    )


def _extract_packages(data: Any) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("packages", "files", "items", "data", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _extract_build_status(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("status", "buildStatus", "state", "Status"):
            val = data.get(key)
            if val is not None:
                return str(val).lower()
    if isinstance(data, str):
        return data.lower()
    return "unknown"


def _extract_token(data: Any) -> str:
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        for key in ("token", "installationToken", "activationToken", "value", "Token"):
            val = data.get(key)
            if val:
                return str(val).strip()
    return ""


def _get_package_download_url(pkg: dict) -> str:
    for key in ("url", "downloadUrl", "download_url", "href", "link", "presigned_url", "presignedUrl"):
        val = pkg.get(key)
        if val:
            return str(val)
    return ""


def _get_package_filename(pkg: dict) -> str:
    for key in ("filename", "name", "fileName", "file_name"):
        val = pkg.get(key)
        if val:
            return str(val)
    return ""


def _upload_bytes_to_s3(region: str, bucket: str, key: str, data: bytes) -> None:
    s3 = boto3.client("s3", region_name=region)
    s3.put_object(Bucket=bucket, Key=key, Body=data)


async def list_packages() -> list[dict]:
    async with _client() as c:
        resp = await c.get("/api/epml/clientpkg")
        if resp.status_code not in (200, 204):
            raise EpmlError(f"list_packages failed: HTTP {resp.status_code} — {resp.text[:400]}")
        if not resp.content:
            return []
        return _extract_packages(resp.json())


async def trigger_build() -> dict:
    async with _client() as c:
        resp = await c.post("/api/epml/clientpkg")
        if resp.status_code not in (200, 201, 202, 204):
            raise EpmlError(f"trigger_build failed: HTTP {resp.status_code} — {resp.text[:400]}")
        if not resp.content:
            return {}
        return resp.json()


async def get_build_status() -> dict:
    async with _client() as c:
        resp = await c.get("/api/epml/clientpkg/status")
        if resp.status_code not in (200, 204):
            raise EpmlError(f"get_build_status failed: HTTP {resp.status_code} — {resp.text[:400]}")
        if not resp.content:
            return {}
        return resp.json()


async def ensure_packages(timeout: int = _DEFAULT_TIMEOUT) -> list[dict]:
    pkgs = await list_packages()
    if pkgs:
        logger.info("EPM-L packages already available: %d packages", len(pkgs))
        return pkgs

    logger.info("No EPM-L packages found — triggering build")
    await trigger_build()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(_POLL_INTERVAL)
        try:
            status_data = await get_build_status()
            status = _extract_build_status(status_data)
            logger.debug("EPM-L build status: %s", status)
            if status in ("complete", "completed", "done", "success", "succeeded", "ready"):
                break
            if status in ("failed", "error", "cancelled"):
                raise EpmlError(f"EPM-L package build failed with status: {status}")
        except EpmlError:
            raise
        except Exception as exc:
            logger.warning("Error polling EPM-L build status: %s", exc)

    pkgs = await list_packages()
    if not pkgs:
        raise EpmlError("EPM-L build completed but no packages are available yet.")
    return pkgs


async def get_installation_token(expiry_minutes: int = 480) -> str:
    expiry = max(30, min(525600, expiry_minutes))
    async with _client() as c:
        resp = await c.get("/api/btplatform/installationtoken", params={"expiry": expiry})
        if resp.status_code not in (200, 201):
            raise EpmlError(f"get_installation_token failed: HTTP {resp.status_code} — {resp.text[:400]}")
        token = _extract_token(resp.json() if resp.content else "")
        if not token:
            raise EpmlError("BeyondTrust returned an empty installation token. Check your PAT permissions.")
        return token


async def download_package(url: str) -> bytes:
    async with _client() as c:
        resp = await c.get(url, follow_redirects=True)
        if resp.status_code != 200:
            raise EpmlError(f"Package download failed: HTTP {resp.status_code}")
        return resp.content


async def sync_packages_to_s3() -> dict:
    from ..services import config_service
    from ..config import settings

    pkgs = await ensure_packages()
    uploaded_rpm = False
    uploaded_deb = False

    for pkg in pkgs:
        filename = _get_package_filename(pkg)
        url = _get_package_download_url(pkg)
        if not filename or not url:
            logger.warning("EPM-L package entry missing filename or url: %s", pkg)
            continue

        is_rpm = filename.lower().endswith(".rpm")
        is_deb = filename.lower().endswith(".deb")
        if not (is_rpm or is_deb):
            continue

        logger.info("Downloading EPM-L package: %s", filename)
        data = await download_package(url)

        prefix = config_service.get("ansible_s3_prefix") or settings.ansible_s3_prefix
        bucket = config_service.get("ansible_s3_bucket") or settings.ansible_s3_bucket
        if not bucket:
            raise EpmlError("ansible_s3_bucket is not configured — cannot upload to S3.")

        region = (config_service.get("ansible_s3_region") or settings.ansible_s3_region
                  or settings.aws_region)
        s3_key = f"{prefix}/{filename}"
        await asyncio.to_thread(_upload_bytes_to_s3, region, bucket, s3_key, data)
        logger.info("Uploaded %s to s3://%s/%s", filename, bucket, s3_key)

        if is_rpm:
            uploaded_rpm = True
        elif is_deb:
            uploaded_deb = True

    return {"rpm_uploaded": uploaded_rpm, "deb_uploaded": uploaded_deb, "packages": pkgs}
