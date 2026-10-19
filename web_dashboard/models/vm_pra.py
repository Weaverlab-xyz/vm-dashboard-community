"""Request shape of the post-deploy "PRA access" action on a cloud VM row.

One model for the four clouds so they cannot drift. ``instance`` is whatever that cloud's
row is keyed by: an EC2 instance id, an Azure VM name, a GCE instance name, an OCI OCID.
See ``services/vm_pra_service``.
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field


class VmPraRequest(BaseModel):
    instance: str = Field(..., min_length=1, max_length=255)
    action: Literal["register", "remove"] = "register"
    # Blank = the configured default, resolved by vm_pra_service.resolve_target.
    jump_group: Optional[str] = Field(None, max_length=255)
    jumpoint_name: Optional[str] = Field(None, max_length=255)
    pra_credential_ref: Optional[str] = Field(None, max_length=1024)
