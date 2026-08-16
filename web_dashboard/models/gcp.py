"""Pydantic models for GCP (Google Cloud Platform) API endpoints."""
from typing import List, Optional
from pydantic import BaseModel, Field

from ..services.vm_naming import MAX_DEPLOY_COUNT


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
    region: str = ""         # derived from zone (us-central1-a → us-central1)
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
    # PRA per-deploy overrides — the configured defaults are the fallback when blank.
    jump_group: Optional[str] = None             # PRA Jump Group name override (else gcp_bt_jump_group_name / bt_jump_group_name)
    jumpoint_name: Optional[str] = None          # PRA Jumpoint name override (else gcp_jumpoint_name / bt_jumpoint_name)
    # A secrets-backend reference (e.g. gcp_sm://…) for the GCE Jumpoint deploy key.
    docker_deploy_key_ref: Optional[str] = None  # else gcp_cloud_run_docker_deploy_key
    count: int = Field(
        default=1, ge=1, le=MAX_DEPLOY_COUNT,
        description="Number of identical instances to launch. 1 is a plain single "
                    "deploy; >1 fans out into a batch that shares one Jumpoint and "
                    "auto-numbers the names (web -> web-01, web-02, …).")


class GCPBulkDeployItem(BaseModel):
    """One VM in a multi-select bulk request: its own image, its own name.

    This is the other axis from ``GCPDeployRequest.count``. Count deploys N copies of
    ONE image; bulk deploys one VM per SELECTED image. Everything else on the request
    is shared, exactly as on the AWS and Azure bulk routes."""
    image_self_link: str
    image_name: str = ""        # display/tracking only
    instance_name: str


class GCPBulkDeployRequest(BaseModel):
    items: List[GCPBulkDeployItem]
    machine_type: str = "e2-medium"
    zone: str = ""
    subnetwork: str = ""
    create_external_ip: bool = False
    ssh_username: str = "gcp-user"
    disk_size_gb: int = 20
    network_tags: List[str] = []
    workgroup: str
    register_in_entitle: bool = False
    register_in_passwordsafe: bool = False
    ssh_key_secret_override: Optional[str] = None
    jump_group: Optional[str] = None
    jumpoint_name: Optional[str] = None
    docker_deploy_key_ref: Optional[str] = None


class GCPDeployResponse(BaseModel):
    job_id: str          # for a batch (count > 1) this is the PARENT job
    status: str
    message: str
    # Batch fields, defaulted so a count-1 response is unchanged for existing callers.
    count: int = 1
    batch_id: Optional[str] = None
    job_ids: List[str] = []   # child job ids, in name order
    names: List[str] = []     # the expanded names actually used, post-truncation


class GCPBulkDeployJobResult(BaseModel):
    image_self_link: str
    instance_name: str
    job_id: str
    status: str


class GCPBulkDeployResponse(BaseModel):
    jobs: List[GCPBulkDeployJobResult]
    count: int
    batch_id: Optional[str] = None


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
