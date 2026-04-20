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
    """
    Return (ClientSecretCredential, subscription_id), fetching from Password Safe
    on first call and caching for subsequent calls.
    """
    global _cred_cache, _sub_id_cache
    _require_azure()
    if _cred_cache is None:
        from ..config import settings

        if (settings.azure_client_id and settings.azure_client_secret
                and settings.azure_tenant_id and settings.azure_subscription_id):
            client_id = settings.azure_client_id
            client_secret = settings.azure_client_secret
            tenant_id = settings.azure_tenant_id
            sub_id = settings.azure_subscription_id
            source = "direct env vars"
        elif settings.beyondtrust_enabled:
            from . import btapi_service
            try:
                client_id = await btapi_service.get_ps_secret(settings.azure_client_id_secret_title)
                client_secret = await btapi_service.get_ps_secret(settings.azure_client_secret_secret_title)
                tenant_id = await btapi_service.get_ps_secret(settings.azure_tenant_id_secret_title)
                sub_id = await btapi_service.get_ps_secret(settings.azure_subscription_id_secret_title)
            except Exception as e:
                raise AzureError(
                    "Azure credentials not configured. Set AZURE_CLIENT_ID, "
                    "AZURE_CLIENT_SECRET, AZURE_TENANT_ID, and "
                    "AZURE_SUBSCRIPTION_ID in your .env file, or configure "
                    f"BeyondTrust Password Safe lookup (underlying error: {e})."
                ) from e
            source = "Password Safe"
        else:
            raise AzureError(
                "Azure credentials not configured. Set AZURE_CLIENT_ID, "
                "AZURE_CLIENT_SECRET, AZURE_TENANT_ID, and "
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

def _get_ssh_key_from_vault_sync(cred, vault_url: str, secret_name: str) -> str:
    """Fetch a secret value from Azure Key Vault (blocking)."""
    client = SecretClient(vault_url=vault_url, credential=cred)
    value = client.get_secret(secret_name).value or ""
    # Normalize \r\n line endings
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    # Azure Key Vault portal collapses PEM newlines to spaces when copy-pasting.
    # Detect single-line PEM and reconstruct proper multi-line format.
    if "\n" not in value and value.startswith("-----BEGIN"):
        header_end = value.index("-----", 5) + 5
        footer_start = value.rindex("-----END")
        header = value[:header_end]
        body_raw = value[header_end:footer_start].strip()
        footer = value[footer_start:].strip()
        body_b64 = "".join(body_raw.split())
        body = "\n".join(body_b64[i:i + 64] for i in range(0, len(body_b64), 64))
        value = f"{header}\n{body}\n{footer}\n"
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

def _list_private_images_sync(cred, sub_id: str, gallery: str, gallery_rg: str, rg: str) -> list:
    compute = _get_compute(cred, sub_id)
    results = []

    # Shared Image Gallery images
    if gallery and gallery_rg:
        try:
            for img_def in compute.gallery_images.list_by_gallery(gallery_rg, gallery):
                versions = list(compute.gallery_image_versions.list_by_gallery_image(
                    gallery_rg, gallery, img_def.name
                ))
                versions.sort(key=lambda v: v.name, reverse=True)
                latest = versions[0] if versions else None
                results.append({
                    "resource_id": img_def.id,
                    "name": img_def.name,
                    "description": img_def.description or "",
                    "state": latest.provisioning_state if latest else "Unknown",
                    "creation_date": (
                        latest.time_created.isoformat() if latest and latest.time_created else ""
                    ),
                    "os_type": _os_type_str(img_def.os_type),
                    "source": "gallery",
                    "gallery_name": gallery,
                    "sku": img_def.identifier.sku if img_def.identifier else "",
                    "location": img_def.location or "",
                })
        except Exception as e:
            logger.warning("Failed to list gallery images from %s/%s: %s", gallery_rg, gallery, e)

    # Standalone managed images in resource group
    try:
        for img in compute.images.list_by_resource_group(rg):
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
        logger.warning("Failed to list managed images from rg=%s: %s", rg, e)

    return results


async def list_private_images(gallery: str, gallery_rg: str, rg: str) -> list:
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
    image_sku: str = None, image_version: str = None
) -> dict:
    compute = _get_compute(cred, sub_id)
    network = _get_network(cred, sub_id)
    tags = {"ManagedBy": "vm-cli-dashboard"}

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
    image_sku: str = None, image_version: str = None
) -> dict:
    try:
        cred, sub_id = await _ensure_creds()
        return await asyncio.to_thread(
            _deploy_vm_sync,
            cred, sub_id, rg, location, vm_name, vm_size,
            image_id, subnet_id, nsg_ids, create_public_ip,
            ssh_username, ssh_public_key,
            image_publisher, image_offer, image_sku, image_version,
        )
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Failed to deploy VM {vm_name}: {e}") from e


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
    resource_client = _get_resource(cred, sub_id)
    compute = _get_compute(cred, sub_id)

    rgs = [rg_item.name for rg_item in resource_client.resource_groups.list()]

    sizes = sorted(
        [s for s in compute.virtual_machine_sizes.list(location)
         if s.name.startswith(("Standard_B", "Standard_D", "Standard_E", "Standard_F"))],
        key=lambda s: (s.number_of_cores, s.memory_in_mb)
    )
    sizes = [s.name for s in sizes]

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

    nsgs = []
    try:
        nsgs_list = list(network.network_security_groups.list(search_rg))
        logger.info("Network options: found %d NSGs in RG=%s", len(nsgs_list), search_rg)
        for nsg in nsgs_list:
            nsgs.append({"id": nsg.id, "name": nsg.name, "resource_group": search_rg})
    except Exception as e:
        logger.warning("Failed to list NSGs from rg=%s: %s", search_rg, e, exc_info=True)

    ssh_keys = _get_ssh_keys_sync(cred, sub_id, rg)

    logger.info("Network options: returning subnets=%d, nsgs=%d, ssh_keys=%d, locations=%d, sizes=%d", 
                len(subnets), len(nsgs), len(ssh_keys), len(locations), len(sizes))
    return {
        "locations": locations,
        "vm_sizes": sizes[:50],
        "subnets": subnets,
        "nsgs": nsgs,
        "ssh_keys": ssh_keys,
        "resource_groups": rgs,
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
