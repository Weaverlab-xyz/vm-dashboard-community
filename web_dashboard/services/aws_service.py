"""
AWS service wrapper using boto3.
All blocking calls are run via _to_thread() — AWS's OWN bounded pool, not the event
loop's shared default executor — to keep the FastAPI event loop free AND stop a slow
AWS starving the other providers. See services/cloud_executor.py.
"""
import base64
import json
import logging
from typing import Optional
from datetime import datetime, timezone

from . import cloud_executor

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError, BotoCoreError
    _boto3_available = True
except ImportError:
    _boto3_available = False

logger = logging.getLogger(__name__)


class AWSError(Exception):
    """Raised when an AWS operation fails."""


async def _to_thread(fn, /, *args, **kwargs):
    """Run a blocking boto3 call on AWS's own thread pool.

    Translates the executor's refusals into AWSError so every existing ``except
    AWSError`` — which is what turns a failure into a 503 or an unavailable tile —
    keeps working unchanged. Anything boto3 itself raises propagates untouched.
    """
    try:
        return await cloud_executor.run("aws", fn, *args, **kwargs)
    except cloud_executor.CloudCallError as exc:
        raise AWSError(str(exc)) from exc


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

    # Dynamic tier first. `credentials()` returns None when this deployment is not on it,
    # which is the normal case and costs a memo lookup. When it IS on it and no lease can
    # be produced it raises rather than returning nothing — otherwise a deployment that
    # had deliberately retired its static key would silently fall through to whatever is
    # left in the environment, which is the outcome this whole feature exists to prevent.
    try:
        from . import workload_credential_lease as _leases
        dynamic = _leases.credentials("aws")
    except _lease_unavailable() as exc:
        raise AWSError(
            "AWS is configured to use BeyondTrust Workload Credentials, but no "
            "credential could be issued: %s" % exc) from exc
    if dynamic:
        kwargs["aws_access_key_id"]     = dynamic["access_key_id"]
        kwargs["aws_secret_access_key"] = dynamic["secret_access_key"]
        # Assumed-role credentials are a triple. Without the session token every call
        # fails InvalidClientTokenId, which reads like a wrong key rather than a missing
        # field — this one line is what the dynamic path turns on.
        kwargs["aws_session_token"]     = dynamic["session_token"]
        return kwargs

    if key_id and secret:
        kwargs["aws_access_key_id"]     = key_id
        kwargs["aws_secret_access_key"] = secret
    return kwargs


def _lease_unavailable():
    """The LeaseUnavailable class, resolved lazily.

    A function rather than a module-level import because the lease module imports this
    one indirectly; a top-level import here would close the cycle.
    """
    from .workload_credential_lease import LeaseUnavailable
    return LeaseUnavailable


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


def _get_eks(region: str):
    _require_boto3()
    return boto3.client("eks", **_aws_kwargs(region))


def create_eks_access_entry(cluster_name: str, region: str, *, principal_arn: str,
                            username: str) -> dict:
    """Map an IAM principal into the cluster as ``username`` via an EKS access entry.

    The modern replacement for editing the ``aws-auth`` ConfigMap — which this function
    deliberately never touches, because a bad edit there can lock every principal out of
    the cluster. Used by the Password Safe token-rotation onboarding: the rotator
    ClusterRoleBinding's ``User`` subject is this username, and without the access entry
    the binding matches nothing and the API server 401s — a failure whose cause is
    invisible from inside the cluster.

    Idempotent: an entry that already exists (``ResourceInUseException``) is success.
    Requires the cluster's authenticationMode to be API or API_AND_CONFIG_MAP; the
    error from a CONFIG_MAP-only cluster is surfaced as-is."""
    _require_boto3()
    eks = _get_eks(region)
    try:
        eks.create_access_entry(
            clusterName=cluster_name, principalArn=principal_arn,
            type="STANDARD", username=username)
        return {"created": True, "already": False}
    except Exception as exc:  # noqa: BLE001 — botocore error classes are dynamic
        if "ResourceInUseException" in exc.__class__.__name__ or \
                "already exists" in str(exc).lower():
            return {"created": False, "already": True}
        raise


# ── EKS OIDC identity-provider federation (Entra ID) ─────────────────────────────
#
# Associate a shared Entra app registration as an EKS cluster's OIDC identity
# provider so a user's Entra token authenticates to the cluster and its `groups`
# claim (Entra group Object IDs) matches the RBAC `Group` binding done by
# k8s_service.bind_entra_group. Additive to IAM/aws-auth (node bootstrap + console
# access stay IAM). EKS allows ONE OIDC provider per cluster. No `groupsPrefix`
# (the bare group OID is the RBAC subject, matching AKS); `usernamePrefix` only
# keeps federated user names from colliding with IAM (we bind groups, not users).

def associate_eks_oidc(cluster_name: str, region: str, *, issuer_url: str, client_id: str,
                       username_claim: str = "oid", groups_claim: str = "groups",
                       config_name: str = "entra") -> dict:
    """Associate the Entra OIDC IdP on ``cluster_name``. Idempotent: an existing
    config of the same name (any status) is treated as success. Async on AWS's side
    (cluster enters UPDATING) — poll :func:`describe_eks_oidc_status` to ACTIVE."""
    _require_boto3()
    existing = describe_eks_oidc_status(cluster_name, region, config_name)
    if existing:
        return {"update_id": None, "already": True, "status": existing}
    eks = _get_eks(region)
    try:
        resp = eks.associate_identity_provider_config(
            clusterName=cluster_name,
            oidc={
                "identityProviderConfigName": config_name,
                "issuerUrl": issuer_url,
                "clientId": client_id,
                "usernameClaim": username_claim,
                "usernamePrefix": "entra:",
                "groupsClaim": groups_claim,
            },
        )
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"EKS associate OIDC identity provider failed: {e}")
    return {"update_id": (resp.get("update") or {}).get("id"), "already": False}


def describe_eks_oidc_status(cluster_name: str, region: str, config_name: str = "entra") -> str:
    """Status of the cluster's ``config_name`` OIDC identity-provider config:
    ``CREATING`` | ``ACTIVE`` | ``DELETING``, or "" when none exists."""
    _require_boto3()
    eks = _get_eks(region)
    try:
        resp = eks.describe_identity_provider_config(
            clusterName=cluster_name,
            identityProviderConfig={"type": "oidc", "name": config_name})
    except (ClientError, BotoCoreError) as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "") if hasattr(e, "response") else ""
        if code in ("ResourceNotFoundException", "NotFoundException"):
            return ""
        raise AWSError(f"EKS describe OIDC identity provider failed: {e}")
    return ((resp.get("identityProviderConfig") or {}).get("oidc") or {}).get("status", "") or ""


def disassociate_eks_oidc(cluster_name: str, region: str, config_name: str = "entra") -> dict:
    """Remove the cluster's ``config_name`` OIDC identity-provider config (idempotent —
    no-op when absent). Async on AWS's side (cluster UPDATING)."""
    _require_boto3()
    if not describe_eks_oidc_status(cluster_name, region, config_name):
        return {"removed": False}
    eks = _get_eks(region)
    try:
        eks.disassociate_identity_provider_config(
            clusterName=cluster_name,
            identityProviderConfig={"type": "oidc", "name": config_name})
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"EKS disassociate OIDC identity provider failed: {e}")
    return {"removed": True}


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
    return await _to_thread(_get_secret_sync, secret_name, region)


async def get_keypair_private_key(region: str, key_name: str) -> str:
    """Fetch the private key PEM for an EC2 key pair from Secrets Manager.

    Naming convention: store the .pem contents as a Secrets Manager secret
    named  ec2/keypairs/<key-name>  (e.g. ec2/keypairs/my-ec2-key).
    """
    secret_name = f"ec2/keypairs/{key_name}"
    try:
        return await _to_thread(_get_secret_sync, secret_name, region)
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
        return await _to_thread(_list_ssh_key_secrets_sync, region, prefix)
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
        return await _to_thread(_list_secret_names_sync, region)
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
        return await _to_thread(_get_ssh_public_key_from_secret_sync, region, secret_name)
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
        return await _to_thread(_list_amis_sync, region)
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
        return await _to_thread(_describe_instances_sync, region, instance_ids)
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
        BlockDeviceMappings=_root_bdm_sync(region, ami_id),
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": tags,
            },
            _volume_tag_spec(tags),
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
        return await _to_thread(
            _launch_instance_sync,
            region, ami_id, instance_name, instance_type,
            public_key, subnet_id, security_group_ids,
            iam_instance_profile, os_type, workgroup,
            correlation_tag,
        )
    except (ClientError, BotoCoreError) as e:
        msg = str(e)
        low = msg.lower().replace(" ", "")
        if "passrole" in low and "sessionpolicy" in low:
            # A session policy only exists here when the credential is a Workload
            # Credentials dynamic lease, and an explicit deny in it beats ANY identity
            # policy — the static-key remedy below cannot work and must not be offered.
            msg += (
                " — Hint: the deny came from a session policy, so this call ran on a "
                "BeyondTrust Workload Credentials dynamic lease (the session name above "
                "embeds the dynamic secret's name). No IAM policy can override an "
                "explicit session-policy deny, so do not widen IAM. If the session "
                "names the everyday secret, a provisioning job was served the read-only "
                "lease, which excludes IAM by design — deploys must run on the "
                "provision lease. If it names the provision secret, that secret's "
                "session policy in Workload Credentials needs iam:PassRole."
            )
        elif "passrole" in low:
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
        return await _to_thread(_terminate_instance_sync, region, instance_id)
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
        return await _to_thread(_get_ssm_parameter_sync, region, name)
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
        return await _to_thread(
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
    tags = [
        {"Key": "Name", "Value": name_tag},
        # managed-by matches dashboard EC2 instances so the sandbox VPC
        # sweep / rollback cleans the host up too.
        {"Key": "managed-by", "Value": "vm-dashboard"},
    ]
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
        BlockDeviceMappings=_root_bdm_sync(region, ami_id),
        TagSpecifications=[{"ResourceType": "instance", "Tags": tags},
                           _volume_tag_spec(tags)],
    )
    inst = resp["Instances"][0]
    return {"instance_id": inst["InstanceId"], "state": inst["State"]["Name"]}


async def run_container_instance(
    region: str, *, ami_id: str, instance_type: str, subnet_id: str,
    security_group_ids: list, instance_profile: str, user_data: str, name_tag: str,
) -> dict:
    """Launch an ECS container instance (the EC2 capacity for the Jumpoint)."""
    try:
        return await _to_thread(
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
            out.append({"instance_id": i["InstanceId"], "state": i["State"]["Name"],
                        "public_ip": i.get("PublicIpAddress")})
    return out


async def find_instances_by_tag(region: str, *, name_tag: str, states: list) -> list:
    """Return [{instance_id, state, public_ip}] for instances with Name=name_tag in
    the given states. Used to find-or-create the shared Jumpoint host (public_ip is
    the host's ephemeral egress IP — used to whitelist it in the Rancher firewall)."""
    try:
        return await _to_thread(_find_instances_by_tag_sync, region, name_tag, states)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to list instances by tag: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _find_instances_by_name_tags_sync(region: str, name_tags: list, states: list) -> dict:
    ec2 = _get_ec2(region)
    resp = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": list(name_tags)},
        {"Name": "instance-state-name", "Values": states},
    ])
    out: dict = {}
    for r in resp.get("Reservations", []):
        for i in r.get("Instances", []):
            name = next((t.get("Value") or "" for t in i.get("Tags", [])
                         if t.get("Key") == "Name"), "")
            if not name:
                continue
            # Two hosts sharing a Name tag shouldn't happen, but if it does, let the
            # liveliest one answer — the singular lookup above takes [0] and the ensure
            # paths reuse whatever it returns.
            prev = out.get(name)
            if prev and prev["state"] in ("running", "pending"):
                continue
            out[name] = {"instance_id": i["InstanceId"], "state": i["State"]["Name"],
                         "public_ip": i.get("PublicIpAddress")}
    return out


async def find_instances_by_name_tags(region: str, *, name_tags: list, states: list) -> dict:
    """``{Name tag: {instance_id, state, public_ip}}`` for instances carrying any of
    ``name_tags`` in ``states`` — one DescribeInstances for the whole set.

    :func:`find_instances_by_tag` stays the find-or-create primitive the ensure paths
    use. This exists for the gateway reconcile pass, which asks about every gateway in a
    region at once: a call per name would turn one page load into N round trips and, on a
    throttled account, make the answer depend on how many gateways you happen to have."""
    if not name_tags:
        return {}
    try:
        return await _to_thread(_find_instances_by_name_tags_sync, region,
                                list(name_tags), states)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to list instances by name tag: {e}") from e
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
        return await _to_thread(_find_nat_ami_sync, region, arch)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to resolve NAT AMI: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# OS-family → newest-AMI resolution for the Packer "Build Image" path. The GCP
# build takes a source image *family* (debian-12) and Compute Engine resolves it;
# EC2 has no equivalent, so this map is ours — the same publisher owners/name
# patterns the community-AMI browser uses, pinned per family so a build recipe
# ("source family debian-12") works identically on both clouds. ssh_username is
# the image's canonical login user: Debian AMIs use `admin`, not ec2-user.
AMI_FAMILIES = {
    "debian-12":    {"owners": ["136693071363"], "name": "debian-12-amd64-*",
                     "ssh_username": "admin"},
    "debian-13":    {"owners": ["136693071363"], "name": "debian-13-amd64-*",
                     "ssh_username": "admin"},
    "ubuntu-22.04": {"owners": ["099720109477"],
                     "name": "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*",
                     "ssh_username": "ubuntu"},
    "ubuntu-24.04": {"owners": ["099720109477"],
                     "name": "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*",
                     "ssh_username": "ubuntu"},
    "al2023":       {"owners": ["amazon"], "name": "al2023-ami-2023.*-kernel-*-x86_64",
                     "ssh_username": "ec2-user"},
}


def _find_ami_by_family_sync(region: str, family: str) -> dict:
    spec = AMI_FAMILIES.get((family or "").strip().lower())
    if not spec:
        raise AWSError(
            f"unknown source AMI family {family!r} — one of {', '.join(sorted(AMI_FAMILIES))}")
    ec2 = _get_ec2(region)
    resp = ec2.describe_images(
        Owners=spec["owners"],
        Filters=[
            {"Name": "name", "Values": [spec["name"]]},
            {"Name": "state", "Values": ["available"]},
            {"Name": "architecture", "Values": ["x86_64"]},
        ],
    )
    imgs = sorted(resp.get("Images", []), key=lambda i: i.get("CreationDate", ""), reverse=True)
    if not imgs:
        raise AWSError(f"no available {family} AMI found in {region}")
    return {"ami_id": imgs[0]["ImageId"], "name": imgs[0].get("Name", ""),
            "ssh_username": spec["ssh_username"]}


async def find_ami_by_family(region: str, family: str) -> dict:
    """Newest public AMI for an OS family (``AMI_FAMILIES``) via DescribeImages.
    Returns ``{ami_id, name, ssh_username}``."""
    try:
        return await _to_thread(_find_ami_by_family_sync, region, family)
    except AWSError:
        raise
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to resolve {family} AMI: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _run_nat_instance_sync(
    region: str, ami_id: str, instance_type: str, subnet_id: str,
    security_group_ids: list, user_data: str, name_tag: str,
) -> dict:
    ec2 = _get_ec2(region)
    tags = [
        {"Key": "Name", "Value": name_tag},
        # managed-by matches dashboard EC2 instances so the sandbox VPC
        # sweep / rollback cleans the NAT up too.
        {"Key": "managed-by", "Value": "vm-dashboard"},
    ]
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
        BlockDeviceMappings=_root_bdm_sync(region, ami_id),
        TagSpecifications=[{"ResourceType": "instance", "Tags": tags},
                           _volume_tag_spec(tags)],
    )
    inst = resp["Instances"][0]
    return {"instance_id": inst["InstanceId"], "state": inst["State"]["Name"]}


async def run_nat_instance(
    region: str, *, ami_id: str, instance_type: str, subnet_id: str,
    security_group_ids: list, user_data: str, name_tag: str,
) -> dict:
    """Launch the shared NAT instance (auto public IP, no instance profile, no EIP)."""
    try:
        return await _to_thread(
            _run_nat_instance_sync, region, ami_id, instance_type, subnet_id,
            security_group_ids, user_data, name_tag,
        )
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to launch NAT instance: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── Managed container nodes on EC2 (Portainer server / Rancher server) ───────
# The dashboard's two managed-container nodes -- the Portainer CE server and the
# Rancher management plane -- run as ONE container on ONE VM with a public,
# source-restricted IP. On GCP that is a COS instance whose container comes from the
# `gce-container-declaration` metadata; there is no AWS equivalent of konlet, so the
# EC2 side runs `docker run` from user-data instead.
#
# The AMI is the ECS-optimized Amazon Linux 2023 image, resolved per region from its
# SSM public parameter (the same one the Gateway host uses). It already carries Docker,
# so nothing is installed at boot -- but the instance deliberately does NOT join an ECS
# cluster and gets no instance profile: these nodes need no AWS API access, exactly like
# their GCE counterparts, and joining a cluster would add a task definition and the
# ecsInstanceRole policy dependency for no benefit.
#
# NOTE ON USER-DATA AND SECRETS: user-data is readable from inside the instance (IMDS)
# and via ec2:DescribeInstanceAttribute. The Rancher bootstrap password and Portainer's
# bcrypt admin hash therefore live at the same exposure as GCE instance metadata does
# for the same two values -- this is parity with the existing GCP path, not a new
# weakening, but it is the reason neither node is given a real secret beyond its own
# first-run credential.

_NODE_MANAGED_TAG = "vm-dashboard"

#: The ECS-optimized Amazon Linux 2023 AMI, per region, from its SSM public parameter.
#: Public because the managed nodes want the same image the Gateway host uses -- it
#: already carries Docker -- and resolving it here means there is no per-region AMI map
#: to maintain anywhere.
ECS_OPTIMIZED_AMI_SSM = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"

#: Where a durable data volume gets mounted, and the device slot it is asked for.
#: On Nitro instances the requested name is NOT what appears in /dev, which is why the
#: user-data below resolves the volume through /dev/disk/by-id instead of trusting it.
NODE_DATA_MOUNT = "/mnt/node-data"
_NODE_DATA_DEVICE = "/dev/sdf"


def container_node_user_data(docker_cmd: str, *, data_volume_id: str = "",
                             mount_path: str = NODE_DATA_MOUNT) -> str:
    """Cloud-init-free bash user-data that starts one container, after mounting the
    node's durable volume if it has one.

    The wait loop is load-bearing rather than defensive. An existing EBS volume cannot
    be attached by ``run_instances`` (block-device mappings only create new ones), so
    the attach happens *after* the instance is running -- which is after user-data has
    started. Without the wait, Portainer would come up against an unmounted directory
    and write its database to the root volume, which is precisely the failure GCP avoids
    by letting konlet own the mount. Mount first, container second, always.

    The device is resolved through ``/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_*``
    because on Nitro instances the requested ``/dev/sdf`` is not what shows up in /dev;
    the requested path is kept as a fallback for the older instance families where it is.
    """
    lines = ["#!/bin/bash", "set -uo pipefail",
             "systemctl enable --now docker || true"]
    if data_volume_id:
        by_id = f"/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_{data_volume_id.replace('-', '')}"
        lines += [
            f'MP={mount_path}',
            f'BYID={by_id}',
            f'DEV={_NODE_DATA_DEVICE}',
            'mkdir -p "$MP"',
            # Up to 5 minutes: the attach is issued once the instance reaches `running`,
            # which can trail user-data by a while on a cold start.
            'for _ in $(seq 1 60); do',
            '  if [ -b "$BYID" ]; then DEV="$BYID"; break; fi',
            '  if [ -b "$DEV" ]; then break; fi',
            '  sleep 5',
            'done',
            'if [ -b "$DEV" ]; then',
            # blkid is the "has this ever been formatted?" test. Formatting a volume that
            # already holds the node's state would be an unrecoverable data loss, so the
            # mkfs is strictly conditional on blkid finding nothing.
            '  if ! blkid "$DEV" >/dev/null 2>&1; then mkfs.ext4 -F "$DEV"; fi',
            '  mount "$DEV" "$MP"',
            '  echo "$DEV $MP ext4 defaults,nofail 0 2" >> /etc/fstab',
            'else',
            # Fail loudly in the log rather than silently starting the container against
            # the root volume -- a node that looks fine and loses its state on the next
            # recreate is the worse outcome.
            '  echo "node-data volume never appeared; refusing to start the container" >&2',
            '  exit 1',
            'fi',
        ]
    lines.append(docker_cmd)
    return "\n".join(lines) + "\n"


def _root_device_name_sync(region: str, ami_id: str) -> str:
    """The AMI's root device name, so a resized root volume maps to the right slot.

    Guessing ``/dev/xvda`` works until it doesn't: a block-device mapping whose name
    does not match the AMI's root device silently creates a SECOND volume and leaves the
    root at its default size.
    """
    ec2 = _get_ec2(region)
    images = ec2.describe_images(ImageIds=[ami_id]).get("Images") or []
    return (images[0].get("RootDeviceName") if images else "") or "/dev/xvda"


def _root_bdm_sync(region: str, ami_id: str, *, size_gb: int = 0) -> list:
    """Root block-device mapping that pins the disk to die with the instance, on gp3.

    Custom Packer AMIs routinely ship ``DeleteOnTermination: false`` on the root device
    — of this account's seven self-owned AMIs, ``rocky9-pws-ready`` and ``ot-sim`` both
    do — so any launch that passes no mapping of its own inherits that flag and strands
    an untagged 21 GB "available" volume at ~$2.10/mo on every terminate.
    ``terraform/ec2_instance`` has overridden this since 0c08347; the boto3 launch paths
    did not, which is where the orphans in this account actually came from.

    gp3 rather than the AMI's gp2 for the same reason the cost doc prefers it: ~20%
    cheaper at equal baseline performance. Size is left to the snapshot unless a caller
    asks for more — a VolumeSize smaller than the snapshot is rejected by EC2.
    """
    ebs: dict = {"DeleteOnTermination": True, "VolumeType": "gp3"}
    if size_gb:
        ebs["VolumeSize"] = int(size_gb)
    return [{"DeviceName": _root_device_name_sync(region, ami_id), "Ebs": ebs}]


def _volume_tag_spec(tags: list) -> dict:
    """Tag spec so the root volume carries the same tags as its instance.

    ``ResourceType: instance`` does NOT propagate to volumes. Without this an orphan is
    invisible to cost allocation — it lands in the "unattributed" bucket with no owner,
    which is exactly what made the two dead volumes in this account untraceable."""
    return {"ResourceType": "volume", "Tags": tags}


def _run_ec2_container_node_sync(
    region: str, ami_id: str, instance_type: str, subnet_id: str,
    security_group_ids: list, user_data: str, name_tag: str, purpose: str,
    root_disk_gb: int,
) -> dict:
    ec2 = _get_ec2(region)
    node_tags = [
        {"Key": "Name", "Value": name_tag},
        {"Key": "managed-by", "Value": _NODE_MANAGED_TAG},
        # `purpose` is how the node is found again, mirroring the GCE label of
        # the same name. Without it a node is indistinguishable from any other
        # dashboard EC2 instance.
        {"Key": "purpose", "Value": purpose},
    ]
    kwargs = {
        "ImageId": ami_id,
        "InstanceType": instance_type,
        "MinCount": 1,
        "MaxCount": 1,
        # Auto-assigned public IP, not an Elastic IP: the node is disposable and an
        # unattached EIP bills. This matches the GCE side's ephemeral external IP, and
        # with it the caveat that a recreate changes the address.
        "NetworkInterfaces": [{
            "DeviceIndex": 0,
            "SubnetId": subnet_id,
            "Groups": security_group_ids,
            "AssociatePublicIpAddress": True,
        }],
        "UserData": user_data,  # boto3 base64-encodes for run_instances
        # Unconditional, not `if root_disk_gb`: the mapping is what pins
        # DeleteOnTermination, so gating it on a resize meant a default-sized node
        # inherited the AMI's flag and stranded its root volume on terminate.
        "BlockDeviceMappings": _root_bdm_sync(region, ami_id, size_gb=root_disk_gb),
        "TagSpecifications": [{"ResourceType": "instance", "Tags": node_tags},
                              _volume_tag_spec(node_tags)],
    }
    inst = ec2.run_instances(**kwargs)["Instances"][0]
    return {"instance_id": inst["InstanceId"], "state": inst["State"]["Name"]}


async def run_ec2_container_node(
    region: str, *, ami_id: str, instance_type: str, subnet_id: str,
    security_group_ids: list, user_data: str, name_tag: str, purpose: str,
    root_disk_gb: int = 0,
) -> dict:
    """Launch a managed container node (no instance profile, auto public IP)."""
    try:
        return await _to_thread(
            _run_ec2_container_node_sync, region, ami_id, instance_type, subnet_id,
            security_group_ids, user_data, name_tag, purpose, root_disk_gb,
        )
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to launch the {purpose} node: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _format_container_node(inst: dict, purpose: str) -> dict:
    """Project an EC2 instance into the cloud-neutral node shape the UI reads.

    ``zone`` carries the availability zone: it is the field the node tables and the
    relocation check already read, and an AZ is the AWS answer to the same question.
    """
    tags = {t.get("Key"): t.get("Value") for t in (inst.get("Tags") or [])}
    ports = {"rancher": 443, "portainer": 9443}.get(purpose, 443)
    ip = inst.get("PublicIpAddress") or ""
    launched = inst.get("LaunchTime")
    return {
        "name": tags.get("Name", "") or inst.get("InstanceId", ""),
        "instance_id": inst.get("InstanceId", ""),
        "zone": (inst.get("Placement") or {}).get("AvailabilityZone", ""),
        # EC2 state names are lowercase; the node tables badge on RUNNING, so normalise
        # here rather than making every reader know which cloud it is looking at.
        "status": (inst.get("State") or {}).get("Name", "unknown").upper(),
        "machine_type": inst.get("InstanceType", ""),
        "image": tags.get("container-image", ""),
        "internal_ip": inst.get("PrivateIpAddress") or "",
        "external_ip": ip,
        "url": (f"https://{ip}" if ports == 443 else f"https://{ip}:{ports}") if ip else "",
        "created_at": launched.isoformat() if launched else None,
    }


def _list_ec2_container_nodes_sync(region: str, purpose: str) -> list:
    ec2 = _get_ec2(region)
    resp = ec2.describe_instances(Filters=[
        {"Name": "tag:purpose", "Values": [purpose]},
        {"Name": "tag:managed-by", "Values": [_NODE_MANAGED_TAG]},
        # A terminated instance is gone but lingers in describe_instances for an hour;
        # reporting it would make a torn-down node look like it is still there.
        {"Name": "instance-state-name",
         "Values": ["pending", "running", "stopping", "stopped"]},
    ])
    out = []
    for res in resp.get("Reservations", []):
        for inst in res.get("Instances", []):
            out.append(_format_container_node(inst, purpose))
    return out


async def list_ec2_container_nodes(region: str, purpose: str) -> list:
    """Every managed container node of ``purpose`` in ``region`` (tag query)."""
    try:
        return await _to_thread(_list_ec2_container_nodes_sync, region, purpose)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to list {purpose} nodes: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


_NODE_READY_TIMEOUT_S = 240
_NODE_READY_POLL_S = 5


async def await_container_node(region: str, instance_id: str, purpose: str) -> dict:
    """Read a just-launched node back once it has an address, in the same shape the UI
    and the relocation check use.

    ``run_instances`` answers before the instance has a public IP or an AZ recorded, and
    the deploy needs both immediately: the URL it pins, and the zone it persists so a
    later teardown looks in the right place. Polling here rather than in the caller
    keeps "what is this node" answered by one function whatever created it.
    """
    import asyncio
    deadline = asyncio.get_event_loop().time() + _NODE_READY_TIMEOUT_S
    last = {}
    while asyncio.get_event_loop().time() < deadline:
        for node in await list_ec2_container_nodes(region, purpose):
            if node.get("instance_id") == instance_id:
                last = node
                if node.get("external_ip"):
                    return node
        await asyncio.sleep(_NODE_READY_POLL_S)
    # Hand back whatever was seen rather than raising: the caller checks for the
    # address and reports a far more specific failure than "timed out" would be.
    return last or {"instance_id": instance_id, "status": "UNKNOWN"}


def _subnet_auto_assigns_public_ips_sync(region: str, subnet_id: str) -> Optional[bool]:
    ec2 = _get_ec2(region)
    subnets = ec2.describe_subnets(SubnetIds=[subnet_id]).get("Subnets") or []
    if not subnets:
        return None
    return bool(subnets[0].get("MapPublicIpOnLaunch"))


async def subnet_auto_assigns_public_ips(region: str, subnet_id: str) -> Optional[bool]:
    """Whether ``subnet_id`` hands its instances a public IP at launch.

    EC2 has no per-instance external-IP switch the way GCE and Azure do -- the
    subnet's MapPublicIpOnLaunch decides -- so this is the only way to know, before
    launching, whether an instance will come up addressable from the internet.

    ``None`` means "could not tell" (no such subnet, or the read failed): callers
    must treat that as unknown rather than as either answer, because both a false
    refusal and a false all-clear are worse than saying so.
    """
    try:
        return await _to_thread(_subnet_auto_assigns_public_ips_sync, region, subnet_id)
    except (ClientError, BotoCoreError) as e:
        logger.warning("could not read subnet %s in %s: %s", subnet_id, region, e)
        return None


def _subnet_availability_zone_sync(region: str, subnet_id: str) -> str:
    ec2 = _get_ec2(region)
    subnets = ec2.describe_subnets(SubnetIds=[subnet_id]).get("Subnets") or []
    return (subnets[0].get("AvailabilityZone") if subnets else "") or ""


async def subnet_availability_zone(region: str, subnet_id: str) -> str:
    """The AZ a subnet lives in.

    A data volume is zonal and may only be attached inside one AZ, so this is the only
    AZ a new one may be created in -- guessing would produce a volume the instance can
    never mount, which surfaces as a mount failure at boot rather than an API error.
    """
    try:
        az = await _to_thread(_subnet_availability_zone_sync, region, subnet_id)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to read subnet {subnet_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")
    if not az:
        raise AWSError(f"Subnet {subnet_id} was not found in {region}, so the node's "
                       f"data volume has no availability zone to be created in.")
    return az


def _set_source_dest_check_sync(region: str, instance_id: str, value: bool) -> None:
    ec2 = _get_ec2(region)
    ec2.modify_instance_attribute(InstanceId=instance_id, SourceDestCheck={"Value": value})


async def set_source_dest_check(region: str, instance_id: str, value: bool) -> None:
    """Toggle an instance's source/dest check (must be False to route/NAT)."""
    try:
        await _to_thread(_set_source_dest_check_sync, region, instance_id, value)
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
        return await _to_thread(_get_instance_primary_eni_sync, region, instance_id)
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
        await _to_thread(_upsert_default_route_via_eni_sync, region, rt_id, eni_id)
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
        await _to_thread(_delete_default_route_sync, region, rt_id)
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
        return await _to_thread(_ensure_nat_security_group_sync, region, vpc_id, vpc_cidr, name)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to ensure NAT security group: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── SSM interface VPC-endpoint primitives (on-demand private-subnet SSM; see
#    ssm_endpoint_service). Interface endpoints bill hourly, so the dashboard
#    creates them on the first EC2/DB deploy and deletes them with the last. ─────

def _ensure_ssm_vpce_security_group_sync(region: str, vpc_id: str, vpc_cidr: str, name: str) -> str:
    ec2 = _get_ec2(region)
    resp = ec2.describe_security_groups(Filters=[
        {"Name": "group-name", "Values": [name]},
        {"Name": "vpc-id", "Values": [vpc_id]},
    ])
    if resp.get("SecurityGroups"):
        return resp["SecurityGroups"][0]["GroupId"]
    sg = ec2.create_security_group(
        GroupName=name,
        Description="SSM interface endpoints - HTTPS ingress from this VPC",
        VpcId=vpc_id,
        TagSpecifications=[{
            "ResourceType": "security-group",
            # dashboard-sandbox DELIBERATELY, unlike the endpoint itself (which is
            # vm-dashboard so its ~$7/mo lands in the dashboard scope on /costs).
            # rollback.sh:48 builds Name=tag:managed-by,Values=dashboard-sandbox and
            # step 2b deletes security groups with exactly that filter; AWS refuses
            # DeleteVpc while any non-default SG remains, so retagging this would leave
            # the SG behind and wedge the whole sandbox teardown. Security groups are
            # free and never appear in Cost Explorer, so this costs no attribution.
            "Tags": [{"Key": "Name", "Value": name},
                     {"Key": "managed-by", "Value": "dashboard-sandbox"}],
        }],
    )
    sg_id = sg["GroupId"]
    # SSM agents reach the endpoints over HTTPS only, from anywhere in the VPC.
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
                        "IpRanges": [{"CidrIp": vpc_cidr}]}],
    )
    return sg_id


async def ensure_ssm_vpce_security_group(region: str, *, vpc_id: str, vpc_cidr: str, name: str) -> str:
    """Find-or-create the SSM-endpoint SG (443 ingress from ``vpc_cidr``)."""
    try:
        return await _to_thread(_ensure_ssm_vpce_security_group_sync, region, vpc_id, vpc_cidr, name)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to ensure SSM endpoint security group: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _node_ingress_permissions(pairs) -> list:
    """Group ``(port, cidr)`` pairs into IpPermissions -- one per port, carrying every
    CIDR for it. Both the authorize and the revoke call take this shape, so grouping it
    once is what keeps the two halves of the diff from disagreeing."""
    by_port: dict = {}
    for port, cidr in sorted(pairs):
        by_port.setdefault(int(port), []).append(cidr)
    return [{"IpProtocol": "tcp", "FromPort": p, "ToPort": p,
             "IpRanges": [{"CidrIp": c} for c in cidrs]}
            for p, cidrs in sorted(by_port.items())]


def _current_node_ingress(sg: dict, ports) -> set:
    """The ``(port, cidr)`` pairs currently allowed on the node's ports.

    Scoped to the node's own ports on purpose: a rule someone added by hand on another
    port is theirs, and revoking it would be this code quietly taking ownership of a
    group it only manages one aspect of.
    """
    wanted = {int(p) for p in ports}
    have = set()
    for perm in sg.get("IpPermissions") or []:
        if (perm.get("IpProtocol") or "").lower() != "tcp":
            continue
        frm, to = perm.get("FromPort"), perm.get("ToPort")
        if frm is None or frm != to or frm not in wanted:
            continue
        for r in perm.get("IpRanges") or []:
            if r.get("CidrIp"):
                have.add((frm, r["CidrIp"]))
    return have


def _ensure_node_security_group_sync(
    region: str, vpc_id: str, name: str, ports: list, source_cidrs: list,
) -> dict:
    """Make the node's security group allow exactly ``source_cidrs`` on ``ports``.

    The GCE analogue is one firewall rule that gets PATCHed, and whose absence means
    closed. A security group has no such single-shot replace, so the group is diffed:
    authorize what is missing, revoke what is no longer wanted. That difference matters
    for fail-closed -- an EMPTY source set revokes every rule rather than deleting the
    group, because a group attached to a running instance cannot be deleted. The node
    ends up unreachable either way, which is the contract callers key off ``opened`` for.
    """
    ec2 = _get_ec2(region)
    created = False
    sg_id = _find_security_group_id_sync(region, vpc_id, name)
    if not sg_id:
        if not source_cidrs:
            # Nothing to open and nothing to close. Creating an empty group here would
            # leave litter behind on an install that never finishes a deploy.
            return {"name": name, "id": "", "opened": False, "created": False}
        sg_id = ec2.create_security_group(
            GroupName=name, VpcId=vpc_id,
            Description="vm-dashboard managed node: source-restricted ingress",
        )["GroupId"]
        created = True
        try:
            ec2.create_tags(Resources=[sg_id], Tags=[
                {"Key": "Name", "Value": name},
                {"Key": "managed-by", "Value": _NODE_MANAGED_TAG},
            ])
        except (ClientError, BotoCoreError) as e:  # tags are cosmetic
            logger.warning("node SG %s: tagging failed (continuing): %s", name, e)

    sg = ec2.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
    have = _current_node_ingress(sg, ports)
    want = {(int(p), c) for p in ports for c in source_cidrs}

    to_add, to_remove = want - have, have - want
    if to_add:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id, IpPermissions=_node_ingress_permissions(to_add))
    if to_remove:
        ec2.revoke_security_group_ingress(
            GroupId=sg_id, IpPermissions=_node_ingress_permissions(to_remove))
    return {"name": name, "id": sg_id, "opened": bool(source_cidrs), "created": created}


async def ensure_node_security_group(
    region: str, *, vpc_id: str, name: str, ports: list, source_cidrs: list,
) -> dict:
    """Converge a managed node's ingress on ``source_cidrs``. Fail-closed on empty."""
    try:
        return await _to_thread(
            _ensure_node_security_group_sync, region, vpc_id, name,
            list(ports), list(source_cidrs),
        )
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to apply ingress for {name}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _release_node_security_group_sync(region: str, vpc_id: str, name: str) -> bool:
    """Delete the node's own security group once nothing references it.

    A teardown revokes the group's rules while the instance is still terminating
    (fail-closed can't wait), which leaves an EMPTY group behind. That is not
    cosmetic: an unreferenced security group blocks a later VPC delete with an opaque
    DependencyViolation, and nothing else in the sandbox rollback claims it -- the
    group carries the dashboard's own tag, not the sandbox's.

    Retried rather than waited on, because the blocker is the terminating instance's
    ENI being released, and there is no waiter for that.
    """
    import time
    ec2 = _get_ec2(region)
    sg_id = _find_security_group_id_sync(region, vpc_id, name)
    if not sg_id:
        return True
    for attempt in range(12):
        try:
            ec2.delete_security_group(GroupId=sg_id)
            return True
        except ClientError as exc:
            code = (exc.response.get("Error") or {}).get("Code", "")
            if code != "DependencyViolation":
                raise
            if attempt == 11:
                logger.warning(
                    "node SG %s (%s) is still referenced after termination — leaving it; "
                    "it will block a VPC delete until removed", name, sg_id)
                return False
            time.sleep(5)
    return False


async def release_node_security_group(region: str, *, vpc_id: str, name: str) -> bool:
    """Best-effort: remove the node's security group after its instance is gone."""
    try:
        return await _to_thread(_release_node_security_group_sync, region, vpc_id, name)
    except (ClientError, BotoCoreError) as e:
        logger.warning("node SG %s: delete failed (continuing): %s", name, e)
        return False
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── Durable data volume for a managed node (Portainer /data) ─────────────────

def _find_node_data_volume_sync(region: str, name: str) -> dict:
    ec2 = _get_ec2(region)
    vols = ec2.describe_volumes(Filters=[
        {"Name": "tag:Name", "Values": [name]},
        {"Name": "tag:managed-by", "Values": [_NODE_MANAGED_TAG]},
        {"Name": "status", "Values": ["available", "in-use", "creating"]},
    ]).get("Volumes") or []
    if not vols:
        return {}
    v = vols[0]
    return {"volume_id": v["VolumeId"], "zone": v.get("AvailabilityZone", ""),
            "size_gb": v.get("Size", 0), "state": v.get("State", "")}


async def find_node_data_volume(region: str, name: str) -> dict:
    """The node's durable data volume, or ``{}``.

    Read BEFORE a launch: an EBS volume is zonal, so an existing one pins the node's AZ,
    and a volume that already holds an initialized Portainer database means the stored
    admin credential -- not a freshly generated one -- is the only one that can sign in.
    """
    try:
        return await _to_thread(_find_node_data_volume_sync, region, name)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to look up the data volume {name}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _ensure_node_data_volume_sync(region: str, name: str, az: str, size_gb: int) -> dict:
    existing = _find_node_data_volume_sync(region, name)
    if existing:
        return {**existing, "created": False}
    ec2 = _get_ec2(region)
    v = ec2.create_volume(
        AvailabilityZone=az, Size=int(size_gb), VolumeType="gp3",
        TagSpecifications=[{"ResourceType": "volume", "Tags": [
            {"Key": "Name", "Value": name},
            {"Key": "managed-by", "Value": _NODE_MANAGED_TAG},
        ]}],
    )
    ec2.get_waiter("volume_available").wait(
        VolumeIds=[v["VolumeId"]],
        WaiterConfig={"Delay": 5, "MaxAttempts": 24})
    return {"volume_id": v["VolumeId"], "zone": az, "size_gb": int(size_gb),
            "state": "available", "created": True}


async def ensure_node_data_volume(region: str, *, name: str, az: str,
                                  size_gb: int) -> dict:
    """Find-or-create the node's durable data volume in ``az``."""
    try:
        return await _to_thread(_ensure_node_data_volume_sync, region, name, az, size_gb)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to ensure the data volume {name}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _attach_node_data_volume_sync(region: str, volume_id: str, instance_id: str,
                                  device: str) -> None:
    ec2 = _get_ec2(region)
    vol = ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]
    for att in vol.get("Attachments") or []:
        if att.get("InstanceId") == instance_id:
            return  # already ours -- a redeploy that reused the instance
    ec2.get_waiter("instance_running").wait(
        InstanceIds=[instance_id], WaiterConfig={"Delay": 5, "MaxAttempts": 60})
    ec2.attach_volume(VolumeId=volume_id, InstanceId=instance_id, Device=device)
    ec2.get_waiter("volume_in_use").wait(
        VolumeIds=[volume_id], WaiterConfig={"Delay": 5, "MaxAttempts": 24})
    # Without this the volume goes away with the instance, which is the opposite of what
    # "durable state" means -- and it would be discovered only on the next teardown.
    ec2.modify_instance_attribute(
        InstanceId=instance_id,
        BlockDeviceMappings=[{"DeviceName": device,
                              "Ebs": {"DeleteOnTermination": False}}])


async def attach_node_data_volume(region: str, *, volume_id: str, instance_id: str,
                                  device: str = _NODE_DATA_DEVICE) -> None:
    """Attach the durable volume to a running node, and pin it to survive termination."""
    try:
        await _to_thread(_attach_node_data_volume_sync, region, volume_id,
                         instance_id, device)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to attach the data volume {volume_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _delete_node_data_volume_sync(region: str, volume_id: str) -> None:
    ec2 = _get_ec2(region)
    try:
        ec2.get_waiter("volume_available").wait(
            VolumeIds=[volume_id], WaiterConfig={"Delay": 5, "MaxAttempts": 36})
    except Exception as exc:  # noqa: BLE001 -- try the delete anyway and report ITS error
        logger.warning("data volume %s did not detach cleanly (%s) — attempting delete",
                       volume_id, exc)
    ec2.delete_volume(VolumeId=volume_id)


async def delete_node_data_volume(region: str, volume_id: str) -> None:
    """Delete a node's data volume once its instance has released it.

    Waits for ``available`` first: a volume still attached to a terminating instance
    refuses deletion, and the raw error ("VolumeInUse") reads as a permissions problem.
    """
    try:
        await _to_thread(_delete_node_data_volume_sync, region, volume_id)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to delete the data volume {volume_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _find_security_group_id_sync(region: str, vpc_id: str, name: str) -> Optional[str]:
    ec2 = _get_ec2(region)
    resp = ec2.describe_security_groups(Filters=[
        {"Name": "group-name", "Values": [name]},
        {"Name": "vpc-id", "Values": [vpc_id]},
    ])
    sgs = resp.get("SecurityGroups")
    return sgs[0]["GroupId"] if sgs else None


async def find_security_group_id(region: str, *, vpc_id: str, name: str) -> Optional[str]:
    """Return the id of the SG named ``name`` in ``vpc_id`` (or None)."""
    try:
        return await _to_thread(_find_security_group_id_sync, region, vpc_id, name)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to look up security group {name}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _find_ssm_endpoints_sync(region: str, vpc_id: str, service_names: list) -> dict:
    ec2 = _get_ec2(region)
    resp = ec2.describe_vpc_endpoints(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "service-name", "Values": service_names},
    ])
    out: dict = {}
    for ep in resp.get("VpcEndpoints", []):
        out[ep["ServiceName"]] = {"endpoint_id": ep["VpcEndpointId"], "state": ep.get("State")}
    return out


async def find_ssm_endpoints(region: str, *, vpc_id: str, service_names: list) -> dict:
    """Return {service_name: {endpoint_id, state}} for the given VPC + service names."""
    try:
        return await _to_thread(_find_ssm_endpoints_sync, region, vpc_id, service_names)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to list VPC endpoints: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _count_vpc_lambdas_sync(region: str, vpc_id: str) -> int:
    _require_boto3()
    lam = boto3.client("lambda", **_aws_kwargs(region))
    n = 0
    for page in lam.get_paginator("list_functions").paginate():
        for fn in page.get("Functions", []):
            if ((fn.get("VpcConfig") or {}).get("VpcId") or "") == vpc_id:
                n += 1
    return n


async def count_vpc_lambdas(region: str, *, vpc_id: str) -> int:
    """How many Lambda functions are attached to this VPC.

    Gates the ``secretsmanager`` endpoint sweep. A vpc-mode Lambda in a private subnet
    has no route to the public Secrets Manager API, so deleting the endpoint under it
    fails its secret reads at RUNTIME with a timeout, not at deploy time — the exact
    silent breakage the cost doc warns about. Asked of AWS rather than the dashboard DB
    because the sandbox script creates these functions, so no local row would exist."""
    try:
        return await _to_thread(_count_vpc_lambdas_sync, region, vpc_id)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to list Lambda functions: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _create_ssm_endpoint_sync(region: str, vpc_id: str, service_name: str, subnet_id: str,
                              security_group_ids: list, name_tag: str) -> str:
    ec2 = _get_ec2(region)
    resp = ec2.create_vpc_endpoint(
        VpcEndpointType="Interface",
        VpcId=vpc_id,
        ServiceName=service_name,
        SubnetIds=[subnet_id],
        SecurityGroupIds=security_group_ids,
        PrivateDnsEnabled=True,
        TagSpecifications=[{
            "ResourceType": "vpc-endpoint",
            # vm-dashboard, not dashboard-sandbox: the DASHBOARD creates and deletes
            # these, and each bills ~$7/mo while it exists, so they belong in the
            # dashboard scope on /costs (cost_service.get_aws_managed_breakdown).
            # Safe for teardown — rollback.sh sweeps endpoints by vpc-id, not by tag.
            "Tags": [{"Key": "Name", "Value": name_tag},
                     {"Key": "managed-by", "Value": "vm-dashboard"}],
        }],
    )
    return resp["VpcEndpoint"]["VpcEndpointId"]


async def create_ssm_endpoint(region: str, *, vpc_id: str, service_name: str, subnet_id: str,
                              security_group_ids: list, name_tag: str) -> str:
    """Create one Interface VPC endpoint (private DNS enabled) — mirrors the sandbox
    script's ``aws ec2 create-vpc-endpoint``."""
    try:
        return await _to_thread(
            _create_ssm_endpoint_sync, region, vpc_id, service_name, subnet_id,
            security_group_ids, name_tag)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to create VPC endpoint {service_name}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _delete_ssm_endpoints_sync(region: str, endpoint_ids: list) -> None:
    if not endpoint_ids:
        return
    ec2 = _get_ec2(region)
    ec2.delete_vpc_endpoints(VpcEndpointIds=endpoint_ids)
    # Interface-endpoint ENIs linger briefly after delete; wait so a follow-up SG
    # delete doesn't hit DependencyViolation and the subnet/VPC can be torn down.
    import time
    for _ in range(24):  # ~60s budget
        resp = ec2.describe_vpc_endpoints(
            Filters=[{"Name": "vpc-endpoint-id", "Values": endpoint_ids}])
        remaining = [e for e in resp.get("VpcEndpoints", [])
                     if e.get("State") not in ("deleted", None)]
        if not remaining:
            return
        time.sleep(2.5)


async def delete_ssm_endpoints(region: str, endpoint_ids: list) -> None:
    """Delete VPC endpoints and wait for their ENIs to clear."""
    try:
        await _to_thread(_delete_ssm_endpoints_sync, region, endpoint_ids)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to delete VPC endpoints: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _delete_security_group_sync(region: str, sg_id: str) -> None:
    ec2 = _get_ec2(region)
    import time
    last_err = None
    for _ in range(6):  # ~15s: ENIs from a just-deleted endpoint may still detach
        try:
            ec2.delete_security_group(GroupId=sg_id)
            return
        except ClientError as e:
            msg = str(e)
            if "InvalidGroup.NotFound" in msg:
                return
            if "DependencyViolation" not in msg:
                raise
            last_err = e
        time.sleep(2.5)
    if last_err:
        raise last_err


async def delete_security_group(region: str, sg_id: str) -> None:
    """Delete a security group (no-op if already gone; retries past a transient
    DependencyViolation while endpoint ENIs finish detaching)."""
    try:
        await _to_thread(_delete_security_group_sync, region, sg_id)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to delete security group {sg_id}: {e}") from e
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
        await _to_thread(_set_workgroup_tag_sync, region, instance_id, workgroup)
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
    # subnet (where the on-demand NAT route applies) + its VM-tier SG. Resolved for
    # THIS region (per-region config sets fall back to the flat keys), so picking a
    # non-default region pre-selects that region's subnet/SG. Empty when unset — the
    # form then leaves the pickers unselected.
    from .region_config import resolve_region
    _rc = resolve_region("aws", region)
    default_subnet_id = _rc["default_subnet_id"]
    default_sg_id = _rc["default_security_group_id"]

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
        return await _to_thread(_get_network_options_sync, region)
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
        return await _to_thread(_get_db_options_sync, region)
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
        return await _to_thread(_search_community_amis_sync, region, os_filter)
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
        return await _to_thread(_copy_ami_sync, region, source_ami_id, name, description)
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
        return await _to_thread(_get_ami_status_sync, region, ami_id)
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
    host_instance_id: str = "",
) -> str:
    """Ensure the ECS cluster exists, register the task definition if needed,
    then launch one jumpoint task. Returns the task ARN.

    launch_type "EC2" (default) places the task on EC2 capacity with host
    networking so it can do protocol tunneling; "FARGATE" is the legacy,
    tunnel-incapable path. EC2 capacity is provisioned by the sandbox script.

    ``host_instance_id`` pins the task to one container instance. With several
    gateway hosts in the same cluster, ECS's default placement would otherwise be
    free to stack two tasks on one host and leave another bare — each gateway host
    needs its own task, so the caller names the host it just launched.
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
    if ec2 and host_instance_id:
        # Cluster query language; ec2InstanceId is a built-in field.
        run_kwargs["placementConstraints"] = [
            {"type": "memberOf", "expression": f"ec2InstanceId == {host_instance_id}"}
        ]
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
    host_instance_id: str = "",
) -> str:
    """Start an ECS task running the BeyondTrust Gateway container. Returns the
    task ARN. launch_type "EC2" (default) is tunnel-capable; "FARGATE" is legacy.
    ``host_instance_id`` pins the task to one container instance — see
    :func:`_run_ecs_task_sync`."""
    try:
        return await _to_thread(
            _run_ecs_task_sync,
            region, cluster, task_family, subnet_id, security_group_ids,
            deploy_key, cpu, memory, execution_role_arn, image, launch_type,
            host_instance_id,
        )
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to start ECS Jumpoint task: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


async def stop_ecs_jumpoint_task(region: str, cluster: str, task_arn: str) -> None:
    """Stop a running ECS Jumpoint task."""
    try:
        await _to_thread(_stop_ecs_task_sync, region, cluster, task_arn)
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
        return await _to_thread(_list_container_instances_sync, region, cluster)
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
        return await _to_thread(_list_ecs_tasks_sync, region, cluster, include_stopped)
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
    ps_env: dict | None = None,
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
        # The SSH key and the PASSWORD_SAFE_* creds ride the RunTask override
        # (ephemeral, tiny — comfortably under the 8192-byte overrides cap), so the
        # client secret is never retained in task-def revision history; everything
        # non-secret is on the task def above.
        overrides={"containerOverrides": [{
            "name": "ansible",
            "environment": (
                [{"name": "SSH_KEY_B64", "value": ssh_key_b64}]
                + [{"name": k, "value": v} for k, v in (ps_env or {}).items()]
            ),
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
    ps_env: dict | None = None,
) -> tuple:
    """Run an Ansible playbook via ECS Fargate. Returns (exit_code, output)."""
    try:
        return await _to_thread(
            _run_ecs_ansible_sync,
            region, cluster, task_family, image, cpu, memory,
            subnet_id, security_group_ids, execution_role_arn,
            target_ip, ansible_user, playbook_b64, ssh_key_b64, job_id,
            secret_entries, manifest_b64, ps_env,
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
        return await _to_thread(
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


# ── ECS Ansible localhost runner (Kubernetes-cluster / cloud-database targets) ──

def _run_ecs_ansible_local_sync(
    region: str,
    cluster: str,
    task_family: str,
    image: str,
    cpu: str,
    memory: str,
    subnet_id: str,
    security_group_ids: list,
    execution_role_arn: str,
    playbook_b64: str,
    conn_vars_b64: str,
    kubeconfig_b64: str,
    job_id: str,
    ps_env: dict | None = None,
) -> tuple:
    """ECS Fargate task that runs a **localhost** Ansible play — the k8s/DB path,
    where Ansible reaches OUT to the cluster API / DB endpoint instead of SSHing to
    a VM. Mirrors ``_run_ecs_ansible_sync`` (register / run / poll / pull logs); the
    only differences are the localhost command (no ``-i '<ip>,'``, no SSH key) and
    the connection material riding the ephemeral override env. Uses the ansible-cloud
    image, which carries the k8s/DB collections + client libs."""
    import time
    from .ansible_localhost_cmd import build_localhost_command
    ecs = _get_ecs(region)
    logs_client = boto3.client("logs", **_aws_kwargs(region))
    log_group = "/ecs/ansible-runner"
    log_stream_prefix = f"ansible-local/{job_id[:8]}" if job_id else "ansible-local/adhoc"

    try:
        logs_client.create_log_group(logGroupName=log_group)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise

    cmd = build_localhost_command(
        with_conn_vars=bool(conn_vars_b64), with_kubeconfig=bool(kubeconfig_b64))

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
            # The playbook rides the task-def env (it isn't a credential). The
            # connection material (DB password / kubeconfig bearer token) stays an
            # ephemeral RunTask override so it isn't retained in task-def history.
            "environment": [{"name": "PLAYBOOK_B64", "value": playbook_b64}],
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

    override_env = []
    if conn_vars_b64:
        override_env.append({"name": "CONN_VARS_B64", "value": conn_vars_b64})
    if kubeconfig_b64:
        override_env.append({"name": "KUBECONFIG_B64", "value": kubeconfig_b64})
    # PASSWORD_SAFE_* for an in-playbook beyondtrust.secrets_safe lookup — rides the same
    # ephemeral override env as the connection material, so the client secret is never in
    # task-def revision history.
    override_env += [{"name": k, "value": v} for k, v in (ps_env or {}).items()]

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
            "name": "ansible",
            "environment": override_env,
        }]},
        count=1,
    )

    tasks = run_resp.get("tasks", [])
    if not tasks:
        raise AWSError(f"ECS ansible-local task failed to start: {run_resp.get('failures', [])}")

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
        logger.warning("ECS ansible-local: could not retrieve logs: %s", log_err)

    return exit_code, output


async def run_ecs_ansible_local_task(
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
    playbook_b64: str,
    conn_vars_b64: str = "",
    kubeconfig_b64: str = "",
    job_id: str,
    ps_env: dict | None = None,
) -> tuple:
    """Run a localhost Ansible play (k8s/DB target) via ECS Fargate.
    Returns (exit_code, output)."""
    try:
        return await _to_thread(
            _run_ecs_ansible_local_sync,
            region, cluster, task_family, image, cpu, memory,
            subnet_id, security_group_ids, execution_role_arn,
            playbook_b64, conn_vars_b64, kubeconfig_b64, job_id, ps_env,
        )
    except AWSError:
        raise
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to run ECS ansible-local task: {e}") from e
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
        return await _to_thread(_describe_ami_sync, region, ami_id)
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
        return await _to_thread(_describe_instance_detail_sync, region, instance_id)
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to describe instance {instance_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


def _deregister_ami_sync(region: str, ami_id: str,
                         keep_snapshot_ids: Optional[list] = None) -> list[str]:
    """Deregister an AMI and delete its backing snapshots. Returns list of deleted snapshot IDs.

    ``keep_snapshot_ids`` holds back snapshots another AMI still references. EC2 would
    refuse the delete anyway, but that arrives as a swallowed ClientError below — an
    intent that important should not be expressed as a silently ignored failure.
    """
    ec2 = _get_ec2(region)
    keep = set(keep_snapshot_ids or ())

    # Collect snapshot IDs before deregistering
    resp = ec2.describe_images(ImageIds=[ami_id], Owners=["self"])
    images = resp.get("Images", [])
    if not images:
        raise AWSError(f"AMI {ami_id} not found or not owned by this account.")

    snapshot_ids = [
        mapping["Ebs"]["SnapshotId"]
        for mapping in images[0].get("BlockDeviceMappings", [])
        if "Ebs" in mapping and "SnapshotId" in mapping["Ebs"]
        and mapping["Ebs"]["SnapshotId"] not in keep
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
        return await _to_thread(_deregister_ami_sync, region, ami_id)
    except AWSError:
        raise
    except (ClientError, BotoCoreError) as e:
        raise AWSError(f"Failed to deregister AMI {ami_id}: {e}") from e
    except NoCredentialsError:
        raise AWSError("AWS credentials not configured.")


# ── Re-registering an AMI (rename, ENA, block-device fixes) ───────────────────
#
# register_image is the ONLY point at which an AMI's block-device mapping can be
# set: import_image auto-generates one and copy_image has no mapping parameter at
# all. Anything that has to correct a mapping therefore has to re-register.

# Attributes describe_images reports that register_image can carry forward. Omitting
# one silently downgrades the image rather than failing: a UEFI image re-registered
# without BootMode comes back legacy-BIOS (and may not boot), and dropping
# EnaSupport/SriovNetSupport quietly costs the network capability the source
# advertised. copy_image preserved all of this implicitly; register_image sets only
# what it is given, so the list has to be explicit.
_AMI_CARRY_FORWARD = ("Architecture", "VirtualizationType", "EnaSupport",
                      "SriovNetSupport", "BootMode", "TpmSupport", "ImdsSupport",
                      "KernelId", "RamdiskId")


def _ami_bdms_for_register(img: dict, *, pin_delete_on_termination: bool = False) -> list:
    """Project an AMI's block-device mappings into register_image's accepted shape.

    ``pin_delete_on_termination`` forces the ROOT device to die with its instance.
    ImportImage and many Packer builders emit ``DeleteOnTermination: false``, which
    strands an untagged root volume on every terminate — see ``_root_bdm_sync`` for
    the launch-side half of the same defect. Only the root device is pinned: a data
    disk in the mapping may be meant to outlive its instance, and this is not the
    place to decide that.
    """
    root_dev = img.get("RootDeviceName", "")
    bdms = []
    for bdm in img.get("BlockDeviceMappings", []):
        new_bdm: dict = {"DeviceName": bdm["DeviceName"]}
        if "Ebs" in bdm:
            ebs = bdm["Ebs"]
            new_ebs: dict = {k: ebs[k] for k in
                             ("SnapshotId", "VolumeSize", "VolumeType",
                              "DeleteOnTermination") if k in ebs}
            if ebs.get("Encrypted"):
                new_ebs["Encrypted"] = True
            if pin_delete_on_termination and bdm["DeviceName"] == root_dev:
                new_ebs["DeleteOnTermination"] = True
            new_bdm["Ebs"] = new_ebs
        elif "VirtualName" in bdm:
            new_bdm["VirtualName"] = bdm["VirtualName"]
        bdms.append(new_bdm)
    return bdms


def _ami_register_kwargs(img: dict, *, name: str, description: str, bdms: list) -> dict:
    """register_image kwargs that preserve everything describe_images reported."""
    kwargs: dict = {
        "Name": name,
        "RootDeviceName": img.get("RootDeviceName", "/dev/sda1"),
        "BlockDeviceMappings": bdms,
    }
    if description:
        kwargs["Description"] = description
    for key in _AMI_CARRY_FORWARD:
        val = img.get(key)
        if val:
            kwargs[key] = val
    return kwargs


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

    # Pin the root disk while we're re-registering anyway: an ENA copy of an AMI
    # that leaked its root volume would otherwise inherit the leak.
    bdms = _ami_bdms_for_register(img, pin_delete_on_termination=True)

    old_name = img.get("Name", ami_id)
    new_name = (old_name[:124] + "-ena") if len(old_name) > 124 else (old_name + "-ena")

    desc = img.get("Description")
    kwargs = _ami_register_kwargs(
        img, name=new_name, description=(desc + " (ENA enabled)") if desc else "",
        bdms=bdms)
    kwargs["EnaSupport"] = True

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
        return await _to_thread(_register_with_ena_sync, region, ami_id)
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
        return await _to_thread(
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
        return await _to_thread(
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

def _copy_and_rename_ami_sync(
    ec2,
    region: str,
    source_ami_id: str,
    name: str,
    description: str,
    poll_interval: int,
    timeout: int,
    progress_cb,
) -> str:
    """Re-register `source_ami_id` as a new AMI named `name`, wait for it to become
    available, then deregister the source AMI. Returns the new AMI id.

    ec2:ImportImage can't name the AMI it creates — it auto-generates
    ``import-ami-…`` — and an AMI's Name is immutable. To give a promoted AMI
    the same ``{name}-{version}`` identity the Azure/GCP promotes produce, the
    freshly-imported AMI is re-registered under the desired Name and the temporary
    one dropped.

    register_image, NOT copy_image, for two reasons:

      * ImportImage emits ``DeleteOnTermination: false`` on the root device, and
        neither import_image nor copy_image accepts a block-device override — this
        re-register is the only point in the whole promote where that flag can be
        corrected. Left alone it strands an untagged root volume on every terminate
        of a promoted image, which is where this account's orphaned volumes came
        from.
      * copy_image duplicates the snapshot; register_image reuses the imported one,
        so the rename is near-instant instead of a second full snapshot copy, and
        stops paying to store the same bits twice.

    The trade is that register_image sets only what it is given, so
    ``_ami_register_kwargs`` has to carry the boot/capability attributes forward
    explicitly. The new AMI SHARES the source's snapshot — hence the source is
    deregistered with that snapshot held back rather than deleted.
    """
    import time
    if progress_cb:
        progress_cb(f"Renaming imported AMI {source_ami_id} -> '{name}' via register_image")

    imgs = ec2.describe_images(ImageIds=[source_ami_id], Owners=["self"]).get("Images", [])
    if not imgs:
        raise AWSError(f"Imported AMI {source_ami_id} not found or not owned by this account.")
    src = imgs[0]

    bdms = _ami_bdms_for_register(src, pin_delete_on_termination=True)
    shared_snapshot_ids = [m["Ebs"]["SnapshotId"] for m in bdms
                           if "Ebs" in m and "SnapshotId" in m["Ebs"]]
    new_ami_id = ec2.register_image(**_ami_register_kwargs(
        src, name=name,
        description=description or f"Promoted AMI (renamed from {source_ami_id})",
        bdms=bdms,
    ))["ImageId"]
    ec2.create_tags(
        Resources=[new_ami_id],
        Tags=[
            {"Key": "managed-by", "Value": "vm-dashboard"},
            {"Key": "PromotedFrom", "Value": source_ami_id},
            {"Key": "Name", "Value": name},
        ],
    )
    if progress_cb:
        progress_cb(f"Registered {new_ami_id}; waiting for it to become available")

    started = time.time()
    while True:
        imgs = ec2.describe_images(ImageIds=[new_ami_id], Owners=["self"]).get("Images", [])
        state = (imgs[0].get("State") if imgs else "") or ""
        if state == "available":
            break
        if state in ("failed", "error", "invalid", "deregistered"):
            reason = (imgs[0].get("StateReason", {}) or {}).get("Message", "") if imgs else ""
            raise AWSError(
                f"Re-register of {source_ami_id} as '{name}' ended in state '{state}': {reason}"
            )
        if time.time() - started > timeout:
            raise AWSError(
                f"Re-register {new_ami_id} (rename of {source_ami_id}) timed out after "
                f"{timeout}s (last state: {state or 'unknown'})"
            )
        time.sleep(poll_interval)

    if progress_cb:
        progress_cb(f"Renamed AMI available: {new_ami_id}; removing temporary {source_ami_id}")
    # Drop the temporary import-ami-… AMI, but KEEP the snapshot: the AMI we just
    # registered is backed by it. Deleting it would leave the promoted image
    # unlaunchable — an error that surfaces only when someone tries to boot it.
    # The rename already succeeded, so a leftover temp AMI is non-fatal — log via
    # the callback and keep the promote green rather than failing on cleanup.
    try:
        _deregister_ami_sync(region, source_ami_id, keep_snapshot_ids=shared_snapshot_ids)
    except Exception as e:
        if progress_cb:
            progress_cb(f"WARNING: could not clean up temporary AMI {source_ami_id}: {e}")
    return new_ami_id


def _import_image_from_vhd_sync(
    region: str,
    s3_bucket: str,
    s3_key: str,
    role_name: str,
    description: str,
    name: str,
    disk_format: str,
    poll_interval: int,
    timeout: int,
    progress_cb,
) -> dict:
    """Trigger ec2:ImportImage from an S3 object and poll until the import
    completes. Returns {task_id, image_id, status}. Mirrors
    `_export_image_to_vhd_sync`'s polling shape so the promote-flow Job sees
    matching status lines for both directions.

    If `name` is given, the auto-named ``import-ami-…`` produced by ImportImage
    is copied to a new AMI with that Name (its immutable Name can't be changed
    in place) and the temporary one is deregistered, so the promoted AMI ends
    up named like the Azure/GCP targets ({name}-{version}). `image_id` in the
    return is then the renamed AMI.

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
            if name:
                # Give the AMI its {name}-{version} identity — ImportImage names
                # it import-ami-… and that Name is immutable, so copy + rename.
                image_id = _copy_and_rename_ami_sync(
                    ec2, region, image_id, name, description,
                    poll_interval, timeout, progress_cb,
                )
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
    name: str = "",
    disk_format: str = "vhd",
    poll_interval: int = 30,
    timeout: int = 7200,
    progress_cb=None,
) -> dict:
    """Import a VHD (or other supported format) from S3 into a new AMI.
    Returns {task_id, image_id, status}. Polls until terminal state.

    If `name` is given, the imported AMI is copied to a new AMI with that Name
    (ImportImage auto-names it import-ami-…, and the Name is immutable) and the
    temporary one is removed; `image_id` is then the renamed AMI."""
    try:
        return await _to_thread(
            _import_image_from_vhd_sync,
            region, s3_bucket, s3_key, role_name, description, name,
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
        return await _to_thread(
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
        return await _to_thread(
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
