"""Pydantic models for the image registry (`/images` page + `/api/images`)."""
from typing import Optional

from pydantic import BaseModel


class ImagePromotion(BaseModel):
    """One promote record under a RegisteredImage.promotions[<cloud>]."""
    status: str                                  # "completed" | "manual" | "failed" | "pending"
    image_id: Optional[str] = None               # cloud-native image identifier
    region: Optional[str] = None                 # for AWS / GCP region scoping
    self_link: Optional[str] = None              # GCP-specific
    notes: Optional[str] = None                  # "manual" status carries the operator instructions
    promoted_at: Optional[str] = None            # ISO timestamp


class RegisteredImageInfo(BaseModel):
    id: str
    name: str
    version: str
    description: Optional[str] = None
    source_cloud: str
    source_image_id: Optional[str] = None
    source_region: Optional[str] = None
    artefact_url: Optional[str] = None
    artefact_format: Optional[str] = None
    promotions: dict[str, ImagePromotion] = {}
    created_at: str
    created_by: str


class RegisterImageRequest(BaseModel):
    name: str
    version: str
    description: Optional[str] = None
    source_cloud: str                            # "aws" | "azure" | "gcp"
    source_image_id: Optional[str] = None
    source_region: Optional[str] = None
    artefact_url: Optional[str] = None
    artefact_format: Optional[str] = None        # "vhd" | "raw" | "vmdk" | "ova"


class PromoteImageRequest(BaseModel):
    target_cloud: str                            # "aws" | "azure" | "gcp"
    target_region: Optional[str] = None          # honoured for AWS / GCP same-cloud cross-region
    target_resource_group: Optional[str] = None  # honoured for Azure
