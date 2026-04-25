"""
Google Cloud Platform service layer — Compute Engine + Secret Manager.

Credential priority (highest to lowest):
  1. config_service DB (wizard-stored service account JSON)
  2. Application Default Credentials (gcloud auth / Workload Identity)

All blocking SDK calls run in asyncio.to_thread() so the FastAPI event loop
is never blocked.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class GCPError(Exception):
    pass


# ── Public image catalog ──────────────────────────────────────────────────────

_PUBLIC_IMAGE_PROJECTS: dict[str, tuple[str, str]] = {
    "debian":  ("debian-cloud",       "Debian"),
    "ubuntu":  ("ubuntu-os-cloud",    "Ubuntu"),
    "rhel":    ("rhel-cloud",         "Red Hat Enterprise Linux"),
    "rocky":   ("rocky-linux-cloud",  "Rocky Linux"),
    "centos":  ("centos-cloud",       "CentOS"),
    "cos":     ("cos-cloud",          "Container-Optimized OS"),
    "windows": ("windows-cloud",      "Windows Server"),
}

_DEFAULT_MACHINE_TYPES = [
    "e2-micro", "e2-small", "e2-medium",
    "e2-standard-2", "e2-standard-4", "e2-standard-8", "e2-standard-16",
    "e2-highmem-2", "e2-highmem-4", "e2-highmem-8",
    "n2-standard-2", "n2-standard-4", "n2-standard-8",
    "n2-highmem-2", "n2-highmem-4",
    "c2-standard-4", "c2-standard-8",
    "t2d-standard-1", "t2d-standard-2", "t2d-standard-4",
]


# ── Credential helpers ────────────────────────────────────────────────────────

def _cfg(key: str) -> str:
    from ..services import config_service
    return config_service.get(key) or ""


def _gcp_project() -> str:
    return _cfg("gcp_project_id")


def _gcp_zone() -> str:
    return _cfg("gcp_zone") or "us-central1-a"


def _gcp_region() -> str:
    zone = _gcp_zone()
    # Derive region from zone (strip last segment: us-central1-a → us-central1)
    parts = zone.rsplit("-", 1)
    return _cfg("gcp_region") or (parts[0] if len(parts) == 2 else zone)


def _gcp_creds():
    """Return google.oauth2 credentials or None for ADC."""
    try:
        from google.oauth2 import service_account as _sa
    except ImportError:
        raise GCPError("google-auth is not installed — run: pip install google-auth")

    raw = _cfg("gcp_service_account_json")
    if not raw:
        return None  # ADC fallback
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GCPError(f"gcp_service_account_json is not valid JSON: {exc}") from exc
    return _sa.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )


def _require_compute():
    try:
        from google.cloud import compute_v1  # noqa: F401
    except ImportError:
        raise GCPError("google-cloud-compute is not installed — run: pip install google-cloud-compute")


def _require_secretmanager():
    try:
        from google.cloud import secretmanager_v1  # noqa: F401
    except ImportError:
        raise GCPError("google-cloud-secret-manager is not installed — run: pip install google-cloud-secret-manager")


# ── Image operations ──────────────────────────────────────────────────────────

def _list_custom_images_sync(project_id: str) -> list[dict]:
    _require_compute()
    from google.cloud import compute_v1

    creds = _gcp_creds()
    client = compute_v1.ImagesClient(credentials=creds)
    images = []
    for img in client.list(project=project_id):
        if img.deprecated and img.deprecated.state in ("DEPRECATED", "OBSOLETE", "DELETED"):
            continue
        images.append({
            "self_link":    img.self_link,
            "name":         img.name,
            "description":  img.description or "",
            "status":       img.status or "READY",
            "creation_date": img.creation_timestamp or "",
            "disk_size_gb": img.disk_size_gb,
            "source":       "custom",
            "family":       img.family or "",
            "labels":       dict(img.labels) if img.labels else {},
        })
    images.sort(key=lambda x: x["creation_date"], reverse=True)
    return images


async def list_custom_images(project_id: str) -> list[dict]:
    return await asyncio.to_thread(_list_custom_images_sync, project_id)


def _list_public_images_sync(os_filter: str = "all") -> list[dict]:
    _require_compute()
    from google.cloud import compute_v1

    creds = _gcp_creds()
    client = compute_v1.ImagesClient(credentials=creds)
    results = []

    targets = (
        {os_filter: _PUBLIC_IMAGE_PROJECTS[os_filter]}
        if os_filter != "all" and os_filter in _PUBLIC_IMAGE_PROJECTS
        else _PUBLIC_IMAGE_PROJECTS
    )

    for os_key, (pub_project, os_label) in targets.items():
        try:
            for img in client.list(project=pub_project):
                if img.deprecated and img.deprecated.state != "":
                    continue
                results.append({
                    "self_link":    img.self_link,
                    "name":         img.name,
                    "description":  img.description or img.name,
                    "status":       img.status or "READY",
                    "creation_date": img.creation_timestamp or "",
                    "disk_size_gb": img.disk_size_gb,
                    "source":       "public",
                    "family":       img.family or "",
                    "os_label":     os_label,
                    "os_key":       os_key,
                })
        except Exception as exc:
            logger.warning("Could not list images for project %s: %s", pub_project, exc)

    # Sort newest first within each group
    results.sort(key=lambda x: x["creation_date"], reverse=True)
    return results


async def list_public_images(os_filter: str = "all") -> list[dict]:
    return await asyncio.to_thread(_list_public_images_sync, os_filter)


def _delete_image_sync(project_id: str, image_name: str) -> None:
    _require_compute()
    from google.cloud import compute_v1

    creds = _gcp_creds()
    client = compute_v1.ImagesClient(credentials=creds)
    op = client.delete(project=project_id, image=image_name)
    op.result()


async def delete_image(project_id: str, image_name: str) -> None:
    await asyncio.to_thread(_delete_image_sync, project_id, image_name)


def _create_image_from_instance_sync(
    project_id: str,
    zone: str,
    instance_name: str,
    image_name: str,
    description: str = "",
    no_stop: bool = True,
) -> dict:
    _require_compute()
    from google.cloud import compute_v1

    creds = _gcp_creds()
    images_client = compute_v1.ImagesClient(credentials=creds)
    instances_client = compute_v1.InstancesClient(credentials=creds)

    instance = instances_client.get(project=project_id, zone=zone, instance=instance_name)
    boot_disk = next((d for d in instance.disks if d.boot), None)
    if not boot_disk:
        raise GCPError(f"No boot disk found on instance {instance_name}")

    image = compute_v1.Image(
        name=image_name,
        description=description,
        source_disk=boot_disk.source,
    )
    op = images_client.insert(project=project_id, image_resource=image)
    op.result()
    created = images_client.get(project=project_id, image=image_name)
    return {
        "self_link":    created.self_link,
        "name":         created.name,
        "description":  created.description or "",
        "status":       created.status or "READY",
        "creation_date": created.creation_timestamp or "",
        "disk_size_gb": created.disk_size_gb,
        "source":       "custom",
    }


async def create_image_from_instance(
    project_id: str,
    zone: str,
    instance_name: str,
    image_name: str,
    description: str = "",
) -> dict:
    return await asyncio.to_thread(
        _create_image_from_instance_sync,
        project_id, zone, instance_name, image_name, description,
    )


# ── Network options ───────────────────────────────────────────────────────────

def _get_network_options_sync(project_id: str, region: str, zone: str) -> dict:
    _require_compute()
    from google.cloud import compute_v1

    creds = _gcp_creds()

    # Zones in region
    zones_client = compute_v1.ZonesClient(credentials=creds)
    zones = [z.name for z in zones_client.list(project=project_id) if z.region.endswith(f"/{region}")]
    if not zones:
        zones = [zone]

    # Machine types for the requested zone
    try:
        mt_client = compute_v1.MachineTypesClient(credentials=creds)
        machine_types = sorted({mt.name for mt in mt_client.list(project=project_id, zone=zone)})
    except Exception:
        machine_types = _DEFAULT_MACHINE_TYPES

    # Subnetworks in region
    subnets_client = compute_v1.SubnetworksClient(credentials=creds)
    subnets = []
    try:
        for sn in subnets_client.list(project=project_id, region=region):
            subnets.append({
                "name":          sn.name,
                "self_link":     sn.self_link,
                "ip_cidr_range": sn.ip_cidr_range or "",
                "network":       sn.network.split("/")[-1] if sn.network else "default",
            })
    except Exception as exc:
        logger.warning("Could not list subnetworks: %s", exc)

    # SSH key configured?
    ssh_secret = _cfg("gcp_ssh_key_secret_name")

    return {
        "zones":          zones,
        "machine_types":  machine_types,
        "subnetworks":    subnets,
        "region":         region,
        "ssh_key_configured": bool(ssh_secret),
    }


async def get_network_options(project_id: str, region: str, zone: str) -> dict:
    return await asyncio.to_thread(_get_network_options_sync, project_id, region, zone)


# ── Secret Manager ────────────────────────────────────────────────────────────

def _get_secret_sync(project_id: str, secret_name: str) -> str:
    _require_secretmanager()
    from google.cloud import secretmanager_v1

    creds = _gcp_creds()
    client = secretmanager_v1.SecretManagerServiceClient(credentials=creds)
    name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")


async def get_secret(project_id: str, secret_name: str) -> str:
    return await asyncio.to_thread(_get_secret_sync, project_id, secret_name)


async def get_ssh_public_key(project_id: str, secret_name: str) -> str:
    """Retrieve an SSH public key from Secret Manager.
    Supports: JSON with a 'public_key' field, or raw public key string.
    """
    raw = await get_secret(project_id, secret_name)
    raw = raw.strip()
    try:
        data = json.loads(raw)
        pub = data.get("public_key") or data.get("publicKey") or ""
        return pub.strip()
    except (json.JSONDecodeError, AttributeError):
        return raw  # plain public key


# ── Instance operations ───────────────────────────────────────────────────────

def _launch_instance_sync(
    project_id: str,
    zone: str,
    instance_name: str,
    machine_type: str,
    image_self_link: str,
    subnetwork: str,
    create_external_ip: bool,
    ssh_username: str,
    ssh_public_key: str,
    disk_size_gb: int = 20,
    network_tags: Optional[list[str]] = None,
    labels: Optional[dict] = None,
) -> dict:
    _require_compute()
    from google.cloud import compute_v1

    creds = _gcp_creds()
    client = compute_v1.InstancesClient(credentials=creds)

    instance = compute_v1.Instance()
    instance.name = instance_name
    instance.machine_type = f"zones/{zone}/machineTypes/{machine_type}"

    # Boot disk
    boot_disk = compute_v1.AttachedDisk()
    boot_disk.boot = True
    boot_disk.auto_delete = True
    boot_disk.initialize_params = compute_v1.AttachedDiskInitializeParams(
        source_image=image_self_link,
        disk_size_gb=disk_size_gb,
        disk_type=f"zones/{zone}/diskTypes/pd-balanced",
    )
    instance.disks = [boot_disk]

    # Network interface
    nic = compute_v1.NetworkInterface()
    if subnetwork:
        nic.subnetwork = subnetwork
    else:
        nic.name = "default"
    if create_external_ip:
        nic.access_configs = [compute_v1.AccessConfig(
            name="External NAT",
            type_="ONE_TO_ONE_NAT",
        )]
    instance.network_interfaces = [nic]

    # SSH key in instance metadata
    instance.metadata = compute_v1.Metadata(
        items=[compute_v1.Items(key="ssh-keys", value=f"{ssh_username}:{ssh_public_key}")]
    )

    # Labels
    merged_labels = {"managed-by": "vm-dashboard"}
    if labels:
        merged_labels.update(labels)
    instance.labels = merged_labels

    # Firewall tags
    if network_tags:
        instance.tags = compute_v1.Tags(items=network_tags)

    op = client.insert(project=project_id, zone=zone, instance_resource=instance)
    op.result()

    # Fetch live IP addresses after boot
    info = client.get(project=project_id, zone=zone, instance=instance_name)
    public_ip = None
    private_ip = None
    for nic_info in info.network_interfaces:
        private_ip = nic_info.network_i_p
        for ac in nic_info.access_configs:
            if ac.nat_i_p:
                public_ip = ac.nat_i_p

    return {
        "instance_name": instance_name,
        "zone":          zone,
        "machine_type":  machine_type,
        "status":        info.status,
        "public_ip":     public_ip,
        "private_ip":    private_ip,
        "self_link":     info.self_link,
    }


async def launch_instance(
    project_id: str,
    zone: str,
    instance_name: str,
    machine_type: str,
    image_self_link: str,
    subnetwork: str,
    create_external_ip: bool,
    ssh_username: str,
    ssh_public_key: str,
    disk_size_gb: int = 20,
    network_tags: Optional[list[str]] = None,
    labels: Optional[dict] = None,
) -> dict:
    return await asyncio.to_thread(
        _launch_instance_sync,
        project_id, zone, instance_name, machine_type, image_self_link,
        subnetwork, create_external_ip, ssh_username, ssh_public_key,
        disk_size_gb, network_tags, labels,
    )


def _describe_instances_sync(project_id: str, zone: str, instance_names: list[str]) -> list[dict]:
    _require_compute()
    from google.cloud import compute_v1

    creds = _gcp_creds()
    client = compute_v1.InstancesClient(credentials=creds)
    results = []
    for name in instance_names:
        try:
            info = client.get(project=project_id, zone=zone, instance=name)
            public_ip = None
            private_ip = None
            for nic in info.network_interfaces:
                private_ip = nic.network_i_p
                for ac in nic.access_configs:
                    if ac.nat_i_p:
                        public_ip = ac.nat_i_p
            results.append({
                "instance_name": info.name,
                "zone":          zone,
                "machine_type":  info.machine_type.split("/")[-1],
                "status":        info.status,
                "public_ip":     public_ip,
                "private_ip":    private_ip,
                "self_link":     info.self_link,
                "creation_timestamp": info.creation_timestamp or "",
            })
        except Exception as exc:
            logger.warning("Could not describe GCE instance %s: %s", name, exc)
            results.append({
                "instance_name": name,
                "zone":          zone,
                "status":        "UNKNOWN",
                "error":         str(exc),
            })
    return results


async def describe_instances(project_id: str, zone: str, instance_names: list[str]) -> list[dict]:
    return await asyncio.to_thread(_describe_instances_sync, project_id, zone, instance_names)


def _terminate_instance_sync(project_id: str, zone: str, instance_name: str) -> None:
    _require_compute()
    from google.cloud import compute_v1

    creds = _gcp_creds()
    client = compute_v1.InstancesClient(credentials=creds)
    op = client.delete(project=project_id, zone=zone, instance=instance_name)
    op.result()


async def terminate_instance(project_id: str, zone: str, instance_name: str) -> None:
    await asyncio.to_thread(_terminate_instance_sync, project_id, zone, instance_name)
