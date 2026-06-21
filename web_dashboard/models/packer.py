"""Pydantic models for the Packer image-builder endpoints."""
import re
from typing import Optional
from pydantic import BaseModel, field_validator

# Shell environment-variable names: a letter or underscore, then letters/digits/
# underscores. Enforced so a user-supplied name can't break out of the HCL
# environment_vars array or the PKR_VAR_ mapping.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProvisionerEnvVar(BaseModel):
    """One environment variable handed to the shell provisioner.

    When ``is_secret_ref`` is true, ``value`` is a configured-secret-manager
    reference (e.g. ``aws_sm://dashboard/foo``, ``azure_kv://foo``,
    ``gcp_sm://foo``, ``bt_safe://...``) resolved at build-launch via
    config_service; the resolved value is injected through a Packer *sensitive*
    variable so it never lands in the generated/archived template or the logs.
    Otherwise ``value`` is a literal inlined into the template."""
    name: str
    value: str = ""
    is_secret_ref: bool = False

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not _ENV_NAME_RE.match(v):
            raise ValueError(
                f"invalid environment variable name {v!r} — must match [A-Za-z_][A-Za-z0-9_]*"
            )
        return v


class AWSPackerBuildRequest(BaseModel):
    image_name: str
    source_ami: str
    instance_type: str = "t3.micro"
    ssh_username: str = "ec2-user"
    provisioner_script: str = ""
    archive_template: bool = False
    # Generic provisioner environment variables (literals + secret-manager refs).
    provisioner_env_vars: list[ProvisionerEnvVar] = []
    # BeyondTrust provisioner knobs — passed to the bt-ready provisioner as env vars.
    bt_admin_user: Optional[str] = None        # → BT_ADMIN_USER (Password-Safe-managed bootstrap account)
    bt_epml: Optional[str] = None              # "deb" | "rpm" — install EPM-L package of this family (else skip)


class AzurePackerBuildRequest(BaseModel):
    image_name: str
    image_publisher: str = "Canonical"
    image_offer: str = "0001-com-ubuntu-server-jammy"
    image_sku: str = "22_04-lts"
    vm_size: str = "Standard_B2s"
    os_type: str = "Linux"  # "Linux" | "Windows" — picks the template generator
    # Windows client (e.g. Win 11) needs Trusted Launch, which Azure can't output
    # as a managed image — so trusted_launch=True switches the build to a Compute
    # Gallery image version (secure boot + vTPM + Windows_Client license).
    trusted_launch: bool = False
    # Gallery destination for trusted_launch builds; auto-derived from config /
    # image_name when blank (azure_shared_image_gallery / azure_gallery_resource_group).
    gallery_name: Optional[str] = None
    gallery_resource_group: Optional[str] = None
    gallery_image_name: Optional[str] = None
    gallery_image_version: Optional[str] = None
    provisioner_script: str = ""
    archive_template: bool = False
    # Generic provisioner environment variables (literals + secret-manager refs).
    provisioner_env_vars: list[ProvisionerEnvVar] = []
    # BeyondTrust provisioner knobs — passed to the bt-ready provisioner as env vars.
    bt_admin_user: Optional[str] = None        # → BT_ADMIN_USER (Password-Safe-managed bootstrap account)
    bt_epml: Optional[str] = None              # "deb" | "rpm" — install EPM-L package of this family (else skip)


class GCPPackerBuildRequest(BaseModel):
    image_name: str
    source_image: str
    machine_type: str = "e2-medium"
    ssh_username: str = "packer"
    provisioner_script: str = ""
    archive_template: bool = False
    # Generic provisioner environment variables (literals + secret-manager refs).
    provisioner_env_vars: list[ProvisionerEnvVar] = []
    # BeyondTrust provisioner knobs — passed to the bt-ready provisioner as env vars.
    bt_admin_user: Optional[str] = None        # → BT_ADMIN_USER (Password-Safe-managed bootstrap account)
    bt_epml: Optional[str] = None              # "deb" | "rpm" — install EPM-L package of this family (else skip)


class PackerBuildResponse(BaseModel):
    job_id: str
    status: str
    message: str
