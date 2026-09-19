"""Pydantic models for the OT (operational technology) demo endpoints."""
from typing import List, Optional
from pydantic import BaseModel, Field


class OTPresetInfo(BaseModel):
    key: str            # e.g. "modbus"
    label: str          # e.g. "Modbus TCP"
    port: int           # canonical TCP port, e.g. 502
    # True when the baked ot-sim image actually serves this endpoint. The cell
    # form offers only these; the standalone-tunnel form offers them all, since it
    # points at real gear.
    cell: bool = False
    # True for a fieldbus protocol, False for a platform endpoint on the same host
    # (the cell's KubeSolo API). Both are brokered identically; the forms group
    # them so "Kubernetes API" is not listed as something a PLC speaks.
    plc: bool = True


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
    BeyondTrust access layer (Web Jump → HMI, a protocol tunnel per brokered
    endpoint, and the Shell Jump / Password Safe onboarding the normal GCE deploy
    already does). The image runs its simulators on KubeSolo, so the cell is also a
    single-node Kubernetes host and its API is one of the endpoints on offer."""
    image_self_link: str
    image_name: str = ""
    instance_name: str
    # e2-medium, not e2-small: the cell runs KubeSolo, the PLC sims and FUXA, and a
    # 2 GB e2-small proved too tight in live use (validated on ot-cell-01). The
    # KubeSolo control plane idles at ~200 MB on top; installing the Entitle agent
    # into the cell as well wants an 8 GB shape, for the agent's own 1Gi request.
    machine_type: str = "e2-medium"
    zone: str = ""                    # defaults to configured gcp_zone
    subnetwork: str = ""              # defaults to the sandbox vm-subnet
    disk_size_gb: int = 20
    network_tags: List[str] = []
    workgroup: str
    # One PRA protocol tunnel is provisioned per protocol. `protocol` is the
    # pre-multi-protocol singular field, still accepted as a one-element alias so
    # older callers keep working; `protocols` wins when both are sent.
    protocols: List[str] = []
    protocol: str = "modbus"          # tunnel preset for the PLC port
    # Single-protocol overrides (the "custom" case) — ignored when the cell has
    # more than one protocol, since one port cannot describe several tunnels.
    plc_port: Optional[int] = Field(default=None, ge=1, le=65535)          # override the preset port
    tunnel_local_port: Optional[int] = Field(default=None, ge=1, le=65535)  # rep-side listen port
    hmi_port: int = Field(default=1881, ge=1, le=65535)
    register_in_passwordsafe: bool = True
    # Ticking this deploys the plant's own industrial-DMZ broker beside the cell and
    # runs the Entitle agent on it: an agent that manages access to plant resources
    # belongs in the plant. It needs the broker image (baked with OT_ROLE=broker) and
    # the Purdue zoning, which is what makes "one way out, and here it is" true.
    register_in_entitle: bool = False
    broker_image_self_link: str = ""
    broker_image_name: str = ""
    # 8 GB: the agent alone requests 1Gi, on top of KubeSolo's own ~200 MB.
    broker_machine_type: str = "e2-standard-2"
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
    # runs KubeSolo, the PLC sims and FUXA, and 2 GB proved too tight in live use.
    instance_type: str = "t3.medium"
    region: Optional[str] = None      # defaults to the configured aws_region
    subnet_id: str
    security_group_ids: List[str]
    workgroup: str
    protocols: List[str] = []         # one PRA tunnel each; see OTCellDeployRequest
    protocol: str = "modbus"          # singular alias, kept for older callers
    plc_port: Optional[int] = Field(default=None, ge=1, le=65535)
    tunnel_local_port: Optional[int] = Field(default=None, ge=1, le=65535)
    hmi_port: int = Field(default=1881, ge=1, le=65535)
    register_in_passwordsafe: bool = True
    register_in_entitle: bool = False
    # The plant's DMZ broker, from a second image baked with OT_ROLE=broker. Only read
    # when register_in_entitle is set: ticking Entitle on a cell MEANS the agent runs
    # in the plant, so there is no shared-agent path to fall back to.
    broker_ami_id: str = ""
    broker_ami_name: str = ""
    # t3.large (8 GB): the agent alone requests 1Gi and KubeSolo idles at ~200 MB, so
    # the cell's own t3.medium would leave the pod Pending with no other symptom.
    broker_instance_type: str = "t3.large"
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
    protocols: List[str] = []         # one PRA tunnel each; see OTCellDeployRequest
    protocol: str = "modbus"          # singular alias, kept for older callers
    plc_port: Optional[int] = Field(default=None, ge=1, le=65535)
    tunnel_local_port: Optional[int] = Field(default=None, ge=1, le=65535)
    hmi_port: int = Field(default=1881, ge=1, le=65535)
    register_in_passwordsafe: bool = True
    register_in_entitle: bool = False
    # The plant's DMZ broker — see OTCellDeployRequestAWS above.
    broker_image_id: str = ""
    broker_image_name: str = ""
    # Standard_D2s_v3 (8 GB), for the same reason t3.large is the AWS default.
    broker_vm_size: str = "Standard_D2s_v3"
    jump_group: Optional[str] = None
    jumpoint_name: Optional[str] = None


class OTCellTunnel(BaseModel):
    """One PRA protocol tunnel on a cell (an entry of the child job's
    ``ot_tunnels``)."""
    protocol: str = ""
    jump_id: str = ""
    local_port: int = 0
    remote_port: int = 0


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
    # Every protocol tunnel this cell has. The singular tunnel_* fields below
    # describe the FIRST one and are kept so older API consumers still read
    # something sensible.
    tunnels: List[OTCellTunnel] = []
    tunnel_jump_id: str = ""
    tunnel_protocol: str = ""
    tunnel_local_port: int = 0
    tunnel_remote_port: int = 0
    shell_jump_id: str = ""
    # The plant's own Entitle agent, when this cell has one: the DMZ broker's VM job
    # and instance, and whether the agent install has reported success. Empty on a
    # cell deployed without it (and on every AWS/Azure cell until those phases land).
    broker_job_id: str = ""
    broker_instance_name: str = ""
    # AWS deletes an instance by id, not by name, so the card needs the broker's --
    # GCP and Azure key on the name and leave this empty.
    broker_instance_id: str = ""
    broker_private_ip: str = ""
    agent_token_name: str = ""
    agent_installed: bool = False
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
