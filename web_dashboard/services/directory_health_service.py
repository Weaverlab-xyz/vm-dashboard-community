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
"""
from __future__ import annotations

import asyncio
import json
import logging
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
    ds = aws_service.aws_client("ds", region)
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
            previous: list, *, force: bool) -> list:
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
        return out

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
    return out


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


async def check(db: Session, row: ManagedDirectory, *, force: bool = False) -> dict:
    """Check one directory now, store the result, and return it as :func:`to_dict`."""
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
            checks = _onprem(db, row, health, previous, force=force)
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
