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

import logging

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

def _verify_sync(region: str):
    ec2 = aws_service._get_ec2(region)
    resp = ec2.describe_regions(RegionNames=[region])
    return len(resp.get("Regions") or [])


async def verify() -> tuple[bool, str]:
    """Prove the credential, cheaply, without a side effect.

    An expected failure is a RETURN VALUE, not an exception — a caught exception
    stringified into a response body is what CodeQL's stack-trace-exposure rule fires on,
    and this codebase learned that the fix is structural rather than a wider except.
    """
    region = default_region()
    if not configured():
        return False, ("No AWS credentials are configured. Add an access key in "
                       "Settings → Integrations → AWS.")
    try:
        count = await aws_service._to_thread(_verify_sync, region)
    except AWSError as exc:
        return False, f"AWS refused the call: {exc}"
    except Exception:  # noqa: BLE001
        logger.warning("AWS POV verify failed", exc_info=True)
        return False, ("AWS did not answer. Check the access key, its permissions and "
                       f"that {region} is enabled for this account.")
    if not count:
        return False, f"The credential works but cannot see region {region}."
    return True, f"Connected to AWS in {region}."


# ── create ───────────────────────────────────────────────────────────────────

def _create_network_sync(env_id: str, region: str, cidr: str, sub_cidr: str) -> dict:
    ec2 = aws_service._get_ec2(region)

    vpc = ec2.create_vpc(CidrBlock=cidr,
                         TagSpecifications=[_tag_spec("vpc", env_id, env_id)])
    vpc_id = vpc["Vpc"]["VpcId"]
    # Without this the guests have no resolvable names, which breaks the Ansible
    # inventory the config-management path builds from them.
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

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
        Description=f"POV environment {env_id} — outbound only, plus intra-environment",
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


async def create_network(env_id: str, region: str, cidr: str, sub_cidr: str) -> dict:
    return await aws_service._to_thread(_create_network_sync, env_id, region, cidr,
                                        sub_cidr)


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
    ec2 = aws_service._get_ec2(region)
    created = []
    for spec in specs:
        image_id = spec["image_id"]
        device, minimum = _root_device(ec2, image_id)
        size = max(int(spec.get("disk_gb") or env.DEFAULT_DISK_GB), minimum)
        kwargs = {
            "ImageId": image_id,
            "InstanceType": spec["instance_type"],
            "MinCount": 1,
            "MaxCount": 1,
            "SubnetId": network["subnet_id"],
            "SecurityGroupIds": [network["security_group_id"]],
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
        resp = ec2.run_instances(**kwargs)
        inst = resp["Instances"][0]
        created.append({"id": inst["InstanceId"], "name": spec["name"]})
        logger.info("POV %s: launched %s as %s", env_id, spec["name"],
                    inst["InstanceId"])
    return created


async def create_vms(env_id: str, region: str, specs: list, network: dict) -> list:
    return await aws_service._to_thread(_create_vms_sync, env_id, region, specs, network)


# ── read ─────────────────────────────────────────────────────────────────────

def _vm(inst: dict) -> dict:
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
        # No NAT-ed guest ports on a cloud: access is PRA, through the POV's own Gateway.
        "published_services": [],
    }


def _environment(env_id: str, instances: list, region: str) -> dict:
    vms = [_vm(i) for i in instances]
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
    ec2 = aws_service._get_ec2(region)
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


async def read_environment(env_id: str, region: str):
    """The environment, or ``None`` when nothing carries its tag.

    ``None`` rather than a refusal: the caller distinguishes "gone" from "broken", and
    ``pov_reconcile`` only marks an environment missing on two independent signals.
    """
    instances = await aws_service._to_thread(_describe_sync, region, _env_filter(env_id))
    if not instances:
        return None
    return _environment(env_id, instances, region)


async def list_environments(region: str) -> list:
    """Every POV environment this credential can see in one region, tag-grouped."""
    instances = await aws_service._to_thread(
        _describe_sync, region,
        [{"Name": f"tag:{env.TAG_MANAGED_BY}", "Values": [env.MANAGED_BY]}])
    grouped: dict = {}
    for inst in instances:
        tags = {t["Key"]: t["Value"] for t in inst.get("Tags") or []}
        env_id = tags.get(env.TAG_ENVIRONMENT) or ""
        if env_id:
            grouped.setdefault(env_id, []).append(inst)
    return [_environment(eid, rows, region) for eid, rows in sorted(grouped.items())]


# ── power ────────────────────────────────────────────────────────────────────

def _power_sync(env_id: str, region: str, action: str) -> int:
    ec2 = aws_service._get_ec2(region)
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


# ── teardown ─────────────────────────────────────────────────────────────────

def _delete_sync(env_id: str, region: str) -> list:
    """Terminate the instances, then unpick the network they sat in.

    Ordered and tolerant. Every step is scoped to the environment tag, every step skips
    what is already gone, and a step that fails is logged and stepped over rather than
    stopping the rest — a teardown that aborts halfway leaves exactly the orphan it was
    called to prevent. The caller runs this again on the next attempt, so anything
    survived is retried, not lost.
    """
    ec2 = aws_service._get_ec2(region)
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
    logger.info("POV %s: removed %d AWS resources from %s", env_id, len(removed), region)
