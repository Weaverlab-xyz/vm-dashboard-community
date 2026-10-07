"""Microsoft Entra hybrid join for Windows servers on AWS and GCP.

A server that joins an on-premises AD domain which Entra Connect synchronises — with
hybrid join configured there — becomes **hybrid joined**: domain joined AND known to
Entra ID, with no domain controller in the cloud. The domain join itself is the one the
dashboard already does through an AWS AD Connector or a GCP DNS link
(``domain_join_service``); this module adds the two things hybrid join needs on top:

* **A declaration.** Only the operator knows whether Entra Connect syncs a domain with
  hybrid join set up, so an on-prem AD row carries it explicitly — ``entra_hybrid`` and
  ``hybrid_ou`` (an OU in Entra Connect's sync scope) in its ``options``. An AD Connector
  or DNS link inherits them from the on-prem row it extends (``linked_directory_id``).
  Nothing in Entra Connect, the SCP or the sync scope is configured from here.
* **Verification.** The device only appears in Entra after a sync cycle (about 30 minutes)
  and the server's own device-registration task. A follow-up job (``windows_hybrid_check``)
  polls Microsoft Graph for a device of that name with ``trustType`` ``ServerAd`` and
  records the outcome on the deploy job as ``entra_hybrid_state``: ``joined``, ``pending``
  (not there yet when it gave up), or ``unverifiable`` (the dashboard's identity cannot
  read devices — it needs ``Device.Read.All``). A missing device is a finding, never a
  failed deploy.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

MODE = "hybrid"
JOB_TYPE = "windows_hybrid_check"
POLL_SECONDS = 300
POLL_LIMIT = 18                 # 90 minutes: a sync cycle is ~30, registration retries
_GRAPH = "https://graph.microsoft.com"
_LINK_PROVIDERS = ("aws_ad_connector", "dns_link")
_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,15}$")

transport = None    # tests set an httpx.MockTransport


class HybridError(Exception):
    """A hybrid-join setting or check cannot proceed; the message says why."""


def _options(row) -> dict:
    try:
        out = json.loads(row.options or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def settings_for(db: Session, row) -> dict:
    """``{"entra_hybrid", "hybrid_ou", "domain"}`` for a directory a server can join:
    the on-prem AD row's own, or the one an AD Connector / DNS link extends."""
    from . import directory_service
    source = row
    if row is not None and row.provider in _LINK_PROVIDERS and row.linked_directory_id:
        source = directory_service.get_directory(db, row.linked_directory_id)
    if source is None or source.provider != "onprem_ad":
        return {"entra_hybrid": False, "hybrid_ou": "", "domain": getattr(row, "name", "")}
    opts = _options(source)
    return {"entra_hybrid": bool(opts.get("entra_hybrid")),
            "hybrid_ou": opts.get("hybrid_ou") or "", "domain": source.name}


def _ou_problem(ou: str, base_dn: str) -> str:
    ou = (ou or "").strip()
    if not ou:
        return ""
    if not ou.upper().startswith("OU="):
        return "the OU must be a distinguished name starting with OU=, e.g. OU=Servers,DC=corp,DC=example,DC=com"
    if base_dn and not ou.replace(" ", "").lower().endswith(base_dn.replace(" ", "").lower()):
        return f"the OU must be inside the domain ({base_dn})"
    return ""


def set_settings(db: Session, row, *, entra_hybrid: bool, hybrid_ou: str = ""):
    """Declare (or withdraw) that Entra Connect syncs this on-prem domain for hybrid join."""
    from datetime import datetime
    if row.provider != "onprem_ad":
        raise HybridError("hybrid join is declared on the on-premises Active Directory row; "
                          "AD Connectors and DNS links inherit it")
    problem = _ou_problem(hybrid_ou, row.base_dn or "")
    if problem:
        raise HybridError(problem)
    opts = _options(row)
    opts["entra_hybrid"] = bool(entra_hybrid)
    opts["hybrid_ou"] = (hybrid_ou or "").strip()
    row.options = json.dumps(opts, sort_keys=True)
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


def deploy_check(db: Session, *, is_windows: bool, ad_directory_id: str) -> tuple:
    """``(problem, ou_default)`` for a deploy asking for hybrid join. A problem is a
    warning: the domain join (if any) still happens; only the hybrid part is skipped."""
    from . import directory_service
    if not is_windows:
        return "Entra join applies to Windows images only; skipped", ""
    if not ad_directory_id:
        return ("hybrid join needs an on-prem Active Directory to join — pick its AD "
                "Connector or DNS link under Join Active Directory"), ""
    row = directory_service.get_directory(db, ad_directory_id)
    s = settings_for(db, row)
    if not s["entra_hybrid"]:
        return (f"{s['domain'] or 'that directory'} is not marked as synced to Entra ID for "
                f"hybrid join (Directories → Hybrid join on its on-prem row); the domain join "
                f"goes ahead, the hybrid check is skipped"), ""
    return "", s["hybrid_ou"]


def computer_name(cloud: str, *, instance_name: str = "", ssm_computer_name: str = "") -> str:
    """The Windows computer name — what Entra names the device. GCE sets it from the
    instance name (NetBIOS, 15 characters); EC2 reports it through Systems Manager."""
    if cloud == "aws":
        return (ssm_computer_name or "").split(".", 1)[0].upper()
    return (instance_name or "")[:15].upper()


def _ssm_computer_name_sync(region: str, instance_id: str) -> str:
    import boto3
    from . import aws_service
    ssm = boto3.client("ssm", **aws_service._aws_kwargs(region))
    out = ssm.describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])
    rows = out.get("InstanceInformationList") or []
    return (rows[0].get("ComputerName") or "") if rows else ""


async def graph_devices(name: str) -> tuple:
    """``(status, devices)`` for devices named ``name``, with the dashboard's identity."""
    import httpx
    from . import azure_service
    credential, _sub = await azure_service._ensure_creds()
    token = (await azure_service._to_thread(credential.get_token, f"{_GRAPH}/.default")).token
    kwargs = {"timeout": 30}
    if transport is not None:
        kwargs["transport"] = transport
    async with httpx.AsyncClient(**kwargs) as client:
        resp = await client.get(
            f"{_GRAPH}/v1.0/devices",
            params={"$filter": f"displayName eq '{name}'",
                    "$select": "id,deviceId,displayName,trustType,onPremisesSyncEnabled,"
                               "approximateLastSignInDateTime"},
            headers={"Authorization": f"Bearer {token}"})
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, list((body or {}).get("value") or [])


def queue_check(db: Session, *, deploy_job_id: str, cloud: str, instance_name: str,
                region: str = "", instance_id: str = "", created_by: str,
                workgroup: Optional[str] = None) -> str:
    from . import job_service
    job = job_service.create_job(db, JOB_TYPE, created_by or "system", workgroup=workgroup,
                                 metadata={"deploy_job_id": deploy_job_id, "cloud": cloud,
                                           "instance_name": instance_name, "region": region,
                                           "instance_id": instance_id})
    job_service.update_metadata(db, deploy_job_id, {"entra_join_mode": MODE,
                                                    "entra_hybrid_state": "pending",
                                                    "entra_hybrid_check_job_id": job.id})
    return job.id


async def run(db: Session, *, job_id: str, meta: dict) -> None:
    """Worker entry point for ``windows_hybrid_check``: wait for the device in Entra."""
    from . import job_service
    deploy_id = meta.get("deploy_job_id") or ""
    cloud = meta.get("cloud") or ""
    job_service.set_running(db, job_id)

    def record(state: str, **extra) -> None:
        job_service.update_metadata(db, deploy_id, {"entra_hybrid_state": state, **extra})

    name = ""
    for attempt in range(POLL_LIMIT):
        if not name:
            if cloud == "aws":
                try:
                    reported = await asyncio.to_thread(
                        _ssm_computer_name_sync, meta.get("region") or "",
                        meta.get("instance_id") or "")
                except Exception as exc:  # noqa: BLE001
                    logger.info("hybrid check %s: no computer name yet: %s", job_id, exc)
                    reported = ""
                name = computer_name("aws", ssm_computer_name=reported)
            else:
                name = computer_name("gcp", instance_name=meta.get("instance_name") or "")
            if name and not _NAME_RE.match(name):
                record("unverifiable", entra_hybrid_note=f"{name!r} is not a Windows computer name")
                return job_service.set_completed(db, job_id, {"state": "unverifiable"})
        if name:
            try:
                status, devices = await graph_devices(name)
            except Exception as exc:  # noqa: BLE001
                logger.info("hybrid check %s: Graph unreachable: %s", job_id, exc)
                status, devices = 0, []
            if status in (401, 403):
                note = ("the dashboard's Azure identity cannot read Entra devices — grant it "
                        "the Graph application permission Device.Read.All to verify hybrid join")
                record("unverifiable", entra_hybrid_note=note)
                return job_service.set_completed(db, job_id, {"state": "unverifiable",
                                                              "note": note})
            hybrid = [d for d in devices if (d.get("trustType") or "") == "ServerAd"]
            if hybrid:
                d = hybrid[0]
                record("joined", entra_device_id=d.get("deviceId") or d.get("id") or "",
                       entra_device_name=d.get("displayName") or name)
                return job_service.set_completed(db, job_id, {"state": "joined",
                                                              "device": d.get("displayName")})
        job_service.update_progress(
            db, job_id, min(95, 5 + attempt * 5),
            f"Waiting for {name or 'the server'} to appear in Entra ID (sync runs about every "
            f"30 minutes)…")
        await asyncio.sleep(POLL_SECONDS)
    note = (f"{name or 'the server'} did not appear in Entra ID as hybrid joined within "
            f"{POLL_LIMIT * POLL_SECONDS // 60} minutes — check that its OU is in Entra "
            f"Connect's sync scope, that hybrid join is configured (SCP), and that the server "
            f"reaches enterpriseregistration.windows.net")
    record("pending", entra_hybrid_note=note)
    job_service.set_completed(db, job_id, {"state": "pending", "note": note})
