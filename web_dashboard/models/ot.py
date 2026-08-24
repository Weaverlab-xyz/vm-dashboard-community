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
    jump_group: Optional[str] = None             # else gcp_bt_jump_group_name / bt_jump_group_name
    jumpoint_name: Optional[str] = None          # else gcp_jumpoint_name / bt_jumpoint_name


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
    machine_type: str = "e2-small"
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


class OTCellDeployResponse(BaseModel):
    job_id: str          # the ot_cell_deploy parent (open /jobs/<id> for progress)
    vm_job_id: str       # the queued gce_deploy child — the cell's inventory record
    status: str
    message: str


class OTCellInfo(BaseModel):
    vm_job_id: str
    instance_name: str = ""
    zone: str = ""
    status: str = ""                  # VM job status (completed = cell live)
    private_ip: Optional[str] = None
    hmi_url: str = ""
    web_jump_id: str = ""
    tunnel_jump_id: str = ""
    tunnel_protocol: str = ""
    tunnel_local_port: int = 0
    tunnel_remote_port: int = 0
    shell_jump_id: str = ""
    workgroup: str = ""
    expires_at: Optional[str] = None
    wiring_complete: bool = False


class OTCellListResponse(BaseModel):
    cells: List[OTCellInfo]
