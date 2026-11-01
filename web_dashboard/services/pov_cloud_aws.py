"""The AWS driver behind ``pov_cloud_env``: EC2 calls, and nothing else.

Everything policy-shaped — the environment id, the tag names, which VMs get user-data,
what a refusal says — lives in ``pov_cloud_env``. This module is the half that knows
boto3, so that adding Azure is a second file of the same shape rather than a second
opinion about what a POV environment is.

Three things here are decisions rather than mechanics, and they are the ones to argue
with before changing:

**A VPC per POV, with an internet gateway and no NAT.** A NAT gateway is roughly thirty
dollars a month standing, before a byte moves, and a POV runs for weeks — on a
community user's own bill. Instances get a public address for egress instead. That is
only safe because of the next decision.

**The security group opens nothing inbound from outside itself.** Every component the
dashboard installs connects *outbound*: the agent polls, the Gateway dials the PRA
appliance, the Resource Broker dials Password Safe. So the group allows all traffic from
itself — the broker has to reach its targets over SSH and WinRM — and nothing at all from
anywhere else. There is no SSH-from-the-internet rule to forget to remove.

**Nothing is cleaned up on a failed create.** Every resource is tagged in the same API
call that creates it, so a half-built environment is fully selectable by
``delete_environment``. A rollback here would be a second teardown path racing the first,
and the one most likely to be interrupted.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from . import aws_service, pov_cloud_env as env
from .aws_service import AWSError

logger = logging.getLogger(__name__)

# EC2 instance states that mean the instance is gone. Excluded from every read: a
# terminated instance still answers DescribeInstances for about an hour, and counting one
# would make a destroyed environment look half-alive and block the row from going
# `destroyed`.
_DEAD_STATES = ("terminated", "shutting-down")

# EC2 state -> the dashboard's runstate vocabulary. Anything unlisted passes through as
# itself: the POV page renders the string, and inventing a mapping for a state AWS added
# later is how a page starts lying about what it can see.
_RUNSTATE = {"running": "running", "stopped": "stopped"}

_ROOT_DEVICE_FALLBACK = "/dev/sda1"


def default_region() -> str:
    from . import config_service
    from ..config import settings
    return (config_service.get("aws_region")
            or getattr(settings, "aws_region", "") or "us-east-2")


def configured() -> bool:
    from . import feature_flags
    return feature_flags.cloud_configured("aws")


def _tag_spec(resource_type: str, env_id: str, name: str = "", role: str = "") -> dict:
    tags = dict(env.base_tags(env_id))
    if name:
        tags[env.TAG_NAME] = name
    if role:
        tags[env.TAG_ROLE] = role
    return {"ResourceType": resource_type,
            "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}


def _env_filter(env_id: str) -> list:
    return [{"Name": f"tag:{env.TAG_ENVIRONMENT}", "Values": [env_id]}]


# ── verify ───────────────────────────────────────────────────────────────────

def _verify_sync(region: str) -> tuple[int, str]:
    ec2 = aws_service._get_ec2_pinned(region)
    resp = ec2.describe_regions(RegionNames=[region])
    # Who the calls ran as. GetCallerIdentity needs no permission, so it cannot fail
    # where DescribeRegions succeeded; it is what tells an operator that the role was
    # actually assumed rather than some other credential being picked up.
    arn = aws_service.aws_client("sts", region, pinned=True).get_caller_identity().get("Arn", "")
    return len(resp.get("Regions") or []), arn


def _credential_label() -> str:
    """Which rung of ``aws_service._aws_kwargs`` the request-path calls are using, in
    words. Same order as ``_aws_kwargs``: a Workload Credentials lease first, unless the
    everyday credential is the SPIFFE identity, then a stored key, then federation."""
    from . import cloud_federation
    from . import config_service
    from . import workload_credential_lease as leases
    if leases.dynamic_enabled("aws") and not leases.everyday_from_federation("aws"):
        return "a Workload Credentials lease"
    if cloud_federation.source("aws") == "stored key":
        key_id = (config_service.get("aws_access_key_id") or "").strip()
        return f"access key {key_id[:4]}…{key_id[-4:]}" if len(key_id) > 8 else "a stored access key"
    if cloud_federation.active("aws"):
        return "the dashboard's SPIFFE identity"
    return "the AWS SDK's default credential chain"


def _not_configured_message() -> str:
    from . import cloud_federation
    why = cloud_federation.reason("aws")
    return ("No AWS credential is configured. Choose Access key and paste one, or choose "
            "SPIFFE identity and set the role to assume"
            + (f" (federation is not in use: {why})." if why else "."))


async def verify() -> tuple[bool, str]:
    """Prove the credential, cheaply, without a side effect.

    An expected failure is a RETURN VALUE, not an exception — a caught exception
    stringified into a response body is what CodeQL's stack-trace-exposure rule fires on,
    and this codebase learned that the fix is structural rather than a wider except.
    """
    region = default_region()
    if not configured():
        return False, _not_configured_message()
    label = _credential_label()
    try:
        count, arn = await aws_service._to_thread(_verify_sync, region)
    except AWSError as exc:
        return False, f"AWS refused the call made with {label}: {exc}"
    except Exception:  # noqa: BLE001
        logger.warning("AWS POV verify failed", exc_info=True)
        return False, (f"AWS did not answer the call made with {label}. Check the "
                       f"credential, its permissions and that {region} is enabled for "
                       f"this account.")
    if not count:
        return False, f"The credential works but cannot see region {region}."
    who = f" as {arn}" if arn else ""
    msg = f"Connected to AWS in {region}{who}, using {label}."
    if not _provisioning_split():
        return True, msg
    ok, wlc_msg = await _verify_provisioning_leg()
    return ok, f"{msg} {wlc_msg}"


def _provisioning_split() -> bool:
    return env.jobs_lease_split("aws")


async def _verify_provisioning_leg() -> tuple[bool, str]:
    # Resolved at call time, not bound as a default, so a patched _to_thread is honoured.
    return await env.verify_jobs_lease("aws", aws_service._to_thread)


# ── create ───────────────────────────────────────────────────────────────────

# What EC2 accepts in a security-group rule description. Anything else (an apostrophe,
# an em dash) fails the whole AuthorizeSecurityGroup* call with InvalidParameterValue.
_SG_DESCRIPTION_OK = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._-:/()#,@[]+=&;{}!$*")


def _sg_description(text: str) -> str:
    return "".join(c for c in (text or "") if c in _SG_DESCRIPTION_OK)[:255]


def _ip_permission(rule: dict, *, egress: bool) -> dict:
    """One ``pov_zones`` rule as an EC2 ``IpPermission``. Peers are CIDRs, never group
    references, so the zone groups never depend on each other and the tag-scoped
    teardown can delete them in any order."""
    perm = {"IpProtocol": "-1" if rule["protocol"] == "all" else rule["protocol"],
            "IpRanges": [{"CidrIp": rule["peer"], "Description": _sg_description(rule["why"])}]}
    ports = rule.get("ports") or []
    if ports and rule["protocol"] != "all":
        perm["FromPort"] = perm["ToPort"] = ports[0]
    return perm


def _zone_permissions(rules: list, direction: str) -> list:
    """Expand multi-port rules: an IpPermission carries one port range, not a list."""
    out = []
    for rule in rules:
        if rule["direction"] != direction:
            continue
        ports = rule.get("ports") or []
        if rule["protocol"] == "all" or not ports:
            out.append(_ip_permission(rule, egress=direction == "egress"))
            continue
        for port in ports:
            out.append(_ip_permission({**rule, "ports": [port]},
                                      egress=direction == "egress"))
    return out


def _create_zones_sync(ec2, env_id: str, vpc_id: str, zones: list) -> dict:
    """One subnet and one security group per Purdue zone. Returns ``{zone: {...}}``.

    Every group starts by losing AWS's default allow-all egress, then gets exactly the
    zone's rules, so enterprise's open egress is a rule this code wrote rather than one
    it forgot to remove. All of it shares the VPC's main route table: a plant subnet still
    has a route to the internet gateway, but no public address and no egress rule to use
    it.
    """
    out = {}
    for zone in zones:
        name = zone["name"]
        spec = _tag_spec("subnet", env_id, f"{env_id}-{name}")
        spec["Tags"].append({"Key": env.TAG_ZONE, "Value": name})
        subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=zone["cidr"],
                                   TagSpecifications=[spec])
        subnet_id = subnet["Subnet"]["SubnetId"]
        ec2.modify_subnet_attribute(SubnetId=subnet_id,
                                    MapPublicIpOnLaunch={"Value": bool(zone["public_ip"])})

        sg_spec = _tag_spec("security-group", env_id, f"{env_id}-{name}-sg")
        sg_spec["Tags"].append({"Key": env.TAG_ZONE, "Value": name})
        sg = ec2.create_security_group(
            GroupName=f"{env_id}-{name}-sg", VpcId=vpc_id,
            Description=_sg_description(f"POV environment {env_id} - Purdue {name} zone"),
            TagSpecifications=[sg_spec])
        sg_id = sg["GroupId"]
        ec2.revoke_security_group_egress(
            GroupId=sg_id,
            IpPermissions=[{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])
        ingress = _zone_permissions(zone["rules"], "ingress")
        egress = _zone_permissions(zone["rules"], "egress")
        if ingress:
            ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=ingress)
        if egress:
            ec2.authorize_security_group_egress(GroupId=sg_id, IpPermissions=egress)
        out[name] = {"subnet_id": subnet_id, "security_group_id": sg_id}
    return out


def _create_network_sync(env_id: str, region: str, cidr: str, sub_cidr: str,
                         zones: list | None = None) -> dict:
    ec2 = aws_service._get_ec2_pinned(region)

    vpc = ec2.create_vpc(CidrBlock=cidr,
                         TagSpecifications=[_tag_spec("vpc", env_id, env_id)])
    vpc_id = vpc["Vpc"]["VpcId"]
    # Without this the guests have no resolvable names, which breaks the Ansible
    # inventory the config-management path builds from them.
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

    if zones:
        return _create_zoned_network_sync(ec2, env_id, vpc_id, zones)

    subnet = ec2.create_subnet(
        VpcId=vpc_id, CidrBlock=sub_cidr,
        TagSpecifications=[_tag_spec("subnet", env_id, f"{env_id}-subnet")])
    subnet_id = subnet["Subnet"]["SubnetId"]
    # Egress for the agent, the Gateway and the Resource Broker, without a NAT gateway.
    ec2.modify_subnet_attribute(SubnetId=subnet_id,
                                MapPublicIpOnLaunch={"Value": True})

    igw = ec2.create_internet_gateway(
        TagSpecifications=[_tag_spec("internet-gateway", env_id, f"{env_id}-igw")])
    igw_id = igw["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

    rts = ec2.describe_route_tables(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["RouteTables"]
    rt_id = rts[0]["RouteTableId"]
    ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0",
                     GatewayId=igw_id)
    # The main route table is created with the VPC, so it is the one resource that cannot
    # be tagged at creation. Tagged here so the teardown finds it like everything else.
    ec2.create_tags(Resources=[rt_id],
                    Tags=_tag_spec("route-table", env_id, f"{env_id}-rt")["Tags"])

    sg = ec2.create_security_group(
        GroupName=f"{env_id}-sg", VpcId=vpc_id,
        Description=_sg_description(
            f"POV environment {env_id} - outbound only, plus intra-environment"),
        TagSpecifications=[_tag_spec("security-group", env_id, f"{env_id}-sg")])
    sg_id = sg["GroupId"]
    # The ONLY inbound rule: the environment talking to itself. The broker reaches its
    # targets over SSH and WinRM, and nothing outside the POV ever initiates a connection
    # into it.
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[{"IpProtocol": "-1",
                        "UserIdGroupPairs": [{"GroupId": sg_id,
                                              "Description": "intra-environment"}]}])

    return {"vpc_id": vpc_id, "subnet_id": subnet_id, "security_group_id": sg_id,
            "internet_gateway_id": igw_id, "route_table_id": rt_id}


def _internet_route_sync(ec2, env_id: str, vpc_id: str) -> tuple:
    """The internet gateway and the main route table's default route. Returns their ids."""
    igw = ec2.create_internet_gateway(
        TagSpecifications=[_tag_spec("internet-gateway", env_id, f"{env_id}-igw")])
    igw_id = igw["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    rts = ec2.describe_route_tables(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["RouteTables"]
    rt_id = rts[0]["RouteTableId"]
    ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0",
                     GatewayId=igw_id)
    ec2.create_tags(Resources=[rt_id],
                    Tags=_tag_spec("route-table", env_id, f"{env_id}-rt")["Tags"])
    return igw_id, rt_id


def _create_zoned_network_sync(ec2, env_id: str, vpc_id: str, zones: list) -> dict:
    """The Purdue-zoned variant: the internet route, then a subnet and group per zone.

    The top-level ``subnet_id`` / ``security_group_id`` are the enterprise zone's, so a
    caller that knows nothing about zones still lands a VM somewhere sensible.
    """
    igw_id, rt_id = _internet_route_sync(ec2, env_id, vpc_id)
    built = _create_zones_sync(ec2, env_id, vpc_id, zones)
    first = built.get("enterprise") or next(iter(built.values()))
    return {"vpc_id": vpc_id, "subnet_id": first["subnet_id"],
            "security_group_id": first["security_group_id"],
            "internet_gateway_id": igw_id, "route_table_id": rt_id, "zones": built}


async def create_network(env_id: str, region: str, cidr: str, sub_cidr: str,
                         zones: list | None = None) -> dict:
    return await aws_service._to_thread(_create_network_sync, env_id, region, cidr,
                                        sub_cidr, zones)


# ── the image-build network ──────────────────────────────────────────────────
#
# Where a POV standard-image build (packer_build_service) launches its instance. The
# provisioning role can launch only into a subnet tagged as the dashboard's own, so the
# account's default VPC is out of reach, and a POV environment's network is not the
# build's to borrow. One per region, built on first use and kept: it costs nothing
# standing (no NAT gateway), and Packer brings and removes its own security group.
#
# Tagged povManagedBy but with NO povEnvironment: every listing and teardown groups on
# the environment tag, so this network never shows up as an orphaned POV on /pov/cloud
# and no environment's delete can take it down mid-build.

IMAGE_BUILD_ROLE = "image-build"
_IMAGE_BUILD_CIDR = "10.254.0.0/16"
_IMAGE_BUILD_SUBNET_CIDR = "10.254.0.0/24"


def image_build_tags() -> dict:
    """The tags on the image-build network, and on everything a POV build creates."""
    return {env.TAG_MANAGED_BY: env.MANAGED_BY, env.TAG_ESTATE: env.MANAGED_BY,
            env.TAG_ROLE: IMAGE_BUILD_ROLE}


def _image_build_spec(resource_type: str, name: str) -> dict:
    tags = {**image_build_tags(), env.TAG_NAME: name}
    return {"ResourceType": resource_type,
            "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}


def _image_build_filters() -> list:
    return [{"Name": f"tag:{env.TAG_MANAGED_BY}", "Values": [env.MANAGED_BY]},
            {"Name": f"tag:{env.TAG_ROLE}", "Values": [IMAGE_BUILD_ROLE]}]


def _aws_code(exc) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _image_build_vpc(ec2, name: str) -> str:
    """The image-build VPC: the lowest-id tagged one, created when there is none.

    Converges rather than locks, because builds can start together on different worker
    replicas: a racer that created a VPC re-lists, and if another tagged VPC sorts first
    it deletes its own and uses that one, so the region ends up with exactly one."""
    def _tagged():
        return sorted(v["VpcId"] for v in
                      ec2.describe_vpcs(Filters=_image_build_filters()).get("Vpcs") or [])
    vpcs = _tagged()
    if vpcs:
        return vpcs[0]
    mine = ec2.create_vpc(CidrBlock=_IMAGE_BUILD_CIDR,
                          TagSpecifications=[_image_build_spec("vpc", name)])["Vpc"]["VpcId"]
    winner = (_tagged() or [mine])[0]
    if winner != mine:
        try:
            ec2.delete_vpc(VpcId=mine)
        except Exception as exc:  # noqa: BLE001 — _remove_extra_image_build_vpcs retries
            logger.warning("image-build network: could not remove duplicate VPC %s: %s",
                           mine, exc)
    return winner


def _remove_extra_image_build_vpcs(ec2, keep: str) -> None:
    """Delete every tagged image-build VPC that sorts after ``keep`` (the lowest id wins,
    so a replica never deletes the one every other replica converges on), with its
    gateways and subnets, unless something still runs in it. Two replicas that each saw only their own VPC (tag
    filters can lag a create), or a delete that failed, would otherwise leave one behind
    for good, against a quota of five per region. Never raises: this is housekeeping."""
    try:
        extras = [v["VpcId"] for v in
                  ec2.describe_vpcs(Filters=_image_build_filters()).get("Vpcs") or []
                  if v["VpcId"] > keep]
    except Exception as exc:  # noqa: BLE001
        logger.warning("image-build network: could not list VPCs to tidy: %s", exc)
        return
    for vpc in extras:
        try:
            live = [i for r in ec2.describe_instances(
                        Filters=[{"Name": "vpc-id", "Values": [vpc]}]).get("Reservations") or []
                    for i in r.get("Instances") or []
                    if (i.get("State") or {}).get("Name") not in ("terminated", "shutting-down")]
            if live:
                logger.info("image-build network: leaving extra VPC %s, %d instance(s) in it",
                            vpc, len(live))
                continue
            for g in ec2.describe_internet_gateways(
                    Filters=[{"Name": "attachment.vpc-id", "Values": [vpc]}]
            ).get("InternetGateways") or []:
                ec2.detach_internet_gateway(InternetGatewayId=g["InternetGatewayId"], VpcId=vpc)
                ec2.delete_internet_gateway(InternetGatewayId=g["InternetGatewayId"])
            for sub in ec2.describe_subnets(
                    Filters=[{"Name": "vpc-id", "Values": [vpc]}]).get("Subnets") or []:
                ec2.delete_subnet(SubnetId=sub["SubnetId"])
            ec2.delete_vpc(VpcId=vpc)
            logger.info("image-build network: removed extra VPC %s (kept %s)", vpc, keep)
        except Exception as exc:  # noqa: BLE001
            logger.warning("image-build network: could not remove extra VPC %s: %s", vpc, exc)


def _ensure_image_build_subnet_sync(region: str) -> str:
    """Find-or-create each piece of the image-build network, so a run that failed half
    way (a VPC with no subnet, an internet gateway never attached, no default route, or
    one left pointing at a gateway since deleted) is finished by the next one instead of
    leaving a VPC behind and starting another: an account has five VPCs per region by
    default. Safe against a build starting at the same time on another worker replica:
    each step tolerates the other having just done it."""
    from botocore.exceptions import ClientError
    ec2 = aws_service._get_ec2_pinned(region)
    name = "vm-dashboard-image-build"
    filters = _image_build_filters()

    def _subnets(vpc=None):
        f = filters + ([{"Name": "vpc-id", "Values": [vpc]}] if vpc else [])
        return sorted((s for s in ec2.describe_subnets(Filters=f).get("Subnets") or []
                       if s.get("State") == "available"),
                      key=lambda s: (s["VpcId"], s["SubnetId"]))

    subnets = _subnets()
    if subnets:
        # The lowest VPC id wins (as _image_build_vpc), then the lowest subnet id, so
        # two builds always pick the same one and the extras can be removed.
        vpc_id, subnet_id = subnets[0]["VpcId"], subnets[0]["SubnetId"]
    else:
        vpc_id = _image_build_vpc(ec2, name)
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
        try:
            subnet_id = ec2.create_subnet(
                VpcId=vpc_id, CidrBlock=_IMAGE_BUILD_SUBNET_CIDR,
                TagSpecifications=[_image_build_spec("subnet", f"{name}-subnet")]
            )["Subnet"]["SubnetId"]
        except ClientError as exc:
            # Another build created it a moment ago: same VPC, same range.
            if _aws_code(exc) != "InvalidSubnet.Conflict" or not _subnets(vpc_id):
                raise
            subnet_id = _subnets(vpc_id)[0]["SubnetId"]
        # Packer connects to the build instance over its public address.
        ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})

    def _attached():
        return ec2.describe_internet_gateways(
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
        ).get("InternetGateways") or []
    igws = _attached()
    if igws:
        igw_id = igws[0]["InternetGatewayId"]
    else:
        loose = [g for g in ec2.describe_internet_gateways(Filters=filters)
                 .get("InternetGateways") or [] if not g.get("Attachments")]
        igw_id = (loose[0]["InternetGatewayId"] if loose else ec2.create_internet_gateway(
            TagSpecifications=[_image_build_spec("internet-gateway", f"{name}-igw")]
        )["InternetGateway"]["InternetGatewayId"])
        try:
            ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
        except ClientError as exc:
            # Another build attached one first (a VPC takes one gateway): use that, and
            # leave ours loose and tagged, for the next build that needs one.
            if not _attached():
                raise
            logger.info("image-build network: %s already has a gateway (%s)", vpc_id,
                        _aws_code(exc))
            igw_id = _attached()[0]["InternetGatewayId"]
    rt = ec2.describe_route_tables(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                 {"Name": "association.main", "Values": ["true"]}])["RouteTables"][0]
    default = next((r for r in rt.get("Routes") or []
                    if r.get("DestinationCidrBlock") == "0.0.0.0/0"), None)
    if default is None:
        try:
            ec2.create_route(RouteTableId=rt["RouteTableId"], DestinationCidrBlock="0.0.0.0/0",
                             GatewayId=igw_id)
        except ClientError as exc:
            if _aws_code(exc) != "RouteAlreadyExists":
                raise
        ec2.create_tags(Resources=[rt["RouteTableId"]],
                        Tags=_image_build_spec("route-table", f"{name}-rt")["Tags"])
        logger.info("built the POV image-build network in %s: %s / %s",
                    region, vpc_id, subnet_id)
    elif default.get("State") == "blackhole" or default.get("GatewayId") != igw_id:
        # Left pointing at a gateway that is gone (or is not this VPC's): repoint it, or
        # the build instance has no way out and Packer waits out its SSH timeout.
        ec2.replace_route(RouteTableId=rt["RouteTableId"], DestinationCidrBlock="0.0.0.0/0",
                          GatewayId=igw_id)
        logger.info("image-build network in %s: repointed its default route to %s",
                    region, igw_id)
    _remove_extra_image_build_vpcs(ec2, vpc_id)
    return subnet_id


# One build per region prepares the network at a time within this process; across worker
# replicas, _ensure_image_build_subnet_sync converges instead (see _image_build_vpc).
_IMAGE_BUILD_LOCKS: dict = {}


async def ensure_image_build_subnet(region: str) -> str:
    """The image-build subnet in ``region``, built the first time a region needs one."""
    lock = _IMAGE_BUILD_LOCKS.setdefault(region, asyncio.Lock())
    async with lock:
        return await aws_service._to_thread(_ensure_image_build_subnet_sync, region)


def _root_device(ec2, image_id: str) -> tuple:
    """``(device_name, minimum_size_gb)`` for an AMI.

    The minimum matters: RunInstances refuses a root volume smaller than the snapshot the
    AMI was built from, and the error names a number rather than the template field that
    produced it. Asking the image is one call and turns that into a clamp.
    """
    try:
        images = ec2.describe_images(ImageIds=[image_id]).get("Images") or []
    except Exception:  # noqa: BLE001 - a describe failure must not mask the real error
        return _ROOT_DEVICE_FALLBACK, 0
    if not images:
        return _ROOT_DEVICE_FALLBACK, 0
    img = images[0]
    device = img.get("RootDeviceName") or _ROOT_DEVICE_FALLBACK
    size = 0
    for mapping in img.get("BlockDeviceMappings") or []:
        if mapping.get("DeviceName") == device:
            size = int((mapping.get("Ebs") or {}).get("VolumeSize") or 0)
            break
    return device, size


def _create_vms_sync(env_id: str, region: str, specs: list, network: dict) -> list:
    ec2 = aws_service._get_ec2_pinned(region)
    created = []
    windows = any(s.get("os_family") == "windows" for s in specs)
    key_name = _import_windows_key_sync(ec2, env_id) if windows else ""
    try:
        for spec in specs:
            if spec.get("login") == STANDARD_LINUX_LOGIN and not spec.get("user_data"):
                spec = {**spec, "user_data": _linux_login_user_data(env_id)}
            vm = _launch_sync(ec2, env_id, spec, network,
                              key_name if spec.get("os_family") == "windows" else "")
            created.append(vm)
            if spec.get("login") == STANDARD_LINUX_LOGIN:
                _remember_linux_login_vm(env_id, vm["id"])
    finally:
        if key_name:
            # The EC2 half is needed only AT launch: GetPasswordData encrypts to the key
            # the instance was launched with, whether or not the pair still exists. So it
            # goes now rather than at teardown, and there is no key pair to leak.
            try:
                ec2.delete_key_pair(KeyName=key_name)
            except Exception:  # noqa: BLE001 - re-imported on the next launch anyway
                logger.warning("POV %s: could not delete key pair %s", env_id, key_name,
                               exc_info=True)
    return created


def _launch_sync(ec2, env_id: str, spec: dict, network: dict, key_name: str) -> dict:
    where = {**network, **env.placement(network, spec)}
    image_id = spec["image_id"]
    device, minimum = _root_device(ec2, image_id)
    size = max(int(spec.get("disk_gb") or env.DEFAULT_DISK_GB), minimum)
    kwargs = {
        "ImageId": image_id,
        "InstanceType": spec["instance_type"],
        "MinCount": 1,
        "MaxCount": 1,
        "SubnetId": where["subnet_id"],
        "SecurityGroupIds": [where["security_group_id"]],
        "BlockDeviceMappings": [{
            "DeviceName": device,
            "Ebs": {"VolumeSize": size, "VolumeType": "gp3",
                    "DeleteOnTermination": True},
        }],
        "TagSpecifications": [
            _tag_spec("instance", env_id, spec["name"], spec.get("role") or "target"),
            # The root volume too, so a stray volume after a failed teardown is still
            # attributable to the POV that made it.
            _tag_spec("volume", env_id, spec["name"], spec.get("role") or "target"),
        ],
        # IMDSv2 required. A POV runs for weeks with a customer inside it, and the
        # instance role — if one is ever attached — should not be readable by anything
        # that can make a plain GET from the guest.
        "MetadataOptions": {"HttpTokens": "required", "HttpEndpoint": "enabled"},
    }
    if spec.get("user_data"):
        kwargs["UserData"] = spec["user_data"]
    if key_name:
        # Windows only. EC2 encrypts the generated Administrator password to this key,
        # and without one GetPasswordData has nothing to return -- ever. See
        # :func:`stored_credentials`.
        kwargs["KeyName"] = key_name
    resp = ec2.run_instances(**kwargs)
    inst = resp["Instances"][0]
    logger.info("POV %s: launched %s as %s", env_id, spec["name"],
                inst["InstanceId"])
    return {"id": inst["InstanceId"], "name": spec["name"]}


async def create_vms(env_id: str, region: str, specs: list, network: dict) -> list:
    return await aws_service._to_thread(_create_vms_sync, env_id, region, specs, network)


# ── the Windows login ────────────────────────────────────────────────────────
#
# A stock Windows AMI has no password anybody chose: EC2Launch generates the
# Administrator's at first boot and publishes it only RSA-encrypted to the instance's key
# pair. These instances used to launch with NO key pair, so that password was unknowable
# by anyone, ever -- and the remedy the Resource Broker install offered, "type the login
# into the VMs tab", had nothing true to type.
#
# So each POV gets one RSA key, generated here and kept encrypted in config like Azure's
# generated password. Only its PUBLIC half reaches EC2, imported for the length of a
# launch and deleted straight after (see `_create_vms_sync`). The password itself is never
# stored: it is decrypted from GetPasswordData per run, the way Skytap's stored
# credentials are read live.
#
# Keyed on the platform environment id, for the reason Azure's `_PW_FMT` gives.
_WINDOWS_KEY_FMT = "pov/{env_id}/windows_key"

# The account EC2Launch sets the generated password on.
WINDOWS_ADMIN = "Administrator"


def windows_key_config_key(env_id: str) -> str:
    return _WINDOWS_KEY_FMT.format(env_id=env_id)


def _windows_key_pem(env_id: str) -> str:
    """This environment's private key, generated once and remembered."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from . import config_service
    key = windows_key_config_key(env_id)
    existing = config_service.get_opt(key)
    if existing:
        return existing
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()).decode()
    config_service.set(key, pem)
    return pem


def _import_windows_key_sync(ec2, env_id: str) -> str:
    """Put this environment's public key in EC2 under a name it owns. Returns the name.

    A pair left by a launch that died before its cleanup is replaced rather than refused:
    the name is derived from an environment id only this dashboard mints, so nothing else
    can own it, and refusing would wedge every later launch of this POV.
    """
    from cryptography.hazmat.primitives import serialization
    name = f"{env_id}-windows"
    private = serialization.load_pem_private_key(_windows_key_pem(env_id).encode(),
                                                 password=None)
    public = private.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                               serialization.PublicFormat.OpenSSH)
    try:
        ec2.delete_key_pair(KeyName=name)
    except Exception:  # noqa: BLE001 - absent is the normal case
        pass
    ec2.import_key_pair(KeyName=name, PublicKeyMaterial=public, TagSpecifications=[
        _tag_spec("key-pair", env_id)])
    return name


def holds_login(env_id: str, os_family: str = "", vm_id: str = "") -> bool:
    """Whether :func:`stored_credentials` can answer for a VM of this family. No network.

    Windows: only for an environment built since the key existed — a POV launched before
    then has instances with no key pair, whose password nothing can recover.

    Linux: only for a VM launched from a Standard Linux image, which is given this POV's
    own login at launch. Asked per VM, because a POV can mix that image with any other,
    and a stock Linux guest still has its login typed by hand.
    """
    family = (os_family or "").strip().lower()
    from . import config_service
    if family == "windows":
        return bool(config_service.get_opt(windows_key_config_key(env_id)))
    if family == "linux" and vm_id:
        return vm_id in _linux_login_vms(env_id)
    return False


def _password_data_sync(region: str, vm_id: str) -> str:
    return (aws_service._get_ec2_pinned(region).get_password_data(InstanceId=vm_id)
            .get("PasswordData") or "").strip()


async def stored_credentials(env_id: str, vm_id: str = "") -> list:
    """A Windows VM's Administrator login, decrypted from EC2, in the contract's shape.

    ``[]`` for a VM this environment holds no key for -- a Linux guest, or a POV built
    before the key existed. Raises :class:`pov_cloud_env.CredentialNotReady` while EC2 has
    not published the password yet, which is normal for the first few minutes of a boot
    and is a "wait", not a fault.
    """
    from . import config_service
    if vm_id and vm_id in _linux_login_vms(env_id):
        password = config_service.get_opt(linux_password_config_key(env_id))
        if not password:
            return []
        return [{
            "text": f"{LINUX_LOGIN_USER} / {password}",
            "notes": ("Set by the dashboard at launch on this Standard Linux guest; one "
                      "password per POV, kept encrypted in config."),
        }]
    pem = config_service.get_opt(windows_key_config_key(env_id))
    if not pem or not vm_id:
        return []
    region = env.recorded_region(env_id, "aws")
    data = await aws_service._to_thread(_password_data_sync, region, vm_id)
    if not data:
        raise env.CredentialNotReady(
            "EC2 has not published this VM's Windows password yet. It does so a few "
            "minutes after first boot (4-15 is normal); try again shortly. If it never "
            "appears, the image does not run EC2Launch, or it was built with a fixed "
            "password -- then enter that login in the Login column on the VMs tab.")
    try:
        password = aws_service.decrypt_windows_password(data, pem)
    except Exception:  # noqa: BLE001 - a key mismatch, never anything worth echoing
        logger.warning("POV %s: could not decrypt the password of %s", env_id, vm_id,
                       exc_info=True)
        return []
    return [{
        "text": f"{WINDOWS_ADMIN} / {password}",
        "notes": ("Generated by EC2 at first boot and decrypted with this POV's own key. "
                  "Read live, never stored."),
    }]


# ── the Standard Linux login ─────────────────────────────────────────────────
#
# A stock Linux AMI on a POV has no login the dashboard knows: no key pair is attached
# and nothing is stored, so an operator types one. A Standard Linux image
# (services/pov_image_recipes) carries the `adminuser` account with password SSH allowed
# for it alone, and THIS is where it gets a password: one per POV, generated here, kept
# encrypted in config like the Windows key, and set by cloud-init on first boot. Never
# baked into the image, which is shared by every POV built from it.
#
# The password does sit in the instance's user-data, readable from IMDS by a process on
# that guest. That is the same exposure Azure's generated admin password has, it is
# confined to the guest it unlocks, and Password Safe rotates the account once onboarded.

from .pov_image_recipes import LINUX_ADMIN_USER as LINUX_LOGIN_USER  # noqa: E402
from .pov_image_recipes import STANDARD_LINUX as STANDARD_LINUX_LOGIN  # noqa: E402
_LINUX_PW_FMT = "pov/{env_id}/linux_password"
_LINUX_VMS_FMT = "pov/{env_id}/linux_login_vms"


def linux_password_config_key(env_id: str) -> str:
    return _LINUX_PW_FMT.format(env_id=env_id)


def _linux_password(env_id: str) -> str:
    import secrets
    from . import config_service
    key = linux_password_config_key(env_id)
    existing = config_service.get_opt(key)
    if existing:
        return existing
    # Letters and digits only: it travels through YAML, a shell-free chpasswd and
    # Ansible's ansible_ssh_pass, and none of those should have to quote it.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    password = "".join(secrets.choice(alphabet) for _ in range(24))
    config_service.set(key, password)
    return password


def _linux_login_user_data(env_id: str) -> str:
    return ("#cloud-config\n"
            "chpasswd:\n"
            "  expire: false\n"
            "  users:\n"
            f"    - name: {LINUX_LOGIN_USER}\n"
            f"      password: {_linux_password(env_id)}\n"
            "      type: text\n")


def _linux_login_vms(env_id: str) -> set:
    from . import config_service
    raw = config_service.get_opt(_LINUX_VMS_FMT.format(env_id=env_id)) or ""
    return {v for v in raw.split(",") if v}


def _remember_linux_login_vm(env_id: str, vm_id: str) -> None:
    from . import config_service
    vms = _linux_login_vms(env_id) | {vm_id}
    config_service.set(_LINUX_VMS_FMT.format(env_id=env_id), ",".join(sorted(vms)))


def _forget_linux_login(env_id: str) -> None:
    from . import config_service
    for key in (linux_password_config_key(env_id), _LINUX_VMS_FMT.format(env_id=env_id)):
        try:
            config_service.delete(key)
        except Exception:  # noqa: BLE001 - never fail a teardown over housekeeping
            # No key name and no traceback: the key is the password's, and a config
            # failure's exception text can carry the row it was writing.
            logger.warning("POV %s: could not clear the stored Standard Linux login",
                           env_id)


def _forget_windows_key(env_id: str) -> None:
    from . import config_service
    try:
        config_service.delete(windows_key_config_key(env_id))
    except Exception:  # noqa: BLE001 - never fail a teardown over housekeeping
        logger.warning("POV %s: could not clear the stored Windows key", env_id,
                       exc_info=True)


# ── read ─────────────────────────────────────────────────────────────────────

def _vm(inst: dict, sizes_by_instance: dict) -> dict:
    tags = {t["Key"]: t["Value"] for t in inst.get("Tags") or []}
    state = ((inst.get("State") or {}).get("Name") or "")
    platform = (inst.get("PlatformDetails") or inst.get("Platform") or "")
    return {
        "id": inst.get("InstanceId") or "",
        "name": tags.get(env.TAG_NAME) or inst.get("InstanceId") or "",
        # "windows" | "linux". AWS reports PlatformDetails for every instance, so unlike
        # Skytap there is no case where the honest answer is blank.
        "os_family": "windows" if "windows" in platform.lower() else "linux",
        "runstate": _RUNSTATE.get(state, state),
        "private_ip": inst.get("PrivateIpAddress") or "",
        "role": tags.get(env.TAG_ROLE) or "target",
        # What this VM costs to keep, in the only two units AWS charges for it: the shape
        # it runs as, and the disk it keeps whether it runs or not. Reported from the same
        # describe the runstate comes from, so the footprint on /pov/cloud can never
        # disagree with the VM list beside it.
        "instance_type": inst.get("InstanceType") or "",
        # DescribeInstances reports each block device's volume ID, not its size — so the
        # sizes come from a separate DescribeVolumes, scoped by the same environment tag
        # the volumes were created with. Summing the instance's own mappings here would
        # quietly total zero.
        "disk_gb": sizes_by_instance.get(inst.get("InstanceId") or "", 0),
        "launched_at": (inst["LaunchTime"].isoformat()
                        if inst.get("LaunchTime") else ""),
        # No NAT-ed guest ports on a cloud: access is PRA, through the POV's own Gateway.
        "published_services": [],
    }


def _environment(env_id: str, instances: list, region: str,
                 sizes_by_instance: dict | None = None) -> dict:
    vms = [_vm(i, sizes_by_instance or {}) for i in instances]
    states = {v["runstate"] for v in vms}
    if not states:
        runstate = ""
    elif states == {"running"}:
        runstate = "running"
    elif states == {"stopped"}:
        runstate = "stopped"
    else:
        # Mid-transition, or genuinely mixed. "busy" is the same word Skytap uses and the
        # POV page already gates its buttons on it.
        runstate = "busy"
    return {
        "id": env_id,
        "name": env_id[len(env.ENV_ID_PREFIX):] if env.is_env_id(env_id) else env_id,
        "runstate": runstate,
        "region": region,
        "vm_count": len(vms),
        "vms": vms,
        # No console URL: AWS has no per-environment page, and a link to the EC2 console
        # filtered by tag would need an account id this dashboard does not always hold.
        "url": "",
    }


def _describe_sync(region: str, filters: list) -> list:
    ec2 = aws_service._get_ec2_pinned(region)
    out = []
    token = None
    while True:
        kwargs = {"Filters": filters, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        resp = ec2.describe_instances(**kwargs)
        for res in resp.get("Reservations") or []:
            for inst in res.get("Instances") or []:
                if ((inst.get("State") or {}).get("Name") or "") in _DEAD_STATES:
                    continue
                out.append(inst)
        token = resp.get("NextToken")
        if not token:
            break
    return out


def _volume_sizes_sync(region: str, filters: list) -> dict:
    """``{instance_id: total attached GB}`` for one tag scope.

    EBS is charged whether the instance is running or stopped, which makes it the line
    that a suspended POV keeps paying — so the footprint has to include it or the page
    would imply a stopped environment is free.
    """
    ec2 = aws_service._get_ec2_pinned(region)
    out: dict = {}
    token = None
    while True:
        kwargs = {"Filters": filters, "MaxResults": 500}
        if token:
            kwargs["NextToken"] = token
        resp = ec2.describe_volumes(**kwargs)
        for vol in resp.get("Volumes") or []:
            size = int(vol.get("Size") or 0)
            for att in vol.get("Attachments") or []:
                iid = att.get("InstanceId") or ""
                if iid:
                    out[iid] = out.get(iid, 0) + size
        token = resp.get("NextToken")
        if not token:
            break
    return out


async def read_environment(env_id: str, region: str):
    """The environment, or ``None`` when nothing carries its tag.

    ``None`` rather than a refusal: the caller distinguishes "gone" from "broken", and
    ``pov_reconcile`` only marks an environment missing on two independent signals.
    """
    filters = _env_filter(env_id)
    instances = await aws_service._to_thread(_describe_sync, region, filters)
    if not instances:
        # No instances is not the same as gone. A teardown that terminated the VMs and
        # then failed on the VPC leaves a real, findable environment — and answering None
        # here would let `pov_reconcile` flag the POV as missing from the platform while
        # its network is still sitting there.
        if not await aws_service._to_thread(_network_env_ids_sync, region, filters):
            return None
        return _environment(env_id, [], region, {})
    sizes = await aws_service._to_thread(_volume_sizes_sync, region, filters)
    return _environment(env_id, instances, region, sizes)


async def list_environments(region: str) -> list:
    """Every POV environment this credential can see in one region, tag-grouped.

    Grouped from the INSTANCES, and then from the tagged VPCs as well — because an
    environment whose instances are all gone but whose network survives is precisely the
    orphan /pov/cloud exists to surface, and grouping on instances alone would render it
    invisible. That state is reachable: a teardown that terminated the instances and then
    failed on the VPC leaves exactly this.
    """
    managed = [{"Name": f"tag:{env.TAG_MANAGED_BY}", "Values": [env.MANAGED_BY]}]
    instances = await aws_service._to_thread(_describe_sync, region, managed)
    grouped: dict = {}
    for inst in instances:
        tags = {t["Key"]: t["Value"] for t in inst.get("Tags") or []}
        env_id = tags.get(env.TAG_ENVIRONMENT) or ""
        if env_id:
            grouped.setdefault(env_id, []).append(inst)

    for env_id in await aws_service._to_thread(_network_env_ids_sync, region, managed):
        grouped.setdefault(env_id, [])

    sizes = await aws_service._to_thread(_volume_sizes_sync, region, managed)
    return [_environment(eid, rows, region, sizes)
            for eid, rows in sorted(grouped.items())]


def _network_env_ids_sync(region: str, filters: list) -> list:
    """Environment ids that still have a VPC, whatever happened to their instances."""
    ec2 = aws_service._get_ec2_pinned(region)
    out = set()
    for vpc in ec2.describe_vpcs(Filters=filters).get("Vpcs") or []:
        tags = {t["Key"]: t["Value"] for t in vpc.get("Tags") or []}
        if tags.get(env.TAG_ENVIRONMENT):
            out.add(tags[env.TAG_ENVIRONMENT])
    return sorted(out)

# ── the network, read back ───────────────────────────────────────────────────

def _read_network_sync(env_id: str, region: str) -> dict:
    """The subnet and security group an environment was built with.

    Read back from the tags rather than remembered on the POV row. The row would be a
    second record of something AWS already knows, and one that goes stale the moment
    anybody changes the environment by hand — while the tag is the same thing the teardown
    selects on, so if this finds it the teardown will too.
    """
    ec2 = aws_service._get_ec2_pinned(region)
    filters = _env_filter(env_id)
    subnets = ec2.describe_subnets(Filters=filters).get("Subnets") or []
    groups = ec2.describe_security_groups(Filters=filters).get("SecurityGroups") or []
    if not subnets or not groups:
        return {}

    def _zone(resource: dict) -> str:
        return next((t["Value"] for t in resource.get("Tags") or []
                     if t.get("Key") == env.TAG_ZONE), "")

    sub_by_zone = {_zone(s): s for s in subnets if _zone(s)}
    sg_by_zone = {_zone(g): g for g in groups if _zone(g)}
    if sub_by_zone:
        # A zoned environment: the map create_vms places by, and the enterprise zone as
        # the default so a zone-unaware caller still gets the broker's subnet.
        zones = {z: {"subnet_id": sub_by_zone[z]["SubnetId"],
                     "security_group_id": sg_by_zone[z]["GroupId"]}
                 for z in sub_by_zone if z in sg_by_zone}
        first = zones.get("enterprise") or next(iter(zones.values()), None)
        if first is None:
            return {}
        return {**first, "vpc_id": subnets[0].get("VpcId") or "", "zones": zones}
    return {"subnet_id": subnets[0]["SubnetId"],
            "security_group_id": groups[0]["GroupId"],
            "vpc_id": subnets[0].get("VpcId") or ""}


async def read_network(env_id: str, region: str) -> dict:
    return await aws_service._to_thread(_read_network_sync, env_id, region)


def _remove_vms_sync(env_id: str, region: str, names: list) -> list:
    """Terminate named instances in one environment, and wait for them to go.

    Scoped by BOTH the environment tag and the name. The name alone is not a key — two
    POVs can each have a `broker` — and this is called to make room for a replacement, so
    selecting one environment's instance while claiming to select another's would delete a
    live customer's agent host.
    """
    ec2 = aws_service._get_ec2_pinned(region)
    wanted = {(n or "").strip().lower() for n in names if (n or "").strip()}
    if not wanted:
        return []
    ids = []
    for inst in _describe_sync(region, _env_filter(env_id)):
        tags = {t["Key"]: t["Value"] for t in inst.get("Tags") or []}
        if (tags.get(env.TAG_NAME) or "").strip().lower() in wanted:
            ids.append(inst["InstanceId"])
    if not ids:
        return []
    ec2.terminate_instances(InstanceIds=ids)
    # Waited out, not fired and forgotten: the replacement launches into the same subnet
    # and security group, and a name that still resolves to a dying instance is how a
    # read-back picks the wrong one.
    ec2.get_waiter("instance_terminated").wait(
        InstanceIds=ids, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    return ids


async def remove_vms(env_id: str, region: str, names: list) -> list:
    removed = await aws_service._to_thread(_remove_vms_sync, env_id, region, names)
    if removed:
        logger.info("POV %s: replaced %s", env_id, ", ".join(removed))
    return removed

# ── power ────────────────────────────────────────────────────────────────────

def _power_sync(env_id: str, region: str, action: str) -> int:
    ec2 = aws_service._get_ec2_pinned(region)
    ids = [i["InstanceId"] for i in _describe_sync(region, _env_filter(env_id))]
    if not ids:
        return 0
    if action == "start":
        ec2.start_instances(InstanceIds=ids)
    else:
        # A plain stop, never Hibernate: hibernation has to be enabled at launch, is
        # unsupported on most instance families and every Windows AMI here, and silently
        # falls back to a stop on the ones where it is not — so asking for it would make
        # the resume path depend on which family a template happened to name.
        ec2.stop_instances(InstanceIds=ids)
    return len(ids)


async def power(env_id: str, region: str, action: str) -> None:
    count = await aws_service._to_thread(_power_sync, env_id, region, action)
    logger.info("POV %s: %s %d instances in %s", env_id, action, count, region)


# ── idle metrics ─────────────────────────────────────────────────────────────

# GetMetricData takes at most 500 queries a call; three per instance.
_METRIC_INSTANCES_PER_CALL = 150
_QUERY_ID_RE = re.compile(r"^(cpu|nin|nout)(\d+)$")


def _idle_activity_sync(env_id: str, region: str, window_s: int) -> dict:
    """``{vm name: [{at, cpu, net}]}`` from CloudWatch, for ``pov_cloud_env.idle_verdict``.

    Basic monitoring only — the free five-minute metrics every instance publishes, so a
    POV needs no detailed monitoring, no agent and no alarm of its own. An instance that
    is not running is listed with no points, which the verdict reads as "not idle".
    """
    instances = _describe_sync(region, _env_filter(env_id))
    out = {}
    names = {}
    for inst in instances:
        iid = inst.get("InstanceId") or ""
        tags = {t["Key"]: t["Value"] for t in inst.get("Tags") or []}
        name = tags.get(env.TAG_NAME) or iid
        # Keyed per instance: two VMs sharing a Name tag merged into one series would
        # look like one VM with complete coverage.
        names[iid] = name if name not in out else f"{name} ({iid})"
        out[names[iid]] = []
    running = [i["InstanceId"] for i in instances
               if ((i.get("State") or {}).get("Name") or "") == "running"]
    if not running:
        return out

    cw = aws_service.aws_client("cloudwatch", region, pinned=True)
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=window_s + env.IDLE_PERIOD_S)
    for i in range(0, len(running), _METRIC_INSTANCES_PER_CALL):
        chunk = running[i:i + _METRIC_INSTANCES_PER_CALL]
        queries = []
        for n, iid in enumerate(chunk):
            for qid, metric, stat in (("cpu", "CPUUtilization", "Average"),
                                      ("nin", "NetworkIn", "Sum"),
                                      ("nout", "NetworkOut", "Sum")):
                queries.append({
                    "Id": f"{qid}{n}",
                    "MetricStat": {
                        "Metric": {"Namespace": "AWS/EC2", "MetricName": metric,
                                   "Dimensions": [{"Name": "InstanceId", "Value": iid}]},
                        "Period": env.IDLE_PERIOD_S, "Stat": stat},
                    "ReturnData": True})
        # {instance index: {period start: {"cpu": .., "nin": .., "nout": ..}}}
        by_period: dict = {}
        token = None
        while True:
            kwargs = {"MetricDataQueries": queries, "StartTime": start, "EndTime": end}
            if token:
                kwargs["NextToken"] = token
            resp = cw.get_metric_data(**kwargs)
            for result in resp.get("MetricDataResults") or []:
                m = _QUERY_ID_RE.match(result.get("Id") or "")
                if not m:
                    continue
                qid, idx = m.group(1), int(m.group(2))
                for ts, val in zip(result.get("Timestamps") or [],
                                   result.get("Values") or []):
                    slot = by_period.setdefault(idx, {}).setdefault(ts.timestamp(), {})
                    slot[qid] = val
            token = resp.get("NextToken")
            if not token:
                break
        for idx, periods in by_period.items():
            name = names[chunk[idx]]
            for at, vals in sorted(periods.items()):
                net = (vals["nin"] + vals["nout"]
                       if "nin" in vals and "nout" in vals else None)
                out[name].append({"at": at, "cpu": vals.get("cpu"), "net": net})
    return out


async def idle_activity(env_id: str, region: str, window_s: int) -> dict:
    return await aws_service._to_thread(_idle_activity_sync, env_id, region, window_s)


# ── teardown ─────────────────────────────────────────────────────────────────

def _delete_sync(env_id: str, region: str) -> list:
    """Terminate the instances, then unpick the network they sat in.

    Ordered and tolerant. Every step is scoped to the environment tag, every step skips
    what is already gone, and a step that fails is logged and stepped over rather than
    stopping the rest — a teardown that aborts halfway leaves exactly the orphan it was
    called to prevent. The caller runs this again on the next attempt, so anything
    survived is retried, not lost.
    """
    ec2 = aws_service._get_ec2_pinned(region)
    removed = []
    filters = _env_filter(env_id)

    ids = [i["InstanceId"] for i in _describe_sync(region, filters)]
    if ids:
        ec2.terminate_instances(InstanceIds=ids)
        removed.extend(ids)
        # The network cannot come apart while an ENI is still attached, and an instance
        # holds its ENI well past the terminate call returning.
        ec2.get_waiter("instance_terminated").wait(
            InstanceIds=ids, WaiterConfig={"Delay": 15, "MaxAttempts": 40})

    for sg in ec2.describe_security_groups(Filters=filters).get("SecurityGroups") or []:
        try:
            ec2.delete_security_group(GroupId=sg["GroupId"])
            removed.append(sg["GroupId"])
        except Exception:  # noqa: BLE001
            logger.warning("POV %s: could not delete %s", env_id, sg["GroupId"],
                           exc_info=True)

    vpcs = ec2.describe_vpcs(Filters=filters).get("Vpcs") or []
    for igw in (ec2.describe_internet_gateways(Filters=filters)
                   .get("InternetGateways") or []):
        for att in igw.get("Attachments") or []:
            try:
                ec2.detach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"],
                                            VpcId=att["VpcId"])
            except Exception:  # noqa: BLE001
                logger.warning("POV %s: could not detach %s", env_id,
                               igw["InternetGatewayId"], exc_info=True)
        try:
            ec2.delete_internet_gateway(InternetGatewayId=igw["InternetGatewayId"])
            removed.append(igw["InternetGatewayId"])
        except Exception:  # noqa: BLE001
            logger.warning("POV %s: could not delete %s", env_id,
                           igw["InternetGatewayId"], exc_info=True)

    for subnet in ec2.describe_subnets(Filters=filters).get("Subnets") or []:
        try:
            ec2.delete_subnet(SubnetId=subnet["SubnetId"])
            removed.append(subnet["SubnetId"])
        except Exception:  # noqa: BLE001
            logger.warning("POV %s: could not delete %s", env_id, subnet["SubnetId"],
                           exc_info=True)

    for vpc in vpcs:
        try:
            # The main route table goes with the VPC; deleting it first is an error, not
            # tidiness, which is why it is tagged but never deleted on its own.
            ec2.delete_vpc(VpcId=vpc["VpcId"])
            removed.append(vpc["VpcId"])
        except Exception:  # noqa: BLE001
            logger.warning("POV %s: could not delete %s", env_id, vpc["VpcId"],
                           exc_info=True)

    return removed


async def delete_environment(env_id: str, region: str) -> None:
    removed = await aws_service._to_thread(_delete_sync, env_id, region)
    # After the instances, never before: a teardown that fails partway is retried, and a
    # surviving Windows VM is still one somebody may need to log into.
    _forget_windows_key(env_id)
    _forget_linux_login(env_id)
    logger.info("POV %s: removed %d AWS resources from %s", env_id, len(removed), region)
