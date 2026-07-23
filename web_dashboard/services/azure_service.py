"""
Azure service wrapper.
All blocking SDK calls are run via asyncio.to_thread() to keep the FastAPI
event loop free — same pattern as services/aws_service.py.

Azure credentials (Client ID, Client Secret, Tenant ID, Subscription ID) are
fetched from BeyondTrust Password Safe at runtime via btapi_service.get_ps_secret().
Credentials are cached in memory after the first successful fetch so Password
Safe is only called once per server lifetime. Call invalidate_credentials() to
force a refresh (e.g. after credential rotation).
"""
import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from typing import Optional

from . import region_catalog

logger = logging.getLogger(__name__)

try:
    from azure.identity import ClientSecretCredential
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.compute.models import (
        VirtualMachine, HardwareProfile, StorageProfile, ImageReference,
        OSDisk, DiskCreateOptionTypes, OSProfile, LinuxConfiguration,
        SshConfiguration, SshPublicKey, NetworkProfile, NetworkInterfaceReference,
        WindowsConfiguration, SecurityProfile, UefiSettings, RunCommandInput,
    )
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.network.models import (
        NetworkInterface, NetworkInterfaceIPConfiguration,
        PublicIPAddress, PublicIPAddressSku,
    )
    from azure.mgmt.containerinstance import ContainerInstanceManagementClient
    from azure.mgmt.containerinstance.models import (
        ContainerGroup, Container, ResourceRequirements,
        ResourceRequests, OperatingSystemTypes,
        EnvironmentVariable, AzureFileVolume, Volume, VolumeMount,
        SecurityContextDefinition as ContainerSecurityContext,
        SecurityContextCapabilitiesDefinition as ContainerSecurityContextCapabilitiesDefinition,
    )
    from azure.mgmt.storage import StorageManagementClient
    # Import from the .resources submodule, not the top-level namespace: azure-mgmt-resource
    # 26.0.0 dropped the convenience re-export of ResourceManagementClient from
    # `azure.mgmt.resource`, so the bare import fails (ImportError) on any build that floats
    # to 26.x — which silently disabled the *entire* Azure SDK here. The .resources path has
    # existed across all supported versions, so this works on old and new alike.
    from azure.mgmt.resource.resources import ResourceManagementClient
    from azure.core.exceptions import ResourceNotFoundError
    from azure.keyvault.secrets import SecretClient
    _azure_available = True
except ImportError as e:
    logger.error("Failed to import Azure SDK: %s", e, exc_info=True)
    _azure_available = False


class AzureError(Exception):
    """Raised when an Azure operation fails."""


def _os_type_str(val) -> str:
    """Normalize Azure SDK os_type — newer SDK versions return plain strings, older return enums."""
    if val is None:
        return "Linux"
    return val.value if hasattr(val, "value") else str(val)


def _require_azure():
    if not _azure_available:
        raise AzureError(
            "Azure SDK not installed. Run: pip install azure-identity "
            "azure-mgmt-compute azure-mgmt-network azure-mgmt-resource "
            "azure-mgmt-containerinstance"
        )


# ── Credential cache ──────────────────────────────────────────────────────────

_cred_cache: Optional["ClientSecretCredential"] = None
_sub_id_cache: Optional[str] = None


async def _ensure_creds() -> tuple:
    """Return (ClientSecretCredential, subscription_id).

    Priority: config_service (DB / wizard) → env vars (settings) → BeyondTrust.
    Result is cached in-process; call invalidate_credentials() after a wizard update.
    """
    global _cred_cache, _sub_id_cache
    _require_azure()
    if _cred_cache is None:
        from ..config import settings
        from . import config_service

        # Config_service (DB) takes precedence; fall back to env vars via settings.
        client_id     = config_service.get("azure_client_id")     or settings.azure_client_id
        client_secret = config_service.get("azure_client_secret") or settings.azure_client_secret
        tenant_id     = config_service.get("azure_tenant_id")     or settings.azure_tenant_id
        sub_id        = config_service.get("azure_subscription_id") or settings.azure_subscription_id

        if client_id and client_secret and tenant_id and sub_id:
            source = "config store / env vars"
        elif settings.beyondtrust_enabled:
            from . import btapi_service
            try:
                client_id     = await btapi_service.get_ps_secret(settings.azure_client_id_secret_title)
                client_secret = await btapi_service.get_ps_secret(settings.azure_client_secret_secret_title)
                tenant_id     = await btapi_service.get_ps_secret(settings.azure_tenant_id_secret_title)
                sub_id        = await btapi_service.get_ps_secret(settings.azure_subscription_id_secret_title)
            except Exception as e:
                raise AzureError(
                    "Azure credentials not configured. Complete the setup wizard, "
                    "set AZURE_CLIENT_ID/SECRET/TENANT_ID/SUBSCRIPTION_ID in .env, "
                    f"or configure BeyondTrust Password Safe lookup (error: {e})."
                ) from e
            source = "Password Safe"
        else:
            raise AzureError(
                "Azure credentials not configured. Complete the setup wizard or "
                "set AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID, and "
                "AZURE_SUBSCRIPTION_ID in your .env file."
            )

        _cred_cache = ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )
        _sub_id_cache = sub_id
        logger.info("Azure credentials loaded from %s.", source)
    return _cred_cache, _sub_id_cache


def invalidate_credentials() -> None:
    """Force re-fetch of Azure credentials on next call (use after credential rotation)."""
    global _cred_cache, _sub_id_cache
    _cred_cache = None
    _sub_id_cache = None
    logger.info("Azure credential cache cleared.")


# The well-known AAD server application that AKS AAD-integration tokens target;
# kubelogin requests a token for `<server-id>/.default`. This is the default when
# the kubeconfig's exec block doesn't carry an explicit --server-id.
AKS_AAD_SERVER_APP_ID = "6dae42f8-4368-4678-94ff-3960e28e3630"


def aks_get_token(server_id: str = AKS_AAD_SERVER_APP_ID) -> str:
    """Mint a short-lived AAD bearer token for an AKS cluster — the server-side
    equivalent of ``kubelogin get-token`` (which an AAD-integrated AKS kubeconfig
    invokes via an exec block). Lets a transient kubectl/helm container authenticate
    to AKS without ``kubelogin``/``az`` in the container, mirroring
    :func:`aws_service.eks_get_token`. Synchronous (called from the sync runner
    kubeconfig prep): builds the credential straight from config/env. Falls back to
    the cached async-loaded credential if the config path is incomplete."""
    _require_azure()
    from ..config import settings
    from . import config_service
    client_id     = config_service.get("azure_client_id")     or settings.azure_client_id
    client_secret = config_service.get("azure_client_secret") or settings.azure_client_secret
    tenant_id     = config_service.get("azure_tenant_id")     or settings.azure_tenant_id
    if client_id and client_secret and tenant_id:
        cred = ClientSecretCredential(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
    elif _cred_cache is not None:
        cred = _cred_cache
    else:
        raise AzureError("Azure credentials not configured for AKS token minting")
    return cred.get_token(f"{server_id}/.default").token


def _get_compute(cred, sub_id):
    return ComputeManagementClient(cred, sub_id)


def _get_network(cred, sub_id):
    return NetworkManagementClient(cred, sub_id)


def _get_aci(cred, sub_id):
    return ContainerInstanceManagementClient(cred, sub_id)


def _get_resource(cred, sub_id):
    return ResourceManagementClient(cred, sub_id)


# ── Key Vault ─────────────────────────────────────────────────────────────────

def _normalize_pem(value: str) -> str:
    """Normalize line endings; reflow single-line PEM blobs from KV portal copy/paste."""
    value = (value or "").replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n").strip()
    if "\n" not in value and value.startswith("-----BEGIN"):
        header_end = value.index("-----", 5) + 5
        footer_start = value.rindex("-----END")
        header = value[:header_end]
        body_raw = value[header_end:footer_start].strip()
        footer = value[footer_start:].strip()
        body_b64 = "".join(body_raw.split())
        body = "\n".join(body_b64[i:i + 64] for i in range(0, len(body_b64), 64))
        value = f"{header}\n{body}\n{footer}\n"
    return value


def _clean_ssh_public_key(value: str) -> str:
    """Flatten an SSH public key to a single line. Azure's `key_data` accepts
    one OpenSSH public-key entry (`algorithm blob [comment]`); embedded CR/LF
    or stray whitespace can cause the VM agent to reject the key."""
    if not value:
        return ""
    flat = value.replace("\r", "").replace("\n", "").strip()
    return " ".join(flat.split())


def _ssh_key_breadcrumbs(value: str) -> dict:
    """Non-sensitive structured info about an SSH public key for log lines.
    Identifies the algorithm + comment + a sha256 prefix so two log lines
    can be cross-referenced ("did the same key reach the cloud API as was
    fetched from the secret?") without writing the key blob to disk."""
    if not value:
        return {"algo": "(empty)", "len": 0, "comment": "(none)", "sha256_12": "—"}
    parts = value.split(None, 2)
    return {
        "algo": parts[0] if parts else "(empty)",
        "len": len(value),
        "comment": parts[2] if len(parts) >= 3 else "(none)",
        "sha256_12": hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12],
    }


def _get_ssh_key_from_vault_sync(cred, vault_url: str, secret_name: str) -> str:
    """Fetch a secret value from Azure Key Vault (blocking)."""
    client = SecretClient(vault_url=vault_url, credential=cred)
    raw = client.get_secret(secret_name).value or ""
    value = _normalize_pem(raw)
    lines = value.splitlines()
    logger.info(
        "SSH key from Key Vault '%s': total_chars=%d, lines=%d, first_line=%r, last_line=%r",
        secret_name, len(value), len(lines),
        lines[0] if lines else "", lines[-1] if lines else "",
    )
    return value


async def get_ssh_key_from_vault(vault_url: str, secret_name: str) -> str:
    """Retrieve the SSH public key from Azure Key Vault using the cached credential."""
    try:
        cred, _ = await _ensure_creds()
        return await asyncio.to_thread(
            _get_ssh_key_from_vault_sync, cred, vault_url, secret_name
        )
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(
            f"Failed to retrieve '{secret_name}' from Key Vault: {e}"
        ) from e


def _list_kv_secret_names_sync(cred, vault_url: str) -> list:
    client = SecretClient(vault_url=vault_url, credential=cred)
    return sorted(p.name for p in client.list_properties_of_secrets())


async def list_kv_secret_names(vault_url: str) -> list:
    """Return every Key Vault secret name — candidate set for the per-launch
    SSH-key-secret override picker."""
    try:
        cred, _ = await _ensure_creds()
        return await asyncio.to_thread(_list_kv_secret_names_sync, cred, vault_url)
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to list Key Vault secrets: {e}") from e


def _get_ssh_keypair_from_vault_sync(cred, vault_url: str, secret_name: str) -> dict:
    """Fetch a unified keypair secret expected to be JSON `{public_key, private_key}`.

    Returns `{'public_key': str|None, 'private_key': str|None}`. On parse failure or
    non-JSON content, returns `{None, None}` — callers must NEVER receive raw secret
    material when the structure is unrecognized (would leak the private key to the
    public-key consumer). Uses `strict=False` so unescaped control chars (real newlines
    in PEM bodies) are tolerated.
    """
    client = SecretClient(vault_url=vault_url, credential=cred)
    raw = (client.get_secret(secret_name).value or "").strip()
    if not raw.startswith("{"):
        logger.warning(
            "SSH keypair secret '%s' is not a JSON object — ignoring. "
            "Use legacy single-purpose secret names if you intend to store a raw key.",
            secret_name,
        )
        return {"public_key": None, "private_key": None}
    try:
        data = json.JSONDecoder(strict=False).decode(raw)
    except json.JSONDecodeError as e:
        logger.warning(
            "SSH keypair secret '%s' looks like JSON but failed to parse (%s). "
            "Store the value as JSON with `public_key` and `private_key` string fields.",
            secret_name, e,
        )
        return {"public_key": None, "private_key": None}
    if not isinstance(data, dict):
        logger.warning(
            "SSH keypair secret '%s' parsed as %s, not an object — ignoring.",
            secret_name, type(data).__name__,
        )
        return {"public_key": None, "private_key": None}
    pub = _clean_ssh_public_key(data.get("public_key") or "")
    priv = _normalize_pem(data.get("private_key") or "")
    pub_crumbs = _ssh_key_breadcrumbs(pub)
    logger.info(
        "SSH keypair from Key Vault '%s': pub algo=%s len=%d sha256_12=%s comment=%r, priv_chars=%d",
        secret_name, pub_crumbs["algo"], pub_crumbs["len"], pub_crumbs["sha256_12"],
        pub_crumbs["comment"], len(priv),
    )
    return {"public_key": pub or None, "private_key": priv or None}


async def get_ssh_keypair_from_vault(vault_url: str, secret_name: str) -> dict:
    """Retrieve the unified SSH keypair JSON secret from Azure Key Vault.

    Returns dict with keys 'public_key' and 'private_key' (either may be None).
    """
    try:
        cred, _ = await _ensure_creds()
        return await asyncio.to_thread(
            _get_ssh_keypair_from_vault_sync, cred, vault_url, secret_name
        )
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(
            f"Failed to retrieve keypair '{secret_name}' from Key Vault: {e}"
        ) from e


async def _resolve_azure_ssh_key(
    vault_url: str,
    unified_secret_name: str,
    legacy_secret_name: str,
    *,
    field: str,
) -> str:
    """Resolution: if unified keypair secret is configured, use it exclusively.
    Otherwise fall back to legacy single-purpose secret. Never silently fall
    through from unified → legacy when both reference the same vault, since the
    legacy raw-string fetcher would expose the entire JSON value (including the
    private key) to a public-key consumer.
    `field` is 'public_key' or 'private_key'."""
    if not vault_url:
        raise AzureError("Azure Key Vault URL not configured.")
    if unified_secret_name:
        keypair = await get_ssh_keypair_from_vault(vault_url, unified_secret_name)
        value = keypair.get(field)
        if value:
            return value
        raise AzureError(
            f"Unified keypair secret '{unified_secret_name}' is missing the "
            f"'{field}' field, or its value is not valid JSON. Update the secret "
            f'to JSON like {{"public_key": "ssh-rsa ...", "private_key": "-----BEGIN ..."}}, '
            f"or clear AZURE_SSH_KEYPAIR_SECRET_NAME to use the legacy "
            f"single-purpose secret name."
        )
    if legacy_secret_name:
        return await get_ssh_key_from_vault(vault_url, legacy_secret_name)
    legacy_var = (
        "AZURE_SSH_KEY_SECRET_NAME" if field == "public_key"
        else "AZURE_SSH_PRIVATE_KEY_SECRET_NAME"
    )
    raise AzureError(
        "Azure SSH keypair not configured. Set AZURE_SSH_KEYPAIR_SECRET_NAME "
        f"(preferred, JSON with public_key/private_key fields) or legacy {legacy_var}."
    )


async def resolve_azure_ssh_public_key(
    vault_url: str, unified_secret_name: str, legacy_secret_name: str = ""
) -> str:
    """Return the SSH public key, preferring the unified keypair secret."""
    return await _resolve_azure_ssh_key(
        vault_url, unified_secret_name, legacy_secret_name, field="public_key"
    )


async def resolve_azure_ssh_private_key(
    vault_url: str, unified_secret_name: str, legacy_secret_name: str = ""
) -> str:
    """Return the SSH private key, preferring the unified keypair secret."""
    return await _resolve_azure_ssh_key(
        vault_url, unified_secret_name, legacy_secret_name, field="private_key"
    )


def verify_ssh_keypair(public_key: str, private_key: str) -> dict:
    """Verify that an SSH public key matches an SSH/PEM private key.

    Returns ``{"matches": bool, "derived_public_key": str|None, "error": str|None}``.
    `derived_public_key` is the OpenSSH public-key string derived from the
    private key — surfaced so callers can show the operator both keys
    side-by-side when a mismatch is detected. Issue #7: deploys had no way to
    catch a stale-pair unified KV secret (operator updated the private_key
    field but forgot to refresh public_key), so SSH silently failed because
    Azure was provisioning the OLD public key while the operator had the NEW
    private key locally.

    Returns matches=False with a populated `error` string for any load /
    decode failure (encrypted private key without a passphrase, malformed
    PEM, unsupported algorithm, etc.) — the caller treats those as "can't
    verify" rather than crashing the request.
    """
    if not public_key or not private_key:
        return {"matches": False, "derived_public_key": None,
                "error": "public_key or private_key is empty"}
    try:
        from cryptography.hazmat.primitives import serialization
        priv_bytes = private_key.encode("utf-8")
        # Try OpenSSH format first (modern ssh-keygen default), then PEM
        # (RSA / EC). load_ssh_private_key accepts both since cryptography
        # 35.0+ but we keep the fallback for older releases.
        try:
            priv = serialization.load_ssh_private_key(priv_bytes, password=None)
        except (ValueError, Exception):  # noqa: BLE001 — cryptography raises various types
            priv = serialization.load_pem_private_key(priv_bytes, password=None)
        derived = priv.public_key().public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        ).decode("utf-8")
        # Compare just the `<algorithm> <blob>` portion — comments differ.
        # E.g. "ssh-rsa AAAA... user@host" ≡ "ssh-rsa AAAA..." for matching.
        def _strip_comment(k: str) -> str:
            parts = k.strip().split(None, 2)
            return f"{parts[0]} {parts[1]}" if len(parts) >= 2 else k.strip()
        return {
            "matches": _strip_comment(derived) == _strip_comment(public_key),
            "derived_public_key": derived,
            "error": None,
        }
    except Exception as e:  # noqa: BLE001
        # Log the real parse error server-side; return a sanitized reason to the
        # caller (this dict is surfaced in an API response, so a raw exception
        # string would leak internal detail — CodeQL py/stack-trace-exposure).
        logger.warning("verify_ssh_keypair: could not parse private key: %s", e)
        return {"matches": False, "derived_public_key": None,
                "error": "could not parse the private key (unsupported format, or it needs a passphrase)"}


# ── Marketplace image sources ─────────────────────────────────────────────────

_MARKETPLACE_SOURCES = {
    "ubuntu": [
        {"publisher": "Canonical", "offer": "0001-com-ubuntu-server-jammy", "sku": "22_04-lts-gen2"},
        {"publisher": "Canonical", "offer": "ubuntu-24_04-lts",             "sku": "server"},
    ],
    "rhel": [
        {"publisher": "RedHat", "offer": "RHEL", "sku": "9-lvm-gen2"},
        {"publisher": "RedHat", "offer": "RHEL", "sku": "8-lvm-gen2"},
    ],
    "debian": [
        {"publisher": "Debian", "offer": "debian-12", "sku": "12-gen2"},
        {"publisher": "Debian", "offer": "debian-11", "sku": "11-gen2"},
    ],
    "windows": [
        {"publisher": "MicrosoftWindowsServer", "offer": "WindowsServer", "sku": "2022-datacenter-azure-edition"},
        {"publisher": "MicrosoftWindowsServer", "offer": "WindowsServer", "sku": "2022-datacenter-azure-edition-core"},
    ],
}


# ── Image operations ──────────────────────────────────────────────────────────

def _list_private_images_sync(cred, sub_id: str, gallery: str, gallery_rg: str, rg: str) -> dict:
    compute = _get_compute(cred, sub_id)
    results = []
    warnings: list[str] = []

    # Shared Image Gallery images
    if gallery and gallery_rg:
        try:
            for img_def in compute.gallery_images.list_by_gallery(gallery_rg, gallery):
                versions = list(compute.gallery_image_versions.list_by_gallery_image(
                    gallery_rg, gallery, img_def.name
                ))
                versions.sort(key=lambda v: v.name, reverse=True)
                latest = versions[0] if versions else None
                published = None
                regions: list[str] = []
                if latest is not None:
                    pub_profile = getattr(latest, "publishing_profile", None)
                    published = getattr(pub_profile, "published_date", None) if pub_profile else None
                    if published is None:
                        # Some SDK versions expose this directly on the version
                        published = getattr(latest, "time_created", None)
                    # Regions the latest version is replicated to — the image is only
                    # deployable in these regions (drives the picker's region filter).
                    tregions = getattr(pub_profile, "target_regions", None) if pub_profile else None
                    regions = [_normalize_region(getattr(tr, "name", "")) for tr in (tregions or []) if getattr(tr, "name", "")]
                results.append({
                    "resource_id": img_def.id,
                    "name": img_def.name,
                    "description": img_def.description or "",
                    "state": latest.provisioning_state if latest else "Unknown",
                    "creation_date": published.isoformat() if published else "",
                    "os_type": _os_type_str(img_def.os_type),
                    "source": "gallery",
                    "gallery_name": gallery,
                    "sku": img_def.identifier.sku if img_def.identifier else "",
                    "location": img_def.location or "",
                    "regions": regions,
                })
        except Exception as e:
            logger.warning("Failed to list gallery images from %s/%s: %s", gallery_rg, gallery, e)
            status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
            if status in (401, 403):
                warnings.append(
                    f"Shared Image Gallery '{gallery}' in resource group '{gallery_rg}' is "
                    f"configured but inaccessible: {e}. Grant the dashboard service principal "
                    f"Reader on that resource group."
                )
            else:
                warnings.append(
                    f"Shared Image Gallery '{gallery}' in resource group '{gallery_rg}' "
                    f"could not be listed: {e}."
                )

    # Standalone managed images. When a gallery RG is configured we scan BOTH
    # the gallery RG and the VM RG so Packer builds (which land in the VM RG
    # via build_resource_group_name) show up alongside gallery-resident
    # artefacts. Dedup by resource_id in case both keys point at the same RG.
    rgs_to_scan: list[tuple[str, str]] = []
    if gallery_rg:
        rgs_to_scan.append(("gallery", gallery_rg))
    if rg and rg != gallery_rg:
        rgs_to_scan.append(("vm", rg))

    seen_image_ids: set[str] = set()
    for label, scan_rg in rgs_to_scan:
        try:
            for img in compute.images.list_by_resource_group(scan_rg):
                if img.id in seen_image_ids:
                    continue
                seen_image_ids.add(img.id)
                results.append({
                    "resource_id": img.id,
                    "name": img.name,
                    "description": (img.tags or {}).get("Description", ""),
                    "state": img.provisioning_state or "",
                    "creation_date": "",
                    "os_type": (_os_type_str(img.storage_profile.os_disk.os_type)
                               if img.storage_profile and img.storage_profile.os_disk else "Linux"),
                    "source": "managed",
                    "gallery_name": "",
                    "sku": "",
                    "location": img.location or "",
                    "resource_group": scan_rg,
                    "regions": [_normalize_region(img.location)] if img.location else [],
                })
        except Exception as e:
            logger.warning("Failed to list managed images from rg=%s (%s): %s", scan_rg, label, e)
            status = (getattr(e, "status_code", None)
                      or getattr(getattr(e, "response", None), "status_code", None))
            if status in (401, 403):
                warnings.append(
                    f"Managed images in resource group '{scan_rg}' are inaccessible: {e}. "
                    f"Grant the dashboard service principal Reader on that resource group."
                )
            else:
                warnings.append(
                    f"Managed images in resource group '{scan_rg}' could not be listed: {e}."
                )

    return {"images": results, "warnings": warnings}


async def list_private_images(gallery: str, gallery_rg: str, rg: str) -> dict:
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(_list_private_images_sync, cred, sub_id, gallery, gallery_rg, rg)
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to list private images: {e}") from e


def _list_marketplace_images_sync(cred, sub_id: str, location: str, os_filter: str) -> list:
    compute = _get_compute(cred, sub_id)
    results = []
    sources = _MARKETPLACE_SOURCES.get(os_filter, []) if os_filter != "all" else [
        item for items in _MARKETPLACE_SOURCES.values() for item in items
    ]
    logger.info("Marketplace: fetching images for location=%s, sources=%s", location, [s["offer"] for s in sources])
    for src in sources:
        try:
            sku_name = src.get("sku", "")
            logger.info("Marketplace: fetching versions for %s/%s/%s", src["publisher"], src["offer"], sku_name)
            if not sku_name:
                logger.warning("Marketplace: SKU name missing for %s/%s", src["publisher"], src["offer"])
                continue
            
            try:
                versions = list(compute.virtual_machine_images.list(
                    location, src["publisher"], src["offer"], sku_name
                ))
                logger.info("Marketplace: found %d versions for %s/%s/%s", len(versions), src["publisher"], src["offer"], sku_name)
                if not versions:
                    continue
                latest_ver = max(versions, key=lambda v: v.name)
                results.append({
                    "resource_id": latest_ver.id,
                    "name": f"{src['publisher']} {src['offer']} {sku_name}",
                    "description": f"{src['publisher']} — {src['offer']}:{sku_name}",
                    "state": "Available",
                    "creation_date": "",
                    "os_type": "Windows" if "windows" in src["offer"].lower() else "Linux",
                    "source": "marketplace",
                    "gallery_name": "",
                    "sku": sku_name,
                    "location": location,
                    # Marketplace deployment fields
                    "publisher": src["publisher"],
                    "offer": src["offer"],
                    "version": latest_ver.name,
                })
            except Exception as e:
                logger.warning("Marketplace: failed to fetch versions for %s/%s/%s: %s", src["publisher"], src["offer"], sku_name, e, exc_info=True)
        except Exception as e:
            logger.warning("Marketplace lookup failed for %s/%s: %s", src["publisher"], src["offer"], e, exc_info=True)
    logger.info("Marketplace: total images found: %d", len(results))
    return results


async def list_marketplace_images(location: str, os_filter: str = "all") -> list:
    try:
        logger.info("Fetching marketplace images for location=%s, filter=%s", location, os_filter)
        cred, sub_id = await _ensure_creds()
        logger.info("Credentials loaded, calling _list_marketplace_images_sync")
        result = await asyncio.to_thread(_list_marketplace_images_sync, cred, sub_id, location, os_filter)
        logger.info("Successfully fetched %d marketplace images", len(result))
        return result
    except AzureError as e:
        logger.error("AzureError in list_marketplace_images: %s", e, exc_info=True)
        raise
    except Exception as e:
        logger.error("Unexpected error in list_marketplace_images: %s", e, exc_info=True)
        raise AzureError(f"Failed to list marketplace images: {e}") from e


def _delete_image_sync(cred, sub_id: str, rg: str, image_name: str) -> None:
    compute = _get_compute(cred, sub_id)
    compute.images.begin_delete(rg, image_name).result()


async def delete_image(rg: str, image_name: str) -> None:
    try:
        cred, sub_id = await _ensure_creds()
        await asyncio.to_thread(_delete_image_sync, cred, sub_id, rg, image_name)
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to delete image {image_name}: {e}") from e


# ── VM quota check ────────────────────────────────────────────────────────────

def _check_quota_sync(cred, sub_id: str, location: str, vm_size: str) -> None:
    """Raise AzureError if regional core quota is insufficient for vm_size."""
    compute = _get_compute(cred, sub_id)

    # Get core count for the requested VM size
    sizes = {s.name: s.number_of_cores for s in compute.virtual_machine_sizes.list(location)}
    required_cores = sizes.get(vm_size)
    if required_cores is None:
        raise AzureError(f"VM size '{vm_size}' is not available in {location}.")

    # Get current regional core usage
    for item in compute.usage.list(location):
        if item.name.value == "cores":
            current = item.current_value
            limit = item.limit
            if current + required_cores > limit:
                raise AzureError(
                    f"Quota exceeded in {location}: {current}/{limit} cores used. "
                    f"'{vm_size}' requires {required_cores} cores "
                    f"(would need {current + required_cores}, limit {limit}). "
                    f"Choose a smaller VM size or request a quota increase."
                )
            return


async def check_vm_quota(location: str, vm_size: str) -> None:
    """Async wrapper — raises AzureError if quota would be exceeded."""
    try:
        cred, sub_id = await _ensure_creds()
        await asyncio.to_thread(_check_quota_sync, cred, sub_id, location, vm_size)
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to check quota: {e}") from e


# ── VM operations ─────────────────────────────────────────────────────────────

_PASSWORD_SYMBOLS = "!@#$%^&*()-_=+"


def generate_windows_admin_password(length: int = 20) -> str:
    """Random password satisfying Azure's Windows rules (12–123 chars, 3 of 4
    character classes) — always includes all four classes for margin."""
    import random
    import secrets
    import string
    length = max(length, 12)
    chars = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(_PASSWORD_SYMBOLS),
    ]
    alphabet = string.ascii_letters + string.digits + _PASSWORD_SYMBOLS
    chars += [secrets.choice(alphabet) for _ in range(length - len(chars))]
    random.SystemRandom().shuffle(chars)
    return "".join(chars)


def store_windows_admin_password(vm_name: str, key_suffix: str, password: str) -> tuple[str, str]:
    """Store a generated Windows admin password in the configured secrets
    backend. Returns ``(backend, ref)`` for job metadata — job records carry
    the reference, never the plaintext. Raises AzureError when the write
    fails: a Windows VM whose password can't be retrieved later is useless,
    so callers must store before deploying."""
    from . import config_service, secrets_backend_service
    backend = config_service.get("secrets_backend") or "database"
    key = f"windows-admin-{vm_name}-{key_suffix}"
    try:
        ref = secrets_backend_service.write_sync(backend, key, password)
    except Exception as e:
        raise AzureError(
            f"Failed to store Windows admin password for {vm_name} in secrets backend '{backend}': {e}"
        ) from e
    return backend, ref


# Deploy bounded-wait timeout: a VM that never leaves "Creating" (e.g. an
# unsupported size/image combo) must fail the job rather than hang it forever.
_VM_DEPLOY_TIMEOUT_S = 1200  # 20 min — generous for any normal deploy

# Managed-image-from-blob bounded-wait timeout: an image-create that never
# reaches "Succeeded" (unreadable/malformed source blob) must fail the promote
# rather than hang the in-app background task forever.
_IMAGE_CREATE_TIMEOUT_S = 1800  # 30 min — generous for a multi-GB VHD import


def _best_effort_cleanup(compute, network, rg, vm_name, nic_name, pip_name=None) -> None:
    """Remove a half-created VM + NIC (+ optional PIP) after a failed deploy so a
    partial deployment doesn't leave billable orphans (the seat row may not yet
    hold vm_resource_id for teardown to find). Best-effort + ordered: VM first
    (frees the NIC; its OS disk has delete_option=Delete), then NIC, then PIP."""
    try:
        compute.virtual_machines.begin_delete(rg, vm_name).wait()
    except Exception as e:
        logger.warning("deploy cleanup: VM %s delete failed: %s", vm_name, e)
    try:
        network.network_interfaces.begin_delete(rg, nic_name).wait()
    except Exception as e:
        logger.warning("deploy cleanup: NIC %s delete failed: %s", nic_name, e)
    if pip_name:
        try:
            network.public_ip_addresses.begin_delete(rg, pip_name).wait()
        except Exception as e:
            logger.warning("deploy cleanup: PIP %s delete failed: %s", pip_name, e)


def _normalize_region(r: str) -> str:
    """Azure regions come back as 'centralus' or sometimes 'Central US' — fold to
    a canonical comparable form."""
    return (r or "").replace(" ", "").lower()


def _resource_region_from_id(network, resource_id: str, kind: str):
    """Region of a subnet's VNet (kind='subnet') or an NSG (kind='nsg') by ARM id.
    Returns the region string, or None if it can't be determined (caller fails open)."""
    try:
        parts = resource_id.split("/")
        rg = parts[parts.index("resourceGroups") + 1]
        if kind == "subnet":
            vnet = parts[parts.index("virtualNetworks") + 1]
            return network.virtual_networks.get(rg, vnet).location
        if kind == "nsg":
            nsg = parts[parts.index("networkSecurityGroups") + 1]
            return network.network_security_groups.get(rg, nsg).location
    except Exception as e:
        logger.warning("region lookup failed for %s (%s): %s", resource_id, kind, e)
    return None


def _sku_trusted_launch_capable(compute, location: str, vm_size: str):
    """True/False when we can determine whether vm_size supports Trusted Launch in
    location (Gen2 + vTPM/Secure Boot, TL not disabled); None when unknown (e.g. the
    SKU list is unavailable or the size isn't listed) so the caller fails open."""
    try:
        for sku in compute.resource_skus.list(filter=f"location eq '{location}'"):
            if sku.resource_type == "virtualMachines" and sku.name == vm_size:
                caps = {c.name: c.value for c in (sku.capabilities or [])}
                gens = caps.get("HyperVGenerations", "") or ""
                tl_disabled = (caps.get("TrustedLaunchDisabled", "False") or "False")
                return ("V2" in gens) and (tl_disabled.lower() != "true")
        return None
    except Exception as e:
        logger.warning("SKU capability lookup failed for %s in %s: %s", vm_size, location, e)
        return None


def _validate_deploy_consistency(compute, network, location, subnet_id, nsg_ids,
                                 vm_size, trusted_launch) -> None:
    """Pre-deploy guards run BEFORE any resource is created: turn the cryptic Azure
    failures (InvalidResourceReference on a cross-region NIC; a Trusted-Launch VM
    stuck forever in 'Creating' on a Gen1 size) into clear, actionable errors. A
    *confirmed* mismatch raises AzureError; a failed check (API hiccup, unknown
    size) is logged and skipped so it never blocks an otherwise valid deploy."""
    want = _normalize_region(location)
    if subnet_id:
        sr = _resource_region_from_id(network, subnet_id, "subnet")
        if sr and _normalize_region(sr) != want:
            raise AzureError(
                f"The selected subnet is in region '{sr}', but the deploy region is "
                f"'{location}'. They must match — pick a subnet in '{location}' or change "
                f"the deploy region."
            )
    for nsg_id in (nsg_ids or []):
        nr = _resource_region_from_id(network, nsg_id, "nsg")
        if nr and _normalize_region(nr) != want:
            raise AzureError(
                f"The selected NSG is in region '{nr}', but the deploy region is "
                f"'{location}'. They must match — pick an NSG in '{location}' or change "
                f"the deploy region."
            )
    if trusted_launch and vm_size:
        cap = _sku_trusted_launch_capable(compute, location, vm_size)
        if cap is False:
            raise AzureError(
                f"VM size '{vm_size}' is not Trusted-Launch capable in '{location}' — it "
                f"must be a Gen2 size with vTPM/Secure Boot (e.g. Standard_D2s_v3). A "
                f"non-Gen2 size would hang in 'Creating' forever."
            )


def _deploy_vm_sync(
    cred, sub_id: str, rg: str, location: str, vm_name: str, vm_size: str,
    image_id: str, subnet_id: str, nsg_ids: list, create_public_ip: bool,
    ssh_username: str, ssh_public_key: str,
    image_publisher: str = None, image_offer: str = None,
    image_sku: str = None, image_version: str = None,
    workgroup: str = "",
    os_type: str = "Linux", admin_password: str = "",
    trusted_launch: bool = False,
) -> dict:
    is_windows = (os_type or "Linux").lower() == "windows"
    if is_windows:
        if not admin_password:
            raise AzureError(f"Windows deploy {vm_name}: admin_password is required.")
        logger.info("Azure deploy %s: Windows VM, admin user=%s (password auth)", vm_name, ssh_username)
    else:
        # Sanitize the public key as a defence-in-depth — callers should already
        # be passing a single-line OpenSSH entry, but a stray CR/LF here will
        # cause waagent to reject the key and SSH auth to silently fail.
        ssh_public_key = _clean_ssh_public_key(ssh_public_key)
        crumbs = _ssh_key_breadcrumbs(ssh_public_key)
        logger.info(
            "Azure deploy %s: injecting SSH key algo=%s len=%d sha256_12=%s comment=%r as user=%s",
            vm_name, crumbs["algo"], crumbs["len"], crumbs["sha256_12"], crumbs["comment"], ssh_username,
        )
    compute = _get_compute(cred, sub_id)
    network = _get_network(cred, sub_id)
    # Fail fast on region/size mismatches BEFORE creating any resource — turns
    # cryptic Azure errors (InvalidResourceReference, stuck "Creating") into clear
    # messages and avoids orphaned half-deploys.
    _validate_deploy_consistency(compute, network, location, subnet_id, nsg_ids,
                                 vm_size, trusted_launch)
    tags = {"managed-by": "vm-dashboard"}
    if workgroup:
        tags["workgroup"] = workgroup

    public_ip_id = None
    pip_name = f"{vm_name}-pip"

    # Step 1: Create Public IP (optional)
    if create_public_ip:
        pip = network.public_ip_addresses.begin_create_or_update(
            rg, pip_name,
            PublicIPAddress(
                location=location,
                public_ip_allocation_method="Dynamic",
                tags=tags,
            )
        ).result()
        public_ip_id = pip.id

    # Step 2: Create NIC
    nic_name = f"{vm_name}-nic"
    ip_config = NetworkInterfaceIPConfiguration(
        name="ipconfig1",
        subnet={"id": subnet_id},
        private_ip_address_allocation="Dynamic",
    )
    if public_ip_id:
        ip_config.public_ip_address = {"id": public_ip_id}

    nic_params = NetworkInterface(
        location=location,
        ip_configurations=[ip_config],
        tags=tags,
    )
    if nsg_ids:
        nic_params.network_security_group = {"id": nsg_ids[0]}

    nic = network.network_interfaces.begin_create_or_update(rg, nic_name, nic_params).result()

    # Step 3: Build image reference (marketplace or managed)
    if image_publisher and image_offer and image_sku and image_version:
        # Marketplace image
        image_ref = ImageReference(
            publisher=image_publisher,
            offer=image_offer,
            sku=image_sku,
            version=image_version,
        )
        logger.info("Deploy: using marketplace image %s/%s/%s/%s", image_publisher, image_offer, image_sku, image_version)
    else:
        # Managed or gallery image
        image_ref = ImageReference(id=image_id)
        logger.info("Deploy: using managed image %s", image_id)

    # Step 4: Create VM
    if is_windows:
        os_profile = OSProfile(
            computer_name=vm_name[:15],
            admin_username=ssh_username,
            admin_password=admin_password,
            windows_configuration=WindowsConfiguration(
                provision_vm_agent=True,
                enable_automatic_updates=True,
            ),
        )
    else:
        os_profile = OSProfile(
            computer_name=vm_name[:15],
            admin_username=ssh_username,
            linux_configuration=LinuxConfiguration(
                disable_password_authentication=True,
                ssh=SshConfiguration(
                    public_keys=[
                        SshPublicKey(
                            path=f"/home/{ssh_username}/.ssh/authorized_keys",
                            key_data=ssh_public_key,
                        )
                    ]
                ),
            ),
        )
    vm_params = VirtualMachine(
        location=location,
        tags=tags,
        hardware_profile=HardwareProfile(vm_size=vm_size),
        storage_profile=StorageProfile(
            image_reference=image_ref,
            os_disk=OSDisk(
                create_option=DiskCreateOptionTypes.FROM_IMAGE,
                delete_option="Delete",
            ),
        ),
        os_profile=os_profile,
        network_profile=NetworkProfile(
            network_interfaces=[NetworkInterfaceReference(id=nic.id, primary=True)]
        ),
    )

    # Trusted Launch (Windows 11 / gallery trusted-launch images): the VM must
    # declare the matching security profile, and Windows client images attest
    # multi-tenant hosting eligibility via the Windows_Client license type.
    if trusted_launch:
        vm_params.security_profile = SecurityProfile(
            security_type="TrustedLaunch",
            uefi_settings=UefiSettings(secure_boot_enabled=True, v_tpm_enabled=True),
        )
        if is_windows:
            vm_params.license_type = "Windows_Client"
        logger.info("Azure deploy %s: Trusted Launch (secure boot + vTPM)%s", vm_name,
                    ", Windows_Client license" if is_windows else "")

    # Create the VM with a bounded wait: a stuck "Creating" must fail the job, not
    # hang it forever. On any failure, best-effort remove the NIC/PIP/VM we created
    # so a half-deploy doesn't orphan billable resources.
    try:
        poller = compute.virtual_machines.begin_create_or_update(rg, vm_name, vm_params)
        deadline = time.monotonic() + _VM_DEPLOY_TIMEOUT_S
        while not poller.done():
            if time.monotonic() > deadline:
                raise AzureError(
                    f"VM {vm_name} did not finish provisioning within "
                    f"{_VM_DEPLOY_TIMEOUT_S // 60} min — provisioning appears stuck "
                    f"(check that the size supports the image; Trusted Launch needs a Gen2 size)."
                )
            poller.wait(15)
        vm = poller.result()

        # Fetch IPs from NIC
        nic_detail = network.network_interfaces.get(rg, nic_name)
        private_ip = None
        public_ip_addr = None
        if nic_detail.ip_configurations:
            private_ip = nic_detail.ip_configurations[0].private_ip_address
            pip_ref = nic_detail.ip_configurations[0].public_ip_address
            if pip_ref:
                pip_detail = network.public_ip_addresses.get(rg, pip_name)
                public_ip_addr = pip_detail.ip_address
    except Exception as exc:
        logger.warning("Azure deploy %s failed (%s) — cleaning up partial resources", vm_name, exc)
        _best_effort_cleanup(compute, network, rg, vm_name, nic_name,
                             pip_name if create_public_ip else None)
        raise

    return {
        "vm_id": vm.id,
        "vm_name": vm_name,
        "private_ip": private_ip,
        "public_ip": public_ip_addr,
        "nic_name": nic_name,
        "pip_name": pip_name if create_public_ip else None,
        "resource_group": rg,
    }


async def deploy_vm(
    rg: str, location: str, vm_name: str, vm_size: str,
    image_id: str, subnet_id: str, nsg_ids: list, create_public_ip: bool,
    ssh_username: str, ssh_public_key: str,
    image_publisher: str = None, image_offer: str = None,
    image_sku: str = None, image_version: str = None,
    workgroup: str = "",
    os_type: str = "Linux", admin_password: str = "",
    trusted_launch: bool = False,
) -> dict:
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(
            _deploy_vm_sync,
            cred, sub_id, rg, location, vm_name, vm_size,
            image_id, subnet_id, nsg_ids, create_public_ip,
            ssh_username, ssh_public_key,
            image_publisher, image_offer, image_sku, image_version,
            workgroup,
            os_type=os_type, admin_password=admin_password,
            trusted_launch=trusted_launch,
        )
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to deploy VM {vm_name}: {e}") from e


# ── Compute Gallery (Trusted Launch image publishing for Windows 11) ──────────
# Win 11 requires Trusted Launch, and Azure cannot create a managed image from a
# Trusted Launch VM — so Win-client Packer builds publish a gallery image VERSION
# instead. Packer creates the version; the gallery + image DEFINITION must exist
# first, so these idempotent helpers create-or-update them before the build.

def _ensure_gallery_sync(cred, sub_id: str, rg: str, location: str, gallery_name: str):
    from azure.mgmt.compute.models import Gallery
    compute = _get_compute(cred, sub_id)
    return compute.galleries.begin_create_or_update(
        rg, gallery_name, Gallery(location=location)
    ).result()


async def ensure_gallery(rg: str, location: str, gallery_name: str):
    """Create the Azure Compute Gallery if missing (idempotent)."""
    cred, sub_id = await _ensure_creds()
    return await asyncio.to_thread(_ensure_gallery_sync, cred, sub_id, rg, location, gallery_name)


def _ensure_tl_image_def_sync(cred, sub_id: str, rg: str, gallery_name: str,
                              image_def_name: str, location: str):
    from azure.mgmt.compute.models import (
        GalleryImage, GalleryImageIdentifier, GalleryImageFeature,
    )
    compute = _get_compute(cred, sub_id)
    img_def = GalleryImage(
        location=location,
        os_type="Windows",
        os_state="Generalized",          # Packer sysprep /generalize'd the source
        hyper_v_generation="V2",         # Gen2 — required for Trusted Launch
        identifier=GalleryImageIdentifier(
            # Dashboard-owned label namespace — NOT the marketplace source.
            publisher="vm-dashboard", offer="windows-client", sku=image_def_name,
        ),
        features=[GalleryImageFeature(name="SecurityType", value="TrustedLaunch")],
    )
    return compute.gallery_images.begin_create_or_update(
        rg, gallery_name, image_def_name, img_def
    ).result()


async def ensure_trusted_launch_image_definition(rg: str, gallery_name: str,
                                                 image_def_name: str, location: str):
    """Create a Gen2 / TrustedLaunch / Generalized Windows image definition if
    missing (idempotent). Packer publishes a version into it."""
    cred, sub_id = await _ensure_creds()
    return await asyncio.to_thread(
        _ensure_tl_image_def_sync, cred, sub_id, rg, gallery_name, image_def_name, location,
    )


def _ensure_linux_image_def_sync(cred, sub_id: str, rg: str, gallery_name: str,
                                 image_def_name: str, location: str,
                                 hyper_v_generation: str = "V2"):
    from azure.mgmt.compute.models import GalleryImage, GalleryImageIdentifier
    compute = _get_compute(cred, sub_id)
    img_def = GalleryImage(
        location=location,
        os_type="Linux",
        os_state="Generalized",          # waagent -deprovision generalized the source
        # Must match the generation of the built VM (which follows the source
        # marketplace SKU). Mismatch makes Packer's gallery publish fail.
        hyper_v_generation=hyper_v_generation,
        identifier=GalleryImageIdentifier(
            # Dashboard-owned label namespace — NOT the marketplace source.
            publisher="vm-dashboard", offer="linux", sku=image_def_name,
        ),
    )
    return compute.gallery_images.begin_create_or_update(
        rg, gallery_name, image_def_name, img_def
    ).result()


async def ensure_linux_image_definition(rg: str, gallery_name: str,
                                        image_def_name: str, location: str,
                                        hyper_v_generation: str = "V2"):
    """Create a Linux / Generalized image definition if missing (idempotent).
    Packer publishes a version into it. ``hyper_v_generation`` (V1/V2) must match
    the built VM's generation, which follows the source marketplace SKU."""
    cred, sub_id = await _ensure_creds()
    return await asyncio.to_thread(
        _ensure_linux_image_def_sync, cred, sub_id, rg, gallery_name, image_def_name,
        location, hyper_v_generation,
    )


def _set_workgroup_tag_sync(cred, sub_id: str, rg: str, vm_name: str, workgroup: str) -> None:
    """Merge a `workgroup` tag into the VM's existing tags (preserves others)."""
    compute = _get_compute(cred, sub_id)
    vm = compute.virtual_machines.get(rg, vm_name)
    tags = dict(vm.tags or {})
    tags["workgroup"] = workgroup
    compute.virtual_machines.begin_update(rg, vm_name, {"tags": tags}).result()


async def set_workgroup_tag(rg: str, vm_name: str, workgroup: str) -> None:
    """Rewrite the `workgroup` tag on an Azure VM (preserves other tags). Used
    by the admin reassign endpoint."""
    try:
        cred, sub_id = await _ensure_creds()
        await asyncio.to_thread(_set_workgroup_tag_sync, cred, sub_id, rg, vm_name, workgroup)
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to set workgroup tag on {vm_name}: {e}") from e


def _set_tag_sync(cred, sub_id: str, rg: str, vm_name: str, key: str, value: str) -> None:
    """Merge a single tag into the VM's existing tags (preserves others)."""
    compute = _get_compute(cred, sub_id)
    vm = compute.virtual_machines.get(rg, vm_name)
    tags = dict(vm.tags or {})
    tags[key] = value
    compute.virtual_machines.begin_update(rg, vm_name, {"tags": tags}).result()


async def set_desktop_pool_tag(rg: str, vm_name: str, pool_name: str) -> None:
    """Tag an Azure VM as a member of a virtual-desktop pool (VDI Phase 1) so the
    pool's live state is recoverable from the cloud. Tag key is
    ``dashboard:desktop_pool``; preserves other tags."""
    try:
        cred, sub_id = await _ensure_creds()
        await asyncio.to_thread(_set_tag_sync, cred, sub_id, rg, vm_name, "dashboard:desktop_pool", pool_name)
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to set desktop_pool tag on {vm_name}: {e}") from e


def _is_dashboard_managed(tags: dict) -> bool:
    """True when a resource carries the dashboard's managed-by tag — the
    canonical ``managed-by=vm-dashboard`` or the legacy
    ``ManagedBy=vm-cli-dashboard`` (kept so resources created before the #194
    tag normalization still show up)."""
    tags = tags or {}
    return (tags.get("managed-by") == "vm-dashboard"
            or tags.get("ManagedBy") == "vm-cli-dashboard")


def _describe_vms_sync(cred, sub_id: str, rg: str) -> list:
    compute = _get_compute(cred, sub_id)
    network = _get_network(cred, sub_id)
    results = []
    for vm in compute.virtual_machines.list(rg):
        tags = vm.tags or {}
        if not _is_dashboard_managed(tags):
            continue
        try:
            iv = compute.virtual_machines.instance_view(rg, vm.name)
            statuses = iv.statuses or []
            power = next(
                (s.display_status for s in statuses if s.code and s.code.startswith("PowerState/")),
                "Unknown"
            )
        except Exception:
            power = "Unknown"

        private_ip = None
        public_ip = None
        if vm.network_profile and vm.network_profile.network_interfaces:
            nic_id = vm.network_profile.network_interfaces[0].id
            nic_name = nic_id.split("/")[-1]
            nic_rg = nic_id.split("/resourceGroups/")[1].split("/")[0]
            try:
                nic = network.network_interfaces.get(nic_rg, nic_name)
                if nic.ip_configurations:
                    private_ip = nic.ip_configurations[0].private_ip_address
                    pip_ref = nic.ip_configurations[0].public_ip_address
                    if pip_ref:
                        pip_name = pip_ref.id.split("/")[-1]
                        pip_rg = pip_ref.id.split("/resourceGroups/")[1].split("/")[0]
                        pip = network.public_ip_addresses.get(pip_rg, pip_name)
                        public_ip = pip.ip_address
            except Exception:
                pass

        results.append({
            "vm_id": vm.id,
            "name": vm.name,
            "state": power,
            "public_ip": public_ip,
            "private_ip": private_ip,
            "location": vm.location or "",
            "size": vm.hardware_profile.vm_size if vm.hardware_profile else "",
            "os_type": (
                _os_type_str(vm.storage_profile.os_disk.os_type)
                if vm.storage_profile and vm.storage_profile.os_disk else ""
            ),
            "workgroup": (tags.get("workgroup") or "").lower() or None,
        })
    return results


async def describe_vms(rg: str) -> list:
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(_describe_vms_sync, cred, sub_id, rg)
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to describe VMs in {rg}: {e}") from e


def _get_vm_sync(cred, sub_id: str, rg: str, vm_name: str) -> Optional[dict]:
    """Fetch a single VM by name, regardless of tags."""
    compute = _get_compute(cred, sub_id)
    network = _get_network(cred, sub_id)
    try:
        vm = compute.virtual_machines.get(rg, vm_name)
    except Exception:
        return None

    try:
        iv = compute.virtual_machines.instance_view(rg, vm.name)
        statuses = iv.statuses or []
        power = next(
            (s.display_status for s in statuses if s.code and s.code.startswith("PowerState/")),
            "Unknown"
        )
    except Exception:
        power = "Unknown"

    private_ip = None
    public_ip = None
    if vm.network_profile and vm.network_profile.network_interfaces:
        nic_id = vm.network_profile.network_interfaces[0].id
        nic_name = nic_id.split("/")[-1]
        nic_rg = nic_id.split("/resourceGroups/")[1].split("/")[0]
        try:
            nic = network.network_interfaces.get(nic_rg, nic_name)
            if nic.ip_configurations:
                private_ip = nic.ip_configurations[0].private_ip_address
                pip_ref = nic.ip_configurations[0].public_ip_address
                if pip_ref:
                    pip_name = pip_ref.id.split("/")[-1]
                    pip_rg = pip_ref.id.split("/resourceGroups/")[1].split("/")[0]
                    pip = network.public_ip_addresses.get(pip_rg, pip_name)
                    public_ip = pip.ip_address
        except Exception:
            pass

    return {
        "vm_id": vm.id,
        "name": vm.name,
        "state": power,
        "public_ip": public_ip,
        "private_ip": private_ip,
        "location": vm.location or "",
        "size": vm.hardware_profile.vm_size if vm.hardware_profile else "",
        "os_type": (
            _os_type_str(vm.storage_profile.os_disk.os_type)
            if vm.storage_profile and vm.storage_profile.os_disk else ""
        ),
    }


async def get_vm(rg: str, vm_name: str) -> Optional[dict]:
    """Fetch a single VM by name (no tag filter). Returns None if not found."""
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(_get_vm_sync, cred, sub_id, rg, vm_name)
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to get VM {vm_name}: {e}") from e


def _terminate_vm_sync(cred, sub_id: str, rg: str, vm_name: str) -> None:
    compute = _get_compute(cred, sub_id)
    network = _get_network(cred, sub_id)

    nic_name = None
    pip_name = None
    try:
        vm = compute.virtual_machines.get(rg, vm_name)
        if vm.network_profile and vm.network_profile.network_interfaces:
            nic_id = vm.network_profile.network_interfaces[0].id
            nic_name = nic_id.split("/")[-1]
            nic = network.network_interfaces.get(rg, nic_name)
            if nic.ip_configurations:
                pip_ref = nic.ip_configurations[0].public_ip_address
                if pip_ref:
                    pip_name = pip_ref.id.split("/")[-1]
    except Exception as e:
        logger.warning("Could not get VM NIC/PIP info before delete: %s", e)

    compute.virtual_machines.begin_delete(rg, vm_name).result()

    if nic_name:
        try:
            network.network_interfaces.begin_delete(rg, nic_name).result()
        except Exception as e:
            logger.warning("Failed to delete NIC %s: %s", nic_name, e)

    if pip_name:
        try:
            network.public_ip_addresses.begin_delete(rg, pip_name).result()
        except Exception as e:
            logger.warning("Failed to delete PIP %s: %s", pip_name, e)


async def terminate_vm(rg: str, vm_name: str) -> None:
    try:
        cred, sub_id = await _ensure_creds()
        await asyncio.to_thread(_terminate_vm_sync, cred, sub_id, rg, vm_name)
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to terminate VM {vm_name}: {e}") from e


# ── Tunnel-capable BeyondTrust Jumpoint on an Azure VM ────────────────────────
# Azure Container Instances (the run_aci_jumpoint_task path) is serverless and
# CANNOT do protocol tunneling — a BT Jumpoint needs NET_ADMIN + NET_RAW +
# IPC_LOCK and /dev/net/tun, which ACI forbids. So for the cloud-database tunnel
# the Jumpoint runs as a PRIVILEGED container on a real Azure VM (cloud-init runs
# `docker run --privileged --device /dev/net/tun …`). One shared, ref-counted VM.

def _vm_jumpoint_cloud_init(container_image: str, deploy_key: str,
                            install_db_clients: bool = False) -> str:
    """Base64 cloud-init: install Docker, then run the BT Jumpoint container
    privileged with /dev/net/tun (the caps a protocol tunnel needs). The deploy
    key is an opaque token, single-quoted for the shell.

    When ``install_db_clients`` is set (for the Password Safe Azure cloud-DB
    onboarding) it also installs the native DB clients the "{engine} Azure Run
    Command Plugin" invokes on this VM at rotation time: psql (/usr/bin/psql),
    mysql (/usr/bin/mysql) and sqlcmd (/opt/mssql-tools18/bin/sqlcmd, from the
    Microsoft apt repo). This is a fresh-VM head start — the onboarding also
    ensures the clients idempotently over Run Command, covering a reused VM."""
    import base64
    packages = ["docker.io"]
    runcmd = ["modprobe tun || true", "systemctl enable --now docker"]
    if install_db_clients:
        packages += ["postgresql-client", "mysql-client", "curl",
                     "gnupg", "apt-transport-https"]
        runcmd += [
            "curl -fsSL https://packages.microsoft.com/keys/microsoft.asc "
            "-o /etc/apt/trusted.gpg.d/microsoft.asc",
            "curl -fsSL https://packages.microsoft.com/config/ubuntu/22.04/prod.list "
            "-o /etc/apt/sources.list.d/mssql-release.list",
            "apt-get update",
            "ACCEPT_EULA=Y apt-get install -y mssql-tools18 unixodbc-dev",
        ]
    runcmd.append(
        "docker run -d --restart always --name jumpoint "
        "--privileged --device /dev/net/tun --cap-add NET_ADMIN --cap-add NET_RAW "
        f"-e DEPLOY_KEY='{deploy_key}' {container_image}")
    lines = ["#cloud-config", "package_update: true", "packages:"]
    lines += [f"  - {p}" for p in packages]
    lines.append("runcmd:")
    lines += [f"  - [ sh, -c, {json.dumps(c)} ]" for c in runcmd]
    cloud_init = "\n".join(lines) + "\n"
    return base64.b64encode(cloud_init.encode()).decode()


def _run_vm_jumpoint_sync(
    cred, sub_id: str, rg: str, location: str, subnet_id: str, name: str,
    container_image: str, deploy_key: str, vm_size: str,
    admin_username: str, admin_password: str, install_db_clients: bool = False,
) -> dict:
    """Find-or-create an Azure VM running the BT Jumpoint container. Idempotent on
    name: returns ``reused=True`` when it already exists. The NIC carries a
    **Standard SKU, Static** public IP used solely for a stable, knowable EGRESS
    address (the dashboard whitelists it in the Rancher node firewall). Standard
    public IPs are *secure by default* — all inbound is blocked unless an NSG
    explicitly allows it, and none is attached — so this adds no ingress path."""
    compute = _get_compute(cred, sub_id)
    network = _get_network(cred, sub_id)
    pip_name = f"{name}-pip"
    try:
        existing = compute.virtual_machines.get(rg, name)
    except Exception:
        existing = None
    if existing is not None:
        # Reuse: surface the existing egress (public) IP so callers can whitelist it.
        public_ip = ""
        try:
            public_ip = network.public_ip_addresses.get(rg, pip_name).ip_address or ""
        except Exception:
            pass  # older jumpoint without a PIP → caller falls back to a manual CIDR
        return {"vm_id": existing.id, "vm_name": name, "resource_group": rg,
                "reused": True, "public_ip": public_ip}

    tags = {"managed-by": "vm-dashboard", "purpose": "clouddb-jumpoint"}
    # Standard + Static = secure-by-default (no inbound) egress IP.
    pip = network.public_ip_addresses.begin_create_or_update(
        rg, pip_name,
        PublicIPAddress(location=location, sku=PublicIPAddressSku(name="Standard"),
                        public_ip_allocation_method="Static", tags=tags),
    ).result()
    nic_name = f"{name}-nic"
    ip_config = NetworkInterfaceIPConfiguration(
        name="ipconfig1", subnet={"id": subnet_id},
        private_ip_address_allocation="Dynamic",
        public_ip_address={"id": pip.id},
    )
    nic = network.network_interfaces.begin_create_or_update(
        rg, nic_name, NetworkInterface(location=location, ip_configurations=[ip_config], tags=tags)
    ).result()

    image_ref = ImageReference(
        publisher="Canonical", offer="0001-com-ubuntu-server-jammy",
        sku="22_04-lts", version="latest",
    )
    os_profile = OSProfile(
        computer_name=name[:15],
        admin_username=admin_username,
        admin_password=admin_password,
        linux_configuration=LinuxConfiguration(disable_password_authentication=False),
        custom_data=_vm_jumpoint_cloud_init(container_image, deploy_key, install_db_clients),
    )
    vm_params = VirtualMachine(
        location=location, tags=tags,
        hardware_profile=HardwareProfile(vm_size=vm_size),
        storage_profile=StorageProfile(
            image_reference=image_ref,
            os_disk=OSDisk(create_option=DiskCreateOptionTypes.FROM_IMAGE, delete_option="Delete"),
        ),
        os_profile=os_profile,
        network_profile=NetworkProfile(
            network_interfaces=[NetworkInterfaceReference(id=nic.id, primary=True)]
        ),
    )
    vm = compute.virtual_machines.begin_create_or_update(rg, name, vm_params).result()
    return {"vm_id": vm.id, "vm_name": name, "resource_group": rg, "reused": False,
            "public_ip": (pip.ip_address or "")}


async def run_vm_jumpoint(
    rg: str, location: str, subnet_id: str, name: str,
    container_image: str, deploy_key: str, vm_size: str = "Standard_B1s",
    admin_username: str = "jpadmin", admin_password: str = "",
    install_db_clients: bool = False,
) -> dict:
    """Ensure an Azure VM Jumpoint (idempotent on name). The VM egresses via a
    Standard, secure-by-default (no inbound) public IP on its NIC, returned as
    ``public_ip`` so callers can whitelist that stable egress address; it phones
    home to PRA over egress. ``install_db_clients`` bakes the native DB clients
    into the VM for the Password Safe cloud-DB Run Command plugin (fresh-VM only)."""
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(
            _run_vm_jumpoint_sync, cred, sub_id, rg, location, subnet_id, name,
            container_image, deploy_key, vm_size, admin_username, admin_password,
            install_db_clients,
        )
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to start Azure VM Jumpoint {name}: {e}") from e


async def stop_vm_jumpoint(rg: str, name: str) -> None:
    """Delete the Jumpoint VM (+ its NIC). Best-effort."""
    try:
        cred, sub_id = await _ensure_creds()
        await asyncio.to_thread(_terminate_vm_sync, cred, sub_id, rg, name)
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to stop Azure VM Jumpoint {name}: {e}") from e


# ── Azure VM Run Command (in-guest shell over the control plane) ──────────────
# The control-plane analog of aws_service.ssm_send_command: run a shell script in
# a VM's guest via the Azure VM agent (waagent) without any inbound path to the
# VM. Used to run the DB client on the shared jump VM for the Password Safe
# cloud-DB onboarding (managed-user creation + plugin key material).

_RUN_CMD_MARKER = "__PSRC__"


def _parse_run_command_output(result) -> tuple:
    """Pull (stdout, stderr) out of a RunCommandResult's InstanceViewStatus list."""
    stdout = stderr = ""
    for st in (getattr(result, "value", None) or []):
        code = (getattr(st, "code", "") or "").lower()
        msg = getattr(st, "message", "") or ""
        if "stdout" in code:
            stdout = msg
        elif "stderr" in code:
            stderr = msg
    return stdout, stderr


def _run_command_script(commands: list) -> list:
    """Wrap the caller's commands so the in-guest shell surfaces the real exit code
    despite Azure reporting ARM-level success even when the script fails: ``set -e``
    aborts on the first failure (marker never prints → response_code stays -1); on
    full success the trailing marker prints the exit status."""
    return ["set -e"] + list(commands) + [f'echo "{_RUN_CMD_MARKER}=$?"']


def _finalize_run_result(stdout: str, stderr: str) -> dict:
    """Turn parsed (stdout, stderr) into the ssm_send_command-shaped result: pull the
    exit-code marker out of stdout so callers get a real {status, response_code} and
    treat both clouds identically."""
    response_code = -1
    m = re.search(rf"{_RUN_CMD_MARKER}=(\d+)", stdout or "")
    if m:
        response_code = int(m.group(1))
        stdout = stdout.replace(m.group(0), "").rstrip()
    return {
        "status": "Success" if response_code == 0 else "Failed",
        "response_code": response_code,
        "stdout": stdout,
        "stderr": stderr or "",
    }


def _run_vm_command_sync(cred, sub_id: str, rg: str, vm_name: str,
                         commands: list, timeout: int) -> dict:
    compute = _get_compute(cred, sub_id)
    poller = compute.virtual_machines.begin_run_command(
        rg, vm_name, RunCommandInput(command_id="RunShellScript",
                                     script=_run_command_script(commands)))
    result = poller.result(timeout=timeout)
    stdout, stderr = _parse_run_command_output(result)
    return _finalize_run_result(stdout, stderr)


async def vm_run_command(rg: str, vm_name: str, commands: list, *,
                         timeout: int = 300) -> dict:
    """Run shell ``commands`` in a VM's guest via Azure VM Run Command and wait for
    the result. Returns ``{status, response_code, stdout, stderr}`` — the same shape
    as :func:`aws_service.ssm_send_command`: a non-Success status (or non-zero
    response_code) is surfaced to the caller rather than raised, so callers decide
    whether an in-guest failure is fatal. Only a transport/SDK error raises."""
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(
            _run_vm_command_sync, cred, sub_id, rg, vm_name, commands, timeout)
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Azure VM Run Command on {vm_name} failed: {e}") from e


def _create_image_from_vm_sync(cred, sub_id: str, rg: str, vm_name: str, image_name: str, generalize: bool) -> dict:
    compute = _get_compute(cred, sub_id)

    if generalize:
        compute.virtual_machines.begin_deallocate(rg, vm_name).result()
        compute.virtual_machines.generalize(rg, vm_name)

    vm = compute.virtual_machines.get(rg, vm_name)
    image_params = {
        "location": vm.location,
        "source_virtual_machine": {"id": vm.id},
        "tags": {"managed-by": "vm-dashboard"},
    }

    img = compute.images.begin_create_or_update(rg, image_name, image_params).result()
    return {"image_id": img.id, "name": img.name, "state": img.provisioning_state}


async def create_image_from_vm(rg: str, vm_name: str, image_name: str, generalize: bool) -> dict:
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(
            _create_image_from_vm_sync, cred, sub_id, rg, vm_name, image_name, generalize
        )
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to create image from VM {vm_name}: {e}") from e


# ── Network options ────────────────────────────────────────────────────────────

def _get_ssh_keys_sync(cred, sub_id: str, rg: str) -> list:
    """List SSH public key resources from the resource group."""
    compute = _get_compute(cred, sub_id)
    ssh_keys = []
    try:
        for key_resource in compute.ssh_public_keys.list_by_resource_group(rg):
            ssh_keys.append({
                "id": key_resource.id,
                "name": key_resource.name,
                "public_key": key_resource.public_key,
                "resource_group": rg,
            })
        logger.info("SSH: found %d SSH public keys in RG=%s", len(ssh_keys), rg)
    except Exception as e:
        logger.warning("Failed to list SSH keys from rg=%s: %s", rg, e, exc_info=True)
    return ssh_keys


def _get_network_options_sync(cred, sub_id: str, location: str, vnet_rg: str, rg: str) -> dict:
    network = _get_network(cred, sub_id)
    compute = _get_compute(cred, sub_id)
    warnings: list[str] = []

    def _status_code(exc: Exception):
        return (
            getattr(exc, "status_code", None)
            or getattr(getattr(exc, "response", None), "status_code", None)
        )

    try:
        sizes_raw = sorted(
            [s for s in compute.virtual_machine_sizes.list(location)
             if s.name.startswith(("Standard_B", "Standard_D", "Standard_E", "Standard_F"))],
            key=lambda s: (s.number_of_cores, s.memory_in_mb)
        )
        sizes = [s.name for s in sizes_raw]
    except Exception as e:
        logger.warning("Failed to list VM sizes for location=%s: %s", location, e)
        sizes = []
        warnings.append(f"VM sizes for location '{location}' could not be listed: {e}.")

    # Location dropdown for the Azure deploy form — from the shared region catalog.
    locations = region_catalog.region_ids("azure")

    subnets = []
    search_rg = vnet_rg or rg
    logger.info("Network options: searching for subnets in RG=%s", search_rg)
    try:
        vnets = list(network.virtual_networks.list(search_rg))
        logger.info("Network options: found %d VNets in RG=%s", len(vnets), search_rg)
        for vnet in vnets:
            # Only surface subnets whose VNet is in the requested region — a NIC
            # can't attach to a subnet in another region (the cross-region failure).
            if location and _normalize_region(vnet.location) != _normalize_region(location):
                continue
            logger.info("Network options: VNet %s (%s) has %d subnets", vnet.name, vnet.location, len(vnet.subnets or []))
            for subnet in (vnet.subnets or []):
                subnets.append({
                    "id": subnet.id,
                    "name": subnet.name,
                    "address_prefix": subnet.address_prefix or "",
                    "vnet_name": vnet.name,
                    # Delegated subnets (e.g. aci-subnet → ContainerInstance) can't
                    # host VM NICs; surface this so the Desktops picker can guard.
                    "delegations": [d.service_name for d in (subnet.delegations or [])],
                })
    except Exception as e:
        logger.warning("Failed to list subnets from rg=%s: %s", search_rg, e, exc_info=True)
        if _status_code(e) in (401, 403):
            warnings.append(
                f"Subnets in resource group '{search_rg}' are inaccessible: {e}. "
                f"Grant the dashboard service principal Reader on that resource group."
            )
        else:
            warnings.append(f"Subnets in resource group '{search_rg}' could not be listed: {e}.")

    nsgs = []
    try:
        nsgs_list = list(network.network_security_groups.list(search_rg))
        logger.info("Network options: found %d NSGs in RG=%s", len(nsgs_list), search_rg)
        for nsg in nsgs_list:
            if location and _normalize_region(nsg.location) != _normalize_region(location):
                continue
            nsgs.append({"id": nsg.id, "name": nsg.name, "resource_group": search_rg})
    except Exception as e:
        logger.warning("Failed to list NSGs from rg=%s: %s", search_rg, e, exc_info=True)
        if _status_code(e) in (401, 403):
            warnings.append(
                f"Network Security Groups in resource group '{search_rg}' are inaccessible: {e}. "
                f"Grant the dashboard service principal Reader on that resource group."
            )
        else:
            warnings.append(f"Network Security Groups in resource group '{search_rg}' could not be listed: {e}.")

    ssh_keys = _get_ssh_keys_sync(cred, sub_id, rg)

    logger.info("Network options: returning subnets=%d, nsgs=%d, ssh_keys=%d, locations=%d, sizes=%d",
                len(subnets), len(nsgs), len(ssh_keys), len(locations), len(sizes))
    return {
        "location": location,
        "locations": locations,
        "vm_sizes": sizes[:50],
        "subnets": subnets,
        "nsgs": nsgs,
        "ssh_keys": ssh_keys,
        "warnings": warnings,
    }


async def get_network_options(location: str, vnet_rg: str, rg: str) -> dict:
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(_get_network_options_sync, cred, sub_id, location, vnet_rg, rg)
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to get network options: {e}") from e


# ── ACI Jumpoint (mirrors ECS Jumpoint in aws_service.py) ─────────────────────

_JUMPOINT_CONTAINER_GROUP_PREFIX = "bt-jumpoint-azure"


def _get_storage_account_key_sync(cred, sub_id: str, rg: str, account_name: str) -> str:
    storage_client = StorageManagementClient(cred, sub_id)
    result = storage_client.storage_accounts.list_keys(rg, account_name)
    # azure-mgmt-storage 25.x models are Mapping-like: attribute access to
    # ``result.keys`` resolves to the dict ``keys()`` METHOD, not the
    # StorageAccountKey list (``keys.keys[0]`` → "'method' object is not
    # subscriptable"). Item access returns the list; each element exposes
    # ``.value`` (also item-accessible on older/newer models).
    first = result["keys"][0]
    return first["value"] if isinstance(first, dict) else first.value


def _run_aci_jumpoint_sync(
    cred, sub_id: str, rg: str, location: str, subnet_id: str,
    image: str, cpu: float, memory: float, deploy_key: str,
    acr_server: str = "", acr_username: str = "", acr_password: str = "",
    storage_account: str = "", storage_account_key: str = "", file_share: str = "jpt",
) -> str:
    aci = _get_aci(cred, sub_id)
    group_name = f"{_JUMPOINT_CONTAINER_GROUP_PREFIX}-{uuid.uuid4().hex[:8]}"

    # ACI ImageRegistryCredential matches by server prefix in the image URL.
    # If the image name doesn't already include the ACR server, prepend it so
    # the credential is actually used — otherwise ACI treats it as Docker Hub.
    if acr_server and not image.startswith(acr_server):
        image = f"{acr_server}/{image}"

    env_vars = []
    if deploy_key:
        env_vars.append(EnvironmentVariable(name="DEPLOY_KEY", value=deploy_key))

    volumes = []
    volume_mounts = []
    if storage_account and storage_account_key:
        volumes.append(Volume(
            name="jpt",
            azure_file=AzureFileVolume(
                share_name=file_share,
                storage_account_name=storage_account,
                storage_account_key=storage_account_key,
            ),
        ))
        volume_mounts.append(VolumeMount(name="jpt", mount_path="/jpt"))

    container = Container(
        name="jumpoint",
        image=image,
        resources=ResourceRequirements(
            requests=ResourceRequests(cpu=cpu, memory_in_gb=memory)
        ),
        environment_variables=env_vars,
        volume_mounts=volume_mounts or None,
    )

    group_params = ContainerGroup(
        location=location,
        containers=[container],
        os_type=OperatingSystemTypes.LINUX,
        restart_policy="Always",
        volumes=volumes or None,
        tags={"managed-by": "vm-dashboard"},
    )

    if subnet_id:
        from azure.mgmt.containerinstance.models import ContainerGroupSubnetId
        group_params.subnet_ids = [ContainerGroupSubnetId(id=subnet_id)]

    if acr_server and acr_username and acr_password:
        from azure.mgmt.containerinstance.models import ImageRegistryCredential
        group_params.image_registry_credentials = [
            ImageRegistryCredential(server=acr_server, username=acr_username, password=acr_password)
        ]

    aci.container_groups.begin_create_or_update(rg, group_name, group_params).result()
    return group_name


async def run_aci_jumpoint_task(
    rg: str, location: str, subnet_id: str,
    image: str, cpu: float, memory: float, deploy_key: str,
    acr_server: str = "", acr_username: str = "", acr_password: str = "",
    storage_account: str = "", storage_account_rg: str = "", file_share: str = "jpt",
) -> str:
    """Start ACI Jumpoint container. Returns container group name."""
    try:
        cred, sub_id = await _ensure_creds()
        storage_key = ""
        if storage_account:
            # Storage account may be in a different RG than the ACI container group.
            # The /jpt mount only persists the jumpoint's identity — it must never
            # block the jumpoint itself (the critical Shell Jump broker). Fetch the
            # key best-effort: on failure, log and start without the mount.
            try:
                storage_key = await asyncio.to_thread(
                    _get_storage_account_key_sync, cred, sub_id,
                    storage_account_rg or rg, storage_account
                )
            except Exception as e:
                logger.warning(
                    "ACI jumpoint: storage-key fetch for /jpt persistence failed "
                    "(%s) — starting jumpoint without persistence", e)
                storage_account = ""  # skip the mount in _run_aci_jumpoint_sync
        return await asyncio.to_thread(
            _run_aci_jumpoint_sync,
            cred, sub_id, rg, location, subnet_id, image, cpu, memory, deploy_key,
            acr_server, acr_username, acr_password,
            storage_account, storage_key, file_share,
        )
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to start ACI Jumpoint: {e}") from e


def _stop_aci_jumpoint_sync(cred, sub_id: str, rg: str, group_name: str) -> None:
    aci = _get_aci(cred, sub_id)
    try:
        aci.container_groups.begin_delete(rg, group_name).result()
    except ResourceNotFoundError:
        pass


async def stop_aci_jumpoint_task(rg: str, group_name: str) -> None:
    try:
        cred, sub_id = await _ensure_creds()
        await asyncio.to_thread(_stop_aci_jumpoint_sync, cred, sub_id, rg, group_name)
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to stop ACI Jumpoint {group_name}: {e}") from e


def _list_aci_tasks_sync(cred, sub_id: str, rg: str) -> list:
    aci = _get_aci(cred, sub_id)
    results = []
    for group in aci.container_groups.list_by_resource_group(rg):
        tags = group.tags or {}
        if not _is_dashboard_managed(tags):
            continue
        if not group.name.startswith(_JUMPOINT_CONTAINER_GROUP_PREFIX):
            continue
        results.append({
            "group_name": group.name,
            "state": group.provisioning_state or "",
            "location": group.location or "",
        })
    return results


async def list_aci_tasks(rg: str) -> list:
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(_list_aci_tasks_sync, cred, sub_id, rg)
    except Exception as e:
        raise AzureError(f"Failed to list ACI tasks: {e}") from e


# ── ACI Ansible runner (mirrors ECS Fargate Ansible in aws_service.py) ────────

_ANSIBLE_RUNNER_PREFIX = "ansible-runner"


def _run_aci_ansible_sync(
    cred, sub_id: str, rg: str, location: str, subnet_id: str,
    image: str, target_ip: str, ansible_user: str,
    playbook_b64: str, ssh_key_b64: str, job_id: str,
    acr_server: str = "", acr_username: str = "", acr_password: str = "",
    secret_entries: list | None = None, manifest_b64: str = "",
    ps_env: dict | None = None,
) -> tuple:
    """
    Create an ACI container group that runs a single Ansible playbook, wait for
    it to finish, return (exit_code, log_output), and delete the group.

    Secrets: each ``secret_entries`` item ``{env, value}`` is passed as a
    ``secure_value`` env (hidden from the portal); ``manifest_b64`` maps env→var
    so the task command builds a 0600 vars file consumed via ``-e @file``.
    """
    import time
    from . import cloud_ansible_secrets as _cas

    aci = _get_aci(cred, sub_id)
    group_name = f"{_ANSIBLE_RUNNER_PREFIX}-{job_id[:8]}"

    _secret_prefix = _cas.command_prefix() if manifest_b64 else ""
    _secret_ev = _cas.extra_vars_arg() if manifest_b64 else ""
    cmd = (
        "set -e && "
        "echo \"$PLAYBOOK_B64\" | base64 -d > /tmp/playbook.yml && "
        "echo \"$SSH_KEY_B64\" | base64 -d > /tmp/ssh_key && "
        "chmod 600 /tmp/ssh_key && "
        + _secret_prefix +
        f"ansible-playbook -i '{target_ip},' "
        "--forks 1 "
        f"-u {ansible_user} "
        "--private-key /tmp/ssh_key "
        + _secret_ev +
        "--ssh-extra-args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null' "
        "/tmp/playbook.yml"
    )

    container = Container(
        name="ansible",
        image=image,
        resources=ResourceRequirements(requests=ResourceRequests(cpu=1.0, memory_in_gb=1.0)),
        command=["sh", "-c", cmd],
        environment_variables=(
            [
                EnvironmentVariable(name="PLAYBOOK_B64", value=playbook_b64),
                EnvironmentVariable(name="SSH_KEY_B64", secure_value=ssh_key_b64),
            ]
            + ([EnvironmentVariable(name=_cas.MANIFEST_ENV, value=manifest_b64)]
               if manifest_b64 else [])
            + [EnvironmentVariable(name=e["env"], secure_value=e["value"])
               for e in (secret_entries or [])]
            # PASSWORD_SAFE_* for an in-playbook beyondtrust.secrets_safe lookup — all
            # passed as secure_value (hidden from the portal); harmless for URL/ID.
            + [EnvironmentVariable(name=k, secure_value=v) for k, v in (ps_env or {}).items()]
        ),
    )

    group_params = ContainerGroup(
        location=location,
        containers=[container],
        os_type=OperatingSystemTypes.LINUX,
        restart_policy="Never",
        tags={"managed-by": "vm-dashboard", "Purpose": "ansible-runner"},
    )

    if subnet_id:
        from azure.mgmt.containerinstance.models import ContainerGroupSubnetId
        group_params.subnet_ids = [ContainerGroupSubnetId(id=subnet_id)]

    if acr_server and acr_username and acr_password:
        from azure.mgmt.containerinstance.models import ImageRegistryCredential
        group_params.image_registry_credentials = [
            ImageRegistryCredential(server=acr_server, username=acr_username, password=acr_password)
        ]

    # Poll until the container exits (max 20 min)
    output = ""
    exit_code = 1
    state = ""
    try:
        logger.info("ACI Ansible: creating container group %s in %s", group_name, rg)
        # Keep create inside the try: when the container can't start, .result()
        # raises and the finally below still deletes the failed group instead
        # of leaking it in the subscription.
        aci.container_groups.begin_create_or_update(rg, group_name, group_params).result()

        for _ in range(120):
            cg = aci.container_groups.get(rg, group_name)
            state = (cg.instance_view.state if cg.instance_view else "") or ""
            if state in ("Succeeded", "Failed", "Stopped"):
                break
            time.sleep(10)

        # Retrieve logs
        try:
            log_resp = aci.containers.list_logs(rg, group_name, "ansible")
            output = log_resp.content or ""
        except Exception as log_err:
            logger.warning("ACI Ansible: could not retrieve logs: %s", log_err)

        # Retrieve exit code from container instance view
        try:
            cg = aci.container_groups.get(rg, group_name)
            for c in (cg.containers or []):
                if c.name == "ansible" and c.instance_view and c.instance_view.current_state:
                    ec = c.instance_view.current_state.exit_code
                    exit_code = ec if ec is not None else (0 if state == "Succeeded" else 1)
                    break
            else:
                exit_code = 0 if state == "Succeeded" else 1
        except Exception as ec_err:
            logger.warning("ACI Ansible: could not get exit code: %s", ec_err)
            exit_code = 0 if state == "Succeeded" else 1

    finally:
        # Always delete the runner container group
        try:
            aci.container_groups.begin_delete(rg, group_name).result()
            logger.info("ACI Ansible: deleted container group %s", group_name)
        except Exception as del_err:
            logger.warning("ACI Ansible: could not delete group %s: %s", group_name, del_err)

    return exit_code, output


async def run_aci_ansible_task(
    rg: str, location: str, subnet_id: str, image: str,
    target_ip: str, ansible_user: str,
    playbook_b64: str, ssh_key_b64: str, job_id: str,
    acr_server: str = "", acr_username: str = "", acr_password: str = "",
    secret_entries: list | None = None, manifest_b64: str = "",
    ps_env: dict | None = None,
) -> tuple:
    """
    Run an Ansible playbook inside the Azure VNet via ACI.
    Returns (exit_code, output_log).
    """
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(
            _run_aci_ansible_sync,
            cred, sub_id, rg, location, subnet_id, image,
            target_ip, ansible_user, playbook_b64, ssh_key_b64, job_id,
            acr_server, acr_username, acr_password,
            secret_entries, manifest_b64, ps_env,
        )
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to run ACI Ansible task: {e}") from e


# ── ACI Kubernetes runner ─────────────────────────────────────────────────────

_K8S_RUNNER_PREFIX = "k8s-runner"


def _run_aci_k8s_sync(
    cred, sub_id: str, rg: str, location: str, subnet_id: str,
    image: str, command: str, kubeconfig_b64: str, stdin_b64: str, job_id: str,
    acr_server: str = "", acr_username: str = "", acr_password: str = "",
) -> tuple:
    """
    Create an ACI container group that runs a single kubectl/helm command against
    a cluster's API, wait for it to finish, return (exit_code, log_output), and
    delete the group.

    Modelled on `_run_aci_ansible_sync` — same create / poll / logs / exit-code /
    cleanup shape. The stock kubectl+helm `image`, the generic shell `command`,
    and the kubeconfig (decoded from the ``KUBECONFIG_B64`` secure env into
    ``$KUBECONFIG``) are the only differences.
    """
    import time

    aci = _get_aci(cred, sub_id)
    group_name = f"{_K8S_RUNNER_PREFIX}-{job_id[:8]}" if job_id else f"{_K8S_RUNNER_PREFIX}-adhoc"

    setup = (
        "set -e; "
        'printf %s "$KUBECONFIG_B64" | base64 -d > /tmp/kubeconfig; '
        "export KUBECONFIG=/tmp/kubeconfig; "
    )
    if stdin_b64:
        full_cmd = setup + 'printf %s "$STDIN_B64" | base64 -d | ' + command
    else:
        full_cmd = setup + command

    env_vars = [EnvironmentVariable(name="KUBECONFIG_B64", secure_value=kubeconfig_b64)]
    if stdin_b64:
        env_vars.append(EnvironmentVariable(name="STDIN_B64", secure_value=stdin_b64))

    container = Container(
        name="k8s",
        image=image,
        resources=ResourceRequirements(requests=ResourceRequests(cpu=1.0, memory_in_gb=1.0)),
        command=["sh", "-c", full_cmd],
        environment_variables=env_vars,
    )

    group_params = ContainerGroup(
        location=location,
        containers=[container],
        os_type=OperatingSystemTypes.LINUX,
        restart_policy="Never",
        tags={"managed-by": "vm-dashboard", "Purpose": "k8s-runner"},
    )

    if subnet_id:
        from azure.mgmt.containerinstance.models import ContainerGroupSubnetId
        group_params.subnet_ids = [ContainerGroupSubnetId(id=subnet_id)]

    if acr_server and acr_username and acr_password:
        from azure.mgmt.containerinstance.models import ImageRegistryCredential
        group_params.image_registry_credentials = [
            ImageRegistryCredential(server=acr_server, username=acr_username, password=acr_password)
        ]

    # Poll until the container exits (max 20 min)
    output = ""
    exit_code = 1
    state = ""
    try:
        logger.info("ACI k8s: creating container group %s in %s", group_name, rg)
        # Keep create inside the try: when the container can't start, .result()
        # raises and the finally below still deletes the failed group instead
        # of leaking it in the subscription.
        aci.container_groups.begin_create_or_update(rg, group_name, group_params).result()

        for _ in range(120):
            cg = aci.container_groups.get(rg, group_name)
            state = (cg.instance_view.state if cg.instance_view else "") or ""
            if state in ("Succeeded", "Failed", "Stopped"):
                break
            time.sleep(10)

        # Retrieve logs
        try:
            log_resp = aci.containers.list_logs(rg, group_name, "k8s")
            output = log_resp.content or ""
        except Exception as log_err:
            logger.warning("ACI k8s: could not retrieve logs: %s", log_err)

        # Retrieve exit code from container instance view
        try:
            cg = aci.container_groups.get(rg, group_name)
            for c in (cg.containers or []):
                if c.name == "k8s" and c.instance_view and c.instance_view.current_state:
                    ec = c.instance_view.current_state.exit_code
                    exit_code = ec if ec is not None else (0 if state == "Succeeded" else 1)
                    break
            else:
                exit_code = 0 if state == "Succeeded" else 1
        except Exception as ec_err:
            logger.warning("ACI k8s: could not get exit code: %s", ec_err)
            exit_code = 0 if state == "Succeeded" else 1

    finally:
        # Always delete the runner container group
        try:
            aci.container_groups.begin_delete(rg, group_name).result()
            logger.info("ACI k8s: deleted container group %s", group_name)
        except Exception as del_err:
            logger.warning("ACI k8s: could not delete group %s: %s", group_name, del_err)

    return exit_code, output


async def run_aci_k8s_task(
    *,
    rg: str, location: str, subnet_id: str, image: str,
    command: str, kubeconfig_b64: str, stdin_b64: str = "", job_id: str,
    acr_server: str = "", acr_username: str = "", acr_password: str = "",
) -> tuple:
    """
    Run a kubectl/helm command against a cluster's API inside the Azure VNet via ACI.
    Returns (exit_code, output_log).
    """
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(
            _run_aci_k8s_sync,
            cred, sub_id, rg, location, subnet_id, image,
            command, kubeconfig_b64, stdin_b64, job_id,
            acr_server, acr_username, acr_password,
        )
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to run ACI k8s task: {e}") from e


# ── ACI Ansible localhost runner (Kubernetes-cluster / cloud-database targets) ──

def _run_aci_ansible_local_sync(
    cred, sub_id: str, rg: str, location: str, subnet_id: str,
    image: str, playbook_b64: str, conn_vars_b64: str, kubeconfig_b64: str,
    job_id: str,
    acr_server: str = "", acr_username: str = "", acr_password: str = "",
    ps_env: dict | None = None,
) -> tuple:
    """Create an ACI container group that runs a **localhost** Ansible play (the
    k8s/DB path — Ansible reaches OUT to the cluster API / DB endpoint instead of
    SSHing to a VM), wait for it, return (exit_code, log_output), and delete the
    group. Mirrors ``_run_aci_ansible_sync``; the command is localhost (no SSH key)
    and the connection material rides ``secure_value`` env vars (hidden in the
    portal). Uses the ansible-cloud image (k8s/DB collections + client libs)."""
    import time
    from .ansible_localhost_cmd import build_localhost_command

    aci = _get_aci(cred, sub_id)
    group_name = f"{_ANSIBLE_RUNNER_PREFIX}-{job_id[:8]}" if job_id else f"{_ANSIBLE_RUNNER_PREFIX}-adhoc"

    cmd = build_localhost_command(
        with_conn_vars=bool(conn_vars_b64), with_kubeconfig=bool(kubeconfig_b64))

    env_vars = [EnvironmentVariable(name="PLAYBOOK_B64", value=playbook_b64)]
    if conn_vars_b64:
        env_vars.append(EnvironmentVariable(name="CONN_VARS_B64", secure_value=conn_vars_b64))
    if kubeconfig_b64:
        env_vars.append(EnvironmentVariable(name="KUBECONFIG_B64", secure_value=kubeconfig_b64))
    # PASSWORD_SAFE_* for an in-playbook beyondtrust.secrets_safe lookup — secure_value.
    for k, v in (ps_env or {}).items():
        env_vars.append(EnvironmentVariable(name=k, secure_value=v))

    container = Container(
        name="ansible",
        image=image,
        resources=ResourceRequirements(requests=ResourceRequests(cpu=1.0, memory_in_gb=1.0)),
        command=["sh", "-c", cmd],
        environment_variables=env_vars,
    )

    group_params = ContainerGroup(
        location=location,
        containers=[container],
        os_type=OperatingSystemTypes.LINUX,
        restart_policy="Never",
        tags={"managed-by": "vm-dashboard", "Purpose": "ansible-runner"},
    )

    if subnet_id:
        from azure.mgmt.containerinstance.models import ContainerGroupSubnetId
        group_params.subnet_ids = [ContainerGroupSubnetId(id=subnet_id)]

    if acr_server and acr_username and acr_password:
        from azure.mgmt.containerinstance.models import ImageRegistryCredential
        group_params.image_registry_credentials = [
            ImageRegistryCredential(server=acr_server, username=acr_username, password=acr_password)
        ]

    output = ""
    exit_code = 1
    state = ""
    try:
        logger.info("ACI ansible-local: creating container group %s in %s", group_name, rg)
        aci.container_groups.begin_create_or_update(rg, group_name, group_params).result()

        for _ in range(120):
            cg = aci.container_groups.get(rg, group_name)
            state = (cg.instance_view.state if cg.instance_view else "") or ""
            if state in ("Succeeded", "Failed", "Stopped"):
                break
            time.sleep(10)

        try:
            log_resp = aci.containers.list_logs(rg, group_name, "ansible")
            output = log_resp.content or ""
        except Exception as log_err:
            logger.warning("ACI ansible-local: could not retrieve logs: %s", log_err)

        try:
            cg = aci.container_groups.get(rg, group_name)
            for c in (cg.containers or []):
                if c.name == "ansible" and c.instance_view and c.instance_view.current_state:
                    ec = c.instance_view.current_state.exit_code
                    exit_code = ec if ec is not None else (0 if state == "Succeeded" else 1)
                    break
            else:
                exit_code = 0 if state == "Succeeded" else 1
        except Exception as ec_err:
            logger.warning("ACI ansible-local: could not get exit code: %s", ec_err)
            exit_code = 0 if state == "Succeeded" else 1

    finally:
        try:
            aci.container_groups.begin_delete(rg, group_name).result()
            logger.info("ACI ansible-local: deleted container group %s", group_name)
        except Exception as del_err:
            logger.warning("ACI ansible-local: could not delete group %s: %s", group_name, del_err)

    return exit_code, output


async def run_aci_ansible_local_task(
    *,
    rg: str, location: str, subnet_id: str, image: str,
    playbook_b64: str, conn_vars_b64: str = "", kubeconfig_b64: str = "", job_id: str,
    acr_server: str = "", acr_username: str = "", acr_password: str = "",
    ps_env: dict | None = None,
) -> tuple:
    """Run a localhost Ansible play (k8s/DB target) inside the Azure VNet via ACI.
    Returns (exit_code, output_log)."""
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(
            _run_aci_ansible_local_sync,
            cred, sub_id, rg, location, subnet_id, image,
            playbook_b64, conn_vars_b64, kubeconfig_b64, job_id,
            acr_server, acr_username, acr_password, ps_env,
        )
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to run ACI ansible-local task: {e}") from e


# ── Generic Docker Compose → ACI container group ─────────────────────────────

# Map compose restart policies onto the single group-level ACI policy.
_ACI_RESTART_MAP = {
    "always": "Always",
    "unless-stopped": "Always",
    "on-failure": "OnFailure",
    "no": "Never",
}


def _deploy_compose_aci_sync(
    cred, sub_id: str, rg: str, location: str, name: str, services: list,
    subnet_id: str = "", acr_server: str = "", acr_username: str = "",
    acr_password: str = "", default_cpu: float = 1.0, default_memory_gb: float = 1.0,
) -> dict:
    """Create an ACI container group from a parsed compose spec — one Container
    per compose service. Returns the created group's info. Unlike the runner
    groups this is a user workload, so it is left running (not deleted)."""
    from azure.mgmt.containerinstance.models import (
        ContainerPort, Port, IpAddress,
    )

    aci = _get_aci(cred, sub_id)

    containers = []
    group_ports = []
    restart_policy = "Always"
    for svc in services:
        cpu = svc.cpu if svc.cpu else default_cpu
        mem_gb = (svc.memory_mb / 1024.0) if svc.memory_mb else default_memory_gb
        env_vars = [EnvironmentVariable(name=k, value=v) for k, v in svc.env]
        container_ports = []
        for _host, container_p, proto in svc.ports:
            container_ports.append(ContainerPort(port=container_p, protocol=proto.upper()))
            group_ports.append(Port(port=container_p, protocol=proto.upper()))
        # ACI's `command` is the full exec form (it replaces the image
        # entrypoint+cmd), so concatenate compose entrypoint + command. Supplying
        # both makes ACI behave identically to ECS/GCE for entrypoint images.
        aci_command = (svc.entrypoint or []) + (svc.command or [])
        containers.append(Container(
            name=svc.name,
            image=svc.image,
            resources=ResourceRequirements(
                requests=ResourceRequests(cpu=cpu, memory_in_gb=round(mem_gb, 2))
            ),
            command=aci_command or None,
            environment_variables=env_vars or None,
            ports=container_ports or None,
        ))
        if svc.restart:
            restart_policy = _ACI_RESTART_MAP.get(svc.restart, restart_policy)

    group_params = ContainerGroup(
        location=location,
        containers=containers,
        os_type=OperatingSystemTypes.LINUX,
        restart_policy=restart_policy,
        tags={"managed-by": "vm-dashboard", "Purpose": "compose"},
    )

    if group_ports:
        # VNet-injected groups get a Private frontend; otherwise expose publicly.
        group_params.ip_address = IpAddress(
            ports=group_ports,
            type="Private" if subnet_id else "Public",
        )

    if subnet_id:
        from azure.mgmt.containerinstance.models import ContainerGroupSubnetId
        group_params.subnet_ids = [ContainerGroupSubnetId(id=subnet_id)]

    if acr_server and acr_username and acr_password:
        from azure.mgmt.containerinstance.models import ImageRegistryCredential
        group_params.image_registry_credentials = [
            ImageRegistryCredential(server=acr_server, username=acr_username, password=acr_password)
        ]

    logger.info("ACI compose: creating container group %s (%d services) in %s", name, len(containers), rg)
    cg = aci.container_groups.begin_create_or_update(rg, name, group_params).result()
    ip = cg.ip_address.ip if cg.ip_address else ""
    return {
        "container_group_name": name,
        "resource_group": rg,
        "state": (cg.instance_view.state if cg.instance_view else "") or "Pending",
        "ip_address": ip,
        "containers": [c.name for c in (cg.containers or [])],
    }


async def deploy_compose_aci(
    rg: str, location: str, name: str, services: list,
    subnet_id: str = "", acr_server: str = "", acr_username: str = "",
    acr_password: str = "", default_cpu: float = 1.0, default_memory_gb: float = 1.0,
) -> dict:
    """Deploy a parsed compose spec to a new ACI container group."""
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(
            _deploy_compose_aci_sync,
            cred, sub_id, rg, location, name, services,
            subnet_id, acr_server, acr_username, acr_password,
            default_cpu, default_memory_gb,
        )
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to deploy compose to ACI: {e}") from e


def _list_aci_container_instances_sync(cred, sub_id: str, rg: str) -> list:
    """List all ACI container instances (not filtered by tag/prefix)."""
    aci = _get_aci(cred, sub_id)
    results = []
    try:
        groups = list(aci.container_groups.list_by_resource_group(rg))
    except Exception as e:
        logger.warning("Failed to list ACI container groups in RG=%s: %s", rg, e, exc_info=True)
        return results
    for group in groups:
        try:
            containers = []
            if group.containers:
                for container in group.containers:
                    img = container.image or "unknown"
                    containers.append(f"{container.name} ({img})")
            cpu = 0.0
            memory = 0.0
            if group.containers and group.containers[0].resources and group.containers[0].resources.requests:
                cpu = float(group.containers[0].resources.requests.cpu or 0)
                memory = float(group.containers[0].resources.requests.memory_in_gb or 0)
            # Use provisioning_state; instance_view requires a separate get() call
            state = group.provisioning_state or "Unknown"
            results.append({
                "id": group.id or "",
                "name": group.name or "",
                "resource_group": rg,
                "state": state,
                "os_type": _os_type_str(group.os_type) if group.os_type else "Linux",
                "cpu": cpu,
                "memory": memory,
                "containers": containers,
                "created_at": None,
                "restart_policy": group.restart_policy if hasattr(group, "restart_policy") else "OnFailure",
            })
        except Exception as e:
            logger.warning("Failed to parse ACI container group %s: %s", getattr(group, "name", "?"), e)
    return results


async def list_aci_container_instances(rg: str) -> list:
    """List all ACI container instances in resource group."""
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(_list_aci_container_instances_sync, cred, sub_id, rg)
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to list ACI container instances: {e}") from e


def _stop_aci_container_group_sync(cred, sub_id: str, rg: str, group_name: str) -> None:
    """Stop a container group by deleting it."""
    aci = _get_aci(cred, sub_id)
    try:
        aci.container_groups.begin_delete(rg, group_name).result()
        logger.info("Stopped ACI container group %s", group_name)
    except Exception as e:
        raise AzureError(f"Failed to stop ACI container {group_name}: {e}") from e


async def stop_aci_container_group(rg: str, container_group_name: str) -> None:
    """Stop (delete) an ACI container group."""
    try:
        cred, sub_id = await _ensure_creds()
        await asyncio.to_thread(_stop_aci_container_group_sync, cred, sub_id, rg, container_group_name)
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to stop ACI container group: {e}") from e


# ── Export managed image to VHD blob (portable artefact) ──────────────────────

def _export_managed_image_to_vhd_sync(
    cred,
    sub_id: str,
    image_rg: str,
    image_name: str,
    dest_storage_account: str,
    dest_container: str,
    dest_blob_name: str,
    sas_duration_seconds: int,
    poll_interval: int,
    timeout: int,
    progress_cb,
) -> dict:
    """Snapshot the image's OS disk, grant SAS access, server-side copy to blob.

    The managed image format is not directly downloadable; the canonical export
    pattern is snapshot → SAS → server-side blob copy → revoke + cleanup. The
    snapshot and SAS access are torn down on success or failure.

    Returns {blob_url, format, snapshot_name}.
    """
    import time
    from azure.mgmt.compute.models import GrantAccessData, Snapshot, CreationData
    from azure.storage.blob import BlobServiceClient

    compute = _get_compute(cred, sub_id)

    image = compute.images.get(image_rg, image_name)
    os_disk = image.storage_profile.os_disk
    location = image.location

    # Identify the underlying source (managed disk, snapshot, or unmanaged blob).
    source_disk_id = None
    if os_disk.managed_disk and os_disk.managed_disk.id:
        source_disk_id = os_disk.managed_disk.id
    elif os_disk.snapshot and os_disk.snapshot.id:
        source_disk_id = os_disk.snapshot.id
    elif os_disk.blob_uri:
        if progress_cb:
            progress_cb(f"Image is unmanaged — copying blob {os_disk.blob_uri} directly")
        return _copy_unmanaged_blob_sync(
            cred, os_disk.blob_uri, dest_storage_account, dest_container,
            dest_blob_name, poll_interval, timeout, progress_cb,
        )
    else:
        raise AzureError(f"Image {image_name} has no recognizable OS disk source for export")

    # 1. Take a snapshot of the source disk (works for both Disk and Snapshot IDs —
    #    snapshots created from snapshots are supported).
    snapshot_name = f"export-{image_name}-{uuid.uuid4().hex[:8]}"
    if progress_cb:
        progress_cb(f"Creating snapshot {snapshot_name} from {source_disk_id}")
    # Use the SDK model objects (not a raw dict) so the request serializes to the
    # ARM REST shape — camelCase keys under `properties`. A raw snake_case dict is
    # sent verbatim by the track2 SDK, which Azure rejects with
    # "Could not find member 'creation_data' on object of type 'ResourceDefinition'".
    snap_params = Snapshot(
        location=location,
        creation_data=CreationData(create_option="Copy", source_resource_id=source_disk_id),
        incremental=False,
    )
    compute.snapshots.begin_create_or_update(image_rg, snapshot_name, snap_params).result()

    sas_url = None
    try:
        # 2. Grant read access to the snapshot — yields a SAS-style URL we can pass
        #    to the destination blob's start_copy_from_url for a fully server-side copy.
        if progress_cb:
            progress_cb(f"Granting SAS access on {snapshot_name}")
        access_op = compute.snapshots.begin_grant_access(
            image_rg, snapshot_name,
            GrantAccessData(access="Read", duration_in_seconds=sas_duration_seconds),
        )
        access = access_op.result()
        sas_url = access.access_sas

        # 3. Kick off server-side copy to the destination blob.
        account_url = f"https://{dest_storage_account}.blob.core.windows.net"
        svc = BlobServiceClient(account_url=account_url, credential=cred)
        container_client = svc.get_container_client(dest_container)
        try:
            container_client.create_container()
        except Exception:
            pass
        blob_client = container_client.get_blob_client(dest_blob_name)

        if progress_cb:
            progress_cb(f"Starting server-side copy → {account_url}/{dest_container}/{dest_blob_name}")
        blob_client.start_copy_from_url(sas_url)

        # 4. Poll copy status.
        started = time.time()
        last_progress = ""
        while True:
            props = blob_client.get_blob_properties()
            status = (props.copy.status or "").lower()
            progress = props.copy.progress or ""
            if progress and progress != last_progress and progress_cb:
                progress_cb(f"Copy {status}: {progress}")
                last_progress = progress

            if status == "success":
                blob_url = f"{account_url}/{dest_container}/{dest_blob_name}"
                if progress_cb:
                    progress_cb(f"Copy complete: {blob_url}")
                return {"blob_url": blob_url, "format": "vhd", "snapshot_name": snapshot_name}
            if status in ("failed", "aborted"):
                raise AzureError(f"Blob copy ended in state '{status}': {props.copy.status_description}")

            if time.time() - started > timeout:
                raise AzureError(f"Blob copy timed out after {timeout}s (last status: {status})")
            time.sleep(poll_interval)

    finally:
        # Best-effort cleanup. Errors here are logged but do not mask the primary outcome.
        try:
            compute.snapshots.begin_revoke_access(image_rg, snapshot_name).wait()
        except Exception as e:
            logger.warning("Failed to revoke SAS on snapshot %s: %s", snapshot_name, e)
        try:
            compute.snapshots.begin_delete(image_rg, snapshot_name).wait()
        except Exception as e:
            logger.warning("Failed to delete snapshot %s: %s", snapshot_name, e)


def _copy_unmanaged_blob_sync(
    cred,
    source_blob_url: str,
    dest_storage_account: str,
    dest_container: str,
    dest_blob_name: str,
    poll_interval: int,
    timeout: int,
    progress_cb,
) -> dict:
    """Server-side copy from one Azure blob URL to another (unmanaged image path)."""
    import time
    from azure.storage.blob import BlobServiceClient

    account_url = f"https://{dest_storage_account}.blob.core.windows.net"
    svc = BlobServiceClient(account_url=account_url, credential=cred)
    container_client = svc.get_container_client(dest_container)
    try:
        container_client.create_container()
    except Exception:
        pass
    blob_client = container_client.get_blob_client(dest_blob_name)

    blob_client.start_copy_from_url(source_blob_url)
    started = time.time()
    while True:
        props = blob_client.get_blob_properties()
        status = (props.copy.status or "").lower()
        if status == "success":
            blob_url = f"{account_url}/{dest_container}/{dest_blob_name}"
            return {"blob_url": blob_url, "format": "vhd", "snapshot_name": ""}
        if status in ("failed", "aborted"):
            raise AzureError(f"Blob copy ended in state '{status}': {props.copy.status_description}")
        if time.time() - started > timeout:
            raise AzureError(f"Blob copy timed out after {timeout}s")
        time.sleep(poll_interval)


async def export_managed_image_to_vhd(
    image_rg: str,
    image_name: str,
    dest_storage_account: str,
    dest_container: str,
    dest_blob_name: str,
    sas_duration_seconds: int = 3600,
    poll_interval: int = 15,
    timeout: int = 7200,
    progress_cb=None,
) -> dict:
    """Export a managed image (or unmanaged image) to a VHD blob.

    Returns {blob_url, format, snapshot_name}. progress_cb is an optional sync
    callable taking a single string for streaming status into a Job log.
    """
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(
            _export_managed_image_to_vhd_sync,
            cred, sub_id, image_rg, image_name,
            dest_storage_account, dest_container, dest_blob_name,
            sas_duration_seconds, poll_interval, timeout, progress_cb,
        )
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to export image {image_name} to VHD: {e}") from e


# ── Import VHD to managed image (cross-cloud promote target side) ────────────

def _create_image_from_blob_sync(
    cred,
    sub_id: str,
    target_rg: str,
    location: str,
    image_name: str,
    blob_uri: str,
    os_type: str,
    hyper_v_generation: str,
    storage_account_id: str,
    progress_cb,
) -> dict:
    """Create a managed image from a VHD blob URI. Returns
    {resource_id, provisioning_state, name}.

    Mirrors `images.begin_create_or_update` from the Azure compute SDK. The
    blob URI must be reachable from the target subscription — usually it
    lives in `storage_account_id`'s same subscription, which the dashboard's
    AAD credential already has access to. Promotes uses this after the
    promote runner has staged the VHD in the dest container.
    """
    from azure.mgmt.compute.models import (
        Image, ImageStorageProfile, ImageOSDisk, OperatingSystemTypes as ComputeOSType,
        OperatingSystemStateTypes, HyperVGenerationTypes, SubResource,
    )

    if progress_cb:
        progress_cb(f"Creating managed image '{image_name}' in {target_rg} from {blob_uri[:80]}…")

    compute = _get_compute(cred, sub_id)

    os_t = ComputeOSType.LINUX if os_type.lower() == "linux" else ComputeOSType.WINDOWS
    gen = (
        HyperVGenerationTypes.V2
        if (hyper_v_generation or "").upper() in ("V2", "GEN2")
        else HyperVGenerationTypes.V1
    )

    os_disk_kwargs: dict = {
        "os_type": os_t,
        "os_state": OperatingSystemStateTypes.GENERALIZED,
        "blob_uri": blob_uri,
    }
    if storage_account_id:
        os_disk_kwargs["storage_account"] = SubResource(id=storage_account_id)

    img_params = Image(
        location=location,
        hyper_v_generation=gen,
        storage_profile=ImageStorageProfile(
            os_disk=ImageOSDisk(**os_disk_kwargs),
            zone_resilient=False,
        ),
        tags={"managed-by": "vm-dashboard", "Purpose": "promote-target"},
    )

    # Bounded wait: a stuck image-create (unreadable/malformed source blob) must
    # fail the promote, not hang the in-app background task forever.
    poller = compute.images.begin_create_or_update(target_rg, image_name, img_params)
    deadline = time.monotonic() + _IMAGE_CREATE_TIMEOUT_S
    while not poller.done():
        if time.monotonic() > deadline:
            raise AzureError(
                f"Managed image '{image_name}' did not finish creating within "
                f"{_IMAGE_CREATE_TIMEOUT_S // 60} min — the source VHD blob may be "
                f"unreadable by the target subscription or malformed ({blob_uri[:120]})."
            )
        poller.wait(15)
    img = poller.result()
    if progress_cb:
        progress_cb(f"Image create returned: {img.provisioning_state} ({img.id})")
    return {
        "resource_id": img.id,
        "name": img.name,
        "provisioning_state": img.provisioning_state,
    }


async def create_image_from_blob(
    target_rg: str,
    location: str,
    image_name: str,
    blob_uri: str,
    os_type: str = "Linux",
    hyper_v_generation: str = "V2",
    storage_account_id: str = "",
    progress_cb=None,
) -> dict:
    """Create a managed image from a VHD blob. Returns {resource_id, name,
    provisioning_state}. `blob_uri` should be the full HTTPS blob URL (no
    SAS — same-subscription AAD trust is sufficient when the operator's
    dashboard SP has read on the source storage account)."""
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(
            _create_image_from_blob_sync,
            cred, sub_id, target_rg, location, image_name, blob_uri,
            os_type, hyper_v_generation, storage_account_id, progress_cb,
        )
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to create image '{image_name}' from blob: {e}") from e


# ── ACI Promote-runner task ──────────────────────────────────────────────────

_PROMOTE_RUNNER_PREFIX = "promote-runner"

# ACI's `command` is the full exec form — it *replaces* the image's
# ENTRYPOINT+CMD, it does not append to the ENTRYPOINT the way AWS ECS's
# container `command` override does. So we must include the runner's
# entrypoint here ourselves; passing only the argv makes ACI try to exec
# `--source-url` as the binary ("executable file not found in $PATH").
# Must stay in lockstep with ENTRYPOINT in runners/promote/Dockerfile.
_PROMOTE_RUNNER_ENTRYPOINT = ["python", "/entrypoint.py"]


def _run_aci_promote_runner_sync(
    cred,
    sub_id: str,
    rg: str,
    location: str,
    subnet_id: str,
    image: str,
    cpu: float,
    memory_gb: float,
    runner_args: list,
    azure_env: dict,
    job_id: str,
    acr_server: str = "",
    acr_username: str = "",
    acr_password: str = "",
    poll_seconds_max: int = 7200,
) -> tuple:
    """Launch the promote-runner image as an ACI container group, wait for
    it to stop, return (exit_code, log_output), and delete the group.

    Mirrors `_run_aci_ansible_sync`. The runner takes its argv from the
    container `command` field rather than env vars; Azure credentials for
    the dest-side upload are passed as `secure_value` env vars.
    """
    import time
    aci = _get_aci(cred, sub_id)
    group_name = f"{_PROMOTE_RUNNER_PREFIX}-{job_id[:8]}"

    env_vars = []
    for k, v in (azure_env or {}).items():
        # Tenant ID and client ID aren't strictly secret but treating
        # everything uniformly keeps the ACI portal from leaking them.
        env_vars.append(EnvironmentVariable(name=k, secure_value=v))

    container = Container(
        name="promote-runner",
        image=image,
        resources=ResourceRequirements(
            requests=ResourceRequests(cpu=cpu, memory_in_gb=memory_gb),
        ),
        # ACI replaces (not appends to) the image entrypoint, so prepend the
        # runner's entrypoint ourselves — see _PROMOTE_RUNNER_ENTRYPOINT.
        command=_PROMOTE_RUNNER_ENTRYPOINT + list(runner_args),
        environment_variables=env_vars,
    )

    group_params = ContainerGroup(
        location=location,
        containers=[container],
        os_type=OperatingSystemTypes.LINUX,
        restart_policy="Never",
        tags={"managed-by": "vm-dashboard", "Purpose": "promote-runner"},
    )
    if subnet_id:
        from azure.mgmt.containerinstance.models import ContainerGroupSubnetId
        group_params.subnet_ids = [ContainerGroupSubnetId(id=subnet_id)]
    if acr_server and acr_username and acr_password:
        from azure.mgmt.containerinstance.models import ImageRegistryCredential
        group_params.image_registry_credentials = [
            ImageRegistryCredential(server=acr_server, username=acr_username, password=acr_password)
        ]

    output = ""
    exit_code = 1
    state = ""
    try:
        logger.info("ACI promote-runner: creating container group %s in %s/%s", group_name, rg, location)
        # .result() blocks until the create LRO settles. When the container
        # can't start (e.g. a bad entrypoint), this *raises* after ACI's
        # provisioning timeout — keep it inside the try so the finally still
        # deletes the failed group instead of leaking it in the subscription.
        aci.container_groups.begin_create_or_update(rg, group_name, group_params).result()

        waited = 0
        # Default 2 hours — multi-GB transfers + qemu-img convert can take a while.
        while waited < poll_seconds_max:
            cg = aci.container_groups.get(rg, group_name)
            state = (cg.instance_view.state if cg.instance_view else "") or ""
            if state in ("Succeeded", "Failed", "Stopped"):
                break
            time.sleep(10)
            waited += 10

        try:
            log_resp = aci.containers.list_logs(rg, group_name, "promote-runner")
            output = log_resp.content or ""
        except Exception as log_err:
            logger.warning("ACI promote-runner: could not retrieve logs: %s", log_err)
            output = f"(failed to retrieve container logs: {log_err})"

        try:
            cg = aci.container_groups.get(rg, group_name)
            for c in (cg.containers or []):
                if c.name == "promote-runner" and c.instance_view and c.instance_view.current_state:
                    ec = c.instance_view.current_state.exit_code
                    exit_code = ec if ec is not None else (0 if state == "Succeeded" else 1)
                    break
            else:
                exit_code = 0 if state == "Succeeded" else 1
        except Exception as ec_err:
            logger.warning("ACI promote-runner: could not get exit code: %s", ec_err)
            exit_code = 0 if state == "Succeeded" else 1

    finally:
        # Always delete the container group — same lifecycle as ACI Ansible
        # and ACI Jumpoint. Promote runs are one-shot.
        try:
            aci.container_groups.begin_delete(rg, group_name).result()
            logger.info("ACI promote-runner: deleted container group %s", group_name)
        except Exception as del_err:
            logger.warning("ACI promote-runner: could not delete group %s: %s", group_name, del_err)

    return exit_code, output


async def run_aci_promote_runner_task(
    rg: str,
    location: str,
    subnet_id: str,
    image: str,
    cpu: float,
    memory_gb: float,
    runner_args: list,
    azure_env: dict,
    job_id: str,
    acr_server: str = "",
    acr_username: str = "",
    acr_password: str = "",
    poll_seconds_max: int = 7200,
) -> tuple:
    """Run the promote-runner image as an ACI container group.
    Returns (exit_code, log_output)."""
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(
            _run_aci_promote_runner_sync,
            cred, sub_id, rg, location, subnet_id, image, cpu, memory_gb,
            runner_args, azure_env, job_id, acr_server, acr_username,
            acr_password, poll_seconds_max,
        )
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to run ACI promote-runner task: {e}") from e


def _delete_staged_blob_sync(
    cred,
    storage_account: str,
    container: str,
    blob_name: str,
) -> None:
    """Delete a single blob from an Azure storage account. Used for promote
    cleanup after the cloud-side image-create reaches Succeeded."""
    from azure.storage.blob import BlobServiceClient
    account_url = f"https://{storage_account}.blob.core.windows.net"
    svc = BlobServiceClient(account_url=account_url, credential=cred)
    blob_client = svc.get_blob_client(container=container, blob=blob_name)
    blob_client.delete_blob()


async def delete_staged_blob(storage_account: str, container: str, blob_name: str) -> None:
    """Best-effort cleanup of a staged blob in any Azure storage account
    the dashboard's AAD credential can reach. Unlike
    `storage_service.delete_image_in("azure_blob", ...)` this targets an
    explicit account/container instead of the configured hub container."""
    try:
        cred, _sub_id = await _ensure_creds()
        await asyncio.to_thread(
            _delete_staged_blob_sync, cred, storage_account, container, blob_name,
        )
    except Exception as e:
        raise AzureError(f"Failed to delete staged blob {storage_account}/{container}/{blob_name}: {e}") from e
