"""The Azure driver behind ``pov_cloud_env``: ARM calls, and nothing else.

The second cloud, and the one that reads most naturally — because Azure has the primitive
AWS lacks. **A resource group IS an environment.** One per POV, named for it, and
`delete_environment` is a single call that takes the VMs, their disks, NICs, public
addresses, the VNet and the NSG with it. Compare `pov_cloud_aws._delete_sync`, which has
to unpick six resource types in dependency order and tolerate each one failing.

Everything policy-shaped — the environment id, the tag names, which VMs get user-data —
still lives in ``pov_cloud_env``. This module knows ARM.

Four decisions that are not mechanics:

**No NAT gateway, a public address per VM.** Same arithmetic as the AWS driver: a NAT
gateway is a standing monthly charge before a byte moves, on a community user's own bill.
It is also no longer optional to choose *something* — Azure retired default outbound
access in September 2025, so a VM with no public IP, NAT or load balancer has no egress at
all, and the agent would never reach the dashboard.

**The NSG carries no custom rules, deliberately.** Azure's default rules already are the
policy this feature wants: `AllowVnetInBound` lets the environment talk to itself,
`DenyAllInBound` refuses everything from outside it, and `AllowInternetOutBound` gives the
agent, the Gateway and the Resource Broker their outbound. Adding rules that restate the
defaults would be three more things to keep correct and no change in behaviour.

**Private IPs are STATIC.** This is the one cloud where it matters. A deallocated Azure VM
with a dynamic private address can come back on a different one — and the POV wire-up
writes each VM's private address into a PRA jump item, a Password Safe managed system and
an Entitle integration. Every scheduled suspend would otherwise quietly invalidate the lot.
AWS, GCP and OCI all preserve the private address across stop/start, so they need nothing.

**Deallocate, never power off.** `begin_power_off` leaves the VM "Stopped" and still
billing for compute — the single most expensive mistake available on this API, and exactly
the one a suspend schedule would make on every POV every night.
"""
from __future__ import annotations

import base64
import ipaddress
import itertools
import logging
import secrets
import string

from . import azure_service, pov_cloud_env as env
from .azure_service import AzureError

logger = logging.getLogger(__name__)

# The RG listing is subscription-wide, not per-region, unlike EC2's DescribeInstances. The
# shared lister reads this and asks once instead of once per known region — which also
# makes the orphan sweep complete, because it finds an environment in a region no POV row
# happens to name.
LISTS_ALL_REGIONS = True

# Azure refuses a set of admin usernames outright (`admin`, `administrator`, `root`, …)
# and refuses any password containing the username. `povadmin` is on neither list and says
# what it is.
ADMIN_USERNAME = "povadmin"

# Where the generated platform login lives. Same `<feature>/<id>/<name>` shape as
# `pov/<id>/share_password`, so it is encrypted at rest, resolvable from an external vault
# by reference and carried by the config-migration tool — all for free.
#
# Keyed on the PLATFORM environment id rather than the POV row id, unlike its siblings:
# an adapter is handed the platform's id and nothing else, and looking the row up to key a
# secret would make a credential read depend on a database join it has no other need for.
_PW_FMT = "pov/{env_id}/platform_password"

# Provisioning states that mean the VM is gone or going. Excluded from every read for the
# same reason AWS excludes `terminated`: a deleting VM still answers a list call.
_DEAD_STATES = ("deleting",)

# Azure's power state strings arrive as `PowerState/running`. Mapped to the dashboard's
# vocabulary; anything unlisted passes through as itself.
_RUNSTATE = {"running": "running", "deallocated": "stopped", "stopped": "stopped"}

# StandardSSD rather than Premium: a POV is a demonstration, not a benchmark, and Premium
# roughly triples the line that keeps billing while the environment is suspended.
_DISK_SKU = "StandardSSD_LRS"

# Windows computer names are capped at 15 characters by NetBIOS, and ARM rejects a longer
# one rather than truncating. The VM RESOURCE keeps the template's full name.
_WINDOWS_COMPUTER_NAME_MAX = 15


def default_region() -> str:
    from . import config_service
    from ..config import settings
    return (config_service.get("azure_location")
            or getattr(settings, "azure_location", "") or "eastus")


def configured() -> bool:
    from . import feature_flags
    return feature_flags.cloud_configured("azure")


def password_config_key(env_id: str) -> str:
    return _PW_FMT.format(env_id=env_id)


def _tags(env_id: str, name: str = "", role: str = "") -> dict:
    """ARM tags are a flat dict, so the shared tag map goes in as-is."""
    tags = dict(env.base_tags(env_id))
    if name:
        tags[env.TAG_NAME] = name
    if role:
        tags[env.TAG_ROLE] = role
    return tags


# Azure rejects an admin password shorter than 12 or longer than 123, and one that does
# not draw from at least THREE of: lowercase, uppercase, digit, symbol. 24 is comfortably
# inside the range and long enough that the class rule is the only thing shaping it.
_PW_LENGTH = 24

# Symbols are deliberately absent, so three classes means lower + upper + digit. The
# password travels through an Ansible run and a WinRM session on its way to the Resource
# Broker install, and a symbol is one quoting bug away from an authentication failure that
# reads as a wrong password. Il1O0 are absent too: this is read aloud sometimes.
_PW_LOWER = [c for c in string.ascii_lowercase if c not in "l"]
_PW_UPPER = [c for c in string.ascii_uppercase if c not in "IO"]
_PW_DIGITS = [c for c in string.digits if c not in "10"]
_PW_POOL = _PW_LOWER + _PW_UPPER + _PW_DIGITS


def _generate_admin_password() -> str:
    """A password Azure will accept, by construction rather than by luck.

    **Not ``pov_share.generate_password``, and the difference is the whole point.** That
    one draws 24 characters uniformly from one pool, which is right for a link password a
    human reads out — but roughly one draw in twenty happens to contain no digit, and
    Azure refuses it at VM creation. The failure would land in a provision job, on about
    5% of Azure POVs, with an operator who cannot see the password to know why.

    So one character of each required class is placed first and the rest filled from the
    pool, then the whole thing is shuffled with ``SystemRandom`` so the guaranteed three
    are not always in front.
    """
    chars = [secrets.choice(_PW_LOWER), secrets.choice(_PW_UPPER),
             secrets.choice(_PW_DIGITS)]
    chars += [secrets.choice(_PW_POOL) for _ in range(_PW_LENGTH - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _platform_password(env_id: str) -> str:
    """This environment's admin password, generated once and remembered.

    Azure has no equivalent of launching an AMI with no key: ``os_profile`` requires an
    admin username and a credential, for Linux as well as Windows. So one is minted at
    build time — which incidentally gives an Azure POV a real platform login, and is why
    its ``stored_credentials`` capability is True where AWS's is False.

    See :func:`_generate_admin_password` for why this does not reuse
    ``pov_share.generate_password``.
    """
    from . import config_service
    key = password_config_key(env_id)
    existing = config_service.get_opt(key)
    if existing:
        return existing
    password = _generate_admin_password()
    config_service.set(key, password)
    return password


def _forget_password(env_id: str) -> None:
    """Drop the stored login when its environment goes.

    A password for VMs that no longer exist is a secret with no purpose, and the row it
    belongs to is about to stop being able to name it.
    """
    from . import config_service
    try:
        config_service.delete(password_config_key(env_id))
    except Exception:  # noqa: BLE001 - never fail a teardown over housekeeping
        logger.warning("POV %s: could not clear the stored platform password", env_id,
                       exc_info=True)


def _image_reference(image_id: str):
    """An ARM image reference from whichever of the two shapes the template carries.

    A resource id (managed image, or a gallery image VERSION) starts with a slash. A
    marketplace URN is ``publisher:offer:sku:version``. Anything else is refused by name
    rather than passed to ARM, whose error for a malformed reference names neither.
    """
    from azure.mgmt.compute.models import ImageReference
    value = (image_id or "").strip()
    if value.startswith("/"):
        return ImageReference(id=value)
    parts = value.split(":")
    if len(parts) == 4 and all(parts):
        return ImageReference(publisher=parts[0], offer=parts[1], sku=parts[2],
                              version=parts[3])
    raise env.CloudEnvError(
        f"{image_id!r} is not an Azure image. Use a marketplace URN "
        f"(publisher:offer:sku:version) or the resource id of a managed or gallery image.")


async def _clients():
    """``(resource, compute, network, subscription_id)``.

    Credentials come from ``azure_service._ensure_creds``, which is the one place this
    codebase resolves them — Workload Credentials lease, then config, then env, then
    Password Safe — and which caches on the credential MATERIAL so a rotated secret is
    picked up without a restart.
    """
    azure_service._require_azure()
    cred, sub_id = await azure_service._ensure_creds()
    if not sub_id:
        raise env.CloudEnvError(
            "no Azure subscription id is configured, so there is nowhere to build a POV. "
            "Set it in Settings → Integrations → POV cloud provider.")
    return (azure_service._get_resource(cred, sub_id),
            azure_service._get_compute(cred, sub_id),
            azure_service._get_network(cred, sub_id), sub_id)


# ── verify ───────────────────────────────────────────────────────────────────

async def verify() -> tuple[bool, str]:
    """Prove the credential and the subscription, without a side effect.

    An expected failure is a RETURN VALUE, not an exception — a caught exception
    stringified into a response body is what CodeQL's stack-trace-exposure rule fires on,
    and the fix for that is structural rather than a wider except.
    """
    if not configured():
        return False, ("No Azure credentials are configured. Add them in "
                       "Settings → Integrations → POV cloud provider.")
    region = default_region()
    try:
        resource, _compute, _network, sub_id = await _clients()
        # A bounded read that proves the credential AND what the subscription can see.
        # Sliced rather than exhausted: the pager would walk every resource group in a
        # busy subscription to answer a question the first page settles.
        await azure_service._to_thread(
            lambda: list(itertools.islice(resource.resource_groups.list(), 1)))
    except AzureError as exc:
        return False, f"Azure refused the call: {exc}"
    except Exception:  # noqa: BLE001
        logger.warning("Azure POV verify failed", exc_info=True)
        return False, ("Azure did not answer. Check the client id, secret, tenant and "
                       "subscription, and that the service principal has Contributor on "
                       "the subscription.")
    return True, f"Connected to Azure subscription {sub_id} ({region})."


def _custom_data(payload: str):
    """Cloud-init for an Azure guest, base64 as ARM requires.

    The SDK passes ``custom_data`` through untouched — it is documented as a base-64
    string and nothing in the client encodes it for you. Sending plain text produces a VM
    that boots fine and simply never runs the bootstrap, which is the same silent failure
    `cloud_init` earns its own capability value to avoid.
    """
    if not payload:
        return None
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def _next_free_ip(network, net: dict, rg: str) -> str:
    """The lowest unused address in this POV's subnet.

    Needed because the private addresses are STATIC — see the module docstring — so
    somebody has to choose them. Read from the NICs that already exist rather than counted
    from a loop index, because the broker VM is created in a later pass than the targets
    and an index would restart.

    **A failed read raises rather than falling back to dynamic.** Guessing here would hand
    two VMs the same address, and ARM reports that as a create failure on the second one
    with no hint that the first is why.
    """
    used = set()
    try:
        for nic in network.network_interfaces.list(rg):
            for cfg in nic.ip_configurations or []:
                if cfg.private_ip_address:
                    used.add(cfg.private_ip_address)
    except Exception as exc:  # noqa: BLE001
        raise env.CloudEnvError(
            f"could not read the addresses already in use in {rg}, so a new VM cannot be "
            f"given one that will not collide: {exc}") from None

    subnet = ipaddress.ip_network(net["subnet_cidr"], strict=False)
    for index, addr in enumerate(subnet.hosts()):
        # Azure reserves the first four addresses of every subnet — network, gateway and
        # two for its own DNS — and the last. `hosts()` has already dropped the network
        # and broadcast, so three more go.
        if index < 3:
            continue
        if str(addr) not in used:
            return str(addr)
    raise env.CloudEnvError(
        f"{net['subnet_cidr']} has no free addresses left for another VM in this POV")


# ── create ───────────────────────────────────────────────────────────────────

def _create_network_sync(resource, network, env_id: str, region: str, cidr: str,
                         sub_cidr: str) -> dict:
    tags = _tags(env_id, env_id)

    # The resource group IS the environment. Named for the POV, so the teardown is one
    # call and an operator looking at the Azure portal sees the POV's name rather than a
    # tag they have to go and read.
    resource.resource_groups.create_or_update(env_id, {"location": region, "tags": tags})

    nsg = network.network_security_groups.begin_create_or_update(
        env_id, f"{env_id}-nsg",
        # No security_rules: the platform defaults ARE the policy. See the module
        # docstring — restating them would be three more things to keep correct.
        {"location": region, "tags": tags}).result()

    vnet = network.virtual_networks.begin_create_or_update(
        env_id, f"{env_id}-vnet", {
            "location": region,
            "tags": tags,
            "address_space": {"address_prefixes": [cidr]},
            "subnets": [{
                "name": "pov",
                "address_prefix": sub_cidr,
                "network_security_group": {"id": nsg.id},
            }],
        }).result()

    return {"resource_group": env_id, "location": region,
            "subnet_id": vnet.subnets[0].id, "subnet_cidr": sub_cidr,
            "nsg_id": nsg.id, "vnet_id": vnet.id}


def _create_vms_sync(compute, network, env_id: str, region: str, specs: list,
                     net: dict) -> list:
    from azure.mgmt.compute.models import (
        DiskCreateOptionTypes, HardwareProfile, LinuxConfiguration, NetworkProfile,
        NetworkInterfaceReference, OSDisk, OSProfile, StorageProfile, VirtualMachine,
        WindowsConfiguration)

    rg = net["resource_group"]
    # The GROUP's location wins over the caller's. They agree on a build — the provision
    # job records the region off the read-back — but `create_broker_vm` runs later and
    # takes its region from the POV row, and a row edited or restored out of step would
    # otherwise put the broker VM in a different region from its own subnet, which ARM
    # rejects with an error naming neither.
    region = net.get("location") or region
    password = _platform_password(env_id)
    created = []

    for spec in specs:
        name = spec["name"]
        role = spec.get("role") or "target"
        tags = _tags(env_id, name, role)
        windows = spec.get("os_family") == "windows"

        pip = network.public_ip_addresses.begin_create_or_update(
            rg, f"{name}-pip", {
                "location": region,
                "tags": tags,
                # Standard SKU is the only one left — Basic retired in September 2025 —
                # and a Standard address must be statically allocated. That is also what
                # keeps a suspended POV's address across a deallocate.
                "sku": {"name": "Standard"},
                "public_ip_allocation_method": "Static",
            }).result()

        nic = network.network_interfaces.begin_create_or_update(
            rg, f"{name}-nic", {
                "location": region,
                "tags": tags,
                "ip_configurations": [{
                    "name": "ipconfig1",
                    "subnet": {"id": net["subnet_id"]},
                    # STATIC, and this is the line that matters. A deallocated VM with a
                    # dynamic private address can come back on a different one, and the
                    # POV wire-up has written the old one into a PRA jump item, a Password
                    # Safe managed system and an Entitle integration. Every scheduled
                    # suspend would silently invalidate all three.
                    "private_ip_allocation_method": "Static",
                    "private_ip_address": _next_free_ip(network, net, rg),
                    "public_ip_address": {"id": pip.id},
                }],
            }).result()

        if windows:
            os_profile = OSProfile(
                computer_name=name[:_WINDOWS_COMPUTER_NAME_MAX],
                admin_username=ADMIN_USERNAME,
                admin_password=password,
                windows_configuration=WindowsConfiguration(provision_vm_agent=True,
                                                           enable_automatic_updates=True))
        else:
            os_profile = OSProfile(
                computer_name=name,
                admin_username=ADMIN_USERNAME,
                admin_password=password,
                # Password auth on purpose, rather than an SSH key. It is the same
                # credential Windows must use, so one login covers the whole environment
                # and `stored_credentials` has something real to return — and nothing
                # outside the VNet can reach port 22 anyway, because the NSG denies it.
                linux_configuration=LinuxConfiguration(
                    disable_password_authentication=False),
                custom_data=_custom_data(spec.get("user_data") or ""))

        vm = compute.virtual_machines.begin_create_or_update(rg, name, VirtualMachine(
            location=region,
            tags=tags,
            hardware_profile=HardwareProfile(vm_size=spec["instance_type"]),
            storage_profile=StorageProfile(
                image_reference=_image_reference(spec["image_id"]),
                os_disk=OSDisk(
                    create_option=DiskCreateOptionTypes.FROM_IMAGE,
                    disk_size_gb=int(spec.get("disk_gb") or env.DEFAULT_DISK_GB),
                    managed_disk={"storage_account_type": _DISK_SKU})),
            os_profile=os_profile,
            network_profile=NetworkProfile(
                network_interfaces=[NetworkInterfaceReference(id=nic.id, primary=True)]),
        )).result()
        created.append({"id": vm.name, "name": name})
        logger.info("POV %s: created Azure VM %s", env_id, name)
    return created


async def create_network(env_id: str, region: str, cidr: str, sub_cidr: str) -> dict:
    resource, _compute, network, _sub = await _clients()
    return await azure_service._to_thread(
        _create_network_sync, resource, network, env_id, region, cidr, sub_cidr)


async def create_vms(env_id: str, region: str, specs: list, net: dict) -> list:
    _resource, compute, network, _sub = await _clients()
    return await azure_service._to_thread(
        _create_vms_sync, compute, network, env_id, region, specs, net)


# ── read ─────────────────────────────────────────────────────────────────────

def _power_state(instance_view) -> str:
    """`PowerState/running` -> `running`, mapped into the dashboard's vocabulary."""
    for status in getattr(instance_view, "statuses", None) or []:
        code = getattr(status, "code", "") or ""
        if code.startswith("PowerState/"):
            raw = code.split("/", 1)[1]
            return _RUNSTATE.get(raw, raw)
    return ""


def _read_environment_sync(resource, compute, network, env_id: str) -> dict | None:
    from azure.core.exceptions import ResourceNotFoundError
    try:
        group = resource.resource_groups.get(env_id)
    except ResourceNotFoundError:
        return None

    # One listing each rather than a get per VM: a five-VM POV is three calls this way and
    # eleven the other, and this runs on every reconcile pass for every environment.
    nics = {n.id: n for n in network.network_interfaces.list(env_id)}
    disk_gb: dict = {}
    for disk in compute.disks.list_by_resource_group(env_id):
        owner = (getattr(disk, "managed_by", "") or "").rsplit("/", 1)[-1]
        if owner:
            disk_gb[owner] = disk_gb.get(owner, 0) + int(disk.disk_size_gb or 0)

    vms = []
    for vm in compute.virtual_machines.list(env_id):
        if (getattr(vm, "provisioning_state", "") or "").lower() in _DEAD_STATES:
            continue
        tags = dict(vm.tags or {})
        private_ip = ""
        for ref in (vm.network_profile.network_interfaces if vm.network_profile else []):
            nic = nics.get(ref.id)
            for cfg in (nic.ip_configurations if nic else None) or []:
                if cfg.private_ip_address:
                    private_ip = cfg.private_ip_address
                    break
            if private_ip:
                break
        view = compute.virtual_machines.instance_view(env_id, vm.name)
        os_type = str(getattr(getattr(vm, "storage_profile", None), "os_disk", None)
                      and vm.storage_profile.os_disk.os_type or "").lower()
        vms.append({
            "id": vm.name,
            "name": tags.get(env.TAG_NAME) or vm.name,
            # Azure always reports the OS type, so unlike Skytap there is no case where
            # the honest answer is blank.
            "os_family": "windows" if "windows" in os_type else "linux",
            "runstate": _power_state(view),
            "private_ip": private_ip,
            "role": tags.get(env.TAG_ROLE) or "target",
            "instance_type": (vm.hardware_profile.vm_size
                              if vm.hardware_profile else ""),
            "disk_gb": disk_gb.get(vm.name, 0),
            "launched_at": "",
            # No NAT-ed guest ports on a cloud: access is PRA, through the POV's Gateway.
            "published_services": [],
        })
    return _environment(env_id, vms, group.location)


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


async def read_environment(env_id: str, region: str):
    """The environment, or ``None`` when its resource group is gone.

    A resource group with no VMs left is NOT gone — that is a teardown which removed the
    machines and then failed, and answering None would let `pov_reconcile` mark the POV
    missing while its network still exists. Same rule the AWS driver applies to a
    surviving VPC.
    """
    resource, compute, network, _sub = await _clients()
    return await azure_service._to_thread(
        _read_environment_sync, resource, compute, network, env_id)


def _list_environments_sync(resource, compute, network) -> list:
    out = []
    tag = env.TAG_MANAGED_BY
    for group in resource.resource_groups.list():
        tags = dict(group.tags or {})
        if tags.get(tag) != env.MANAGED_BY:
            continue
        found = _read_environment_sync(resource, compute, network, group.name)
        if found is not None:
            out.append(found)
    return out


async def list_environments(region: str = "") -> list:
    """Every POV environment in the subscription.

    ``region`` is accepted and ignored: resource groups list subscription-wide, which is
    why this driver sets ``LISTS_ALL_REGIONS`` and the shared lister asks once. That also
    makes the orphan sweep complete, because it finds an environment in a region no POV
    row happens to name.
    """
    resource, compute, network, _sub = await _clients()
    return await azure_service._to_thread(_list_environments_sync, resource, compute,
                                          network)


async def read_network(env_id: str, region: str) -> dict:
    """The subnet an environment was built with, read back off its resource group."""
    _resource, _compute, network, _sub = await _clients()

    def _read():
        for vnet in network.virtual_networks.list(env_id):
            for subnet in vnet.subnets or []:
                return {"resource_group": env_id, "location": vnet.location,
                        "subnet_id": subnet.id,
                        "subnet_cidr": subnet.address_prefix or "",
                        "vnet_id": vnet.id}
        return {}

    return await azure_service._to_thread(_read)


# ── power ────────────────────────────────────────────────────────────────────

def _power_sync(compute, env_id: str, action: str) -> int:
    names = [vm.name for vm in compute.virtual_machines.list(env_id)]
    pollers = []
    for name in names:
        if action == "start":
            pollers.append(compute.virtual_machines.begin_start(env_id, name))
        else:
            # DEALLOCATE, never begin_power_off. Power-off leaves the VM "Stopped" and
            # still billing for compute — which a nightly suspend schedule would do to
            # every POV, every night, while the page reported it as suspended.
            pollers.append(compute.virtual_machines.begin_deallocate(env_id, name))
    for poller in pollers:
        # Started together, waited together: five VMs deallocating in series is five times
        # the wall clock for no reason, and the job holds a worker slot throughout.
        poller.result()
    return len(names)


async def power(env_id: str, region: str, action: str) -> None:
    _resource, compute, _network, _sub = await _clients()
    count = await azure_service._to_thread(_power_sync, compute, env_id, action)
    logger.info("POV %s: %s %d Azure VMs", env_id, action, count)


# ── teardown ─────────────────────────────────────────────────────────────────

def _remove_vms_sync(compute, network, env_id: str, names: list) -> list:
    """Delete named VMs and the NIC and public address each one owns.

    Scoped by resource group AND name. The group is already one POV's, so the name cannot
    reach another environment here the way it could on AWS — but the NIC and public IP are
    separate resources that deleting a VM does not take with it, and leaving them behind
    would hold the static private address the replacement is about to ask for.
    """
    from azure.core.exceptions import ResourceNotFoundError
    wanted = {(n or "").strip().lower() for n in names if (n or "").strip()}
    if not wanted:
        return []
    removed = []
    for vm in list(compute.virtual_machines.list(env_id)):
        if vm.name.strip().lower() not in wanted:
            continue
        compute.virtual_machines.begin_delete(env_id, vm.name).result()
        removed.append(vm.name)
        for suffix, client in (("-nic", network.network_interfaces),
                               ("-pip", network.public_ip_addresses)):
            try:
                client.begin_delete(env_id, f"{vm.name}{suffix}").result()
            except ResourceNotFoundError:
                pass
            except Exception:  # noqa: BLE001
                logger.warning("POV %s: could not delete %s%s", env_id, vm.name, suffix,
                               exc_info=True)
    return removed


async def remove_vms(env_id: str, region: str, names: list) -> list:
    _resource, compute, network, _sub = await _clients()
    removed = await azure_service._to_thread(_remove_vms_sync, compute, network, env_id,
                                             names)
    if removed:
        logger.info("POV %s: replaced %s", env_id, ", ".join(removed))
    return removed


async def delete_environment(env_id: str, region: str) -> None:
    """Delete the resource group, and everything in it.

    **One call.** This is what a native environment primitive buys: the VMs, their managed
    disks, NICs, public addresses, the VNet and the NSG all go, in ARM's own dependency
    order, with no list to keep in step with what `create_network` and `create_vms`
    actually made. The AWS driver unpicks six resource types by hand for the same result.

    Idempotent on a group that is already gone, like every other adapter's delete: the
    caller is a destroy job that both the Destroy button and the reaper can reach.
    """
    from azure.core.exceptions import ResourceNotFoundError
    resource, _compute, _network, _sub = await _clients()

    def _delete():
        try:
            resource.resource_groups.begin_delete(env_id).result()
        except ResourceNotFoundError:
            return False
        return True

    deleted = await azure_service._to_thread(_delete)
    # After the group, not before: a failed delete must leave the credential in place, or
    # a retry builds VMs nobody can log into.
    if deleted:
        _forget_password(env_id)
    logger.info("POV %s: resource group %s", env_id, "deleted" if deleted else "not found")


# ── the platform login ───────────────────────────────────────────────────────

async def stored_credentials(env_id: str, vm_id: str = "") -> list:
    """The admin login this environment was built with, in the contract's shape.

    ``[{text, notes}]`` rather than ``{username, password}`` — the contract is shaped
    around Skytap storing *what somebody typed into a box*, and ``pov_credentials`` parses
    it. The ``user / password`` form is the first separator that parser tries.

    One credential for the whole environment, not one per VM, because that is what Azure
    forced: every VM in the group was created with the same generated admin account.
    ``vm_id`` is accepted and ignored.
    """
    from . import config_service
    password = config_service.get_opt(password_config_key(env_id))
    if not password:
        return []
    return [{
        "text": f"{ADMIN_USERNAME} / {password}",
        "notes": ("Generated by the dashboard when this POV was built. Azure requires an "
                  "admin account at creation; the same one is on every VM here."),
    }]
