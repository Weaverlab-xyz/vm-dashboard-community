"""Directory health: is each registered directory up and answering.

**Infrastructure only, on purpose.** This module answers "is the directory service
working" — the cloud control plane's own state for a managed domain, each domain
controller's status, whether an identity provider signs in and answers, whether an
on-prem DC answers LDAP through its agent, how fresh Entra Connect's last sync is. It
never reads accounts, groups, roles, MFA registration, credentials or privilege.

That line is the reason the module exists in this shape. Identity *posture* — stale and
privileged accounts, standing access, attack paths, risky sign-ins, expiring app
secrets — is BeyondTrust Identity Security Insights' data, and the dashboard will take it
from that API rather than collect a second, disagreeing copy. Before adding a check here,
ask whether it describes the *service* or the *identities in it*; only the first belongs.

How a check reaches the page:

* :func:`check_due` runs from ``jobs_worker`` (one process, so a provider is asked once
  per interval, not once per gunicorn worker) and from ``POST /api/directories/{id}/
  health/check``. Each check overwrites the directory's :class:`DirectoryHealth` row.
* The page reads only that table (:func:`snapshot`). No provider is called on a page load.
* An on-prem directory is probed by its remote agent with an ``agent_discover`` job
  scoped to the one host and port — the same anonymous rootDSE read the Discover dialog
  uses, so it needs no credential and no new agent build. The job finishes on the
  agent's schedule; :func:`apply_probe` folds its result in when it completes.
* An on-prem **AD** domain registered with a Password Safe account also gets the domain
  controller check, every few hours: ``builtin:ad-dc-health``, a read-only Config
  Management run over WinRM that reports replication, FSMO holders and each DC's
  services, clock, SYSVOL and LDAPS certificate. It prints its report as base64 chunks
  in the job's log; :func:`apply_deep` reads them back when the job completes.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ..database import DirectoryHealth, ManagedDirectory, RemoteAgent
from . import config_service

logger = logging.getLogger(__name__)

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"
STATUSES = (OK, WARN, FAIL, UNKNOWN)

DEFAULT_INTERVAL_MINUTES = 15
# An on-prem probe is a job row on /jobs. Probing every 15 minutes would bury the jobs
# list in health checks, so on-prem directories are probed at most hourly.
ONPREM_MIN_INTERVAL_MINUTES = 60
# An agent that has not finished a probe in this long is treated as not going to: the
# next pass queues a fresh one rather than waiting on a job that was lost.
PENDING_TIMEOUT = timedelta(minutes=30)

# Providers with nothing of their own to check: a cloud-side link to an on-prem domain
# runs no domain controllers, so its health is the on-prem directory's.
UNMONITORED_PROVIDERS = ("dns_link", "gcp_vyos_link")
# Entra Connect runs a delta sync about every 30 minutes.
SYNC_WARN_AFTER = timedelta(hours=1)
SYNC_FAIL_AFTER = timedelta(hours=3)

_PROBE_KEY = "ldap_probe"

# The domain controller deep check: builtin:ad-dc-health, an agent_ansible run over WinRM on
# an on-prem AD domain, with the directory's Password Safe account checked out per run.
DEEP_ASSET = "builtin:ad-dc-health"
DEFAULT_DEEP_INTERVAL_HOURS = 6
DEEP_PENDING_TIMEOUT = timedelta(minutes=60)
_DEEP_KEY = "deep_health"
_DC_PREFIX = "ad_dc:"           # not "dc:", which the AWS checks use for their DCs
CORE_SERVICES = ("NTDS", "DNS", "Netlogon", "Kdc", "W32Time", "DFSR")
# Kerberos refuses tickets beyond 5 minutes of skew; warn well before that.
SKEW_WARN_S, SKEW_FAIL_S = 60, 240
REPL_WARN_AGE, REPL_FAIL_AGE = timedelta(hours=3), timedelta(hours=24)
REPL_FAIL_FAILURES = 3
CERT_WARN_DAYS, CERT_FAIL_DAYS = 30, 7
# DfsrReplicatedFolderInfo.State: 0 uninitialised, 1 initialised, 2 initial sync,
# 3 auto recovery, 4 normal, 5 in error.
SYSVOL_STATES = {0: "uninitialised", 1: "initialised", 2: "initial sync",
                 3: "auto recovery", 4: "normal", 5: "in error"}
_CHUNK_RE = re.compile(r"DIRHEALTH_B64:(\d{1,4})/(\d{1,4}):([A-Za-z0-9+/=]{1,8000})")
MAX_REPORT_BYTES = 2_000_000
MAX_REPORT_DCS = 50
MAX_REPORT_LINKS = 300


def _check(key: str, label: str, status: str, detail: str = "") -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail}


def overall(checks: list) -> str:
    """The worst status among ``checks``. Unknown never outranks a real answer: a
    directory whose control plane is fine but whose sync time cannot be read is ok."""
    seen = {c.get("status") for c in checks}
    for status in (FAIL, WARN, OK):
        if status in seen:
            return status
    return UNKNOWN


def interval_minutes() -> int:
    """Minutes between checks. 0 turns the scheduled checks off; the page's Check now
    still works."""
    try:
        value = int(config_service.get("directory_health_interval_minutes")
                    or DEFAULT_INTERVAL_MINUTES)
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL_MINUTES
    return max(0, value)


# ── AWS Directory Service ─────────────────────────────────────────────────────

_AWS_STAGE = {"Active": OK, "Requested": WARN, "Creating": WARN, "Created": WARN,
              "RestoreFromSnapshot": WARN, "Restoring": WARN, "Updating": WARN,
              "Impaired": FAIL, "Inoperable": FAIL, "Failed": FAIL, "RestoreFailed": FAIL,
              "Deleting": FAIL, "Deleted": FAIL}
_AWS_DC = {"Active": OK, "Creating": WARN, "Restoring": WARN, "Updating": WARN,
           "Impaired": FAIL, "Failed": FAIL, "Deleting": FAIL, "Deleted": FAIL}


def _aws_sync(region: str, directory_id: str, with_dcs: bool) -> tuple:
    import boto3
    from . import aws_service
    ds = boto3.client("ds", **aws_service._aws_kwargs(region))
    found = ds.describe_directories(DirectoryIds=[directory_id]).get(
        "DirectoryDescriptions") or []
    dcs = []
    if with_dcs and found:
        token = None
        while True:
            kw = {"NextToken": token} if token else {}
            resp = ds.describe_domain_controllers(DirectoryId=directory_id, **kw)
            dcs.extend(resp.get("DomainControllers") or [])
            token = resp.get("NextToken")
            if not token:
                break
    return (found[0] if found else None), dcs


def aws_checks(directory: Optional[dict], dcs: list) -> list:
    """Checks from ``DescribeDirectories`` and ``DescribeDomainControllers``. Pure."""
    if directory is None:
        return [_check("control_plane", "AWS Directory Service", FAIL,
                       "The directory no longer exists in this account and region.")]
    stage = directory.get("Stage") or ""
    reason = directory.get("StageReason") or ""
    out = [_check("control_plane", "AWS Directory Service", _AWS_STAGE.get(stage, WARN),
                  f"Stage {stage or 'unknown'}" + (f": {reason}" if reason else ""))]
    for dc in dcs:
        status = dc.get("Status") or ""
        where = dc.get("AvailabilityZone") or dc.get("SubnetId") or ""
        why = dc.get("StatusReason") or ""
        out.append(_check(
            f"dc:{dc.get('DomainControllerId')}",
            f"Domain controller {dc.get('DnsIpAddr') or dc.get('DomainControllerId')}",
            _AWS_DC.get(status, WARN),
            f"{status or 'unknown'}" + (f" in {where}" if where else "")
            + (f": {why}" if why else "")))
    return out


async def _aws(row: ManagedDirectory) -> list:
    if not row.directory_id or not row.region:
        return [_check("control_plane", "AWS Directory Service", UNKNOWN,
                       "The row has no directory id or region to look up.")]
    directory, dcs = await asyncio.to_thread(
        _aws_sync, row.region, row.directory_id, row.provider == "aws_managed_ad")
    return aws_checks(directory, dcs)


# ── GCP Managed Service for Microsoft AD ──────────────────────────────────────

_GCP_STATE = {"READY": OK, "CREATING": WARN, "UPDATING": WARN, "REPAIRING": WARN,
              "PERFORMING_MAINTENANCE": WARN, "DOWN": FAIL, "DELETING": FAIL}


def gcp_checks(domain: Optional[dict]) -> list:
    """Checks from a ``managedidentities`` domain resource. Pure."""
    if domain is None:
        return [_check("control_plane", "GCP Managed AD", FAIL,
                       "The domain no longer exists in this project.")]
    state = domain.get("state") or ""
    msg = domain.get("statusMessage") or ""
    regions = ", ".join(domain.get("locations") or [])
    detail = f"State {state or 'unknown'}" + (f": {msg}" if msg else "")
    if regions:
        detail += f" (domain controllers in {regions})"
    return [_check("control_plane", "GCP Managed AD", _GCP_STATE.get(state, WARN), detail)]


def _gcp_sync(resource_name: str) -> Optional[dict]:
    from . import gcp_service
    session = gcp_service._authed_session()
    resp = session.get(f"https://managedidentities.googleapis.com/v1/{resource_name}")
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise RuntimeError(f"GCP answered HTTP {resp.status_code}")
    return resp.json()


async def _gcp(row: ManagedDirectory) -> list:
    if not row.resource_name:
        return [_check("control_plane", "GCP Managed AD", UNKNOWN,
                       "The row has no domain resource name to look up.")]
    return gcp_checks(await asyncio.to_thread(_gcp_sync, row.resource_name))


# ── Microsoft Entra Domain Services ───────────────────────────────────────────

def azure_checks(resource: Optional[dict]) -> list:
    """Checks from a ``Microsoft.AAD/domainServices`` resource. Pure.

    Entra DS evaluates its own health per replica set and raises alerts for what it
    finds (an NSG blocking its management ports, an expiring secure LDAP certificate,
    a sync that has stopped). Those alerts are the service's health, so they are
    reported as they come rather than re-derived."""
    if resource is None:
        return [_check("control_plane", "Entra Domain Services", FAIL,
                       "The managed domain no longer exists in this subscription.")]
    props = resource.get("properties") or {}
    state = props.get("provisioningState") or ""
    out = [_check("control_plane", "Entra Domain Services",
                  OK if state == "Succeeded" else (FAIL if state == "Failed" else WARN),
                  f"Provisioning state {state or 'unknown'}")]
    for rs in props.get("replicaSets") or []:
        where = rs.get("location") or rs.get("replicaSetId") or "replica set"
        service = rs.get("serviceStatus") or ""
        evaluated = rs.get("healthLastEvaluated") or ""
        out.append(_check(
            f"replica:{rs.get('replicaSetId') or where}", f"Replica set {where}",
            OK if service == "Running" else WARN,
            f"Service {service or 'unknown'}"
            + (f", health last evaluated {evaluated}" if evaluated else "")))
        for alert in rs.get("healthAlerts") or []:
            severity = (alert.get("severity") or "").strip()
            out.append(_check(
                f"alert:{alert.get('id') or alert.get('name')}",
                f"Alert in {where}: {alert.get('name') or 'health alert'}",
                FAIL if severity.lower() in ("high", "critical") else WARN,
                (alert.get("issue") or "")
                + (f" (severity {severity})" if severity else "")))
    return out


async def _azure(row: ManagedDirectory) -> list:
    from . import directory_service
    if not row.resource_name:
        return [_check("control_plane", "Entra Domain Services", UNKNOWN,
                       "The row has no resource id to look up.")]
    status, body = await directory_service._arm_get(
        row.resource_name, {"api-version": directory_service._AZURE_DS_API})
    if status == 404:
        return azure_checks(None)
    if status != 200:
        raise RuntimeError(f"Azure answered HTTP {status}")
    return azure_checks(body)


# ── cloud identity providers ──────────────────────────────────────────────────

def sync_check(sync: Optional[dict], *, now: Optional[datetime] = None) -> Optional[dict]:
    """The Entra Connect freshness check, or None for a cloud-only tenant. Pure."""
    if not sync or not sync.get("enabled"):
        return None
    raw = sync.get("last_sync") or ""
    if not raw:
        return _check("hybrid_sync", "Entra Connect sync", WARN,
                      "Directory sync is on but Entra ID reports no completed sync.")
    try:
        last = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return _check("hybrid_sync", "Entra Connect sync", UNKNOWN,
                      f"Unreadable last sync time {raw!r}.")
    age = (now or datetime.utcnow()) - last
    status = OK if age <= SYNC_WARN_AFTER else (WARN if age <= SYNC_FAIL_AFTER else FAIL)
    minutes = max(0, int(age.total_seconds() // 60))
    return _check("hybrid_sync", "Entra Connect sync", status,
                  f"Last sync {minutes} minute(s) ago ({raw}).")


async def _idp(row: ManagedDirectory) -> list:
    from . import directory_service
    from .directory_service import DirectoryError
    label = directory_service.PROVIDER_LABELS.get(row.provider, row.provider)
    started = time.monotonic()
    # idp_call rather than idp_test: idp_test forgets the cached sign-in first, and for a
    # Password Safe-managed credential that would be a checkout every interval.
    try:
        result = await directory_service.idp_call(row, "test")
    except DirectoryError as exc:
        return [_check("sign_in", f"{label} sign-in and read", FAIL, str(exc))]
    ms = int((time.monotonic() - started) * 1000)
    if result.get("ok"):
        out = [_check("sign_in", f"{label} sign-in and read", OK,
                      f"Signed in and read a user in {ms} ms.")]
    else:
        out = [_check("sign_in", f"{label} sign-in and read", WARN,
                      result.get("detail") or "Signed in, but could not read.")]
    if row.provider == "entra_id":
        try:
            check = sync_check(await directory_service.idp_call(row, "sync_status"))
        except DirectoryError as exc:
            check = _check("hybrid_sync", "Entra Connect sync", UNKNOWN,
                           f"{exc} Grant Organization.Read.All to see sync freshness.")
        if check:
            out.append(check)
    return out


# ── on-prem AD and LDAP, through the remote agent ─────────────────────────────

def probe_check(row: ManagedDirectory, result: Optional[dict], *, failed: bool = False,
                error: str = "", agent_name: str = "") -> dict:
    """The LDAP check from a finished single-host discovery job. Pure."""
    target = f"{row.host}:{row.port}"
    via = f" through agent '{agent_name}'" if agent_name else ""
    if failed:
        return _check(_PROBE_KEY, "LDAP answers", FAIL,
                      f"The probe of {target}{via} failed: {error or 'no reason given'}.")
    findings = [f for f in ((result or {}).get("findings") or [])
                if isinstance(f, dict) and f.get("kind") == "directory"]
    if not findings:
        return _check(_PROBE_KEY, "LDAP answers", FAIL,
                      f"No LDAP answer from {target}{via}. Check the host is up and that "
                      f"the agent's policy.yaml lists it under targets.")
    found = findings[0]
    seen = []
    if found.get("dc_hostname"):
        seen.append(f"DC {found['dc_hostname']}")
    if found.get("functional_level"):
        seen.append(found["functional_level"])
    detail = f"{target} answered{via}" + (f" ({', '.join(seen)})" if seen else "") + "."
    if row.provider == "onprem_ad":
        want, got = (row.name or "").lower(), (found.get("domain") or "").lower()
        if got and want and got != want:
            return _check(_PROBE_KEY, "LDAP answers", WARN,
                          f"{target} answered for domain {got}, not {want}.")
    else:
        want = (row.base_dn or "").replace(" ", "").lower()
        got = (found.get("base_dn") or "").replace(" ", "").lower()
        if got and want and got != want and not want.endswith(got) and not got.endswith(want):
            return _check(_PROBE_KEY, "LDAP answers", WARN,
                          f"{target} serves {found.get('base_dn')}, not {row.base_dn}.")
    return _check(_PROBE_KEY, "LDAP answers", OK, detail)


def _probe_meta(row: ManagedDirectory) -> dict:
    from . import agent_job_meta
    return agent_job_meta.normalize({
        "description": f"Directory health probe: {row.name}",
        "scan_kind": "directory", "hostnames": [row.host], "cidrs": [],
        "ports": {"directory": [int(row.port or 389)]},
        "timeout_s": 5, "max_hosts": 1, "concurrency": 1,
    })


def _onprem(db: Session, row: ManagedDirectory, health: DirectoryHealth,
            previous: list, *, force: bool, force_deep: bool = False) -> list:
    from . import agent_service, job_service
    agent = db.get(RemoteAgent, row.agent_id) if row.agent_id else None
    if agent is None:
        return [_check("agent", "Remote agent", FAIL,
                       "No remote agent is assigned, so nothing can reach this directory.")]
    state = agent_service.status_of(agent)
    out = [_check("agent", f"Remote agent '{agent.name}'",
                  OK if state == "online" else FAIL,
                  f"{state.capitalize()}"
                  + (f", last seen {agent.last_seen_at:%Y-%m-%d %H:%M} UTC"
                     if agent.last_seen_at else ""))]
    last_probe = next((c for c in previous if c.get("key") == _PROBE_KEY), None)

    refusal = ""
    if state != "online":
        refusal = "The agent is not online, so no probe was queued."
    elif not agent_service.supports_directory(agent):
        refusal = agent_service.directory_upgrade_hint(agent)
    elif agent.reported_job_types_list and \
            "agent_discover" not in agent.reported_job_types_list:
        refusal = (f"Agent '{agent.name}' does not offer discovery. Add agent_discover to "
                   f"job_types in its policy.yaml.")
    elif not row.host:
        refusal = "The directory has no host to probe."

    if refusal:
        out.append(_check(_PROBE_KEY, "LDAP answers", UNKNOWN, refusal))
    else:
        now = datetime.utcnow()
        waiting = health.pending_job_id and health.pending_since and \
            now - health.pending_since < PENDING_TIMEOUT
        due = force or not health.probe_queued_at or \
            now - health.probe_queued_at >= timedelta(minutes=ONPREM_MIN_INTERVAL_MINUTES)
        if not waiting and due:
            job = job_service.create_job(db, job_type="agent_discover",
                                         created_by="directory-health",
                                         metadata=_probe_meta(row), agent_id=agent.id)
            health.pending_job_id, health.pending_since = job.id, now
            health.probe_queued_at = now
            waiting = True
        if last_probe:
            out.append(last_probe)
        else:
            out.append(_check(_PROBE_KEY, "LDAP answers", UNKNOWN,
                              "Waiting for the agent to probe the directory."
                              if waiting else "Not probed yet."))

    if row.provider == "onprem_ad":
        out.extend(_deep(db, row, agent, state, health, previous, force=force_deep))
    return out


# ── the domain controller deep check (on-prem AD only) ────────────────────────

def deep_interval_hours() -> int:
    """Hours between deep checks. 0 turns the scheduled ones off; Check now still runs."""
    try:
        value = int(config_service.get("directory_health_deep_interval_hours")
                    or DEFAULT_DEEP_INTERVAL_HOURS)
    except (TypeError, ValueError):
        value = DEFAULT_DEEP_INTERVAL_HOURS
    return max(0, value)


def _is_deep(check: dict) -> bool:
    key = str(check.get("key") or "")
    return key in ("replication", "fsmo", _DEEP_KEY) or key.startswith(_DC_PREFIX)


def _deep_refusal(row: ManagedDirectory, agent: RemoteAgent, state: str) -> str:
    from . import agent_service, directory_service
    if not (row.credentials_ref or "").startswith(directory_service._MANAGED_REF_PREFIX):
        return ("The directory has no Password Safe account to run the check as. "
                "Register it with one to see replication, FSMO and DC health.")
    if state != "online":
        return "The agent is not online, so the domain controller check was not queued."
    if "agent_ansible" not in agent_service.allowed_job_types(agent):
        return (f"Agent '{agent.name}' is not granted Config Management, which runs the "
                f"domain controller check.")
    if not agent_service.supports_directory(agent):
        return agent_service.directory_upgrade_hint(agent)
    if not row.host:
        return "The directory has no domain controller host recorded."
    return ""


def _deep_meta(row: ManagedDirectory, agent: RemoteAgent) -> dict:
    from . import agent_ansible_meta
    return agent_ansible_meta.normalize({
        "description": f"Directory health: domain controller check of {row.name} "
                       f"(agent: {agent.name})",
        "run_kind": "directory", "transport": "winrm",
        "target_host": row.host, "target_port": 5986,
        "target_id": row.id, "target_label": f"onprem_ad/{row.name}",
        "asset": DEEP_ASSET, "asset_backend": "", "extra_vars": {},
    })


def _deep(db: Session, row: ManagedDirectory, agent: RemoteAgent, state: str,
          health: DirectoryHealth, previous: list, *, force: bool) -> list:
    """Queue the deep check when it is due, and carry its last results forward."""
    from . import agent_ansible_meta, job_service
    kept = [c for c in previous if _is_deep(c) and c.get("key") != _DEEP_KEY]
    refusal = _deep_refusal(row, agent, state)
    if refusal:
        return kept + [_check(_DEEP_KEY, "Domain controller check", UNKNOWN, refusal)]

    now = datetime.utcnow()
    waiting = bool(health.deep_job_id and health.deep_since
                   and now - health.deep_since < DEEP_PENDING_TIMEOUT)
    hours = deep_interval_hours()
    due = force or (hours > 0 and (not health.deep_queued_at or
                                   now - health.deep_queued_at >= timedelta(hours=hours)))
    if not waiting and due:
        meta = _deep_meta(row, agent)
        problem = agent_ansible_meta.check(meta)
        if problem:
            return kept + [_check(_DEEP_KEY, "Domain controller check", UNKNOWN, problem)]
        job = job_service.create_job(db, job_type="agent_ansible",
                                     created_by="directory-health", workgroup="ansible",
                                     metadata=meta, agent_id=agent.id)
        health.deep_job_id, health.deep_since, health.deep_queued_at = job.id, now, now
        waiting = True
    if kept:
        return kept
    previous_status = next((c for c in previous if c.get("key") == _DEEP_KEY), None)
    if previous_status and not waiting:
        return [previous_status]
    return [_check(_DEEP_KEY, "Domain controller check", UNKNOWN,
                   "Waiting for the agent to run the domain controller check."
                   if waiting else "Not run yet.")]


def parse_report(lines) -> Optional[dict]:
    """The dc-health report from a job's log lines, or None. The lines came from a
    domain controller through the agent, so everything here is untrusted: sizes are
    capped, the base64 must be strict, and the shape is checked before use."""
    chunks: dict = {}
    total = 0
    for line in lines:
        for m in _CHUNK_RE.finditer(str(line or "")):
            i, n, data = int(m.group(1)), int(m.group(2)), m.group(3)
            if i == 1 or n != total:
                chunks, total = {}, n          # a new set starts; keep only the latest
            chunks[i] = data
    if not total or any(i not in chunks for i in range(1, total + 1)):
        return None
    joined = "".join(chunks[i] for i in range(1, total + 1))
    if len(joined) > MAX_REPORT_BYTES:
        return None
    try:
        report = json.loads(base64.b64decode(joined, validate=True).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(report, dict) or report.get("v") != 1:
        return None
    for key, kind in (("dcs", list), ("replication", list), ("fsmo", dict), ("errors", list)):
        value = report.get(key)
        if value is None:
            report[key] = kind()
        elif not isinstance(value, kind):
            return None
    report["dcs"] = [d for d in report["dcs"] if isinstance(d, dict)][:MAX_REPORT_DCS]
    report["replication"] = [r for r in report["replication"]
                             if isinstance(r, dict)][:MAX_REPORT_LINKS]
    return report


def _text(value, limit: int = 200) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value if value is not None else ""))[:limit]


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _when(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        from datetime import timezone
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _short_name(host: str) -> str:
    return (host or "").split(".")[0].lower()


def _worst(statuses) -> str:
    return overall([{"status": s} for s in statuses])


def _errors_for(report: dict, prefix: str) -> str:
    found = [_text(e, 300) for e in report.get("errors") or []
             if str(e).lower().startswith(prefix)]
    return found[0] if found else ""


def _replication_check(report: dict, now: datetime) -> dict:
    links = report.get("replication") or []
    if not links:
        err = _errors_for(report, "replication")
        if err:
            return _check("replication", "AD replication", UNKNOWN, f"Could not read: {err}")
        if len(report.get("dcs") or []) <= 1:
            return _check("replication", "AD replication", OK,
                          "One domain controller, so there is nothing to replicate.")
        return _check("replication", "AD replication", UNKNOWN,
                      "No replication partners were reported.")
    worst, worst_detail = OK, ""
    for link in links:
        failures = _int(link.get("failures")) or 0
        result = _int(link.get("last_result")) or 0
        last = _when(link.get("last_success"))
        age = (now - last) if last else None
        if failures >= REPL_FAIL_FAILURES or age is None or age > REPL_FAIL_AGE:
            status = FAIL
        elif failures or result or age > REPL_WARN_AGE:
            status = WARN
        else:
            continue
        if _worst([worst, status]) != worst or not worst_detail:
            worst = _worst([worst, status])
            ago = f"{int(age.total_seconds() // 3600)}h ago" if age else "never"
            worst_detail = (f"{_text(link.get('server'))} from {_text(link.get('partner'))}: "
                            f"{failures} consecutive failure(s), last result {result}, "
                            f"last success {ago}")
    if worst == OK:
        return _check("replication", "AD replication", OK,
                      f"{len(links)} inbound link(s) replicating.")
    return _check("replication", "AD replication", worst,
                  f"{worst_detail} ({len(links)} inbound link(s) checked).")


def _fsmo_check(report: dict) -> dict:
    fsmo = report.get("fsmo") or {}
    if not fsmo:
        err = _errors_for(report, "fsmo")
        return _check("fsmo", "FSMO roles", UNKNOWN,
                      f"Could not read: {err}" if err else "No role holders were reported.")
    known = {_short_name(d.get("name")) for d in report.get("dcs") or []}
    holders: dict = {}
    missing = []
    for role, holder in fsmo.items():
        holder = _text(holder)
        holders.setdefault(holder, []).append(_text(role, 40))
        if known and _short_name(holder) not in known:
            missing.append(f"{role} on {holder}")
    if missing:
        return _check("fsmo", "FSMO roles", FAIL,
                      "Held by a server that is not a current domain controller: "
                      + "; ".join(missing) + ".")
    return _check("fsmo", "FSMO roles", OK,
                  "; ".join(f"{h}: {', '.join(r)}" for h, r in holders.items()) + ".")


def _dc_check(dc: dict, now: datetime) -> dict:
    name = _text(dc.get("name"), 120) or "unknown"
    parts, statuses = [], []
    errors = [_text(e, 200) for e in (dc.get("errors") or []) if isinstance(e, str)]

    services = dc.get("services") if isinstance(dc.get("services"), dict) else {}
    stopped = [f"{svc} {_text(state, 20).lower()}" for svc, state in services.items()
               if svc in CORE_SERVICES and str(state) != "Running"]
    if stopped:
        statuses.append(FAIL)
        parts.append("stopped: " + ", ".join(stopped))
    elif services:
        statuses.append(OK)
        parts.append("services running")

    offset = dc.get("offset_s")
    if isinstance(offset, (int, float)):
        skew = abs(float(offset))
        statuses.append(FAIL if skew > SKEW_FAIL_S else WARN if skew > SKEW_WARN_S else OK)
        parts.append(f"clock offset {offset:+.0f}s")

    sysvol = _int(dc.get("sysvol_state"))
    if sysvol is not None:
        statuses.append(OK if sysvol == 4 else FAIL if sysvol == 5 else WARN)
        parts.append(f"SYSVOL {SYSVOL_STATES.get(sysvol, f'state {sysvol}')}")

    not_after = _when(dc.get("ldaps_not_after"))
    if not_after:
        days = (not_after - now).days
        statuses.append(FAIL if days < CERT_FAIL_DAYS else WARN if days < CERT_WARN_DAYS else OK)
        parts.append(f"LDAPS certificate expired {-days} day(s) ago" if days < 0
                     else f"LDAPS certificate expires in {days} day(s)")

    if errors:
        parts.append("could not read " + "; ".join(errors))
    status = _worst(statuses) if statuses else UNKNOWN
    site = _text(dc.get("site"), 80)
    label = f"Domain controller {name}" + (f" ({site})" if site else "")
    return _check(f"{_DC_PREFIX}{name.lower()}", label, status,
                  ("; ".join(parts) or "Nothing could be read.") + ".")


def dc_health_checks(report: dict, *, now: Optional[datetime] = None) -> list:
    """Checks from a parsed dc-health report. Pure."""
    now = now or datetime.utcnow()
    out = [_replication_check(report, now), _fsmo_check(report)]
    out.extend(_dc_check(dc, now) for dc in report.get("dcs") or [])
    if not report.get("dcs"):
        err = _errors_for(report, "domain controllers") or _errors_for(report, "collector")
        out.append(_check(_DEEP_KEY, "Domain controller check", UNKNOWN,
                          f"No domain controllers were read: {err}" if err else
                          "No domain controllers were read."))
    return out


def apply_deep(db: Session, job, *, status: str, error: str = "") -> bool:
    """Fold a finished deep check into its directory's health. ``True`` if ``job`` was one.

    Called from ``api/agent.complete`` for every ``agent_ansible`` job; anything not
    queued by :func:`_deep` matches no row and returns ``False`` untouched. The report
    is read from the job's log lines, which the agent pushes before it completes.
    """
    from . import job_service
    health = db.query(DirectoryHealth).filter(
        DirectoryHealth.deep_job_id == job.id).first()
    if health is None:
        return False
    health.deep_job_id, health.deep_since = None, None
    if status != "completed":
        fresh = [_check(_DEEP_KEY, "Domain controller check", FAIL,
                        f"The check failed (job {job.id}): {_text(error, 300) or 'no reason given'}.")]
    else:
        report = parse_report(line for _seq, line in job_service.get_job_logs(db, job.id))
        fresh = dc_health_checks(report) if report else [
            _check(_DEEP_KEY, "Domain controller check", FAIL,
                   f"The check finished but printed no readable report (job {job.id}).")]
    checks = [c for c in _loads(health.checks) if not _is_deep(c)] + fresh
    health.checks = json.dumps(checks)
    health.status = overall(checks)
    db.commit()
    return True


def apply_probe(db: Session, job, *, status: str, result: Optional[dict],
                error: str = "") -> bool:
    """Fold a finished probe into its directory's health. ``True`` if ``job`` was one.

    Called from ``api/agent.complete`` for every ``agent_discover`` job; anything not
    queued by :func:`_onprem` matches no row and returns ``False`` untouched.
    """
    health = db.query(DirectoryHealth).filter(
        DirectoryHealth.pending_job_id == job.id).first()
    if health is None:
        return False
    row = db.get(ManagedDirectory, health.directory_id)
    health.pending_job_id, health.pending_since = None, None
    if row is None:
        db.commit()
        return True
    agent = db.get(RemoteAgent, row.agent_id) if row.agent_id else None
    probe = probe_check(row, result, failed=(status != "completed"), error=error,
                        agent_name=agent.name if agent else "")
    checks = [c for c in _loads(health.checks) if c.get("key") != _PROBE_KEY] + [probe]
    health.checks = json.dumps(checks)
    health.status = overall(checks)
    db.commit()
    return True


# ── one directory, and every due directory ────────────────────────────────────

def _loads(raw) -> list:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _health_row(db: Session, directory_id: str) -> DirectoryHealth:
    health = db.get(DirectoryHealth, directory_id)
    if health is None:
        health = DirectoryHealth(directory_id=directory_id, status=UNKNOWN)
        db.add(health)
    return health


def monitored(row: ManagedDirectory) -> bool:
    return row.status != "deleted" and row.provider not in UNMONITORED_PROVIDERS


async def check(db: Session, row: ManagedDirectory, *, force: bool = False,
                force_deep: bool = False) -> dict:
    """Check one directory now, store the result, and return it as :func:`to_dict`.

    ``force`` re-runs the cheap checks now. ``force_deep`` also queues the domain
    controller check now, and is separate because that run checks out the directory's
    Password Safe credential: the API grants it only to ``directories:write``."""
    from . import directory_service
    health = _health_row(db, row.id)
    previous = _loads(health.checks)
    started = time.monotonic()
    try:
        if row.status != "available":
            checks = [_check("lifecycle", "Lifecycle", UNKNOWN,
                             f"Not checked while the directory is {row.status}.")]
        elif row.provider in directory_service.IDP_PROVIDERS:
            checks = await _idp(row)
        elif row.provider in directory_service.ONPREM_PROVIDERS:
            checks = _onprem(db, row, health, previous, force=force, force_deep=force_deep)
        elif row.cloud == "aws":
            checks = await _aws(row)
        elif row.cloud == "gcp":
            checks = await _gcp(row)
        elif row.cloud == "azure":
            checks = await _azure(row)
        else:
            checks = [_check("control_plane", "Health", UNKNOWN,
                             "No health check exists for this kind of directory.")]
    except Exception as exc:  # noqa: BLE001 - a provider error is a result, not a crash
        logger.warning("directory health: %s (%s) check failed: %s", row.name, row.id, exc)
        checks = [_check("control_plane", "Health check", UNKNOWN,
                         f"Could not ask the provider: {type(exc).__name__}: "
                         f"{str(exc)[:300]}")]
    health.checks = json.dumps(checks)
    health.status = overall(checks)
    health.checked_at = datetime.utcnow()
    health.duration_ms = int((time.monotonic() - started) * 1000)
    db.commit()
    return to_dict(row, health)


def _due(health: Optional[DirectoryHealth], minutes: int, now: datetime) -> bool:
    return health is None or health.checked_at is None or \
        now - health.checked_at >= timedelta(minutes=minutes)


async def check_due(db: Session) -> int:
    """Check every monitored directory whose last check is older than the interval."""
    from . import feature_flags
    minutes = interval_minutes()
    if not minutes or not feature_flags.enabled("directories_enabled"):
        return 0
    now = datetime.utcnow()
    rows = [r for r in db.query(ManagedDirectory)
            .filter(ManagedDirectory.status != "deleted").all() if monitored(r)]
    done = 0
    for row in rows:
        if not _due(db.get(DirectoryHealth, row.id), minutes, now):
            continue
        await check(db, row)
        done += 1
    return done


async def check_loop() -> None:
    """Forever: check what is due, sleep a minute. A peer of the job runner, like
    ``dashboard_collect.collect_loop``, and for the same reasons: not a job type, owns a
    try/except per pass, and re-reads its settings each pass."""
    from ..database import SessionLocal
    while True:
        try:
            db = SessionLocal()
            try:
                done = await check_due(db)
                if done:
                    logger.info("directory health: checked %d directory(ies)", done)
            finally:
                db.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # noqa: BLE001
            logger.warning("directory health pass failed: %s", exc)
        await asyncio.sleep(60)


# ── what the page reads ───────────────────────────────────────────────────────

def to_dict(row: ManagedDirectory, health: Optional[DirectoryHealth]) -> dict:
    from . import directory_service
    return {
        "directory_id": row.id, "name": row.name, "provider": row.provider,
        "provider_label": directory_service.PROVIDER_LABELS.get(row.provider, row.provider),
        "cloud": row.cloud, "directory_status": row.status,
        "status": health.status if health else UNKNOWN,
        "checks": _loads(health.checks) if health else [],
        "checked_at": health.checked_at.isoformat() + "Z"
        if health and health.checked_at else None,
        "duration_ms": health.duration_ms if health else None,
        "probe_pending": bool(health and health.pending_job_id),
        "deep_pending": bool(health and health.deep_job_id),
    }


def snapshot(db: Session, rows: list) -> dict:
    """Every given directory's last check, worst first, plus a count per status."""
    from . import directory_service
    monitored_rows = [r for r in rows if monitored(r)]
    ids = [r.id for r in monitored_rows]
    found = {h.directory_id: h for h in
             db.query(DirectoryHealth).filter(DirectoryHealth.directory_id.in_(ids)).all()} \
        if ids else {}
    items = [to_dict(r, found.get(r.id)) for r in monitored_rows]
    rank = {FAIL: 0, WARN: 1, UNKNOWN: 2, OK: 3}
    items.sort(key=lambda i: (rank.get(i["status"], 2), i["name"] or ""))
    counts = {s: sum(1 for i in items if i["status"] == s) for s in STATUSES}
    return {"directories": items, "counts": counts,
            "interval_minutes": interval_minutes(),
            "unmonitored": [{"directory_id": r.id, "name": r.name,
                             "provider_label": directory_service.PROVIDER_LABELS.get(
                                 r.provider, r.provider)}
                            for r in rows
                            if r.status != "deleted" and r.provider in UNMONITORED_PROVIDERS]}
