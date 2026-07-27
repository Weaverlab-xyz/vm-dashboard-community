"""Pydantic models for OCI (Oracle Cloud Infrastructure) API endpoints."""
from typing import List, Optional
from pydantic import BaseModel, Field

from ..services.vm_naming import MAX_DEPLOY_COUNT


class OCIImageInfo(BaseModel):
    ocid: str
    display_name: str
    operating_system: str = ""
    operating_system_version: str = ""
    lifecycle_state: str = "AVAILABLE"
    time_created: str = ""
    size_gb: int = 0
    source: str = "platform"   # "platform" | "custom"


class OCIInstanceInfo(BaseModel):
    ocid: str
    display_name: str
    shape: str = ""
    ocpus: Optional[float] = None
    memory_gb: Optional[float] = None
    lifecycle_state: str = ""   # PROVISIONING | RUNNING | STOPPED | TERMINATED …
    availability_domain: str = ""
    public_ip: Optional[str] = None
    private_ip: Optional[str] = None
    time_created: str = ""
    workgroup: Optional[str] = None   # from the `workgroup` freeform tag
    job_id: Optional[str] = None
    deployed_by: Optional[str] = None


class OCIShapeInfo(BaseModel):
    shape: str
    ocpus: Optional[float] = None
    memory_gb: Optional[float] = None
    is_flexible: bool = False
    free_tier: bool = False       # one of the Always-Free shapes


class OCISubnetInfo(BaseModel):
    ocid: str
    display_name: str
    cidr_block: str = ""
    vcn_ocid: str = ""
    prohibit_public_ip: bool = False


class OCINetworkOptions(BaseModel):
    availability_domains: List[str] = []
    shapes: List[OCIShapeInfo] = []
    subnets: List[OCISubnetInfo] = []
    region: str = ""
    compartment_ocid: str = ""
    ssh_key_configured: bool = False
    free_tier: dict = {}          # services.oci_freetier.free_tier_catalog()
    cached_at: Optional[str] = None


class OCIDeployRequest(BaseModel):
    image_ocid: str
    image_name: str = ""              # display/tracking only
    instance_name: str
    shape: str = "VM.Standard.E2.1.Micro"
    ocpus: Optional[float] = None     # flex shapes only (A1.Flex)
    memory_gb: Optional[float] = None # flex shapes only
    availability_domain: str = ""     # blank → first AD in the compartment
    subnet_ocid: str = ""             # blank → configured oci_default_subnet_ocid
    assign_public_ip: bool = False
    ssh_username: str = "opc"         # Oracle Linux default login user
    boot_volume_gb: int = 50
    workgroup: str                    # written as the `workgroup` freeform tag
    # Free-tier warn-and-confirm gate. When the selection is outside the
    # Always-Free envelope the API rejects the deploy unless this is true.
    acknowledge_charges: bool = False
    register_in_entitle: bool = False
    register_in_passwordsafe: bool = False
    ssh_key_secret_override: Optional[str] = None   # OCI Vault secret (OCID/name) with a public_key
    # PRA per-launch overrides (fall back to oci_bt_* / bt_* config).
    jump_group: Optional[str] = None
    jumpoint_name: Optional[str] = None
    count: int = Field(
        default=1, ge=1, le=MAX_DEPLOY_COUNT,
        description="Number of identical instances to launch. 1 is a plain single "
                    "deploy; >1 fans out into a batch and auto-numbers the names. The "
                    "free-tier gate is evaluated against the whole batch, so a count "
                    "that would exceed the Always-Free envelope needs acknowledge_charges.")


class OCIDeployResponse(BaseModel):
    job_id: str          # for a batch (count > 1) this is the PARENT job
    status: str
    message: str
    # Populated (with status="warning") when the selection exceeds the free tier
    # and acknowledge_charges was not set — the form surfaces these + the checkbox.
    free_tier_warnings: List[str] = []
    # Batch fields, defaulted so a count-1 response is unchanged for existing callers.
    count: int = 1
    batch_id: Optional[str] = None
    job_ids: List[str] = []   # child job ids, in name order
    names: List[str] = []     # the expanded names actually used, post-truncation


class OCISSHKeyDetail(BaseModel):
    secret_name: str
    public_key_preview: str


class OCIImageListResponse(BaseModel):
    images: List[OCIImageInfo]
    compartment_ocid: str = ""


class OCIInstanceListResponse(BaseModel):
    instances: List[OCIInstanceInfo]
    compartment_ocid: str = ""
    region: str = ""
