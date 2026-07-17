"""
Oracle Cloud Infrastructure service layer — Compute + Networking + Vault secrets.

Credentials use OCI API-key signing, resolved from config_service (wizard-stored,
encrypted) via the ``_cfg`` helper: tenancy OCID + user OCID + key fingerprint +
private-key PEM (+ optional passphrase) + region. Every resource lives in a
compartment (``oci_compartment_ocid``; blank → the tenancy root).

All blocking oci-SDK calls run in ``asyncio.to_thread()`` so the FastAPI event
loop is never blocked — the same discipline as aws_service / gcp_service. The SDK
itself is imported lazily inside ``_require_oci`` so the app boots cleanly when
OCI isn't configured (community-edition invariant).
"""
import asyncio
import base64
import json
import logging
from typing import List, Optional

from . import oci_freetier

logger = logging.getLogger(__name__)


class OCIError(Exception):
    pass


# ── Credential helpers ────────────────────────────────────────────────────────

def _cfg(key: str) -> str:
    from . import config_service
    return config_service.get(key) or ""


def _require_oci():
    try:
        import oci  # noqa: F401
    except ImportError:
        raise OCIError("oci SDK is not installed — run: pip install oci")


def _oci_config() -> dict:
    """Build (and validate) the oci-SDK config dict from stored credentials.
    Raises OCIError with the missing keys when OCI isn't configured."""
    _require_oci()
    import oci

    tenancy = _cfg("oci_tenancy_ocid")
    user = _cfg("oci_user_ocid")
    fingerprint = _cfg("oci_fingerprint")
    key_content = _cfg("oci_private_key")
    region = _cfg("oci_region") or "us-ashburn-1"
    passphrase = _cfg("oci_private_key_passphrase")

    missing = [n for n, v in (
        ("oci_tenancy_ocid", tenancy), ("oci_user_ocid", user),
        ("oci_fingerprint", fingerprint), ("oci_private_key", key_content),
    ) if not v]
    if missing:
        raise OCIError("OCI is not configured — missing " + ", ".join(missing)
                       + " (run the setup wizard).")

    cfg = {
        "user": user,
        "fingerprint": fingerprint,
        "tenancy": tenancy,
        "region": region,
        "key_content": key_content,
    }
    if passphrase:
        cfg["pass_phrase"] = passphrase
    try:
        oci.config.validate_config(cfg)
    except Exception as exc:  # noqa: BLE001 — surface as our error type
        raise OCIError(f"invalid OCI credentials: {exc}") from exc
    return cfg


def _compartment() -> str:
    """Target compartment OCID; falls back to the tenancy root when unset."""
    return _cfg("oci_compartment_ocid") or _cfg("oci_tenancy_ocid")


def _iso(dt) -> str:
    try:
        return dt.isoformat() if dt else ""
    except Exception:
        return str(dt or "")


# ── Image operations ──────────────────────────────────────────────────────────

def _list_images_sync(compartment_id: str) -> list[dict]:
    import oci
    client = oci.core.ComputeClient(_oci_config())
    raw = oci.pagination.list_call_get_all_results(
        client.list_images, compartment_id=compartment_id).data
    images = []
    for img in raw:
        if getattr(img, "lifecycle_state", "") not in ("AVAILABLE", ""):
            continue
        # Oracle-provided (platform) images have no owning compartment; custom
        # images captured in this tenancy carry the compartment id.
        source = "custom" if getattr(img, "compartment_id", None) else "platform"
        images.append({
            "ocid":         img.id,
            "display_name": img.display_name or "",
            "operating_system": getattr(img, "operating_system", "") or "",
            "operating_system_version": getattr(img, "operating_system_version", "") or "",
            "lifecycle_state": getattr(img, "lifecycle_state", "AVAILABLE") or "AVAILABLE",
            "time_created": _iso(getattr(img, "time_created", None)),
            "size_gb":      int((getattr(img, "size_in_mbs", 0) or 0) / 1024),
            "source":       source,
        })
    images.sort(key=lambda x: x["time_created"], reverse=True)
    return images


async def list_images(compartment_id: str = "") -> list[dict]:
    return await asyncio.to_thread(_list_images_sync, compartment_id or _compartment())


# ── Shapes / availability domains / subnets ───────────────────────────────────

def _list_availability_domains_sync(compartment_id: str) -> list[str]:
    import oci
    client = oci.identity.IdentityClient(_oci_config())
    ads = client.list_availability_domains(compartment_id=compartment_id).data
    return [a.name for a in ads]


def _list_shapes_sync(compartment_id: str, availability_domain: str = "") -> list[dict]:
    import oci
    client = oci.core.ComputeClient(_oci_config())
    kwargs = {"compartment_id": compartment_id}
    if availability_domain:
        kwargs["availability_domain"] = availability_domain
    raw = oci.pagination.list_call_get_all_results(client.list_shapes, **kwargs).data
    seen: dict = {}
    for s in raw:
        name = s.shape
        if name in seen:
            continue
        is_flex = "Flex" in name or getattr(s, "ocpu_options", None) is not None
        seen[name] = {
            "shape":       name,
            "ocpus":       getattr(s, "ocpus", None),
            "memory_gb":   getattr(s, "memory_in_gbs", None),
            "is_flexible": bool(is_flex),
            "free_tier":   oci_freetier.is_free_shape(name),
        }
    # Free-tier shapes first, then alphabetical.
    return sorted(seen.values(), key=lambda x: (not x["free_tier"], x["shape"]))


def _list_subnets_sync(compartment_id: str, vcn_id: str = "") -> list[dict]:
    import oci
    vnet = oci.core.VirtualNetworkClient(_oci_config())
    kwargs = {"compartment_id": compartment_id}
    if vcn_id:
        kwargs["vcn_id"] = vcn_id
    raw = oci.pagination.list_call_get_all_results(vnet.list_subnets, **kwargs).data
    subnets = []
    for sn in raw:
        if getattr(sn, "lifecycle_state", "AVAILABLE") not in ("AVAILABLE", ""):
            continue
        subnets.append({
            "ocid":         sn.id,
            "display_name": sn.display_name or "",
            "cidr_block":   getattr(sn, "cidr_block", "") or "",
            "vcn_ocid":     getattr(sn, "vcn_id", "") or "",
            "prohibit_public_ip": bool(getattr(sn, "prohibit_public_ip_on_vnic", False)),
        })
    return subnets


def _get_network_options_sync(compartment_id: str, vcn_id: str) -> dict:
    ads: list[str] = []
    try:
        ads = _list_availability_domains_sync(compartment_id)
    except Exception as exc:
        logger.warning("OCI list_availability_domains failed: %s", exc)

    try:
        shapes = _list_shapes_sync(compartment_id, ads[0] if ads else "")
    except Exception as exc:
        logger.warning("OCI list_shapes failed: %s", exc)
        shapes = []

    subnets: list[dict] = []
    try:
        subnets = _list_subnets_sync(compartment_id, vcn_id)
    except Exception as exc:
        logger.warning("OCI list_subnets failed: %s", exc)

    return {
        "availability_domains": ads,
        "shapes":         shapes,
        "subnets":        subnets,
        "region":         _cfg("oci_region") or "us-ashburn-1",
        "compartment_ocid": compartment_id,
        "ssh_key_configured": bool(_cfg("oci_ssh_key_secret")),
        "free_tier":      oci_freetier.free_tier_catalog(),
    }


async def get_network_options(compartment_id: str = "", vcn_id: str = "") -> dict:
    return await asyncio.to_thread(
        _get_network_options_sync, compartment_id or _compartment(), vcn_id or _cfg("oci_vcn_ocid"))


async def list_availability_domains(compartment_id: str = "") -> list[str]:
    return await asyncio.to_thread(_list_availability_domains_sync, compartment_id or _compartment())


async def list_subnets(compartment_id: str = "", vcn_id: str = "") -> list[dict]:
    return await asyncio.to_thread(
        _list_subnets_sync, compartment_id or _compartment(), vcn_id or _cfg("oci_vcn_ocid"))


# ── Vault secrets (SSH keypair) ───────────────────────────────────────────────

def _get_secret_content_sync(secret_ref: str) -> str:
    """Return the plaintext of an OCI Vault secret. ``secret_ref`` is either a
    secret OCID (ocid1.vaultsecret…) or a secret *name* (needs oci_vault_ocid)."""
    import oci
    client = oci.secrets.SecretsClient(_oci_config())
    if secret_ref.startswith("ocid1.vaultsecret"):
        bundle = client.get_secret_bundle(secret_id=secret_ref).data
    else:
        vault_id = _cfg("oci_vault_ocid")
        if not vault_id:
            raise OCIError("oci_vault_ocid is required to resolve a Vault secret by name "
                           f"(got name {secret_ref!r}); set it or use the secret OCID")
        bundle = client.get_secret_bundle_by_name(
            secret_name=secret_ref, vault_id=vault_id).data
    content = bundle.secret_bundle_content.content
    return base64.b64decode(content).decode("utf-8")


async def get_secret(secret_ref: str) -> str:
    return await asyncio.to_thread(_get_secret_content_sync, secret_ref)


def _clean_public_key(value: str) -> str:
    if not value:
        return ""
    flat = value.replace("\r", "").replace("\n", "").strip()
    return " ".join(flat.split())


async def get_ssh_public_key(secret_ref: str) -> str:
    """SSH public key from a Vault secret — JSON {public_key} or a raw key string."""
    raw = await get_secret(secret_ref)
    try:
        data = json.loads(raw)
        pub = _clean_public_key(data.get("public_key") or data.get("publicKey") or "")
    except (json.JSONDecodeError, AttributeError):
        pub = _clean_public_key(raw)
    return pub


async def get_ssh_private_key(secret_ref: str) -> str:
    """SSH **private** key (PEM) from a Vault secret — only when the secret is a
    JSON keypair with a ``private_key`` field (returns "" otherwise). Never logged.
    The Entitle / Password Safe VM hooks call this to pair the key cloud-init injected."""
    if not secret_ref:
        return ""
    raw = await get_secret(secret_ref)
    try:
        data = json.loads(raw)
        priv = data.get("private_key") or data.get("privateKey") or ""
    except (json.JSONDecodeError, AttributeError):
        priv = ""
    return priv.strip() if priv else ""


# ── Instance operations ───────────────────────────────────────────────────────

def _instance_ips_sync(compute, vnet, compartment_id: str, instance_id: str) -> tuple:
    """(private_ip, public_ip) for an instance via its primary VNIC attachment."""
    private_ip = public_ip = None
    try:
        attachments = compute.list_vnic_attachments(
            compartment_id=compartment_id, instance_id=instance_id).data
        for att in attachments:
            if getattr(att, "vnic_id", None):
                vnic = vnet.get_vnic(att.vnic_id).data
                private_ip = getattr(vnic, "private_ip", None) or private_ip
                public_ip = getattr(vnic, "public_ip", None) or public_ip
                if private_ip:
                    break
    except Exception as exc:
        logger.warning("OCI VNIC lookup failed for %s: %s", instance_id, exc)
    return private_ip, public_ip


def _instance_to_dict(inst, private_ip=None, public_ip=None) -> dict:
    sc = getattr(inst, "shape_config", None)
    tags = getattr(inst, "freeform_tags", None) or {}
    return {
        "ocid":          inst.id,
        "display_name":  inst.display_name or "",
        "shape":         getattr(inst, "shape", "") or "",
        "ocpus":         getattr(sc, "ocpus", None) if sc else None,
        "memory_gb":     getattr(sc, "memory_in_gbs", None) if sc else None,
        "lifecycle_state": getattr(inst, "lifecycle_state", "") or "",
        "availability_domain": getattr(inst, "availability_domain", "") or "",
        "private_ip":    private_ip,
        "public_ip":     public_ip,
        "time_created":  _iso(getattr(inst, "time_created", None)),
        "workgroup":     tags.get("workgroup") or None,
    }


def _launch_instance_sync(
    compartment_id: str,
    availability_domain: str,
    instance_name: str,
    shape: str,
    image_ocid: str,
    subnet_ocid: str,
    assign_public_ip: bool,
    ssh_public_key: str,
    ocpus: Optional[float] = None,
    memory_gb: Optional[float] = None,
    boot_volume_gb: int = 50,
    workgroup: str = "",
) -> dict:
    import oci
    cfg = _oci_config()
    compute = oci.core.ComputeClient(cfg)
    vnet = oci.core.VirtualNetworkClient(cfg)

    if not availability_domain:
        ads = _list_availability_domains_sync(compartment_id)
        if not ads:
            raise OCIError("no availability domains found in the compartment")
        availability_domain = ads[0]

    metadata = {}
    if ssh_public_key:
        metadata["ssh_authorized_keys"] = _clean_public_key(ssh_public_key)

    freeform = {"managed-by": "vm-dashboard"}
    if workgroup:
        freeform["workgroup"] = workgroup

    source = oci.core.models.InstanceSourceViaImageDetails(
        image_id=image_ocid,
        boot_volume_size_in_gbs=int(boot_volume_gb) if boot_volume_gb else None,
    )
    vnic = oci.core.models.CreateVnicDetails(
        subnet_id=subnet_ocid,
        assign_public_ip=bool(assign_public_ip),
    )
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=availability_domain,
        compartment_id=compartment_id,
        shape=shape,
        display_name=instance_name,
        source_details=source,
        create_vnic_details=vnic,
        metadata=metadata,
        freeform_tags=freeform,
    )
    # Flexible shapes (A1.Flex, E-flex) require an explicit OCPU/memory config.
    if ocpus:
        details.shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=float(ocpus),
            memory_in_gbs=float(memory_gb) if memory_gb else None,
        )

    resp = compute.launch_instance(details)
    instance = resp.data
    # Wait for RUNNING so we can return live IPs (bounded).
    try:
        instance = oci.wait_until(
            compute, compute.get_instance(instance.id),
            "lifecycle_state", "RUNNING", max_wait_seconds=600,
        ).data
    except Exception as exc:
        logger.warning("OCI instance %s did not reach RUNNING in time: %s", instance.id, exc)

    private_ip, public_ip = _instance_ips_sync(compute, vnet, compartment_id, instance.id)
    return _instance_to_dict(instance, private_ip, public_ip)


async def launch_instance(
    compartment_id: str,
    availability_domain: str,
    instance_name: str,
    shape: str,
    image_ocid: str,
    subnet_ocid: str,
    assign_public_ip: bool,
    ssh_public_key: str,
    ocpus: Optional[float] = None,
    memory_gb: Optional[float] = None,
    boot_volume_gb: int = 50,
    workgroup: str = "",
) -> dict:
    try:
        return await asyncio.to_thread(
            _launch_instance_sync, compartment_id, availability_domain, instance_name,
            shape, image_ocid, subnet_ocid, assign_public_ip, ssh_public_key,
            ocpus, memory_gb, boot_volume_gb, workgroup,
        )
    except OCIError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OCIError(f"launch_instance failed: {exc}") from exc


def _describe_instances_sync(compartment_id: str, instance_ocids: list[str]) -> list[dict]:
    import oci
    cfg = _oci_config()
    compute = oci.core.ComputeClient(cfg)
    vnet = oci.core.VirtualNetworkClient(cfg)
    results = []
    for ocid in instance_ocids:
        try:
            inst = compute.get_instance(ocid).data
            if getattr(inst, "lifecycle_state", "") == "TERMINATED":
                continue
            private_ip, public_ip = _instance_ips_sync(compute, vnet, compartment_id, ocid)
            results.append(_instance_to_dict(inst, private_ip, public_ip))
        except Exception as exc:
            logger.warning("OCI get_instance %s failed: %s", ocid, exc)
    return results


async def describe_instances(compartment_id: str, instance_ocids: list[str]) -> list[dict]:
    if not instance_ocids:
        return []
    return await asyncio.to_thread(
        _describe_instances_sync, compartment_id or _compartment(), instance_ocids)


def _terminate_instance_sync(instance_id: str, preserve_boot_volume: bool = False) -> None:
    import oci
    compute = oci.core.ComputeClient(_oci_config())
    compute.terminate_instance(instance_id, preserve_boot_volume=preserve_boot_volume)


async def terminate_instance(instance_id: str, preserve_boot_volume: bool = False) -> None:
    try:
        await asyncio.to_thread(_terminate_instance_sync, instance_id, preserve_boot_volume)
    except OCIError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OCIError(f"terminate_instance failed: {exc}") from exc
