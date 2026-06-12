"""Pydantic models for the Packer image-builder endpoints."""
from typing import Optional
from pydantic import BaseModel


class AWSPackerBuildRequest(BaseModel):
    image_name: str
    source_ami: str
    instance_type: str = "t3.micro"
    ssh_username: str = "ec2-user"
    provisioner_script: str = ""
    archive_template: bool = False


class AzurePackerBuildRequest(BaseModel):
    image_name: str
    image_publisher: str = "Canonical"
    image_offer: str = "0001-com-ubuntu-server-jammy"
    image_sku: str = "22_04-lts"
    vm_size: str = "Standard_B2s"
    os_type: str = "Linux"  # "Linux" | "Windows" — picks the template generator
    provisioner_script: str = ""
    archive_template: bool = False


class GCPPackerBuildRequest(BaseModel):
    image_name: str
    source_image: str
    machine_type: str = "e2-medium"
    ssh_username: str = "packer"
    provisioner_script: str = ""
    archive_template: bool = False


class PackerBuildResponse(BaseModel):
    job_id: str
    status: str
    message: str
