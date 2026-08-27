"""The auto-delete sweep, and the operator mutations behind the Extend control.

Answers the *when* and *what happened*; ``services/expiry_policy.py`` owns the *may I*.
The split mirrors ``ephemeral_gc`` (sweep) over ``ephemeral_secrets`` (pure predicate).

A sweep enumerates the inventory, applies ``expiry_policy.reap_target``, reports, and —
when every gate is open — destroys. It never implements teardown itself: for a VM it
enqueues the identical job row the cloud's own DELETE endpoint creates, and for a database
or cluster it calls the same ``start_decommission``. That is what keeps this small, and it
means the PRA tunnel, vault and Password Safe cleanup all happen exactly as they do when a
human presses Destroy.

**Four gates stand between overdue and destroyed**, and the pass reports its full target
list whichever of them is shut, so an operator always sees what *would* happen:

  1. ``resource_expiry_enabled``;
  2. a stamped ``expires_at`` — NULL means never, which is why enabling the feature on an
     existing fleet acts on nothing;
  3. the feature armed for ``ARM_DELAY_MINUTES``;
  4. ``enforce`` on, ``dry_run`` off, and *enforcement* armed for its own
     ``ARM_DELAY_MINUTES`` — so turning report-only off cannot act on a backlog that
     accumulated while it was on.

At-most-once is structural: enqueueing a destroy clears the resource's ``expires_at`` in
the same commit, which removes it from ``expired()``'s view entirely. No new column, no
filtering on ``extra_data``, and for databases and clusters it is doubled by
``start_decommission``'s own in-flight check.

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

from ..database import CloudDatabase, Job, JobLog, K8sCluster, PovEnvironment
from . import expiry_policy, job_service

logger = logging.getLogger(__name__)

SWEEP_JOB_TYPE = "expiry_sweep"

_LAST_SWEEP_KEY = "resource_expiry_last_sweep"
_ARMED_AT_KEY = "resource_expiry_armed_at"
# When a pass first observed enforcement live (enforce on AND dry-run off). Deletion waits
# ARM_DELAY_MINUTES past this, so unchecking report-only can't act on a backlog the
# operator hasn't had a chance to review. See expiry_policy.deletion_active.
_ENFORCE_SINCE_KEY = "resource_expiry_enforce_since"

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


def _enforce_since() -> Optional[datetime]:
    raw = ""
    try:
        raw = _cs().get(_ENFORCE_SINCE_KEY, "") or ""
    except Exception:                                  # pragma: no cover - defensive
        pass
    ts = expiry_policy.parse_ts(raw) if raw else None
    return datetime.utcfromtimestamp(ts) if ts else None


def _note_enforcement(db: Session, now: datetime) -> Optional[datetime]:
    """Keep ``resource_expiry_enforce_since`` in step with the enforcement flags.

    Set on the first pass that observes enforcement live; CLEARED the moment it isn't.
    Clearing matters: an operator who turns report-only back on has withdrawn consent to
    delete, so turning it off again must restart the full arming delay rather than
    resuming a clock that kept running while the feature was inert.
    """
    live = expiry_policy.enforce() and not expiry_policy.dry_run()
    existing = _enforce_since()
    if live and existing is None:
        try:
            _cs().set(_ENFORCE_SINCE_KEY, now.isoformat())
        except Exception:
            logger.exception("failed to persist %s", _ENFORCE_SINCE_KEY)
            return None
        try:
            job_service.log_audit(
                db, REAPER_ACTOR, "resource_expiry_enforce_armed",
                details={"enforce_since": now.isoformat(),
                         "arm_delay_minutes": expiry_policy.ARM_DELAY_MINUTES},
            )
        except Exception:
            logger.warning("could not audit resource_expiry_enforce_armed", exc_info=True)
        logger.warning("auto-delete ENFORCEMENT armed at %s; resources past their expiry "
                       "will be destroyed from %d minutes hence",
                       now.isoformat(), expiry_policy.ARM_DELAY_MINUTES)
        return now
    if not live and existing is not None:
        try:
            _cs().set(_ENFORCE_SINCE_KEY, "")
        except Exception:
            logger.exception("failed to clear %s", _ENFORCE_SINCE_KEY)
        logger.info("auto-delete enforcement withdrawn; the arming delay will restart")
        return None
    return existing


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

def enqueue_sweep_if_due(db: Session, *, min_gap_seconds: Optional[int] = None) -> Optional[str]:
    """Create one ``expiry_sweep`` job unless a pass is already active or just ran.

    Returns the new job id, or None when nothing was enqueued. Called on a timer by
    ``main._expiry_sweeper_loop`` and on demand by ``POST /api/expiry/sweep``.

    THREE layers keep this to one pass per tick, and the third is not redundant:

      * the advisory lock serializes concurrent callers on PostgreSQL — both gunicorn
        workers reaching the same tick;
      * the **active-job** check refuses while a pass is queued, pending or running;
      * the **recency** check refuses while a pass merely *finished* moments ago.

    The recency check exists because the active-job check alone provably does not hold.
    ``ACTIVE_STATUSES`` is a liveness test, and a sweep with nothing to reap completes in
    well under a second: with ``jobs_worker.POLL_INTERVAL`` at 2s across three replicas the
    mean claim latency is ~0.7s, so whenever a replica polls inside the ~0.4s that separates
    the two app workers' ticks, the first row is already ``completed`` before the second
    worker looks — and a duplicate is created. Measured on the live install before this
    guard existed: 5 of 55 rows were pairs 0.13–0.4s apart. **A liveness-based dedupe cannot
    hold when the work is instantaneous; it needs a recency term.** Worth remembering before
    writing the next "is one already running?" check in this codebase.

    Harmless while a pass only reports. Under ``enforce`` two concurrent passes are two
    destroy enqueues aimed at the same resource, which is why this is fixed here rather
    than left to the queue.

    ``min_gap_seconds`` defaults to :func:`expiry_policy.sweep_min_gap_seconds` (half the
    interval). Pass 0 to skip the recency check — the operator-facing force-sweep endpoint
    does, because a human who just pressed the button means now, and only the timer path
    ever fires twice. Defaulting to the guard rather than to 0 keeps a future caller from
    reintroducing the duplicate by forgetting a kwarg.
    """
    if not expiry_policy.enabled():
        return None
    if min_gap_seconds is None:
        try:
            min_gap_seconds = expiry_policy.sweep_min_gap_seconds()
        except Exception:                              # pragma: no cover - defensive
            min_gap_seconds = 60
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
        if min_gap_seconds > 0:
            # Any status: a pass that already ran this tick covered this moment's work
            # whether it completed, failed or was cancelled. Naive UTC, because
            # `Job.created_at` is `datetime.utcnow()` and mixing an aware value into the
            # comparison would let the session TimeZone decide the cutoff.
            floor = _utcnow() - timedelta(seconds=min_gap_seconds)
            recent = (
                db.query(Job.id)
                .filter(Job.job_type == SWEEP_JOB_TYPE, Job.created_at >= floor)
                .first()
            )
            if recent:
                return None
        # create_job commits, which ends the transaction and releases the lock.
        job = job_service.create_job(db, job_type=SWEEP_JOB_TYPE, created_by="system")
        return job.id
    except Exception:
        logger.warning("could not enqueue an auto-delete sweep", exc_info=True)
        db.rollback()
        return None


# ── Housekeeping ──────────────────────────────────────────────────────────────

# Rows deleted per prune call. The first pass after retention is configured on a
# long-lived install could face thousands; a bounded batch keeps that off the sweep's
# critical path, and at 48 rows/day against a prune every interval any backlog drains
# within a few passes anyway.
_PRUNE_BATCH = 500


def prune_sweep_history(db: Session) -> int:
    """Drop *completed* sweep rows past the retention window, and their Live Output.

    Returns rows deleted. Never raises — losing a prune must not fail the pass that
    called it.

    **``Job.job_type == SWEEP_JOB_TYPE`` is the load-bearing filter in this function, and
    the reason this is not a general "prune old jobs" helper.** A cloud VM has no inventory
    table: its deploy Job row IS its record of existence, which is why ``job:<id>`` is
    already its inventory id and why ``expires_at`` rides on that row. Deleting job rows by
    age would therefore delete *VMs from the inventory* while leaving them running and
    billing in the cloud — the exact orphan this whole feature exists to prevent. A sweep
    row is the one job type that references no resource at all, so it is the one that can
    be thrown away.

    Only ``completed`` rows expire. A failed or cancelled pass is evidence and stays until
    an operator has seen it, matching ``notification_service.prune``; the /jobs filter
    hides exactly the same set, so a failure stays visible in both places.

    ``job_logs`` is deleted first and explicitly: it carries no foreign key to ``jobs``
    (see ``database.JobLog``), so nothing cascades and the rows would otherwise be
    unreachable orphans keyed to a job id that no longer exists.
    """
    days = expiry_policy.sweep_retention_days()
    if not days:
        return 0
    cutoff = _utcnow() - timedelta(days=days)
    try:
        ids = [r[0] for r in (
            db.query(Job.id)
            .filter(Job.job_type == SWEEP_JOB_TYPE,
                    Job.status == "completed",
                    Job.created_at < cutoff)
            .limit(_PRUNE_BATCH)
            .all()
        )]
        if not ids:
            return 0
        db.query(JobLog).filter(JobLog.job_id.in_(ids)).delete(synchronize_session=False)
        n = db.query(Job).filter(Job.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        logger.info("pruned %d completed %s row(s) older than %d day(s)",
                    n, SWEEP_JOB_TYPE, days)
        return int(n or 0)
    except Exception:                                  # noqa: BLE001
        logger.warning("could not prune %s history", SWEEP_JOB_TYPE, exc_info=True)
        db.rollback()
        return 0


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
                   f"destroyed {result.get('reaped', 0)}")
        if result.get("failed"):
            summary += f", {result['failed']} failed"
        if result.get("warned"):
            summary += f", {result['warned']} warned"
        if not result.get("deleting"):
            summary += " (report only — nothing deleted)"
    job_service.update_progress(db, job_id, 100, summary)
    job_service.set_completed(db, job_id, {"sweep": result})

    # The pass that writes the rows is the pass that prunes them — the same shape as
    # notification_service's drain loop, and it needs no second timer. Ordered after
    # set_completed so a prune failure cannot leave the job looking unfinished, and
    # harmless to this row: it was created seconds ago, far inside the retention window.
    prune_sweep_history(db)


# ── Outbound notification ─────────────────────────────────────────────────────

def _notify(db: Session, event_type: str, *, title: str, body: str, target: dict,
            url: str = "", fields: Optional[dict] = None,
            dedupe_bucket: str = "") -> int:
    """Queue one notification about a resource. Returns rows queued (0 when off).

    Deliberately thin: it only builds the event and hands it to the outbox, which
    INSERTs and returns. No HTTP happens anywhere on this path — the worker's drain
    loop does the sending, so a dead webhook can never slow down or fail a sweep.

    Guarded even though ``emit_safe`` is itself guarded: building the event reads a
    target dict assembled elsewhere, and a resource is already destroyed by the time
    the reaped notification is built. Failing to announce it must not turn a completed
    reap into an error.
    """
    try:
        from . import notification_service, notify_policy
        event = notify_policy.NotificationEvent(
            event_type=event_type, title=title, body=body,
            resource_id=target.get("id") or "",
            resource_kind=target.get("kind") or "",
            resource_name=target.get("name") or "",
            cloud=target.get("cloud") or "",
            region=target.get("region") or "",
            workgroup=target.get("workgroup") or "",
            url=url, dedupe_bucket=dedupe_bucket,
            fields={k: v for k, v in (fields or {}).items() if v not in (None, "")},
        )
        return notification_service.emit_safe(db, event)
    except Exception:                                  # noqa: BLE001
        logger.warning("could not queue a %s notification for %s", event_type,
                       target.get("id"), exc_info=True)
        return 0


def _remaining_label(seconds: float) -> str:
    """"about 3 days" / "about 4h". A POV ladder starts a week out, and "about 168h" is a
    number nobody converts in their head."""
    if seconds >= 36 * 3600:
        days = max(1, int(round(seconds / 86400)))
        return "about %d day%s" % (days, "s" if days != 1 else "")
    return "about %dh" % max(1, int(seconds // 3600))


def _warn_expiring(db: Session, items: list, *, now_ts: float) -> int:
    """Warn about each resource heading into its auto-delete window.

    This is what the ``expiry_warned_at`` column was added for — its own docstring in
    database.py reserves it for "the server-side warning channels" — and until now
    nothing wrote it. The dashboard's client-side banner is unaffected: it is derived
    fresh on every poll and has its own per-browser dismissal.

    Fire-once is a write, not a hope: the stamp is committed here, so a second sweep
    (or a second worker) finds the latch set. It is stamped only *after* the outbox
    accepted the rows, so a pass that queued nothing — notifications off, no endpoints
    configured, storm-suppressed — leaves the resource still eligible to warn later.
    ``set_expiry`` clears the latch on an extend, which correctly re-warns against the
    new deadline.

    **A POV warns on a ladder instead of once.** One mail a day before a customer's lab
    disappears is a mail that gets read afterwards — the evaluation has been running for
    weeks and whoever created it has moved on. So a row carrying ``warned_stage_minutes``
    is driven by :func:`expiry_policy.next_warn_stage`, and the latch records the tightest
    rung already sent rather than a bare "warned". ``expiry_warned_at`` is stamped
    alongside it, so the two never disagree about whether anything was sent at all.
    """
    warn_seconds = expiry_policy.warn_hours() * 3600
    warned = 0
    for item in items:
        if not item.get("expires_at"):
            continue
        expires_ts = expiry_policy.parse_ts(item.get("expires_at"))
        if not expires_ts:
            continue
        remaining = expires_ts - now_ts
        # Already past expiry is the reaper's business, not the warner's — a "expires
        # soon" message about something being destroyed right now is just noise.
        if remaining <= 0:
            continue
        capable, _ = expiry_policy.ttl_capable(item)
        if not capable:
            continue                                   # nothing will auto-delete it anyway

        resolved = _resolve_row(db, item["id"])
        if resolved is None:
            continue
        row = resolved[0]

        # Which discipline a row follows is a property of the ROW, not of its kind: the
        # ladder needs somewhere to record the rung, and only a row with the latch column
        # has one. So a kind that grows the column later gets the ladder for free, and one
        # without it keeps warn-once — with no list of kinds here to fall out of step with
        # the schema.
        laddered = hasattr(row, "warned_stage_minutes")
        if laddered:
            stage = expiry_policy.next_warn_stage(
                remaining, getattr(row, "warned_stage_minutes", None))
            if stage is None:
                continue
        else:
            if remaining > warn_seconds:
                continue
            if getattr(row, "expiry_warned_at", None) is not None:
                continue
            stage = None

        queued = _notify(
            db, "resource.expiring",
            title=f"{item['name']} auto-deletes in {_remaining_label(remaining)}",
            body=("Its auto-delete timer expires soon. Extend it from the dashboard if "
                  "you still need it — otherwise it will be destroyed automatically."),
            target=item,
            url=item.get("detail_href") or "/inventory",
            fields={"Expires": item.get("expires_at"), "State": item.get("state")})
        if not queued:
            continue

        try:
            fresh = _resolve_row(db, item["id"])
            if fresh is not None:
                fresh[0].expiry_warned_at = _utcnow()
                if stage is not None:
                    fresh[0].warned_stage_minutes = stage
                db.commit()
                warned += 1
        except Exception:
            logger.warning("could not stamp expiry_warned_at for %s", item["id"],
                           exc_info=True)
            db.rollback()
    return warned


# ── Deletion ──────────────────────────────────────────────────────────────────

class ReapRefused(Exception):
    """This resource must not be reaped, with an operator-facing reason. Distinct from an
    unexpected error: a refusal is a decision the report should explain, not a fault."""


def _reap_vm(db: Session, target: dict) -> str:
    """Enqueue the destroy job for one expired VM and return its id.

    Rebuilds the *same* job row the cloud's own DELETE endpoint creates — same job_type,
    same metadata keys, from the deploy job's own recorded values. Deliberately not a
    reimplementation of teardown: the whole reason the reaper is small is that every
    teardown path, with its PRA tunnel and vault and Password Safe cleanup, already exists
    behind these job types.
    """
    deploy_job_id = target.get("job_id")
    if not deploy_job_id:
        raise ReapRefused("no deploy job recorded, so its teardown cannot be located")
    deploy_job = db.query(Job).filter(Job.id == deploy_job_id).first()
    if deploy_job is None:
        raise ReapRefused("its deploy job no longer exists")

    meta = deploy_job.metadata_dict or {}
    if meta.get("destroyed"):
        raise ReapRefused("already destroyed")

    destroy_type, payload = expiry_policy.build_destroy_metadata(
        deploy_job.job_type, meta, deploy_job_id)
    if destroy_type is None:
        raise ReapRefused(payload)                     # payload is the reason string

    destroy_job = job_service.create_job(
        db, job_type=destroy_type, created_by=REAPER_ACTOR,
        workgroup=deploy_job.workgroup, metadata=payload)

    # At-most-once, committed with the enqueue. Clearing expires_at removes the resource
    # from expired()'s view entirely, so the next pass cannot enqueue a second destroy —
    # the same trick as the existing `destroyed` marker, and portable (no new column, and
    # no filtering on extra_data). The breadcrumbs are read-only for one known row.
    fresh = db.query(Job).filter(Job.id == deploy_job_id).first()
    fresh.expires_at = None
    bc = fresh.metadata_dict or {}
    bc["expiry_reaped_at"] = _utcnow().isoformat()
    bc["expiry_reap_job_id"] = destroy_job.id
    fresh.metadata_dict = bc
    db.commit()
    return destroy_job.id


def _reap_row(db: Session, target: dict) -> str:
    """Start the teardown for one expired database, cluster or POV, returning the job id.

    Goes through the same ``start_decommission`` the DELETE endpoints call, which already
    refuses to start a second teardown while one is in flight and flips the row's status
    out of the reapable set — so idempotency here is doubled rather than assumed. For a
    POV it enqueues the same ``pov_env_destroy`` job that endpoint creates, and the
    ``expires_at`` clear below is what makes it at-most-once (the row also leaves the
    reapable state set the moment the job starts).
    """
    kind = target["kind"]
    rid = (target["id"].split(":", 1) + [""])[1]
    if not rid:
        raise ReapRefused("unrecognised inventory id")

    if kind == "database":
        from . import cloud_database_service
        out = cloud_database_service.start_decommission(db, rid, created_by=REAPER_ACTOR)
        model, jid = CloudDatabase, out.get("job_id")
    elif kind == "k8s":
        from . import k8s_service
        out = k8s_service.start_decommission(db, rid, created_by=REAPER_ACTOR)
        model, jid = K8sCluster, out.get("job_id")
    elif kind == "pov":
        # The identical job row DELETE /api/pov/managed/{id} creates. Going through the
        # queue rather than calling the teardown directly is what makes the share link,
        # the Entitle integrations, the Password Safe managed systems, the PRA jump items,
        # the Gateway and the broker agent all get reaped exactly as they do when a human
        # presses Destroy — this module never learns that ordering, and cannot get it wrong.
        env = db.query(PovEnvironment).filter(PovEnvironment.id == rid).first()
        if env is None:
            raise ReapRefused("the POV environment row is gone")
        job = job_service.create_job(
            db, job_type="pov_env_destroy", created_by=REAPER_ACTOR,
            workgroup=env.workgroup, metadata={"environment_id": env.id})
        model, jid = PovEnvironment, job.id
    else:                                              # pragma: no cover - guarded upstream
        raise ReapRefused(f"{kind} has no queued teardown")

    row = db.query(model).filter(model.id == rid).first()
    if row is not None:
        row.expires_at = None
        db.commit()
    return jid or ""


def _reap_one(db: Session, target: dict) -> dict:
    """Reap one target. Returns the target annotated with ``destroy_job_id`` or ``error``.

    Never raises. One resource that refuses or errors — a 403, a deploy job missing its
    region, a teardown already running — must not abandon the rest of the pass, the same
    way ``_reap_cloud_run_jobs`` counts a failed delete and moves on. A resource left
    un-reaped simply reappears next pass, or stays visible as overdue on /inventory with
    the reason on the report.
    """
    out = dict(target)
    try:
        if target["kind"] == "vm":
            out["destroy_job_id"] = _reap_vm(db, target)
        else:
            out["destroy_job_id"] = _reap_row(db, target)
    except ReapRefused as exc:
        db.rollback()
        out["error"] = str(exc)
        logger.info("auto-delete skipped %s (%s): %s", target["id"], target["name"], exc)
        return out
    except Exception as exc:                           # noqa: BLE001
        db.rollback()
        out["error"] = str(exc)[:200]
        logger.warning("auto-delete failed for %s (%s)", target["id"], target["name"],
                       exc_info=True)
        return out

    logger.warning("auto-delete DESTROYED %s %s/%s %r (expired %dm ago) via job %s",
                   target["kind"], target["cloud"], target["region"], target["name"],
                   target["overdue_s"] // 60, out["destroy_job_id"])
    try:
        job_service.log_audit(
            db, REAPER_ACTOR, "resource_expiry_reaped", target_vm=target["name"],
            details={"inventory_id": target["id"], "kind": target["kind"],
                     "cloud": target["cloud"], "region": target["region"],
                     "expires_at": target["expires_at"],
                     "overdue_seconds": target["overdue_s"],
                     "destroy_job_id": out["destroy_job_id"]},
        )
    except Exception:
        logger.warning("could not audit resource_expiry_reaped for %s", target["id"],
                       exc_info=True)

    # Queue the "we destroyed this" notification. INSERT-only and non-raising, like the
    # audit append above — the resource is already gone, so failing to announce it must
    # not turn a completed reap into an error. Note this fires on ENQUEUE of the
    # teardown, not its completion; the destroy job carries the rest of the story.
    _notify(db, "resource.reaped",
            title=f"Auto-deleted {target['name']} ({target['cloud']})",
            body=("Its auto-delete timer expired and the dashboard started its teardown. "
                  "This was not requested by a person."),
            target=target,
            url=f"/jobs/{out['destroy_job_id']}",
            fields={"Expired": target.get("expires_at"),
                    "Overdue": f"{target['overdue_s'] // 60}m",
                    "Destroy job": out["destroy_job_id"]})
    return out


# ── The sweep ─────────────────────────────────────────────────────────────────

def sweep_once(db: Session, *, job_id: Optional[str] = None) -> dict:
    """One pass: find what is past its expiry, and destroy it when policy allows.

    Enumeration is ``inventory_service.collect(db)`` — no new query code. That function
    already normalizes every kind to one row shape, already owns the ``job:`` /
    ``clouddb:`` / ``k8s:`` id scheme, and already excludes destroyed and deleted rows.
    Once every 30 minutes it costs nothing the /inventory page doesn't already pay.

    Four independent gates stand between a resource being overdue and being destroyed —
    the feature enabled, a stamped expiry (NULL means never), the feature armed, and
    enforcement armed with report-only off. When any of them is shut the pass still
    reports the full target list, so an operator always sees what *would* happen.
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

    # Enforcement carries its own arming delay, so unchecking report-only cannot act on a
    # backlog nobody has reviewed. Tracked here rather than read from the flags alone so
    # the clock starts from an observed pass.
    enforce_since = _note_enforcement(db, started)
    may_delete = is_armed and expiry_policy.deletion_active(
        enforce_since.timestamp() if enforce_since else None, now_ts)

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

    # Warn about what is heading INTO the window, before acting on what is already past
    # it. Independent of the arming and enforcement gates on purpose: an operator who has
    # not yet armed deletion still wants to know a timer is running down, and warning is
    # not destructive. Never raises.
    try:
        warned = _warn_expiring(db, items, now_ts=now_ts)
    except Exception:
        logger.warning("auto-delete expiry warnings failed", exc_info=True)
        db.rollback()
        warned = 0

    # Oldest-overdue first, so a capped pass acts on the longest-expired resources and the
    # report reads in the order an operator would triage.
    targets.sort(key=lambda t: t["overdue_s"], reverse=True)
    cap = expiry_policy.max_per_pass()
    due_total = len(targets)

    if job_id and targets:
        verb = "destroying" if may_delete else "past their auto-delete expiry"
        job_service.append_job_log(db, job_id, f"{due_total} resource(s) {verb}:")

    # Act. Each target is independent — see _reap_one, which never raises.
    #
    # The cap counts DELETIONS, not attempts. A resource that refuses — a deploy job with
    # no recorded region, say — destroys nothing, so charging it a slot would let one
    # permanently un-reapable resource starve the whole feature: it sorts oldest-first
    # forever, so with a small cap it would take the same slot on every pass and nothing
    # else would ever be reaped. Refusals are cheap (a metadata read, no cloud call), so
    # attempting them all costs nothing and keeps them visible in the report.
    reaped = failed = 0
    deferred = 0
    for t in targets:
        if not may_delete:
            t["would_delete"] = True
            if job_id:
                job_service.append_job_log(
                    db, job_id,
                    f"  {t['kind']}/{t['cloud']} {t['name']} — expired "
                    f"{t['overdue_s'] // 60}m ago; NOT deleted "
                    f"({_why_not_deleting(is_armed, enforce_since, now_ts)})")
            continue
        if reaped >= cap:
            t["deferred"] = True
            deferred += 1
            continue
        outcome = _reap_one(db, t)
        t.update(outcome)
        if t.get("error"):
            failed += 1
        else:
            reaped += 1
        if job_id:
            tail = (f"FAILED: {t['error']}" if t.get("error")
                    else f"destroy job {t['destroy_job_id']}")
            job_service.append_job_log(
                db, job_id,
                f"  {t['kind']}/{t['cloud']} {t['name']} — expired "
                f"{t['overdue_s'] // 60}m ago → {tail}")
    if deferred and job_id:
        job_service.append_job_log(
            db, job_id, f"  …and {deferred} more, deferred to the next sweep "
                        f"(max {cap} deletions per pass)")
    capped = deferred > 0

    ended = _utcnow()
    result = {
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "duration_seconds": round((ended - started).total_seconds(), 2),
        "dry_run": expiry_policy.dry_run(),
        "enforce": expiry_policy.enforce(),
        "armed": is_armed,
        "armed_at": armed_at.isoformat(),
        "enforce_since": enforce_since.isoformat() if enforce_since else None,
        # The one field that answers "did this pass have teeth" without the reader having
        # to combine four flags themselves.
        "deleting": may_delete,
        "scanned": len(items),
        # Everything past its expiry, not just what this pass acted on — an operator
        # reading "due 3" while 40 are overdue would draw exactly the wrong conclusion.
        "due": due_total,
        "reaped": reaped,
        "failed": failed,
        "deferred": deferred,
        "capped": capped,
        # Resources warned for the first time this pass. Zero on a steady-state estate —
        # each resource is warned once — so a non-zero value means new timers landed.
        "warned": warned,
        "targets": targets,
        "skipped_reasons": skipped_reasons,
    }
    _persist_result(result)

    if targets:
        # Audited only when there is something to say. A hash-chain entry every 30
        # minutes forever would bury the entries that matter.
        details = {k: result[k] for k in
                   ("scanned", "due", "reaped", "failed", "capped",
                    "warned", "dry_run", "enforce", "armed", "deleting")}
        details["job_id"] = job_id
        try:
            job_service.log_audit(
                db, REAPER_ACTOR, "resource_expiry_sweep", details=details,
            )
        except Exception:
            logger.warning("could not audit resource_expiry_sweep", exc_info=True)
        # warning, not info, on a pass that actually destroyed something: this is the one
        # log line that says infrastructure went away without a human asking.
        (logger.warning if reaped else logger.info)(
            "auto-delete sweep: %d of %d resource(s) past expiry, %d destroyed, %d failed"
            "%s", len(targets), len(items), reaped, failed,
            "" if may_delete else " (report only — nothing was deleted)")

    return result


def _why_not_deleting(is_armed: bool, enforce_since, now_ts: float) -> str:
    """The single reason this pass isn't destroying, for the Live Output line.

    Ordered from most-deliberate to most-transient so the operator reads the thing they'd
    actually have to change first.
    """
    if not expiry_policy.enforce():
        return "deletion is not enabled"
    if expiry_policy.dry_run():
        return "report-only mode"
    if not is_armed:
        return "the feature is still arming"
    if not enforce_since:
        return "enforcement was just enabled"
    mins = int((expiry_policy.ARM_DELAY_MINUTES * 60 - (now_ts - enforce_since.timestamp()))
               // 60)
    return f"enforcement arms in {max(1, mins)}m"


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

    ``hv:`` is absent deliberately, not by omission. A synced hypervisor VM has no
    dashboard row to stamp a timer on and no teardown to run, so there is nothing to
    resolve — ``expiry_policy.ttl_capable`` refuses it first, and this is the second of
    the two places that would have to agree before one could ever be reaped.
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
    elif prefix == "pov":
        row = db.query(PovEnvironment).filter(PovEnvironment.id == rid).first()
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
            # that no longer exists. The ladder latch goes with it, or an extend would
            # leave every rung tighter than the one already sent permanently silenced —
            # which is the whole reason `warned_stage_minutes` is a rung and not a bool.
            row.expiry_warned_at = None
            if hasattr(row, "warned_stage_minutes"):
                row.warned_stage_minutes = None
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
    enforce_since = _enforce_since()
    now = _utcnow().timestamp()
    return {
        "enabled": expiry_policy.enabled(),
        "enforce": expiry_policy.enforce(),
        "dry_run": expiry_policy.dry_run(),
        "default_hours": expiry_policy.default_hours(),
        "extend_hours": expiry_policy.extend_hours(),
        "max_total_hours": expiry_policy.max_total_hours(),
        "warn_hours": expiry_policy.warn_hours(),
        "allow_never": expiry_policy.allow_never(),
        "armed": expiry_policy.armed(armed_at.timestamp() if armed_at else None, now),
        "armed_at": armed_at.isoformat() if armed_at else None,
        "arms_at": ((armed_at + timedelta(minutes=expiry_policy.ARM_DELAY_MINUTES))
                    .isoformat() if armed_at else None),
        # Enforcement's own arming clock — what the UI needs to say "nothing will be
        # deleted before HH:MM" after an operator turns report-only off.
        "enforce_since": enforce_since.isoformat() if enforce_since else None,
        "enforce_arms_at": ((enforce_since
                             + timedelta(minutes=expiry_policy.ARM_DELAY_MINUTES))
                            .isoformat() if enforce_since else None),
        "floors": {
            "min_ttl_minutes": expiry_policy.MIN_TTL_MINUTES_FLOOR,
            "grace_minutes": expiry_policy.REAP_GRACE_MIN_FLOOR,
            "arm_delay_minutes": expiry_policy.ARM_DELAY_MINUTES,
            "max_per_pass_ceiling": expiry_policy.MAX_PER_PASS_CEILING,
        },
        # Whether this build can delete at all (it can), kept distinct from whether it
        # WILL right now — that's `deleting`, which folds in both arming clocks and the
        # two flags so the UI never has to combine them itself and get it wrong.
        "deletion_available": True,
        "deleting": expiry_policy.armed(armed_at.timestamp() if armed_at else None, now)
        and expiry_policy.deletion_active(
            enforce_since.timestamp() if enforce_since else None, now),
    }
