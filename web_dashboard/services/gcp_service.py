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
import time
from datetime import datetime, timedelta, timezone
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


# ── GKE fleet + Connect Gateway + IAM (Workforce Identity Federation) ─────────
#
# The "Entra federation" action's GCP leg. A workforce (WIF) user reaches a private
# GKE cluster ONLY through Connect Gateway (connectgateway.googleapis.com) — the
# cluster's own authenticator can't validate a workforce token — so we register the
# cluster to the project's fleet, enable the Connect Gateway APIs, and grant the
# workforce group's principalSet the gkehub.gateway* roles. Kubernetes RBAC (the
# principalSet ClusterRoleBinding) is applied separately by k8s_service. All REST via
# an AuthorizedSession (google-auth only — gkehub/resourcemanager have no client lib
# wired here). Synchronous; callers wrap in asyncio.to_thread.

_GATEWAY_ROLES = ("roles/gkehub.gatewayEditor", "roles/gkehub.viewer")


def _authed_session():
    """An AuthorizedSession bound to the dashboard's GCP creds (SA JSON or ADC),
    cloud-platform scope — for the fleet / Connect Gateway / IAM REST calls."""
    from google.auth.transport.requests import AuthorizedSession
    creds = _gcp_creds()
    if creds is None:  # ADC
        import google.auth
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(creds)


def _poll_lro(session, op: dict, base: str, timeout_s: int = 300) -> dict:
    """Poll a GCP long-running operation (``op`` has a ``name``) under ``base`` (the
    API root, e.g. https://gkehub.googleapis.com/v1) until done. Returns the final op."""
    import time
    if not op.get("name") or op.get("done"):
        return op
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = session.get(f"{base}/{op['name']}")
        r.raise_for_status()
        cur = r.json()
        if cur.get("done"):
            if cur.get("error"):
                raise GCPError(f"GCP operation failed: {cur['error']}")
            return cur
        time.sleep(5)
    raise GCPError(f"GCP operation {op['name']} did not complete within {timeout_s}s")


def project_number(project: str = "") -> str:
    """Resolve a project's numeric id (the Connect Gateway URL uses the number)."""
    project = project or _gcp_project()
    s = _authed_session()
    r = s.get(f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}")
    r.raise_for_status()
    return str(r.json().get("projectNumber") or "")


def enable_connect_gateway_apis(project: str = "") -> None:
    """Ensure the APIs Connect Gateway needs are enabled (idempotent)."""
    project = project or _gcp_project()
    s = _authed_session()
    r = s.post(
        f"https://serviceusage.googleapis.com/v1/projects/{project}/services:batchEnable",
        json={"serviceIds": ["connectgateway.googleapis.com",
                             "gkeconnect.googleapis.com", "gkehub.googleapis.com"]})
    r.raise_for_status()
    _poll_lro(s, r.json(), "https://serviceusage.googleapis.com/v1")


def register_fleet_membership(project: str, location: str, cluster_name: str,
                              membership_id: str = "") -> str:
    """Register a GKE cluster to the project's fleet (idempotent) so Connect Gateway
    can reach it; returns the membership id. ``location`` is the cluster's zone or
    region. GKE-on-GCP clusters get the Connect agent installed automatically."""
    project = project or _gcp_project()
    membership_id = membership_id or cluster_name
    s = _authed_session()
    base = "https://gkehub.googleapis.com/v1"
    parent = f"projects/{project}/locations/global"
    if s.get(f"{base}/{parent}/memberships/{membership_id}").status_code == 200:
        return membership_id  # already registered
    resource_link = (f"//container.googleapis.com/projects/{project}"
                     f"/locations/{location}/clusters/{cluster_name}")
    r = s.post(f"{base}/{parent}/memberships?membershipId={membership_id}",
               json={"endpoint": {"gkeCluster": {"resourceLink": resource_link}}})
    r.raise_for_status()
    _poll_lro(s, r.json(), base)
    return membership_id


def grant_gateway_iam(principal_set: str, project: str = "", roles: tuple = _GATEWAY_ROLES) -> None:
    """Add ``principal_set`` to the Connect Gateway IAM roles on the project so its
    members can reach the cluster through the gateway (idempotent)."""
    _modify_project_iam(principal_set, project, roles, add=True)


def revoke_gateway_iam(principal_set: str, project: str = "", roles: tuple = _GATEWAY_ROLES) -> None:
    """Remove ``principal_set`` from the Connect Gateway IAM roles (idempotent)."""
    _modify_project_iam(principal_set, project, roles, add=False)


def _modify_project_iam(member: str, project: str, roles: tuple, *, add: bool) -> None:
    project = project or _gcp_project()
    s = _authed_session()
    base = f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}"
    r = s.post(f"{base}:getIamPolicy", json={"options": {"requestedPolicyVersion": 3}})
    r.raise_for_status()
    policy = r.json()
    bindings = policy.setdefault("bindings", [])
    changed = False
    for role in roles:
        b = next((x for x in bindings if x.get("role") == role and not x.get("condition")), None)
        if add:
            if b is None:
                bindings.append({"role": role, "members": [member]})
                changed = True
            elif member not in b.get("members", []):
                b.setdefault("members", []).append(member)
                changed = True
        elif b is not None and member in b.get("members", []):
            b["members"].remove(member)
            changed = True
    if changed:
        s.post(f"{base}:setIamPolicy", json={"policy": policy}).raise_for_status()


def connect_gateway_server_url(membership_id: str, project: str = "", location: str = "global") -> str:
    """The Connect Gateway kube-apiserver URL for a fleet membership (uses the project
    NUMBER, per the gateway URL scheme)."""
    return (f"https://connectgateway.googleapis.com/v1/projects/{project_number(project)}"
            f"/locations/{location}/gkeMemberships/{membership_id}")


def find_gke_cluster(name: str, project: str = "") -> tuple:
    """Locate a GKE cluster by name across all locations in the project. Returns
    (name, location) — location is the zone (zonal) or region (regional), needed for
    the fleet membership resourceLink. Raises when the cluster isn't found."""
    project = project or _gcp_project()
    s = _authed_session()
    r = s.get(f"https://container.googleapis.com/v1/projects/{project}/locations/-/clusters")
    r.raise_for_status()
    for c in (r.json().get("clusters") or []):
        if c.get("name") == name:
            return c["name"], c.get("location") or ""
    raise GCPError(f"GKE cluster '{name}' not found in project {project}")


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

    # Reuse if already present — but "reused" must mean a LIVE gateway. A name match
    # alone isn't enough: a STOPPED/TERMINATED VM was previously reported reused with a
    # dead jumpoint (no gateway for the tunnel). If it isn't RUNNING, start it — COS
    # re-runs the jumpoint container on boot — and wait for RUNNING before returning.
    try:
        existing = client.get(project=project_id, zone=zone, instance=name)
        status = existing.status
        if status != "RUNNING":
            logger.info("GCE Jumpoint '%s' exists but status=%s — starting it for a live gateway",
                        name, status)
            try:
                start_op = client.start(project=project_id, zone=zone, instance=name)
                start_op.result(timeout=180)
                existing = client.get(project=project_id, zone=zone, instance=name)
                status = existing.status
            except Exception as start_err:
                logger.warning("GCE Jumpoint '%s' start failed (status=%s): %s",
                               name, status, start_err)
        return {
            "name": name, "zone": zone, "self_link": existing.self_link,
            "status": status, "reused": True,
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


# ── Rancher management node on COS-on-GCE ─────────────────────────────────────
# The central Rancher server runs as a SINGLE privileged container on a COS GCE
# VM with a PUBLIC (source-restricted) IP — the same konlet mechanism as the
# Jumpoint above, and the direct analogue of Rancher's single-node docker
# install (`docker run -d --privileged -p 80:80 -p 443:443 rancher/rancher`).
# Privileged is required for Rancher's embedded components; COS runs the
# container on the HOST network, so 80/443 bind on the VM and reachability is
# governed by the firewall (see _ensure_rancher_firewall_sync). The node is
# EPHEMERAL: the boot disk auto-deletes and the IP is ephemeral, so a delete
# wipes state — acceptable for the disposable-lab posture.

_RANCHER_LABEL = "rancher"

# Shared-core / <4 GB machine types Rancher will OOM on. Best-effort guard; not
# exhaustive (custom types etc.), but catches the common footguns.
_RANCHER_TOO_SMALL = {"e2-micro", "e2-small", "f1-micro", "g1-small", "e2-highcpu-2"}


def _rancher_container_spec_yaml(container_image: str, bootstrap_password: str) -> str:
    """Generate the gce-container-declaration konlet YAML for the Rancher server.

    ``securityContext.privileged: true`` mirrors the single-node docker install's
    ``--privileged``. No port block is needed: COS runs the container on the host
    network, so Rancher binds host 80/443 directly (firewall-governed). The
    bootstrap password is injected as ``CATTLE_BOOTSTRAP_PASSWORD`` (Rancher 2.6+
    first-run admin password)."""
    import yaml
    spec = {
        "spec": {
            "containers": [{
                "name": "rancher",
                "image": container_image,
                "env": [{"name": "CATTLE_BOOTSTRAP_PASSWORD", "value": bootstrap_password}],
                "securityContext": {"privileged": True},
                "stdin": False,
                "tty": False,
            }],
            "restartPolicy": "Always",
        }
    }
    return yaml.safe_dump(spec, default_flow_style=False)


def _ensure_rancher_firewall_sync(
    project_id: str,
    network: str,
    tag: str,
    source_cidrs: list[str],
    name: str,
) -> dict:
    """Get-or-create/patch a source-restricted INGRESS firewall (tcp 80/443)
    scoped to the Rancher VM's network ``tag``. Idempotent: patches source_ranges
    on an existing rule so CIDR edits in Settings take effect on redeploy.

    Fail-closed: an EMPTY ``source_cidrs`` opens nothing — if a rule by this name
    already exists it is DELETED (removing CIDRs closes the node). The caller
    (rancher_node_service) decides whether an empty list means "closed" or
    (with gcp_rancher_allow_open) ["0.0.0.0/0"]."""
    _require_compute()
    from google.cloud import compute_v1
    from google.api_core.exceptions import NotFound

    creds = _gcp_creds()
    client = compute_v1.FirewallsClient(credentials=creds)

    if not source_cidrs:
        # Fail closed — ensure no rule is left open.
        try:
            op = client.delete(project=project_id, firewall=name)
            op.result(timeout=60)
            logger.warning("Rancher firewall '%s' deleted — no allowed source CIDRs (node is unreachable)", name)
        except NotFound:
            pass
        return {"name": name, "opened": False}

    fw = compute_v1.Firewall()
    fw.name = name
    fw.network = network if "/" in network else f"global/networks/{network or 'default'}"
    fw.direction = "INGRESS"
    fw.allowed = [compute_v1.Allowed(I_p_protocol="tcp", ports=["80", "443"])]
    fw.source_ranges = list(source_cidrs)
    fw.target_tags = [tag]

    try:
        client.get(project=project_id, firewall=name)
        op = client.patch(project=project_id, firewall=name, firewall_resource=fw)
        op.result(timeout=60)
        created = False
    except NotFound:
        op = client.insert(project=project_id, firewall_resource=fw)
        op.result(timeout=60)
        created = True
    logger.info("Rancher firewall '%s' %s (tcp 80/443, sources=%s, tag=%s)",
                name, "created" if created else "updated", source_cidrs, tag)
    return {"name": name, "opened": True, "created": created}


def _delete_rancher_firewall_sync(project_id: str, name: str) -> None:
    """Delete the Rancher ingress firewall rule (quiet no-op if absent)."""
    _require_compute()
    from google.cloud import compute_v1
    from google.api_core.exceptions import NotFound
    creds = _gcp_creds()
    client = compute_v1.FirewallsClient(credentials=creds)
    try:
        op = client.delete(project=project_id, firewall=name)
        op.result(timeout=60)
    except NotFound:
        pass


def _external_ip_of(info) -> str:
    """Best-effort: pull the ephemeral external IP off a compute instance."""
    for nic in (info.network_interfaces or []):
        for ac in (nic.access_configs or []):
            if ac.nat_i_p:
                return ac.nat_i_p
    return ""


def _run_gce_rancher_sync(
    project_id: str,
    zone: str,
    name: str,
    container_image: str,
    bootstrap_password: str,
    network: str = "",
    subnetwork: str = "",
    machine_type: str = "e2-medium",
    boot_disk_gb: int = 30,
    network_tag: str = "rancher",
    cos_image_family: str = "cos-stable",
    create_external_ip: bool = True,
) -> dict:
    """Launch (or reuse) a COS GCE instance running the Rancher server container.
    Idempotent on existence: a RUNNING same-named VM is returned as-is; a stopped
    one is started (COS re-runs the container on boot). Returns the public IP +
    derived https URL so the caller can pin server-url and bootstrap."""
    if machine_type in _RANCHER_TOO_SMALL:
        raise GCPError(
            f"machine_type '{machine_type}' has <4 GB RAM — Rancher will OOM. "
            f"Use e2-medium (4 GB) or larger.")
    _require_compute()
    from google.cloud import compute_v1
    from google.api_core.exceptions import NotFound

    creds = _gcp_creds()
    client = compute_v1.InstancesClient(credentials=creds)

    # Reuse a LIVE node. As with the Jumpoint, a name match isn't enough — a
    # stopped VM has no Rancher listening, so start it and wait for RUNNING.
    try:
        existing = client.get(project=project_id, zone=zone, instance=name)
        status = existing.status
        if status != "RUNNING":
            logger.info("GCE Rancher '%s' exists but status=%s — starting it", name, status)
            try:
                start_op = client.start(project=project_id, zone=zone, instance=name)
                start_op.result(timeout=180)
                existing = client.get(project=project_id, zone=zone, instance=name)
                status = existing.status
            except Exception as start_err:
                logger.warning("GCE Rancher '%s' start failed (status=%s): %s", name, status, start_err)
        external_ip = _external_ip_of(existing) if create_external_ip else ""
        return {
            "name": name, "zone": zone, "self_link": existing.self_link,
            "status": status, "external_ip": external_ip,
            "url": f"https://{external_ip}" if external_ip else "", "reused": True,
        }
    except NotFound:
        pass

    instance = compute_v1.Instance()
    instance.name = name
    instance.machine_type = f"zones/{zone}/machineTypes/{machine_type}"

    # Boot disk from Container-Optimised OS. Ephemeral: auto_delete=True — a VM
    # delete discards /var/lib/rancher (state), matching the disposable posture.
    disk = compute_v1.AttachedDisk()
    disk.boot = True
    disk.auto_delete = True
    disk.initialize_params = compute_v1.AttachedDiskInitializeParams()
    disk.initialize_params.source_image = (
        f"projects/cos-cloud/global/images/family/{cos_image_family}"
    )
    disk.initialize_params.disk_size_gb = boot_disk_gb
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

    container_yaml = _rancher_container_spec_yaml(container_image, bootstrap_password)
    instance.metadata = compute_v1.Metadata(items=[
        compute_v1.Items(key="gce-container-declaration", value=container_yaml),
        compute_v1.Items(key="google-logging-enabled", value="true"),
    ])
    instance.labels = {"managed-by": "vm-dashboard", "purpose": _RANCHER_LABEL}
    instance.tags = compute_v1.Tags(items=[network_tag])

    logger.info("Starting GCE COS Rancher '%s' in %s (image=%s, machine=%s)",
                name, zone, container_image, machine_type)
    op = client.insert(project=project_id, zone=zone, instance_resource=instance)
    op.result(timeout=300)

    info = client.get(project=project_id, zone=zone, instance=name)
    external_ip = _external_ip_of(info) if create_external_ip else ""
    return {
        "name": name, "zone": zone, "self_link": info.self_link,
        "status": info.status, "external_ip": external_ip,
        "url": f"https://{external_ip}" if external_ip else "", "reused": False,
    }


async def run_gce_rancher(
    project_id: str,
    zone: str,
    name: str,
    container_image: str,
    bootstrap_password: str,
    network: str = "",
    subnetwork: str = "",
    machine_type: str = "e2-medium",
    boot_disk_gb: int = 30,
    network_tag: str = "rancher",
    create_external_ip: bool = True,
) -> dict:
    """Async wrapper for _run_gce_rancher_sync."""
    try:
        return await asyncio.to_thread(
            _run_gce_rancher_sync,
            project_id, zone, name, container_image, bootstrap_password,
            network, subnetwork, machine_type, boot_disk_gb, network_tag,
            "cos-stable", create_external_ip,
        )
    except GCPError:
        raise
    except Exception as e:
        raise GCPError(f"Failed to start GCE Rancher node '{name}': {e}") from e


async def ensure_rancher_firewall(project_id: str, network: str, tag: str,
                                  source_cidrs: list[str], name: str) -> dict:
    """Async wrapper for _ensure_rancher_firewall_sync."""
    try:
        return await asyncio.to_thread(
            _ensure_rancher_firewall_sync, project_id, network, tag, source_cidrs, name)
    except GCPError:
        raise
    except Exception as e:
        raise GCPError(f"Failed to configure Rancher firewall '{name}': {e}") from e


async def stop_gce_rancher(project_id: str, zone: str, name: str, *,
                           delete_firewall: bool = False, firewall_name: str = "") -> None:
    """Delete the Rancher node VM (quiet no-op if absent). Optionally delete its
    ingress firewall rule too."""
    try:
        await asyncio.to_thread(_terminate_instance_sync, project_id, zone, name)
    except Exception as e:
        msg = str(e)
        if not ("404" in msg or "not found" in msg.lower()):
            raise GCPError(f"Failed to stop GCE Rancher node '{name}': {e}") from e
    if delete_firewall and firewall_name:
        try:
            await asyncio.to_thread(_delete_rancher_firewall_sync, project_id, firewall_name)
        except Exception as e:
            logger.warning("Rancher firewall '%s' delete failed (continuing): %s", firewall_name, e)


def _list_gce_rancher_sync(project_id: str) -> list[dict]:
    """List Rancher node COS instances across zones (labels.purpose=rancher)."""
    _require_compute()
    from google.cloud import compute_v1

    creds = _gcp_creds()
    client = compute_v1.InstancesClient(credentials=creds)
    request = compute_v1.AggregatedListInstancesRequest(
        project=project_id,
        filter=f'labels.purpose = "{_RANCHER_LABEL}"',
    )
    results = []
    for zone_path, scoped in client.aggregated_list(request=request):
        for inst in (scoped.instances or []):
            labels = dict(inst.labels) if inst.labels else {}
            if labels.get("purpose") != _RANCHER_LABEL:
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
                "url":          f"https://{external_ip}" if external_ip else "",
                "created_at":   inst.creation_timestamp or "",
            })
    return results


async def list_gce_rancher(project_id: str) -> list[dict]:
    """List the Rancher management-node container instances (COS on GCE)."""
    try:
        return await asyncio.to_thread(_list_gce_rancher_sync, project_id)
    except GCPError:
        raise
    except Exception as e:
        raise GCPError(f"Failed to list GCE Rancher node instances: {e}") from e


# ── Cloud Run Jobs Ansible runner (mirrors ACI runner in azure_service.py) ───

_ANSIBLE_RUNNER_PREFIX = "ansible-runner"


def _require_run():
    try:
        from google.cloud import run_v2  # noqa: F401
    except ImportError:
        raise GCPError("google-cloud-run is not installed — run: pip install google-cloud-run")


def _fetch_cloud_run_job_logs(project_id: str, job_name: str, execution_name: str, creds,
                              start_rfc3339: str = "") -> str:
    """Retrieve Cloud Run job stdout/stderr from Cloud Logging via REST."""
    try:
        from google.auth.transport.requests import AuthorizedSession
        import requests as _requests
    except ImportError:
        return ""

    session = AuthorizedSession(creds or _gcp_creds())
    url = "https://logging.googleapis.com/v2/entries:list"
    # execution_name may be absent (e.g. run_job failed before returning the
    # execution) — fall back to filtering by job_name so failures are still
    # surfaced rather than crashing on a None .split().
    filter_parts = [
        'resource.type="cloud_run_job"',
        f'resource.labels.job_name="{job_name}"',
    ]
    # Scope to THIS run by timestamp rather than the per-execution label: the label
    # (run.googleapis.com/execution_name) IS on the entries, but it lags Cloud Logging
    # ingestion past our retry window, so filtering on it returned nothing. The floor is
    # this run's start — delete-before-create means the fixed job name isn't reused
    # concurrently, so only this run's logs are at/after it.
    if start_rfc3339:
        filter_parts.append(f'timestamp>="{start_rfc3339}"')
    body = {
        "resourceNames": [f"projects/{project_id}"],
        "filter": " ".join(filter_parts),
        "orderBy": "timestamp asc",
        "pageSize": 1000,
    }
    # Cloud Logging ingestion lags container exit by seconds-to-tens-of-seconds. The
    # k8s runner reads command OUTPUT from these logs (e.g. a minted SA token via
    # `kubectl create token`), so an immediate query usually returns nothing — retry
    # until entries appear rather than handing the caller an empty result.
    import json as _json
    import time
    lines = []
    last_count = -1
    for _attempt in range(24):  # ~120s: Cloud Logging ingestion of Cloud Run Job stdout lags well past a few seconds here
        resp = session.post(url, json=body, timeout=30)
        resp.raise_for_status()
        entries = resp.json().get("entries", [])
        last_count = len(entries)
        if entries:
            for entry in entries:
                text = entry.get("textPayload") or entry.get("jsonPayload", {}).get("message", "")
                if text:
                    lines.append(text)
            if lines:
                break
            logger.warning("Cloud Run k8s logs: %d entries but no text in textPayload/jsonPayload.message "
                           "— raw sample: %s", len(entries), _json.dumps(entries[:2])[:1800])
            break
        time.sleep(5)
    if not lines:
        logger.warning("Cloud Run k8s logs: no usable text (last entry count=%d, filter=%s)",
                       last_count, " ".join(filter_parts))
    return "\n".join(lines)


def _run_cloud_run_ansible_sync(
    project_id: str, region: str, image: str,
    target_ip: str, ansible_user: str,
    playbook_b64: str, ssh_key_b64: str, job_id: str,
    vpc_connector: str = "",
    secret_entries: list | None = None, manifest_b64: str = "",
    service_account: str = "",
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

    from . import cloud_ansible_secrets as _cas
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

    # GCP secret-env: each secret var references a Secret Manager secret (must live
    # in this project). The manifest env is plain (non-secret env→var mapping).
    _secret_env = []
    if manifest_b64:
        _secret_env.append(run_v2.EnvVar(name=_cas.MANIFEST_ENV, value=manifest_b64))
        for e in (secret_entries or []):
            _secret_env.append(run_v2.EnvVar(
                name=e["env"],
                value_source=run_v2.EnvVarSource(
                    secret_key_ref=run_v2.SecretKeySelector(secret=e["secret_name"], version="latest")),
            ))

    task_template = run_v2.TaskTemplate(
        containers=[
            run_v2.Container(
                image=image,
                command=["sh", "-c", cmd],
                env=[
                    run_v2.EnvVar(name="PLAYBOOK_B64", value=playbook_b64),
                    run_v2.EnvVar(name="SSH_KEY_B64", value=ssh_key_b64),
                ] + _secret_env,
                resources=run_v2.ResourceRequirements(
                    limits={"cpu": "1000m", "memory": "512Mi"},
                ),
            )
        ],
        max_retries=0,
        timeout="1200s",
        # Pin the job identity so an ephemeral secret's accessor can be bound to
        # exactly this SA (secret-env / ephemeral managed-account checkout). Blank
        # → the project default compute SA (unchanged legacy behaviour).
        **({"service_account": service_account} if service_account else {}),
    )

    exec_template = run_v2.ExecutionTemplate(template=task_template)
    if vpc_connector:
        exec_template.annotations = {
            "run.googleapis.com/vpc-access-connector": vpc_connector,
            "run.googleapis.com/vpc-access-egress": "private-ranges-only",
        }

    job = run_v2.Job(
        template=exec_template,
        labels={"managed-by": "vm-dashboard", "purpose": "ansible-runner"},
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
    service_account: str = "",
    secret_entries: list | None = None, manifest_b64: str = "",
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
            secret_entries, manifest_b64,
            service_account,
        )
    except GCPError:
        raise
    except Exception as e:
        raise GCPError(f"Failed to run Cloud Run Ansible task: {e}") from e


# ── Cloud Run Kubernetes runner ───────────────────────────────────────────────

_K8S_RUNNER_PREFIX = "k8s-runner"


def _run_cloud_run_k8s_sync(
    project_id: str, region: str, image: str,
    command: str, kubeconfig_b64: str, stdin_b64: str, job_id: str,
    vpc_connector: str = "",
) -> tuple:
    """
    Create a Cloud Run Job that runs a single kubectl/helm command against a
    cluster's API, wait for it to finish, return (exit_code, log_output), and
    delete the job.

    Modelled on `_run_cloud_run_ansible_sync` — same create / run / poll / logs /
    cleanup shape. The stock kubectl+helm `image`, the generic shell `command`,
    and the kubeconfig (decoded from the ``KUBECONFIG_B64`` env into
    ``$KUBECONFIG``) are the only differences.
    """
    import time
    _require_run()
    from google.cloud import run_v2

    creds = _gcp_creds()
    jobs_client = run_v2.JobsClient(credentials=creds)
    executions_client = run_v2.ExecutionsClient(credentials=creds)

    # Unique per invocation: a fixed "-adhoc" name (empty job_id — the SA-token mint,
    # apply/get helpers) collided (409 "already exists") when two runner ops overlapped
    # or one left a leftover. A uuid suffix removes the clash entirely; the finally-block
    # still deletes this run's job, and the log fetch scopes by this exact name + time.
    import uuid
    _suffix = job_id[:8] if job_id else uuid.uuid4().hex[:8]
    job_name = f"{_K8S_RUNNER_PREFIX}-{_suffix}"
    parent = f"projects/{project_id}/locations/{region}"
    job_resource_name = f"{parent}/jobs/{job_name}"

    setup = (
        "set -e; "
        'printf %s "$KUBECONFIG_B64" | base64 -d > /tmp/kubeconfig; '
        "export KUBECONFIG=/tmp/kubeconfig; "
    )
    if stdin_b64:
        full_cmd = setup + 'printf %s "$STDIN_B64" | base64 -d | ' + command
    else:
        full_cmd = setup + command

    env = [run_v2.EnvVar(name="KUBECONFIG_B64", value=kubeconfig_b64)]
    if stdin_b64:
        env.append(run_v2.EnvVar(name="STDIN_B64", value=stdin_b64))

    task_template = run_v2.TaskTemplate(
        containers=[
            run_v2.Container(
                image=image,
                command=["sh", "-c", full_cmd],
                env=env,
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
        labels={"managed-by": "vm-dashboard", "purpose": "k8s-runner"},
    )

    # A prior run's job can linger — its delete in the finally below failed, the run
    # crashed before reaching it, or two adhoc runs raced on the fixed
    # "k8s-runner-adhoc" name — which then 409s "already exists" on create. Remove any
    # leftover of the same name first (best-effort; NotFound is the normal case).
    try:
        jobs_client.delete_job(name=job_resource_name).result()
        logger.info("Cloud Run k8s: removed a pre-existing job %s before create", job_name)
    except Exception as stale_err:
        logger.debug("Cloud Run k8s: no pre-existing job %s to remove (%s)", job_name, stale_err)

    logger.info("Cloud Run k8s: creating job %s in %s/%s", job_name, project_id, region)
    create_op = jobs_client.create_job(parent=parent, job_id=job_name, job=job)
    create_op.result()

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
            # Scope the log query to this execution's start (create_time) so a reused
            # job name doesn't pull a prior run's output; the label-based scope lagged
            # ingestion. run_v2 exposes create_time as a tz-aware datetime.
            _ct = getattr(execution, "create_time", None)
            _floor = _ct.isoformat().replace("+00:00", "Z") if hasattr(_ct, "isoformat") else ""
            output = _fetch_cloud_run_job_logs(project_id, job_name, execution_name, creds, start_rfc3339=_floor)
        except Exception as log_err:
            logger.warning("Cloud Run k8s: could not retrieve logs: %s", log_err)

    finally:
        try:
            del_op = jobs_client.delete_job(name=job_resource_name)
            del_op.result()
            logger.info("Cloud Run k8s: deleted job %s", job_name)
        except Exception as del_err:
            logger.warning("Cloud Run k8s: could not delete job %s: %s", job_name, del_err)

    return exit_code, output


async def run_cloud_run_k8s_task(
    *,
    project_id: str, region: str, image: str,
    command: str, kubeconfig_b64: str, stdin_b64: str = "", job_id: str,
    vpc_connector: str = "",
) -> tuple:
    """
    Run a kubectl/helm command against a cluster's API via a GCP Cloud Run Job.
    Returns (exit_code, output_log).
    """
    try:
        return await asyncio.to_thread(
            _run_cloud_run_k8s_sync,
            project_id, region, image,
            command, kubeconfig_b64, stdin_b64, job_id,
            vpc_connector,
        )
    except GCPError:
        raise
    except Exception as e:
        raise GCPError(f"Failed to run Cloud Run k8s task: {e}") from e


# ── Export custom image to VHD on GCS (portable artefact) ─────────────────────

def _fetch_cloud_build_log(project_id: str, build_id: str, creds, tail: int = 30,
                           since: Optional[datetime] = None) -> str:
    """Return the last `tail` non-empty lines of a Cloud Build's step output from
    Cloud Logging. Used to surface the *real* export failure (e.g. the Daisy
    `ZONE_RESOURCE_POOL_EXHAUSTED` or a 503 SERVICE_UNAVAILABLE creating the
    export worker VM) instead of the SDK's generic "Build failed; check build
    logs for details".

    A timestamp lower bound is REQUIRED, not optional: Cloud Logging's
    ``entries:list`` returns *nothing* for an otherwise-correct build_id filter
    without one (measured live: 0 entries vs 112 with a bound). Omitting it
    silently defeated this whole surfacing path — the fetch returned empty and
    the job fell back to the generic "Build failed". Pass the build's create_time
    as ``since`` for a tight window; otherwise a 24h lookback is used (the exact
    build_id filter keeps it from matching unrelated builds)."""
    if not build_id:
        return ""
    try:
        from google.auth.transport.requests import AuthorizedSession
    except ImportError:
        return ""
    floor = since or (datetime.now(timezone.utc) - timedelta(hours=24))
    if floor.tzinfo is None:
        floor = floor.replace(tzinfo=timezone.utc)
    # A few minutes of slack absorbs create_time vs log-ingestion clock skew.
    ts = (floor - timedelta(minutes=5)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    session = AuthorizedSession(creds or _gcp_creds())
    body = {
        "resourceNames": [f"projects/{project_id}"],
        "filter": (f'resource.type="build" resource.labels.build_id="{build_id}" '
                   f'timestamp>="{ts}"'),
        "orderBy": "timestamp asc",
        "pageSize": 1000,
    }
    resp = session.post("https://logging.googleapis.com/v2/entries:list", json=body, timeout=30)
    resp.raise_for_status()
    lines = []
    for entry in resp.json().get("entries", []):
        text = entry.get("textPayload") or entry.get("jsonPayload", {}).get("message", "")
        if text and text.strip():
            lines.append(text.rstrip())
    return "\n".join(lines[-tail:])


# Substrings that mark a *transient* Compute capacity/availability failure while
# Daisy creates the export worker VM — worth an automatic retry, since a fresh
# build often lands in a zone/pool that has room. Deliberately excludes quota
# and permission errors, which won't fix themselves on retry.
_TRANSIENT_EXPORT_MARKERS = (
    "service unavailable",
    "httperrorstatuscode:503",
    "code: 503",
    "zone_resource_pool_exhausted",
    "resource_pool_exhausted",
    "resource pool exhausted",
    "does not have enough resources",
    "resource_availability",
)


def _is_transient_export_error(text: str) -> bool:
    """True if an export failure detail looks like a retryable Compute-capacity blip."""
    tl = (text or "").lower()
    return any(m in tl for m in _TRANSIENT_EXPORT_MARKERS)


def _export_build_error_detail(project_id, build_id, creds, since, fallback: str) -> str:
    """Best-effort real error for a failed export build: the Cloud Logging step-log
    tail if we can read it, else ``fallback`` (usually the SDK's generic message)."""
    tail = ""
    try:
        tail = _fetch_cloud_build_log(project_id, build_id, creds, since=since)
    except Exception as log_err:
        logger.warning("Cloud Build export: could not retrieve build log: %s", log_err)
    return tail.strip() or fallback


def _export_candidate_zones(project_id: str, preferred_zone: str, creds, limit: int = 10) -> list:
    """Ordered list of zones to try for the export worker VM.

    The Daisy exporter creates a temporary VM in a single zone and fails hard
    with ZONE_RESOURCE_POOL_EXHAUSTED when that zone is out of capacity — and,
    left to its own devices, it keeps picking the *same* zone, so a plain retry
    doesn't help. Tried in order:
      1. the preferred zone,
      2. the preferred region's other UP zones (a single-zone blip), then
      3. one UP zone from each *other* US region — so a whole-region outage
         (e.g. us-central1 at capacity, observed 2026-07-15) still finds
         somewhere to run the export. The source image is global and the
         exporter auto-creates a per-region scratch bucket, so a cross-region
         worker is fine; it just adds some GCS egress on the way to the hub.
    Best-effort: falls back to ``[preferred]`` (or ``[""]`` — let Daisy choose)
    if the project's zones can't be enumerated."""
    pref = (preferred_zone or "").strip()
    try:
        from google.auth.transport.requests import AuthorizedSession
        session = AuthorizedSession(creds or _gcp_creds())
        resp = session.get(
            f"https://compute.googleapis.com/compute/v1/projects/{project_id}/zones",
            timeout=30,
        )
        resp.raise_for_status()
        up = sorted(z["name"] for z in resp.json().get("items", [])
                    if z.get("status") == "UP" and z.get("name"))
    except Exception as e:
        logger.warning("export: could not enumerate zones (%s); no zone fallback", e)
        return [pref] if pref else [""]

    def region_of(z: str) -> str:
        return z.rsplit("-", 1)[0]

    up_set = set(up)
    pref_region = region_of(pref) if pref else ""
    ordered: list = []

    def add(z: str) -> None:
        if z and z not in ordered:
            ordered.append(z)

    if pref in up_set:
        add(pref)
    for z in up:                       # same-region siblings (single-zone blip)
        if region_of(z) == pref_region:
            add(z)
    seen_regions = set()               # then one zone per other US region
    for z in up:
        r = region_of(z)
        if r == pref_region or not r.startswith("us-") or r in seen_regions:
            continue
        seen_regions.add(r)
        add(z)

    return ordered[:limit] or ([pref] if pref else [""])


def _export_custom_image_to_vhd_sync(
    project_id: str,
    image_name: str,
    dest_bucket: str,
    dest_object: str,
    network: str,
    subnet: str,
    timeout: int,
    progress_cb,
    zone: str = "",
    retry_delay: int = 20,
) -> dict:
    """Trigger the Daisy gce_vm_image_export workflow via Cloud Build.

    The container `gcr.io/compute-image-tools/gce_vm_image_export` accepts
    `-format=vpc` to produce a VHD blob written to gs://dest_bucket/dest_object.
    Args also include `-source_image` and `-destination_uri`.

    On a transient Compute-capacity failure (503 SERVICE_UNAVAILABLE /
    ZONE_RESOURCE_POOL_EXHAUSTED while creating the export worker VM) the export
    is retried in a *different* zone via the `-zone` flag — same-region siblings
    first, then other US regions if the whole region is out of capacity — a plain
    retry is useless because the exporter otherwise keeps picking the same
    exhausted zone (see _export_candidate_zones). Non-transient failures (quota,
    permissions, a bad source image) raise immediately. ``zone`` is the preferred
    first zone.

    Returns {gs_url, format, build_id}.
    """
    from google.cloud.devtools import cloudbuild_v1
    creds = _gcp_creds()
    client = cloudbuild_v1.CloudBuildClient(credentials=creds)

    dest_uri = f"gs://{dest_bucket}/{dest_object}"
    base_args = [
        "-timeout=" + f"{max(timeout - 600, 600)}s",
        "-source_image=" + image_name,
        "-client_id=api",
        "-format=vpc",
        "-destination_uri=" + dest_uri,
    ]
    if network:
        base_args.append("-network=" + network)
    if subnet:
        base_args.append("-subnet=" + subnet)

    zones = _export_candidate_zones(project_id, zone, creds)
    total = len(zones)
    last_err: Optional[GCPError] = None
    for attempt, z in enumerate(zones, start=1):
        args = list(base_args)
        if z:
            args.append("-zone=" + z)
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
            # Force step output to Cloud Logging (not GCS-only, which is the default
            # here and leaves nothing queryable). Without this the build's real error
            # — e.g. the Daisy ZONE_RESOURCE_POOL_EXHAUSTED — never reaches Cloud
            # Logging, so _fetch_cloud_build_log finds only audit entries and the job
            # falls back to the generic "Build failed". Requires the build's service
            # account to have roles/logging.logWriter (granted in setup-gcp.sh).
            options=cloudbuild_v1.BuildOptions(
                logging=cloudbuild_v1.BuildOptions.LoggingMode.CLOUD_LOGGING_ONLY,
            ),
        )

        if progress_cb:
            where = f" in {z}" if z else ""
            suffix = f" (attempt {attempt}/{total}{where})" if total > 1 else where
            progress_cb(f"Submitting Cloud Build export for {image_name} → {dest_uri}{suffix}")

        op = client.create_build(project_id=project_id, build=build)
        metadata = op.metadata
        build_id = metadata.build.id if metadata and metadata.build else ""
        build_create = getattr(metadata.build, "create_time", None) if (metadata and metadata.build) else None
        if progress_cb and build_id:
            progress_cb(f"Cloud Build {build_id} started — polling")

        # op.result() blocks until the build finishes and RAISES on failure with a
        # useless generic "Build failed; check build logs for details". Pull the
        # build's own log tail (the Daisy error — a ZONE_RESOURCE_POOL_EXHAUSTED,
        # a 503 creating the worker VM, a permission denial) so the operator sees
        # the real reason instead of having to `gcloud builds log`.
        try:
            result = op.result(timeout=timeout)
            status = cloudbuild_v1.Build.Status(result.status).name if result.status else "UNKNOWN"
            if status == "SUCCESS":
                if progress_cb:
                    progress_cb(f"Export complete: {dest_uri}")
                return {"gs_url": dest_uri, "format": "vhd", "build_id": build_id}
            detail = _export_build_error_detail(
                project_id, build_id, creds, build_create, result.status_detail or status)
            err = GCPError(f"Cloud Build {build_id} ended in status {status}:\n{detail}")
        except Exception as build_err:
            detail = _export_build_error_detail(
                project_id, build_id, creds, build_create, str(build_err))
            err = GCPError(f"Cloud Build {build_id or '(unknown)'} export failed:\n{detail}")

        last_err = err
        if attempt < total and _is_transient_export_error(str(err)):
            next_zone = zones[attempt]
            logger.warning("Cloud Build export: transient failure in zone %r (attempt %d/%d), "
                           "retrying in %r: %s", z or "(default)", attempt, total, next_zone,
                           detail.splitlines()[-1] if detail else "")
            if progress_cb:
                progress_cb(f"Export hit a capacity error in {z or 'the default zone'}; "
                            f"retrying in {next_zone} in {retry_delay}s…")
            time.sleep(retry_delay)
            continue
        raise err

    # Loop only exits here if the last attempt failed transiently with no zones left.
    raise last_err


async def export_custom_image_to_vhd(
    project_id: str,
    image_name: str,
    dest_bucket: str,
    dest_object: str,
    network: str = "",
    subnet: str = "",
    timeout: int = 7200,
    progress_cb=None,
    zone: str = "",
) -> dict:
    """Export a GCE custom image to a VHD on GCS via the Daisy workflow.

    Returns {gs_url, format, build_id}. progress_cb is an optional sync callable
    taking a single string for streaming status into a Job log. ``zone`` is the
    preferred zone for the export worker VM; on a capacity failure the export
    retries in sibling zones of the same region.
    """
    try:
        from google.cloud.devtools import cloudbuild_v1  # noqa: F401
    except ImportError:
        raise GCPError("google-cloud-build is not installed — run: pip install google-cloud-build")
    try:
        return await asyncio.to_thread(
            _export_custom_image_to_vhd_sync,
            project_id, image_name, dest_bucket, dest_object,
            network, subnet, timeout, progress_cb, zone,
        )
    except GCPError:
        raise
    except Exception as e:
        raise GCPError(f"Failed to export image {image_name} to VHD: {e}") from e


# ── Import GCS-staged tar.gz → custom image (cross-cloud promote target) ─────

# images.insert from a raw-disk tarball converts server-side and can take a
# while for a multi-GB disk; 40 min is generous but still bounds a genuinely
# stuck operation so it fails the promote instead of hanging forever.
_IMAGE_INSERT_TIMEOUT_S = 2400


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

    # compute.images.insert rejects the gs:// scheme for rawDisk.source ("not a
    # valid Google Cloud Storage object or Artifact Registry URL") — it requires
    # the https://storage.googleapis.com/<bucket>/<object> form. Normalise here so
    # the caller can pass either.
    raw_source = gcs_url
    if raw_source.startswith("gs://"):
        raw_source = "https://storage.googleapis.com/" + raw_source[len("gs://"):]

    image = compute_v1.Image(
        name=image_name,
        description=description,
        family=family or None,
        raw_disk=compute_v1.RawDisk(source=raw_source),
    )
    op = images_client.insert(project=project_id, image_resource=image)
    # Bounded wait: a stuck images.insert (unreadable/malformed rawDisk source)
    # must fail the promote, not hang the in-app background task forever.
    import concurrent.futures
    try:
        op.result(timeout=_IMAGE_INSERT_TIMEOUT_S)  # blocks until READY/FAILED
    except concurrent.futures.TimeoutError:
        raise GCPError(
            f"Custom image '{image_name}' did not finish creating within "
            f"{_IMAGE_INSERT_TIMEOUT_S // 60} min — the source object may be "
            f"unreadable or malformed ({raw_source[:120]})."
        )

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
        labels={"managed-by": "vm-dashboard", "purpose": "promote-runner"},
    )

    logger.info(
        "Cloud Run promote-runner: creating job %s in %s/%s",
        job_name, project_id, region,
    )
    # The job name is derived from the image id (stable across runs), so a run
    # that was cancelled or whose app process restarted before the finally-delete
    # ran can leave the job behind — making create_job 409 "already exists".
    # Best-effort delete any stale instance first so retries are idempotent.
    from google.api_core import exceptions as _gcp_exc
    try:
        jobs_client.delete_job(name=job_resource_name).result()
        logger.info("Cloud Run promote-runner: removed stale job %s before create", job_name)
    except _gcp_exc.NotFound:
        pass
    create_op = jobs_client.create_job(parent=parent, job_id=job_name, job=job)
    create_op.result()

    output = ""
    exit_code = 1
    execution_name = None

    try:
        run_op = jobs_client.run_job(name=job_resource_name)
        try:
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
        except Exception as run_err:
            # The run LRO raises when the execution fails to reach a clean state
            # (e.g. the task was OOM-killed). Keep exit_code=1 and fall through to
            # log retrieval so the container's own output is surfaced instead of a
            # bare Cloud Run "Task ... exit code 1". execution_name may be None here.
            logger.warning("Cloud Run promote-runner: execution failed: %s", run_err)

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
