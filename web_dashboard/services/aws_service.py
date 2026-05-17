"""
AWS service wrapper using boto3.
All blocking calls are run in a thread pool via asyncio.to_thread() to keep
the FastAPI event loop free (same pattern as services/powershell.py).
"""
import asyncio
import base64
import json
from typing import Optional
from datetime import datetime, timezone

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError, BotoCoreError
    _boto3_available = True
except ImportError:
    _boto3_available = False


class AWSError(Exception):
    """Raised when an AWS operation fails."""


def _require_boto3():
    if not _boto3_available:
        raise AWSError("boto3 is not installed. Run: pip install boto3")


def _aws_kwargs(region: str) -> dict:
    """Build boto3 client kwargs, preferring config_service (DB) over env vars.

    Explicit credential kwargs are passed so boto3 doesn't fall back to the
    environment or instance metadata — the wizard is the authoritative source.
    """
    import os
    try:
        from . import config_service
        key_id = config_service.get("aws_access_key_id") or os.environ.get("AWS_ACCESS_KEY_ID", "")
        secret  = config_service.get("aws_secret_access_key") or os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        region  = config_service.get("aws_region") or region or os.environ.get("AWS_REGION", "us-east-2")
    except Exception:
        key_id = os.environ.get("AWS_ACCESS_KEY_ID", "")
        secret  = os.environ.get("AWS_SECRET_ACCESS_KEY", "")

    kwargs: dict = {"region_name": region}
    if key_id and secret:
        kwargs["aws_access_key_id"]     = key_id
        kwargs["aws_secret_access_key"] = secret
    return kwargs


def _get_ec2(region: str):
    _require_boto3()
    return boto3.client("ec2", **_aws_kwargs(region))


def _get_sm(region: str):
    _require_boto3()
    return boto3.client("secretsmanager", **_aws_kwargs(region))


# ── Secrets Manager ────────────────────────────────────────────────────────────

def _get_secret_sync(secret_name: str, region: str) -> str:
    _require_boto3()
    sm = boto3.client("secretsmanager", **_aws_kwargs(region))
    resp = sm.get_secret_value(SecretId=secret_name)
    if "SecretString" in resp:
        return resp["SecretString"]
    return base64.b64decode(resp["SecretBinary"]).decode()


async def get_secret(secret_name: str, region: str) -> str:
    """Retrieve a plaintext secret string from AWS Secrets Manager."""
    return await asyncio.to_thread(_get_secret_sync, secret_name, region)


async def get_keypair_private_key(region: str, key_name: str) -> str:
    """Fetch the private key PEM for an EC2 key pair from Secrets Manager.

    Naming convention: store the .pem contents as a Secrets Manager secret
    named  ec2/keypairs/<key-name>  (e.g. ec2/keypairs/my-ec2-key).
    """
    secret_name = f"ec2/keypairs/{key_name}"
    try:
        return await asyncio.to_thread(_get_secret_sync, secret_name, region)
    except Exception as e:
        raise AWSError(
            f"Private key not found in Secrets Manager. "
            f"Expected secret name: '{secret_name}'. "
            f"Store the .pem contents there to enable SSH key retrieval. "
            f"Original error: {e}"
        ) from e


def _list_ssh_key_secrets_sync(region: str, prefix: str) -> list:
    """List Secrets Manager secrets whose names start with *prefix*."""
    sm = _get_sm(region)
    secrets = []
    kwargs: dict = {"Filters": [{"Key": "name", "Values": [prefix]}], "MaxResults": 100}
    while True:
        resp = sm.list_secrets(**kwargs)
        for s in resp.get("SecretList", []):
            last_changed = s.get("LastChangedDate") or s.get("CreatedDate")
            secrets.append({
                "name": s["Name"],
                "description": s.get("Description", ""),
                "last_changed": last_changed.isoformat() if last_changed else "",
            })
        next_token = resp.get("NextToken")
        if not next_token:
            break
        kwargs["NextToken"] = next_token
    return secrets


async def get_ssh_key_secrets(region: str, prefix: str) -> list:
    """Return all Secrets Manager secrets whose names start with *prefix*."""
    try:
        return await asyncio.to_thread(_list_ssh_key_secrets_sync, region, prefix)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to list SSH key secrets: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _get_ssh_public_key_from_secret_sync(region: str, secret_name: str) -> dict:
    """Retrieve the SSH public key stored in a Secrets Manager secret.

    Supports two secret formats:
    - JSON: {"public_key": "ssh-rsa AAAA...", "description": "optional"}
    - Plain string: the raw public key itself
    """
    raw = _get_secret_sync(secret_name, region)
    try:
        data = json.loads(raw)
        public_key = data.get("public_key", raw)
        description = data.get("description", "")
    except (json.JSONDecodeError, AttributeError):
        public_key = raw
        description = ""
    return {"name": secret_name, "public_key": public_key.strip(), "description": description}


async def get_ssh_public_key_from_secret(region: str, secret_name: str) -> dict:
    """Fetch and return the SSH public key from a Secrets Manager secret."""
    try:
        return await asyncio.to_thread(_get_ssh_public_key_from_secret_sync, region, secret_name)
    except AWSError:
        raise
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to retrieve SSH key secret '{secret_name}': {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── AMI operations ─────────────────────────────────────────────────────────────

def _list_amis_sync(region: str) -> list:
    ec2 = _get_ec2(region)
    resp = ec2.describe_images(Owners=["self"])
    images = resp.get("Images", [])
    # Sort newest first
    images.sort(key=lambda x: x.get("CreationDate", ""), reverse=True)
    return [_format_ami(img) for img in images]


def _format_ami(img: dict) -> dict:
    name = img.get("Name", "")
    tags = {t["Key"]: t["Value"] for t in img.get("Tags", [])}
    return {
        "ami_id": img["ImageId"],
        "name": name or tags.get("Name", img["ImageId"]),
        "description": img.get("Description", ""),
        "state": img.get("State", ""),
        "creation_date": img.get("CreationDate", ""),
        "architecture": img.get("Architecture", ""),
        "virtualization_type": img.get("VirtualizationType", ""),
        "root_device_type": img.get("RootDeviceType", ""),
        "platform": img.get("Platform", "linux"),
        "size_gb": sum(
            bdm.get("Ebs", {}).get("VolumeSize", 0)
            for bdm in img.get("BlockDeviceMappings", [])
        ),
        "ena_support": img.get("EnaSupport", False),
        "tags": tags,
    }


async def list_amis(region: str) -> list:
    """Return all AMIs owned by the account."""
    try:
        return await asyncio.to_thread(_list_amis_sync, region)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to list AMIs: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured. Check ~/.aws/credentials or environment variables.")


# ── EC2 instance operations ────────────────────────────────────────────────────

def _describe_instances_sync(region: str, instance_ids: list) -> list:
    if not instance_ids:
        return []
    ec2 = _get_ec2(region)
    resp = ec2.describe_instances(InstanceIds=instance_ids)
    instances = []
    for reservation in resp.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            instances.append(_format_instance(inst))
    return instances


def _format_instance(inst: dict) -> dict:
    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
    return {
        "instance_id": inst["InstanceId"],
        "name": tags.get("Name", inst["InstanceId"]),
        "instance_type": inst.get("InstanceType", ""),
        "state": inst.get("State", {}).get("Name", ""),
        "public_ip": inst.get("PublicIpAddress"),
        "private_ip": inst.get("PrivateIpAddress"),
        "ami_id": inst.get("ImageId", ""),
        "launch_time": inst.get("LaunchTime", "").isoformat() if inst.get("LaunchTime") else "",
        "availability_zone": inst.get("Placement", {}).get("AvailabilityZone", ""),
        "key_name": inst.get("KeyName"),
        "tags": tags,
    }


async def describe_instances(region: str, instance_ids: list) -> list:
    """Return live state for a list of instance IDs."""
    try:
        return await asyncio.to_thread(_describe_instances_sync, region, instance_ids)
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidInstanceID.NotFound":
            return []
        raise AWSError(f"Failed to describe instances: {e}") from e
    except (BotoCoreError, NoCredentialsError) as e:
        raise AWSError(f"AWS error: {e}") from e


# ── EC2 launch / terminate ────────────────────────────────────────────────────

def _build_userdata(public_key: str, os_type: str, region: str) -> str:
    """Build a cloud-config UserData string for the given OS type.

    Ensures the public key is a single line (guards against secrets stored
    with embedded newlines that would break the YAML structure).
    All Linux instances get SSM agent installation/activation so Session
    Manager works on private-IP-only instances.
    """
    # SSH public keys must be a single line; strip whitespace and collapse any
    # internal newlines that would produce malformed cloud-config YAML.
    clean_key = public_key.strip().replace("\r", "").replace("\n", "")

    ssm_base = f"https://s3.{region}.amazonaws.com/amazon-ssm-{region}/latest"

    if os_type in ("debian", "ubuntu"):
        ssm_deb_url = f"{ssm_base}/debian_amd64/amazon-ssm-agent.deb"
        runcmd = (
            "runcmd:\n"
            f"  - wget -q -O /tmp/ssm-agent.deb '{ssm_deb_url}'\n"
            "  - dpkg -i /tmp/ssm-agent.deb\n"
            "  - systemctl enable amazon-ssm-agent\n"
            "  - systemctl start amazon-ssm-agent\n"
        )
    elif os_type in ("rhel", "rocky", "almalinux", "fedora"):
        ssm_rpm_url = f"{ssm_base}/linux_amd64/amazon-ssm-agent.rpm"
        runcmd = (
            "runcmd:\n"
            f"  - dnf install -y '{ssm_rpm_url}' || yum install -y '{ssm_rpm_url}'\n"
            "  - systemctl enable amazon-ssm-agent\n"
            "  - systemctl start amazon-ssm-agent\n"
        )
    elif os_type == "amazon-linux":
        # Agent is pre-installed; ensure it's enabled and running.
        runcmd = (
            "runcmd:\n"
            "  - systemctl enable amazon-ssm-agent\n"
            "  - systemctl start amazon-ssm-agent\n"
        )
    else:
        runcmd = ""

    return (
        "#cloud-config\n"
        "ssh_authorized_keys:\n"
        f"  - {clean_key}\n"
        + (f"\n{runcmd}" if runcmd else "")
    )


def _launch_instance_sync(
    region: str,
    ami_id: str,
    instance_name: str,
    instance_type: str,
    public_key: str,
    subnet_id: str,
    security_group_ids: list,
    iam_instance_profile: str = "",
    os_type: str = "",
    workgroup: str = "",
) -> dict:
    ec2 = _get_ec2(region)
    tags = [
        {"Key": "Name", "Value": instance_name},
        {"Key": "ManagedBy", "Value": "vm-cli-dashboard"},
    ]
    if workgroup:
        tags.append({"Key": "Workgroup", "Value": workgroup})
    kwargs: dict = dict(
        ImageId=ami_id,
        InstanceType=instance_type,
        SubnetId=subnet_id,
        SecurityGroupIds=security_group_ids,
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": tags,
            }
        ],
    )
    if public_key:
        userdata = _build_userdata(public_key, os_type, region)
        kwargs["UserData"] = userdata  # boto3 base64-encodes blob types automatically
    if iam_instance_profile:
        kwargs["IamInstanceProfile"] = {"Name": iam_instance_profile}
    resp = ec2.run_instances(**kwargs)
    inst = resp["Instances"][0]
    return {
        "instance_id": inst["InstanceId"],
        "state": inst["State"]["Name"],
        "private_ip": inst.get("PrivateIpAddress"),
        "public_ip": inst.get("PublicIpAddress"),
    }


def _terminate_instance_sync(region: str, instance_id: str) -> dict:
    ec2 = _get_ec2(region)
    resp = ec2.terminate_instances(InstanceIds=[instance_id])
    state = resp["TerminatingInstances"][0]["CurrentState"]["Name"]
    return {"instance_id": instance_id, "state": state}


async def launch_instance(
    region: str,
    ami_id: str,
    instance_name: str,
    instance_type: str,
    public_key: str,
    subnet_id: str,
    security_group_ids: list,
    iam_instance_profile: str = "",
    os_type: str = "",
    workgroup: str = "",
) -> dict:
    """Launch a new EC2 instance and return its ID and initial state.

    *public_key* is injected into the instance via cloud-init UserData.
    Pass an empty string to skip key injection (e.g. for Windows AMIs).
    *iam_instance_profile* attaches an instance profile by name (e.g. for SSM access).
    *os_type* controls OS-specific UserData (e.g. SSM agent install for Debian).
    *workgroup*, when non-empty, is written as a `Workgroup=<name>` tag so the
    instance is discoverable per-workgroup by the dashboard and external tools.
    """
    try:
        return await asyncio.to_thread(
            _launch_instance_sync,
            region, ami_id, instance_name, instance_type,
            public_key, subnet_id, security_group_ids,
            iam_instance_profile, os_type, workgroup,
        )
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to launch instance: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


async def terminate_instance(region: str, instance_id: str) -> dict:
    """Terminate an EC2 instance."""
    try:
        return await asyncio.to_thread(_terminate_instance_sync, region, instance_id)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to terminate instance {instance_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _set_workgroup_tag_sync(region: str, instance_id: str, workgroup: str) -> None:
    """Overwrite the `Workgroup` tag on an existing instance."""
    ec2 = _get_ec2(region)
    ec2.create_tags(
        Resources=[instance_id],
        Tags=[{"Key": "Workgroup", "Value": workgroup}],
    )


async def set_workgroup_tag(region: str, instance_id: str, workgroup: str) -> None:
    """Rewrite (or create) the `Workgroup=<name>` tag on an EC2 instance.

    Used by the admin reassign endpoint. AWS `create_tags` is upsert semantics
    so this works whether or not the tag exists.
    """
    try:
        await asyncio.to_thread(_set_workgroup_tag_sync, region, instance_id, workgroup)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to set Workgroup tag on {instance_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── Network options (for deploy form dropdowns) ────────────────────────────────

def _get_network_options_sync(region: str) -> dict:
    ec2 = _get_ec2(region)

    raw_subnets = ec2.describe_subnets().get("Subnets", [])
    subnets = []
    for s in raw_subnets:
        tags = {t["Key"]: t["Value"] for t in s.get("Tags", [])}
        label = tags.get("Name", s["SubnetId"])
        subnets.append({
            "id": s["SubnetId"],
            "name": f"{label} ({s['SubnetId']}) – {s['CidrBlock']} – {s['AvailabilityZone']}",
            "vpc_id": s["VpcId"],
            "az": s["AvailabilityZone"],
            "cidr": s["CidrBlock"],
        })

    raw_sgs = ec2.describe_security_groups().get("SecurityGroups", [])
    security_groups = [
        {
            "id": sg["GroupId"],
            "name": f"{sg['GroupName']} ({sg['GroupId']})",
            "description": sg.get("Description", ""),
            "vpc_id": sg.get("VpcId", ""),
        }
        for sg in raw_sgs
    ]

    instance_types = [
        "t2.micro", "t2.small", "t2.medium", "t2.large",
        "t3.micro", "t3.small", "t3.medium", "t3.large", "t3.xlarge",
        "t3.2xlarge", "m5.large", "m5.xlarge", "m5.2xlarge",
        "c5.large", "c5.xlarge", "r5.large",
    ]

    return {
        "subnets": subnets,
        "security_groups": security_groups,
        "instance_types": instance_types,
    }


async def get_network_options(region: str) -> dict:
    """Return dropdowns for the deploy form: subnets, security groups, instance types."""
    try:
        return await asyncio.to_thread(_get_network_options_sync, region)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to fetch network options: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── Community AMI search ──────────────────────────────────────────────────────

# Well-known free-tier-compatible AMI owners and name patterns.
# AWS does not tag AMIs as "free tier" — eligibility is determined by instance
# type (t2.micro / t3.micro).  These owners publish the images most commonly
# used with free-tier instances.
_COMMUNITY_AMI_SOURCES = {
    "amazon-linux": {
        "owners": ["amazon"],
        "filters": [
            {"Name": "name", "Values": ["al2023-ami-*-x86_64", "amzn2-ami-hvm-*-x86_64-gp2"]},
            {"Name": "state", "Values": ["available"]},
            {"Name": "architecture", "Values": ["x86_64"]},
        ],
        "os_type": "amazon-linux",
    },
    "ubuntu": {
        "owners": ["099720109477"],   # Canonical
        "filters": [
            {"Name": "name", "Values": ["ubuntu/images/hvm-ssd/ubuntu-*-amd64-server-*",
                                        "ubuntu/images/hvm-ssd-gp3/ubuntu-*-amd64-server-*"]},
            {"Name": "state", "Values": ["available"]},
            {"Name": "architecture", "Values": ["x86_64"]},
        ],
        "os_type": "ubuntu",
    },
    "debian": {
        "owners": ["136693071363"],   # Debian
        "filters": [
            {"Name": "name", "Values": ["debian-*-amd64-*"]},
            {"Name": "state", "Values": ["available"]},
            {"Name": "architecture", "Values": ["x86_64"]},
        ],
        "os_type": "debian",
    },
}

_FREE_TIER_NOTE = "Compatible with t2.micro / t3.micro (free tier)"


def _search_community_amis_sync(region: str, os_filter: Optional[str]) -> list:
    ec2 = _get_ec2(region)
    sources = (
        [_COMMUNITY_AMI_SOURCES[os_filter]]
        if os_filter and os_filter in _COMMUNITY_AMI_SOURCES
        else list(_COMMUNITY_AMI_SOURCES.values())
    )

    results = []
    for src in sources:
        resp = ec2.describe_images(Owners=src["owners"], Filters=src["filters"])
        images = resp.get("Images", [])
        images.sort(key=lambda x: x.get("CreationDate", ""), reverse=True)
        for img in images[:20]:
            entry = _format_ami(img)
            entry["os_type"] = src["os_type"]
            entry["free_tier_note"] = _FREE_TIER_NOTE
            results.append(entry)

    results.sort(key=lambda x: x.get("creation_date", ""), reverse=True)
    return results


async def search_community_amis(region: str, os_filter: Optional[str] = None) -> list:
    """Return free-tier-compatible community AMIs (Amazon Linux, Ubuntu, Debian)."""
    try:
        return await asyncio.to_thread(_search_community_amis_sync, region, os_filter)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to search community AMIs: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _copy_ami_sync(
    region: str,
    source_ami_id: str,
    name: str,
    description: str,
) -> str:
    """Copy a public AMI into this account. Returns the new AMI ID (state: pending)."""
    ec2 = _get_ec2(region)
    resp = ec2.copy_image(
        Name=name,
        Description=description or f"Copied from {source_ami_id}",
        SourceImageId=source_ami_id,
        SourceRegion=region,
    )
    new_ami_id = resp["ImageId"]
    # Tag immediately so we can identify it later
    ec2.create_tags(
        Resources=[new_ami_id],
        Tags=[
            {"Key": "ManagedBy", "Value": "vm-cli-dashboard"},
            {"Key": "CopiedFrom", "Value": source_ami_id},
            {"Key": "Name", "Value": name},
        ],
    )
    return new_ami_id


async def copy_ami(
    region: str,
    source_ami_id: str,
    name: str,
    description: str = "",
) -> str:
    """Copy a community AMI into the account. Returns the new AMI ID."""
    try:
        return await asyncio.to_thread(_copy_ami_sync, region, source_ami_id, name, description)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to copy AMI {source_ami_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _get_ami_status_sync(region: str, ami_id: str) -> dict:
    ec2 = _get_ec2(region)
    resp = ec2.describe_images(ImageIds=[ami_id])
    images = resp.get("Images", [])
    if not images:
        return {"ami_id": ami_id, "state": "not-found", "name": ""}
    img = images[0]
    return {
        "ami_id": ami_id,
        "state": img.get("State", ""),
        "name": img.get("Name", ""),
        "state_reason": img.get("StateReason", {}).get("Message", ""),
    }


async def get_ami_status(region: str, ami_id: str) -> dict:
    """Poll the state of an AMI (used during copy to check for 'available')."""
    try:
        return await asyncio.to_thread(_get_ami_status_sync, region, ami_id)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to get AMI status for {ami_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── ECS Jumpoint task ─────────────────────────────────────────────────────────

def _get_ecs(region: str):
    _require_boto3()
    return boto3.client("ecs", **_aws_kwargs(region))


def _ensure_task_definition_sync(
    region: str,
    family: str,
    cpu: str,
    memory: str,
    execution_role_arn: str,
    image: str = "beyondtrust/sra-jumpoint",
) -> str:
    """Register the bt-jumpoint task definition if it doesn't already exist."""
    ecs = _get_ecs(region)
    try:
        resp = ecs.describe_task_definition(taskDefinition=family)
        return resp["taskDefinition"]["taskDefinitionArn"]
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("ClientException", "InvalidParameterException"):
            raise

    # Register a new task definition
    kwargs = {
        "family": family,
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": cpu,
        "memory": memory,
        "containerDefinitions": [
            {
                "name": "jumpoint",
                "image": image,
                "essential": True,
                "environment": [],  # DEPLOY_KEY passed as run-time override
            }
        ],
    }
    if execution_role_arn:
        kwargs["executionRoleArn"] = execution_role_arn

    resp = ecs.register_task_definition(**kwargs)
    return resp["taskDefinition"]["taskDefinitionArn"]


def _run_ecs_task_sync(
    region: str,
    cluster: str,
    task_family: str,
    subnet_id: str,
    security_group_ids: list,
    deploy_key: str,
    cpu: str,
    memory: str,
    execution_role_arn: str,
    image: str = "beyondtrust/sra-jumpoint",
) -> str:
    """Ensure the ECS cluster exists, register the task definition if needed,
    then launch one Fargate task. Returns the task ARN."""
    ecs = _get_ecs(region)

    # Ensure the ECS service-linked role exists (required before first ECS use in an account)
    try:
        iam = boto3.client("iam", **_aws_kwargs(region))
        iam.create_service_linked_role(AWSServiceName="ecs.amazonaws.com")
    except ClientError as e:
        # "InvalidInput" means the role already exists — that's fine
        if e.response["Error"]["Code"] != "InvalidInput":
            raise

    # Create cluster (idempotent — returns existing if already present)
    ecs.create_cluster(clusterName=cluster)

    task_def_arn = _ensure_task_definition_sync(
        region, task_family, cpu, memory, execution_role_arn, image
    )

    resp = ecs.run_task(
        cluster=cluster,
        taskDefinition=task_def_arn,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": [subnet_id],
                "securityGroups": security_group_ids,
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": "jumpoint",
                    "environment": [{"name": "DEPLOY_KEY", "value": deploy_key}],
                }
            ]
        },
        count=1,
    )

    tasks = resp.get("tasks", [])
    if not tasks:
        failures = resp.get("failures", [])
        raise AWSError(f"ECS task failed to start: {failures}")
    return tasks[0]["taskArn"]


def _stop_ecs_task_sync(region: str, cluster: str, task_arn: str) -> None:
    ecs = _get_ecs(region)
    ecs.stop_task(
        cluster=cluster,
        task=task_arn,
        reason="EC2 instance destroyed via Infrastructure Management Dashboard",
    )


async def run_ecs_jumpoint_task(
    region: str,
    cluster: str,
    task_family: str,
    subnet_id: str,
    security_group_ids: list,
    deploy_key: str,
    cpu: str = "256",
    memory: str = "512",
    execution_role_arn: str = "",
    image: str = "beyondtrust/sra-jumpoint",
) -> str:
    """Start an ECS Fargate task running the BeyondTrust Jumpoint container.
    Returns the task ARN."""
    try:
        return await asyncio.to_thread(
            _run_ecs_task_sync,
            region, cluster, task_family, subnet_id, security_group_ids,
            deploy_key, cpu, memory, execution_role_arn, image,
        )
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to start ECS Jumpoint task: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


async def stop_ecs_jumpoint_task(region: str, cluster: str, task_arn: str) -> None:
    """Stop a running ECS Jumpoint task."""
    try:
        await asyncio.to_thread(_stop_ecs_task_sync, region, cluster, task_arn)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to stop ECS task {task_arn}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── ECS task listing ──────────────────────────────────────────────────────────

def _list_ecs_tasks_sync(region: str, cluster: str, include_stopped: bool) -> list[dict]:
    ecs = _get_ecs(region)
    arns: list[str] = []
    statuses = ["RUNNING", "STOPPED"] if include_stopped else ["RUNNING"]
    for status in statuses:
        paginator = ecs.get_paginator("list_tasks")
        for page in paginator.paginate(cluster=cluster, desiredStatus=status):
            arns.extend(page.get("taskArns", []))
    if not arns:
        return []
    results: list[dict] = []
    for i in range(0, len(arns), 100):
        resp = ecs.describe_tasks(cluster=cluster, tasks=arns[i:i + 100])
        results.extend(resp.get("tasks", []))
    return results


async def list_ecs_tasks(region: str, cluster: str, include_stopped: bool = False) -> list[dict]:
    """List ECS tasks in the given cluster."""
    try:
        return await asyncio.to_thread(_list_ecs_tasks_sync, region, cluster, include_stopped)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to list ECS tasks: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── ECS Ansible runner ────────────────────────────────────────────────────────

def _run_ecs_ansible_sync(
    region: str,
    cluster: str,
    task_family: str,
    image: str,
    cpu: str,
    memory: str,
    subnet_id: str,
    security_group_ids: list,
    execution_role_arn: str,
    target_ip: str,
    ansible_user: str,
    playbook_b64: str,
    ssh_key_b64: str,
    job_id: str,
) -> tuple:
    """Create an ECS Fargate task that runs one Ansible playbook, wait for it to
    finish, retrieve CloudWatch logs, and return (exit_code, output)."""
    import time
    ecs = _get_ecs(region)
    logs_client = boto3.client("logs", region_name=region)
    log_group = "/ecs/ansible-runner"
    log_stream_prefix = f"ansible/{job_id[:8]}"

    try:
        logs_client.create_log_group(logGroupName=log_group)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise

    cmd = (
        "set -e && "
        'echo "$PLAYBOOK_B64" | base64 -d > /tmp/playbook.yml && '
        'echo "$SSH_KEY_B64" | base64 -d > /tmp/ssh_key && '
        "chmod 600 /tmp/ssh_key && "
        f"ansible-playbook -i '{target_ip},' --forks 1 "
        f"-u {ansible_user} --private-key /tmp/ssh_key "
        "--ssh-extra-args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null' "
        "/tmp/playbook.yml"
    )

    td_kwargs: dict = dict(
        family=task_family,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu=str(cpu),
        memory=str(memory),
        containerDefinitions=[{
            "name": "ansible",
            "image": image,
            "essential": True,
            "command": ["sh", "-c", cmd],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": log_group,
                    "awslogs-region": region,
                    "awslogs-stream-prefix": log_stream_prefix,
                },
            },
        }],
    )
    if execution_role_arn:
        td_kwargs["executionRoleArn"] = execution_role_arn

    td_resp = ecs.register_task_definition(**td_kwargs)
    task_def_arn = td_resp["taskDefinition"]["taskDefinitionArn"]

    ecs.create_cluster(clusterName=cluster)

    run_resp = ecs.run_task(
        cluster=cluster,
        taskDefinition=task_def_arn,
        launchType="FARGATE",
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": [subnet_id] if subnet_id else [],
            "securityGroups": security_group_ids or [],
            "assignPublicIp": "DISABLED" if subnet_id else "ENABLED",
        }},
        overrides={"containerOverrides": [{
            "name": "ansible",
            "environment": [
                {"name": "PLAYBOOK_B64", "value": playbook_b64},
                {"name": "SSH_KEY_B64", "value": ssh_key_b64},
            ],
        }]},
        count=1,
    )

    tasks = run_resp.get("tasks", [])
    if not tasks:
        raise AWSError(f"ECS ansible task failed to start: {run_resp.get('failures', [])}")

    task_arn = tasks[0]["taskArn"]
    task_id = task_arn.split("/")[-1]

    # Poll until stopped (max 20 min)
    exit_code = 1
    for _ in range(120):
        desc = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])
        t = desc.get("tasks", [{}])[0]
        if t.get("lastStatus") == "STOPPED":
            for c in t.get("containers", []):
                if c.get("name") == "ansible":
                    ec = c.get("exitCode")
                    exit_code = ec if ec is not None else 1
                    break
            break
        time.sleep(10)

    # Retrieve CloudWatch logs
    output = ""
    try:
        log_stream = f"{log_stream_prefix}/ansible/{task_id}"
        log_resp = logs_client.get_log_events(
            logGroupName=log_group,
            logStreamName=log_stream,
            startFromHead=True,
        )
        output = "\n".join(e["message"] for e in log_resp.get("events", []))
    except Exception as log_err:
        logger.warning("ECS Ansible: could not retrieve logs: %s", log_err)

    return exit_code, output


async def run_ecs_ansible_task(
    region: str,
    cluster: str,
    task_family: str,
    image: str,
    cpu: str,
    memory: str,
    subnet_id: str,
    security_group_ids: list,
    execution_role_arn: str,
    target_ip: str,
    ansible_user: str,
    playbook_b64: str,
    ssh_key_b64: str,
    job_id: str,
) -> tuple:
    """Run an Ansible playbook via ECS Fargate. Returns (exit_code, output)."""
    try:
        return await asyncio.to_thread(
            _run_ecs_ansible_sync,
            region, cluster, task_family, image, cpu, memory,
            subnet_id, security_group_ids, execution_role_arn,
            target_ip, ansible_user, playbook_b64, ssh_key_b64, job_id,
        )
    except AWSError:
        raise
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to run ECS Ansible task: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _describe_ami_sync(region: str, ami_id: str) -> dict:
    ec2 = _get_ec2(region)
    resp = ec2.describe_images(ImageIds=[ami_id])
    images = resp.get("Images", [])
    if not images:
        return {"name": ami_id, "description": ""}
    img = images[0]
    return {
        "name": img.get("Name", ami_id),
        "description": img.get("Description", ""),
        "platform": img.get("PlatformDetails", ""),
    }


async def describe_ami(region: str, ami_id: str) -> dict:
    """Return name and description for a single AMI."""
    try:
        return await asyncio.to_thread(_describe_ami_sync, region, ami_id)
    except (ClientError, BotoCoreError) as e:
        return {"name": ami_id, "description": ""}
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _describe_instance_detail_sync(region: str, instance_id: str) -> dict:
    """Return subnet_id and security_group_ids for a specific instance."""
    ec2 = _get_ec2(region)
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    reservations = resp.get("Reservations", [])
    if not reservations:
        return {}
    instance = reservations[0]["Instances"][0]
    return {
        "subnet_id": instance.get("SubnetId", ""),
        "security_group_ids": [sg["GroupId"] for sg in instance.get("SecurityGroups", [])],
    }


async def describe_instance_detail(region: str, instance_id: str) -> dict:
    """Return subnet_id and security_group_ids for a running instance."""
    try:
        return await asyncio.to_thread(_describe_instance_detail_sync, region, instance_id)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to describe instance {instance_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _deregister_ami_sync(region: str, ami_id: str) -> list[str]:
    """Deregister an AMI and delete its backing snapshots. Returns list of deleted snapshot IDs."""
    ec2 = _get_ec2(region)

    # Collect snapshot IDs before deregistering
    resp = ec2.describe_images(ImageIds=[ami_id], Owners=["self"])
    images = resp.get("Images", [])
    if not images:
        raise AWSError(f"AMI {ami_id} not found or not owned by this account.")

    snapshot_ids = [
        mapping["Ebs"]["SnapshotId"]
        for mapping in images[0].get("BlockDeviceMappings", [])
        if "Ebs" in mapping and "SnapshotId" in mapping["Ebs"]
    ]

    ec2.deregister_image(ImageId=ami_id)

    deleted_snapshots = []
    for snap_id in snapshot_ids:
        try:
            ec2.delete_snapshot(SnapshotId=snap_id)
            deleted_snapshots.append(snap_id)
        except ClientError:
            pass  # Snapshot may already be deleted or shared — skip silently

    return deleted_snapshots


async def deregister_ami(region: str, ami_id: str) -> list[str]:
    """Deregister an AMI and delete its backing EBS snapshots."""
    try:
        return await asyncio.to_thread(_deregister_ami_sync, region, ami_id)
    except AWSError:
        raise
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to deregister AMI {ami_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── Enable ENA on an AMI ──────────────────────────────────────────────────────

def _register_with_ena_sync(region: str, ami_id: str) -> str:
    """Re-register an AMI from the same backing snapshot(s) with EnaSupport=True.
    Returns the new AMI ID. The original AMI is left intact."""
    ec2 = _get_ec2(region)

    resp = ec2.describe_images(ImageIds=[ami_id], Owners=["self"])
    images = resp.get("Images", [])
    if not images:
        raise AWSError(f"AMI {ami_id} not found or not owned by this account.")

    img = images[0]
    if img.get("EnaSupport"):
        raise AWSError(f"AMI {ami_id} already has ENA enabled.")

    # Build block device mappings — keep only fields accepted by register_image
    bdms = []
    for bdm in img.get("BlockDeviceMappings", []):
        new_bdm: dict = {"DeviceName": bdm["DeviceName"]}
        if "Ebs" in bdm:
            ebs = bdm["Ebs"]
            new_ebs: dict = {}
            if "SnapshotId" in ebs:
                new_ebs["SnapshotId"] = ebs["SnapshotId"]
            if "VolumeSize" in ebs:
                new_ebs["VolumeSize"] = ebs["VolumeSize"]
            if "VolumeType" in ebs:
                new_ebs["VolumeType"] = ebs["VolumeType"]
            if "DeleteOnTermination" in ebs:
                new_ebs["DeleteOnTermination"] = ebs["DeleteOnTermination"]
            if ebs.get("Encrypted"):
                new_ebs["Encrypted"] = True
            new_bdm["Ebs"] = new_ebs
        elif "VirtualName" in bdm:
            new_bdm["VirtualName"] = bdm["VirtualName"]
        bdms.append(new_bdm)

    old_name = img.get("Name", ami_id)
    new_name = (old_name[:124] + "-ena") if len(old_name) > 124 else (old_name + "-ena")

    kwargs: dict = {
        "Name": new_name,
        "Architecture": img.get("Architecture", "x86_64"),
        "RootDeviceName": img.get("RootDeviceName", "/dev/sda1"),
        "BlockDeviceMappings": bdms,
        "VirtualizationType": img.get("VirtualizationType", "hvm"),
        "EnaSupport": True,
    }
    if img.get("Description"):
        kwargs["Description"] = img["Description"] + " (ENA enabled)"
    if img.get("KernelId"):
        kwargs["KernelId"] = img["KernelId"]
    if img.get("RamdiskId"):
        kwargs["RamdiskId"] = img["RamdiskId"]

    new_ami_id = ec2.register_image(**kwargs)["ImageId"]

    ec2.create_tags(
        Resources=[new_ami_id],
        Tags=[
            {"Key": "Name", "Value": new_name},
            {"Key": "ManagedBy", "Value": "vm-cli-dashboard"},
            {"Key": "SourceAMI", "Value": ami_id},
        ],
    )
    return new_ami_id


async def enable_ena_support(region: str, ami_id: str) -> str:
    """Re-register an AMI with EnaSupport=True (same backing snapshot, new AMI ID).
    Returns the new AMI ID. The original AMI is left intact."""
    try:
        return await asyncio.to_thread(_register_with_ena_sync, region, ami_id)
    except AWSError:
        raise
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to enable ENA on {ami_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── Create image from running instance ────────────────────────────────────────

def _create_image_sync(
    region: str,
    instance_id: str,
    name: str,
    description: str,
    no_reboot: bool,
) -> str:
    """Create an AMI from a running instance. Returns the new AMI ID (state: pending)."""
    ec2 = _get_ec2(region)
    resp = ec2.create_image(
        InstanceId=instance_id,
        Name=name,
        Description=description or f"Created from {instance_id}",
        NoReboot=no_reboot,
        TagSpecifications=[
            {
                "ResourceType": "image",
                "Tags": [
                    {"Key": "Name", "Value": name},
                    {"Key": "ManagedBy", "Value": "vm-cli-dashboard"},
                    {"Key": "SourceInstance", "Value": instance_id},
                ],
            }
        ],
    )
    return resp["ImageId"]


async def create_image_from_instance(
    region: str,
    instance_id: str,
    name: str,
    description: str = "",
    no_reboot: bool = True,
) -> str:
    """Create an AMI from a running EC2 instance. Returns the new AMI ID."""
    try:
        return await asyncio.to_thread(
            _create_image_sync, region, instance_id, name, description, no_reboot
        )
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to create image from {instance_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── Export AMI to VHD (portable artefact for cross-cloud promotion) ───────────

def _export_image_to_vhd_sync(
    region: str,
    ami_id: str,
    s3_bucket: str,
    s3_prefix: str,
    role_name: str,
    description: str,
    poll_interval: int,
    timeout: int,
    progress_cb,
) -> dict:
    """Trigger ec2:ExportImage for an AMI and poll until completion.

    Requires the `vmimport` IAM service role (or one named via role_name) with
    trust policy permitting vmie.amazonaws.com — see
    https://docs.aws.amazon.com/vm-import/latest/userguide/required-permissions.html

    Returns {task_id, s3_url, format}.
    """
    import time
    ec2 = _get_ec2(region)

    prefix = s3_prefix.rstrip("/") + "/" if s3_prefix and not s3_prefix.endswith("/") else (s3_prefix or "")
    if progress_cb:
        progress_cb(f"Starting AWS export-image for {ami_id} → s3://{s3_bucket}/{prefix}")

    resp = ec2.export_image(
        ImageId=ami_id,
        DiskImageFormat="VHD",
        S3ExportLocation={"S3Bucket": s3_bucket, "S3Prefix": prefix},
        RoleName=role_name,
        Description=description or f"Exported by vm-cli-dashboard from {ami_id}",
    )
    task_id = resp["ExportImageTaskId"]
    if progress_cb:
        progress_cb(f"Export task created: {task_id}")

    started = time.time()
    last_progress = ""
    while True:
        tasks = ec2.describe_export_image_tasks(ExportImageTaskIds=[task_id]).get("ExportImageTasks", [])
        if not tasks:
            raise AWSError(f"Export task {task_id} disappeared")
        task = tasks[0]
        status = (task.get("Status") or "").lower()
        msg = task.get("StatusMessage") or task.get("Progress") or ""
        if msg and msg != last_progress and progress_cb:
            progress_cb(f"Export {task_id}: {status} ({msg})")
            last_progress = msg

        if status == "completed":
            s3_url = f"s3://{s3_bucket}/{prefix}{task_id}.vhd"
            if progress_cb:
                progress_cb(f"Export complete: {s3_url}")
            return {"task_id": task_id, "s3_url": s3_url, "format": "vhd"}
        if status in ("cancelled", "deleted"):
            raise AWSError(f"Export task {task_id} ended in state '{status}': {msg}")
        if status == "failed":
            raise AWSError(f"Export task {task_id} failed: {msg}")

        if time.time() - started > timeout:
            raise AWSError(f"Export task {task_id} timed out after {timeout}s (last status: {status})")

        time.sleep(poll_interval)


async def export_image_to_vhd(
    region: str,
    ami_id: str,
    s3_bucket: str,
    s3_prefix: str = "exports/",
    role_name: str = "vmimport",
    description: str = "",
    poll_interval: int = 30,
    timeout: int = 7200,
    progress_cb=None,
) -> dict:
    """Export an AMI to a VHD in S3. Returns {task_id, s3_url, format}.

    progress_cb is an optional sync callable taking a single string for
    streaming status into a Job log.
    """
    try:
        return await asyncio.to_thread(
            _export_image_to_vhd_sync,
            region, ami_id, s3_bucket, s3_prefix, role_name,
            description, poll_interval, timeout, progress_cb,
        )
    except AWSError:
        raise
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to export {ami_id} to VHD: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── Import VHD from S3 (cross-cloud promote target side) ─────────────────────

def _import_image_from_vhd_sync(
    region: str,
    s3_bucket: str,
    s3_key: str,
    role_name: str,
    description: str,
    disk_format: str,
    poll_interval: int,
    timeout: int,
    progress_cb,
) -> dict:
    """Trigger ec2:ImportImage from an S3 object and poll until the import
    completes. Returns {task_id, image_id, status}. Mirrors
    `_export_image_to_vhd_sync`'s polling shape so the promote-flow Job sees
    matching status lines for both directions.

    The `vmimport` IAM service role (or whatever `role_name` points at) must
    trust vmie.amazonaws.com and have s3:GetObject on the bucket/key — same
    role used by export-image. AWS docs:
    https://docs.aws.amazon.com/vm-import/latest/userguide/required-permissions.html
    """
    import time
    ec2 = _get_ec2(region)

    if progress_cb:
        progress_cb(f"Starting AWS import-image from s3://{s3_bucket}/{s3_key} ({disk_format})")

    resp = ec2.import_image(
        Description=description or f"Imported by vm-cli-dashboard from s3://{s3_bucket}/{s3_key}",
        DiskContainers=[{
            "Description": description or "promote target",
            "Format": disk_format.upper(),
            "UserBucket": {"S3Bucket": s3_bucket, "S3Key": s3_key},
        }],
        RoleName=role_name,
    )
    task_id = resp["ImportTaskId"]
    if progress_cb:
        progress_cb(f"Import task created: {task_id}")

    started = time.time()
    last_progress = ""
    while True:
        tasks = ec2.describe_import_image_tasks(ImportTaskIds=[task_id]).get("ImportImageTasks", [])
        if not tasks:
            raise AWSError(f"Import task {task_id} disappeared")
        task = tasks[0]
        status = (task.get("Status") or "").lower()
        msg = task.get("StatusMessage") or task.get("Progress") or ""
        if msg and msg != last_progress and progress_cb:
            progress_cb(f"Import {task_id}: {status} ({msg})")
            last_progress = msg

        if status == "completed":
            image_id = task.get("ImageId")
            if progress_cb:
                progress_cb(f"Import complete: {image_id}")
            return {"task_id": task_id, "image_id": image_id, "status": status}
        if status in ("cancelled", "deleted", "cancelling", "deleting"):
            raise AWSError(f"Import task {task_id} ended in state '{status}': {msg}")
        if status == "failed":
            raise AWSError(f"Import task {task_id} failed: {msg}")

        if time.time() - started > timeout:
            raise AWSError(f"Import task {task_id} timed out after {timeout}s (last status: {status})")

        time.sleep(poll_interval)


async def import_image_from_vhd(
    region: str,
    s3_bucket: str,
    s3_key: str,
    role_name: str = "vmimport",
    description: str = "",
    disk_format: str = "vhd",
    poll_interval: int = 30,
    timeout: int = 7200,
    progress_cb=None,
) -> dict:
    """Import a VHD (or other supported format) from S3 into a new AMI.
    Returns {task_id, image_id, status}. Polls until terminal state."""
    try:
        return await asyncio.to_thread(
            _import_image_from_vhd_sync,
            region, s3_bucket, s3_key, role_name, description,
            disk_format, poll_interval, timeout, progress_cb,
        )
    except AWSError:
        raise
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to import s3://{s3_bucket}/{s3_key}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── ECS Promote-runner task ──────────────────────────────────────────────────

def _run_promote_runner_ecs_sync(
    region: str,
    cluster: str,
    task_family: str,
    image: str,
    cpu: str,
    memory: str,
    subnet_id: str,
    security_group_ids: list,
    execution_role_arn: str,
    task_role_arn: str,
    runner_args: list,
    job_id: str,
    poll_seconds_max: int = 7200,
) -> tuple:
    """Launch the promote-runner ECS Fargate task and wait for it to stop.
    Returns (exit_code, log_output).

    Modelled on `_run_ecs_ansible_sync` — same task-def-register / run-task /
    poll-describe-tasks / pull-CloudWatch-logs shape. The runner image,
    command-line args, and IAM are the only meaningful differences.

    runner_args is the argv list passed to the container's entrypoint
    (e.g. ["--source-url", "https://…", "--target", "s3", ...]). The
    dashboard pre-signs the source URL and assembles the args list; we
    don't validate the shape here.
    """
    import time
    ecs = _get_ecs(region)
    logs_client = boto3.client("logs", **_aws_kwargs(region))
    log_group = "/ecs/promote-runner"
    log_stream_prefix = f"promote/{job_id[:8]}"

    try:
        logs_client.create_log_group(logGroupName=log_group)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise

    td_kwargs: dict = dict(
        family=task_family,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu=str(cpu),
        memory=str(memory),
        containerDefinitions=[{
            "name": "promote-runner",
            "image": image,
            "essential": True,
            # ECS Fargate launches the container's entrypoint with this argv
            # appended. Our Dockerfile sets ENTRYPOINT to the python script,
            # so `command` here becomes argparse argv.
            "command": list(runner_args),
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": log_group,
                    "awslogs-region": region,
                    "awslogs-stream-prefix": log_stream_prefix,
                },
            },
        }],
    )
    if execution_role_arn:
        td_kwargs["executionRoleArn"] = execution_role_arn
    if task_role_arn:
        # The runner uses this role for S3 write to the staging bucket.
        td_kwargs["taskRoleArn"] = task_role_arn

    td_resp = ecs.register_task_definition(**td_kwargs)
    task_def_arn = td_resp["taskDefinition"]["taskDefinitionArn"]

    ecs.create_cluster(clusterName=cluster)

    run_resp = ecs.run_task(
        cluster=cluster,
        taskDefinition=task_def_arn,
        launchType="FARGATE",
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": [subnet_id] if subnet_id else [],
            "securityGroups": security_group_ids or [],
            # Public IP is needed to reach the presigned source URL when the
            # subnet doesn't have a NAT gateway. Operators with a NAT can
            # switch this off via subnet routing.
            "assignPublicIp": "ENABLED",
        }},
        count=1,
    )

    tasks = run_resp.get("tasks", [])
    if not tasks:
        raise AWSError(f"ECS promote-runner task failed to start: {run_resp.get('failures', [])}")
    task_arn = tasks[0]["taskArn"]
    task_id = task_arn.split("/")[-1]

    # Poll until STOPPED. VHD transfers of 10+ GB can take a while so the
    # default cap of 2 hours is generous; caller can shrink for tests.
    exit_code = 1
    waited = 0
    while waited < poll_seconds_max:
        desc = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])
        t = desc.get("tasks", [{}])[0]
        if t.get("lastStatus") == "STOPPED":
            for c in t.get("containers", []):
                if c.get("name") == "promote-runner":
                    ec = c.get("exitCode")
                    exit_code = ec if ec is not None else 1
                    break
            break
        time.sleep(10)
        waited += 10

    output = ""
    try:
        log_stream = f"{log_stream_prefix}/promote-runner/{task_id}"
        log_resp = logs_client.get_log_events(
            logGroupName=log_group,
            logStreamName=log_stream,
            startFromHead=True,
        )
        output = "\n".join(e.get("message", "") for e in log_resp.get("events", []))
    except Exception as e:
        output = f"(failed to read CloudWatch logs: {e})"

    return (exit_code, output)


async def run_promote_runner_ecs(
    region: str,
    cluster: str,
    task_family: str,
    image: str,
    cpu: str,
    memory: str,
    subnet_id: str,
    security_group_ids: list,
    execution_role_arn: str,
    task_role_arn: str,
    runner_args: list,
    job_id: str,
    poll_seconds_max: int = 7200,
) -> tuple:
    """Async wrapper around the ECS launch+poll. Returns (exit_code, log_output)."""
    try:
        return await asyncio.to_thread(
            _run_promote_runner_ecs_sync,
            region, cluster, task_family, image, cpu, memory,
            subnet_id, security_group_ids, execution_role_arn,
            task_role_arn, runner_args, job_id, poll_seconds_max,
        )
    except AWSError:
        raise
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to launch promote-runner ECS task: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")
