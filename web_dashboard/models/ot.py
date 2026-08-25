"""Pydantic models for the OT (operational technology) demo endpoints."""
from typing import List, Optional
from pydantic import BaseModel, Field


class OTPresetInfo(BaseModel):
    key: str            # e.g. "modbus"
    label: str          # e.g. "Modbus TCP"
    port: int           # canonical TCP port, e.g. 502


class OTPresetsResponse(BaseModel):
    presets: List[OTPresetInfo]


class OTTunnelRequest(BaseModel):
    """A standalone PRA protocol tunnel to any reachable OT endpoint."""
    name: str = Field(min_length=1, max_length=80)
    hostname: str = Field(min_length=1)          # host/IP the Jumpoint dials
    protocol: str = "modbus"                     # preset key, or "custom"
    remote_port: Optional[int] = Field(default=None, ge=1, le=65535)  # required for "custom"
    local_port: Optional[int] = Field(default=None, ge=1, le=65535)   # defaults to remote_port
    jump_group: Optional[str] = None             # else the cloud's *_bt_jump_group_name / bt_jump_group_name
    jumpoint_name: Optional[str] = None          # else the cloud's *_jumpoint_name / bt_jumpoint_name
    cloud: str = "gcp"                           # whose shared gateway the tunnel rides (gcp/aws/azure)


class OTTunnelInfo(BaseModel):
    slug: str
    name: str = ""
    hostname: str = ""
    protocol: str = ""
    local_port: int = 0
    remote_port: int = 0
    tunnel_jump_id: str = ""
    created_by: str = ""
    created_at: str = ""


class OTTunnelListResponse(BaseModel):
    tunnels: List[OTTunnelInfo]


class OTTunnelResponse(BaseModel):
    slug: str
    tunnel_jump_id: str = ""
    local_port: int
    remote_port: int
    message: str


class OTCellDeployRequest(BaseModel):
    """One-click OT demo cell: a VM from the Packer-baked ``ot-sim`` image plus the
    BeyondTrust access layer (Web Jump → HMI, protocol tunnel → PLC port, and the
    Shell Jump / Password Safe onboarding the normal GCE deploy already does)."""
    image_self_link: str
    image_name: str = ""
    instance_name: str
    # e2-medium, not e2-small: the cell runs Docker + the PLC sim + FUXA, and a
    # 2 GB e2-small proved too tight in live use (validated on ot-cell-01).
    machine_type: str = "e2-medium"
    zone: str = ""                    # defaults to configured gcp_zone
    subnetwork: str = ""              # defaults to the sandbox vm-subnet
    disk_size_gb: int = 20
    network_tags: List[str] = []
    workgroup: str
    protocol: str = "modbus"          # tunnel preset for the PLC port
    plc_port: Optional[int] = Field(default=None, ge=1, le=65535)          # override the preset port
    tunnel_local_port: Optional[int] = Field(default=None, ge=1, le=65535)  # rep-side listen port
    hmi_port: int = Field(default=1881, ge=1, le=65535)
    register_in_passwordsafe: bool = True
    register_in_entitle: bool = False
    jump_group: Optional[str] = None
    jumpoint_name: Optional[str] = None


class OTCellDeployRequestAWS(BaseModel):
    """AWS flavour of the OT demo cell: an EC2 instance from the ``ot-sim`` AMI.
    Mirrors ``DeployRequest`` (models/aws) where the cell has a choice to make and
    pins the rest — no public-IP knob exists on the EC2 path (the subnet decides),
    so the deploy form must point at the private sandbox subnet."""
    ami_id: str
    ami_name: str = ""
    instance_name: str
    # t3.medium (4 GB) — the same budget as the GCP default e2-medium: the cell
    # runs Docker + the PLC sim + FUXA, and 2 GB proved too tight in live use.
    instance_type: str = "t3.medium"
    region: Optional[str] = None      # defaults to the configured aws_region
    subnet_id: str
    security_group_ids: List[str]
    workgroup: str
    protocol: str = "modbus"
    plc_port: Optional[int] = Field(default=None, ge=1, le=65535)
    tunnel_local_port: Optional[int] = Field(default=None, ge=1, le=65535)
    hmi_port: int = Field(default=1881, ge=1, le=65535)
    register_in_passwordsafe: bool = True
    register_in_entitle: bool = False
    jump_group: Optional[str] = None
    jumpoint_name: Optional[str] = None


class OTCellDeployRequestAzure(BaseModel):
    """Azure flavour of the OT demo cell: a VM from the ``ot-sim`` gallery image.
    The SSH public key is resolved server-side from the configured Key Vault (the
    same key every Azure deploy injects), and the VM never gets a public IP."""
    image_id: str
    image_name: str = ""
    vm_name: str
    # Standard_B2s (4 GB) — the same budget as the GCP default e2-medium.
    vm_size: str = "Standard_B2s"
    location: str = ""                # defaults to the configured azure_location
    subnet_id: str
    nsg_ids: List[str] = []
    workgroup: str
    protocol: str = "modbus"
    plc_port: Optional[int] = Field(default=None, ge=1, le=65535)
    tunnel_local_port: Optional[int] = Field(default=None, ge=1, le=65535)
    hmi_port: int = Field(default=1881, ge=1, le=65535)
    register_in_passwordsafe: bool = True
    register_in_entitle: bool = False
    jump_group: Optional[str] = None
    jumpoint_name: Optional[str] = None


class OTCellDeployResponse(BaseModel):
    job_id: str          # the ot_cell_deploy parent (open /jobs/<id> for progress)
    vm_job_id: str       # the queued VM-deploy child — the cell's inventory record
    status: str
    message: str


class OTCellInfo(BaseModel):
    vm_job_id: str
    cloud: str = "gcp"
    instance_name: str = ""
    # GCP: the zone; AWS: the region; Azure: the location — whatever the cloud's
    # destroy endpoint needs alongside the name/id.
    zone: str = ""
    instance_id: str = ""             # AWS only — DELETE /api/aws/instances/{id}
    status: str = ""                  # VM job status (completed = cell live)
    private_ip: Optional[str] = None
    hmi_url: str = ""
    web_jump_id: str = ""
    tunnel_jump_id: str = ""
    tunnel_protocol: str = ""
    tunnel_local_port: int = 0
    tunnel_remote_port: int = 0
    shell_jump_id: str = ""
    # PRA checkout of the cell's admin credential: the Vault account PRA users
    # check out / inject, kept current by a Password Safe SyncedAccounts link.
    vault_account_id: str = ""
    vault_account_name: str = ""
    ps_checkout_synced: bool = False
    workgroup: str = ""
    expires_at: Optional[str] = None
    wiring_complete: bool = False


class OTCellListResponse(BaseModel):
    cells: List[OTCellInfo]
