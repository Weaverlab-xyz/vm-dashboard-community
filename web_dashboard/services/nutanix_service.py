"""
Nutanix AHV service layer — Prism Central REST API v3.

Works with both Prism Central (multi-cluster) and Prism Element (single-cluster).
Authentication is HTTP Basic; no SDK required — httpx is sufficient.

All blocking HTTP calls run in asyncio.to_thread() so the FastAPI event loop
is never blocked.
"""
import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

# ── Cloud image catalog ───────────────────────────────────────────────────────

_CLOUD_IMAGES = [
    {
        "id": "ubuntu-2404",
        "name": "Ubuntu 24.04 LTS (Noble)",
        "distro": "ubuntu",
        "filename": "noble-server-cloudimg-amd64.img",
        "url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
    },
    {
        "id": "ubuntu-2204",
        "name": "Ubuntu 22.04 LTS (Jammy)",
        "distro": "ubuntu",
        "filename": "jammy-server-cloudimg-amd64.img",
        "url": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
    },
    {
        "id": "debian-12",
        "name": "Debian 12 (Bookworm)",
        "distro": "debian",
        "filename": "debian-12-genericcloud-amd64.qcow2",
        "url": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
    },
    {
        "id": "rocky-9",
        "name": "Rocky Linux 9",
        "distro": "rocky",
        "filename": "Rocky-9-GenericCloud.latest.x86_64.qcow2",
        "url": "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud.latest.x86_64.qcow2",
    },
    {
        "id": "rocky-8",
        "name": "Rocky Linux 8",
        "distro": "rocky",
        "filename": "Rocky-8-GenericCloud.latest.x86_64.qcow2",
        "url": "https://dl.rockylinux.org/pub/rocky/8/images/x86_64/Rocky-8-GenericCloud.latest.x86_64.qcow2",
    },
    {
        "id": "almalinux-9",
        "name": "AlmaLinux 9",
        "distro": "alma",
        "filename": "AlmaLinux-9-GenericCloud-latest.x86_64.qcow2",
        "url": "https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2",
    },
    {
        "id": "centos-stream-9",
        "name": "CentOS Stream 9",
        "distro": "centos",
        "filename": "CentOS-Stream-GenericCloud-9-latest.x86_64.qcow2",
        "url": "https://cloud.centos.org/centos/9-stream/x86_64/images/CentOS-Stream-GenericCloud-9-latest.x86_64.qcow2",
    },
]


class NutanixError(Exception):
    pass


def _cfg(key: str) -> str:
    from . import config_service
    return config_service.get(key) or ""


def _cfg_bool(key: str, default: bool = False) -> bool:
    from . import config_service
    return config_service.get_bool(key, default)


def _client() -> httpx.Client:
    host = _cfg("nutanix_host")
    if not host:
        raise NutanixError("NUTANIX_HOST is not configured")

    port     = int(_cfg("nutanix_port") or "9440")
    username = _cfg("nutanix_username") or "admin"
    password = _cfg("nutanix_password")
    if not password:
        raise NutanixError("NUTANIX_PASSWORD is not configured")

    verify = _cfg_bool("nutanix_verify_ssl", False)

    return httpx.Client(
        base_url=f"https://{host}:{port}/api/nutanix/v3",
        auth=(username, password),
        verify=verify,
        timeout=60.0,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )


# ── Normalisation helpers ─────────────────────────────────────────────────────

def _normalise_vm(entity: dict) -> dict:
    metadata  = entity.get("metadata", {})
    status    = entity.get("status", {})
    resources = status.get("resources", {})

    power_state = (resources.get("power_state") or "UNKNOWN").upper()

    vcpus   = resources.get("num_vcpus_per_socket", 1) * resources.get("num_sockets", 1)
    mem_mib = resources.get("memory_size_mib", 0)

    ips: list[str] = []
    for nic in resources.get("nic_list", []):
        for ep in nic.get("ip_endpoint_list", []):
            ip = ep.get("ip", "")
            if ip and ":" not in ip and not ip.startswith("127."):
                ips.append(ip)

    ngt      = (resources.get("guest_tools") or {}).get("nutanix_guest_tools") or {}
    ngt_on   = ngt.get("ngt_state") == "ENABLED"
    ngt_live = bool(ngt.get("is_reachable", False))

    return {
        "uuid":        metadata.get("uuid", ""),
        "name":        status.get("name", ""),
        "power_state": power_state,
        "is_running":  power_state == "ON",
        "vcpus":       vcpus,
        "mem_mib":     mem_mib,
        "ip_addresses": ips,
        "ngt_enabled":  ngt_on,
        "ngt_reachable": ngt_live,
        "cluster":     (status.get("cluster_reference") or {}).get("name", ""),
        "cluster_uuid": (status.get("cluster_reference") or {}).get("uuid", ""),
        "description": status.get("description", ""),
    }


def _normalise_image(entity: dict) -> dict:
    metadata  = entity.get("metadata", {})
    status    = entity.get("status", {})
    resources = status.get("resources", {})
    return {
        "uuid":       metadata.get("uuid", ""),
        "name":       status.get("name", ""),
        "state":      (resources.get("state") or "UNKNOWN").upper(),
        "image_type": resources.get("image_type", ""),
        "size_bytes": resources.get("size_bytes", 0),
        "source_uri": resources.get("source_uri", ""),
    }


def _normalise_cluster(entity: dict) -> dict:
    metadata = entity.get("metadata", {})
    status   = entity.get("status", {})
    return {
        "uuid": metadata.get("uuid", ""),
        "name": status.get("name", ""),
    }


def _normalise_subnet(entity: dict) -> dict:
    metadata  = entity.get("metadata", {})
    status    = entity.get("status", {})
    resources = status.get("resources", {})
    return {
        "uuid":         metadata.get("uuid", ""),
        "name":         status.get("name", ""),
        "subnet_type":  resources.get("subnet_type", ""),
        "cluster_uuid": (status.get("cluster_reference") or {}).get("uuid", ""),
        "cluster_name": (status.get("cluster_reference") or {}).get("name", ""),
    }


# ── Paginated list helper ─────────────────────────────────────────────────────

def _list_all(c: httpx.Client, kind: str) -> list[dict]:
    """Fetch all entities of a given kind using Prism v3 pagination."""
    offset, length = 0, 500
    entities: list[dict] = []
    while True:
        r = c.post(f"/{kind}/list", json={"kind": kind[:-1], "offset": offset, "length": length})
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise NutanixError(f"Prism API error {r.status_code}: {r.text[:300]}") from e
        data  = r.json()
        batch = data.get("entities") or []
        entities.extend(batch)
        total = (data.get("metadata") or {}).get("total_matches", 0)
        offset += length
        if offset >= total or not batch:
            break
    return entities


# ── List VMs ──────────────────────────────────────────────────────────────────

def _list_vms_sync() -> list[dict]:
    with _client() as c:
        entities = _list_all(c, "vms")
    return sorted([_normalise_vm(e) for e in entities], key=lambda v: v["name"].lower())


# ── List images ───────────────────────────────────────────────────────────────

def _list_images_sync() -> list[dict]:
    with _client() as c:
        entities = _list_all(c, "images")
    return sorted([_normalise_image(e) for e in entities], key=lambda i: i["name"].lower())


# ── List clusters ─────────────────────────────────────────────────────────────

def _list_clusters_sync() -> list[dict]:
    with _client() as c:
        entities = _list_all(c, "clusters")
    return sorted([_normalise_cluster(e) for e in entities], key=lambda c: c["name"].lower())


# ── List subnets ──────────────────────────────────────────────────────────────

def _list_subnets_sync() -> list[dict]:
    with _client() as c:
        entities = _list_all(c, "subnets")
    return sorted([_normalise_subnet(e) for e in entities], key=lambda s: s["name"].lower())


# ── Task polling ──────────────────────────────────────────────────────────────

def _wait_for_task(c: httpx.Client, task_uuid: str, timeout: int = 600) -> None:
    deadline = time.monotonic() + timeout
    while True:
        r = c.get(f"/tasks/{task_uuid}")
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise NutanixError(f"Task poll error: {r.status_code}") from e

        data   = r.json()
        status = (data.get("status") or "").upper()

        if status == "SUCCEEDED":
            return
        if status == "FAILED":
            msg = data.get("error_detail") or data.get("progress_message") or "Task failed"
            raise NutanixError(msg)
        if time.monotonic() > deadline:
            raise NutanixError(f"Task {task_uuid} timed out after {timeout}s")
        time.sleep(3)


# ── Image import ──────────────────────────────────────────────────────────────

def _import_image_sync(name: str, source_uri: str) -> dict:
    """Import an image from a public URL into the Nutanix Image Service."""
    logger.info("Nutanix: importing image '%s' from %s", name, source_uri)
    with _client() as c:
        body = {
            "spec": {
                "name": name,
                "resources": {
                    "image_type": "DISK_IMAGE",
                    "source_uri": source_uri,
                },
            },
            "metadata": {"kind": "image", "name": name},
        }
        r = c.post("/images", json=body)
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise NutanixError(f"Image create failed {r.status_code}: {r.text[:300]}") from e

        data     = r.json()
        uuid     = (data.get("metadata") or {}).get("uuid", "")
        task_uuid = (data.get("status") or {}).get("execution_context", {}).get("task_uuid")

        if task_uuid:
            _wait_for_task(c, task_uuid, timeout=900)

        logger.info("Nutanix: image '%s' imported with uuid %s", name, uuid)
        return {"uuid": uuid, "name": name, "status": "COMPLETE"}


# ── Deploy VM from image ──────────────────────────────────────────────────────

def _deploy_vm_sync(
    vm_name: str,
    image_uuid: str,
    cluster_uuid: str,
    subnet_uuid: str,
    vcpus: int = 2,
    num_sockets: int = 1,
    memory_mib: int = 4096,
    disk_size_mib: int = 40960,
) -> dict:
    """Deploy a VM from an existing Image Service image."""
    logger.info("Nutanix: deploying VM '%s' from image %s", vm_name, image_uuid)
    with _client() as c:
        body = {
            "spec": {
                "name": vm_name,
                "resources": {
                    "num_vcpus_per_socket": vcpus,
                    "num_sockets": num_sockets,
                    "memory_size_mib": memory_mib,
                    "power_state": "ON",
                    "disk_list": [
                        {
                            "disk_size_mib": disk_size_mib,
                            "device_properties": {
                                "device_type": "DISK",
                                "disk_address": {"adapter_type": "SCSI", "device_index": 0},
                            },
                            "data_source_reference": {
                                "kind": "image",
                                "uuid": image_uuid,
                            },
                        }
                    ],
                    "nic_list": [
                        {
                            "subnet_reference": {
                                "kind": "subnet",
                                "uuid": subnet_uuid,
                            }
                        }
                    ],
                },
                "cluster_reference": {
                    "kind": "cluster",
                    "uuid": cluster_uuid,
                },
            },
            "metadata": {"kind": "vm"},
        }
        r = c.post("/vms", json=body)
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise NutanixError(f"VM create failed {r.status_code}: {r.text[:300]}") from e

        data      = r.json()
        vm_uuid   = (data.get("metadata") or {}).get("uuid", "")
        task_uuid = (data.get("status") or {}).get("execution_context", {}).get("task_uuid")

        if task_uuid:
            _wait_for_task(c, task_uuid, timeout=300)

        logger.info("Nutanix: VM '%s' created with uuid %s", vm_name, vm_uuid)
        return {"uuid": vm_uuid, "name": vm_name, "status": "ON"}


# ── Delete image ──────────────────────────────────────────────────────────────

def _delete_image_sync(uuid: str) -> dict:
    with _client() as c:
        r = c.delete(f"/images/{uuid}")
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise NutanixError(f"Image delete failed {r.status_code}: {r.text[:300]}") from e
        task_uuid = (r.json().get("status") or {}).get("execution_context", {}).get("task_uuid")
        if task_uuid:
            _wait_for_task(c, task_uuid)
    logger.info("Nutanix: image %s deleted", uuid)
    return {"uuid": uuid, "status": "deleted"}


# ── Delete VM ─────────────────────────────────────────────────────────────────

def _delete_vm_sync(uuid: str, name: str = "") -> dict:
    with _client() as c:
        # Force off first if running
        try:
            r = c.post(f"/vms/{uuid}/set_power_state", json={"transition": "OFF"})
            task_uuid = r.json().get("task_uuid")
            if task_uuid:
                _wait_for_task(c, task_uuid, timeout=60)
        except Exception:
            pass

        r = c.delete(f"/vms/{uuid}")
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise NutanixError(f"VM delete failed {r.status_code}: {r.text[:300]}") from e
        task_uuid = (r.json().get("status") or {}).get("execution_context", {}).get("task_uuid")
        if task_uuid:
            _wait_for_task(c, task_uuid)
    logger.info("Nutanix: VM %s (%s) deleted", name or uuid, uuid)
    return {"uuid": uuid, "name": name, "status": "deleted"}


# ── Power operations ──────────────────────────────────────────────────────────

_TRANSITIONS = {
    "start":    "ON",
    "shutdown": "ACPI_SHUTDOWN",
    "stop":     "OFF",
    "reboot":   "ACPI_REBOOT",
    "reset":    "RESET",
    "pause":    "PAUSE",
    "resume":   "RESUME",
}


def _power_op_sync(uuid: str, name: str, op: str) -> dict:
    if op not in _TRANSITIONS:
        raise NutanixError(f"Unknown operation: {op}")

    transition = _TRANSITIONS[op]
    logger.info("Nutanix: %s (%s) on %s (%s)", op, transition, name or uuid, uuid)

    with _client() as c:
        r = c.post(
            f"/vms/{uuid}/set_power_state",
            json={"transition": transition},
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise NutanixError(f"Power op failed {r.status_code}: {r.text[:300]}") from e

        task_uuid = r.json().get("task_uuid")
        if task_uuid:
            _wait_for_task(c, task_uuid)

    return {"uuid": uuid, "name": name, "op": op, "status": "OK"}


# ── Async public API ──────────────────────────────────────────────────────────

def list_cloud_images() -> list[dict]:
    return _CLOUD_IMAGES


async def list_vms() -> list[dict]:
    try:
        return await asyncio.to_thread(_list_vms_sync)
    except NutanixError:
        raise
    except Exception as e:
        raise NutanixError(f"Failed to list VMs: {e}") from e


async def list_images() -> list[dict]:
    try:
        return await asyncio.to_thread(_list_images_sync)
    except NutanixError:
        raise
    except Exception as e:
        raise NutanixError(f"Failed to list images: {e}") from e


async def list_clusters() -> list[dict]:
    try:
        return await asyncio.to_thread(_list_clusters_sync)
    except NutanixError:
        raise
    except Exception as e:
        raise NutanixError(f"Failed to list clusters: {e}") from e


async def list_subnets() -> list[dict]:
    try:
        return await asyncio.to_thread(_list_subnets_sync)
    except NutanixError:
        raise
    except Exception as e:
        raise NutanixError(f"Failed to list subnets: {e}") from e


async def import_image(name: str, source_uri: str) -> dict:
    try:
        return await asyncio.to_thread(_import_image_sync, name, source_uri)
    except NutanixError:
        raise
    except Exception as e:
        raise NutanixError(f"Image import failed: {e}") from e


async def deploy_vm(
    vm_name: str,
    image_uuid: str,
    cluster_uuid: str,
    subnet_uuid: str,
    vcpus: int = 2,
    num_sockets: int = 1,
    memory_mib: int = 4096,
    disk_size_mib: int = 40960,
) -> dict:
    try:
        return await asyncio.to_thread(
            _deploy_vm_sync,
            vm_name, image_uuid, cluster_uuid, subnet_uuid,
            vcpus, num_sockets, memory_mib, disk_size_mib,
        )
    except NutanixError:
        raise
    except Exception as e:
        raise NutanixError(f"VM deploy failed: {e}") from e


async def delete_image(uuid: str) -> dict:
    try:
        return await asyncio.to_thread(_delete_image_sync, uuid)
    except NutanixError:
        raise
    except Exception as e:
        raise NutanixError(f"Delete image {uuid} failed: {e}") from e


async def delete_vm(uuid: str, name: str = "") -> dict:
    try:
        return await asyncio.to_thread(_delete_vm_sync, uuid, name)
    except NutanixError:
        raise
    except Exception as e:
        raise NutanixError(f"Delete VM {name or uuid} failed: {e}") from e


async def power_op(uuid: str, name: str, op: str) -> dict:
    if op not in _TRANSITIONS:
        raise NutanixError(
            f"Invalid operation '{op}'. Must be one of: {', '.join(sorted(_TRANSITIONS))}"
        )
    try:
        return await asyncio.to_thread(_power_op_sync, uuid, name, op)
    except NutanixError:
        raise
    except Exception as e:
        raise NutanixError(f"Power operation '{op}' on {name or uuid} failed: {e}") from e
