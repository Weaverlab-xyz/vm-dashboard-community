"""Joining a Windows server to a managed Active Directory at deploy (AWS and GCP).

Neither cloud needs a domain credential from the dashboard:

* **AWS** — Systems Manager's ``AWS-JoinDirectoryServiceDomain`` document runs on the
  instance and joins it through Directory Service (seamless domain join). It works for
  Managed Microsoft AD, AD Connector and Simple AD. The instance's SSM instance profile
  needs ``AmazonSSMDirectoryServiceAccess`` alongside ``AmazonSSMManagedInstanceCore``,
  and the instance must reach the directory's DNS addresses (same or peered VPC). The
  join reboots the instance.
* **GCP** — instance metadata ``managed-ad-domain`` makes the guest agent join during
  first boot. The VM must run as a service account holding
  ``roles/managedidentities.domainJoin`` (``gcp_domain_join_service_account``) and sit in
  one of the domain's authorized networks.

A failed join is a WARNING on the deploy job (``ad_join_error``), never a failed deploy:
the local administrator still reaches the server. A server recorded as joined
(``ad_joined``) is what ``directory_service.start_decommission`` refuses on.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

AWS_JOIN_DOCUMENT = "AWS-JoinDirectoryServiceDomain"


class DomainJoinError(Exception):
    """The requested join cannot be attempted; the message says why."""


def _cfg(key: str) -> str:
    from . import directory_service
    return directory_service._cfg(key)


def resolve(db: Session, directory_id: Optional[str], cloud: str, region: str = ""):
    """The ManagedDirectory a deploy asked to join, checked for this cloud/region, or
    None when no join was asked for. Raises DomainJoinError when it cannot be used."""
    if not directory_id:
        return None
    from . import directory_service
    row = directory_service.get_directory(db, directory_id)
    if row is None or row.status == "deleted":
        raise DomainJoinError(f"directory {directory_id} does not exist")
    if row.cloud != cloud:
        raise DomainJoinError(f"{row.name} is a {row.cloud} directory; this is a {cloud} VM")
    if row.status != "available":
        raise DomainJoinError(f"{row.name} is {row.status}, not available")
    if cloud == "aws" and region and row.region and row.region != region:
        raise DomainJoinError(f"{row.name} is in {row.region}; this instance is in {region}")
    return row


def _ou(ou: str) -> str:
    return (ou or "").strip() or _cfg("directory_join_default_ou")


def aws_join_parameters(row, ou: str = "") -> dict:
    """The SSM document parameters (each value a list of strings, as SSM requires)."""
    import json
    params = {
        "directoryId": [row.directory_id],
        "directoryName": [row.name],
        "dnsIpAddresses": list(json.loads(row.dns_ips or "[]")),
    }
    ou = _ou(ou)
    if ou:
        params["directoryOU"] = [ou]
    return params


async def join_aws(db: Session, job_id: str, *, row, ou: str, region: str,
                   instance_id: str, result: dict) -> None:
    """Join an EC2 instance to ``row`` through SSM. Records the outcome on ``result``."""
    from . import aws_service, job_service
    result["ad_directory_id"] = row.id
    result["ad_domain"] = row.name
    params = aws_join_parameters(row, ou)
    if not params["dnsIpAddresses"]:
        result["ad_join_error"] = (f"{row.name} has no DNS addresses recorded, so the "
                                   "instance cannot find its domain controllers")
        return
    try:
        job_service.update_progress(db, job_id, 82,
                                    f"Waiting for Systems Manager before joining {row.name}…")
        await aws_service.wait_ssm_online(region, instance_id)
        job_service.update_progress(db, job_id, 84, f"Joining {row.name}…")
        res = await aws_service.ssm_send_document(
            region, instance_id, AWS_JOIN_DOCUMENT, params, timeout=900,
            comment=f"vm-dashboard join {row.name}")
    except Exception as e:  # noqa: BLE001
        result["ad_join_error"] = str(e)
        return
    if res.get("status") == "Success":
        result["ad_joined"] = True
        if params.get("directoryOU"):
            result["ad_ou"] = params["directoryOU"][0]
        job_service.update_progress(db, job_id, 88, f"Joined {row.name}; the instance is rebooting.")
        return
    detail = (res.get("stderr") or res.get("stdout") or "").strip()[:600]
    result["ad_join_error"] = (
        f"{AWS_JOIN_DOCUMENT} ended {res.get('status')}"
        + (f": {detail}" if detail else "")
        + ". Check that the instance profile has AmazonSSMDirectoryServiceAccess and that "
          "the instance's VPC can reach the directory's DNS addresses.")


def gcp_join_metadata(row, ou: str = "") -> dict:
    """Instance metadata that makes the GCE guest agent join ``row`` at first boot."""
    md = {"managed-ad-domain": row.resource_name,
          # A failed join must leave a reachable VM — the local admin still works.
          "managed-ad-domain-join-failure-stop": "false"}
    ou = _ou(ou)
    if ou:
        md["managed-ad-ou-name"] = ou
    return md


def gcp_join_service_account() -> str:
    sa = _cfg("gcp_domain_join_service_account")
    if not sa:
        raise DomainJoinError(
            "GCP domain join runs as the VM's service account, and none is configured — "
            "set gcp_domain_join_service_account (Settings → Managed Active Directory) to "
            "an account holding roles/managedidentities.domainJoin.")
    return sa


# ── Joining an ON-PREM domain from the on-prem agent (GCP "DNS link") ─────────
#
# GCP has no AD Connector. With a DNS link (a Cloud DNS forwarding zone) the VPC resolves
# the on-prem domain, and the join is run by the remote agent that already reaches that
# domain: a WinRM play against the new server, logging on as its local administrator
# and joining as the directory's Password Safe account. No domain credential goes into
# instance metadata.

# First-boot script that opens a WinRM HTTPS listener (5986) for that play. Not secret.
# LocalAccountTokenFilterPolicy lets a LOCAL administrator other than the built-in one
# act as administrator over WinRM; without it remote UAC filtering refuses the join.
WINRM_BOOTSTRAP_PS1 = r"""
$ErrorActionPreference = 'Stop'
Enable-PSRemoting -SkipNetworkProfileCheck -Force | Out-Null
$cert = New-SelfSignedCertificate -DnsName $env:COMPUTERNAME -CertStoreLocation Cert:\LocalMachine\My
if (-not (Get-ChildItem WSMan:\localhost\Listener | Where-Object { $_.Keys -contains 'Transport=HTTPS' })) {
  New-Item -Path WSMan:\localhost\Listener -Transport HTTPS -Address * -CertificateThumbPrint $cert.Thumbprint -Force | Out-Null
}
New-NetFirewallRule -DisplayName 'WinRM HTTPS (vm-dashboard AD join)' -Direction Inbound -Protocol TCP -LocalPort 5986 -Action Allow | Out-Null
Set-ItemProperty -Path HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System -Name LocalAccountTokenFilterPolicy -Value 1 -Type DWord
""".strip()

JOIN_PLAYBOOK = "builtin:ad-join-computer"
WINRM_HTTPS_PORT = 5986


def agent_join_metadata() -> dict:
    """Instance metadata for a server an agent will join: the WinRM bootstrap only."""
    return {"sysprep-specialize-script-ps1": WINRM_BOOTSTRAP_PS1}


def onprem_for_link(db: Session, link_row):
    """The on-prem directory a DNS link extends, checked for an agent join."""
    from . import agent_service, directory_service
    onprem = directory_service.get_directory(db, link_row.linked_directory_id or "")
    if not onprem or onprem.cloud != "local" or onprem.status != "available":
        raise DomainJoinError(f"the on-prem directory behind {link_row.name} is no longer "
                              f"registered")
    from ..database import RemoteAgent
    agent = db.query(RemoteAgent).filter(RemoteAgent.id == (onprem.agent_id or ""),
                                         RemoteAgent.is_active.is_(True)).first()
    if not agent:
        raise DomainJoinError(f"{onprem.name} has no active remote agent to run the join")
    if "agent_ansible" not in agent_service.allowed_job_types(agent):
        raise DomainJoinError(f"agent '{agent.name}' is not granted Config Management, "
                              f"which runs the join")
    if not agent_service.supports_directory(agent):
        raise DomainJoinError(agent_service.directory_upgrade_hint(agent))
    return onprem, agent


def queue_agent_join(db: Session, *, deploy_job_id: str, link_row, vm_name: str,
                     private_ip: str, ou: str, created_by: str,
                     workgroup: Optional[str] = None) -> str:
    """Queue the agent run that joins a deployed Windows server to the on-prem domain.
    Returns the job id. Call it after the deploy job has COMPLETED: the run's bundle
    reads the server's stored administrator password from that job."""
    from . import agent_ansible_meta, job_service
    onprem, agent = onprem_for_link(db, link_row)
    if not private_ip:
        raise DomainJoinError("the server has no private address for the agent to reach")
    ou = _ou(ou)
    meta = agent_ansible_meta.normalize({
        "description": f"Join {vm_name} to {onprem.name} (agent: {agent.name})",
        "run_kind": "directory", "transport": "winrm",
        "target_host": private_ip, "target_port": WINRM_HTTPS_PORT,
        "target_id": onprem.id, "target_label": f"{vm_name} → {onprem.name}",
        "asset": JOIN_PLAYBOOK, "asset_backend": "",
        "extra_vars": {"ad_join_ou": ou} if ou else {},
        "join_deploy_job_id": deploy_job_id,
    })
    problem = agent_ansible_meta.check(meta)
    if problem:
        raise DomainJoinError(problem)
    job = job_service.create_job(db, job_type="agent_ansible", created_by=created_by,
                                 workgroup=workgroup or "ansible", metadata=meta,
                                 agent_id=agent.id)
    return job.id
