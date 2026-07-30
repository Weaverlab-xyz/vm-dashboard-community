"""Pydantic models for the Packer image-builder endpoints."""
import re
from typing import Optional
from pydantic import BaseModel, field_validator, model_validator

# Shell environment-variable names: a letter or underscore, then letters/digits/
# underscores. Enforced so a user-supplied name can't break out of the HCL
# environment_vars array or the PKR_VAR_ mapping.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# OCI placement identifiers. These are inlined into the generated HCL (they are
# not secrets, so they don't go through the sensitive-variable path), so each is
# pinned to the narrowest charset the real value uses. Belt and braces on top of
# packer_service._hcl_escape.
_OCID_RE = re.compile(r"^ocid1\.[a-z0-9]+\.[A-Za-z0-9\.\-_]+$")
_OCI_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Availability domains carry a tenancy-specific prefix and a colon: "Uocm:PHX-AD-1".
_OCI_AD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:\-]*$")
_POSIX_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")


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
    bt_epml_source: Optional[str] = None       # "beyondtrust" (default) | "storage" — where BT_EPML_URL points


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
    bt_epml_source: Optional[str] = None       # "beyondtrust" (default) | "storage" — where BT_EPML_URL points


class GCPPackerBuildRequest(BaseModel):
    image_name: str
    source_image: str
    machine_type: str = "e2-medium"
    ssh_username: str = "packer"
    # Boot-disk knobs for the transient build VM. disk_type defaults to pd-ssd —
    # the googlecompute builder's own default (pd-standard, HDD) has low IOPS that
    # drags out provisioning. disk_size_gb is optional (None → source-image default;
    # a size smaller than the image would fail the build). Both are inlined into the
    # generated HCL, so disk_type is allow-listed and disk_size_gb is range-checked.
    disk_type: str = "pd-ssd"
    disk_size_gb: Optional[int] = None
    provisioner_script: str = ""
    archive_template: bool = False
    # Generic provisioner environment variables (literals + secret-manager refs).
    provisioner_env_vars: list[ProvisionerEnvVar] = []
    # BeyondTrust provisioner knobs — passed to the bt-ready provisioner as env vars.
    bt_admin_user: Optional[str] = None        # → BT_ADMIN_USER (Password-Safe-managed bootstrap account)
    bt_epml: Optional[str] = None              # "deb" | "rpm" — install EPM-L package of this family (else skip)
    bt_epml_source: Optional[str] = None       # "beyondtrust" (default) | "storage" — where BT_EPML_URL points

    @field_validator("disk_type")
    @classmethod
    def _valid_disk_type(cls, v: str) -> str:
        v = (v or "").strip() or "pd-ssd"
        allowed = {"pd-standard", "pd-balanced", "pd-ssd", "pd-extreme"}
        if v not in allowed:
            raise ValueError(f"invalid disk_type {v!r} — must be one of {sorted(allowed)}")
        return v

    @field_validator("disk_size_gb")
    @classmethod
    def _valid_disk_size(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        if not (10 <= v <= 2000):
            raise ValueError("disk_size_gb must be between 10 and 2000")
        return v


class OCIPackerBuildRequest(BaseModel):
    """One OCI custom-image build (oracle-oci builder).

    Unlike the other three clouds, the builder needs the *whole* placement told to
    it — availability domain, compartment (resolved server-side), subnet and shape
    are all required options, not defaults it can infer. Every field below is
    inlined into the generated HCL, so each is validated against a conservative
    charset here as well as HCL-escaped at generation time."""
    image_name: str
    base_image_ocid: str
    shape: str = "VM.Standard.E2.1.Micro"   # the Always-Free AMD micro
    availability_domain: str
    subnet_ocid: str = ""                   # blank → oci_default_subnet_ocid
    ssh_username: str = "opc"               # Oracle Linux default; "ubuntu" for Ubuntu images
    # Flex shapes (VM.Standard.A1.Flex, *.E4.Flex …) carry no fixed size, so the
    # builder requires a shape_config. Left None for fixed shapes, where emitting
    # one is an error.
    ocpus: Optional[float] = None
    memory_gb: Optional[float] = None
    # OCI's boot-volume floor is 50 GB, which is also the free-tier default.
    boot_volume_gb: Optional[int] = None
    # Free-tier guardrail: oci_freetier.evaluate() is advisory, so a selection
    # outside the Always-Free envelope needs the operator to acknowledge it —
    # the same warn-and-confirm gate the deploy form uses.
    acknowledge_charges: bool = False
    provisioner_script: str = ""
    archive_template: bool = False
    # Generic provisioner environment variables (literals + secret-manager refs).
    provisioner_env_vars: list[ProvisionerEnvVar] = []
    # BeyondTrust provisioner knobs — passed to the bt-ready provisioner as env vars.
    bt_admin_user: Optional[str] = None        # → BT_ADMIN_USER (Password-Safe-managed bootstrap account)
    bt_epml: Optional[str] = None              # "deb" | "rpm" — install EPM-L package of this family (else skip)
    bt_epml_source: Optional[str] = None       # "beyondtrust" (default) | "storage" — where BT_EPML_URL points

    @field_validator("base_image_ocid")
    @classmethod
    def _valid_image_ocid(cls, v: str) -> str:
        v = (v or "").strip()
        if not _OCID_RE.match(v):
            raise ValueError(f"invalid base_image_ocid {v!r} — expected an ocid1.image… OCID")
        return v

    @field_validator("subnet_ocid")
    @classmethod
    def _valid_subnet_ocid(cls, v: str) -> str:
        v = (v or "").strip()
        # Blank is allowed — the runner falls back to oci_default_subnet_ocid.
        if v and not _OCID_RE.match(v):
            raise ValueError(f"invalid subnet_ocid {v!r} — expected an ocid1.subnet… OCID")
        return v

    @field_validator("shape")
    @classmethod
    def _valid_shape(cls, v: str) -> str:
        v = (v or "").strip()
        if not _OCI_SHAPE_RE.match(v):
            raise ValueError(f"invalid shape {v!r} — expected e.g. VM.Standard.E2.1.Micro")
        return v

    @field_validator("availability_domain")
    @classmethod
    def _valid_ad(cls, v: str) -> str:
        # OCI availability domains look like "Uocm:PHX-AD-1" — the colon is part
        # of the tenancy-specific prefix, so it has to be in the allowed set.
        v = (v or "").strip()
        if not _OCI_AD_RE.match(v):
            raise ValueError(f"invalid availability_domain {v!r} — expected e.g. Uocm:PHX-AD-1")
        return v

    @field_validator("ssh_username")
    @classmethod
    def _valid_ssh_username(cls, v: str) -> str:
        v = (v or "").strip() or "opc"
        if not _POSIX_USER_RE.match(v):
            raise ValueError(f"invalid ssh_username {v!r} — must match [a-z_][a-z0-9_-]*")
        return v

    @field_validator("ocpus", "memory_gb")
    @classmethod
    def _valid_shape_config(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        if not (0 < float(v) <= 1024):
            raise ValueError("ocpus/memory_gb must be between 0 and 1024")
        return float(v)

    @field_validator("boot_volume_gb")
    @classmethod
    def _valid_boot_volume(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        if not (50 <= v <= 32768):
            raise ValueError("boot_volume_gb must be between 50 (OCI's minimum) and 32768")
        return v

    @model_validator(mode="after")
    def _flex_shape_sizing(self):
        """OCI requires a shape_config on a flexible shape and rejects one on a
        fixed shape. Normalising the pair here — rather than in the generator —
        is what lets generate_oci_template key the shape_config block on `ocpus`
        alone, and turns a mismatch into a 422 at request time instead of an
        opaque launch failure ten minutes into the build."""
        if self.shape.endswith(".Flex") or ".Flex." in self.shape:
            if not self.ocpus:
                raise ValueError(
                    f"{self.shape} is a flexible shape — ocpus is required "
                    "(memory_gb defaults to 8 GB per OCPU)")
            if not self.memory_gb:
                self.memory_gb = float(self.ocpus) * 8
        else:
            self.ocpus = None
            self.memory_gb = None
        return self


class PackerBuildResponse(BaseModel):
    job_id: str
    status: str
    message: str
