"""
Google Cloud Platform service layer — Compute Engine + Secret Manager.

Credential priority (highest to lowest):
  1. config_service DB (wizard-stored service account JSON)
  2. Application Default Credentials (gcloud auth / Workload Identity)

All blocking SDK calls run in asyncio.to_thread() so the FastAPI event loop
is never blocked.
"""
import asyncio
import hashlib
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


def _clean_public_key(value: str) -> str:
    """Sanitize an SSH public key for metadata injection.

    GCE's instance "ssh-keys" metadata is line-delimited (`user:key\nuser:key…`),
    so any CR or LF embedded inside a single key entry corrupts the value and
    the guest agent silently rejects it. Strip all internal whitespace except
    the single space separating algorithm/blob/comment.
    """
    if not value:
        return ""
    # Collapse any CR/LF into nothing — a valid ssh-rsa entry is one line.
    flat = value.replace("\r", "").replace("\n", "").strip()
    # Collapse runs of whitespace inside (algorithm[ ]blob[ ]comment).
    return " ".join(flat.split())


def _ssh_key_breadcrumbs(value: str) -> dict:
    """Non-sensitive structured info about an SSH public key for log lines."""
    if not value:
        return {"algo": "(empty)", "len": 0, "comment": "(none)", "sha256_12": "—"}
    parts = value.split(None, 2)
    return {
        "algo": parts[0] if parts else "(empty)",
        "len": len(value),
        "comment": parts[2] if len(parts) >= 3 else "(none)",
        "sha256_12": hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12],
    }


async def get_ssh_public_key(project_id: str, secret_name: str) -> str:
    """Retrieve an SSH public key from Secret Manager.
    Supports: JSON with a 'public_key' field, or raw public key string.
    """
    raw = await get_secret(project_id, secret_name)
    try:
        data = json.loads(raw)
        pub = _clean_public_key(data.get("public_key") or data.get("publicKey") or "")
    except (json.JSONDecodeError, AttributeError):
        pub = _clean_public_key(raw)
    crumbs = _ssh_key_breadcrumbs(pub)
    logger.info(
        "SSH public key from Secret Manager '%s': algo=%s len=%d sha256_12=%s comment=%r",
        secret_name, crumbs["algo"], crumbs["len"], crumbs["sha256_12"], crumbs["comment"],
    )
    return pub


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

    # Sanitize the public key — GCE's "ssh-keys" metadata is line-delimited,
    # so any embedded CR/LF in this value silently corrupts the entry.
    ssh_public_key = _clean_public_key(ssh_public_key)
    crumbs = _ssh_key_breadcrumbs(ssh_public_key)
    logger.info(
        "GCP deploy %s: injecting SSH key algo=%s len=%d sha256_12=%s comment=%r as user=%s",
        instance_name, crumbs["algo"], crumbs["len"], crumbs["sha256_12"], crumbs["comment"],
        ssh_username,
    )

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


# ── BeyondTrust SRA Jumpoint on COS-on-GCE ────────────────────────────────────
# Cloud Run Service requires the container to bind to $PORT and serve HTTP; the
# BT SRA Jumpoint is an outbound-only daemon, so Cloud Run is not viable. Use a
# small Container-Optimised-OS GCE instance instead — closest behavioural match
# to AWS ECS Fargate / Azure ACI for this purpose.

_JUMPOINT_LABEL = "bt-jumpoint"


def _jumpoint_container_spec_yaml(container_image: str, deploy_key: str) -> str:
    """Generate the gce-container-declaration metadata YAML.
    COS reads this on first boot and runs the container under containerd."""
    import yaml
    spec = {
        "spec": {
            "containers": [{
                "name": "jumpoint",
                "image": container_image,
                "env": [{"name": "DEPLOY_KEY", "value": deploy_key}],
                "stdin": False,
                "tty": False,
            }],
            "restartPolicy": "Always",
        }
    }
    return yaml.safe_dump(spec, default_flow_style=False)


def _run_gce_jumpoint_sync(
    project_id: str,
    zone: str,
    name: str,
    container_image: str,
    deploy_key: str,
    network: str = "",
    subnetwork: str = "",
    machine_type: str = "e2-micro",
    cos_image_family: str = "cos-stable",
    create_external_ip: bool = True,
) -> dict:
    """Launch a small COS GCE instance running the BT Jumpoint container.
    Idempotent on existence: if an instance with the same name is already
    RUNNING in the zone, returns its info without re-creating."""
    _require_compute()
    from google.cloud import compute_v1
    from google.api_core.exceptions import NotFound

    creds = _gcp_creds()
    client = compute_v1.InstancesClient(credentials=creds)

    # Reuse if already present
    try:
        existing = client.get(project=project_id, zone=zone, instance=name)
        return {
            "name": name, "zone": zone, "self_link": existing.self_link,
            "status": existing.status, "reused": True,
        }
    except NotFound:
        pass

    instance = compute_v1.Instance()
    instance.name = name
    instance.machine_type = f"zones/{zone}/machineTypes/{machine_type}"

    # Boot disk from Container-Optimised OS
    disk = compute_v1.AttachedDisk()
    disk.boot = True
    disk.auto_delete = True
    disk.initialize_params = compute_v1.AttachedDiskInitializeParams()
    disk.initialize_params.source_image = (
        f"projects/cos-cloud/global/images/family/{cos_image_family}"
    )
    disk.initialize_params.disk_size_gb = 10
    instance.disks = [disk]

    # Network
    nic = compute_v1.NetworkInterface()
    if subnetwork:
        nic.subnetwork = subnetwork
    elif network:
        nic.network = network
    if create_external_ip:
        nic.access_configs = [compute_v1.AccessConfig(
            name="External NAT", type_="ONE_TO_ONE_NAT",
        )]
    instance.network_interfaces = [nic]

    # COS reads gce-container-declaration on first boot and runs the container
    container_yaml = _jumpoint_container_spec_yaml(container_image, deploy_key)
    instance.metadata = compute_v1.Metadata(items=[
        compute_v1.Items(key="gce-container-declaration", value=container_yaml),
        compute_v1.Items(key="google-logging-enabled", value="true"),
    ])

    instance.labels = {"managed-by": "vm-dashboard", "purpose": _JUMPOINT_LABEL}
    # Network tag — paired with the sandbox firewall rule
    # `${prefix}-allow-ssh-from-jumpoint` (source-tags=bt-jumpoint) so the
    # Jumpoint can SSH into VMs in the user-VM subnet.
    instance.tags = compute_v1.Tags(items=[_JUMPOINT_LABEL])

    logger.info(
        "Starting GCE COS Jumpoint '%s' in %s (image=%s, machine=%s, deploy_key_len=%d)",
        name, zone, container_image, machine_type, len(deploy_key or ""),
    )
    op = client.insert(project=project_id, zone=zone, instance_resource=instance)
    op.result(timeout=300)

    info = client.get(project=project_id, zone=zone, instance=name)
    return {
        "name": name, "zone": zone, "self_link": info.self_link,
        "status": info.status, "reused": False,
    }


async def run_gce_jumpoint(
    project_id: str,
    zone: str,
    name: str,
    container_image: str,
    deploy_key: str,
    network: str = "",
    subnetwork: str = "",
    machine_type: str = "e2-micro",
    create_external_ip: bool = True,
) -> dict:
    """Async wrapper for _run_gce_jumpoint_sync."""
    try:
        return await asyncio.to_thread(
            _run_gce_jumpoint_sync,
            project_id, zone, name, container_image, deploy_key,
            network, subnetwork, machine_type, "cos-stable", create_external_ip,
        )
    except GCPError:
        raise
    except Exception as e:
        raise GCPError(f"Failed to start GCE Jumpoint '{name}': {e}") from e


async def stop_gce_jumpoint(project_id: str, zone: str, name: str) -> None:
    """Delete the GCE Jumpoint instance. Quiet no-op if it doesn't exist."""
    try:
        await asyncio.to_thread(_terminate_instance_sync, project_id, zone, name)
    except Exception as e:
        # NotFound is benign; log everything else
        msg = str(e)
        if "404" in msg or "not found" in msg.lower():
            return
        raise GCPError(f"Failed to stop GCE Jumpoint '{name}': {e}") from e


# ── Cloud Run Jobs Ansible runner (mirrors ACI runner in azure_service.py) ───

_ANSIBLE_RUNNER_PREFIX = "ansible-runner"


def _require_run():
    try:
        from google.cloud import run_v2  # noqa: F401
    except ImportError:
        raise GCPError("google-cloud-run is not installed — run: pip install google-cloud-run")


def _fetch_cloud_run_job_logs(project_id: str, job_name: str, execution_name: str, creds) -> str:
    """Retrieve Cloud Run job stdout/stderr from Cloud Logging via REST."""
    try:
        from google.auth.transport.requests import AuthorizedSession
        import requests as _requests
    except ImportError:
        return ""

    exec_short = execution_name.split("/")[-1]
    session = AuthorizedSession(creds or _gcp_creds())
    url = "https://logging.googleapis.com/v2/entries:list"
    body = {
        "resourceNames": [f"projects/{project_id}"],
        "filter": (
            f'resource.type="cloud_run_job" '
            f'resource.labels.job_name="{job_name}" '
            f'resource.labels.execution_name="{exec_short}"'
        ),
        "orderBy": "timestamp asc",
        "pageSize": 1000,
    }
    resp = session.post(url, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    lines = []
    for entry in data.get("entries", []):
        text = entry.get("textPayload") or entry.get("jsonPayload", {}).get("message", "")
        if text:
            lines.append(text)
    return "\n".join(lines)


def _run_cloud_run_ansible_sync(
    project_id: str, region: str, image: str,
    target_ip: str, ansible_user: str,
    playbook_b64: str, ssh_key_b64: str, job_id: str,
    vpc_connector: str = "",
) -> tuple:
    """
    Create a Cloud Run Job that runs a single Ansible playbook, wait for it to
    finish, return (exit_code, log_output), and delete the job.
    """
    import time
    _require_run()
    from google.cloud import run_v2

    creds = _gcp_creds()
    jobs_client = run_v2.JobsClient(credentials=creds)
    executions_client = run_v2.ExecutionsClient(credentials=creds)

    job_name = f"{_ANSIBLE_RUNNER_PREFIX}-{job_id[:8]}"
    parent = f"projects/{project_id}/locations/{region}"
    job_resource_name = f"{parent}/jobs/{job_name}"

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

    task_template = run_v2.TaskTemplate(
        containers=[
            run_v2.Container(
                image=image,
                command=["sh", "-c", cmd],
                env=[
                    run_v2.EnvVar(name="PLAYBOOK_B64", value=playbook_b64),
                    run_v2.EnvVar(name="SSH_KEY_B64", value=ssh_key_b64),
                ],
                resources=run_v2.ResourceRequirements(
                    limits={"cpu": "1000m", "memory": "512Mi"},
                ),
            )
        ],
        max_retries=0,
        timeout="1200s",
    )

    exec_template = run_v2.ExecutionTemplate(template=task_template)
    if vpc_connector:
        exec_template.annotations = {
            "run.googleapis.com/vpc-access-connector": vpc_connector,
            "run.googleapis.com/vpc-access-egress": "private-ranges-only",
        }

    job = run_v2.Job(
        template=exec_template,
        labels={"managed-by": "vm-cli-dashboard", "purpose": "ansible-runner"},
    )

    logger.info("Cloud Run Ansible: creating job %s in %s/%s", job_name, project_id, region)
    create_op = jobs_client.create_job(parent=parent, job_id=job_name, job=job)
    created_job = create_op.result()

    output = ""
    exit_code = 1
    execution_name = None

    try:
        run_op = jobs_client.run_job(name=job_resource_name)
        execution = run_op.result()
        execution_name = execution.name

        # Poll until execution completes (max 20 min)
        for _ in range(120):
            exec_info = executions_client.get_execution(name=execution_name)
            if exec_info.completion_time and not exec_info.reconciling:
                succeeded = exec_info.succeeded_count or 0
                failed = exec_info.failed_count or 0
                exit_code = 0 if (succeeded > 0 and failed == 0) else 1
                break
            time.sleep(10)

        try:
            output = _fetch_cloud_run_job_logs(project_id, job_name, execution_name, creds)
        except Exception as log_err:
            logger.warning("Cloud Run Ansible: could not retrieve logs: %s", log_err)

    finally:
        try:
            del_op = jobs_client.delete_job(name=job_resource_name)
            del_op.result()
            logger.info("Cloud Run Ansible: deleted job %s", job_name)
        except Exception as del_err:
            logger.warning("Cloud Run Ansible: could not delete job %s: %s", job_name, del_err)

    return exit_code, output


async def run_cloud_run_ansible_task(
    project_id: str, region: str, image: str,
    target_ip: str, ansible_user: str,
    playbook_b64: str, ssh_key_b64: str, job_id: str,
    vpc_connector: str = "",
) -> tuple:
    """
    Run an Ansible playbook via a GCP Cloud Run Job.
    Returns (exit_code, output_log).
    """
    try:
        return await asyncio.to_thread(
            _run_cloud_run_ansible_sync,
            project_id, region, image,
            target_ip, ansible_user,
            playbook_b64, ssh_key_b64, job_id,
            vpc_connector,
        )
    except GCPError:
        raise
    except Exception as e:
        raise GCPError(f"Failed to run Cloud Run Ansible task: {e}") from e


# ── Cloud Run Services (long-running container instances) ─────────────────────

def _list_cloud_run_services_sync(project_id: str, region: str) -> list[dict]:
    """List Cloud Run services in a region. Each entry summarises the service."""
    from google.cloud import run_v2
    creds = _gcp_creds()
    client = run_v2.ServicesClient(credentials=creds)
    parent = f"projects/{project_id}/locations/{region}"
    out: list[dict] = []
    for svc in client.list_services(parent=parent):
        image = ""
        if svc.template and svc.template.containers:
            image = svc.template.containers[0].image or ""
        ready = False
        for cond in (svc.terminal_condition,) if svc.terminal_condition else ():
            if cond.type_ == "Ready":
                ready = (cond.state == run_v2.Condition.State.CONDITION_SUCCEEDED)
        traffic_pct = sum((t.percent or 0) for t in (svc.traffic_statuses or [])) or 100
        last_modifier = svc.last_modifier or ""
        out.append({
            "name": svc.name.split("/")[-1],
            "full_name": svc.name,
            "region": region,
            "image": image,
            "uri": svc.uri or "",
            "ready": ready,
            "traffic_percent": traffic_pct,
            "create_time": svc.create_time.isoformat() if svc.create_time else None,
            "update_time": svc.update_time.isoformat() if svc.update_time else None,
            "last_modifier": last_modifier,
        })
    return out


async def list_cloud_run_services(project_id: str, region: str) -> list[dict]:
    """List Cloud Run services in a region (async wrapper)."""
    try:
        return await asyncio.to_thread(_list_cloud_run_services_sync, project_id, region)
    except GCPError:
        raise
    except Exception as e:
        raise GCPError(f"Failed to list Cloud Run services in {region}: {e}") from e


def _delete_cloud_run_service_sync(project_id: str, region: str, name: str) -> None:
    from google.cloud import run_v2
    creds = _gcp_creds()
    client = run_v2.ServicesClient(credentials=creds)
    full = f"projects/{project_id}/locations/{region}/services/{name}"
    op = client.delete_service(name=full)
    op.result(timeout=120)


async def delete_cloud_run_service(project_id: str, region: str, name: str) -> None:
    """Delete a Cloud Run service (async wrapper)."""
    try:
        await asyncio.to_thread(_delete_cloud_run_service_sync, project_id, region, name)
    except GCPError:
        raise
    except Exception as e:
        raise GCPError(f"Failed to delete Cloud Run service '{name}': {e}") from e


# ── Export custom image to VHD on GCS (portable artefact) ─────────────────────

def _export_custom_image_to_vhd_sync(
    project_id: str,
    image_name: str,
    dest_bucket: str,
    dest_object: str,
    network: str,
    subnet: str,
    timeout: int,
    progress_cb,
) -> dict:
    """Trigger the Daisy gce_vm_image_export workflow via Cloud Build.

    The container `gcr.io/compute-image-tools/gce_vm_image_export` accepts
    `-format=vpc` to produce a VHD blob written to gs://dest_bucket/dest_object.
    Args also include `-source_image` and `-destination_uri`.

    Returns {gs_url, format, build_id}.
    """
    from google.cloud.devtools import cloudbuild_v1
    creds = _gcp_creds()
    client = cloudbuild_v1.CloudBuildClient(credentials=creds)

    dest_uri = f"gs://{dest_bucket}/{dest_object}"
    args = [
        "-timeout=" + f"{max(timeout - 600, 600)}s",
        "-source_image=" + image_name,
        "-client_id=api",
        "-format=vpc",
        "-destination_uri=" + dest_uri,
    ]
    if network:
        args.append("-network=" + network)
    if subnet:
        args.append("-subnet=" + subnet)

    build = cloudbuild_v1.Build(
        steps=[
            cloudbuild_v1.BuildStep(
                name="gcr.io/compute-image-tools/gce_vm_image_export:release",
                args=args,
                env=["BUILD_ID=$BUILD_ID"],
            )
        ],
        timeout={"seconds": timeout},
        tags=["vm-cli-dashboard", "image-export"],
    )

    if progress_cb:
        progress_cb(f"Submitting Cloud Build export for {image_name} → {dest_uri}")

    op = client.create_build(project_id=project_id, build=build)
    metadata = op.metadata
    build_id = metadata.build.id if metadata and metadata.build else ""
    if progress_cb and build_id:
        progress_cb(f"Cloud Build {build_id} started — polling")

    # op.result() blocks until the build finishes; SDK raises on non-SUCCESS.
    result = op.result(timeout=timeout)
    status = cloudbuild_v1.Build.Status(result.status).name if result.status else "UNKNOWN"
    if status != "SUCCESS":
        raise GCPError(f"Cloud Build {build_id} ended in status {status}: {result.status_detail}")

    if progress_cb:
        progress_cb(f"Export complete: {dest_uri}")

    return {"gs_url": dest_uri, "format": "vhd", "build_id": build_id}


async def export_custom_image_to_vhd(
    project_id: str,
    image_name: str,
    dest_bucket: str,
    dest_object: str,
    network: str = "",
    subnet: str = "",
    timeout: int = 7200,
    progress_cb=None,
) -> dict:
    """Export a GCE custom image to a VHD on GCS via the Daisy workflow.

    Returns {gs_url, format, build_id}. progress_cb is an optional sync callable
    taking a single string for streaming status into a Job log.
    """
    try:
        from google.cloud.devtools import cloudbuild_v1  # noqa: F401
    except ImportError:
        raise GCPError("google-cloud-build is not installed — run: pip install google-cloud-build")
    try:
        return await asyncio.to_thread(
            _export_custom_image_to_vhd_sync,
            project_id, image_name, dest_bucket, dest_object,
            network, subnet, timeout, progress_cb,
        )
    except GCPError:
        raise
    except Exception as e:
        raise GCPError(f"Failed to export image {image_name} to VHD: {e}") from e
