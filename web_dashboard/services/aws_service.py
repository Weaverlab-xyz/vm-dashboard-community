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


def eks_get_token(cluster_name: str, region: str) -> str:
    """Mint a short-lived EKS bearer token — the ``aws eks get-token`` algorithm,
    server-side and offline (no API call): a presigned STS ``GetCallerIdentity``
    URL carrying the ``x-k8s-aws-id: <cluster>`` header, base64url-encoded with a
    ``k8s-aws-v1.`` prefix. Lets a transient kubectl/helm container authenticate to
    a provisioned EKS cluster without the ``aws`` CLI or AWS creds in the container.
    The token is valid ~15 min — ample for a one-shot apply/helm. Reuses the same
    credential resolution as every other AWS call (:func:`_aws_kwargs`)."""
    _require_boto3()
    sts = boto3.client("sts", **_aws_kwargs(region))
    # EKS binds the token to a specific cluster via this signed header.
    sts.meta.events.register(
        "before-sign.sts.GetCallerIdentity",
        lambda request, **kwargs: request.headers.add_header("x-k8s-aws-id", cluster_name),
    )
    url = sts.generate_presigned_url(
        "get_caller_identity", Params={}, ExpiresIn=900, HttpMethod="GET")
    return "k8s-aws-v1." + base64.urlsafe_b64encode(url.encode("utf-8")).decode("utf-8").rstrip("=")


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


def _list_secret_names_sync(region: str) -> list:
    """List all Secrets Manager secret names in the region (no name filter)."""
    sm = _get_sm(region)
    names: list[str] = []
    kwargs: dict = {"MaxResults": 100}
    while True:
        resp = sm.list_secrets(**kwargs)
        names.extend(s["Name"] for s in resp.get("SecretList", []))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return sorted(names)


async def list_secret_names(region: str) -> list:
    """Return every Secrets Manager secret name — the candidate set for the
    per-launch SSH-key-secret override picker."""
    try:
        return await asyncio.to_thread(_list_secret_names_sync, region)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to list secrets: {e}") from e
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


async def get_ssh_private_key_from_secret(region: str, secret_name: str) -> str:
    """Return the SSH **private** key from a Secrets Manager secret when it's a JSON
    keypair carrying a ``private_key`` field. Returns ``""`` when the secret holds
    only a public key. Never logs key material."""
    try:
        raw = await get_secret(secret_name, region)
    except Exception:  # noqa: BLE001 — caller treats absence as "no private key"
        return ""
    try:
        data = json.loads(raw)
        priv = data.get("private_key") or data.get("privateKey") or ""
    except (json.JSONDecodeError, AttributeError):
        priv = ""
    return priv.strip() if priv else ""


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


def _iam_instance_profile_ref(value: str) -> dict:
    """Build the boto3 RunInstances ``IamInstanceProfile`` argument from an
    operator-supplied value.

    boto3 accepts EITHER ``{"Name": <instance-profile-name>}`` OR
    ``{"Arn": <instance-profile-arn>}`` — passing an ARN in the ``Name`` field
    fails with "Invalid IAM Instance Profile name". We accept whichever the
    operator configured (the setup wizard advertises both). Note this is the
    *instance profile* name/ARN, which is NOT necessarily the role name —
    in the IAM console it is the "Instance profile ARN" on the role's summary,
    and the name is the segment after ``instance-profile/``."""
    v = (value or "").strip()
    if v.lower().startswith("arn:"):
        return {"Arn": v}
    return {"Name": v}


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
    correlation_tag: str = "",
) -> dict:
    ec2 = _get_ec2(region)
    tags = [
        {"Key": "Name", "Value": instance_name},
        {"Key": "managed-by", "Value": "vm-dashboard"},
    ]
    if workgroup:
        tags.append({"Key": "Workgroup", "Value": workgroup})
    # Cloud-identity JIT Phase 2 correlation: when the elevation went
    # through Entitle, the handle yields a non-empty correlation_tag
    # like "entitle:req_abc". Attaching it as an EC2 tag means a future
    # audit query can join the dashboard's entitle_activations row to
    # the CloudTrail RunInstances event by tag value.
    if correlation_tag:
        tags.append({"Key": "EntitleRequestId", "Value": correlation_tag})
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
        kwargs["IamInstanceProfile"] = _iam_instance_profile_ref(iam_instance_profile)
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
    correlation_tag: str = "",
) -> dict:
    """Launch a new EC2 instance and return its ID and initial state.

    *public_key* is injected into the instance via cloud-init UserData.
    Pass an empty string to skip key injection (e.g. for Windows AMIs).
    *iam_instance_profile* attaches an instance profile by name (e.g. for SSM access).
    *os_type* controls OS-specific UserData (e.g. SSM agent install for Debian).
    *workgroup*, when non-empty, is written as a `Workgroup=<name>` tag so the
    instance is discoverable per-workgroup by the dashboard and external tools.
    *correlation_tag*, when non-empty, is written as `EntitleRequestId=<tag>`
    so audit can join to the dashboard's entitle_activations row
    (cloud-identity JIT Phase 2).
    """
    try:
        return await asyncio.to_thread(
            _launch_instance_sync,
            region, ami_id, instance_name, instance_type,
            public_key, subnet_id, security_group_ids,
            iam_instance_profile, os_type, workgroup,
            correlation_tag,
        )
    except (ClientError, BotoCoreError) as e:
        msg = str(e)
        low = msg.lower().replace(" ", "")
        if "passrole" in low:
            msg += (
                " — Hint: launching an instance with an SSM instance profile requires "
                "the dashboard's own AWS identity to hold iam:PassRole for the role "
                "inside that profile. Attach a policy granting iam:PassRole on the "
                "role's ARN (ideally conditioned on iam:PassedToService = "
                "ec2.amazonaws.com) to the IAM principal the dashboard authenticates as."
            )
        elif "instanceprofile" in low:
            msg += (
                " — Hint: the SSM Instance Profile setting must be the *instance "
                "profile* name or ARN, not the role name. In IAM open the role and "
                "copy its 'Instance profile ARN'; the name is the part after "
                "'instance-profile/'."
            )
        raise AWSError(f"Failed to launch instance: {msg}") from e
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


# ── Jumpoint host (ECS container instance) primitives ──────────────────────────

def _get_ssm_parameter_sync(region: str, name: str) -> str:
    _require_boto3()
    ssm = boto3.client("ssm", **_aws_kwargs(region))
    return ssm.get_parameter(Name=name)["Parameter"]["Value"]


async def get_ssm_parameter(region: str, name: str) -> str:
    """Read an SSM parameter value (used to resolve the ECS-optimized AMI id)."""
    try:
        return await asyncio.to_thread(_get_ssm_parameter_sync, region, name)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to read SSM parameter {name}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _run_ssm_command_sync(region: str, instance_id: str, commands: list,
                          timeout: int, poll_interval: int) -> dict:
    """Send an AWS-RunShellScript command to a managed instance and poll to
    completion. Returns {status, response_code, stdout, stderr, command_id}.

    Used to run DB-client SQL on the shared Jumpoint host (the only dashboard
    component with line-of-sight to the private DB) — the same SSM SendCommand
    path Password Safe's DB custom plugin uses for rotation."""
    import time
    _require_boto3()
    ssm = boto3.client("ssm", **_aws_kwargs(region))
    send = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Comment="vm-dashboard cloud-db onboarding"[:100],
        Parameters={
            "commands": list(commands),
            # SSM caps executionTimeout at 172800; keep it >= our poll window.
            "executionTimeout": [str(max(int(timeout), 60))],
        },
    )
    command_id = send["Command"]["CommandId"]
    # The invocation isn't queryable for a beat after send — tolerate the
    # InvocationDoesNotExist race while polling for a terminal status.
    terminal = {"Success", "Failed", "Cancelled", "TimedOut", "Undeliverable", "Terminated"}
    deadline = time.monotonic() + max(int(timeout), 60)
    inv: dict = {}
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        try:
            inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "InvocationDoesNotExist":
                continue
            raise
        if inv.get("Status") in terminal:
            break
    return {
        "command_id": command_id,
        "status": inv.get("Status", "TimedOut"),
        "response_code": inv.get("ResponseCode", -1),
        "stdout": inv.get("StandardOutputContent", "") or "",
        "stderr": inv.get("StandardErrorContent", "") or "",
    }


async def ssm_send_command(region: str, instance_id: str, commands: list, *,
                           timeout: int = 300, poll_interval: int = 3) -> dict:
    """Run shell ``commands`` on an SSM-managed instance and wait for the result.

    Returns ``{command_id, status, response_code, stdout, stderr}``. A non-Success
    status (or non-zero response_code) is surfaced to the caller — this never raises
    on a failed command, only on an AWS/transport error, so callers decide whether a
    SQL failure is fatal."""
    try:
        return await asyncio.to_thread(
            _run_ssm_command_sync, region, instance_id, commands, timeout, poll_interval)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"SSM SendCommand to {instance_id} failed: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _run_container_instance_sync(
    region: str, ami_id: str, instance_type: str, subnet_id: str,
    security_group_ids: list, instance_profile: str, user_data: str, name_tag: str,
) -> dict:
    ec2 = _get_ec2(region)
    resp = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        # NetworkInterfaces (not top-level SubnetId/SecurityGroupIds) so we can
        # force a public IP regardless of the subnet's auto-assign setting.
        NetworkInterfaces=[{
            "DeviceIndex": 0,
            "SubnetId": subnet_id,
            "Groups": security_group_ids,
            "AssociatePublicIpAddress": True,
        }],
        IamInstanceProfile={"Name": instance_profile},
        UserData=user_data,  # boto3 base64-encodes for run_instances
        TagSpecifications=[{
            "ResourceType": "instance",
            "Tags": [
                {"Key": "Name", "Value": name_tag},
                # managed-by matches dashboard EC2 instances so the sandbox VPC
                # sweep / rollback cleans the host up too.
                {"Key": "managed-by", "Value": "vm-dashboard"},
            ],
        }],
    )
    inst = resp["Instances"][0]
    return {"instance_id": inst["InstanceId"], "state": inst["State"]["Name"]}


async def run_container_instance(
    region: str, *, ami_id: str, instance_type: str, subnet_id: str,
    security_group_ids: list, instance_profile: str, user_data: str, name_tag: str,
) -> dict:
    """Launch an ECS container instance (the EC2 capacity for the Jumpoint)."""
    try:
        return await asyncio.to_thread(
            _run_container_instance_sync, region, ami_id, instance_type, subnet_id,
            security_group_ids, instance_profile, user_data, name_tag,
        )
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to launch container instance: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _find_instances_by_tag_sync(region: str, name_tag: str, states: list) -> list:
    ec2 = _get_ec2(region)
    resp = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [name_tag]},
        {"Name": "instance-state-name", "Values": states},
    ])
    out = []
    for r in resp.get("Reservations", []):
        for i in r.get("Instances", []):
            out.append({"instance_id": i["InstanceId"], "state": i["State"]["Name"]})
    return out


async def find_instances_by_tag(region: str, *, name_tag: str, states: list) -> list:
    """Return [{instance_id, state}] for instances with Name=name_tag in the
    given states. Used to find-or-create the shared Jumpoint host."""
    try:
        return await asyncio.to_thread(_find_instances_by_tag_sync, region, name_tag, states)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to list instances by tag: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── NAT instance primitives (shared on-demand egress; see nat_instance_service) ──

def _find_nat_ami_sync(region: str, arch: str) -> str:
    ec2 = _get_ec2(region)
    resp = ec2.describe_images(
        Owners=["amazon"],  # AL2023 is published under the "amazon" owner alias
        Filters=[
            {"Name": "name", "Values": [f"al2023-ami-2023.*-kernel-*-{arch}"]},
            {"Name": "state", "Values": ["available"]},
            {"Name": "architecture", "Values": [arch]},
        ],
    )
    imgs = sorted(resp.get("Images", []), key=lambda i: i.get("CreationDate", ""), reverse=True)
    if not imgs:
        raise AWSError(f"No available AL2023 {arch} AMI found in {region}")
    return imgs[0]["ImageId"]


async def find_nat_ami(region: str, arch: str = "arm64") -> str:
    """Newest Amazon Linux 2023 AMI id for ``arch`` via DescribeImages — mirrors
    the EKS module's data.aws_ami.nat and avoids needing an ssm:GetParameter grant."""
    try:
        return await asyncio.to_thread(_find_nat_ami_sync, region, arch)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to resolve NAT AMI: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _run_nat_instance_sync(
    region: str, ami_id: str, instance_type: str, subnet_id: str,
    security_group_ids: list, user_data: str, name_tag: str,
) -> dict:
    ec2 = _get_ec2(region)
    resp = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        # Auto-assigned public IP (NOT an EIP) so egress works with zero standing
        # cost — the instance is terminated when the last VM is destroyed. No
        # IamInstanceProfile: a NAT instance needs no AWS API access.
        NetworkInterfaces=[{
            "DeviceIndex": 0,
            "SubnetId": subnet_id,
            "Groups": security_group_ids,
            "AssociatePublicIpAddress": True,
        }],
        UserData=user_data,  # boto3 base64-encodes for run_instances
        TagSpecifications=[{
            "ResourceType": "instance",
            "Tags": [
                {"Key": "Name", "Value": name_tag},
                # managed-by matches dashboard EC2 instances so the sandbox VPC
                # sweep / rollback cleans the NAT up too.
                {"Key": "managed-by", "Value": "vm-dashboard"},
            ],
        }],
    )
    inst = resp["Instances"][0]
    return {"instance_id": inst["InstanceId"], "state": inst["State"]["Name"]}


async def run_nat_instance(
    region: str, *, ami_id: str, instance_type: str, subnet_id: str,
    security_group_ids: list, user_data: str, name_tag: str,
) -> dict:
    """Launch the shared NAT instance (auto public IP, no instance profile, no EIP)."""
    try:
        return await asyncio.to_thread(
            _run_nat_instance_sync, region, ami_id, instance_type, subnet_id,
            security_group_ids, user_data, name_tag,
        )
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to launch NAT instance: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _set_source_dest_check_sync(region: str, instance_id: str, value: bool) -> None:
    ec2 = _get_ec2(region)
    ec2.modify_instance_attribute(InstanceId=instance_id, SourceDestCheck={"Value": value})


async def set_source_dest_check(region: str, instance_id: str, value: bool) -> None:
    """Toggle an instance's source/dest check (must be False to route/NAT)."""
    try:
        await asyncio.to_thread(_set_source_dest_check_sync, region, instance_id, value)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to set source/dest check on {instance_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _get_instance_primary_eni_sync(region: str, instance_id: str) -> str:
    ec2 = _get_ec2(region)
    # A just-launched instance is eventually consistent — describe_instances by id
    # can briefly raise InvalidInstanceID.NotFound (or return no ENI yet). Retry
    # past the lag so the NAT route can be attached right after launch.
    import time
    last_err = None
    for _ in range(8):  # ~20s budget
        try:
            resp = ec2.describe_instances(InstanceIds=[instance_id])
            for r in resp.get("Reservations", []):
                for i in r.get("Instances", []):
                    for eni in i.get("NetworkInterfaces", []):
                        if eni.get("Attachment", {}).get("DeviceIndex") == 0:
                            return eni["NetworkInterfaceId"]
        except ClientError as e:
            if "InvalidInstanceID.NotFound" not in str(e):
                raise
            last_err = e
        time.sleep(2.5)
    raise AWSError(f"No primary ENI found for instance {instance_id}"
                   + (f" (last: {last_err})" if last_err else ""))


async def get_instance_primary_eni(region: str, instance_id: str) -> str:
    """Return the DeviceIndex-0 ENI id of an instance — the NAT route target."""
    try:
        return await asyncio.to_thread(_get_instance_primary_eni_sync, region, instance_id)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to get primary ENI for {instance_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _default_route_target_sync(region: str, rt_id: str) -> str | None:
    ec2 = _get_ec2(region)
    resp = ec2.describe_route_tables(RouteTableIds=[rt_id])
    for rt in resp.get("RouteTables", []):
        for rte in rt.get("Routes", []):
            if rte.get("DestinationCidrBlock") == "0.0.0.0/0":
                return (rte.get("NetworkInterfaceId") or rte.get("NatGatewayId")
                        or rte.get("GatewayId"))
    return None


def _upsert_default_route_via_eni_sync(region: str, rt_id: str, eni_id: str) -> None:
    ec2 = _get_ec2(region)
    current = _default_route_target_sync(region, rt_id)
    if current == eni_id:
        return  # already correct
    if current is None:
        ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0",
                         NetworkInterfaceId=eni_id)
    else:
        # Stale target (e.g. NAT was replaced) — repoint it.
        ec2.replace_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0",
                          NetworkInterfaceId=eni_id)


async def upsert_default_route_via_eni(region: str, rt_id: str, eni_id: str) -> None:
    """Ensure ``rt_id`` has ``0.0.0.0/0 -> eni_id`` (create / replace-if-stale / no-op)."""
    try:
        await asyncio.to_thread(_upsert_default_route_via_eni_sync, region, rt_id, eni_id)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to set default route on {rt_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _delete_default_route_sync(region: str, rt_id: str) -> None:
    ec2 = _get_ec2(region)
    try:
        ec2.delete_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0")
    except ClientError as e:
        if "InvalidRoute.NotFound" not in str(e):
            raise


async def delete_default_route(region: str, rt_id: str) -> None:
    """Delete the ``0.0.0.0/0`` route from ``rt_id`` (no-op if already absent)."""
    try:
        await asyncio.to_thread(_delete_default_route_sync, region, rt_id)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to delete default route on {rt_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _ensure_nat_security_group_sync(region: str, vpc_id: str, vpc_cidr: str, name: str) -> str:
    ec2 = _get_ec2(region)
    resp = ec2.describe_security_groups(Filters=[
        {"Name": "group-name", "Values": [name]},
        {"Name": "vpc-id", "Values": [vpc_id]},
    ])
    if resp.get("SecurityGroups"):
        return resp["SecurityGroups"][0]["GroupId"]
    sg = ec2.create_security_group(
        GroupName=name,
        Description="NAT instance - ingress from this VPC, egress all",
        VpcId=vpc_id,
        TagSpecifications=[{
            "ResourceType": "security-group",
            "Tags": [{"Key": "Name", "Value": name},
                     {"Key": "managed-by", "Value": "vm-dashboard"}],
        }],
    )
    sg_id = sg["GroupId"]
    # Ingress: all protocols from the VPC (forwarded traffic from private VMs).
    # Egress: default 0.0.0.0/0 all is present on create — leave it.
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[{"IpProtocol": "-1", "IpRanges": [{"CidrIp": vpc_cidr}]}],
    )
    return sg_id


async def ensure_nat_security_group(region: str, *, vpc_id: str, vpc_cidr: str, name: str) -> str:
    """Find-or-create the NAT SG (ingress all from ``vpc_cidr``, egress all).
    Fallback for sandboxes that predate the script pre-creating it."""
    try:
        return await asyncio.to_thread(_ensure_nat_security_group_sync, region, vpc_id, vpc_cidr, name)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to ensure NAT security group: {e}") from e
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

    # Deploy-form defaults from the sandbox config so new VMs land on the private
    # subnet (where the on-demand NAT route applies) + its VM-tier SG. Empty when
    # unset — the form then leaves the pickers unselected.
    from . import config_service
    default_subnet_id = config_service.get("aws_default_subnet_id") or ""
    default_sg_id = config_service.get("aws_default_security_group_id") or ""

    return {
        "subnets": subnets,
        "security_groups": security_groups,
        "instance_types": instance_types,
        "default_subnet_id": default_subnet_id,
        "default_security_group_id": default_sg_id,
    }


async def get_network_options(region: str) -> dict:
    """Return dropdowns for the deploy form: subnets, security groups, instance types."""
    try:
        return await asyncio.to_thread(_get_network_options_sync, region)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to fetch network options: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── RDS options (for the cloud-databases provision form) ──────────────────────

# Static curated list, mirroring instance_types above — current-generation
# classes that support PostgreSQL in all commercial regions. Avoids the slow,
# paginated DescribeOrderableDBInstanceOptions call.
DB_INSTANCE_CLASSES = [
    "db.t3.micro", "db.t3.small", "db.t3.medium", "db.t3.large",
    "db.t4g.micro", "db.t4g.small", "db.t4g.medium", "db.t4g.large",
    "db.m5.large", "db.m5.xlarge", "db.m5.2xlarge",
    "db.m6g.large", "db.m6g.xlarge",
    "db.r5.large", "db.r5.xlarge",
    "db.r6g.large", "db.r6g.xlarge",
]


def _aws_kwargs_pinned(region: str) -> dict:
    """_aws_kwargs, but the caller's region wins over the wizard default.

    _aws_kwargs lets the configured aws_region override its region argument —
    right for pages that always target the default region, wrong for the DB
    provision form, where the user may type a different region.
    """
    kwargs = _aws_kwargs(region)
    if region:
        kwargs["region_name"] = region
    return kwargs


def _get_db_options_sync(region: str) -> dict:
    _require_boto3()
    rds = boto3.client("rds", **_aws_kwargs_pinned(region))

    groups, marker = [], None
    while True:  # DescribeDBSubnetGroups paginates via Marker
        resp = rds.describe_db_subnet_groups(**({"Marker": marker} if marker else {}))
        for g in resp.get("DBSubnetGroups", []):
            subnets = g.get("Subnets", [])
            azs = sorted({s["SubnetAvailabilityZone"]["Name"] for s in subnets})
            groups.append({
                "name": g["DBSubnetGroupName"],
                "label": f"{g['DBSubnetGroupName']} – {g.get('VpcId', '')} – "
                         f"{len(subnets)} subnets ({', '.join(azs)})",
                "description": g.get("DBSubnetGroupDescription", ""),
                "vpc_id": g.get("VpcId", ""),
            })
        marker = resp.get("Marker")
        if not marker:
            break

    # Same dict shape as the EC2 deploy form's list so the picker markup matches.
    ec2 = boto3.client("ec2", **_aws_kwargs_pinned(region))
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

    return {
        "region": region,
        "instance_classes": DB_INSTANCE_CLASSES,
        "db_subnet_groups": groups,
        "security_groups": security_groups,
    }


async def get_db_options(region: str) -> dict:
    """Pickers for the database provision form: instance classes, DB subnet
    groups, and security groups in the given region."""
    try:
        return await asyncio.to_thread(_get_db_options_sync, region)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to fetch database options: {e}") from e
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
            {"Key": "managed-by", "Value": "vm-dashboard"},
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
    launch_type: str = "EC2",
) -> str:
    """Register the bt-jumpoint task definition if a matching one doesn't exist.

    The jumpoint needs PROTOCOL-TUNNEL capabilities (NET_ADMIN/NET_RAW/IPC_LOCK +
    /dev/net/tun) which only the EC2 launch type can grant — Fargate forbids them.
    A task-def family may already exist from a prior FARGATE run, so we re-register
    a new revision when the existing one's compatibility doesn't match.
    """
    ec2 = launch_type.upper() == "EC2"
    ecs = _get_ecs(region)
    try:
        resp = ecs.describe_task_definition(taskDefinition=family)
        td = resp["taskDefinition"]
        compat = set(td.get("requiresCompatibilities") or td.get("compatibilities") or [])
        # Reuse only when the existing revision already targets the launch type
        # we want — otherwise fall through and register a fresh revision.
        if (("EC2" in compat) if ec2 else ("FARGATE" in compat)):
            return td["taskDefinitionArn"]
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("ClientException", "InvalidParameterException"):
            raise

    container = {
        "name": "jumpoint",
        "image": image,
        "essential": True,
        "environment": [],  # DEPLOY_KEY passed as run-time override
    }
    if ec2:
        # EC2 launch type: host networking (uses the instance's ENI/SG) + the
        # Linux caps and TUN device the jumpoint needs to build tunnels.
        kwargs = {
            "family": family,
            "networkMode": "host",
            "requiresCompatibilities": ["EC2"],
            "containerDefinitions": [{
                **container,
                "memory": int(memory) if str(memory).isdigit() else 512,
                "linuxParameters": {
                    "capabilities": {"add": ["NET_ADMIN", "NET_RAW", "IPC_LOCK"]},
                    "devices": [{
                        "hostPath": "/dev/net/tun",
                        "containerPath": "/dev/net/tun",
                        "permissions": ["read", "write"],
                    }],
                    "initProcessEnabled": True,
                },
            }],
        }
    else:
        kwargs = {
            "family": family,
            "networkMode": "awsvpc",
            "requiresCompatibilities": ["FARGATE"],
            "cpu": cpu,
            "memory": memory,
            "containerDefinitions": [container],
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
    launch_type: str = "EC2",
) -> str:
    """Ensure the ECS cluster exists, register the task definition if needed,
    then launch one jumpoint task. Returns the task ARN.

    launch_type "EC2" (default) places the task on EC2 capacity with host
    networking so it can do protocol tunneling; "FARGATE" is the legacy,
    tunnel-incapable path. EC2 capacity is provisioned by the sandbox script.
    """
    ec2 = launch_type.upper() == "EC2"
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
        region, task_family, cpu, memory, execution_role_arn, image, launch_type
    )

    run_kwargs = {
        "cluster": cluster,
        "taskDefinition": task_def_arn,
        "launchType": "EC2" if ec2 else "FARGATE",
        "overrides": {
            "containerOverrides": [
                {
                    "name": "jumpoint",
                    "environment": [{"name": "DEPLOY_KEY", "value": deploy_key}],
                }
            ]
        },
        "count": 1,
    }
    if not ec2:
        # awsvpc networking is Fargate-only here; the EC2 task uses host
        # networking (the container instance's ENI/SG), so no networkConfiguration.
        run_kwargs["networkConfiguration"] = {
            "awsvpcConfiguration": {
                "subnets": [subnet_id],
                "securityGroups": security_group_ids,
                "assignPublicIp": "ENABLED",
            }
        }

    resp = ecs.run_task(**run_kwargs)

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
    launch_type: str = "EC2",
) -> str:
    """Start an ECS task running the BeyondTrust Jumpoint container. Returns the
    task ARN. launch_type "EC2" (default) is tunnel-capable; "FARGATE" is legacy."""
    try:
        return await asyncio.to_thread(
            _run_ecs_task_sync,
            region, cluster, task_family, subnet_id, security_group_ids,
            deploy_key, cpu, memory, execution_role_arn, image, launch_type,
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


def _list_container_instances_sync(region: str, cluster: str) -> list:
    ecs = _get_ecs(region)
    arns = ecs.list_container_instances(cluster=cluster).get("containerInstanceArns", [])
    if not arns:
        return []
    resp = ecs.describe_container_instances(cluster=cluster, containerInstances=arns)
    return [{"arn": c["containerInstanceArn"], "status": c.get("status"),
             "ec2_instance_id": c.get("ec2InstanceId")}
            for c in resp.get("containerInstances", [])]


async def list_container_instances(region: str, cluster: str) -> list:
    """Return registered ECS container instances (the EC2 capacity) with status —
    used to poll for the Jumpoint host coming online before running the task."""
    try:
        return await asyncio.to_thread(_list_container_instances_sync, region, cluster)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to list container instances: {e}") from e
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
    secret_entries: list | None = None,
    manifest_b64: str = "",
) -> tuple:
    """Create an ECS Fargate task that runs one Ansible playbook, wait for it to
    finish, retrieve CloudWatch logs, and return (exit_code, output)."""
    import time
    ecs = _get_ecs(region)
    logs_client = boto3.client("logs", **_aws_kwargs(region))
    log_group = "/ecs/ansible-runner"
    log_stream_prefix = f"ansible/{job_id[:8]}"

    try:
        logs_client.create_log_group(logGroupName=log_group)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise

    from . import cloud_ansible_secrets as _cas
    _secret_prefix = _cas.command_prefix() if manifest_b64 else ""
    _secret_ev = _cas.extra_vars_arg() if manifest_b64 else ""
    cmd = (
        "set -e && "
        'echo "$PLAYBOOK_B64" | base64 -d > /tmp/playbook.yml && '
        'echo "$SSH_KEY_B64" | base64 -d > /tmp/ssh_key && '
        "chmod 600 /tmp/ssh_key && "
        + _secret_prefix +
        f"ansible-playbook -i '{target_ip},' --forks 1 "
        f"-u {ansible_user} --private-key /tmp/ssh_key "
        + _secret_ev +
        "--ssh-extra-args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null' "
        "/tmp/playbook.yml"
    )

    # ECS secrets: valueFrom → Secrets Manager ARN (the execution role must be
    # allowed secretsmanager:GetSecretValue on it). Defined on the task def.
    _secrets_def = [{"name": e["env"], "valueFrom": e["arn"]} for e in (secret_entries or [])]

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
            # The playbook (and the secret-var manifest, which carries var *names*
            # not values) ride the per-run task definition, NOT the RunTask
            # override. AWS caps the RunTask containerOverrides at 8192 bytes, and
            # a larger playbook + the ~4.5 KB base64 SSH key together blow that
            # limit (InvalidParameterException). A playbook isn't a credential —
            # real secrets are injected via `secrets`/valueFrom (below) or the
            # ephemeral store — so it's safe on the task def; only the SSH key
            # stays an ephemeral RunTask override so it isn't retained in task-def
            # revision history.
            "environment": [{"name": "PLAYBOOK_B64", "value": playbook_b64}]
                + ([{"name": _cas.MANIFEST_ENV, "value": manifest_b64}] if manifest_b64 else []),
            **({"secrets": _secrets_def} if _secrets_def else {}),
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
            "assignPublicIp": "ENABLED",  # public-subnet egress via IGW (sandbox has no NAT; runner needs egress, not inbound)
        }},
        # Only the SSH key rides the RunTask override (ephemeral, ~4.5 KB base64 —
        # comfortably under the 8192-byte overrides cap); everything else is on the
        # task def above.
        overrides={"containerOverrides": [{
            "name": "ansible",
            "environment": [
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
    secret_entries: list | None = None,
    manifest_b64: str = "",
) -> tuple:
    """Run an Ansible playbook via ECS Fargate. Returns (exit_code, output)."""
    try:
        return await asyncio.to_thread(
            _run_ecs_ansible_sync,
            region, cluster, task_family, image, cpu, memory,
            subnet_id, security_group_ids, execution_role_arn,
            target_ip, ansible_user, playbook_b64, ssh_key_b64, job_id,
            secret_entries, manifest_b64,
        )
    except AWSError:
        raise
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to run ECS Ansible task: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── ECS Kubernetes runner ─────────────────────────────────────────────────────

def _run_ecs_k8s_sync(
    region: str,
    cluster: str,
    task_family: str,
    image: str,
    cpu: str,
    memory: str,
    subnet_id: str,
    security_group_ids: list,
    execution_role_arn: str,
    command: str,
    kubeconfig_b64: str,
    stdin_b64: str,
    job_id: str,
) -> tuple:
    """Create an ECS Fargate task that runs one kubectl/helm command against a
    cluster's API, wait for it to finish, retrieve CloudWatch logs, and return
    (exit_code, output).

    Modelled on `_run_ecs_ansible_sync` — same task-def-register / run-task /
    poll-describe-tasks / pull-CloudWatch-logs shape. The stock kubectl+helm
    `image`, the generic shell `command`, and the kubeconfig (decoded from
    ``KUBECONFIG_B64`` env into ``$KUBECONFIG``) are the only differences."""
    import time
    ecs = _get_ecs(region)
    logs_client = boto3.client("logs", **_aws_kwargs(region))
    log_group = "/ecs/k8s-runner"
    log_stream_prefix = f"k8s/{job_id[:8]}" if job_id else "k8s/adhoc"

    try:
        logs_client.create_log_group(logGroupName=log_group)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise

    # Decode the kubeconfig from the env var into $KUBECONFIG, then run the
    # caller's ready-to-run shell command (optionally piping decoded stdin in).
    setup = (
        "set -e; "
        'printf %s "$KUBECONFIG_B64" | base64 -d > /tmp/kubeconfig; '
        "export KUBECONFIG=/tmp/kubeconfig; "
    )
    if stdin_b64:
        full_cmd = setup + 'printf %s "$STDIN_B64" | base64 -d | ' + command
    else:
        full_cmd = setup + command

    td_kwargs: dict = dict(
        family=task_family,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu=str(cpu),
        memory=str(memory),
        containerDefinitions=[{
            "name": "k8s",
            "image": image,
            "essential": True,
            "command": ["sh", "-c", full_cmd],
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

    environment = [{"name": "KUBECONFIG_B64", "value": kubeconfig_b64}]
    if stdin_b64:
        environment.append({"name": "STDIN_B64", "value": stdin_b64})

    run_resp = ecs.run_task(
        cluster=cluster,
        taskDefinition=task_def_arn,
        launchType="FARGATE",
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": [subnet_id] if subnet_id else [],
            "securityGroups": security_group_ids or [],
            "assignPublicIp": "ENABLED",  # public-subnet egress via IGW (sandbox has no NAT; runner needs egress, not inbound)
        }},
        overrides={"containerOverrides": [{
            "name": "k8s",
            "environment": environment,
        }]},
        count=1,
    )

    tasks = run_resp.get("tasks", [])
    if not tasks:
        raise AWSError(f"ECS k8s task failed to start: {run_resp.get('failures', [])}")

    task_arn = tasks[0]["taskArn"]
    task_id = task_arn.split("/")[-1]

    # Poll until stopped (max 20 min)
    exit_code = 1
    for _ in range(120):
        desc = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])
        t = desc.get("tasks", [{}])[0]
        if t.get("lastStatus") == "STOPPED":
            for c in t.get("containers", []):
                if c.get("name") == "k8s":
                    ec = c.get("exitCode")
                    exit_code = ec if ec is not None else 1
                    break
            break
        time.sleep(10)

    # Retrieve CloudWatch logs
    output = ""
    try:
        log_stream = f"{log_stream_prefix}/k8s/{task_id}"
        log_resp = logs_client.get_log_events(
            logGroupName=log_group,
            logStreamName=log_stream,
            startFromHead=True,
        )
        output = "\n".join(e["message"] for e in log_resp.get("events", []))
    except Exception as log_err:
        logger.warning("ECS k8s: could not retrieve logs: %s", log_err)

    return exit_code, output


async def run_ecs_k8s_task(
    *,
    region: str,
    cluster: str,
    task_family: str,
    image: str,
    cpu: str,
    memory: str,
    subnet_id: str,
    security_group_ids: list,
    execution_role_arn: str,
    command: str,
    kubeconfig_b64: str,
    stdin_b64: str = "",
    job_id: str,
) -> tuple:
    """Run a kubectl/helm command against a cluster's API via ECS Fargate.
    Returns (exit_code, output)."""
    try:
        return await asyncio.to_thread(
            _run_ecs_k8s_sync,
            region, cluster, task_family, image, cpu, memory,
            subnet_id, security_group_ids, execution_role_arn,
            command, kubeconfig_b64, stdin_b64, job_id,
        )
    except AWSError:
        raise
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to run ECS k8s task: {e}") from e
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
            {"Key": "managed-by", "Value": "vm-dashboard"},
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
                    {"Key": "managed-by", "Value": "vm-dashboard"},
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


# ── Generic Docker Compose → ECS Fargate task ────────────────────────────────

def _deploy_compose_ecs_sync(
    region: str,
    cluster: str,
    family: str,
    services: list,
    cpu: str,
    memory: str,
    subnet_id: str,
    security_group_ids: list,
    execution_role_arn: str,
    assign_public_ip: bool = True,
) -> dict:
    """Register a Fargate task definition with one containerDefinition per
    compose service and launch a single task. Returns task info.

    Fargate sizing is task-level (`cpu`/`memory`); compose per-service limits map
    to per-container `memoryReservation` when present. awslogs is configured so
    container output is reachable from CloudWatch like the other runners."""
    ecs = _get_ecs(region)
    logs_client = boto3.client("logs", **_aws_kwargs(region))
    log_group = "/ecs/compose"
    try:
        logs_client.create_log_group(logGroupName=log_group)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise

    container_defs = []
    for svc in services:
        cdef: dict = {
            "name": svc.name,
            "image": svc.image,
            "essential": True,
            "environment": [{"name": k, "value": v} for k, v in svc.env],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": log_group,
                    "awslogs-region": region,
                    "awslogs-stream-prefix": family,
                },
            },
        }
        if svc.entrypoint:
            cdef["entryPoint"] = svc.entrypoint
        if svc.command:
            cdef["command"] = svc.command
        if svc.ports:
            cdef["portMappings"] = [
                {"containerPort": cport, "hostPort": cport, "protocol": proto}
                for _host, cport, proto in svc.ports
            ]
        if svc.memory_mb:
            cdef["memoryReservation"] = int(svc.memory_mb)
        container_defs.append(cdef)

    td_kwargs: dict = dict(
        family=family,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu=str(cpu),
        memory=str(memory),
        containerDefinitions=container_defs,
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
            "assignPublicIp": "ENABLED" if assign_public_ip else "DISABLED",
        }},
        count=1,
        startedBy="vm-dashboard-compose",
    )

    tasks = run_resp.get("tasks", [])
    if not tasks:
        raise AWSError(f"ECS compose task failed to start: {run_resp.get('failures', [])}")
    task_arn = tasks[0]["taskArn"]
    return {
        "task_arn": task_arn,
        "task_id": task_arn.split("/")[-1],
        "cluster": cluster,
        "task_definition": task_def_arn.split("/")[-1],
        "containers": [c["name"] for c in container_defs],
    }


async def deploy_compose_ecs(
    region: str,
    cluster: str,
    family: str,
    services: list,
    cpu: str,
    memory: str,
    subnet_id: str,
    security_group_ids: list,
    execution_role_arn: str,
    assign_public_ip: bool = True,
) -> dict:
    """Deploy a parsed compose spec to a new ECS Fargate task."""
    try:
        return await asyncio.to_thread(
            _deploy_compose_ecs_sync,
            region, cluster, family, services, cpu, memory,
            subnet_id, security_group_ids, execution_role_arn, assign_public_ip,
        )
    except AWSError:
        raise
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to deploy compose to ECS: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")
