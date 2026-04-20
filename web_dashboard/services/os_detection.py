"""
Pure string-parsing helpers for determining a Linux distro + default SSH user
from an AMI name (EC2) or Azure image metadata. Used by the core AWS/Azure
deploy paths to pick the right remote user for SSH key injection — intentionally
dependency-free so the helpers stay available when optional features like
Ansible config-management are disabled.
"""
from __future__ import annotations

import re


def detect_os_type(ami_name: str) -> tuple[str, str]:
    """
    Parse an AMI name to determine OS type and default Ansible/SSH user.
    Returns (os_type, ansible_user).
    """
    name = ami_name.lower()
    if ("amzn" in name or "al2023" in name or "al2" in name
            or ("amazon" in name and "linux" in name)):
        return ("amazon-linux", "ec2-user")
    if "ubuntu" in name:
        return ("ubuntu", "ubuntu")
    if "debian" in name:
        # Official Debian Project AMIs (debian-NN-amd64-*) use "admin".
        # Community/marketplace Debian AMIs typically use "debian".
        if re.match(r"debian-\d+[-.]", name):
            return ("debian", "admin")
        return ("debian", "debian")
    if "fedora" in name:
        return ("fedora", "fedora")
    if "rocky" in name:
        return ("rocky", "rocky")
    if "almalinux" in name or "alma" in name:
        return ("almalinux", "almalinux")
    if any(k in name for k in ("rhel", "redhat", "centos")):
        return ("rhel", "ec2-user")
    return ("unknown", "ec2-user")


def detect_azure_os_type(
    image_sku: str = "",
    image_offer: str = "",
    image_publisher: str = "",
    image_id: str = "",
) -> tuple[str, str]:
    """
    Detect OS type and default SSH user from Azure image metadata.
    Returns (os_type, ansible_user).

    Checks image_offer and image_sku first (marketplace images), then falls
    back to image_id path segment (managed/gallery images).
    os_type == "windows" is a skip sentinel — callers should exclude these VMs.
    """
    def _check(s: str) -> tuple[str, str] | None:
        s = s.lower()
        if "ubuntu" in s:
            return ("ubuntu", "azureuser")
        if "debian" in s:
            return ("debian", "azureuser")
        if any(k in s for k in ("rhel", "redhat", "rocky", "centos", "alma")):
            return ("rhel", "azureuser")
        if "windows" in s:
            return ("windows", "")
        return None

    for field in (image_offer, image_sku, image_publisher):
        result = _check(field)
        if result:
            return result

    if image_id:
        last_segment = image_id.rstrip("/").split("/")[-1]
        result = _check(last_segment)
        if result:
            return result

    return ("linux", "azureuser")
