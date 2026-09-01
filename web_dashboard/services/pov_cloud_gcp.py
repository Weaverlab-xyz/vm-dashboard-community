"""The GCP driver behind ``pov_cloud_env``: Compute Engine calls, and nothing else.

The third cloud, and it sits between the other two. Like AWS it has no native environment
object, so teardown unpicks a handful of resource types by tag rather than deleting one
group; unlike AWS its labels are constrained enough that the shared tag keys cannot be
used verbatim.

Everything policy-shaped — the environment id, which VMs get user-data, what a refusal
says — still lives in ``pov_cloud_env``. This module knows Compute Engine.

Four things here are decisions rather than mechanics:

**GCP label keys are not the dashboard's tag keys.** A GCE label key must match
``[a-z]([-_a-z0-9]*)?`` — no capitals at all — so ``povEnvironment`` is rejected outright,
with an error naming the API field rather than the tag. ``_LABEL_KEY`` maps each shared key
to its GCP spelling, in one place, and the teardown filters on the mapped name.

**The zone is resolved, never assembled.** ``f"{region}-a"`` is wrong in ``us-east1`` and
``europe-west1``, which have no ``-a`` zone, and GCE reports a nonexistent zone as
``403 Permission denied on 'locations/us-east1-a' (or it may not exist)`` — which reads as
a credentials problem and has cost this codebase a debugging session before. The region's
own zone list is one call and is always right.

**A network per POV, and one firewall rule on it.** GCP's default is deny-all ingress and
allow-all egress, so the only rule needed is the environment talking to itself. Nothing
from outside it reaches in: the agent, the Gateway and the Resource Broker all dial out.

**An external address for egress, and no Cloud NAT.** Same arithmetic as the other two
drivers — a NAT gateway is a standing hourly charge before a byte moves. The address is
ephemeral on purpose: nothing here depends on it surviving a stop, and a reserved one
bills while detached.
"""
from __future__ import annotations

import logging

from . import gcp_service, pov_cloud_env as env
from .gcp_service import GCPError

logger = logging.getLogger(__name__)

# Instances are zonal and subnetworks are regional, so a listing is per region like AWS's
# rather than global like Azure's.
LISTS_ALL_REGIONS = False

# The dashboard's tag keys, in the only spelling GCE accepts. A label key must match
# `[a-z]([-_a-z0-9]*)?`: lowercase to start, then lowercase, digits, dashes and
# underscores. `povEnvironment` is refused by the API, and `Name` is not a label here at
# all — a GCE instance carries its name as a first-class field.
_LABEL_KEY = {
    env.TAG_ENVIRONMENT: "pov_environment",
    env.TAG_MANAGED_BY: "pov_managed_by",
    env.TAG_ESTATE: "managed-by",
    env.TAG_ROLE: "pov_role",
}

# GCE instance statuses that mean the instance is gone or going. Excluded from every read,
# for the same reason AWS excludes `terminated`.
_DEAD_STATES = ("terminating",)

# GCE status -> the dashboard's runstate vocabulary. TERMINATED is GCE's word for a stopped
# instance whose disks survive and whose compute is not billed — the state a suspend
# schedule is aiming at, despite the name. Anything unlisted passes through lowercased.
_RUNSTATE = {"running": "running", "terminated": "stopped", "suspended": "stopped"}

# pd-balanced rather than pd-ssd: a POV is a demonstration, not a benchmark, and this is
# the line that keeps billing while the environment is suspended.
_DISK_TYPE = "pd-balanced"

# Cloud-init reads `user-data`; the guest agent reads `startup-script` and re-runs it on
# EVERY boot. The bootstrap carries a single-use enrolment code, so a payload that re-ran
# on each start would spend the rest of the POV's life failing to redeem a spent one.
_USER_DATA_KEY = "user-data"


def _cfg(key: str, fallback: str = "") -> str:
    """Config first, then settings, and never raising.

    `gcp_service._cfg` reads config_service alone and lets its failure out — fine on the
    demo path, where the database is always there by the time anything calls it. These
    two are read from the lab-platform registry, which the UI and the tests exercise
    constantly and sometimes before a database exists at all. Same shape as
    `skytap_service._cfg`, and it also means a POV instance can set either in `.env`.
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


def default_region() -> str:
    return _cfg("gcp_region", "us-central1")


def configured() -> bool:
    from . import feature_flags
    return feature_flags.cloud_configured("gcp")


def configured_project_id() -> str:
    """The project POVs are built in.

    Exposed because the ``projects`` capability is True for GCP alone: a project is a real
    boundary here, `PovEnvironment.project_id` records which one an environment went into,
    and the teardown reads it back rather than re-deriving it. ``expiry_reaper`` puts the
    reason plainly — a destroy aimed at the wrong project is the worst version of this bug.
    """
    return _cfg("gcp_project_id")


def _labels(env_id: str, role: str = "") -> dict:
    """The shared tag map, in GCE's spelling."""
    out = {_LABEL_KEY[k]: v for k, v in env.base_tags(env_id).items() if k in _LABEL_KEY}
    if role:
        out[_LABEL_KEY[env.TAG_ROLE]] = role
    return out


def _label_filter(env_id: str) -> str:
    return f"labels.{_LABEL_KEY[env.TAG_ENVIRONMENT]}={env_id}"


def _project(env_id: str = "") -> str:
    """The project an environment lives in: the one recorded on its row, else the default.

    Recorded first, for the reason `configured_project_id` gives. A POV built before the
    project setting was changed must still be readable and destroyable in the project it
    actually went into.
    """
    if env_id:
        recorded = env.recorded_project(env_id, "gcp")
        if recorded:
            return recorded
    project = configured_project_id()
    if not project:
        raise env.CloudEnvError(
            "no GCP project is configured, so there is nowhere to build a POV. Set it in "
            "Settings → Integrations → POV cloud provider.")
    return project


def _zone_sync(project: str, region: str) -> str:
    """The first zone of ``region``, asked rather than assembled.

    ``f"{region}-a"`` is wrong in us-east1 and europe-west1, which start at ``-b``. GCE
    reports a nonexistent zone as a 403 that reads like a permissions problem, so this
    mistake does not look like itself.
    """
    from google.cloud import compute_v1
    creds = gcp_service._gcp_creds()
    found = compute_v1.RegionsClient(credentials=creds).get(project=project,
                                                            region=region)
    zones = sorted((z.rsplit("/", 1)[-1]) for z in (found.zones or []))
    if not zones:
        raise env.CloudEnvError(
            f"GCP region {region} reports no zones, so there is nowhere to place a VM")
    return zones[0]


def _clients():
    from google.cloud import compute_v1
    creds = gcp_service._gcp_creds()
    return {
        "instances": compute_v1.InstancesClient(credentials=creds),
        "networks": compute_v1.NetworksClient(credentials=creds),
        "subnetworks": compute_v1.SubnetworksClient(credentials=creds),
        "firewalls": compute_v1.FirewallsClient(credentials=creds),
    }


# ── verify ───────────────────────────────────────────────────────────────────

def _verify_sync(project: str, region: str) -> str:
    return _zone_sync(project, region)


async def verify() -> tuple[bool, str]:
    """Prove the credential, the project and the region, in one cheap read.

    An expected failure is a RETURN VALUE, not an exception — a caught exception
    stringified into a response body is what CodeQL's stack-trace-exposure rule fires on.
    """
    if not configured():
        return False, ("No GCP credentials are configured. Add them in "
                       "Settings → Integrations → POV cloud provider.")
    region = default_region()
    try:
        project = _project()
        zone = await gcp_service._to_thread(_verify_sync, project, region)
    except env.CloudEnvError as exc:
        return False, str(exc)
    except GCPError as exc:
        return False, f"GCP refused the call: {exc}"
    except Exception:  # noqa: BLE001
        logger.warning("GCP POV verify failed", exc_info=True)
        return False, ("GCP did not answer. Check the service-account key, that the "
                       f"Compute Engine API is enabled, and that {region} is available "
                       "to this project.")
    return True, f"Connected to GCP project {project} ({region}, first zone {zone})."


# ── naming ───────────────────────────────────────────────────────────────────
#
# **The network layer is selected by NAME, not by label, and that is forced.** GCE labels
# are not universal: an Instance, a Disk and an Address carry them, but a Network, a
# Subnetwork and a Firewall do not — there is no field to set. So the three network
# resources are named deterministically from the environment id, which is itself derived
# from the POV name, and the teardown looks them up by name. Instances keep the labels.

_SUFFIXES = ("-net", "-subnet", "-fw")

# A GCE resource name is capped at 63 characters and must match
# `[a-z]([-a-z0-9]*[a-z0-9])?`. The environment id already satisfies the shape; the length
# is what a long POV name breaks, and it breaks it in the network layer rather than at the
# form. Refused up front with the real number instead.
_GCE_NAME_MAX = 63


def _resource_name(env_id: str, suffix: str) -> str:
    name = f"{env_id}{suffix}"
    if len(name) > _GCE_NAME_MAX:
        budget = _GCE_NAME_MAX - len(env.ENV_ID_PREFIX) - max(len(s) for s in _SUFFIXES)
        raise env.CloudEnvError(
            f"the POV name is too long for GCP: it becomes {name!r}, and a Compute Engine "
            f"resource name is capped at {_GCE_NAME_MAX} characters. Use a name of "
            f"{budget} characters or fewer.")
    return name


# ── create ───────────────────────────────────────────────────────────────────

def _create_network_sync(project: str, env_id: str, region: str, cidr: str,
                         sub_cidr: str) -> dict:
    from google.cloud import compute_v1

    clients = _clients()
    net_name = _resource_name(env_id, "-net")
    sub_name = _resource_name(env_id, "-subnet")
    fw_name = _resource_name(env_id, "-fw")

    clients["networks"].insert(
        project=project,
        network_resource=compute_v1.Network(
            name=net_name,
            description=f"POV environment {env_id}",
            # No auto subnets: one per POV, in the POV's region, so the subnetwork and the
            # instances' zone can never end up in different regions — GCE rejects that at
            # insert time with "Scope of the specified subnetwork doesn't match the scope
            # of the instance", naming neither region.
            auto_create_subnetworks=False)).result()
    # Read back, rather than taken from the insert. A GCE long-running operation resolves
    # to None, so anything treating `.result()` as the created resource hands the next
    # call a null self-link.
    network = clients["networks"].get(project=project, network=net_name)

    clients["subnetworks"].insert(
        project=project, region=region,
        subnetwork_resource=compute_v1.Subnetwork(
            name=sub_name, ip_cidr_range=sub_cidr, network=network.self_link,
            region=region, description=f"POV environment {env_id}")).result()
    subnet = clients["subnetworks"].get(project=project, region=region,
                                        subnetwork=sub_name)

    # The ONE rule. GCP's default is deny-all ingress and allow-all egress, so the only
    # thing to add is the environment talking to itself — the broker reaching its targets
    # over SSH and WinRM. Nothing from outside the POV ever initiates a connection in.
    clients["firewalls"].insert(
        project=project,
        firewall_resource=compute_v1.Firewall(
            name=fw_name, network=network.self_link, direction="INGRESS",
            description=f"POV environment {env_id} — intra-environment only",
            source_ranges=[sub_cidr],
            allowed=[compute_v1.Allowed(I_p_protocol="all")])).result()

    return {"project": project, "region": region, "network": network.self_link,
            "subnet_id": subnet.self_link, "subnet_cidr": sub_cidr,
            "network_name": net_name, "subnet_name": sub_name, "firewall_name": fw_name}


def _create_vms_sync(project: str, env_id: str, region: str, specs: list,
                     net: dict) -> list:
    from google.cloud import compute_v1

    instances = _clients()["instances"]
    zone = _zone_sync(project, region)
    created = []

    for spec in specs:
        name = spec["name"]
        role = spec.get("role") or "target"
        metadata_items = []
        if spec.get("user_data"):
            # `user-data`, read by cloud-init on FIRST boot. Deliberately not
            # `startup-script`, which the guest agent re-runs on EVERY boot: the payload
            # carries a single-use enrolment code, so a re-run would spend the rest of the
            # POV's life failing to redeem a spent one.
            metadata_items.append(
                compute_v1.Items(key=_USER_DATA_KEY, value=spec["user_data"]))

        instance = compute_v1.Instance(
            name=name,
            machine_type=f"zones/{zone}/machineTypes/{spec['instance_type']}",
            labels=_labels(env_id, role),
            disks=[compute_v1.AttachedDisk(
                boot=True, auto_delete=True,
                initialize_params=compute_v1.AttachedDiskInitializeParams(
                    source_image=spec["image_id"],
                    disk_size_gb=int(spec.get("disk_gb") or env.DEFAULT_DISK_GB),
                    disk_type=f"zones/{zone}/diskTypes/{_DISK_TYPE}",
                    labels=_labels(env_id, role)))],
            network_interfaces=[compute_v1.NetworkInterface(
                subnetwork=net["subnet_id"],
                # An ephemeral external address, for egress only — no Cloud NAT. Ephemeral
                # rather than reserved because nothing here depends on it surviving a
                # stop, and a reserved address bills while detached.
                access_configs=[compute_v1.AccessConfig(
                    name="External NAT", type_="ONE_TO_ONE_NAT")])],
            metadata=compute_v1.Metadata(items=metadata_items) if metadata_items else None,
        )
        instances.insert(project=project, zone=zone, instance_resource=instance).result()
        created.append({"id": name, "name": name})
        logger.info("POV %s: created GCE instance %s in %s", env_id, name, zone)
    return created


async def create_network(env_id: str, region: str, cidr: str, sub_cidr: str) -> dict:
    return await gcp_service._to_thread(
        _create_network_sync, _project(env_id), env_id, region, cidr, sub_cidr)


async def create_vms(env_id: str, region: str, specs: list, net: dict) -> list:
    return await gcp_service._to_thread(
        _create_vms_sync, net.get("project") or _project(env_id), env_id, region, specs,
        net)


# ── read ─────────────────────────────────────────────────────────────────────

def _vm(inst) -> dict:
    labels = dict(inst.labels or {})
    status = (inst.status or "").lower()
    private_ip = ""
    for nic in inst.network_interfaces or []:
        if nic.network_i_p:
            private_ip = nic.network_i_p
            break
    disk_gb = sum(int(d.disk_size_gb or 0) for d in (inst.disks or []))
    return {
        "id": inst.name,
        "name": inst.name,
        # GCE reports the guest OS only through the image the disk came from, and a
        # licence list is a guess dressed as a fact. The template already knows, so the
        # POV row keeps what it was built with rather than this overwriting it with "".
        "os_family": "windows" if any(
            "windows" in (lic or "").lower()
            for d in (inst.disks or []) for lic in (d.licenses or [])) else "linux",
        "runstate": _RUNSTATE.get(status, status),
        "private_ip": private_ip,
        "role": labels.get(_LABEL_KEY[env.TAG_ROLE]) or "target",
        "instance_type": (inst.machine_type or "").rsplit("/", 1)[-1],
        "disk_gb": disk_gb,
        "launched_at": inst.creation_timestamp or "",
        # No NAT-ed guest ports on a cloud: access is PRA, through the POV's own Gateway.
        "published_services": [],
    }


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


def _instances_sync(project: str, filter_expr: str) -> list:
    """Every live instance matching a label filter, across the whole project.

    ``aggregated_list`` rather than a per-zone walk: instances are zonal, the POV's zone
    is resolved rather than remembered, and asking every zone by hand would be dozens of
    calls to find at most a handful.
    """
    instances = _clients()["instances"]
    out = []
    for _scope, scoped in instances.aggregated_list(project=project,
                                                    filter=filter_expr):
        for inst in (scoped.instances or []):
            if (inst.status or "").lower() in _DEAD_STATES:
                continue
            out.append(inst)
    return out


def _read_environment_sync(project: str, env_id: str, region: str):
    found = _instances_sync(project, _label_filter(env_id))
    if found:
        return _environment(env_id, [_vm(i) for i in found], region)
    # No instances is not the same as gone. A teardown that deleted the VMs and then
    # failed on the network leaves a real environment, and answering None would let
    # `pov_reconcile` mark the POV missing while its network still exists.
    from google.api_core.exceptions import NotFound
    try:
        _clients()["networks"].get(project=project,
                                   network=_resource_name(env_id, "-net"))
    except NotFound:
        return None
    return _environment(env_id, [], region)


async def read_environment(env_id: str, region: str):
    return await gcp_service._to_thread(_read_environment_sync, _project(env_id), env_id,
                                        region)


def _list_environments_sync(project: str, region: str) -> list:
    managed = f"labels.{_LABEL_KEY[env.TAG_MANAGED_BY]}={env.MANAGED_BY}"
    grouped: dict = {}
    for inst in _instances_sync(project, managed):
        env_id = dict(inst.labels or {}).get(_LABEL_KEY[env.TAG_ENVIRONMENT]) or ""
        if env_id:
            grouped.setdefault(env_id, []).append(inst)

    # Networks carry no labels, so an environment whose instances are gone is found by
    # NAME — the suffix this driver appends to the environment id. Same orphan the AWS
    # driver looks for in its surviving VPCs, and the reason /pov/cloud exists.
    for network in _clients()["networks"].list(project=project):
        name = network.name or ""
        if name.startswith(env.ENV_ID_PREFIX) and name.endswith("-net"):
            grouped.setdefault(name[:-len("-net")], [])

    return [_environment(eid, [_vm(i) for i in rows], region)
            for eid, rows in sorted(grouped.items())]


async def list_environments(region: str) -> list:
    return await gcp_service._to_thread(_list_environments_sync, _project(), region)


async def read_network(env_id: str, region: str) -> dict:
    """The subnet an environment was built with, read back off its name."""
    project = _project(env_id)

    def _read():
        from google.api_core.exceptions import NotFound
        try:
            subnet = _clients()["subnetworks"].get(
                project=project, region=region,
                subnetwork=_resource_name(env_id, "-subnet"))
        except NotFound:
            return {}
        return {"project": project, "region": region, "subnet_id": subnet.self_link,
                "subnet_cidr": subnet.ip_cidr_range or "",
                "network": subnet.network or ""}

    return await gcp_service._to_thread(_read)


# ── power ────────────────────────────────────────────────────────────────────

def _power_sync(project: str, env_id: str, action: str) -> int:
    instances = _clients()["instances"]
    found = _instances_sync(project, _label_filter(env_id))
    ops = []
    for inst in found:
        zone = (inst.zone or "").rsplit("/", 1)[-1]
        if action == "start":
            ops.append(instances.start(project=project, zone=zone, instance=inst.name))
        else:
            # `stop`, not `suspend`. GCE's suspend preserves RAM to disk and CHARGES for
            # that storage plus the reserved resources; stop lands on TERMINATED, where
            # only the disks bill. TERMINATED is the state a suspend schedule is aiming
            # at, despite what the word suggests.
            ops.append(instances.stop(project=project, zone=zone, instance=inst.name))
    for op in ops:
        # Issued together, waited together: a handful of instances stopping in series is
        # a multiple of the wall clock for no reason, and the job holds a worker slot.
        op.result()
    return len(found)


async def power(env_id: str, region: str, action: str) -> None:
    project = _project(env_id)
    count = await gcp_service._to_thread(_power_sync, project, env_id, action)
    logger.info("POV %s: %s %d GCE instances", env_id, action, count)


# ── teardown ─────────────────────────────────────────────────────────────────

def _delete_sync(project: str, env_id: str, region: str) -> list:
    """Delete the instances, then the network they sat on.

    Ordered and tolerant, like the AWS driver and for the same reason: every step skips
    what is already gone, and a step that fails is logged and stepped over rather than
    stopping the rest — a teardown that aborts halfway leaves exactly the orphan it was
    called to prevent. The caller runs this again on the next attempt.
    """
    from google.api_core.exceptions import NotFound

    clients = _clients()
    removed = []

    ops = []
    for inst in _instances_sync(project, _label_filter(env_id)):
        zone = (inst.zone or "").rsplit("/", 1)[-1]
        ops.append((inst.name, clients["instances"].delete(
            project=project, zone=zone, instance=inst.name)))
    for name, op in ops:
        try:
            # Waited out, not fired and forgotten: the subnetwork cannot be deleted while
            # an instance still holds an address in it.
            op.result()
            removed.append(name)
        except Exception:  # noqa: BLE001
            logger.warning("POV %s: could not delete instance %s", env_id, name,
                           exc_info=True)

    # Named rather than labelled, because a Firewall, a Subnetwork and a Network carry no
    # labels — see the naming block above.
    for what, delete in (
            ("-fw", lambda n: clients["firewalls"].delete(project=project, firewall=n)),
            ("-subnet", lambda n: clients["subnetworks"].delete(
                project=project, region=region, subnetwork=n)),
            ("-net", lambda n: clients["networks"].delete(project=project, network=n))):
        try:
            name = _resource_name(env_id, what)
        except env.CloudEnvError:
            continue
        try:
            delete(name).result()
            removed.append(name)
        except NotFound:
            pass
        except Exception:  # noqa: BLE001
            logger.warning("POV %s: could not delete %s", env_id, name, exc_info=True)

    return removed


async def delete_environment(env_id: str, region: str) -> None:
    project = _project(env_id)
    removed = await gcp_service._to_thread(_delete_sync, project, env_id, region)
    logger.info("POV %s: removed %d GCP resources from %s/%s", env_id, len(removed),
                project, region)


def _remove_vms_sync(project: str, env_id: str, names: list) -> list:
    """Delete named instances in one environment.

    Scoped by BOTH the environment label and the name. The name alone is not a key — two
    POVs can each have a `broker` — and this is called to make room for a replacement, so
    selecting one environment's instance while claiming to select another's would delete a
    live customer's agent host.
    """
    clients = _clients()
    wanted = {(n or "").strip().lower() for n in names if (n or "").strip()}
    if not wanted:
        return []
    ops, removed = [], []
    for inst in _instances_sync(project, _label_filter(env_id)):
        if inst.name.strip().lower() not in wanted:
            continue
        zone = (inst.zone or "").rsplit("/", 1)[-1]
        ops.append((inst.name, clients["instances"].delete(
            project=project, zone=zone, instance=inst.name)))
    for name, op in ops:
        op.result()
        removed.append(name)
    return removed


async def remove_vms(env_id: str, region: str, names: list) -> list:
    removed = await gcp_service._to_thread(_remove_vms_sync, _project(env_id), env_id,
                                           names)
    if removed:
        logger.info("POV %s: replaced %s", env_id, ", ".join(removed))
    return removed
