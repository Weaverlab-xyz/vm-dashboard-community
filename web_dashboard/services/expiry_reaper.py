"""The auto-delete sweep, and the operator mutations behind the Extend control.

Answers the *when* and *what happened*; ``services/expiry_policy.py`` owns the *may I*.
The split mirrors ``ephemeral_gc`` (sweep) over ``ephemeral_secrets`` (pure predicate).

**This module contains no deletion code.** A sweep enumerates, applies
``expiry_policy.reap_target``, and *reports* — deliberately, so the observe-only release
cannot destroy anything by configuration accident. Enqueueing the destroy jobs is a
separate, small change whose whole review surface is "does it destroy exactly the right
thing, at most once."

How a pass reaches the worker, and why:

    main._expiry_sweeper_loop  ──►  enqueue_sweep_if_due()   [app, gunicorn -w 2]
                                        └─ creates ONE `expiry_sweep` job
    jobs_worker._claim_one     ──►  run()                    [worker, replicas: 3]

The loop only *enqueues*. ``_claim_one``'s ``UPDATE ... WHERE status='pending'`` rowcount
is the lock that makes the pass itself single-flight — the idiom the queue already relies
on, portable across SQLite and PostgreSQL, and correct no matter how many app workers or
worker replicas are running. Routing through the queue also means a pass gets a Job row
on /jobs, per-resource Live Output, mid-pass cancel, and stale-job reconcile for free.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import CloudDatabase, Job, K8sCluster
from . import expiry_policy, job_service

logger = logging.getLogger(__name__)

SWEEP_JOB_TYPE = "expiry_sweep"

_LAST_SWEEP_KEY = "resource_expiry_last_sweep"
_ARMED_AT_KEY = "resource_expiry_armed_at"

# Transaction-advisory-lock id serializing the ENQUEUE side across processes. Distinct
# from init_db's 20260101 and the audit chain's 20260102 — sharing either would wedge
# schema init or audit appends against a sweep. No-op on SQLite, where the active-job
# check alone suffices (and a duplicate would only produce a second no-op pass).
_ENQUEUE_LOCK_ID = 20260103

# Actor recorded on sweep-initiated work. A non-login name, so `created_by` on a job and
# `username` in the audit log both read unambiguously as machine action and stay
# separable in an audit query.
REAPER_ACTOR = "expiry-reaper"


def _cs():
    from . import config_service
    return config_service


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Persisted last-pass summary ───────────────────────────────────────────────

def get_last_sweep_result() -> dict:
    """The most recent pass summary, for the Settings panel and /api/expiry/last-sweep.

    Mirrors ``cloud_identity_sweeper_service.get_last_sweep_result``, including its
    never-run and corrupted fallbacks: an operator reading "corrupted" learns more than
    one staring at an empty panel.
    """
    try:
        raw = _cs().get(_LAST_SWEEP_KEY, "") or ""
    except Exception:                                  # pragma: no cover - defensive
        return {"never_run": True}
    if not raw:
        return {"never_run": True}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"corrupted": True, "raw_preview": raw[:200]}


def _persist_result(result: dict) -> None:
    """Best-effort save. A failure here is logged, not raised — the pass already did
    its work, and losing the report must not fail the job."""
    try:
        _cs().set(_LAST_SWEEP_KEY, json.dumps(result, sort_keys=True, default=str))
    except Exception:
        logger.exception("failed to persist %s", _LAST_SWEEP_KEY)


def _armed_at() -> Optional[datetime]:
    raw = ""
    try:
        raw = _cs().get(_ARMED_AT_KEY, "") or ""
    except Exception:                                  # pragma: no cover - defensive
        pass
    if not raw:
        return None
    ts = expiry_policy.parse_ts(raw)
    if ts is None:
        return None
    return datetime.utcfromtimestamp(ts)


def _arm(db: Session, now: datetime) -> datetime:
    """Record the first pass that saw the feature enabled, and audit it.

    The arming delay is the guaranteed gap between "an operator flipped the toggle" and
    "something could be destroyed", with at least one sweep report on disk in between.
    ``ARM_DELAY_MINUTES`` is a module constant precisely so this window cannot be
    shortened from the Settings page.
    """
    try:
        _cs().set(_ARMED_AT_KEY, now.isoformat())
    except Exception:
        logger.exception("failed to persist %s", _ARMED_AT_KEY)
        return now
    try:
        job_service.log_audit(
            db, REAPER_ACTOR, "resource_expiry_armed",
            details={"armed_at": now.isoformat(),
                     "arm_delay_minutes": expiry_policy.ARM_DELAY_MINUTES},
        )
    except Exception:
        logger.warning("could not audit resource_expiry_armed", exc_info=True)
    logger.info("auto-delete timer armed at %s; no resource may be reaped for %d minutes",
                now.isoformat(), expiry_policy.ARM_DELAY_MINUTES)
    return now


# ── Enqueue side (runs in the app) ────────────────────────────────────────────

def enqueue_sweep_if_due(db: Session) -> Optional[str]:
    """Create one ``expiry_sweep`` job unless a pass is already queued or running.

    Returns the new job id, or None when nothing was enqueued. Called on a timer by
    ``main._expiry_sweeper_loop`` and on demand by ``POST /api/expiry/sweep``.

    Two layers keep this to one pass at a time: the advisory lock serializes concurrent
    callers on PostgreSQL (both gunicorn workers hitting the same tick), and the
    active-job check is what actually decides. On SQLite the check stands alone, which is
    fine — a duplicate would claim, find the same work, and report it twice.
    """
    if not expiry_policy.enabled():
        return None
    try:
        from ..database import _is_sqlite
        if not _is_sqlite:
            db.execute(text("SELECT pg_advisory_xact_lock(:i)"), {"i": _ENQUEUE_LOCK_ID})
        existing = (
            db.query(Job.id)
            .filter(Job.job_type == SWEEP_JOB_TYPE,
                    Job.status.in_(job_service.ACTIVE_STATUSES))
            .first()
        )
        if existing:
            return None
        # create_job commits, which ends the transaction and releases the lock.
        job = job_service.create_job(db, job_type=SWEEP_JOB_TYPE, created_by="system")
        return job.id
    except Exception:
        logger.warning("could not enqueue an auto-delete sweep", exc_info=True)
        db.rollback()
        return None


# ── Worker entry point ────────────────────────────────────────────────────────

async def run(db: Session, *, job_id: str, meta: dict) -> None:
    """Run one sweep as a claimed job. Owns its own completion, like every other
    service the dispatcher calls, and never raises."""
    try:
        result = sweep_once(db, job_id=job_id)
    except Exception as exc:                           # noqa: BLE001
        logger.exception("auto-delete sweep failed")
        job_service.set_failed(db, job_id, f"auto-delete sweep failed: {exc}"[:500])
        return

    if result.get("skipped"):
        summary = f"Sweep skipped: {result['skipped']}"
    else:
        summary = (f"Scanned {result.get('scanned', 0)}, "
                   f"past expiry {result.get('due', 0)}, "
                   f"deleted {result.get('reaped', 0)}"
                   + (" (report only)" if result.get("dry_run") else ""))
    job_service.update_progress(db, job_id, 100, summary)
    job_service.set_completed(db, job_id, {"sweep": result})


# ── The sweep ─────────────────────────────────────────────────────────────────

def sweep_once(db: Session, *, job_id: Optional[str] = None) -> dict:
    """One pass: enumerate the inventory, find what is past its expiry, report it.

    Enumeration is ``inventory_service.collect(db)`` — no new query code. That function
    already normalizes every kind to one row shape, already owns the ``job:`` /
    ``clouddb:`` / ``k8s:`` id scheme, and already excludes destroyed and deleted rows.
    Once every 30 minutes it costs nothing the /inventory page doesn't already pay.

    Reporting only, by construction: this module has no enqueue path. ``reaped`` is
    therefore always 0 here, and is kept in the summary so the shape doesn't change when
    deletion lands.
    """
    from . import inventory_service

    started = _utcnow()
    if not expiry_policy.enabled():
        result = {"started_at": started.isoformat(), "ended_at": started.isoformat(),
                  "skipped": "resource_expiry_enabled is off"}
        _persist_result(result)
        return result

    # First pass with the feature on starts the arming clock and reaps nothing.
    armed_at = _armed_at()
    if armed_at is None:
        armed_at = _arm(db, started)
    armed_ts = armed_at.timestamp()
    now_ts = started.timestamp()
    is_armed = expiry_policy.armed(armed_ts, now_ts)

    grace = expiry_policy.grace_minutes()
    items = inventory_service.collect(db)

    targets, skipped_reasons = [], {}
    for item in items:
        if not item.get("expires_at"):
            continue                                   # no timer → nothing to report
        target = expiry_policy.reap_target(
            item, now_ts=now_ts, grace_min=grace, armed_at_ts=armed_ts)
        if target is None:
            reason = _skip_reason(item, now_ts=now_ts, grace=grace, is_armed=is_armed)
            if reason:
                skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
            continue
        targets.append(target)

    # Oldest-overdue first, so a capped pass acts on the longest-expired resources and
    # the report reads in the order an operator would triage.
    targets.sort(key=lambda t: t["overdue_s"], reverse=True)
    cap = expiry_policy.max_per_pass()
    capped = len(targets) > cap
    if capped:
        targets = targets[:cap]

    ended = _utcnow()
    result = {
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "duration_seconds": round((ended - started).total_seconds(), 2),
        "dry_run": expiry_policy.dry_run(),
        "enforce": expiry_policy.enforce(),
        "armed": is_armed,
        "armed_at": armed_at.isoformat(),
        "scanned": len(items),
        "due": len(targets),
        # Always 0 in this release: nothing in this module deletes.
        "reaped": 0,
        "skipped": 0,
        "failed": 0,
        "capped": capped,
        "targets": targets,
        "skipped_reasons": skipped_reasons,
    }
    _persist_result(result)

    if job_id and targets:
        job_service.append_job_log(
            db, job_id, f"{len(targets)} resource(s) past their auto-delete expiry:")
        for t in targets:
            job_service.append_job_log(
                db, job_id,
                f"  {t['kind']}/{t['cloud']} {t['name']} — expired "
                f"{t['overdue_s'] // 60}m ago (report only; deletion is not enabled "
                f"in this build)")

    if targets:
        # Audited only when there is something to say. A hash-chain entry every 30
        # minutes forever would bury the entries that matter.
        details = {k: result[k] for k in
                   ("scanned", "due", "reaped", "failed", "capped",
                    "dry_run", "enforce", "armed")}
        details["job_id"] = job_id
        try:
            job_service.log_audit(
                db, REAPER_ACTOR, "resource_expiry_sweep", details=details,
            )
        except Exception:
            logger.warning("could not audit resource_expiry_sweep", exc_info=True)
        logger.info("auto-delete sweep: %d of %d resource(s) past expiry (report only)",
                    len(targets), len(items))

    return result


def _skip_reason(item: dict, *, now_ts: float, grace: int, is_armed: bool) -> Optional[str]:
    """Why a timered resource wasn't a target, for the report's rollup. None when the
    answer is simply "not expired yet", which isn't worth counting."""
    if not is_armed:
        return "arming delay has not elapsed"
    capable, _ = expiry_policy.ttl_capable(item)
    if not capable:
        return "not eligible for auto-delete"
    if expiry_policy.is_exempt(item):
        return "workgroup is exempt"
    if not expiry_policy.state_is_idle(item):
        return f"state {item.get('state') or 'unknown'!r} is not idle"
    expires_ts = expiry_policy.parse_ts(item.get("expires_at"))
    if not expires_ts:
        return "expiry could not be read"
    if expires_ts > now_ts:
        return None                                    # simply not due
    return "within the grace period"


# ── Operator mutations (the Extend control) ───────────────────────────────────

class ExpiryError(Exception):
    """A refused expiry change, with an operator-facing explanation."""


def _resolve_row(db: Session, inv_id: str):
    """``(row, created_at, current_expiry)`` for an inventory id, or None.

    The id scheme is inventory_service's, so this stays the one place that knows a
    ``job:`` prefix means the deploy Job row carries the timer.
    """
    prefix, _, rid = (inv_id or "").partition(":")
    if not rid:
        return None
    if prefix == "job":
        row = db.query(Job).filter(Job.id == rid).first()
    elif prefix == "clouddb":
        row = db.query(CloudDatabase).filter(CloudDatabase.id == rid).first()
    elif prefix == "k8s":
        row = db.query(K8sCluster).filter(K8sCluster.id == rid).first()
    else:
        return None
    if row is None:
        return None
    return row, row.created_at, row.expires_at


def set_expiry(db: Session, items: list, *, extend_hours=None, absolute=None,
               never: bool = False, is_admin: bool = False, actor: str) -> dict:
    """Apply one expiry change to each of ``items`` (RBAC-filtered inventory dicts).

    Returns ``{"updated": [...], "failed": [...]}``. A per-item failure is reported, not
    raised, so one ineligible row in a selection doesn't discard the rest.

    Callers must have already filtered ``items`` through ``inventory_service.visible_to``
    — that is what stops a client naming a resource it cannot see.
    """
    updated, failed = [], []
    for item in items:
        inv_id = item.get("id")
        try:
            capable, why = expiry_policy.ttl_capable(item)
            if not capable:
                raise ExpiryError(why or "this resource cannot carry an auto-delete timer")
            resolved = _resolve_row(db, inv_id)
            if resolved is None:
                raise ExpiryError("no longer in the inventory — refresh and try again")
            row, created_at, current = resolved
            new_expiry, clamped = expiry_policy.resolve_expiry(
                created_at=created_at, current=current, extend_hours_req=extend_hours,
                absolute=absolute, never=never, is_admin=is_admin)
            previous = row.expires_at
            row.expires_at = new_expiry
            # A moved expiry earns a fresh warning: the old one described a deadline
            # that no longer exists.
            row.expiry_warned_at = None
            db.commit()
        except ExpiryError as exc:
            db.rollback()
            failed.append({"id": inv_id, "name": item.get("name"), "error": str(exc)})
            continue
        except ValueError as exc:                      # policy refusal from resolve_expiry
            db.rollback()
            failed.append({"id": inv_id, "name": item.get("name"), "error": str(exc)})
            continue
        except Exception as exc:                       # noqa: BLE001
            db.rollback()
            logger.warning("could not set expiry on %s", inv_id, exc_info=True)
            failed.append({"id": inv_id, "name": item.get("name"), "error": str(exc)[:200]})
            continue

        updated.append({
            "id": inv_id,
            "name": item.get("name"),
            "expires_at": new_expiry.isoformat() if new_expiry else None,
            "previous": previous.isoformat() if previous else None,
            "clamped": clamped,
        })
        # Clearing a timer gets its own action name so the one event an auditor cares
        # about is greppable without parsing details.
        try:
            job_service.log_audit(
                db, actor,
                "resource_expiry_cleared" if new_expiry is None else "resource_expiry_set",
                target_vm=item.get("name"),
                details={"inventory_id": inv_id, "kind": item.get("kind"),
                         "cloud": item.get("cloud"),
                         "previous": previous.isoformat() if previous else None,
                         "expires_at": new_expiry.isoformat() if new_expiry else None,
                         "clamped": clamped},
            )
        except Exception:
            logger.warning("could not audit expiry change for %s", inv_id, exc_info=True)

    return {"updated": updated, "failed": failed}


def status() -> dict:
    """Feature state for the clients — thresholds, arming, and the floors, so the UI
    can explain a clamp instead of just showing one."""
    armed_at = _armed_at()
    return {
        "enabled": expiry_policy.enabled(),
        "enforce": expiry_policy.enforce(),
        "dry_run": expiry_policy.dry_run(),
        "default_hours": expiry_policy.default_hours(),
        "extend_hours": expiry_policy.extend_hours(),
        "max_total_hours": expiry_policy.max_total_hours(),
        "warn_hours": expiry_policy.warn_hours(),
        "allow_never": expiry_policy.allow_never(),
        "armed": expiry_policy.armed(armed_at.timestamp() if armed_at else None,
                                     _utcnow().timestamp()),
        "armed_at": armed_at.isoformat() if armed_at else None,
        "arms_at": ((armed_at + timedelta(minutes=expiry_policy.ARM_DELAY_MINUTES))
                    .isoformat() if armed_at else None),
        "floors": {
            "min_ttl_minutes": expiry_policy.MIN_TTL_MINUTES_FLOOR,
            "grace_minutes": expiry_policy.REAP_GRACE_MIN_FLOOR,
            "arm_delay_minutes": expiry_policy.ARM_DELAY_MINUTES,
            "max_per_pass_ceiling": expiry_policy.MAX_PER_PASS_CEILING,
        },
        # Always false in this release — nothing here deletes. Surfaced so the UI can
        # say "report only" honestly rather than inferring it from the flags.
        "deletion_available": False,
    }
