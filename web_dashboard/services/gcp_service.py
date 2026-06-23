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


def gke_get_token() -> str:
    """Mint a short-lived GCP OAuth access token for GKE API auth — the server-side
    equivalent of the ``gke-gcloud-auth-plugin`` exec a GKE kubeconfig invokes. GKE
    accepts a cloud-platform access token as a bearer token, so this lets a transient
    kubectl/helm container authenticate to GKE without ``gcloud``/the auth plugin,
    mirroring :func:`aws_service.eks_get_token`. Synchronous (called from the sync
    runner kubeconfig prep)."""
    from google.auth.transport.requests import Request
    creds = _gcp_creds()
    if creds is None:  # ADC
        try:
            import google.auth
        except ImportError:
            raise GCPError("google-auth is not installed — run: pip install google-auth")
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(Request())
    if not creds.token:
        raise GCPError("GKE token mint returned an empty access token")
    return creds.token


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


def _list_secret_names_sync(project_id: str) -> list:
    _require_secretmanager()
    from google.cloud import secretmanager_v1

    client = secretmanager_v1.SecretManagerServiceClient(credentials=_gcp_creds())
    parent = f"projects/{project_id}"
    return sorted(s.name.split("/")[-1] for s in client.list_secrets(request={"parent": parent}))


async def list_secret_names(project_id: str) -> list:
    """Return every Secret Manager secret id — candidate set for the per-launch
    SSH-key-secret override picker."""
    return await asyncio.to_thread(_list_secret_names_sync, project_id)


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


async def get_ssh_private_key(project_id: str, secret_name: str) -> str:
    """Retrieve the SSH **private** key (PEM) from Secret Manager.

    Only available when the secret is a JSON keypair with a ``private_key`` field
    (the same unified-keypair shape ``get_ssh_public_key`` reads ``public_key`` from).
    Returns ``""`` when the secret holds only a raw/public key — i.e. no private key
    is stored alongside. Never logs the key material.
    """
    raw = await get_secret(project_id, secret_name)
    try:
        data = json.loads(raw)
        priv = data.get("private_key") or data.get("privateKey") or ""
    except (json.JSONDecodeError, AttributeError):
        priv = ""  # raw secret = public key only; no private key available
    return priv.strip() if priv else ""


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
            labels = dict(info.labels) if info.labels else {}
            results.append({
                "instance_name": info.name,
                "zone":          zone,
                "machine_type":  info.machine_type.split("/")[-1],
                "status":        info.status,
                "public_ip":     public_ip,
                "private_ip":    private_ip,
                "self_link":     info.self_link,
                "creation_timestamp": info.creation_timestamp or "",
                "workgroup":     labels.get("workgroup") or None,
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


def _set_workgroup_label_sync(project_id: str, zone: str, instance_name: str, workgroup: str) -> None:
    """Merge a `workgroup` label into the instance (preserves other labels).
    Compute Engine requires the current `label_fingerprint` for optimistic
    concurrency on label edits."""
    _require_compute()
    from google.cloud import compute_v1

    creds = _gcp_creds()
    client = compute_v1.InstancesClient(credentials=creds)
    info = client.get(project=project_id, zone=zone, instance=instance_name)
    labels = dict(info.labels) if info.labels else {}
    labels["workgroup"] = workgroup
    req = compute_v1.InstancesSetLabelsRequest(
        labels=labels,
        label_fingerprint=info.label_fingerprint,
    )
    op = client.set_labels(
        project=project_id,
        zone=zone,
        instance=instance_name,
        instances_set_labels_request_resource=req,
    )
    try:
        op.result(timeout=30)
    except Exception:
        pass


async def set_workgroup_label(project_id: str, zone: str, instance_name: str, workgroup: str) -> None:
    """Rewrite the `workgroup` label on a GCE instance (preserves other labels).
    Used by the admin reassign endpoint."""
    await asyncio.to_thread(_set_workgroup_label_sync, project_id, zone, instance_name, workgroup)


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
    COS reads this on first boot and runs the container under containerd.

    ``securityContext.privileged: true`` is load-bearing for **protocol tunneling**:
    a BeyondTrust Jumpoint needs NET_ADMIN + NET_RAW + IPC_LOCK and access to
    /dev/net/tun. konlet only exposes the all-or-nothing ``privileged`` flag (no
    granular caps/devices), and privileged grants all of those plus host /dev
    (COS ships the tun module). Without it the jumpoint registers online but
    tunnel data times out — the same serverless limitation that rules out Cloud
    Run. A GCE COS VM is a real VM, so privileged IS permitted here."""
    import yaml
    spec = {
        "spec": {
            "containers": [{
                "name": "jumpoint",
                "image": container_image,
                "env": [{"name": "DEPLOY_KEY", "value": deploy_key}],
                "securityContext": {"privileged": True},
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


def _container_image_from_metadata(info) -> str:
    """Best-effort: pull the container image out of the gce-container-declaration
    metadata COS boots from. Returns "" on any parse failure."""
    try:
        import yaml
        for item in info.metadata.items:
            if item.key == "gce-container-declaration":
                spec = yaml.safe_load(item.value)
                return spec["spec"]["containers"][0]["image"]
    except Exception:
        pass
    return ""


def _list_gce_jumpoints_sync(project_id: str) -> list[dict]:
    """List Jumpoint COS instances across all zones (labels.purpose=bt-jumpoint).
    Aggregated list because each Jumpoint follows its paired VM's deploy zone."""
    _require_compute()
    from google.cloud import compute_v1

    creds = _gcp_creds()
    client = compute_v1.InstancesClient(credentials=creds)
    request = compute_v1.AggregatedListInstancesRequest(
        project=project_id,
        filter=f'labels.purpose = "{_JUMPOINT_LABEL}"',
    )

    results = []
    for zone_path, scoped in client.aggregated_list(request=request):
        for inst in (scoped.instances or []):
            labels = dict(inst.labels) if inst.labels else {}
            if labels.get("purpose") != _JUMPOINT_LABEL:
                continue
            internal_ip = ""
            external_ip = ""
            for nic in inst.network_interfaces:
                internal_ip = nic.network_i_p or internal_ip
                for ac in nic.access_configs:
                    if ac.nat_i_p:
                        external_ip = ac.nat_i_p
            results.append({
                "name":         inst.name,
                "zone":         zone_path.split("/")[-1],
                "status":       inst.status,
                "machine_type": inst.machine_type.split("/")[-1] if inst.machine_type else "",
                "image":        _container_image_from_metadata(inst),
                "internal_ip":  internal_ip,
                "external_ip":  external_ip,
                "created_at":   inst.creation_timestamp or "",
            })
    return results


async def list_gce_jumpoints(project_id: str) -> list[dict]:
    """List the BT Jumpoint container instances (COS on GCE) in the project."""
    try:
        return await asyncio.to_thread(_list_gce_jumpoints_sync, project_id)
    except GCPError:
        raise
    except Exception as e:
        raise GCPError(f"Failed to list GCE Jumpoint instances: {e}") from e


# ── Generic Docker Compose → GCE Container-Optimized OS instance ──────────────
# Cloud Run Jobs are single-container, so multi-service compose runs on a COS
# GCE instance via the gce-container-declaration konlet spec (same mechanism as
# the Jumpoint, generalized to N containers).

_COMPOSE_LABEL = "compose"

# konlet exposes a single instance-level restart policy.
_GCE_RESTART_MAP = {
    "always": "Always",
    "unless-stopped": "Always",
    "on-failure": "OnFailure",
    "no": "Never",
}


def _compose_container_spec_yaml(services: list) -> str:
    """Build the gce-container-declaration YAML for a parsed compose spec.

    COS containers share the VM's host network, so compose port mappings aren't
    expressed here — reachability is governed by the instance's firewall tags.
    konlet maps `command` → image ENTRYPOINT and `args` → image CMD, so compose
    `entrypoint` becomes konlet `command` and compose `command` becomes `args`."""
    import yaml
    containers = []
    restart_policy = "Always"
    for svc in services:
        c: dict = {"name": svc.name, "image": svc.image, "stdin": False, "tty": False}
        if svc.entrypoint:
            c["command"] = list(svc.entrypoint)
        if svc.command:
            c["args"] = list(svc.command)
        if svc.env:
            c["env"] = [{"name": k, "value": v} for k, v in svc.env]
        containers.append(c)
        if svc.restart:
            restart_policy = _GCE_RESTART_MAP.get(svc.restart, restart_policy)
    spec = {"spec": {"containers": containers, "restartPolicy": restart_policy}}
    return yaml.safe_dump(spec, default_flow_style=False)


def _deploy_compose_gce_sync(
    project_id: str,
    zone: str,
    name: str,
    services: list,
    machine_type: str = "e2-small",
    network: str = "",
    subnetwork: str = "",
    create_external_ip: bool = False,
    cos_image_family: str = "cos-stable",
) -> dict:
    """Launch a COS GCE instance running all compose services as containers."""
    _require_compute()
    from google.cloud import compute_v1
    from google.api_core.exceptions import NotFound

    creds = _gcp_creds()
    client = compute_v1.InstancesClient(credentials=creds)

    try:
        client.get(project=project_id, zone=zone, instance=name)
        raise GCPError(f"A GCE instance named '{name}' already exists in {zone}.")
    except NotFound:
        pass

    instance = compute_v1.Instance()
    instance.name = name
    instance.machine_type = f"zones/{zone}/machineTypes/{machine_type}"

    disk = compute_v1.AttachedDisk()
    disk.boot = True
    disk.auto_delete = True
    disk.initialize_params = compute_v1.AttachedDiskInitializeParams()
    disk.initialize_params.source_image = (
        f"projects/cos-cloud/global/images/family/{cos_image_family}"
    )
    disk.initialize_params.disk_size_gb = 10
    instance.disks = [disk]

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

    container_yaml = _compose_container_spec_yaml(services)
    instance.metadata = compute_v1.Metadata(items=[
        compute_v1.Items(key="gce-container-declaration", value=container_yaml),
        compute_v1.Items(key="google-logging-enabled", value="true"),
    ])
    instance.labels = {"managed-by": "vm-dashboard", "purpose": _COMPOSE_LABEL}

    logger.info(
        "GCE compose: creating COS instance '%s' in %s (%d services, machine=%s)",
        name, zone, len(services), machine_type,
    )
    op = client.insert(project=project_id, zone=zone, instance_resource=instance)
    op.result(timeout=300)

    info = client.get(project=project_id, zone=zone, instance=name)
    return {
        "name": name, "zone": zone, "self_link": info.self_link,
        "status": info.status, "containers": [s.name for s in services],
    }


async def deploy_compose_gce(
    project_id: str,
    zone: str,
    name: str,
    services: list,
    machine_type: str = "e2-small",
    network: str = "",
    subnetwork: str = "",
    create_external_ip: bool = False,
) -> dict:
    """Deploy a parsed compose spec to a new COS GCE instance."""
    try:
        return await asyncio.to_thread(
            _deploy_compose_gce_sync,
            project_id, zone, name, services, machine_type,
            network, subnetwork, create_external_ip, "cos-stable",
        )
    except GCPError:
        raise
    except Exception as e:
        raise GCPError(f"Failed to deploy compose to GCE: {e}") from e


def _list_gce_compose_sync(project_id: str) -> list[dict]:
    """List compose COS instances across all zones (labels.purpose=compose)."""
    _require_compute()
    from google.cloud import compute_v1

    creds = _gcp_creds()
    client = compute_v1.InstancesClient(credentials=creds)
    request = compute_v1.AggregatedListInstancesRequest(
        project=project_id,
        filter=f'labels.purpose = "{_COMPOSE_LABEL}"',
    )
    results = []
    for zone_path, scoped in client.aggregated_list(request=request):
        for inst in (scoped.instances or []):
            labels = dict(inst.labels) if inst.labels else {}
            if labels.get("purpose") != _COMPOSE_LABEL:
                continue
            internal_ip = ""
            external_ip = ""
            for nic in inst.network_interfaces:
                internal_ip = nic.network_i_p or internal_ip
                for ac in nic.access_configs:
                    if ac.nat_i_p:
                        external_ip = ac.nat_i_p
            results.append({
                "name":         inst.name,
                "zone":         zone_path.split("/")[-1],
                "status":       inst.status,
                "machine_type": inst.machine_type.split("/")[-1] if inst.machine_type else "",
                "image":        _container_image_from_metadata(inst),
                "internal_ip":  internal_ip,
                "external_ip":  external_ip,
                "created_at":   inst.creation_timestamp or "",
            })
    return results


async def list_gce_compose(project_id: str) -> list[dict]:
    """List the compose container instances (COS on GCE) in the project."""
    try:
        return await asyncio.to_thread(_list_gce_compose_sync, project_id)
    except GCPError:
        raise
    except Exception as e:
        raise GCPError(f"Failed to list GCE compose instances: {e}") from e


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


# ── Import GCS-staged tar.gz → custom image (cross-cloud promote target) ─────

def _create_image_from_gcs_sync(
    project_id: str,
    image_name: str,
    gcs_url: str,
    description: str,
    family: str,
    progress_cb,
) -> dict:
    """Call `compute.images.insert` with `rawDisk.source = <gs://...>` and
    poll until the operation completes. Returns
    {name, self_link, status}. Mirrors the AWS / Azure import shape so the
    image-registry orchestration can treat all three targets uniformly.

    The GCS object must be a .tar.gz containing exactly one `disk.raw`
    entry — see runners/promote/entrypoint.py for the wrapping step.
    """
    _require_compute()
    from google.cloud import compute_v1

    if progress_cb:
        progress_cb(f"Creating custom image '{image_name}' in {project_id} from {gcs_url}")

    creds = _gcp_creds()
    images_client = compute_v1.ImagesClient(credentials=creds)

    image = compute_v1.Image(
        name=image_name,
        description=description,
        family=family or None,
        raw_disk=compute_v1.RawDisk(source=gcs_url),
    )
    op = images_client.insert(project=project_id, image_resource=image)
    op.result()  # blocks until READY/FAILED

    created = images_client.get(project=project_id, image=image_name)
    if progress_cb:
        progress_cb(f"Image insert returned: status={created.status} ({created.self_link})")
    return {
        "name": created.name,
        "self_link": created.self_link,
        "status": created.status or "",
    }


async def create_image_from_gcs(
    project_id: str,
    image_name: str,
    gcs_url: str,
    description: str = "",
    family: str = "",
    progress_cb=None,
) -> dict:
    """Create a GCP custom image from a tar.gz on GCS. Returns
    {name, self_link, status}. Caller is expected to have already staged
    the tar.gz at `gcs_url` (the promote runner does this)."""
    try:
        return await asyncio.to_thread(
            _create_image_from_gcs_sync,
            project_id, image_name, gcs_url, description, family, progress_cb,
        )
    except GCPError:
        raise
    except Exception as e:
        raise GCPError(f"Failed to create image '{image_name}' from {gcs_url}: {e}") from e


# ── Cloud Run Promote-runner Job ─────────────────────────────────────────────

_PROMOTE_RUNNER_PREFIX = "promote-runner"


def _run_cloud_run_promote_runner_sync(
    project_id: str,
    region: str,
    image: str,
    runner_args: list,
    job_id: str,
    cpu: str,
    memory: str,
    timeout_seconds: int,
    vpc_connector: str = "",
    service_account_email: str = "",
) -> tuple:
    """Create a Cloud Run Job that runs the promote-runner image, wait for it
    to finish, return (exit_code, log_output), and delete the job.

    Modelled on `_run_cloud_run_ansible_sync` — same create/run/poll/delete
    shape. Differences:
      - Container args come from `runner_args`, not env-var-decoded script.
      - Wider default CPU/memory (qemu-img headroom for multi-GB VHDs).
      - Optional service account so the container can write to the dest
        GCS bucket via workload identity instead of a JSON key.
    """
    import time
    _require_run()
    from google.cloud import run_v2

    creds = _gcp_creds()
    jobs_client = run_v2.JobsClient(credentials=creds)
    executions_client = run_v2.ExecutionsClient(credentials=creds)

    job_name = f"{_PROMOTE_RUNNER_PREFIX}-{job_id[:8]}"
    parent = f"projects/{project_id}/locations/{region}"
    job_resource_name = f"{parent}/jobs/{job_name}"

    container = run_v2.Container(
        image=image,
        # Dockerfile ENTRYPOINT runs the python script; `args` becomes argv.
        # Cloud Run distinguishes command (override entrypoint) from args
        # (append to entrypoint), so we use `args` to keep the entrypoint
        # intact.
        args=list(runner_args),
        resources=run_v2.ResourceRequirements(
            limits={"cpu": cpu, "memory": memory},
        ),
    )

    task_template = run_v2.TaskTemplate(
        containers=[container],
        max_retries=0,
        timeout=f"{int(timeout_seconds)}s",
    )
    if service_account_email:
        task_template.service_account = service_account_email

    exec_template = run_v2.ExecutionTemplate(template=task_template)
    if vpc_connector:
        exec_template.annotations = {
            "run.googleapis.com/vpc-access-connector": vpc_connector,
            "run.googleapis.com/vpc-access-egress": "private-ranges-only",
        }

    job = run_v2.Job(
        template=exec_template,
        labels={"managed-by": "vm-cli-dashboard", "purpose": "promote-runner"},
    )

    logger.info(
        "Cloud Run promote-runner: creating job %s in %s/%s",
        job_name, project_id, region,
    )
    create_op = jobs_client.create_job(parent=parent, job_id=job_name, job=job)
    create_op.result()

    output = ""
    exit_code = 1
    execution_name = None

    try:
        run_op = jobs_client.run_job(name=job_resource_name)
        execution = run_op.result()
        execution_name = execution.name

        # Poll until completion. Multi-GB image transfers + qemu-img + tar
        # can easily exceed 20 minutes, so allow up to 2h by default.
        waited = 0
        # Poll every 10s; ceiling at the explicit timeout passed in (the
        # Cloud Run Job's own timeout will cut us off too).
        while waited < timeout_seconds:
            exec_info = executions_client.get_execution(name=execution_name)
            if exec_info.completion_time and not exec_info.reconciling:
                succeeded = exec_info.succeeded_count or 0
                failed = exec_info.failed_count or 0
                exit_code = 0 if (succeeded > 0 and failed == 0) else 1
                break
            time.sleep(10)
            waited += 10

        try:
            output = _fetch_cloud_run_job_logs(project_id, job_name, execution_name, creds)
        except Exception as log_err:
            logger.warning("Cloud Run promote-runner: could not retrieve logs: %s", log_err)
            output = f"(failed to retrieve Cloud Logging entries: {log_err})"

    finally:
        try:
            del_op = jobs_client.delete_job(name=job_resource_name)
            del_op.result()
            logger.info("Cloud Run promote-runner: deleted job %s", job_name)
        except Exception as del_err:
            logger.warning("Cloud Run promote-runner: could not delete job %s: %s", job_name, del_err)

    return exit_code, output


async def run_cloud_run_promote_runner_task(
    project_id: str,
    region: str,
    image: str,
    runner_args: list,
    job_id: str,
    cpu: str = "2000m",
    memory: str = "4Gi",
    timeout_seconds: int = 7200,
    vpc_connector: str = "",
    service_account_email: str = "",
) -> tuple:
    """Run the promote-runner image as a Cloud Run Job. Returns
    (exit_code, log_output)."""
    try:
        return await asyncio.to_thread(
            _run_cloud_run_promote_runner_sync,
            project_id, region, image, runner_args, job_id,
            cpu, memory, timeout_seconds, vpc_connector, service_account_email,
        )
    except GCPError:
        raise
    except Exception as e:
        raise GCPError(f"Failed to run Cloud Run promote-runner job: {e}") from e


def _delete_gcs_object_sync(bucket: str, object_name: str) -> None:
    """Delete a single GCS object. Used for promote cleanup after the
    cloud-side image-create reaches READY."""
    try:
        from google.cloud import storage as gcs  # noqa: F401
    except ImportError:
        raise GCPError("google-cloud-storage is not installed")
    from google.cloud import storage as gcs
    client = gcs.Client(credentials=_gcp_creds(), project=_cfg("gcp_project_id"))
    client.bucket(bucket).blob(object_name).delete()


async def delete_gcs_object(bucket: str, object_name: str) -> None:
    """Best-effort cleanup of a staged GCS object. Targets an explicit bucket
    so it works even when the staging lives outside `storage_gcs_bucket`."""
    try:
        await asyncio.to_thread(_delete_gcs_object_sync, bucket, object_name)
    except Exception as e:
        raise GCPError(f"Failed to delete gs://{bucket}/{object_name}: {e}") from e
