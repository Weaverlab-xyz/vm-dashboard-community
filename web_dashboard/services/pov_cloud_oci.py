"""The OCI driver behind ``pov_cloud_env``: Compute and VCN calls, and nothing else.

The fourth cloud. Its shape is closest to AWS's — no environment object, so the teardown
unpicks resource types in dependency order — with three differences that matter.

Everything policy-shaped — the environment id, which VMs get user-data, what a refusal
says — still lives in ``pov_cloud_env``. This module knows OCI.

**A compartment is NOT used as the environment, despite being the obvious analogy.** It
looks like Azure's resource group and behaves nothing like it: creating one needs
tenancy-level IAM the POV's credential should not have, deleting one requires it to be
empty first, the delete is a slow asynchronous operation, and a deleted compartment's name
stays reserved. A POV that could not be torn down in one pass — or whose name could not be
reused for ninety days — would be worse than the tagging this uses instead. The compartment
is still *recorded*, for the same reason GCP records its project: a destroy aimed at the
wrong one is the worst version of this bug.

**The default security list allows SSH from anywhere.** Every VCN gets one, and a POV
placed on it would be a customer environment with port 22 open to the internet. This
driver creates its own, allowing only the POV's own subnet, and never attaches the default.

**Availability domains are listed, never assembled.** They are named like
``Uocm:PHX-AD-1`` — a tenancy-specific prefix and a region code — so there is no string to
build, and many regions have exactly one.

Shapes are the other OCI-specific wrinkle: a Flex shape is refused without an explicit
OCPU count, so the template's instance type accepts ``SHAPE``, ``SHAPE:ocpus`` or
``SHAPE:ocpus:memory_gb``. See :func:`parse_shape`.
"""
from __future__ import annotations

import base64
import logging

from . import oci_service, pov_cloud_env as env
from .oci_service import OCIError

logger = logging.getLogger(__name__)

# Instances and VCNs are regional, and the SDK client is built per region, so a listing is
# per region like AWS's rather than tenancy-wide.
LISTS_ALL_REGIONS = False

# OCI freeform tags are a plain string→string map with none of GCE's key restrictions, so
# the shared tag keys go in unchanged. Worth stating because the GCP driver next door has
# to map every one of them.
#
# OCI also has *defined* tags, which are namespaced and governed by tenancy policy. Not
# used here: they need a tag namespace to exist first, which is a tenancy-level action a
# POV's credential should not be doing.

# Lifecycle states that mean the instance is gone or going. Excluded from every read, for
# the same reason AWS excludes `terminated`.
_DEAD_STATES = ("terminating", "terminated")

# OCI lifecycle state -> the dashboard's runstate vocabulary. Anything unlisted passes
# through lowercased.
_RUNSTATE = {"running": "running", "stopped": "stopped"}

# A Flex shape is refused outright without a shape_config, and the error names the API
# field rather than the template. These are the defaults when the instance type does not
# say — two OCPUs is a working demonstration VM, and OCI's E-series allows up to 16 GB per
# OCPU.
_FLEX_DEFAULT_OCPUS = 2.0
_FLEX_DEFAULT_MEMORY_GB = 16.0


def default_region() -> str:
    return _cfg("oci_region", "us-ashburn-1")


def _cfg(key: str, fallback: str = "") -> str:
    """Config first, then settings, and never raising.

    `oci_service._cfg` reads config_service alone and lets its failure out — fine on the
    demo path, where the database is always there by the time anything calls it. These are
    read from the lab-platform registry, which the UI and the tests exercise constantly
    and sometimes before a database exists. Same shape as `skytap_service._cfg`.
    """
    try:
        from . import config_service
        value = config_service.get(key)
        if value:
            return value
    except Exception:  # noqa: BLE001
        pass
    from ..config import settings
    return getattr(settings, key, "") or fallback


def configured() -> bool:
    from . import feature_flags
    return feature_flags.cloud_configured("oci")


def configured_project_id() -> str:
    """The compartment POVs are built in, falling back to the tenancy root.

    Exposed because the ``projects`` capability is True here, as it is for GCP: a
    compartment is a real container an environment goes INTO, `PovEnvironment.project_id`
    records which, and the teardown reads it back rather than re-deriving it.
    ``expiry_reaper`` states the rule — a destroy aimed at the wrong project is the worst
    version of this bug — and a compartment is exactly that kind of boundary.
    """
    return _cfg("oci_compartment_ocid") or _cfg("oci_tenancy_ocid")


def _compartment(env_id: str = "") -> str:
    """The compartment an environment lives in: the recorded one, else the default."""
    if env_id:
        recorded = env.recorded_project(env_id, "oci")
        if recorded:
            return recorded
    found = configured_project_id()
    if not found:
        raise env.CloudEnvError(
            "no OCI compartment or tenancy is configured, so there is nowhere to build a "
            "POV. Set it in Settings → Integrations → POV cloud provider.")
    return found


def parse_shape(instance_type: str) -> tuple:
    """``(shape, ocpus, memory_gb)`` from a template's instance type.

    OCI's Flex shapes — which are most of the current catalogue — are refused without an
    explicit OCPU count, and the API's error names ``shapeConfig`` rather than the
    template field that produced it. Rather than add a column that only one cloud reads,
    the instance type accepts an optional suffix:

        VM.Standard.E4.Flex          two OCPUs and 16 GB, the defaults below
        VM.Standard.E4.Flex:4        four OCPUs, 16 GB per OCPU capped at OCI's own limit
        VM.Standard.E4.Flex:4:32     four OCPUs and 32 GB
        VM.Standard2.2               a fixed shape; a suffix on one of these is refused

    A fixed shape with a suffix is refused rather than ignored: it means the author
    believed they were sizing something.
    """
    raw = (instance_type or "").strip()
    if not raw:
        raise env.CloudEnvError("a template VM has no OCI shape")
    parts = raw.split(":")
    shape = parts[0].strip()
    is_flex = shape.lower().endswith(".flex")

    if len(parts) == 1:
        if is_flex:
            return shape, _FLEX_DEFAULT_OCPUS, _FLEX_DEFAULT_MEMORY_GB
        return shape, None, None
    if not is_flex:
        raise env.CloudEnvError(
            f"{raw!r} sizes a fixed shape. {shape} has a built-in OCPU count, so the "
            f"suffix would be ignored — drop it, or pick a .Flex shape.")
    try:
        ocpus = float(parts[1])
        memory = float(parts[2]) if len(parts) > 2 else _FLEX_DEFAULT_MEMORY_GB
    except ValueError:
        raise env.CloudEnvError(
            f"{raw!r} is not a shape this understands. Use SHAPE, SHAPE:ocpus or "
            f"SHAPE:ocpus:memory_gb.") from None
    if ocpus <= 0 or memory <= 0:
        raise env.CloudEnvError(f"{raw!r} asks for a shape with no CPU or no memory")
    return shape, ocpus, memory


def _user_data(payload: str):
    """Cloud-init for an OCI guest, base64 as the metadata field requires.

    The SDK passes ``user_data`` through untouched — OCI documents it as base64 and
    nothing encodes it for you. Plain text gives an instance that boots fine and never
    runs its bootstrap, which is the silent failure `cloud_init` earns its own capability
    value to avoid. The Azure driver carries the same wrinkle.
    """
    if not payload:
        return None
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def _clients():
    import oci
    cfg = oci_service._oci_config()
    return (oci.core.ComputeClient(cfg), oci.core.VirtualNetworkClient(cfg),
            oci.identity.IdentityClient(cfg))


def _availability_domain_sync(identity, compartment: str) -> str:
    """The first availability domain of the configured region.

    Listed, never assembled: an AD name is ``Uocm:PHX-AD-1`` — a tenancy-specific prefix
    and a region code — so there is nothing to build, and many regions have exactly one.
    """
    ads = identity.list_availability_domains(compartment_id=compartment).data or []
    if not ads:
        raise env.CloudEnvError(
            "this OCI region reports no availability domains for the configured "
            "compartment, so there is nowhere to place a VM")
    return sorted(a.name for a in ads)[0]


# ── verify ───────────────────────────────────────────────────────────────────

async def verify() -> tuple[bool, str]:
    """Prove the credential, the compartment and the region, in one cheap read.

    An expected failure is a RETURN VALUE, not an exception — a caught exception
    stringified into a response body is what CodeQL's stack-trace-exposure rule fires on.
    """
    if not configured():
        return False, ("No OCI credentials are configured. Add them in "
                       "Settings → Integrations → POV cloud provider.")
    region = default_region()
    try:
        compartment = _compartment()
        _compute, _vnet, identity = _clients()
        ad = await oci_service._to_thread(_availability_domain_sync, identity,
                                          compartment)
    except env.CloudEnvError as exc:
        return False, str(exc)
    except OCIError as exc:
        return False, f"OCI refused the call: {exc}"
    except Exception:  # noqa: BLE001
        logger.warning("OCI POV verify failed", exc_info=True)
        return False, ("OCI did not answer. Check the tenancy, user, fingerprint and "
                       "private key, and that the compartment exists in "
                       f"{region}.")
    return True, f"Connected to OCI in {region} (first availability domain {ad})."


# ── create ───────────────────────────────────────────────────────────────────

def _create_network_sync(compartment: str, env_id: str, cidr: str, sub_cidr: str) -> dict:
    import oci

    _compute, vnet, _identity = _clients()
    tags = dict(env.base_tags(env_id))

    vcn = vnet.create_vcn(oci.core.models.CreateVcnDetails(
        compartment_id=compartment, cidr_block=cidr, display_name=f"{env_id}-vcn",
        dns_label=None, freeform_tags=tags)).data
    oci.wait_until(vnet, vnet.get_vcn(vcn.id), "lifecycle_state", "AVAILABLE")

    gateway = vnet.create_internet_gateway(
        oci.core.models.CreateInternetGatewayDetails(
            compartment_id=compartment, vcn_id=vcn.id, is_enabled=True,
            display_name=f"{env_id}-igw", freeform_tags=tags)).data

    route = vnet.create_route_table(oci.core.models.CreateRouteTableDetails(
        compartment_id=compartment, vcn_id=vcn.id, display_name=f"{env_id}-rt",
        freeform_tags=tags,
        route_rules=[oci.core.models.RouteRule(
            destination="0.0.0.0/0", destination_type="CIDR_BLOCK",
            network_entity_id=gateway.id)])).data

    # Our OWN security list, and the default is never attached. **Every VCN gets a default
    # security list that allows SSH from 0.0.0.0/0** — a POV placed on it would be a
    # customer environment with port 22 open to the internet. This one allows the POV's own
    # subnet in and everything out, which is what the agent, the Gateway and the Resource
    # Broker need and nothing more.
    security = vnet.create_security_list(oci.core.models.CreateSecurityListDetails(
        compartment_id=compartment, vcn_id=vcn.id, display_name=f"{env_id}-sl",
        freeform_tags=tags,
        egress_security_rules=[oci.core.models.EgressSecurityRule(
            destination="0.0.0.0/0", destination_type="CIDR_BLOCK", protocol="all")],
        ingress_security_rules=[oci.core.models.IngressSecurityRule(
            source=sub_cidr, source_type="CIDR_BLOCK", protocol="all")])).data

    subnet = vnet.create_subnet(oci.core.models.CreateSubnetDetails(
        compartment_id=compartment, vcn_id=vcn.id, cidr_block=sub_cidr,
        display_name=f"{env_id}-subnet", route_table_id=route.id,
        security_list_ids=[security.id], freeform_tags=tags)).data
    oci.wait_until(vnet, vnet.get_subnet(subnet.id), "lifecycle_state", "AVAILABLE")

    return {"compartment": compartment, "vcn_id": vcn.id, "subnet_id": subnet.id,
            "subnet_cidr": sub_cidr, "internet_gateway_id": gateway.id,
            "route_table_id": route.id, "security_list_id": security.id}


def _create_vms_sync(compartment: str, env_id: str, specs: list, net: dict) -> list:
    import oci

    compute, _vnet, identity = _clients()
    ad = _availability_domain_sync(identity, compartment)
    created = []

    for spec in specs:
        name = spec["name"]
        role = spec.get("role") or "target"
        shape, ocpus, memory = parse_shape(spec["instance_type"])
        tags = dict(env.base_tags(env_id))
        tags[env.TAG_NAME] = name
        tags[env.TAG_ROLE] = role

        metadata = {}
        payload = _user_data(spec.get("user_data") or "")
        if payload:
            metadata["user_data"] = payload

        details = oci.core.models.LaunchInstanceDetails(
            availability_domain=ad,
            compartment_id=compartment,
            shape=shape,
            display_name=name,
            freeform_tags=tags,
            metadata=metadata,
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=spec["image_id"],
                boot_volume_size_in_gbs=int(spec.get("disk_gb")
                                            or env.DEFAULT_DISK_GB)),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=net["subnet_id"],
                # A public address for egress, and no NAT gateway — the same arithmetic
                # as the other three drivers. Safe only because the security list above
                # admits nothing from outside the POV's own subnet.
                assign_public_ip=True,
                freeform_tags=tags))
        if ocpus:
            # A Flex shape is refused outright without this, and the error names
            # `shapeConfig` rather than the template field that produced it.
            details.shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=float(ocpus), memory_in_gbs=float(memory))

        instance = compute.launch_instance(details).data
        created.append({"id": instance.id, "name": name})
        logger.info("POV %s: launched OCI instance %s (%s)", env_id, name, instance.id)
    return created


async def create_network(env_id: str, region: str, cidr: str, sub_cidr: str) -> dict:
    return await oci_service._to_thread(_create_network_sync, _compartment(env_id),
                                        env_id, cidr, sub_cidr)


async def create_vms(env_id: str, region: str, specs: list, net: dict) -> list:
    return await oci_service._to_thread(
        _create_vms_sync, net.get("compartment") or _compartment(env_id), env_id, specs,
        net)


# ── read ─────────────────────────────────────────────────────────────────────

def _vm(inst, private_ip: str, os_family: str) -> dict:
    tags = dict(getattr(inst, "freeform_tags", None) or {})
    state = (inst.lifecycle_state or "").lower()
    shape_cfg = getattr(inst, "shape_config", None)
    shape = inst.shape or ""
    if shape_cfg and getattr(shape_cfg, "ocpus", None):
        shape = f"{shape}:{shape_cfg.ocpus:g}:{(shape_cfg.memory_in_gbs or 0):g}"
    return {
        "id": inst.id,
        "name": tags.get(env.TAG_NAME) or inst.display_name or inst.id,
        "os_family": os_family,
        "runstate": _RUNSTATE.get(state, state),
        "private_ip": private_ip,
        "role": tags.get(env.TAG_ROLE) or "target",
        # Reported in the same form the template writes, so an operator comparing the two
        # is not left translating a shape_config back into a suffix.
        "instance_type": shape,
        "disk_gb": 0,
        "launched_at": (inst.time_created.isoformat()
                        if getattr(inst, "time_created", None) else ""),
        # No NAT-ed guest ports on a cloud: access is PRA, through the POV's own Gateway.
        "published_services": [],
    }


def _instance_details_sync(compute, vnet, compartment: str, inst) -> tuple:
    """``(private_ip, os_family)`` for one instance.

    Both need a second call: the address lives on the VNIC rather than the instance, and
    OCI reports the guest OS only through the image the boot volume came from. A failure
    on either is not worth failing the read for — an address the POV page cannot show yet
    is better than a page that will not load.
    """
    private_ip, os_family = "", "linux"
    try:
        for attachment in compute.list_vnic_attachments(
                compartment_id=compartment, instance_id=inst.id).data or []:
            vnic = vnet.get_vnic(attachment.vnic_id).data
            if vnic and vnic.private_ip:
                private_ip = vnic.private_ip
                break
    except Exception:  # noqa: BLE001
        logger.warning("POV: could not read the VNIC for %s", inst.id, exc_info=True)
    try:
        image = compute.get_image(inst.image_id).data if getattr(
            inst, "image_id", None) else None
        if image and "windows" in (image.operating_system or "").lower():
            os_family = "windows"
    except Exception:  # noqa: BLE001
        pass
    return private_ip, os_family


def _list_instances_sync(compute, compartment: str) -> list:
    from oci.pagination import list_call_get_all_results
    found = list_call_get_all_results(compute.list_instances,
                                      compartment_id=compartment).data or []
    return [i for i in found
            if (i.lifecycle_state or "").lower() not in _DEAD_STATES]


def _environment(env_id: str, vms: list, region: str) -> dict:
    states = {v["runstate"] for v in vms}
    if not states:
        runstate = ""
    elif states == {"running"}:
        runstate = "running"
    elif states == {"stopped"}:
        runstate = "stopped"
    else:
        runstate = "busy"
    return {
        "id": env_id,
        "name": env_id[len(env.ENV_ID_PREFIX):] if env.is_env_id(env_id) else env_id,
        "runstate": runstate,
        "region": region,
        "vm_count": len(vms),
        "vms": vms,
        "url": "",
    }


def _of_env(inst, env_id: str) -> bool:
    return (dict(getattr(inst, "freeform_tags", None) or {})
            .get(env.TAG_ENVIRONMENT)) == env_id


def _read_environment_sync(compartment: str, env_id: str, region: str):
    compute, vnet, _identity = _clients()
    mine = [i for i in _list_instances_sync(compute, compartment) if _of_env(i, env_id)]
    if mine:
        vms = []
        for inst in mine:
            ip, os_family = _instance_details_sync(compute, vnet, compartment, inst)
            vms.append(_vm(inst, ip, os_family))
        return _environment(env_id, vms, region)

    # No instances is not the same as gone. A teardown that terminated the VMs and then
    # failed on the VCN leaves a real environment, and answering None would let
    # `pov_reconcile` mark the POV missing while its network still exists.
    for vcn in (vnet.list_vcns(compartment_id=compartment).data or []):
        if (dict(vcn.freeform_tags or {}).get(env.TAG_ENVIRONMENT)) == env_id:
            return _environment(env_id, [], region)
    return None


async def read_environment(env_id: str, region: str):
    return await oci_service._to_thread(_read_environment_sync, _compartment(env_id),
                                        env_id, region)


def _list_environments_sync(compartment: str, region: str) -> list:
    compute, vnet, _identity = _clients()
    grouped: dict = {}
    for inst in _list_instances_sync(compute, compartment):
        tags = dict(getattr(inst, "freeform_tags", None) or {})
        if tags.get(env.TAG_MANAGED_BY) != env.MANAGED_BY:
            continue
        env_id = tags.get(env.TAG_ENVIRONMENT) or ""
        if env_id:
            grouped.setdefault(env_id, []).append(inst)

    # And the environments whose instances are gone but whose VCN survives — the orphan
    # /pov/cloud exists to surface, and the state a half-finished teardown leaves.
    for vcn in (vnet.list_vcns(compartment_id=compartment).data or []):
        tags = dict(vcn.freeform_tags or {})
        if tags.get(env.TAG_MANAGED_BY) == env.MANAGED_BY and tags.get(
                env.TAG_ENVIRONMENT):
            grouped.setdefault(tags[env.TAG_ENVIRONMENT], [])

    out = []
    for env_id, rows in sorted(grouped.items()):
        vms = []
        for inst in rows:
            ip, os_family = _instance_details_sync(compute, vnet, compartment, inst)
            vms.append(_vm(inst, ip, os_family))
        out.append(_environment(env_id, vms, region))
    return out


async def list_environments(region: str) -> list:
    return await oci_service._to_thread(_list_environments_sync, _compartment(), region)


async def read_network(env_id: str, region: str) -> dict:
    """The subnet an environment was built with, read back off its tag."""
    compartment = _compartment(env_id)

    def _read():
        _compute, vnet, _identity = _clients()
        for subnet in (vnet.list_subnets(compartment_id=compartment).data or []):
            if (dict(subnet.freeform_tags or {}).get(env.TAG_ENVIRONMENT)) == env_id:
                return {"compartment": compartment, "subnet_id": subnet.id,
                        "subnet_cidr": subnet.cidr_block or "",
                        "vcn_id": subnet.vcn_id}
        return {}

    return await oci_service._to_thread(_read)


# ── power ────────────────────────────────────────────────────────────────────

def _power_sync(compartment: str, env_id: str, action: str) -> int:
    compute, _vnet, _identity = _clients()
    mine = [i for i in _list_instances_sync(compute, compartment) if _of_env(i, env_id)]
    for inst in mine:
        # SOFTSTOP asks the guest to shut down and falls back to a hard stop after a
        # timeout, which is what a scheduled suspend of a customer's environment should
        # do. A STOP is the pull-the-cord version and risks a dirty filesystem on a POV
        # somebody resumes next morning.
        compute.instance_action(inst.id, "START" if action == "start" else "SOFTSTOP")
    return len(mine)


async def power(env_id: str, region: str, action: str) -> None:
    compartment = _compartment(env_id)
    count = await oci_service._to_thread(_power_sync, compartment, env_id, action)
    logger.info("POV %s: %s %d OCI instances", env_id, action, count)


# ── teardown ─────────────────────────────────────────────────────────────────

def _terminate_instances_sync(compute, compartment: str, env_id: str,
                              names: set | None = None) -> list:
    """Terminate this environment's instances and wait for them to go.

    Waited out, not fired and forgotten: OCI refuses to delete a subnet while a VNIC still
    lives in it, and an instance holds its VNIC well past the terminate call returning.
    """
    import oci

    removed, waiters = [], []
    for inst in _list_instances_sync(compute, compartment):
        if not _of_env(inst, env_id):
            continue
        # Assigned unconditionally. It was set only inside the filter branch and read
        # outside it, which the conditional happened to short-circuit past — a NameError
        # one edit away from being real.
        label = (dict(inst.freeform_tags or {}).get(env.TAG_NAME)
                 or inst.display_name or inst.id)
        if names is not None and label.strip().lower() not in names:
            continue
        compute.terminate_instance(inst.id, preserve_boot_volume=False)
        waiters.append(inst.id)
        removed.append(label)
    for instance_id in waiters:
        try:
            oci.wait_until(compute, compute.get_instance(instance_id),
                           "lifecycle_state", "TERMINATED", max_wait_seconds=900)
        except Exception:  # noqa: BLE001
            logger.warning("POV %s: %s did not report TERMINATED", env_id, instance_id,
                           exc_info=True)
    return removed


def _delete_sync(compartment: str, env_id: str) -> list:
    """Terminate the instances, then unpick the VCN they sat in.

    Ordered and tolerant, like the AWS driver: every step is scoped to the environment
    tag, every step skips what is already gone, and a step that fails is logged and
    stepped over rather than stopping the rest — a teardown that aborts halfway leaves
    exactly the orphan it was called to prevent.
    """
    compute, vnet, _identity = _clients()
    removed = list(_terminate_instances_sync(compute, compartment, env_id))

    def _mine(rows):
        return [r for r in (rows or [])
                if (dict(getattr(r, "freeform_tags", None) or {})
                    .get(env.TAG_ENVIRONMENT)) == env_id]

    vcns = _mine(vnet.list_vcns(compartment_id=compartment).data)

    # Subnet first: a route table and a security list cannot be deleted while a subnet
    # still references them, and the VCN cannot go while any of the three remain.
    for step, rows, delete in (
            ("subnet", _mine(vnet.list_subnets(compartment_id=compartment).data),
             lambda r: vnet.delete_subnet(r.id)),
            ("route table", [r for v in vcns for r in _mine(
                vnet.list_route_tables(compartment_id=compartment, vcn_id=v.id).data)],
             lambda r: vnet.delete_route_table(r.id)),
            ("security list", [r for v in vcns for r in _mine(
                vnet.list_security_lists(compartment_id=compartment, vcn_id=v.id).data)],
             lambda r: vnet.delete_security_list(r.id)),
            ("internet gateway", [r for v in vcns for r in _mine(
                vnet.list_internet_gateways(compartment_id=compartment,
                                            vcn_id=v.id).data)],
             lambda r: vnet.delete_internet_gateway(r.id)),
            ("vcn", vcns, lambda r: vnet.delete_vcn(r.id))):
        for row in rows:
            try:
                delete(row)
                removed.append(row.id)
            except Exception:  # noqa: BLE001
                logger.warning("POV %s: could not delete %s %s", env_id, step, row.id,
                               exc_info=True)
    return removed


async def delete_environment(env_id: str, region: str) -> None:
    compartment = _compartment(env_id)
    removed = await oci_service._to_thread(_delete_sync, compartment, env_id)
    logger.info("POV %s: removed %d OCI resources from %s", env_id, len(removed), region)


async def remove_vms(env_id: str, region: str, names: list) -> list:
    """Terminate named instances in one environment.

    Scoped by BOTH the environment tag and the name. The name alone is not a key — two
    POVs can each have a `broker` — and this is called to make room for a replacement, so
    selecting one environment's instance while claiming to select another's would delete a
    live customer's agent host.
    """
    wanted = {(n or "").strip().lower() for n in names if (n or "").strip()}
    if not wanted:
        return []
    compartment = _compartment(env_id)

    def _remove():
        compute, _vnet, _identity = _clients()
        return _terminate_instances_sync(compute, compartment, env_id, names=wanted)

    removed = await oci_service._to_thread(_remove)
    if removed:
        logger.info("POV %s: replaced %s", env_id, ", ".join(removed))
    return removed
