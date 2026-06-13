"""
BeyondTrust EPM for Linux (EPM-L) SaaS API service.

Talks to the Pathfinder public API gateway. The EPM-L OpenAPI spec writes
paths as /api/<rest> with an empty servers block; the deployed gateway
serves them at:

    https://api.beyondtrust.io/site/<site-id>/epm/linux/<rest>

(the /api prefix is replaced by the site+product base — see _get_api_base).
Auth is `Authorization: Bearer <PAT>` with a Pathfinder Personal Access
Token (PAT_ prefix, no token exchange). PATs are bound to the site that was
active when they were created, so the configured site id must match.

PAT and site id are read from config_service (DB-encrypted) with .env
fallback (EPML_PAT / EPML_SITE_ID / EPML_BASE_URL). Package download links
are pre-signed S3 URLs (~30 min) fetched WITHOUT the Authorization header.
sync_packages_to_storage() uploads packages via ansible_storage — whichever
backend is configured (S3, Azure Blob Storage, or GCS).
"""
import asyncio
import logging
import time
from typing import Any

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
        pat = settings.epml_pat
    if not pat:
        raise EpmlError(
            "EPM-L PAT is not configured. "
            "Go to Settings → BeyondTrust to set the Personal Access Token."
        )
    return pat


def _sanitize_site_id(raw: str) -> str:
    """Strip the noise a copy-pasted site UUID tends to carry."""
    return raw.strip("\"'{}/ \t\r\n")


def _get_site_id() -> str:
    from ..services import config_service
    site_id = config_service.get("epml_site_id")
    if not site_id:
        from ..config import settings
        site_id = settings.epml_site_id
    site_id = _sanitize_site_id(site_id or "")
    if not site_id:
        raise EpmlError(
            "EPM-L Site ID is not configured. "
            "Go to Settings → BeyondTrust to set the Site ID — open "
            "https://app.beyondtrust.io/api/platform/currentSite while signed "
            "in and copy the site_id field."
        )
    return site_id


def _get_base_url() -> str:
    from ..services import config_service
    from ..config import settings
    base = config_service.get("epml_base_url") or settings.epml_base_url
    return base.rstrip("/")


def _get_api_base() -> str:
    """Gateway base for every EPM-L call.

    spec /api/<rest>  ->  {base}/site/{site_id}/epm/linux/<rest>
    """
    base = _get_base_url()
    if "/site/" in base:
        # Operator pasted a full gateway URL — use it verbatim.
        return base
    return f"{base}/site/{_get_site_id()}/epm/linux"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=_get_api_base(),
        headers={
            "Authorization": f"Bearer {_get_pat()}",
            "Accept": "application/json",
        },
        timeout=30.0,
    )


def _raise_for_api_error(resp: httpx.Response, action: str) -> None:
    """Translate known gateway error responses into actionable messages."""
    status = resp.status_code
    body = resp.text or ""
    lowered = body.lower()
    if status == 401 and "access denied for this site" in lowered:
        raise EpmlError(
            "BeyondTrust rejected the request for this site: the Site ID is "
            "wrong or the PAT was created while a different site was active. "
            "Verify Settings → BeyondTrust → EPM-L Site ID matches the site "
            f"the PAT was created on. (HTTP {status})"
        )
    if status == 401 and "token could not be decoded" in lowered:
        raise EpmlError(
            "The EPM-L PAT is malformed or truncated. Re-paste the full token "
            f"(it starts with PAT_) in Settings → BeyondTrust. (HTTP {status})"
        )
    if status == 401 and "personal access token not found" in lowered:
        raise EpmlError(
            "BeyondTrust does not recognize the PAT yet. Newly created tokens "
            "can take a few seconds to propagate — retry shortly. If it "
            f"persists, the PAT may have been revoked. (HTTP {status})"
        )
    if status == 421:
        raise EpmlError(
            "The BeyondTrust gateway did not recognize the request path. "
            "Check the EPM-L base URL — it should be "
            f"https://api.beyondtrust.io. (HTTP {status})"
        )
    if status == 403 and ("sha-256" in lowered or "key=value" in lowered):
        raise EpmlError(
            "The request reached an AWS-IAM-signed endpoint — the EPM-L base "
            "URL is wrong for PAT auth. It should be "
            f"https://api.beyondtrust.io. (HTTP {status})"
        )
    raise EpmlError(f"{action} failed: HTTP {status} — {body[:400]}")


def _extract_packages(data: Any) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("clientpkg", "packages", "files", "items", "data", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _extract_build_status(data: Any) -> str:
    # Real gateway shape: {"building": true|false} (boolean). Anything else
    # falls through to the legacy string-status handling below.
    if isinstance(data, dict) and isinstance(data.get("building"), bool):
        return "building" if data["building"] else "idle"
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
    for key in ("link", "url", "downloadUrl", "download_url", "href", "presigned_url", "presignedUrl"):
        val = pkg.get(key)
        if val:
            return str(val)
    return ""


def _get_package_filename(pkg: dict) -> str:
    for key in ("file", "filename", "name", "fileName", "file_name"):
        val = pkg.get(key)
        if val:
            return str(val)
    return ""


async def list_packages() -> list[dict]:
    async with _client() as c:
        resp = await c.get("/epml/clientpkg")
        if resp.status_code not in (200, 204):
            _raise_for_api_error(resp, "list_packages")
        if not resp.content:
            return []
        return _extract_packages(resp.json())


async def trigger_build() -> dict:
    async with _client() as c:
        resp = await c.post("/epml/clientpkg")
        if resp.status_code not in (200, 201, 202, 204):
            _raise_for_api_error(resp, "trigger_build")
        if not resp.content:
            return {}
        return resp.json()


async def get_build_status() -> dict:
    async with _client() as c:
        resp = await c.get("/epml/clientpkg/status")
        if resp.status_code not in (200, 204):
            _raise_for_api_error(resp, "get_build_status")
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
            if status in ("failed", "error", "cancelled"):
                raise EpmlError(f"EPM-L package build failed with status: {status}")
            if status in ("idle", "complete", "completed", "done", "success", "succeeded", "ready"):
                # The build queue reports done — but the package list can lag,
                # and "idle" also appears in the gap before a just-triggered
                # build starts. Only finish once packages actually exist.
                pkgs = await list_packages()
                if pkgs:
                    return pkgs
        except EpmlError:
            raise
        except Exception as exc:
            logger.warning("Error polling EPM-L build status: %s", exc)

    pkgs = await list_packages()
    if not pkgs:
        raise EpmlError(
            f"EPM-L build did not produce packages within {timeout // 60} minutes. "
            "Check GET /api/epml/build-status and re-run the sync — it resumes "
            "where the build left off."
        )
    return pkgs


async def get_installation_token(expiry_minutes: int = 480) -> str:
    expiry = max(30, min(525600, expiry_minutes))
    async with _client() as c:
        resp = await c.get("/btplatform/installationtoken", params={"expiry": expiry})
        if resp.status_code not in (200, 201):
            _raise_for_api_error(resp, "get_installation_token")
        token = _extract_token(resp.json() if resp.content else "")
        if not token:
            raise EpmlError("BeyondTrust returned an empty installation token. Check your PAT permissions.")
        return token


async def download_package(url: str) -> bytes:
    # Pre-signed S3 links carry their auth in the query string, and S3
    # rejects requests that ALSO send an Authorization header — so this uses
    # a bare client (no default headers) instead of _client().
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=300.0), follow_redirects=True
    ) as c:
        resp = await c.get(url)
        if resp.status_code != 200:
            raise EpmlError(
                f"Package download failed: HTTP {resp.status_code}. Download "
                "links expire about 30 minutes after listing — re-run the sync "
                "to get fresh ones."
            )
        return resp.content


async def sync_packages_to_storage() -> dict:
    """Ensure packages exist, download each, upload to the configured asset storage backend.

    Storage backend is determined by ansible_storage (S3 > Azure Blob > GCS).
    Returns {"rpm_uploaded": bool, "deb_uploaded": bool, "packages": [...]}
    """
    from . import ansible_storage
    from .ansible_storage import AnsibleStorageError

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

        try:
            await ansible_storage.upload_asset(filename, data)
        except AnsibleStorageError as exc:
            raise EpmlError(str(exc)) from exc
        logger.info("Uploaded %s to asset storage", filename)

        if is_rpm:
            uploaded_rpm = True
        elif is_deb:
            uploaded_deb = True

    return {"rpm_uploaded": uploaded_rpm, "deb_uploaded": uploaded_deb, "packages": pkgs}


async def package_download_url(family: str) -> str:
    """A fresh BeyondTrust **presigned** download URL for the EPM-L package of the
    given family (``deb`` or ``rpm``). Used to hand a Packer build a download URL
    via ``BT_EPML_URL`` so the bt-ready provisioner can install the package at
    build time.

    Note: BeyondTrust presigned links expire ~30 minutes after listing, so this
    is resolved at build-launch time (close to when the provisioner runs)."""
    family = (family or "").lower()
    if family not in ("deb", "rpm"):
        raise EpmlError(f"unknown EPM-L package family {family!r} (expected 'deb' or 'rpm')")
    suffix = "." + family
    for pkg in await ensure_packages():
        filename = _get_package_filename(pkg)
        if filename and filename.lower().endswith(suffix):
            url = _get_package_download_url(pkg)
            if url:
                return url
            raise EpmlError(f"EPM-L {family} package found but has no download URL")
    raise EpmlError(
        f"no EPM-L {family} package available from BeyondTrust — build/sync packages first"
    )
