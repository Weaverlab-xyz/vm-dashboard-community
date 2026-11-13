"""Stop and start for dashboard-provisioned cloud databases.

A demo cell's auto-suspend has to really stop what it suspends, or a "suspended" cell
keeps billing. VMs already have power ops (``*_power`` jobs, services/suspend_sweeper);
this module is the database half. It calls the cloud SDKs directly, never Terraform --
a stop is not a change to the resource's declared shape, and a terraform apply per
suspend would hold a HEAVY slot for the whole plan:

  ======================  ==============================  ===========================
  cloud / provider        stop                            start
  ======================  ==============================  ===========================
  GCP Cloud SQL           settings.activationPolicy=NEVER  activationPolicy=ALWAYS
  AWS RDS                 stop_db_instance                 start_db_instance
  Azure Flexible Server   POST .../flexibleServers/x/stop  POST .../start
  ======================  ==============================  ===========================

Everything else is refused up front, with the reason: Azure SQL Database (a logical
server, no stop), OCI Autonomous (not wired yet), MongoDB Atlas (paused through Atlas,
not the cloud), and any ``registered`` row (somebody else's database).

Row states. ``available`` -> ``stopping`` -> ``stopped`` -> ``starting`` -> ``available``.
The transitional two are written when the job is QUEUED, not when it runs, so every
path that refuses a non-``available`` database (Password Safe onboarding, Entitle
registration, the adapter pairing) refuses a database that is about to go down too.
A failed power job puts the row back where it was and records the error on
``power_state``; the job is the authoritative failure.

Bookkeeping lives in ``CloudDatabase.power_state`` (JSON, see :func:`read_state`).

AWS and Azure restart a stopped instance by themselves after 7 days. Nothing here can
prevent that -- neither cloud accepts a longer stop -- so :func:`needs_restop` answers
"has this row been stopped long enough that the cloud has probably started it again",
for a reconcile loop to re-issue the stop.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

JOB_TYPE = "clouddb_power"
ACTIONS = ("stop", "start")

STATUS_RUNNING = "available"
STATUS_STOPPED = "stopped"
STATUS_STOPPING = "stopping"
STATUS_STARTING = "starting"
TRANSITIONAL = frozenset({STATUS_STOPPING, STATUS_STARTING})
#: Every status this module writes. Code that asks "is this database alive and billable"
#: (the Gateway's reference count, the expiry reaper) must treat all four as alive.
POWER_STATUSES = frozenset({STATUS_STOPPED, STATUS_STOPPING, STATUS_STARTING})

#: Providers whose instance can be stopped and started, by the cloud's own API.
POWERABLE_PROVIDERS = frozenset({"rds", "cloudsql", "flexibleserver"})

#: How long each cloud lets an instance stay stopped before starting it again itself.
AUTO_RESTART_AFTER = {"aws": timedelta(days=7), "azure": timedelta(days=7)}

#: A stop or start that has not settled in this long fails the job. RDS stops in ~5
#: minutes and starts in ~10; Azure Flexible Server is similar; Cloud SQL is quicker.
POWER_TIMEOUT_S = 40 * 60
POLL_INTERVAL_S = 15.0

_AZURE_API = {
    "Microsoft.DBforPostgreSQL": "2022-12-01",
    "Microsoft.DBforMySQL": "2023-06-30",
}


class PowerRefused(Exception):
    """The request cannot be honoured in the row's current state (HTTP 409)."""


class PowerUnsupported(Exception):
    """This database can never be stopped by the dashboard (HTTP 400)."""


# ── bookkeeping ──────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def read_state(row) -> dict:
    """``power_state`` as a dict. Keys (all optional):

    ``state``        "running" | "stopped" -- what the last COMPLETED power job left
    ``resume_status`` the status a start returns the row to (always ``available`` today;
                     stored so a future powerable state does not need a migration)
    ``prior_status`` the status a FAILED job puts the row back to
    ``stopped_at`` / ``started_at``  ISO-8601 UTC
    ``job_id``       the last power job; ``action`` its action
    ``last_error``   the last failure, cleared by the next success
    """
    raw = getattr(row, "power_state", None)
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def _write_state(row, state: dict) -> None:
    row.power_state = json.dumps(state, sort_keys=True)


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def is_stopped(row) -> bool:
    """True when the row is stopped or on its way down. Callers deciding whether the
    INSTANCE is up use this rather than comparing the status to one literal."""
    return (getattr(row, "status", "") or "") in (STATUS_STOPPED, STATUS_STOPPING)


def needs_restop(row, now: Optional[datetime] = None) -> bool:
    """Whether a stopped database has been stopped long enough that its cloud has
    probably started it again (AWS RDS and Azure Flexible Server both do, after 7
    days), so a reconcile loop should queue another stop.

    False for anything not ``stopped``, for clouds with no such limit (Cloud SQL), and
    for a row with no recorded stop time -- never guess a stop time, because the cost of
    a wrong True is a needless stop job and the cost of a wrong False is only billing
    that the loop catches on its next pass once the time is recorded.
    """
    if (getattr(row, "status", "") or "") != STATUS_STOPPED:
        return False
    limit = AUTO_RESTART_AFTER.get(getattr(row, "cloud", "") or "")
    if limit is None:
        return False
    stopped_at = _parse_ts(read_state(row).get("stopped_at"))
    if stopped_at is None:
        return False
    return (now or _now()) >= stopped_at + limit


def unsupported_reason(row) -> Optional[str]:
    """Why this database can never be powered by the dashboard, or None."""
    if (getattr(row, "source", None) or "provisioned") == "registered":
        return ("a registered database belongs to somebody else's account; the dashboard "
                "does not stop or start it")
    provider = getattr(row, "provider", None) or ""
    if provider == "atlas":
        return "MongoDB Atlas clusters are paused through Atlas, not the cloud provider"
    if provider not in POWERABLE_PROVIDERS:
        return (f"{getattr(row, 'cloud', '')} {provider or 'database'} instances have no "
                f"stop/start the dashboard drives (supported: AWS RDS, GCP Cloud SQL, "
                f"Azure Flexible Server)")
    return None


def _active_power_job(db, db_id: str):
    from ..database import Job
    from . import job_service
    jobs = (db.query(Job)
              .filter(Job.job_type == JOB_TYPE,
                      Job.status.in_(job_service.ACTIVE_STATUSES))
              .order_by(Job.created_at.desc()).all())
    return next((j for j in jobs if (j.metadata_dict or {}).get("db_id") == db_id), None)


# ── enqueue (the API / a demo cell's suspend) ────────────────────────────────

def start_power(db, db_id: str, action: str, actor: str = "", *,
                restop: bool = False) -> dict:
    """Queue a ``clouddb_power`` job and flip the row to its transitional status.

    Idempotent where it can be: a stop of a stopped database, or a start of an available
    one, returns ``{"ok": True, "noop": True}`` with no job; a request while a power job
    for the same database is already queued or running returns THAT job when it is the
    same action, and refuses when it is the opposite one (two jobs racing is how a row
    ends up ``available`` over a stopped instance).

    ``restop`` re-issues a stop on a row that is already ``stopped`` -- what a reconcile
    loop does when :func:`needs_restop` says the cloud has started it again.

    Raises ``CloudDatabaseError`` (unknown id), :class:`PowerUnsupported` (can never be
    powered) or :class:`PowerRefused` (not now).
    """
    from ..database import CloudDatabase
    from . import job_service
    from .cloud_database_service import CloudDatabaseError

    if action not in ACTIONS:
        raise PowerUnsupported(f"unknown power action {action!r} (expected stop or start)")
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if row is None:
        raise CloudDatabaseError(f"database {db_id} not found")
    reason = unsupported_reason(row)
    if reason:
        raise PowerUnsupported(reason)

    status = row.status or ""
    active = _active_power_job(db, db_id)
    if active is not None:
        if (active.metadata_dict or {}).get("action") == action:
            return {"ok": True, "db_id": db_id, "action": action, "job_id": active.id,
                    "status": status}
        raise PowerRefused(f"a {(active.metadata_dict or {}).get('action')} of this "
                           f"database is already in progress (job {active.id})")

    if status in TRANSITIONAL:
        # No active job, yet the row is mid-transition: the job that set it died (worker
        # recycled, reconciled stale). Either action is allowed; the runner reads the
        # instance's real state before acting, so retrying converges.
        prior = read_state(row).get("prior_status") or STATUS_RUNNING
    elif action == "stop":
        if status == STATUS_STOPPED and not restop:
            return {"ok": True, "noop": True, "db_id": db_id, "action": action,
                    "status": status}
        if status not in (STATUS_RUNNING, STATUS_STOPPED):
            raise PowerRefused(f"database is {status!r} -- only an available database can "
                               f"be stopped")
        prior = status
    else:
        if status == STATUS_RUNNING:
            return {"ok": True, "noop": True, "db_id": db_id, "action": action,
                    "status": status}
        if status != STATUS_STOPPED:
            raise PowerRefused(f"database is {status!r} -- only a stopped database can "
                               f"be started")
        prior = status

    state = read_state(row)
    state["prior_status"] = prior
    _write_state(row, state)
    row.status = STATUS_STOPPING if action == "stop" else STATUS_STARTING
    db.commit()
    job = job_service.create_job(
        db, job_type=JOB_TYPE, created_by=actor or row.created_by or "system",
        metadata={"db_id": db_id, "action": action, "cloud": row.cloud,
                  "engine": row.engine, **({"restop": True} if restop else {})},
        workgroup=row.workgroup,
    )
    try:
        job_service.log_audit(db, actor or "system", f"clouddb_power_{action}",
                              details={"db_id": db_id, "job_id": job.id,
                                       "cloud": row.cloud, "engine": row.engine})
    except Exception as exc:  # noqa: BLE001 - the audit row never blocks the job
        logger.warning("clouddb power: audit for %s failed: %s", db_id, exc)
    return {"ok": True, "db_id": db_id, "action": action, "job_id": job.id,
            "status": row.status}


def revert_stale(row) -> None:
    """Put a row a dead power job left mid-transition back where it was. Called by
    ``job_service._flip_resource_row`` when the stale reconciler fails the job."""
    if (row.status or "") in TRANSITIONAL:
        row.status = read_state(row).get("prior_status") or STATUS_RUNNING


# ── cloud backends ───────────────────────────────────────────────────────────
#
# Each returns a backend with three blocking methods -- ``request_stop()``,
# ``request_start()`` and ``read()`` -- where read() answers "running", "stopped",
# "transition" or a cloud-specific string (anything else is a failure). The runner does
# the waiting in asyncio, with each read a short call on the clouddb pool, so a
# 10-minute stop never parks a thread for 10 minutes.

class _RdsBackend:
    _RUNNING = {"available"}
    _STOPPED = {"stopped"}
    _TRANSITION = {"stopping", "starting", "backing-up", "modifying", "configuring-enhanced-monitoring",
                   "configuring-log-exports", "rebooting", "maintenance", "upgrading"}

    def __init__(self, identifier: str, region: str):
        self.identifier, self.region = identifier, region

    def _client(self):
        from . import aws_service
        return aws_service.aws_client("rds", self.region)

    def read(self) -> str:
        resp = self._client().describe_db_instances(DBInstanceIdentifier=self.identifier)
        st = ((resp.get("DBInstances") or [{}])[0].get("DBInstanceStatus") or "").lower()
        if st in self._RUNNING:
            return "running"
        if st in self._STOPPED:
            return "stopped"
        if st in self._TRANSITION:
            return "transition"
        return st or "unknown"

    def request_stop(self) -> None:
        self._client().stop_db_instance(DBInstanceIdentifier=self.identifier)

    def request_start(self) -> None:
        self._client().start_db_instance(DBInstanceIdentifier=self.identifier)


class _CloudSqlBackend:
    def __init__(self, project: str, name: str):
        self.project, self.name = project, name

    def _session(self):
        from . import gcp_service
        return gcp_service._authed_session()

    def _url(self) -> str:
        from . import gcp_service
        return f"{gcp_service._SQLADMIN_BASE}/projects/{self.project}/instances/{self.name}"

    def read(self) -> str:
        r = self._session().get(self._url())
        r.raise_for_status()
        body = r.json() or {}
        policy = ((body.get("settings") or {}).get("activationPolicy") or "").upper()
        state = (body.get("state") or "").upper()
        # activationPolicy is what was ASKED for; state is what GCP has done about it.
        # Cloud SQL reports a stopped instance as state=STOPPED (older API versions left
        # it RUNNABLE with policy NEVER), so both spellings of "stopped" are accepted.
        if policy == "NEVER" and state in ("STOPPED", "RUNNABLE", "SUSPENDED"):
            return "stopped"
        if policy == "ALWAYS" and state == "RUNNABLE":
            return "running"
        if state in ("PENDING_CREATE", "MAINTENANCE", "STOPPED", "RUNNABLE"):
            return "transition"
        return state.lower() or "unknown"

    def _patch(self, policy: str) -> None:
        from . import gcp_service
        s = self._session()
        r = s.patch(self._url(), json={"settings": {"activationPolicy": policy}})
        r.raise_for_status()
        op = (r.json() or {}).get("name") or ""
        # Short wait on the patch's own operation so a rejected patch fails here with
        # GCP's message; the runner's poll covers the instance actually settling.
        gcp_service._sql_wait_operation(s, self.project, op, timeout_s=600,
                                        what=f"activationPolicy={policy}")

    def request_stop(self) -> None:
        self._patch("NEVER")

    def request_start(self) -> None:
        self._patch("ALWAYS")


class _AzureFlexibleBackend:
    """ARM REST, as azure_service.aks_azure_rbac_enabled does -- the image carries no
    azure-mgmt-rdbms, and POST .../stop is exactly what the SDK's begin_stop sends."""

    def __init__(self, resource_id: str, credential):
        self.resource_id, self.credential = resource_id, credential
        ns = next((n for n in _AZURE_API if f"/providers/{n}/".lower()
                   in resource_id.lower()), "")
        if not ns or "/flexibleservers/" not in resource_id.lower():
            raise PowerUnsupported(f"{resource_id!r} is not an Azure Flexible Server id")
        self.api = _AZURE_API[ns]

    def _headers(self) -> dict:
        from . import azure_service
        tok = self.credential.get_token(f"{azure_service._ARM}/.default").token
        return {"Authorization": f"Bearer {tok}"}

    def _url(self, suffix: str = "") -> str:
        from . import azure_service
        return f"{azure_service._ARM}{self.resource_id}{suffix}?api-version={self.api}"

    def read(self) -> str:
        import httpx
        r = httpx.get(self._url(), headers=self._headers(), timeout=30)
        r.raise_for_status()
        st = (((r.json() or {}).get("properties") or {}).get("state") or "").lower()
        if st == "ready":
            return "running"
        if st == "stopped":
            return "stopped"
        if st in ("stopping", "starting", "updating", "restarting", "provisioning"):
            return "transition"
        return st or "unknown"

    def _post(self, verb: str) -> None:
        import httpx
        r = httpx.post(self._url(f"/{verb}"), headers=self._headers(), timeout=60)
        if r.status_code not in (200, 202, 204):
            raise RuntimeError(f"Azure {verb} returned HTTP {r.status_code}: {r.text[:300]}")

    def request_stop(self) -> None:
        self._post("stop")

    def request_start(self) -> None:
        self._post("start")


async def _backend_for(db, row):
    """The backend for ``row``. Overridden by tests (``_BACKEND_FACTORY``)."""
    if _BACKEND_FACTORY is not None:
        return _BACKEND_FACTORY(row)
    from . import cloud_database_service as cds
    prov = cds._provision_job_for(db, row.id)
    tfv = ((prov.metadata_dict or {}).get("tf_variables") or {}) if prov else {}
    name = tfv.get("identifier") or f"clouddb-{row.id[:8]}"
    if row.cloud == "aws":
        return _RdsBackend(name, cds._row_region(row))
    if row.cloud == "gcp":
        project = tfv.get("project") or cds._cfg("gcp_project") or cds._cfg("gcp_project_id")
        if not project:
            raise RuntimeError("gcp_project is not configured")
        return _CloudSqlBackend(project, name)
    if row.cloud == "azure":
        from . import azure_service
        credential, _sub = await azure_service._ensure_creds()
        if not row.instance_id:
            raise RuntimeError("the database has no recorded Azure resource id")
        return _AzureFlexibleBackend(row.instance_id, credential)
    raise PowerUnsupported(f"no power backend for cloud {row.cloud!r}")


#: Test seam: ``callable(row) -> backend``. None in production.
_BACKEND_FACTORY = None


async def drive(backend, action: str, *, call, timeout_s: float = POWER_TIMEOUT_S,
                interval_s: float = POLL_INTERVAL_S, on_wait=None) -> str:
    """Bring ``backend`` to the state ``action`` asks for and return it.

    Reads first, so a retry after a dead job -- or a stop of an instance the cloud has
    already stopped -- issues nothing. An instance mid-transition (say, RDS starting
    itself back up on day 7) is waited out before the request, because both clouds
    reject a stop of an instance that is not yet available.
    """
    want = "stopped" if action == "stop" else "running"
    deadline = time.monotonic() + timeout_s
    requested = False
    while True:
        state = await call(backend.read)
        if state == want:
            return state
        if state not in ("running", "stopped", "transition"):
            raise RuntimeError(f"instance is in state {state!r}; refusing to {action} it")
        if state != "transition" and not requested:
            await call(backend.request_stop if action == "stop" else backend.request_start)
            requested = True
        if time.monotonic() >= deadline:
            raise RuntimeError(f"the {action} did not finish within {int(timeout_s)}s "
                               f"(last state {state!r})")
        if on_wait:
            on_wait(state)
        await asyncio.sleep(interval_s)


# ── the job ──────────────────────────────────────────────────────────────────

async def run_power(db, *, db_id: str, job_id: str, action: str) -> None:
    """Worker body of a ``clouddb_power`` job. Owns its own set_completed / set_failed
    and does not raise, like every other runner the worker dispatches to."""
    from ..database import CloudDatabase
    from . import job_service
    from .cloud_database_service import _to_thread

    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if row is None:
        job_service.set_failed(db, job_id, f"database {db_id} not found")
        return
    job_service.set_running(db, job_id)
    state = read_state(row)
    prior = state.get("prior_status") or (STATUS_STOPPED if action == "start"
                                          else STATUS_RUNNING)
    verb = "Stopping" if action == "stop" else "Starting"
    try:
        if action not in ACTIONS:
            raise PowerUnsupported(f"unknown power action {action!r}")
        reason = unsupported_reason(row)
        if reason:
            raise PowerUnsupported(reason)
        job_service.update_progress(db, job_id, 10, f"{verb} the database instance…")
        backend = await _backend_for(db, row)
        await drive(backend, action, call=_to_thread,
                    on_wait=lambda st: job_service.update_progress(
                        db, job_id, 50, f"{verb} the database instance… ({st})"))
    except Exception as exc:  # noqa: BLE001 - recorded on the row and the job
        db.rollback()
        row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
        if row is not None:
            st = read_state(row)
            st.update(last_error=str(exc)[:500], job_id=job_id, action=action)
            _write_state(row, st)
            if (row.status or "") in TRANSITIONAL:
                row.status = prior
            db.commit()
        job_service.set_failed(db, job_id, f"database {action} failed: {exc}")
        logger.warning("clouddb power %s db_id=%s failed: %s", action, db_id, exc)
        return

    db.refresh(row)
    st = read_state(row)
    st.pop("last_error", None)
    st.update(job_id=job_id, action=action)
    if action == "stop":
        st.update(state="stopped", stopped_at=_now().isoformat(),
                  resume_status=STATUS_RUNNING)
        row.status = STATUS_STOPPED
    else:
        st.update(state="running", started_at=_now().isoformat())
        st.pop("stopped_at", None)
        row.status = st.get("resume_status") or STATUS_RUNNING
    st.pop("prior_status", None)
    _write_state(row, st)
    db.commit()
    job_service.set_completed(db, job_id, {"db_id": db_id, "action": action,
                                           "status": row.status})
    logger.info("clouddb power %s db_id=%s -> %s", action, db_id, row.status)


async def ensure_running_for_teardown(db, row, job_id: str) -> Optional[str]:
    """Start a stopped database before a decommission, where its cloud needs that.

    GCP and Azure destroy a database's CHILDREN (Cloud SQL databases and users, Flexible
    Server databases and configurations) through the instance, and both refuse those
    calls on a stopped instance -- the destroy fails halfway and the row lands ``failed``
    with the instance still there. AWS deletes a stopped RDS instance as-is, so it is
    left stopped (and is cheaper for it).

    Returns a warning string when the start failed (the destroy is still attempted and
    its own error is the one that matters), None otherwise. Reads ``power_state`` rather
    than the status, because ``start_decommission`` has already overwritten that.
    """
    from . import job_service
    from .cloud_database_service import _to_thread
    if read_state(row).get("state") != "stopped" or row.cloud not in ("gcp", "azure"):
        return None
    if unsupported_reason(row):
        return None
    job_service.update_progress(db, job_id, 55,
                                "Starting the stopped instance so it can be destroyed…")
    try:
        backend = await _backend_for(db, row)
        await drive(backend, "start", call=_to_thread)
    except Exception as exc:  # noqa: BLE001 - reported; the destroy still runs
        logger.warning("clouddb decommission: start-before-destroy for %s failed: %s",
                       row.id, exc)
        return f"starting the stopped instance before destroy failed: {exc}"
    st = read_state(row)
    st.update(state="running", started_at=_now().isoformat())
    st.pop("stopped_at", None)
    _write_state(row, st)
    db.commit()
    return None
