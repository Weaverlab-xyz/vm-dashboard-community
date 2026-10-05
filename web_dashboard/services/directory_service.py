"""
Managed Active Directory for Windows servers on AWS and GCP.

Entra ID join only exists for Windows on Azure VMs, so a Windows server on AWS or GCP
gets its domain identity from Active Directory instead. Both clouds sell one:

* **AWS Directory Service** — Managed Microsoft AD (built here), plus AD Connector and
  Simple AD (discovered and registered, never built).
* **GCP Managed Service for Microsoft Active Directory** — built or discovered.

Two ways a directory reaches the dashboard, the CloudDatabase / K8sCluster split:

* ``provisioned`` — :func:`provision` + :func:`run_provision_apply` build it from
  ``terraform/directory/<module>``, recording state so :func:`run_decommission` destroys
  exactly what was built. The administrator credential is set fresh after the apply and
  stored through ``windows_admin_secret`` (Password Safe / an external manager, never the
  dashboard database), optionally onboarded into Password Safe and rotated.
* ``registered`` — :func:`discover` lists what already exists, :func:`register` records
  one. No credential is stored or needed: joining a server authenticates through the
  cloud on both sides (SSM seamless join, GCE metadata join). Deleting it only forgets it.

Directories carry NO auto-delete timer by default: servers joined to one break when it
goes. And :func:`start_decommission` refuses while any dashboard VM is still joined.
"""

import json
import logging
import os
import re
import secrets
import string
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..database import Job, ManagedDirectory
from . import job_service, terraform, terraform_provider_env

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
# Built with os.path.join(_REPO_ROOT, "terraform", ...) on purpose:
# tests/test_terraform_modules_shipped.py finds module paths with that exact shape.
_TEMPLATE_DIRS = {
    "aws": os.path.join(_REPO_ROOT, "terraform", "directory", "aws_managed_ad"),
    "gcp": os.path.join(_REPO_ROOT, "terraform", "directory", "gcp_managed_ad"),
}
_DEPLOYMENTS_DIR = os.path.join(_REPO_ROOT, "terraform", "deployments")

PROVISIONING_CLOUDS = tuple(sorted(_TEMPLATE_DIRS))
PROVISION_JOB_TYPE = "directory_provision"
DECOMMISSION_JOB_TYPE = "directory_decommission"
INVENTORY_KIND = "directory"

PROVIDER_LABELS = {
    "aws_managed_ad": "AWS Managed Microsoft AD",
    "aws_ad_connector": "AWS AD Connector",
    "aws_simple_ad": "AWS Simple AD",
    "gcp_managed_ad": "GCP Managed Microsoft AD",
    "onprem_ad": "On-premises Active Directory",
    "ldap": "On-premises LDAP",
}
ONPREM_PROVIDERS = ("onprem_ad", "ldap")
_MANAGED_REF_PREFIX = "psmanaged:"
_AWS_TYPES = {"MicrosoftAD": "aws_managed_ad", "ADConnector": "aws_ad_connector",
              "SimpleAD": "aws_simple_ad"}
AWS_EDITIONS = ("Standard", "Enterprise")

# Shown on the build form so nobody builds one by accident. Approximate list prices for
# the two domain controllers each option runs; the console is authoritative.
APPROX_MONTHLY_COST = {
    ("aws", "Standard"): "about $150–200/month",
    ("aws", "Enterprise"): "about $600/month",
    ("gcp", ""): "about $300/month per region",
}

# An AD Connector proxies to on-prem domain controllers instead of running its own.
# Approximate list prices; the AWS pricing page is authoritative.
CONNECTOR_SIZES = ("Small", "Large")
CONNECTOR_MONTHLY_COST = {"Small": "about $36/month", "Large": "about $110/month"}
_IPV4_RE = re.compile(r"^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$")

AWS_ADMIN_USER = "Admin"
GCP_ADMIN_USER = "setupadmin"

_FQDN_RE = re.compile(r"^(?=.{1,64}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_NETBIOS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,14}$")
_CIDR24_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.0/24$")


class DirectoryError(Exception):
    """A directory operation cannot proceed; the message says why and what to do."""


def _cfg(key: str, default: str = "") -> str:
    try:
        from . import config_service
        val = config_service.get(key)
        if val not in (None, ""):
            return str(val)
    except Exception:  # noqa: BLE001
        pass
    from ..config import settings
    val = getattr(settings, key, None)
    return default if val in (None, "") else str(val)


def template_dir(cloud: str) -> str:
    path = _TEMPLATE_DIRS.get((cloud or "").lower())
    if not path:
        raise DirectoryError(f"no managed-directory module for cloud {cloud!r} — "
                             f"built: {', '.join(PROVISIONING_CLOUDS)}.")
    return path


def _deploy_dir(job_id: str) -> str:
    return os.path.join(_DEPLOYMENTS_DIR, job_id)


def _jl(value) -> list:
    try:
        out = json.loads(value or "[]")
        return out if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


def to_dict(row: ManagedDirectory) -> dict:
    """What the API returns. Never a credential — only where it lives."""
    return {
        "id": row.id, "name": row.name, "netbios": row.netbios, "cloud": row.cloud,
        "provider": row.provider, "provider_label": PROVIDER_LABELS.get(row.provider, row.provider),
        "source": row.source, "status": row.status, "edition": row.edition,
        "region": row.region, "locations": _jl(row.locations), "project": row.project,
        "directory_id": row.directory_id, "resource_name": row.resource_name,
        "vpc_id": row.vpc_id, "subnet_ids": _jl(row.subnet_ids),
        "networks": _jl(row.networks), "dns_ips": _jl(row.dns_ips),
        "admin_username": row.admin_username,
        "admin_password_backend": row.admin_password_backend,
        "admin_password_custody": row.admin_password_custody,
        "ps_system_id": row.ps_system_id, "ps_account_id": row.ps_account_id,
        "ps_error": row.ps_error, "error_message": row.error_message,
        "workgroup": row.workgroup, "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "deploy_job_id": row.deploy_job_id,
        "agent_id": row.agent_id, "host": row.host, "port": row.port,
        "use_ldaps": bool(row.use_ldaps), "base_dn": row.base_dn,
        # The account NAME only, so the page can say who the playbooks bind as.
        "bind_account": (_managed_ref_or_none(row) or {}).get("account_name"),
        "linked_directory_id": row.linked_directory_id,
    }


def list_directories(db: Session, workgroup: Optional[str] = None) -> list:
    q = db.query(ManagedDirectory).filter(ManagedDirectory.status != "deleted")
    if workgroup:
        q = q.filter(ManagedDirectory.workgroup == workgroup)
    return q.order_by(ManagedDirectory.created_at.desc()).all()


def get_directory(db: Session, directory_id: str) -> Optional[ManagedDirectory]:
    return db.query(ManagedDirectory).filter(ManagedDirectory.id == directory_id).first()


def generate_admin_password(length: int = 24) -> str:
    """Meets AWS Managed AD's rules (8–64 chars, 3 of 4 classes, no 'admin') and GCP's
    (which sets its own, but this is also used for the AWS create-time throwaway)."""
    symbols = "!#%^*-_=+"
    alphabet = string.ascii_letters + string.digits + symbols
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in symbols for c in pw)
                and "admin" not in pw.lower()):
            return pw


# ── which VMs are joined ──────────────────────────────────────────────────────

_DEPLOY_JOB_TYPES = ("ec2_deploy", "gce_deploy")


def joined_vms(db: Session, directory_id: str) -> list:
    """Names of live dashboard VMs recorded as joined to this directory."""
    out = []
    for job in db.query(Job).filter(Job.job_type.in_(_DEPLOY_JOB_TYPES)).all():
        meta = job.metadata_dict
        if meta.get("destroyed") or meta.get("ad_directory_id") != directory_id:
            continue
        if not meta.get("ad_joined"):
            continue
        out.append(meta.get("instance_name") or meta.get("vm_name")
                   or meta.get("instance_id") or job.id)
    return out


def joinable_for(db: Session, cloud: str, region: str = "") -> list:
    """Directories a Windows deploy on ``cloud`` (in ``region``, for AWS) can join."""
    rows = [r for r in list_directories(db) if r.cloud == cloud and r.status == "available"]
    if cloud == "aws" and region:
        rows = [r for r in rows if (r.region or "") == region]
    return rows


# ── provisioning ──────────────────────────────────────────────────────────────

def provision(db: Session, *, cloud: str, name: str, created_by: str,
              acknowledge_cost: bool = False, netbios: str = "",
              edition: str = "", region: str = "", vpc_id: str = "",
              subnet_ids: Optional[list] = None, project: str = "",
              locations: Optional[list] = None, reserved_ip_range: str = "",
              networks: Optional[list] = None, register_in_passwordsafe: bool = False,
              workgroup: Optional[str] = None) -> dict:
    """Validate, record the directory and enqueue its build. Returns ``{directory_id, job_id}``.

    Everything that can be refused is refused here, before anything bills: an unknown
    cloud, a bad name, no acceptable place for the admin password, and an unacknowledged
    cost — this resource has no timer, so nothing will take it down by itself."""
    from . import windows_admin_secret
    cloud = (cloud or "").lower()
    template_dir(cloud)
    name = (name or "").strip().lower().rstrip(".")
    if not _FQDN_RE.match(name):
        raise DirectoryError(
            f"{name!r} is not a usable domain name — use a fully qualified name with at "
            f"least two labels, e.g. corp.example.com (64 characters at most).")
    netbios = (netbios or "").strip().upper()
    if netbios and not _NETBIOS_RE.match(netbios):
        raise DirectoryError(f"NetBIOS name {netbios!r} must be 1–15 letters, digits or "
                             f"hyphens.")
    if not acknowledge_cost:
        raise DirectoryError(
            "A managed directory runs two domain controllers around the clock "
            f"({APPROX_MONTHLY_COST.get((cloud, edition or ('Standard' if cloud == 'aws' else '')), 'a standing monthly cost')}) "
            "and has no auto-delete timer. Confirm the cost to build it.")
    try:
        windows_admin_secret.resolve_backend(cloud)
    except windows_admin_secret.WindowsSecretError as e:
        raise DirectoryError(str(e)) from e

    fields: dict = {}
    if cloud == "aws":
        edition = edition or _cfg("directory_aws_default_edition", "Standard")
        if edition not in AWS_EDITIONS:
            raise DirectoryError(f"edition must be one of {', '.join(AWS_EDITIONS)}")
        region = region or _cfg("aws_region", "us-east-2")
        subnet_ids = [s.strip() for s in (subnet_ids or []) if s and s.strip()]
        if not vpc_id or len(subnet_ids) != 2:
            raise DirectoryError(
                "AWS Managed Microsoft AD needs a VPC and exactly two subnets in different "
                "Availability Zones — one domain controller goes in each.")
        fields = dict(edition=edition, region=region, vpc_id=vpc_id,
                      subnet_ids=json.dumps(subnet_ids), admin_username=AWS_ADMIN_USER)
    else:
        project = (project or "").strip() or _cfg("gcp_project") or _cfg("gcp_project_id")
        if not project:
            raise DirectoryError("a GCP project id is required (form or gcp_project).")
        locations = [x.strip() for x in (locations or []) if x and x.strip()] \
            or [_cfg("gcp_region", "us-central1")]
        reserved_ip_range = (reserved_ip_range or "").strip() \
            or _cfg("directory_gcp_reserved_ip_range")
        if not _CIDR24_RE.match(reserved_ip_range or ""):
            raise DirectoryError(
                "GCP Managed AD needs an unused /24 for its domain controllers (e.g. "
                "10.250.0.0/24) that overlaps nothing in the authorized networks.")
        networks = [x.strip() for x in (networks or []) if x and x.strip()]
        if not networks and _cfg("gcp_network"):
            networks = [_cfg("gcp_network")]
        if not networks:
            raise DirectoryError(
                "GCP Managed AD needs at least one authorized VPC network — the network "
                "your Windows servers are on.")
        networks = [_qualify_network(n, project) for n in networks]
        fields = dict(project=project, locations=json.dumps(locations),
                      reserved_ip_range=reserved_ip_range, networks=json.dumps(networks),
                      admin_username=GCP_ADMIN_USER, edition="")

    row = ManagedDirectory(
        name=name, netbios=netbios or None, cloud=cloud,
        provider="aws_managed_ad" if cloud == "aws" else "gcp_managed_ad",
        source="provisioned", status="provisioning", workgroup=workgroup,
        created_by=created_by, expires_at=None, **fields)
    db.add(row)
    db.flush()
    job = job_service.create_job(
        db, PROVISION_JOB_TYPE, created_by, workgroup=workgroup,
        metadata={"directory_id": row.id, "name": name, "cloud": cloud,
                  "register_in_passwordsafe": bool(register_in_passwordsafe)})
    row.deploy_job_id = job.id
    db.commit()
    logger.info("directory: queued %s %r as job %s", cloud, name, job.id)
    return {"directory_id": row.id, "job_id": job.id}


# ── AWS AD Connector: an on-prem domain extended to AWS ───────────────────────

def provision_ad_connector(db: Session, *, onprem_directory_id: str, region: str,
                           vpc_id: str, subnet_ids: list, dns_ips: list,
                           created_by: str, size: str = "Small", netbios: str = "",
                           connector_account: Optional[dict] = None,
                           acknowledge_cost: bool = False,
                           workgroup: Optional[str] = None) -> dict:
    """Validate, record and enqueue an AWS AD Connector for a registered on-prem AD.

    The connector is created through the Directory Service API rather than Terraform,
    because ConnectDirectory takes the service account's password and Terraform would
    keep it in state. The password is checked out of Password Safe in the worker, used
    once, and never stored.

    AWS has no API to change that password afterwards (botocore's ds model offers
    ConnectDirectory but no credential update), so the account's automatic rotation in
    Password Safe must be off; the docs and the build form say so."""
    onprem = get_directory(db, onprem_directory_id)
    if not onprem or onprem.cloud != "local" or onprem.provider != "onprem_ad":
        raise DirectoryError("an AD Connector extends a registered on-premises Active "
                             "Directory — register the domain on this page first")
    if onprem.status != "available":
        raise DirectoryError(f"{onprem.name} is {onprem.status}, not available")
    size = (size or "Small").strip().capitalize()
    if size not in CONNECTOR_SIZES:
        raise DirectoryError(f"size must be one of {', '.join(CONNECTOR_SIZES)}")
    if not acknowledge_cost:
        raise DirectoryError(
            f"An AD Connector bills around the clock ({CONNECTOR_MONTHLY_COST[size]}, "
            f"plus the VPN or Direct Connect it needs) and has no auto-delete timer. "
            f"Confirm the cost to build it.")
    region = (region or "").strip() or _cfg("aws_region", "us-east-2")
    subnet_ids = [x.strip() for x in (subnet_ids or []) if x and x.strip()]
    if not vpc_id or len(subnet_ids) != 2:
        raise DirectoryError("an AD Connector needs a VPC and exactly two subnets in "
                             "different Availability Zones")
    dns_ips = [x.strip() for x in (dns_ips or []) if x and x.strip()]
    bad = [x for x in dns_ips if not _IPV4_RE.match(x)]
    if not dns_ips or bad:
        raise DirectoryError(
            "list the IPv4 addresses of on-prem DNS servers (normally your domain "
            "controllers) reachable from the VPC over the VPN"
            + (f" — not addresses: {', '.join(bad)}" if bad else ""))
    netbios = (netbios or "").strip().upper()
    if netbios and not _NETBIOS_RE.match(netbios):
        raise DirectoryError(f"NetBIOS name {netbios!r} must be 1–15 letters, digits or "
                             f"hyphens.")
    ref = connector_account or _managed_ref_or_none(onprem) or {}
    if not (ref.get("system_id") and ref.get("account_id")):
        raise DirectoryError("the connector's service account must be a Password Safe "
                             "managed account")
    clash = db.query(ManagedDirectory).filter(
        ManagedDirectory.linked_directory_id == onprem.id,
        ManagedDirectory.provider == "aws_ad_connector",
        ManagedDirectory.region == region,
        ManagedDirectory.status != "deleted").first()
    if clash:
        raise DirectoryError(f"{onprem.name} already has an AD Connector in {region}")

    row = ManagedDirectory(
        name=onprem.name, netbios=netbios or None, cloud="aws",
        provider="aws_ad_connector", source="provisioned", status="provisioning",
        edition=size, region=region, vpc_id=vpc_id, subnet_ids=json.dumps(subnet_ids),
        dns_ips=json.dumps(dns_ips), linked_directory_id=onprem.id,
        credentials_ref=_MANAGED_REF_PREFIX + json.dumps({
            "system_id": ref["system_id"], "account_id": ref["account_id"],
            "account_name": ref.get("account_name") or "",
        }, sort_keys=True),
        workgroup=workgroup, created_by=created_by, expires_at=None)
    db.add(row)
    db.flush()
    job = job_service.create_job(
        db, PROVISION_JOB_TYPE, created_by, workgroup=workgroup,
        metadata={"directory_id": row.id, "name": row.name, "cloud": "aws",
                  "kind": "ad_connector", "linked_directory_id": onprem.id})
    row.deploy_job_id = job.id
    db.commit()
    logger.info("directory: queued AD Connector for %s in %s as job %s",
                onprem.name, region, job.id)
    return {"directory_id": row.id, "job_id": job.id}


def connector_username(account_name: str) -> str:
    """The bare sAMAccountName ConnectDirectory wants, from DOMAIN\\user or a UPN."""
    name = (account_name or "").strip()
    name = name.split("\\")[-1]
    return name.split("@")[0]


def _aws_connect_sync(row: ManagedDirectory, password: str, username: str) -> str:
    import boto3
    from . import aws_service
    ds = boto3.client("ds", **aws_service._aws_kwargs(row.region))
    kw = dict(
        Name=row.name, Password=password, Size=row.edition or "Small",
        Description=f"AD Connector for on-prem {row.name}"[:128],
        ConnectSettings={"VpcId": row.vpc_id, "SubnetIds": _jl(row.subnet_ids),
                         "CustomerDnsIps": _jl(row.dns_ips),
                         "CustomerUserName": username},
        Tags=[{"Key": "vm-dashboard-directory", "Value": row.id}])
    if row.netbios:
        kw["ShortName"] = row.netbios
    return ds.connect_directory(**kw)["DirectoryId"]


def _aws_wait_active_sync(region: str, directory_id: str, *, timeout: int = 1800,
                          interval: int = 20, sleep=None) -> dict:
    """Poll until the connector is Active, or raise with AWS's own reason."""
    import time
    import boto3
    from . import aws_service
    sleep = sleep or time.sleep
    ds = boto3.client("ds", **aws_service._aws_kwargs(region))
    waited = 0
    while True:
        desc = (ds.describe_directories(DirectoryIds=[directory_id])
                .get("DirectoryDescriptions") or [{}])[0]
        stage = desc.get("Stage")
        if stage == "Active":
            return desc
        if stage in ("Failed", "Impaired", "Inoperable", "Deleted"):
            raise DirectoryError(
                f"AWS reports the connector {stage}: {desc.get('StageReason') or 'no reason given'}"
                " — check that the VPC reaches the DNS addresses over the VPN on 53, 88 and "
                "389, and that the service account's password is current")
        if waited >= timeout:
            raise DirectoryError(f"the connector was still {stage} after {timeout // 60} "
                                 f"minutes")
        sleep(interval)
        waited += interval


def _aws_delete_sync(region: str, directory_id: str) -> None:
    import boto3
    from . import aws_service
    ds = boto3.client("ds", **aws_service._aws_kwargs(region))
    try:
        ds.delete_directory(DirectoryId=directory_id)
    except ds.exceptions.EntityDoesNotExistException:
        pass


async def _run_connector_apply(db: Session, row: ManagedDirectory, job_id: str) -> None:
    import asyncio
    from ..api.websocket import broadcast_progress
    from . import btapi_service
    job_service.set_running(db, job_id)
    try:
        ref = _managed_ref_or_none(row)
        if not ref:
            raise DirectoryError("the connector has no Password Safe service account recorded")
        await broadcast_progress(job_id, 5, "Checking out the connector service account…")
        duration = int(_cfg("ansible_managed_request_duration_min", "60") or 60)
        try:
            _req, password = await btapi_service.get_ps_credential_with_request(
                ref["system_id"], ref["account_id"], duration_min=duration)
        except btapi_service.BTAPIError as exc:
            raise DirectoryError(f"Password Safe checkout failed: {exc}") from exc
        if not password:
            raise DirectoryError("Password Safe returned an empty credential")
        await broadcast_progress(job_id, 15, "Creating the AD Connector…")
        try:
            row.directory_id = await asyncio.to_thread(
                _aws_connect_sync, row, password, connector_username(ref.get("account_name")))
        finally:
            password = ""
        db.commit()
        await broadcast_progress(job_id, 30, "Waiting for AWS to reach your domain "
                                             "controllers (5–20 minutes)…")
        desc = await asyncio.to_thread(_aws_wait_active_sync, row.region, row.directory_id)
        settings = desc.get("ConnectSettings") or {}
        row.dns_ips = json.dumps(desc.get("DnsIpAddrs") or settings.get("CustomerDnsIps")
                                 or _jl(row.dns_ips))
        row.security_group_id = settings.get("SecurityGroupId") or row.security_group_id
        row.status = "available"
        row.error_message = None
        row.updated_at = datetime.utcnow()
        db.commit()
        job_service.set_completed(db, job_id, result={
            "directory_id": row.id, "name": row.name, "aws_directory_id": row.directory_id})
    except Exception as exc:  # noqa: BLE001
        # A connector AWS created but could not activate still bills; directory_id stays
        # recorded so Destroy can delete it.
        row.status = "failed"
        row.error_message = str(exc)[:2000]
        row.updated_at = datetime.utcnow()
        db.commit()
        logger.error("directory: AD Connector failed for %s: %s", row.id, exc)
        job_service.set_failed(db, job_id, str(exc)[:2000])


def _qualify_network(network: str, project: str) -> str:
    n = network.strip()
    if n.startswith("projects/"):
        return n
    if "/" not in n:
        return f"projects/{project}/global/networks/{n}"
    m = re.search(r"(projects/[^/]+/global/networks/[^/]+)", n)
    return m.group(1) if m else n


def _tf_variables(row: ManagedDirectory, admin_password: str = "") -> dict:
    if row.cloud == "aws":
        return {
            "region": row.region, "domain_name": row.name, "short_name": row.netbios or "",
            "edition": row.edition or "Standard", "vpc_id": row.vpc_id,
            "subnet_ids": _jl(row.subnet_ids),
            # Required by the module even on destroy, where it is never used; on apply it
            # is a throwaway that the post-apply reset replaces (see the module header).
            "admin_password": admin_password or generate_admin_password(),
            "directory_row_id": row.id,
        }
    return {
        "project": row.project, "domain_name": row.name, "locations": _jl(row.locations),
        "reserved_ip_range": row.reserved_ip_range,
        "authorized_networks": _jl(row.networks), "admin": GCP_ADMIN_USER,
    }


def _read_outputs(row: ManagedDirectory, outputs: dict) -> None:
    def val(key):
        v = outputs.get(key)
        return v.get("value") if isinstance(v, dict) and "value" in v else v
    if row.cloud == "aws":
        row.directory_id = val("directory_id")
        row.dns_ips = json.dumps(list(val("dns_ip_addresses") or []))
        row.security_group_id = val("security_group_id")
    else:
        row.resource_name = val("resource_name")


# ── the administrator credential ──────────────────────────────────────────────

def _aws_reset_admin_sync(region: str, directory_id: str, password: str) -> None:
    import boto3
    from . import aws_service
    ds = boto3.client("ds", **aws_service._aws_kwargs(region))
    ds.reset_user_password(DirectoryId=directory_id, UserName=AWS_ADMIN_USER,
                           NewPassword=password)


def _gcp_reset_admin_sync(resource_name: str) -> str:
    from . import gcp_service
    session = gcp_service._authed_session()
    resp = session.post(
        f"https://managedidentities.googleapis.com/v1/{resource_name}:resetAdminPassword",
        json={})
    if resp.status_code != 200:
        raise DirectoryError(f"resetAdminPassword failed ({resp.status_code}): {resp.text[:400]}")
    return resp.json().get("password") or ""


async def set_admin_password(row: ManagedDirectory) -> str:
    """Set a fresh administrator password on a PROVISIONED directory and return it.

    AWS: we choose it and call ResetUserPassword (which is also what makes the create-time
    value in Terraform state dead). GCP: the API chooses it and returns it."""
    import asyncio
    if row.source != "provisioned":
        raise DirectoryError("only a directory built here has its administrator managed here")
    if row.cloud == "aws":
        pw = generate_admin_password()
        await asyncio.to_thread(_aws_reset_admin_sync, row.region, row.directory_id, pw)
        return pw
    pw = await asyncio.to_thread(_gcp_reset_admin_sync, row.resource_name)
    if not pw:
        raise DirectoryError("resetAdminPassword returned no password")
    return pw


async def _store_admin_password(db: Session, row: ManagedDirectory, password: str) -> None:
    import asyncio
    from . import windows_admin_secret
    old_backend, old_ref = row.admin_password_backend, row.admin_password_ref
    backend, ref = await asyncio.to_thread(
        windows_admin_secret.store, row.cloud, row.name, row.id[:8], password,
        prefix=windows_admin_secret.DIRECTORY_PREFIX)
    row.admin_password_backend, row.admin_password_ref = backend, ref
    row.admin_password_custody = "secret_manager"
    db.commit()
    if old_ref and (old_backend, old_ref) != (backend, ref):
        windows_admin_secret.delete(old_backend, old_ref)


async def _onboard_password_safe(db: Session, row: ManagedDirectory, job_id: str,
                                 password: str) -> None:
    """Onboard the directory administrator into Password Safe (Active Directory
    platform), seeded then rotated; on success retire the secret-manager copy."""
    from . import ps_vm_hook, windows_admin_secret
    result: dict = {}
    address = (_jl(row.dns_ips) or [row.name])[0]
    await ps_vm_hook.register_password_managed(
        db, job_id, name=row.name, host_name=row.name, address=address, port=636,
        username=row.admin_username, password=password, result=result,
        fa_name=_cfg("passwordsafe_directory_functional_account"),
        fa_missing=("no Password Safe functional account for directories — set "
                    "passwordsafe_directory_functional_account to one on an Active "
                    "Directory platform"),
        platform_tokens=("active", "directory"), platform_label="Active Directory",
        rotate_key="passwordsafe_directory_change_password_on_register",
        entity_type_id=int(_cfg("passwordsafe_directory_entity_type_id", "0") or "0"))
    row.ps_system_id = str(result.get("ps_managed_system_id") or "") or None
    row.ps_account_id = str(result.get("ps_managed_account_id") or "") or None
    row.ps_tf_state = result.get("ps_registration_tf_state")
    row.ps_error = result.get("ps_error") or result.get("ps_change_password_error")
    if ps_vm_hook.password_safe_holds_credential(result):
        row.admin_password_custody = "passwordsafe_managed"
        err = windows_admin_secret.delete(row.admin_password_backend, row.admin_password_ref)
        if not err:
            row.admin_password_backend = row.admin_password_ref = None
        else:
            row.ps_error = f"build-time copy not deleted: {err}"
    db.commit()


# ── worker entry points ───────────────────────────────────────────────────────

_BUILD_MILESTONES = {
    "aws": (("aws_directory_service_directory.ad: creating", 20,
             "Creating the domain controllers (20–45 min)…"),
            ("aws_directory_service_directory.ad: still creating", 40,
             "Creating the domain controllers (20–45 min)…")),
    "gcp": (("google_active_directory_domain.ad: creating", 20,
             "Creating the managed domain (up to an hour)…"),
            ("google_active_directory_domain.ad: still creating", 40,
             "Creating the managed domain (up to an hour)…")),
}
_TEARDOWN_MILESTONES = (("destroying", 40, "Destroying the directory…"),
                        ("destruction complete", 85, "Destroyed…"))


def _job_stream(job_id: str, start_pct: int, start_msg: str, cloud: str = ""):
    """Stream terraform output to the job's Live Output and advance a coarse bar."""
    from ..api.websocket import broadcast_progress
    state = {"pct": start_pct, "msg": start_msg}
    needles = _BUILD_MILESTONES.get(cloud, ()) + _TEARDOWN_MILESTONES

    async def on_line(line: str) -> None:
        job_service.cancel_check(job_id, state)
        low = line.lower()
        for needle, pct, msg in needles:
            if needle in low:
                state["pct"], state["msg"] = max(state["pct"], pct), msg
                break
        await broadcast_progress(job_id, state["pct"], state["msg"], log_line=line)

    return on_line


async def run_provision_apply(db: Session, *, directory_id: str, job_id: str) -> None:
    """Worker entry point for ``directory_provision``."""
    from ..api.websocket import broadcast_progress
    row = get_directory(db, directory_id)
    if not row:
        logger.warning("directory: row %s vanished before apply", directory_id)
        return
    if row.provider == "aws_ad_connector":
        return await _run_connector_apply(db, row, job_id)
    job = job_service.get_job(db, job_id)
    want_ps = bool((job.metadata_dict if job else {}).get("register_in_passwordsafe"))
    job_service.set_running(db, job_id)
    built = False
    try:
        await broadcast_progress(job_id, 5, "Creating the directory…")
        outputs = await terraform.apply(
            _deploy_dir(job_id), _tf_variables(row),
            template_dir=template_dir(row.cloud),
            env=terraform_provider_env.provider_env(row.cloud),
            on_line=_job_stream(job_id, 5, "Creating the directory…", row.cloud))
        built = True
        _read_outputs(row, outputs)
        row.status = "available"
        row.error_message = None
        row.updated_at = datetime.utcnow()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        row.status = "failed"
        row.error_message = str(exc)[:2000]
        row.updated_at = datetime.utcnow()
        db.commit()
        logger.error("directory: provision failed for %s: %s", directory_id, exc)
        job_service.set_failed(db, job_id, str(exc)[:2000])
        return

    # The directory exists from here on, so nothing below fails the build: a missing
    # credential is fixable with Reset admin password; a destroyed directory is not.
    notes = []
    password = ""
    try:
        await broadcast_progress(job_id, 85, "Setting the administrator password…")
        password = await set_admin_password(row)
        await _store_admin_password(db, row, password)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"administrator password not stored: {exc} — use Reset admin password")
        logger.warning("directory: admin credential for %s failed: %s", directory_id, exc)
    if password and want_ps:
        from . import ps_vm_hook
        if ps_vm_hook.registration_enabled():
            await _onboard_password_safe(db, row, job_id, password)
            if row.ps_error:
                notes.append(f"Password Safe: {row.ps_error}")
        else:
            notes.append("Password Safe registration is disabled globally "
                         "(passwordsafe_registration_enabled)")
    password = ""
    if notes:
        row.error_message = "; ".join(notes)[:2000]
    row.updated_at = datetime.utcnow()
    db.commit()
    job_service.set_completed(db, job_id, result={
        "directory_id": row.id, "name": row.name, "built": built,
        "admin_password_backend": row.admin_password_backend,
        "admin_password_custody": row.admin_password_custody,
        "warnings": notes})


def start_decommission(db: Session, *, directory_id: str, created_by: str) -> dict:
    """Enqueue teardown of a PROVISIONED directory. Refused while VMs are joined."""
    row = get_directory(db, directory_id)
    if not row:
        raise DirectoryError(f"directory {directory_id} not found")
    if row.source != "provisioned":
        raise DirectoryError(f"{row.name} was not built here — unregister it instead; "
                             f"this dashboard never destroys a directory it did not build.")
    if row.status == "decommissioning":
        raise DirectoryError(f"{row.name} is already being destroyed")
    joined = joined_vms(db, row.id)
    if joined:
        raise DirectoryError(
            f"{row.name} still has {len(joined)} joined server(s): "
            f"{', '.join(sorted(joined)[:10])}. Destroy them (or remove them from the "
            f"domain) first — deleting the directory breaks every server joined to it.")
    row.status = "decommissioning"
    row.expires_at = None
    row.updated_at = datetime.utcnow()
    job = job_service.create_job(
        db, DECOMMISSION_JOB_TYPE, created_by, workgroup=row.workgroup,
        metadata={"directory_id": row.id, "name": row.name, "cloud": row.cloud})
    db.commit()
    return {"directory_id": row.id, "job_id": job.id}


async def run_decommission(db: Session, *, directory_id: str, job_id: str) -> None:
    """Worker entry point for ``directory_decommission``: Password Safe, then the
    directory, then the stored administrator password."""
    from ..api.websocket import broadcast_progress
    from . import windows_admin_secret
    row = get_directory(db, directory_id)
    if not row:
        return
    job_service.set_running(db, job_id)
    try:
        if row.provider == "aws_ad_connector":
            # Created through the API, not Terraform, so it is deleted the same way.
            # Nothing else to clean up: the connector holds no stored credential here.
            import asyncio
            await broadcast_progress(job_id, 20, "Deleting the AD Connector…")
            if row.directory_id:
                await asyncio.to_thread(_aws_delete_sync, row.region, row.directory_id)
            row.status = "deleted"
            row.error_message = None
            row.updated_at = datetime.utcnow()
            db.commit()
            job_service.set_completed(db, job_id, result={"directory_id": row.id,
                                                          "name": row.name})
            return
        if row.ps_tf_state:
            await broadcast_progress(job_id, 10, "Removing the Password Safe objects…")
            try:
                from . import ps_resource_service
                await ps_resource_service.deregister(row.ps_tf_state)
                row.ps_tf_state = row.ps_system_id = row.ps_account_id = None
                db.commit()
            except Exception as exc:  # noqa: BLE001 — never strand a billing directory
                logger.warning("directory: Password Safe deregister failed for %s: %s",
                               directory_id, exc)
        await broadcast_progress(job_id, 20, "Destroying the directory…")
        await terraform.destroy(
            _deploy_dir(row.deploy_job_id or job_id), variables=_tf_variables(row),
            template_dir=template_dir(row.cloud),
            env=terraform_provider_env.provider_env(row.cloud),
            on_line=_job_stream(job_id, 20, "Destroying the directory…", row.cloud))
        err = windows_admin_secret.delete(row.admin_password_backend, row.admin_password_ref)
        row.admin_password_backend = row.admin_password_ref = None
        row.status = "deleted"
        row.error_message = f"stored admin password not deleted: {err}" if err else None
        row.updated_at = datetime.utcnow()
        db.commit()
        job_service.set_completed(db, job_id, result={"directory_id": row.id,
                                                      "name": row.name})
    except Exception as exc:  # noqa: BLE001
        row.status = "failed"
        row.error_message = str(exc)[:2000]
        row.updated_at = datetime.utcnow()
        db.commit()
        job_service.set_failed(db, job_id, str(exc)[:2000])


async def reset_admin_password(db: Session, *, directory_id: str) -> dict:
    """Set and store a fresh administrator password (e.g. when the post-build store
    failed). Refused when Password Safe manages the account — rotate it there."""
    row = get_directory(db, directory_id)
    if not row:
        raise DirectoryError(f"directory {directory_id} not found")
    if row.admin_password_custody == "passwordsafe_managed":
        raise DirectoryError("Password Safe manages this administrator account — rotate "
                             "it in Password Safe.")
    if row.provider == "aws_ad_connector":
        raise DirectoryError("an AD Connector has no administrator of its own — its "
                             "domain's administrators are on-premises")
    if row.status != "available":
        raise DirectoryError(f"{row.name} is {row.status}, not available")
    password = await set_admin_password(row)
    await _store_admin_password(db, row, password)
    return {"admin_password_backend": row.admin_password_backend}


# ── discovery and registration ────────────────────────────────────────────────

def _aws_describe_sync(region: str) -> list:
    import boto3
    from . import aws_service
    ds = boto3.client("ds", **aws_service._aws_kwargs(region))
    out, token = [], None
    while True:
        kw = {"NextToken": token} if token else {}
        resp = ds.describe_directories(**kw)
        out.extend(resp.get("DirectoryDescriptions") or [])
        token = resp.get("NextToken")
        if not token:
            return out


def _aws_candidate(d: dict, region: str) -> dict:
    vpc = d.get("VpcSettings") or (d.get("ConnectSettings") or {})
    return {
        "cloud": "aws", "region": region, "identifier": d.get("DirectoryId"),
        "name": (d.get("Name") or "").lower(), "netbios": d.get("ShortName"),
        "provider": _AWS_TYPES.get(d.get("Type"), "aws_managed_ad"),
        "edition": d.get("Edition") or "", "stage": d.get("Stage"),
        "vpc_id": vpc.get("VpcId"), "subnet_ids": vpc.get("SubnetIds") or [],
        "dns_ips": d.get("DnsIpAddrs") or (d.get("ConnectSettings") or {}).get("ConnectIps") or [],
        "security_group_id": vpc.get("SecurityGroupId"),
        "joinable": d.get("Stage") == "Active",
    }


def _gcp_list_sync(project: str) -> list:
    from . import gcp_service
    session = gcp_service._authed_session()
    url = (f"https://managedidentities.googleapis.com/v1/projects/{project}"
           f"/locations/global/domains")
    out, token = [], None
    while True:
        resp = session.get(url, params={"pageToken": token} if token else None)
        if resp.status_code != 200:
            raise DirectoryError(f"listing GCP managed domains failed "
                                 f"({resp.status_code}): {resp.text[:400]}")
        body = resp.json()
        out.extend(body.get("domains") or [])
        token = body.get("nextPageToken")
        if not token:
            return out


def _gcp_candidate(d: dict, project: str) -> dict:
    return {
        "cloud": "gcp", "project": project, "identifier": d.get("name"),
        "name": (d.get("fqdn") or (d.get("name") or "").rsplit("/", 1)[-1]).lower(),
        "provider": "gcp_managed_ad", "stage": d.get("state"),
        "locations": d.get("locations") or [],
        "networks": d.get("authorizedNetworks") or [],
        "reserved_ip_range": d.get("reservedIpRange"),
        "admin_username": d.get("admin") or GCP_ADMIN_USER,
        "joinable": d.get("state") == "READY",
    }


async def discover(db: Session, *, cloud: str, region: str = "", project: str = "") -> list:
    """Directories that already exist in the cloud account, each flagged with whether
    it is already registered here. Read-only."""
    import asyncio
    cloud = (cloud or "").lower()
    if cloud == "aws":
        region = region or _cfg("aws_region", "us-east-2")
        raw = await asyncio.to_thread(_aws_describe_sync, region)
        found = [_aws_candidate(d, region) for d in raw]
        known = {r.directory_id for r in list_directories(db) if r.cloud == "aws"}
    elif cloud == "gcp":
        project = project or _cfg("gcp_project") or _cfg("gcp_project_id")
        if not project:
            raise DirectoryError("a GCP project id is required to discover domains")
        raw = await asyncio.to_thread(_gcp_list_sync, project)
        found = [_gcp_candidate(d, project) for d in raw]
        known = {r.resource_name for r in list_directories(db) if r.cloud == "gcp"}
    else:
        raise DirectoryError(f"discovery covers aws and gcp, not {cloud!r}")
    for c in found:
        c["registered"] = c["identifier"] in known
    return found


async def register(db: Session, *, cloud: str, identifier: str, created_by: str,
                   region: str = "", project: str = "",
                   workgroup: Optional[str] = None) -> ManagedDirectory:
    """Record an existing directory. Re-reads it from the cloud rather than trusting the
    client's copy, and refuses one that is not usable yet. Writes nothing to the cloud."""
    found = await discover(db, cloud=cloud, region=region, project=project)
    match = next((c for c in found if c["identifier"] == identifier), None)
    if not match:
        raise DirectoryError(f"no {cloud} directory {identifier!r} found"
                             + (f" in {region}" if region else ""))
    if match["registered"]:
        raise DirectoryError(f"{match['name']} is already registered")
    if not match["joinable"]:
        raise DirectoryError(f"{match['name']} is {match['stage']}, not ready for joins")
    row = ManagedDirectory(
        name=match["name"], netbios=match.get("netbios"), cloud=cloud,
        provider=match["provider"], source="registered", status="available",
        edition=match.get("edition") or None, region=match.get("region"),
        project=match.get("project"),
        locations=json.dumps(match.get("locations") or []) if cloud == "gcp" else None,
        directory_id=identifier if cloud == "aws" else None,
        resource_name=identifier if cloud == "gcp" else None,
        vpc_id=match.get("vpc_id"), subnet_ids=json.dumps(match.get("subnet_ids") or []),
        networks=json.dumps(match.get("networks") or []),
        reserved_ip_range=match.get("reserved_ip_range"),
        dns_ips=json.dumps(match.get("dns_ips") or []),
        security_group_id=match.get("security_group_id"),
        admin_username=match.get("admin_username"),
        workgroup=workgroup, created_by=created_by, expires_at=None)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ── on-prem directories (cloud="local", reached through a remote agent) ───────

def base_dn_for(domain: str) -> str:
    return ",".join(f"DC={p}" for p in (domain or "").strip(".").split(".") if p)


def domain_for(base_dn: str) -> str:
    parts = [p.split("=", 1)[1] for p in (base_dn or "").split(",")
             if p.strip().lower().startswith("dc=") and "=" in p]
    return ".".join(parts).lower()


def _managed_ref_or_none(row) -> Optional[dict]:
    raw = row.credentials_ref or ""
    if not raw.startswith(_MANAGED_REF_PREFIX):
        return None
    try:
        return json.loads(raw[len(_MANAGED_REF_PREFIX):])
    except ValueError:
        return None


def register_onprem(db: Session, *, name: str, provider: str, host: str, port: int = 0,
                    use_ldaps: bool = True, base_dn: str = "", agent_id: str,
                    managed_account: dict, created_by: str,
                    workgroup: Optional[str] = None) -> ManagedDirectory:
    """Record an on-prem AD or LDAP directory reached through a remote agent.

    The sibling of ``cloud_database_service.register_database`` with ``cloud='local'``:
    no cloud API, no Terraform, just an inventory row and the references needed to reach
    it. ``managed_account`` is a Password Safe system/account pair — the credential the
    playbooks bind with is checked out at run time (:func:`directory_connection_vars`)
    and never stored here."""
    from ..database import RemoteAgent
    from . import agent_service
    provider = (provider or "").strip().lower()
    if provider not in ONPREM_PROVIDERS:
        raise DirectoryError(f"provider must be one of {', '.join(ONPREM_PROVIDERS)}")
    name = (name or "").strip().lower().rstrip(".")
    base_dn = (base_dn or "").strip()
    if provider == "onprem_ad":
        if not _FQDN_RE.match(name):
            raise DirectoryError(f"{name!r} is not a usable AD domain name (e.g. corp.example.com)")
        base_dn = base_dn or base_dn_for(name)
    elif not (name and base_dn):
        raise DirectoryError("an LDAP directory needs a name and its base DN")
    host = (host or "").strip()
    if not host:
        raise DirectoryError("a host is required — a domain controller or LDAP server "
                             "the agent can reach")
    port = int(port or (636 if use_ldaps else 389))
    for key in ("system_id", "account_id"):
        if not (managed_account or {}).get(key):
            raise DirectoryError(
                "a Password Safe managed account is required: the dashboard checks the "
                "credential out at run time rather than storing one")
    agent = db.query(RemoteAgent).filter(RemoteAgent.id == (agent_id or ""),
                                         RemoteAgent.is_active.is_(True)).first()
    if not agent:
        raise DirectoryError("that remote agent is not registered")
    if not agent_service.supports_directory(agent):
        raise DirectoryError(agent_service.directory_upgrade_hint(agent))
    if db.query(ManagedDirectory).filter(ManagedDirectory.host == host,
                                         ManagedDirectory.port == port,
                                         ManagedDirectory.status != "deleted").first():
        raise DirectoryError(f"a directory at {host}:{port} is already registered")
    row = ManagedDirectory(
        name=name, cloud="local", provider=provider, source="registered",
        status="available", agent_id=agent.id, host=host, port=port,
        use_ldaps=bool(use_ldaps), base_dn=base_dn,
        credentials_ref=_MANAGED_REF_PREFIX + json.dumps({
            "system_id": managed_account["system_id"],
            "account_id": managed_account["account_id"],
            "account_name": managed_account.get("account_name") or "",
        }, sort_keys=True),
        workgroup=workgroup, created_by=created_by, expires_at=None)
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("directory: registered on-prem %s %s at %s:%s via agent %s",
                provider, name, host, port, agent.id)
    return row


def bind_identity(row: ManagedDirectory, account_name: str) -> str:
    """The name to bind as. AD accepts a UPN, so a bare account name on an AD row becomes
    ``user@domain``; a DOMAIN\\user or an existing UPN/DN is used as given. LDAP takes the
    account name as given (normally a full DN)."""
    name = (account_name or "").strip()
    if row.provider == "onprem_ad" and name and "@" not in name and "\\" not in name \
            and "=" not in name:
        return f"{name}@{row.name}"
    return name


async def directory_connection_vars(row: ManagedDirectory) -> dict:
    """``dir_*`` vars for a playbook run, credential checked out just-in-time.

    Same contract as ``cloud_database_service._registered_connection_vars``: check out
    against the pinned system/account, hand the value to the run inline, and let the
    request expire on its duration. Nothing is persisted."""
    from . import btapi_service
    ref = _managed_ref_or_none(row)
    if not ref:
        raise DirectoryError(f"directory {row.name} has no Password Safe managed account "
                             f"recorded — re-register it with one")
    duration = int(_cfg("ansible_managed_request_duration_min", "60") or 60)
    try:
        _req, credential = await btapi_service.get_ps_credential_with_request(
            ref["system_id"], ref["account_id"], duration_min=duration)
    except btapi_service.BTAPIError as exc:
        raise DirectoryError(f"Password Safe checkout failed for {row.name}: {exc}") from exc
    if not credential:
        raise DirectoryError(f"Password Safe returned an empty credential for {row.name}")
    return {
        "dir_provider": row.provider,
        "dir_domain": row.name if row.provider == "onprem_ad" else domain_for(row.base_dn),
        "dir_host": row.host or "",
        "dir_port": int(row.port or (636 if row.use_ldaps else 389)),
        "dir_use_ldaps": bool(row.use_ldaps),
        "dir_base_dn": row.base_dn or "",
        "dir_bind_dn": bind_identity(row, ref.get("account_name") or ""),
        "dir_bind_password": credential,
    }


def unregister(db: Session, *, directory_id: str) -> None:
    """Forget a REGISTERED directory. Never touches the cloud."""
    row = get_directory(db, directory_id)
    if not row:
        raise DirectoryError(f"directory {directory_id} not found")
    if row.source != "registered":
        raise DirectoryError(f"{row.name} was built here — destroy it instead")
    linked = db.query(ManagedDirectory).filter(
        ManagedDirectory.linked_directory_id == row.id,
        ManagedDirectory.status != "deleted").all()
    if linked:
        raise DirectoryError(
            f"{row.name} is extended to the cloud by "
            f"{', '.join(PROVIDER_LABELS.get(r.provider, r.provider) for r in linked)} — "
            f"destroy that first")
    joined = joined_vms(db, row.id)
    if joined:
        raise DirectoryError(f"{row.name} still has joined server(s): "
                             f"{', '.join(sorted(joined)[:10])}")
    db.delete(row)
    db.commit()
