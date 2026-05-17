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
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from azure.identity import ClientSecretCredential
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.compute.models import (
        VirtualMachine, HardwareProfile, StorageProfile, ImageReference,
        OSDisk, DiskCreateOptionTypes, OSProfile, LinuxConfiguration,
        SshConfiguration, SshPublicKey, NetworkProfile, NetworkInterfaceReference,
    )
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.network.models import (
        NetworkInterface, NetworkInterfaceIPConfiguration,
        PublicIPAddress,
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
    from azure.mgmt.resource import ResourceManagementClient
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
        return {"matches": False, "derived_public_key": None, "error": str(e)}


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
                if latest is not None:
                    pub_profile = getattr(latest, "publishing_profile", None)
                    published = getattr(pub_profile, "published_date", None) if pub_profile else None
                    if published is None:
                        # Some SDK versions expose this directly on the version
                        published = getattr(latest, "time_created", None)
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

    # Standalone managed images in resource group. When a separate gallery RG is
    # configured, scope managed-image lookup to that RG instead of the VM RG so
    # the gallery setting fully controls where private images come from.
    effective_rg = gallery_rg or rg
    try:
        for img in compute.images.list_by_resource_group(effective_rg):
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
            })
    except Exception as e:
        logger.warning("Failed to list managed images from rg=%s: %s", effective_rg, e)
        warnings.append(
            f"Managed images in resource group '{effective_rg}' could not be listed: {e}."
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

def _deploy_vm_sync(
    cred, sub_id: str, rg: str, location: str, vm_name: str, vm_size: str,
    image_id: str, subnet_id: str, nsg_ids: list, create_public_ip: bool,
    ssh_username: str, ssh_public_key: str,
    image_publisher: str = None, image_offer: str = None,
    image_sku: str = None, image_version: str = None,
    workgroup: str = "",
) -> dict:
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
    tags = {"ManagedBy": "vm-cli-dashboard"}
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
        os_profile=OSProfile(
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
        ),
        network_profile=NetworkProfile(
            network_interfaces=[NetworkInterfaceReference(id=nic.id, primary=True)]
        ),
    )

    vm = compute.virtual_machines.begin_create_or_update(rg, vm_name, vm_params).result()

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
        )
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to deploy VM {vm_name}: {e}") from e


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


def _describe_vms_sync(cred, sub_id: str, rg: str) -> list:
    compute = _get_compute(cred, sub_id)
    network = _get_network(cred, sub_id)
    results = []
    for vm in compute.virtual_machines.list(rg):
        tags = vm.tags or {}
        if tags.get("ManagedBy") != "vm-cli-dashboard":
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


def _create_image_from_vm_sync(cred, sub_id: str, rg: str, vm_name: str, image_name: str, generalize: bool) -> dict:
    compute = _get_compute(cred, sub_id)

    if generalize:
        compute.virtual_machines.begin_deallocate(rg, vm_name).result()
        compute.virtual_machines.generalize(rg, vm_name)

    vm = compute.virtual_machines.get(rg, vm_name)
    image_params = {
        "location": vm.location,
        "source_virtual_machine": {"id": vm.id},
        "tags": {"ManagedBy": "vm-cli-dashboard"},
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

    locations = [
        "eastus", "eastus2", "westus", "westus2", "westus3",
        "centralus", "northcentralus", "southcentralus",
        "northeurope", "westeurope", "uksouth", "ukwest",
        "australiaeast", "australiasoutheast",
        "japaneast", "japanwest",
        "canadacentral", "canadaeast",
    ]

    subnets = []
    search_rg = vnet_rg or rg
    logger.info("Network options: searching for subnets in RG=%s", search_rg)
    try:
        vnets = list(network.virtual_networks.list(search_rg))
        logger.info("Network options: found %d VNets in RG=%s", len(vnets), search_rg)
        for vnet in vnets:
            logger.info("Network options: VNet %s has %d subnets", vnet.name, len(vnet.subnets or []))
            for subnet in (vnet.subnets or []):
                subnets.append({
                    "id": subnet.id,
                    "name": subnet.name,
                    "address_prefix": subnet.address_prefix or "",
                    "vnet_name": vnet.name,
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
    keys = storage_client.storage_accounts.list_keys(rg, account_name)
    return keys.keys[0].value


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
        tags={"ManagedBy": "vm-cli-dashboard"},
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
            storage_key = await asyncio.to_thread(
                _get_storage_account_key_sync, cred, sub_id,
                storage_account_rg or rg, storage_account
            )
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
        if tags.get("ManagedBy") != "vm-cli-dashboard":
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
) -> tuple:
    """
    Create an ACI container group that runs a single Ansible playbook, wait for
    it to finish, return (exit_code, log_output), and delete the group.
    """
    import time

    aci = _get_aci(cred, sub_id)
    group_name = f"{_ANSIBLE_RUNNER_PREFIX}-{job_id[:8]}"

    cmd = (
        "set -e && "
        "echo \"$PLAYBOOK_B64\" | base64 -d > /tmp/playbook.yml && "
        "echo \"$SSH_KEY_B64\" | base64 -d > /tmp/ssh_key && "
        "chmod 600 /tmp/ssh_key && "
        f"ansible-playbook -i '{target_ip},' "
        "--forks 1 "
        f"-u {ansible_user} "
        "--private-key /tmp/ssh_key "
        "--ssh-extra-args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null' "
        "/tmp/playbook.yml"
    )

    container = Container(
        name="ansible",
        image=image,
        resources=ResourceRequirements(requests=ResourceRequests(cpu=1.0, memory_in_gb=1.0)),
        command=["sh", "-c", cmd],
        environment_variables=[
            EnvironmentVariable(name="PLAYBOOK_B64", value=playbook_b64),
            EnvironmentVariable(name="SSH_KEY_B64", secure_value=ssh_key_b64),
        ],
    )

    group_params = ContainerGroup(
        location=location,
        containers=[container],
        os_type=OperatingSystemTypes.LINUX,
        restart_policy="Never",
        tags={"ManagedBy": "vm-cli-dashboard", "Purpose": "ansible-runner"},
    )

    if subnet_id:
        from azure.mgmt.containerinstance.models import ContainerGroupSubnetId
        group_params.subnet_ids = [ContainerGroupSubnetId(id=subnet_id)]

    if acr_server and acr_username and acr_password:
        from azure.mgmt.containerinstance.models import ImageRegistryCredential
        group_params.image_registry_credentials = [
            ImageRegistryCredential(server=acr_server, username=acr_username, password=acr_password)
        ]

    logger.info("ACI Ansible: creating container group %s in %s", group_name, rg)
    aci.container_groups.begin_create_or_update(rg, group_name, group_params).result()

    # Poll until the container exits (max 20 min)
    output = ""
    exit_code = 1
    state = ""
    try:
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
        )
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to run ACI Ansible task: {e}") from e


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
    from azure.mgmt.compute.models import GrantAccessData
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
    snap_params = {
        "location": location,
        "creation_data": {"create_option": "Copy", "source_resource_id": source_disk_id},
        "incremental": False,
    }
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
        tags={"ManagedBy": "vm-cli-dashboard", "Purpose": "promote-target"},
    )

    poller = compute.images.begin_create_or_update(target_rg, image_name, img_params)
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
        # ENTRYPOINT in the runner Dockerfile launches the python script;
        # ACI maps `command` to the container CMD, which becomes argparse
        # argv. Don't pre-pend python -m anything.
        command=list(runner_args),
        environment_variables=env_vars,
    )

    group_params = ContainerGroup(
        location=location,
        containers=[container],
        os_type=OperatingSystemTypes.LINUX,
        restart_policy="Never",
        tags={"ManagedBy": "vm-cli-dashboard", "Purpose": "promote-runner"},
    )
    if subnet_id:
        from azure.mgmt.containerinstance.models import ContainerGroupSubnetId
        group_params.subnet_ids = [ContainerGroupSubnetId(id=subnet_id)]
    if acr_server and acr_username and acr_password:
        from azure.mgmt.containerinstance.models import ImageRegistryCredential
        group_params.image_registry_credentials = [
            ImageRegistryCredential(server=acr_server, username=acr_username, password=acr_password)
        ]

    logger.info("ACI promote-runner: creating container group %s in %s/%s", group_name, rg, location)
    aci.container_groups.begin_create_or_update(rg, group_name, group_params).result()

    output = ""
    exit_code = 1
    state = ""
    try:
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
