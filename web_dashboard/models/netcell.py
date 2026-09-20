"""Pydantic models for the network demo cell (a VyOS router/firewall).

GCP only, deliberately. The OT cell reached AWS and Azure in later phases once its
shape had settled on one cloud, and this feature has a sharper reason to do the same:
what a VyOS guest does with each cloud's key injection is the least certain thing
about it, and answering that once is cheaper than answering it three times wrong.
"""
from typing import List, Optional
from pydantic import BaseModel, Field


class NetCellDeployRequest(BaseModel):
    """One network demo cell: a VM from the Packer-baked ``vyos-cell`` image, deployed
    down the ordinary ``gce_deploy`` path.

    There is no parent orchestration job and no wiring step, which is the whole
    difference from the OT cell. That cell needs a parent because it provisions a Web
    Jump and a protocol tunnel per endpoint once the VM is up. A firewall needs SSH and
    nothing else, and the plain deploy path already provisions the Shell Jump, the
    Password Safe onboarding, the shared-gateway reference, the expiry stamp and the
    inventory row. Adding a parent that wires nothing would be a second lifecycle to
    keep correct in exchange for nothing -- so the cell is a normal VM deploy carrying
    a marker, a forced onboarding method and its own preflight guards.

    The consequence worth knowing: **Destroy and the expiry reaper already work**,
    because the row they act on is an ordinary ``gce_deploy`` row.
    """
    image_self_link: str
    image_name: str = ""
    instance_name: str
    # e2-small, not the e2-medium the OT cell needs: VyOS routes and filters, it does
    # not run a Kubernetes control plane and four simulators.
    machine_type: str = "e2-small"
    zone: str = ""                    # defaults to configured gcp_zone
    subnetwork: str = ""              # defaults to the sandbox vm-subnet
    disk_size_gb: int = 20
    network_tags: List[str] = []
    workgroup: str
    # What the image was baked with, asserted by the operator rather than read off the
    # image -- the same arrangement, for the same reason, as OTCellDeployRequest.runtime.
    # VyOS reorganised the firewall tree in 1.4 (`firewall ipv4 name` plus a `forward
    # filter` hook, where 1.3 had `firewall name`), so the commands an SE is about to
    # type in front of an audience differ by release train. The dashboard cannot read
    # the train off an image, and getting it wrong looks like a typo rather than a
    # version mismatch.
    vyos_release: str = "1.4"
    # Name of the baseline ruleset the bake attached, so the doc and the deploy agree on
    # where the demo's DROP rule lands. Overridable because the bake's VYOS_RULESET is.
    ruleset: str = "BLOCKLIST"
    # Default ON, unlike a plain deploy. A cell whose credential is not vaulted is a
    # router with a password somebody has to be told -- which is the thing being
    # demonstrated against.
    register_in_passwordsafe: bool = True
    jump_group: Optional[str] = None
    jumpoint_name: Optional[str] = None


class NetCellDeployResponse(BaseModel):
    job_id: str
    status: str
    message: str
    # What the guards decided, surfaced so the form can show it rather than leaving the
    # SE to discover the platform caveat mid-demo.
    passwordsafe_method: str = ""
    notes: List[str] = []


class NetCellInfo(BaseModel):
    job_id: str
    instance_name: str = ""
    zone: str = ""
    region: str = ""
    machine_type: str = ""
    status: str = ""
    created_by: str = ""
    created_at: str = ""
    workgroup: str = ""
    vyos_release: str = ""
    ruleset: str = ""
    private_ip: str = ""
    shell_jump_id: str = ""
    ps_managed_system_id: str = ""


class NetCellListResponse(BaseModel):
    cells: List[NetCellInfo]
