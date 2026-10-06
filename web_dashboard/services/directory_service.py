"""
Managed Active Directory for Windows servers on AWS, GCP and Azure.

A Windows server gets its domain identity from Active Directory. All three clouds sell
a managed one:

* **AWS Directory Service** — Managed Microsoft AD (built here), plus AD Connector and
  Simple AD (discovered and registered, never built).
* **GCP Managed Service for Microsoft Active Directory** — built or discovered.
* **Microsoft Entra Domain Services** — built or discovered. Azure VMs can also join
  Entra ID directly (windows_server_hook.entra_join_azure); Entra DS is for servers that
  need Kerberos, LDAP or Group Policy. It has NO administrator of its own — admins are
  Entra users in "AAD DC Administrators" — so instead of setting a password after the
  build, an operator pins an existing such account (a Password Safe managed account) as
  the domain-join account, and the dashboard never creates or changes a user.

Two ways a directory reaches the dashboard, the CloudDatabase / K8sCluster split:

* ``provisioned`` — :func:`provision` + :func:`run_provision_apply` build it from
  ``terraform/directory/<module>``, recording state so :func:`run_decommission` destroys
  exactly what was built. The administrator credential is set fresh after the apply and
  stored through ``windows_admin_secret`` (Password Safe / an external manager, never the
  dashboard database), optionally onboarded into Password Safe and rotated.
* ``registered`` — :func:`discover` lists what already exists, :func:`register` records
  one. No credential is stored or needed: joining a server authenticates through the
  cloud on both sides (SSM seamless join, GCE metadata join). Deleting it only forgets it.

A third family has no domain controllers at all: **cloud identity providers** (Entra ID,
Okta, PingOne), ``cloud="saas"``. :func:`register_idp` records one, and the browse and
membership functions below hand the work to ``services/directory_idp``. They hold no
credential either, only a vault ref or a Password Safe pointer, and group-membership
writes stay off until an operator turns them on for that directory.

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
    "azure": os.path.join(_REPO_ROOT, "terraform", "directory", "azure_managed_ad"),
}
# Modules chosen by provider rather than cloud: a cloud-side extension of an on-prem
# directory, which runs no domain controllers of its own.
_PROVIDER_TEMPLATE_DIRS = {
    "dns_link": os.path.join(_REPO_ROOT, "terraform", "directory", "gcp_dns_forward"),
    "gcp_vyos_link": os.path.join(_REPO_ROOT, "terraform", "directory", "gcp_vyos_peer"),
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
    "azure_managed_ad": "Microsoft Entra Domain Services",
    "dns_link": "GCP DNS link to on-prem AD",
    "gcp_vyos_link": "GCP VyOS site link (WireGuard)",
    "onprem_ad": "On-premises Active Directory",
    "ldap": "On-premises LDAP",
    "entra_id": "Microsoft Entra ID",
    "okta": "Okta",
    "pingone": "PingOne",
}
ONPREM_PROVIDERS = ("onprem_ad", "ldap")
IDP_PROVIDERS = ("entra_id", "okta", "pingone")    # == tuple(directory_idp.PROVIDERS)
# LDAP servers a provider="ldap" row can name as its vendor.
LDAP_VENDORS = {
    "openldap": "OpenLDAP",
    "pingdirectory": "PingDirectory",
    "okta_ldap": "Okta LDAP Interface",
    "389ds": "389 Directory Server",
    "freeipa": "FreeIPA",
}
# rootDSE vendorName / Password Safe platform fragments → vendor. Checked in order.
_VENDOR_RULES = (
    ("pingdirectory", ("ping identity", "pingdirectory", "ping directory", "unboundid")),
    ("okta_ldap", ("okta",)),
    ("freeipa", ("freeipa", "red hat idm")),
    ("389ds", ("389", "fedora project", "red hat directory server")),
    ("openldap", ("openldap",)),
)
OKTA_LDAP_READONLY_PLAYBOOKS = ("ldap-search.yml",)
IDP_CLOUD = "saas"
_MANAGED_REF_PREFIX = "psmanaged:"
_AWS_TYPES = {"MicrosoftAD": "aws_managed_ad", "ADConnector": "aws_ad_connector",
              "SimpleAD": "aws_simple_ad"}
AWS_EDITIONS = ("Standard", "Enterprise")
AZURE_SKUS = ("Standard", "Enterprise", "Premium")

# Shown on the build form so nobody builds one by accident. Approximate list prices for
# the two domain controllers each option runs; the console is authoritative.
APPROX_MONTHLY_COST = {
    ("aws", "Standard"): "about $150–200/month",
    ("aws", "Enterprise"): "about $600/month",
    ("gcp", ""): "about $300/month per region",
    ("azure", "Standard"): "about $110/month",
    ("azure", "Enterprise"): "about $290/month",
    ("azure", "Premium"): "about $1,170/month",
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
_AZ_NAME_RE = re.compile(r"^[A-Za-z0-9._()-]{1,90}$")


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


def _options(row) -> dict:
    try:
        out = json.loads(row.options or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def template_dir(cloud: str, provider: str = "") -> str:
    path = (_PROVIDER_TEMPLATE_DIRS.get(provider or "")
            or _TEMPLATE_DIRS.get((cloud or "").lower()))
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
        "provider": row.provider, "provider_label": _provider_label(row),
        "vendor": row.vendor, "entitle_integration_id": row.entitle_integration_id,
        "entitle_integration_name": row.entitle_integration_name,
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
        # Cloud identity providers. The credential's KIND only, never the ref itself.
        "endpoint": row.endpoint, "tenant_id": row.tenant_id, "client_id": row.client_id,
        "auth_mode": row.auth_mode, "writes_enabled": bool(row.writes_enabled),
        "credential_kind": _credential_kind(row),
        "is_idp": row.provider in IDP_PROVIDERS,
        **({"site_link": _vyos().to_dict_extra(row)} if row.provider == "gcp_vyos_link" else {}),
    }


def _vyos():
    from . import vyos_link_service
    return vyos_link_service


def _provider_label(row) -> str:
    if row.provider == "ldap" and row.vendor in LDAP_VENDORS:
        return LDAP_VENDORS[row.vendor]
    return PROVIDER_LABELS.get(row.provider, row.provider)


def _credential_kind(row) -> str:
    if row.provider not in IDP_PROVIDERS:
        return ""
    from . import directory_idp
    return directory_idp.credential_kind(row.credentials_ref or "", row.auth_mode or "")


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

_DEPLOY_JOB_TYPES = ("ec2_deploy", "gce_deploy", "azure_deploy")


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
    # A site link is a network path, not something a server can join.
    rows = [r for r in list_directories(db) if r.cloud == cloud and r.status == "available"
            and r.provider != "gcp_vyos_link"]
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
              workgroup: Optional[str] = None, resource_group: str = "",
              vnet_resource_group: str = "", vnet_name: str = "", subnet_cidr: str = "",
              manage_vnet_dns: bool = False,
              managed_account: Optional[dict] = None) -> dict:
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
            f"({APPROX_MONTHLY_COST.get((cloud, edition or ('' if cloud == 'gcp' else 'Standard')), 'a standing monthly cost')}) "
            "and has no auto-delete timer. Confirm the cost to build it.")
    if cloud == "azure":
        # No administrator password to store: Entra DS has no administrator of its own.
        if register_in_passwordsafe:
            raise DirectoryError(
                "Entra Domain Services creates no administrator to onboard. Pin an existing "
                "AAD DC Administrators account from Password Safe as its join account.")
    else:
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
    elif cloud == "azure":
        fields = _azure_build_fields(
            edition=edition, region=region, resource_group=resource_group,
            vnet_resource_group=vnet_resource_group, vnet_name=vnet_name,
            subnet_cidr=subnet_cidr, manage_vnet_dns=manage_vnet_dns,
            managed_account=managed_account)
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
        provider=f"{cloud}_managed_ad",
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


def provision_dns_link(db: Session, *, onprem_directory_id: str, project: str,
                       networks: list, dns_ips: list, created_by: str,
                       workgroup: Optional[str] = None) -> dict:
    """Validate, record and enqueue a GCP DNS link for a registered on-prem AD.

    A Cloud DNS private forwarding zone, so the listed VPC networks resolve the on-prem
    domain and find its DCs over the VPN. Joining then needs no Managed AD: the
    on-prem agent runs the join over WinRM (domain_join_service.queue_agent_join)."""
    onprem = get_directory(db, onprem_directory_id)
    if not onprem or onprem.cloud != "local" or onprem.provider != "onprem_ad":
        raise DirectoryError("a DNS link extends a registered on-premises Active Directory "
                             "— register the domain on this page first")
    if onprem.status != "available":
        raise DirectoryError(f"{onprem.name} is {onprem.status}, not available")
    if not onprem.agent_id:
        raise DirectoryError(f"{onprem.name} has no remote agent, and the join runs "
                             f"through it — re-register it with one")
    project = (project or "").strip() or _cfg("gcp_project") or _cfg("gcp_project_id")
    if not project:
        raise DirectoryError("a GCP project id is required (form or gcp_project)")
    networks = [x.strip() for x in (networks or []) if x and x.strip()]
    if not networks and _cfg("gcp_network"):
        networks = [_cfg("gcp_network")]
    if not networks:
        raise DirectoryError("name at least one VPC network your Windows servers are on")
    networks = [_qualify_network(n, project) for n in networks]
    dns_ips = [x.strip() for x in (dns_ips or []) if x and x.strip()]
    bad = [x for x in dns_ips if not _IPV4_RE.match(x)]
    if not dns_ips or bad:
        raise DirectoryError(
            "list the IPv4 addresses of on-prem DNS servers (normally your domain "
            "controllers) reachable from the VPC over the VPN"
            + (f" — not addresses: {', '.join(bad)}" if bad else ""))
    clash = db.query(ManagedDirectory).filter(
        ManagedDirectory.linked_directory_id == onprem.id,
        ManagedDirectory.provider == "dns_link", ManagedDirectory.project == project,
        ManagedDirectory.status != "deleted").first()
    if clash:
        raise DirectoryError(f"{onprem.name} already has a DNS link in {project}")
    row = ManagedDirectory(
        name=onprem.name, cloud="gcp", provider="dns_link", source="provisioned",
        status="provisioning", project=project, networks=json.dumps(networks),
        dns_ips=json.dumps(dns_ips), linked_directory_id=onprem.id,
        workgroup=workgroup, created_by=created_by, expires_at=None)
    db.add(row)
    db.flush()
    job = job_service.create_job(
        db, PROVISION_JOB_TYPE, created_by, workgroup=workgroup,
        metadata={"directory_id": row.id, "name": row.name, "cloud": "gcp",
                  "kind": "dns_link", "linked_directory_id": onprem.id})
    row.deploy_job_id = job.id
    db.commit()
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
    if row.provider == "gcp_vyos_link":
        return _vyos().tf_variables(row)
    if row.provider == "dns_link":
        return {"project": row.project, "domain_name": row.name,
                "dns_ips": _jl(row.dns_ips), "networks": _jl(row.networks),
                "directory_row_id": row.id}
    if row.cloud == "azure":
        vnet_rg, _, vnet_name = (_jl(row.networks) or ["/"])[0].partition("/")
        return {
            "resource_group_name": row.project, "location": row.region,
            "domain_name": row.name, "sku": row.edition or "Standard",
            "vnet_resource_group": vnet_rg, "vnet_name": vnet_name,
            "subnet_cidr": row.reserved_ip_range,
            "manage_vnet_dns": bool(_options(row).get("manage_vnet_dns")),
            "directory_row_id": row.id,
        }
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
    if row.provider == "dns_link":
        row.resource_name = val("zone_name")
    elif row.cloud == "azure":
        row.resource_name = val("resource_name")
        row.dns_ips = json.dumps(list(val("dns_ip_addresses") or []))
    elif row.cloud == "aws":
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
    if row.cloud == "azure":
        raise DirectoryError(
            "Entra Domain Services has no administrator of its own: admins are Entra users "
            "in AAD DC Administrators. Pin one from Password Safe as the join account.")
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
    "azure": (("azurerm_active_directory_domain_service.ds: creating", 20,
               "Creating the managed domain (45–60 min)…"),
              ("azurerm_active_directory_domain_service.ds: still creating", 40,
               "Creating the managed domain (45–60 min)…")),
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
    if row.provider == "gcp_vyos_link":
        return await _vyos().run(db, row, job_id, check_only=bool(
            (job.metadata_dict if job else {}).get("check_only")))
    want_ps = bool((job.metadata_dict if job else {}).get("register_in_passwordsafe"))
    job_service.set_running(db, job_id)
    built = False
    try:
        await broadcast_progress(job_id, 5, "Creating the directory…")
        outputs = await terraform.apply(
            _deploy_dir(job_id), _tf_variables(row),
            template_dir=template_dir(row.cloud, row.provider),
            env=terraform_provider_env.provider_env(row.cloud),
            on_line=_job_stream(job_id, 5, "Creating the directory…", row.cloud))
        built = True
        _read_outputs(row, outputs)
        row.status = "available"
        row.error_message = None
        row.updated_at = datetime.utcnow()
        db.commit()
        if row.provider == "dns_link":
            # No domain controllers and no administrator here: the domain is on-prem.
            job_service.set_completed(db, job_id, result={
                "directory_id": row.id, "name": row.name, "built": True,
                "zone_name": row.resource_name})
            return
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
    if row.cloud == "azure":
        notes = azure_build_notes(row)
        row.error_message = "; ".join(notes)[:2000]
        row.updated_at = datetime.utcnow()
        db.commit()
        job_service.set_completed(db, job_id, result={
            "directory_id": row.id, "name": row.name, "built": True,
            "dns_ips": _jl(row.dns_ips), "warnings": notes})
        return
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
    if row.provider == "gcp_vyos_link":
        problem = _vyos().destroy_problem(db, row)
        if problem:
            raise DirectoryError(problem)
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
            template_dir=template_dir(row.cloud, row.provider),
            env=terraform_provider_env.provider_env(row.cloud),
            on_line=_job_stream(job_id, 20, "Destroying the directory…", row.cloud))
        err = windows_admin_secret.delete(row.admin_password_backend, row.admin_password_ref)
        cleanup = f"stored admin password not deleted: {err}" if err else None
        if row.provider == "gcp_vyos_link":
            # After the VM is gone, so a failed delete leaves a key to nothing.
            key_err = _vyos().delete_secret(row)
            if key_err:
                cleanup = f"WireGuard key not deleted from Secret Manager: {key_err}"
        row.admin_password_backend = row.admin_password_ref = None
        row.status = "deleted"
        row.error_message = cleanup
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
    if row.provider in ("aws_ad_connector", "dns_link", "gcp_vyos_link"):
        raise DirectoryError(f"a {PROVIDER_LABELS[row.provider]} has no administrator of "
                             f"its own — its domain's administrators are on-premises")
    if row.cloud == "azure":
        raise DirectoryError(
            "Entra Domain Services has no administrator password to reset: its admins are "
            "Entra users, so reset theirs in Entra, or rotate the pinned join account in "
            "Password Safe.")
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
    elif cloud == "azure":
        found = [_azure_candidate(d) for d in await _azure_list()]
        known = {(r.resource_name or "").lower() for r in list_directories(db)
                 if r.cloud == "azure"}
        for c in found:
            c["registered"] = c["identifier"].lower() in known
        return found
    else:
        raise DirectoryError(f"discovery covers aws, gcp and azure, not {cloud!r}")
    for c in found:
        c["registered"] = c["identifier"] in known
    return found


async def register(db: Session, *, cloud: str, identifier: str, created_by: str,
                   region: str = "", project: str = "",
                   workgroup: Optional[str] = None,
                   managed_account: Optional[dict] = None) -> ManagedDirectory:
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
    if cloud == "azure":
        return _register_azure(db, match, created_by=created_by, workgroup=workgroup,
                               managed_account=managed_account)
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


def _register_azure(db: Session, match: dict, *, created_by: str,
                    workgroup: Optional[str], managed_account: Optional[dict]):
    m = re.search(r"/resourceGroups/([^/]+)/", match["identifier"], re.IGNORECASE)
    row = ManagedDirectory(
        name=match["name"], cloud="azure", provider="azure_managed_ad",
        source="registered", status="available", edition=match.get("edition") or None,
        region=match.get("region"), project=m.group(1) if m else None,
        resource_name=match["identifier"], dns_ips=json.dumps(match.get("dns_ips") or []),
        subnet_ids=json.dumps(match.get("subnet_ids") or []),
        credentials_ref=_join_account_ref(managed_account) if managed_account else None,
        admin_username=(managed_account or {}).get("account_name") or None,
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


def okta_org(value: str) -> str:
    """The Okta org name in ``acme``, ``acme.okta.com`` or ``acme.ldap.okta.com``."""
    value = (value or "").strip().lower().rstrip(".")
    value = re.sub(r"^[a-z]+://", "", value).split("/", 1)[0]
    for suffix in (".ldap.okta.com", ".okta.com"):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
            break
    return value if re.match(r"^[a-z0-9][a-z0-9-]{0,62}$", value) else ""


def _normalize_onprem(*, name: str, provider: str, base_dn: str, host: str, port: int,
                      use_ldaps: bool, vendor: str = "") -> dict:
    provider = (provider or "").strip().lower()
    vendor = (vendor or "").strip().lower()
    name = (name or "").strip().lower().rstrip(".")
    base_dn = (base_dn or "").strip()
    host = (host or "").strip()
    if provider == "onprem_ad" and _FQDN_RE.match(name):
        base_dn = base_dn or base_dn_for(name)
    if provider == "ldap" and vendor == "okta_ldap":
        # Okta's LDAP Interface has one shape per org: LDAPS on 636 at
        # <org>.ldap.okta.com, base DN dc=<org>,dc=okta,dc=com. Fill what was left blank.
        org = okta_org(name) or okta_org(host)
        if org:
            host = host or f"{org}.ldap.okta.com"
            base_dn = base_dn or f"dc={org},dc=okta,dc=com"
            name = name or f"{org}.okta.com"
        use_ldaps = True
        port = port or 636
    port = int(port or (636 if use_ldaps else 389))
    return {"provider": provider, "name": name, "base_dn": base_dn, "host": host,
            "port": port, "use_ldaps": bool(use_ldaps), "vendor": vendor}


def onprem_problem(db: Session, *, name: str, provider: str, host: str, port: int = 0,
                   use_ldaps: bool = True, base_dn: str = "", agent_id: str,
                   managed_account: dict, vendor: str = "") -> str:
    """Why an on-prem directory cannot be registered, or "" when it can.

    Returns rather than raises so a batch caller (the Password Safe import) can report
    each reason without turning an exception into response text."""
    from ..database import RemoteAgent
    from . import agent_service
    n = _normalize_onprem(name=name, provider=provider, base_dn=base_dn, host=host,
                          port=port, use_ldaps=use_ldaps, vendor=vendor)
    provider, name, base_dn, host, port = (n["provider"], n["name"], n["base_dn"],
                                           n["host"], n["port"])
    if provider not in ONPREM_PROVIDERS:
        return f"provider must be one of {', '.join(ONPREM_PROVIDERS)}"
    if n["vendor"] and (provider != "ldap" or n["vendor"] not in LDAP_VENDORS):
        return (f"vendor must be one of {', '.join(LDAP_VENDORS)}, and only for an LDAP "
                f"directory")
    if n["vendor"] == "okta_ldap":
        if not (okta_org(name) or okta_org(host)):
            return "an Okta LDAP Interface needs the org name, e.g. acme or acme.okta.com"
        if port != 636 or not n["use_ldaps"]:
            return "Okta's LDAP Interface is LDAPS on port 636 only"
    if provider == "onprem_ad" and not _FQDN_RE.match(name):
        return f"{name!r} is not a usable AD domain name (e.g. corp.example.com)"
    if provider == "ldap" and not (name and base_dn):
        return "an LDAP directory needs a name and its base DN"
    if not host:
        return "a host is required — a domain controller or LDAP server the agent can reach"
    for key in ("system_id", "account_id"):
        if not (managed_account or {}).get(key):
            return ("a Password Safe managed account is required: the dashboard checks the "
                    "credential out at run time rather than storing one")
    agent = db.query(RemoteAgent).filter(RemoteAgent.id == (agent_id or ""),
                                         RemoteAgent.is_active.is_(True)).first()
    if not agent:
        return "that remote agent is not registered"
    if not agent_service.supports_directory(agent):
        return agent_service.directory_upgrade_hint(agent)
    if db.query(ManagedDirectory).filter(ManagedDirectory.host == host,
                                         ManagedDirectory.port == port,
                                         ManagedDirectory.status != "deleted").first():
        return f"a directory at {host}:{port} is already registered"
    return ""


def register_onprem(db: Session, *, name: str, provider: str, host: str, port: int = 0,
                    use_ldaps: bool = True, base_dn: str = "", agent_id: str,
                    managed_account: dict, created_by: str,
                    workgroup: Optional[str] = None, vendor: str = "") -> ManagedDirectory:
    """Record an on-prem AD or LDAP directory reached through a remote agent.

    The sibling of ``cloud_database_service.register_database`` with ``cloud='local'``:
    no cloud API, no Terraform, just an inventory row and the references needed to reach
    it. ``managed_account`` is a Password Safe system/account pair — the credential the
    playbooks bind with is checked out at run time (:func:`directory_connection_vars`)
    and never stored here. Every refusal is :func:`onprem_problem`'s."""
    problem = onprem_problem(db, name=name, provider=provider, host=host, port=port,
                             use_ldaps=use_ldaps, base_dn=base_dn, agent_id=agent_id,
                             managed_account=managed_account, vendor=vendor)
    if problem:
        raise DirectoryError(problem)
    n = _normalize_onprem(name=name, provider=provider, base_dn=base_dn, host=host,
                          port=port, use_ldaps=use_ldaps, vendor=vendor)
    row = ManagedDirectory(
        name=n["name"], cloud="local", provider=n["provider"], source="registered",
        status="available", agent_id=agent_id, host=n["host"], port=n["port"],
        use_ldaps=n["use_ldaps"], base_dn=n["base_dn"], vendor=n["vendor"] or None,
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
                row.provider, row.name, row.host, row.port, agent_id)
    return row


def vendor_for(vendor_name: str) -> str:
    """An LDAP vendor key from a rootDSE ``vendorName`` (or a Password Safe platform
    name), or "" when it is not one this dashboard treats differently."""
    text = (vendor_name or "").lower()
    for vendor, needles in _VENDOR_RULES:
        if any(n in text for n in needles):
            return vendor
    return ""


def run_refusal(row: ManagedDirectory, asset: str) -> str:
    """Why this playbook may not run against this directory, or "".

    Okta's LDAP Interface answers binds and searches only, so a write play would fail
    halfway through its first task. Refusing it up front is a usability guard rather
    than a security boundary: playbook filenames are operator-chosen, and Okta itself
    rejects the write."""
    if (row.vendor or "") == "okta_ldap":
        base = os.path.basename((asset or "").replace("\\", "/"))
        if base not in OKTA_LDAP_READONLY_PLAYBOOKS:
            return (f"Okta's LDAP Interface is search-only, so {base or 'that playbook'} "
                    f"cannot run against {row.name}. Use "
                    f"{' or '.join(OKTA_LDAP_READONLY_PLAYBOOKS)}, or change users and "
                    f"groups through the Okta identity provider instead.")
    return ""


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
    if row.provider in IDP_PROVIDERS:
        from . import directory_idp
        directory_idp.forget(row.id)
    db.delete(row)
    db.commit()


# ── Cloud identity providers (Entra ID, Okta, PingOne) ────────────────────────
#
# Registered, never built: the tenant already exists and the dashboard only reads it and,
# when an operator opts in per directory, changes group membership. The provider calls
# live in services/directory_idp; this section owns the row and the rules around it.

_IDP_REQUIRED = {
    "entra_id": ("tenant_id", "client_id"),
    "okta": ("endpoint",),
    "pingone": ("endpoint", "tenant_id", "client_id"),
}


def _idp_credentials_ref(credentials_ref: str, managed_account: Optional[dict]) -> str:
    """The pointer to store: a vault ref as given, or a Password Safe pin in the same
    ``psmanaged:`` shape the on-prem directories use."""
    if managed_account and managed_account.get("system_id") and managed_account.get("account_id"):
        return _MANAGED_REF_PREFIX + json.dumps({
            "system_id": managed_account["system_id"],
            "account_id": managed_account["account_id"],
            "account_name": managed_account.get("account_name") or "",
        }, sort_keys=True)
    return (credentials_ref or "").strip()


def _normalize_idp(provider: str, endpoint: str, tenant_id: str, client_id: str,
                   auth_mode: str) -> tuple:
    from .directory_idp import AUTH_MODES, okta
    provider = (provider or "").strip().lower()
    endpoint = (endpoint or "").strip()
    if provider == "okta":
        endpoint = okta.normalize_endpoint(endpoint) or endpoint
    elif provider == "pingone":
        endpoint = endpoint.lower().lstrip(".")
    tenant_id = (tenant_id or "").strip().lower()
    client_id = (client_id or "").strip()
    modes = AUTH_MODES.get(provider, ())
    auth_mode = (auth_mode or "").strip().lower() or (modes[0] if modes else "")
    return provider, endpoint, tenant_id, client_id, auth_mode


def _idp_duplicate(db: Session, provider: str, endpoint: str, tenant_id: str,
                   exclude_id: str = "") -> str:
    q = db.query(ManagedDirectory).filter(ManagedDirectory.provider == provider,
                                          ManagedDirectory.status != "deleted")
    if exclude_id:
        q = q.filter(ManagedDirectory.id != exclude_id)
    for other in q.all():
        if provider == "okta":
            same = (other.endpoint or "") == endpoint
        else:
            same = bool(tenant_id) and (other.tenant_id or "") == tenant_id and (
                provider != "pingone" or (other.endpoint or "") == endpoint)
        if same:
            what = "org" if provider == "okta" else "tenant"
            return (f"that {PROVIDER_LABELS[provider]} {what} is already registered as "
                    f"{other.name}")
    return ""


def idp_problem(db: Session, *, provider: str, endpoint: str = "", tenant_id: str = "",
                client_id: str = "", auth_mode: str = "", credentials_ref: str = "",
                managed_account: Optional[dict] = None, options: Optional[dict] = None,
                exclude_id: str = "") -> str:
    """Why this identity provider cannot be registered, or "" when it can. Returns rather
    than raises, like :func:`onprem_problem`, so a batch caller can report each reason."""
    from .directory_idp import (AUTH_MODES, NO_SECRET_MODES, OPTION_KEYS, entra, okta,
                                pingone, vault_prefixes)
    provider, endpoint, tenant_id, client_id, auth_mode = _normalize_idp(
        provider, endpoint, tenant_id, client_id, auth_mode)
    if provider not in IDP_PROVIDERS:
        return f"provider must be one of {', '.join(IDP_PROVIDERS)}"
    label = PROVIDER_LABELS[provider]
    if auth_mode not in AUTH_MODES[provider]:
        return f"{label} signs in with {' or '.join(AUTH_MODES[provider])}"
    required = list(_IDP_REQUIRED[provider])
    if provider == "entra_id" and auth_mode == "dashboard_azure":
        required = []          # both come from the dashboard's own Azure identity
    if provider == "okta" and auth_mode == "private_key_jwt":
        required.append("client_id")
    given = {"endpoint": endpoint, "tenant_id": tenant_id, "client_id": client_id}
    missing = [k for k in required if not given[k]]
    if missing:
        return f"{label} needs {', '.join(m.replace('_', ' ') for m in missing)}"
    if provider == "entra_id" and tenant_id and not entra.valid_tenant(tenant_id):
        return "the Entra tenant id must be a GUID (Entra admin center → Overview)"
    if provider == "okta" and not okta.normalize_endpoint(endpoint):
        return ("the Okta endpoint must be the org URL, https://<org>.okta.com (or "
                "oktapreview.com / okta-emea.com / okta-gov.com) — the management API is "
                "served there even with a custom sign-in domain")
    if provider == "pingone":
        if endpoint not in pingone.TLDS:
            return f"the PingOne region must be one of {', '.join(pingone.TLDS)}"
        if not pingone.valid_environment(tenant_id):
            return "the PingOne environment id must be a UUID"
    bad_opts = sorted(set(options or {}) - set(OPTION_KEYS[provider]))
    if bad_opts:
        return f"unknown {label} option(s): {', '.join(bad_opts)}"
    if auth_mode not in NO_SECRET_MODES:
        ref = _idp_credentials_ref(credentials_ref, managed_account)
        prefixes = " ".join(vault_prefixes())
        if not ref:
            return (f"a credential is required: a Password Safe managed account, or a "
                    f"vault reference ({prefixes}) — the dashboard never stores the "
                    f"secret itself")
        if not (ref.startswith(_MANAGED_REF_PREFIX) or ref.startswith(vault_prefixes())):
            return f"the credential must be a vault reference ({prefixes}), not the secret itself"
    return _idp_duplicate(db, provider, endpoint, tenant_id, exclude_id)


async def register_idp(db: Session, *, provider: str, name: str = "", endpoint: str = "",
                       tenant_id: str = "", client_id: str = "", auth_mode: str = "",
                       credentials_ref: str = "", managed_account: Optional[dict] = None,
                       options: Optional[dict] = None, writes_enabled: bool = False,
                       created_by: str, workgroup: Optional[str] = None) -> tuple:
    """Record a cloud identity provider after one successful sign-in and read.

    The test runs BEFORE the commit, so a wrong secret or a missing consent fails here,
    loudly, rather than leaving a row that errors on every browse. Returns
    ``(row, test_result)``."""
    import uuid
    from urllib.parse import urlsplit
    from . import directory_idp
    problem = idp_problem(db, provider=provider, endpoint=endpoint, tenant_id=tenant_id,
                          client_id=client_id, auth_mode=auth_mode,
                          credentials_ref=credentials_ref, managed_account=managed_account,
                          options=options)
    if problem:
        raise DirectoryError(problem)
    provider, endpoint, tenant_id, client_id, auth_mode = _normalize_idp(
        provider, endpoint, tenant_id, client_id, auth_mode)
    row = ManagedDirectory(
        id=str(uuid.uuid4()), name="", cloud=IDP_CLOUD, provider=provider,
        source="registered", status="available", endpoint=endpoint,
        tenant_id=tenant_id or None, client_id=client_id or None, auth_mode=auth_mode,
        credentials_ref=(None if auth_mode in directory_idp.NO_SECRET_MODES
                         else _idp_credentials_ref(credentials_ref, managed_account)),
        options=json.dumps(options or {}, sort_keys=True),
        writes_enabled=bool(writes_enabled),
        workgroup=workgroup, created_by=created_by, expires_at=None)
    try:
        module, conn, header = await directory_idp.authorize(row, fresh=True)
        if provider == "entra_id" and auth_mode == "dashboard_azure":
            # The tenant is whichever one the dashboard's identity lives in; read it off
            # the token rather than trusting a typed value.
            from .azure_service import _jwt_claims
            tid = str(_jwt_claims(header.split(" ", 1)[-1]).get("tid") or "").lower()
            if tenant_id and tid and tid != tenant_id:
                raise DirectoryError(
                    f"the dashboard's Azure identity signs in to tenant {tid}, not "
                    f"{tenant_id} — use a client secret from an app in that tenant")
            row.tenant_id = tid or tenant_id or None
            dup = _idp_duplicate(db, provider, "", row.tenant_id or "")
            if dup:
                raise DirectoryError(dup)
        result = await module.test(conn, header)
    except directory_idp.IdPError as exc:
        raise DirectoryError(str(exc)) from exc
    finally:
        directory_idp.forget(row.id)
    if not result.get("ok"):
        raise DirectoryError(result.get("detail") or "the connection test failed")
    default_name = ((urlsplit(endpoint).hostname or "") if provider == "okta"
                    else f"{PROVIDER_LABELS[provider]} {(row.tenant_id or '')[:8]}")
    row.name = ((name or "").strip() or default_name).strip()[:255]
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("directory: registered %s %s (%s)", provider, row.name, row.id)
    return row, result


def update_idp(db: Session, row: ManagedDirectory, *, name: Optional[str] = None,
               writes_enabled: Optional[bool] = None, credentials_ref: Optional[str] = None,
               managed_account: Optional[dict] = None) -> ManagedDirectory:
    """Rename, toggle membership writes, or repoint the credential. Repointing is
    validated by :func:`idp_problem` like a registration."""
    from . import directory_idp
    _require_idp(row)
    if credentials_ref is not None or managed_account:
        problem = idp_problem(db, provider=row.provider, endpoint=row.endpoint or "",
                              tenant_id=row.tenant_id or "", client_id=row.client_id or "",
                              auth_mode=row.auth_mode or "",
                              credentials_ref=credentials_ref or "",
                              managed_account=managed_account, exclude_id=row.id)
        if problem:
            raise DirectoryError(problem)
        if row.auth_mode not in directory_idp.NO_SECRET_MODES:
            row.credentials_ref = _idp_credentials_ref(credentials_ref or "", managed_account)
    if name is not None and name.strip():
        row.name = name.strip()[:255]
    if writes_enabled is not None:
        row.writes_enabled = bool(writes_enabled)
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    directory_idp.forget(row.id)
    return row


def _require_idp(row: ManagedDirectory) -> None:
    if row.provider not in IDP_PROVIDERS:
        raise DirectoryError(f"{row.name} is {PROVIDER_LABELS.get(row.provider, row.provider)},"
                             f" not a cloud identity provider")


async def idp_call(row: ManagedDirectory, fn: str, *args, **kwargs):
    """A call to the provider, its errors as :class:`DirectoryError`."""
    from . import directory_idp
    _require_idp(row)
    try:
        return await directory_idp.call(row, fn, *args, **kwargs)
    except directory_idp.IdPError as exc:
        raise DirectoryError(str(exc)) from exc


async def idp_test(row: ManagedDirectory) -> dict:
    """Sign in afresh and read one user and one group."""
    from . import directory_idp
    _require_idp(row)
    directory_idp.forget(row.id)
    return await idp_call(row, "test")


async def change_membership(row: ManagedDirectory, *, group_id: str, user_id: str,
                            action: str) -> dict:
    """Add a user to, or remove one from, a group. Refused unless the directory has
    writes enabled and the provider says the group's members are its to change."""
    if action not in ("add", "remove"):
        raise DirectoryError("action must be add or remove")
    _require_idp(row)
    if not row.writes_enabled:
        raise DirectoryError(f"group-membership writes are off for {row.name} — turn them "
                             f"on for this directory first")
    group = await idp_call(row, "get_group", group_id)
    if not group.get("editable"):
        raise DirectoryError(f"{group.get('name') or group_id} is {group['editable_reason']}")
    fn = "add_member" if action == "add" else "remove_member"
    out = await idp_call(row, fn, group["id"], user_id)
    return {"group_id": group["id"], "group_name": group.get("name") or "",
            "user_id": user_id, "action": action, "changed": bool(out.get("changed"))}


# ── Microsoft Entra Domain Services (Azure) ───────────────────────────────────
#
# Built from terraform/directory/azure_managed_ad or discovered through ARM. What makes it
# different from AWS and GCP is the credential: there is no administrator to create or
# reset. Admins are Entra users in the tenant's "AAD DC Administrators" group, so the
# dashboard pins one existing account (a Password Safe managed account) as the JOIN
# account, checks it out per join, and never creates a user or changes a group.

ENTRA_DS_APP_ID = "2565bd9d-da50-47d4-8b85-4c97f669dc36"
_AZURE_DS_API = "2021-05-01"
_ARM = "https://management.azure.com"
_GRAPH = "https://graph.microsoft.com"


def _join_account_ref(managed_account: dict) -> str:
    for key in ("system_id", "account_id"):
        if not (managed_account or {}).get(key):
            raise DirectoryError("the join account must be a Password Safe managed account "
                                 "(system and account)")
    return _MANAGED_REF_PREFIX + json.dumps({
        "system_id": managed_account["system_id"],
        "account_id": managed_account["account_id"],
        "account_name": managed_account.get("account_name") or "",
    }, sort_keys=True)


def _azure_build_fields(*, edition: str, region: str, resource_group: str,
                        vnet_resource_group: str, vnet_name: str, subnet_cidr: str,
                        manage_vnet_dns: bool, managed_account: Optional[dict]) -> dict:
    edition = edition or "Standard"
    if edition not in AZURE_SKUS:
        raise DirectoryError(f"SKU must be one of {', '.join(AZURE_SKUS)}")
    resource_group = (resource_group or "").strip() or _cfg("azure_resource_group")
    region = (region or "").strip() or _cfg("azure_location", "eastus")
    vnet_resource_group = ((vnet_resource_group or "").strip()
                           or _cfg("azure_vnet_resource_group") or resource_group)
    vnet_name = (vnet_name or "").strip() or _cfg("azure_vnet_name")
    subnet_cidr = (subnet_cidr or "").strip()
    if not resource_group:
        raise DirectoryError("a resource group is required (form or azure_resource_group)")
    if not vnet_name:
        raise DirectoryError(
            "Entra Domain Services needs the VNet your Windows servers are on (or one "
            "peered with it); it adds a dedicated subnet there.")
    if not _CIDR24_RE.match(subnet_cidr):
        raise DirectoryError(
            "Entra Domain Services needs an unused /24 in that VNet for its dedicated "
            "subnet, e.g. 10.0.250.0/24.")
    for value in (resource_group, vnet_resource_group, vnet_name):
        if not _AZ_NAME_RE.match(value):
            raise DirectoryError(f"{value!r} is not a valid Azure resource group or VNet name")
    return dict(
        edition=edition, region=region, project=resource_group,
        networks=json.dumps([f"{vnet_resource_group}/{vnet_name}"]),
        reserved_ip_range=subnet_cidr,
        credentials_ref=_join_account_ref(managed_account) if managed_account else None,
        admin_username=(managed_account or {}).get("account_name") or None,
        options=json.dumps({"manage_vnet_dns": bool(manage_vnet_dns)}))


def azure_build_notes(row: ManagedDirectory) -> list:
    """What an operator still has to do once a managed domain exists."""
    notes = []
    if not row.credentials_ref:
        notes.append("no domain-join account is pinned yet — pick an AAD DC Administrators "
                     "account from Password Safe before joining servers")
    if not _options(row).get("manage_vnet_dns"):
        notes.append("the VNet's DNS was left alone — point it (or a forwarder) at "
                     + (", ".join(_jl(row.dns_ips)) or "the domain controllers")
                     + " before joining servers")
    notes.append("a cloud-only Entra user must change their password once before they can "
                 "sign in to the managed domain")
    return notes


async def _arm_get(path: str, params: Optional[dict] = None) -> tuple:
    """``(status, body)`` for a GET on ARM with the dashboard's Azure identity."""
    import httpx
    from . import azure_service
    credential, _sub = await azure_service._ensure_creds()
    token = await azure_service._arm_token(credential)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{_ARM}{path}", params=params or {},
                                headers={"Authorization": f"Bearer {token}"})
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body


async def _graph_get(path: str) -> int:
    """The HTTP status of a Graph GET with the dashboard's Azure identity."""
    import httpx
    from . import azure_service
    credential, _sub = await azure_service._ensure_creds()
    token = (await azure_service._to_thread(credential.get_token,
                                            f"{_GRAPH}/.default")).token
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{_GRAPH}{path}",
                                headers={"Authorization": f"Bearer {token}"})
    return resp.status_code


async def _azure_subscription() -> str:
    from . import azure_service
    return await azure_service.subscription_id()


async def _azure_list() -> list:
    sub = await _azure_subscription()
    status, body = await _arm_get(
        f"/subscriptions/{sub}/providers/Microsoft.AAD/domainServices",
        {"api-version": _AZURE_DS_API})
    if status != 200:
        raise DirectoryError(f"listing Entra Domain Services failed (HTTP {status})")
    return list(body.get("value") or [])


def _azure_candidate(d: dict) -> dict:
    props = d.get("properties") or {}
    replicas = props.get("replicaSets") or []
    dns = [ip for rs in replicas for ip in (rs.get("domainControllerIpAddress") or [])]
    stage = props.get("provisioningState") or ""
    return {"cloud": "azure", "identifier": d.get("id") or "",
            "name": (props.get("domainName") or "").lower(),
            "provider": "azure_managed_ad", "stage": stage,
            "joinable": stage == "Succeeded" and bool(dns),
            "region": d.get("location") or "", "edition": props.get("sku") or "",
            "dns_ips": dns,
            "subnet_ids": [rs.get("subnetId") for rs in replicas if rs.get("subnetId")]}


async def azure_preflight(db: Session) -> None:
    """Refuse a build that would fail 45 minutes in, naming the fix. Nothing is changed:
    registering the resource provider or creating the service principal is the
    operator's call, so each refusal prints the command instead of running it."""
    sub = await _azure_subscription()
    status, body = await _arm_get(f"/subscriptions/{sub}/providers/Microsoft.AAD",
                                  {"api-version": "2021-04-01"})
    if status == 200 and (body.get("registrationState") or "") != "Registered":
        raise DirectoryError(
            "the Microsoft.AAD resource provider is not registered in this subscription — "
            "run: az provider register --namespace Microsoft.AAD")
    sp = await _graph_get(f"/v1.0/servicePrincipals(appId='{ENTRA_DS_APP_ID}')?$select=id")
    if sp == 404:
        raise DirectoryError(
            "the Domain Services service principal is missing from the tenant — a Global "
            f"Administrator runs: az ad sp create --id {ENTRA_DS_APP_ID}")
    if sp != 200:
        # 401/403: the dashboard's app has no Graph read permission, which is common and
        # not an answer. Terraform reports the real error if the principal is missing.
        logger.info("directory: could not check the Entra DS service principal (HTTP %s)", sp)
    existing = await _azure_list()
    if existing:
        c = _azure_candidate(existing[0])
        raise DirectoryError(
            f"this subscription already has Entra Domain Services ({c['name'] or c['identifier']}) "
            f"and a tenant may have only one — register it with Discover instead")


def set_join_account(db: Session, row: ManagedDirectory,
                     managed_account: Optional[dict]) -> ManagedDirectory:
    """Pin (or with None clear) the account Azure VMs join an Entra DS domain as."""
    if row.cloud != "azure":
        raise DirectoryError(f"{row.name} needs no join account — joins on "
                             f"{row.cloud} authenticate through the cloud")
    if managed_account:
        row.credentials_ref = _join_account_ref(managed_account)
        row.admin_username = managed_account.get("account_name") or None
    else:
        row.credentials_ref = None
        row.admin_username = None
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


def join_identity(row: ManagedDirectory, account_name: str) -> str:
    """Entra DS users sign in by their Entra UPN; a bare name gets the managed domain."""
    name = (account_name or "").strip()
    if name and "@" not in name and "\\" not in name:
        return f"{name}@{row.name}"
    return name


async def azure_join_credential(row: ManagedDirectory) -> tuple:
    """``(user, password)`` for one join, checked out of Password Safe just in time."""
    from . import btapi_service
    ref = _managed_ref_or_none(row)
    if not ref:
        raise DirectoryError(f"{row.name} has no domain-join account pinned — pick an AAD "
                             f"DC Administrators account on the Directories page")
    duration = int(_cfg("ansible_managed_request_duration_min", "60") or 60)
    try:
        _req, password = await btapi_service.get_ps_credential_with_request(
            ref["system_id"], ref["account_id"], duration_min=duration)
    except btapi_service.BTAPIError as exc:
        raise DirectoryError(f"Password Safe checkout failed for {row.name}'s join "
                             f"account") from exc
    if not password:
        raise DirectoryError(f"Password Safe returned an empty credential for {row.name}")
    return join_identity(row, ref.get("account_name") or ""), password
