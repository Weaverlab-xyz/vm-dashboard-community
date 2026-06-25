"""Pydantic models for GCP (Google Cloud Platform) API endpoints."""
from typing import List, Optional
from pydantic import BaseModel


class GCPImageInfo(BaseModel):
    self_link: str
    name: str
    description: str = ""
    status: str = "READY"
    creation_date: str = ""
    disk_size_gb: int = 0
    source: str = "custom"   # "custom" | "public"
    family: str = ""
    os_label: str = ""       # Human-readable OS name (public images)
    os_key: str = ""         # Filter key: debian / ubuntu / rhel / rocky / centos / cos


class GCPInstanceInfo(BaseModel):
    instance_name: str
    zone: str
    machine_type: str = ""
    status: str = ""         # RUNNING | TERMINATED | STAGING | STOPPING | SUSPENDED
    public_ip: Optional[str] = None
    private_ip: Optional[str] = None
    self_link: str = ""
    creation_timestamp: str = ""
    workgroup: Optional[str] = None  # from `workgroup` GCE label; None = unassigned
    job_id: Optional[str] = None
    deployed_by: Optional[str] = None


class GCPSubnetInfo(BaseModel):
    name: str
    self_link: str
    ip_cidr_range: str = ""
    network: str = "default"


class GCPNetworkOptions(BaseModel):
    zones: List[str] = []
    machine_types: List[str] = []
    subnetworks: List[GCPSubnetInfo] = []
    region: str = ""
    ssh_key_configured: bool = False
    cached_at: Optional[str] = None


class GCPDeployRequest(BaseModel):
    image_self_link: str
    image_name: str = ""        # For display/tracking only
    instance_name: str
    machine_type: str = "e2-medium"
    zone: str = ""              # Defaults to configured gcp_zone
    subnetwork: str = ""        # Full subnetwork self_link or empty for default
    create_external_ip: bool = False
    ssh_username: str = "gcp-user"
    disk_size_gb: int = 20
    network_tags: List[str] = []
    workgroup: str              # written as `workgroup` GCE label
    register_in_entitle: bool = False  # opt in to registering this VM as an Entitle SSH integration
    register_in_passwordsafe: bool = False  # opt in to onboarding this VM into Password Safe (managed system + account)
    ssh_key_secret_override: Optional[str] = None  # optional Secret Manager secret to use for the SSH key (must be JSON with a public_key)
    # Per-deploy override — config default is the fallback. A secrets-backend
    # reference (e.g. gcp_sm://…) for the GCE Jumpoint deploy key. (GCP uses a
    # per-VM Jumpoint container, not a shell-jump, so there's no jump_group here.)
    docker_deploy_key_ref: Optional[str] = None  # else gcp_cloud_run_docker_deploy_key


class GCPDeployResponse(BaseModel):
    job_id: str
    status: str
    message: str


class GCPCreateImageRequest(BaseModel):
    image_name: str
    description: str = ""


class GCPSSHKeyDetail(BaseModel):
    secret_name: str
    public_key_preview: str  # First 60 chars of the public key


class GCPImageListResponse(BaseModel):
    images: List[GCPImageInfo]
    project_id: str = ""


class GCPInstanceListResponse(BaseModel):
    instances: List[GCPInstanceInfo]
    project_id: str = ""
    zone: str = ""
