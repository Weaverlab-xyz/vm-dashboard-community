"""
Pydantic models for AWS/Terraform API endpoints.
"""
from typing import List, Optional
from pydantic import BaseModel, Field


class AMIInfo(BaseModel):
    ami_id: str
    name: str
    description: str = ""
    state: str = ""
    creation_date: str = ""
    architecture: str = ""
    virtualization_type: str = ""
    platform: str = "linux"
    size_gb: int = 0
    ena_support: bool = False
    tags: dict = {}


class AMIListResponse(BaseModel):
    amis: List[AMIInfo]
    count: int
    cached_at: Optional[str] = None


class EC2InstanceInfo(BaseModel):
    instance_id: str
    name: str
    instance_type: str = ""
    state: str = ""
    public_ip: Optional[str] = None
    private_ip: Optional[str] = None
    ami_id: str = ""
    launch_time: str = ""
    availability_zone: str = ""
    key_name: Optional[str] = None
    workgroup: Optional[str] = None  # from Job.workgroup; None = unassigned
    # Dashboard-specific fields (from DB)
    job_id: Optional[str] = None
    deployed_by: Optional[str] = None


class EC2InstanceListResponse(BaseModel):
    instances: List[EC2InstanceInfo]
    count: int
    cached_at: Optional[str] = None


class NetworkOptions(BaseModel):
    subnets: List[dict]
    security_groups: List[dict]
    instance_types: List[str]
    cached_at: Optional[str] = None


class SSHKeySecretDetail(BaseModel):
    name: str
    public_key: str
    description: str = ""


class DeployRequest(BaseModel):
    ami_id: str = Field(..., description="AMI ID to deploy")
    instance_name: str = Field(..., description="Name tag for the instance")
    instance_type: str = Field(default="t3.medium", description="EC2 instance type")
    subnet_id: str = Field(..., description="VPC subnet ID")
    security_group_ids: List[str] = Field(..., description="Security group IDs")
    workgroup: str = Field(..., description="Workgroup the instance belongs to (written as Workgroup tag)")


class DeployResponse(BaseModel):
    job_id: str
    status: str
    message: str


class DestroyResponse(BaseModel):
    job_id: str
    status: str
    message: str


class CommunityAMIInfo(BaseModel):
    ami_id: str
    name: str
    description: str = ""
    os_type: str = ""        # amazon-linux | ubuntu | debian
    architecture: str = ""
    creation_date: str = ""
    free_tier_note: str = ""
    size_gb: int = 0


class CommunityAMIListResponse(BaseModel):
    amis: List[CommunityAMIInfo]
    count: int


class CopyAMIRequest(BaseModel):
    source_ami_id: str = Field(..., description="Public AMI ID to copy")
    name: str = Field(..., description="Name for the private copy")
    description: str = Field(default="", description="Optional description")


class CopyAMIResponse(BaseModel):
    job_id: str
    status: str
    message: str


class BulkDeployItem(BaseModel):
    ami_id: str = Field(..., description="AMI ID to deploy")
    instance_name: str = Field(..., description="Name tag for this specific instance")


class BulkDeployRequest(BaseModel):
    items: List[BulkDeployItem] = Field(..., description="List of AMIs to deploy with per-instance names")
    instance_type: str = Field(default="t3.medium", description="EC2 instance type (shared)")
    subnet_id: str = Field(..., description="VPC subnet ID (shared)")
    security_group_ids: List[str] = Field(..., description="Security group IDs (shared)")
    workgroup: str = Field(..., description="Workgroup all deployed instances belong to (written as Workgroup tag)")


class BulkDeployJobResult(BaseModel):
    ami_id: str
    instance_name: str
    job_id: str
    status: str


class BulkDeployResponse(BaseModel):
    jobs: List[BulkDeployJobResult]
    count: int


class CreateImageRequest(BaseModel):
    name: str = Field(..., description="Name for the new AMI")
    description: str = Field(default="", description="Optional description")
    no_reboot: bool = Field(default=True, description="If True, instance is not rebooted before imaging")


class CreateImageResponse(BaseModel):
    job_id: str
    status: str
    message: str
