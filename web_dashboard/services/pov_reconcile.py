"""Reading POV environments back off the lab platform, so the dashboard's view stays true.

``PovEnvironment.runstate`` was, until this module, written in exactly three places: the
provision job, an explicit power action, and ``pov_env_service.refresh_vms`` — whose only
two callers are those same two paths. Nothing ever asked the platform again.

That is a real problem and not a cosmetic one, because of what *else* changes a POV's
runstate. ``suspend_on_idle`` — which ``docs/integrations/skytap.md`` calls the single
biggest lever on lab-platform spend — suspends an idle environment on the platform's own
timer, with nothing to tell this dashboard it happened. The row went on saying ``running``
indefinitely. And the POV page gates its Start/Suspend buttons on that value, so an SE
looking at a POV the platform had suspended overnight was offered **Suspend** and denied
**Start**: the cost feature working exactly as designed was what broke the controls.

One collection read per pass fixes four things at once, which is why they are one module
rather than four:

    runstate        the above
    rate_limited    the platform's own throttle flag, moved onto the row where the
                    buttons are — it was only ever on the read-only table beside them
    suspend_on_idle visible at last, on the row it applies to
    drift           an environment deleted on the platform by hand left a row claiming
                    `active` forever; `delete_environment` is idempotent on 404, so
                    nothing ever found out

**Absence from the listing is not proof of deletion.** The listing is project-scoped when
a Project ID is set (see ``skytap_service._environments_path``), so an environment created
before that setting — or outside it — is invisible to the list and perfectly alive. So a
row that goes missing from the listing is *confirmed with a direct read* before it is
flagged, and a 404 from that read is the only thing that sets ``platform_missing``. That is
the same refusal-to-guess that makes ``verify()`` report an empty catalogue as a failure
rather than a warning.

**Nothing here destroys anything.** A missing environment is flagged for a human and left
alone. The row holds the only record of which PRA, Password Safe and Entitle tenants that
POV was wired into, and the reaping manifest that goes with it; deleting it because a
listing came back short would throw that away on the strength of a scope question.

Its own job type rather than a step inside ``expiry_sweep``, which also walks POV rows
every pass. That sweep is not enqueued at all while ``resource_expiry_enabled`` is off
(``expiry_reaper.enqueue_sweep_if_due`` returns None), and a fresh POV instance starts with
it off — see ``docs/pov-instance.md``. Hanging this off it would mean the dashboard's view
of a customer's running environments silently stopped updating because an unrelated feature
had not been turned on yet.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import Job, PovEnvironment, SessionLocal
from . import job_service, lab_platforms

logger = logging.getLogger(__name__)

RECONCILE_JOB_TYPE = "pov_env_reconcile"

# Its own advisory-lock id, distinct from the expiry sweep's. Two enqueue paths sharing a
# lock id would serialize on each other for no reason.
_ENQUEUE_LOCK_ID = 20260829

# How often the app asks for a pass, and the floor under it. Ten minutes is chosen against
# what it is for: an SE who suspends a POV, or a platform idle timer that fires, should be
# visible before somebody gives up and refreshes the page a third time. It is one paginated
# collection read per pass, which the POV page itself already makes on every load.
DEFAULT_INTERVAL_S = 600
MIN_INTERVAL_S = 60

# Statuses whose rows this pass must not touch. A provision or a destroy job owns its row
# for the length of the job and writes `runstate` itself; a sweep landing in the middle
# would overwrite a job's own view of the environment with a listing read taken before its
# last transition.
_OWNED_BY_A_JOB = ("provisioning", "destroying")


def _utcnow() -> datetime:
    return datetime.utcnow()


def interval_seconds() -> int:
    """The cadence, read live so a Settings change takes effect on the next pass."""
    try:
        from . import config_service
        raw = config_service.get("pov_reconcile_interval_seconds")
        if raw:
            return max(MIN_INTERVAL_S, int(raw))
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_INTERVAL_S


# ── enqueue side (runs in the app) ───────────────────────────────────────────

def enqueue_if_due(db: Session, *, min_gap_seconds: int | None = None) -> str | None:
    """Create one reconcile job unless a pass is active or just ran.

    Same three guards as ``expiry_reaper.enqueue_sweep_if_due``, and the recency one is
    not redundant for the same measured reason it is not there: the app runs under
    ``gunicorn -w 2``, a pass over a handful of rows finishes in well under a second, and a
    liveness check alone cannot dedupe instantaneous work. Duplicates here are harmless
    rather than destructive, but two passes are two sets of platform reads against a
    rate-limited account, which is the thing this integration is most careful about.
    """
    if not _feature_on():
        return None
    if min_gap_seconds is None:
        min_gap_seconds = max(30, interval_seconds() // 2)
    try:
        from ..database import _is_sqlite
        if not _is_sqlite:
            db.execute(text("SELECT pg_advisory_xact_lock(:i)"), {"i": _ENQUEUE_LOCK_ID})
        active = (db.query(Job.id)
                    .filter(Job.job_type == RECONCILE_JOB_TYPE,
                            Job.status.in_(job_service.ACTIVE_STATUSES))
                    .first())
        if active:
            return None
        if min_gap_seconds > 0:
            floor = _utcnow() - timedelta(seconds=min_gap_seconds)
            recent = (db.query(Job.id)
                        .filter(Job.job_type == RECONCILE_JOB_TYPE,
                                Job.created_at >= floor)
                        .first())
            if recent:
                return None
        job = job_service.create_job(db, job_type=RECONCILE_JOB_TYPE, created_by="system")
        return job.id
    except Exception:  # noqa: BLE001
        logger.warning("could not enqueue a POV reconcile", exc_info=True)
        db.rollback()
        return None


def _feature_on() -> bool:
    """POV environments enabled, on a profile that has them.

    Through ``feature_flags.enabled`` — its docstring calls itself "the one place this
    happens", and it is what applies the profile mask. On a demo instance the flag reports
    False whatever config stores, so a loop reading the raw key would keep asking a lab
    platform the rest of the install has masked off.
    """
    try:
        from . import feature_flags
        return feature_flags.enabled("pov_environments_enabled")
    except Exception:  # noqa: BLE001
        return False


# ── the pass itself (runs in the worker) ─────────────────────────────────────

def _managed(db: Session) -> list[PovEnvironment]:
    """Rows worth asking the platform about: ours, not destroyed, not mid-job, and with
    a platform id to ask about."""
    from . import pov_env_service
    rows = (db.query(PovEnvironment)
              .filter(PovEnvironment.status != pov_env_service.STATUS_DESTROYED)
              .all())
    return [r for r in rows
            if (r.platform_environment_id or "").strip()
            and r.status not in _OWNED_BY_A_JOB]


async def _confirm_missing(mod, env: PovEnvironment) -> bool:
    """Is this environment really gone, or merely outside the listing's scope?

    A 404 from a direct read is the only answer that counts. Anything else — a rate limit,
    a network blip, a 500 — is not evidence of deletion, and is reported as "still there"
    so that a bad afternoon on the platform's side cannot paint an estate as missing.
    """
    try:
        await mod.get_environment(env.platform_environment_id)
        return False
    except Exception as exc:  # noqa: BLE001
        if "(404)" in str(exc):
            return True
        logger.info("POV reconcile: %s did not confirm as missing (%s)", env.name, exc)
        return False


async def reconcile(db: Session, platform: str, *, job_id: str = "") -> dict:
    """One pass over one platform. Returns a summary for the job result.

    Never raises for a single row: a POV whose read fails must not stop the others being
    refreshed, because the whole value of the pass is that it is the thing keeping every
    row honest.
    """
    mod = lab_platforms.adapter(platform)
    rows = [r for r in _managed(db) if r.platform == platform]
    out = {"platform": platform, "checked": len(rows), "updated": 0,
           "missing": 0, "recovered": 0, "skipped": 0}
    if not rows:
        return out

    if not mod.configured():
        out["skipped"] = len(rows)
        return out

    live = {str(e.get("id")): e for e in await mod.list_environments()}
    now = _utcnow()

    for env in rows:
        raw = live.get(str(env.platform_environment_id))
        if raw is None:
            # Absent from a project-scoped listing is a question, not an answer.
            if await _confirm_missing(mod, env):
                if not env.platform_missing:
                    out["missing"] += 1
                    logger.warning(
                        "POV reconcile: environment %s (%s) is gone from %s — the row is "
                        "flagged, not deleted; it still holds this POV's tenant "
                        "references and reaping manifest",
                        env.platform_environment_id, env.name, platform)
                env.platform_missing = True
                env.platform_seen_at = now
            continue

        # Seen. A row that was flagged and is visible again is un-flagged: the usual cause
        # is a Project ID that changed under it, and a flag that only ever latches on is a
        # flag nobody trusts.
        if env.platform_missing:
            out["recovered"] += 1
        env.platform_missing = False
        env.platform_seen_at = now

        changed = False
        runstate = str(raw.get("runstate") or "")
        if runstate and runstate != (env.runstate or ""):
            logger.info("POV reconcile: %s runstate %s -> %s",
                        env.name, env.runstate or "?", runstate)
            env.runstate = runstate
            changed = True

        limited = bool(raw.get("rate_limited"))
        if limited != bool(env.rate_limited):
            env.rate_limited = limited
            changed = True

        idle = raw.get("suspend_on_idle")
        idle = int(idle) if isinstance(idle, (int, float)) else None
        if idle != env.suspend_on_idle_seconds:
            env.suspend_on_idle_seconds = idle
            changed = True

        # The spend accrual, from the read this pass ALREADY made. No extra platform call
        # and no billing API: the live environment dict carries every VM's shape, disk and
        # runstate, which is all a list-price rate needs. See services/pov_spend.
        _accrue_spend(db, env, raw)

        if changed:
            out["updated"] += 1

    db.commit()
    if job_id:
        job_service.append_job_log(
            db, job_id,
            f"{platform}: checked {out['checked']}, updated {out['updated']}, "
            f"newly missing {out['missing']}, recovered {out['recovered']}")
    return out


# Who a scheduled power action is attributed to. Its own actor, not the operator who set
# the schedule: the /jobs row should say why the POV suspended at 19:00, and a username
# there reads as somebody having pressed a button.
SCHEDULE_ACTOR = "pov-schedule"

# Who a spend-cap suspension is attributed to. Its own actor, like the
# schedule's: the /jobs row should say why the POV stopped, and a username
# there reads as somebody having pressed a button.
SPEND_ACTOR = "pov-spend-cap"


def _log(db: Session, job_id: str, line: str) -> None:
    if job_id:
        job_service.append_job_log(db, job_id, line)


def _power_job_in_flight(db: Session, environment_id: str) -> bool:
    """Whether a power job is already queued or running for this POV.

    Scanned in Python rather than filtered in SQL because the environment id lives in the
    job's JSON metadata, and there is no JSON filter portable across SQLite and Postgres —
    the same constraint `database.py` cites for putting expiry on real columns. The set is
    tiny: only ACTIVE power jobs, across all POVs.
    """
    rows = (db.query(Job)
              .filter(Job.job_type == "pov_env_power",
                      Job.status.in_(job_service.ACTIVE_STATUSES))
              .all())
    return any(r.metadata_dict.get("environment_id") == environment_id for r in rows)


def sweep_schedules(db: Session, *, job_id: str = "") -> int:
    """Act on any suspend schedule whose boundary has been crossed. Returns how many.

    Rides this pass rather than a timer of its own, for the reason the accessor sweep
    gives above: this is the loop that already knows every POV's real runstate, already
    runs every ten minutes, and is already single-flight across gunicorn workers and
    worker replicas. A second sweeper would be a second thing to arm and a second thing to
    forget.

    **It only ever enqueues a `pov_env_power` job** — the same one the Suspend and Start
    buttons create. Calling a cloud SDK from inside the reconcile pass would put a
    multi-minute power operation in front of every other row's read, and would make the
    action invisible: as a job it has a row, a Live Output and a cancel, and its failure
    reaches the dashboard's failed-jobs panel like any other.

    Skipped entirely for a platform with an idle timer of its own. The capability table is
    the arbiter, so a platform that grows one later stops being driven from here without
    this function learning its name.
    """
    from . import pov_env_service, pov_schedule

    now = datetime.now(timezone.utc)
    acted = 0
    rows = (db.query(PovEnvironment)
              .filter(PovEnvironment.status == pov_env_service.STATUS_ACTIVE)
              .all())
    for env in rows:
        if not pov_schedule.has_schedule(env):
            continue
        try:
            if lab_platforms.supports(env.platform, "idle_suspend"):
                continue
            wanted = pov_schedule.due_action(env, now)
        except Exception as exc:  # noqa: BLE001 — one bad row never stops the sweep
            logger.warning("POV %s: could not evaluate its schedule", env.id,
                           exc_info=True)
            _log(db, job_id, f"{env.name}: schedule ignored ({exc})")
            continue

        # Stamped whatever happens, and BEFORE the enqueue. The latch is what bounds the
        # next window; leaving it behind on the path that acts would make the same
        # boundary fire again on every pass until the power job finished.
        env.schedule_last_checked_at = now.replace(tzinfo=None)

        if not wanted or (env.runstate or "") == wanted:
            continue
        if not pov_env_service.may_act_on(env):
            continue
        # Something is already changing this environment's power state — the operator
        # pressed a button, or a previous crossing is still running. Two power jobs for
        # one environment is how a resume and a suspend race to a coin flip.
        if _power_job_in_flight(db, env.id):
            _log(db, job_id,
                 f"{env.name}: a power job is already running, schedule deferred")
            continue

        job = job_service.create_job(
            db, job_type="pov_env_power", created_by=SCHEDULE_ACTOR,
            workgroup=env.workgroup,
            metadata={"environment_id": env.id, "runstate": wanted})
        acted += 1
        logger.info("POV %s: schedule -> %s (job %s)", env.name, wanted, job.id)
        _log(db, job_id, f"{env.name}: schedule says {wanted} (job {job.id})")

    db.commit()
    return acted


def _spend_config() -> tuple:
    """``(action, warn_percent)`` as configured, both already normalised."""
    from ..config import settings
    from . import config_service, pov_spend
    try:
        action = (config_service.get("pov_spend_cap_action")
                  or getattr(settings, "pov_spend_cap_action", ""))
        percent = (config_service.get("pov_spend_warn_percent")
                   or getattr(settings, "pov_spend_warn_percent", None))
    except Exception:  # noqa: BLE001 — a sweep never fails on a config read
        action, percent = "", None
    return pov_spend.normalize_action(action), pov_spend.warn_percent(percent)


def _accrue_spend(db: Session, env: PovEnvironment, raw: dict) -> None:
    """Add this interval's estimated cost to the row. Never raises.

    Called from inside the platform loop, with the environment dict that loop already
    fetched — so the accrual costs no extra API call, and it cannot disagree with the
    runstate recorded beside it.

    The rate is a LIST-PRICE estimate and only exists for clouds with a price source. Where
    there is none the clock still moves on, so a rate that appears later does not then bill
    for the blind period.
    """
    from . import pov_cloud_cost, pov_spend

    try:
        rate = pov_cloud_cost.rate_usd_per_hour(raw, env.region or "", env.platform)
        total, at, _added = pov_spend.accrue(
            env.spend_estimate_usd, env.spend_accrued_at, rate,
            datetime.now(timezone.utc))
        env.spend_estimate_usd = total
        env.spend_accrued_at = at.replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        logger.warning("POV %s: could not accrue spend", env.id, exc_info=True)


def sweep_spend(db: Session, *, job_id: str = "") -> int:
    """Act on any POV that has newly reached its warning threshold or its cap.

    Runs AFTER the platform loop, on totals that loop has just accrued. Returns how many
    rows it acted on.

    **The cap suspends; it never destroys.** That is what lets this feature exist without
    the auto-delete timer's two arming clocks and dry-run mode — the worst outcome is a POV
    somebody starts again, and the default action is `warn` regardless. Like the schedule,
    it only ever ENQUEUES a `pov_env_power` job, so the action has a /jobs row, Live Output
    and a place in the failed-jobs panel.
    """
    from . import pov_env_service, pov_spend

    action, percent = _spend_config()
    now = datetime.now(timezone.utc)
    acted = 0

    for env in (db.query(PovEnvironment)
                  .filter(PovEnvironment.status == pov_env_service.STATUS_ACTIVE)
                  .all()):
        try:
            reached = pov_spend.state(env, warn_at_percent=percent)
        except Exception:  # noqa: BLE001 — one bad row never stops the sweep
            logger.warning("POV %s: could not evaluate its spend cap", env.id,
                           exc_info=True)
            continue
        if not reached:
            continue

        spent = float(env.spend_estimate_usd or 0.0)
        cap = float(env.spend_cap_usd or 0.0)
        if reached == "warn":
            # Latched before the log line, not after: a warning that failed to write its
            # latch would repeat every ten minutes for the rest of the evaluation.
            env.spend_warned_at = now.replace(tzinfo=None)
            acted += 1
            logger.info("POV %s: spend at %.0f%% of its cap", env.name,
                        spent / cap * 100 if cap else 0)
            _log(db, job_id,
                 f"{env.name}: estimated ${spent:,.2f} of its ${cap:,.2f} cap "
                 f"({percent}% threshold reached)")
            continue

        # Reached the cap. Latched whatever the action is — under `warn` the operator has
        # been told once and does not need telling every pass.
        env.spend_capped_at = now.replace(tzinfo=None)
        acted += 1
        if action != pov_spend.ACTION_SUSPEND:
            logger.info("POV %s: over its spend cap, action is warn-only", env.name)
            _log(db, job_id,
                 f"{env.name}: estimated ${spent:,.2f} is OVER its ${cap:,.2f} cap. "
                 f"Nothing was suspended — the action is set to warn.")
            continue
        if (env.runstate or "") == "stopped" or not pov_env_service.may_act_on(env):
            _log(db, job_id, f"{env.name}: over its ${cap:,.2f} cap and already stopped")
            continue
        if _power_job_in_flight(db, env.id):
            # Not latched in this case — the cap has NOT been acted on yet, and latching
            # here would let a POV sail past it because something else was mid-flight.
            env.spend_capped_at = None
            _log(db, job_id,
                 f"{env.name}: over its cap, but a power job is already running")
            continue
        job = job_service.create_job(
            db, job_type="pov_env_power", created_by=SPEND_ACTOR,
            workgroup=env.workgroup,
            metadata={"environment_id": env.id, "runstate": "stopped"})
        logger.info("POV %s: suspended at its spend cap (job %s)", env.name, job.id)
        _log(db, job_id,
             f"{env.name}: estimated ${spent:,.2f} reached its ${cap:,.2f} cap — "
             f"suspending (job {job.id})")

    db.commit()
    return acted

async def run_reconcile(job_id: str, meta: dict) -> None:
    """The job body: reconcile every configured lab platform."""
    db = SessionLocal()
    try:
        if not _feature_on():
            job_service.set_completed(db, job_id,
                                      {"skipped": "POV environments are not enabled"})
            return

        # The accessor sweep runs FIRST and OUTSIDE the platform loop, and both halves of
        # that are deliberate. It is the backstop for the two ways an accessor's teardown
        # never runs — Entitle not calling delete_actor, and a POV that reached `destroyed`
        # without this module being asked — and whether a prospect's login should still
        # work has nothing to do with whether Skytap is reachable, which is what every
        # branch inside that loop turns on. It rides this pass rather than the expiry sweep
        # because a fresh POV instance starts with the auto-delete timer OFF
        # (docs/pov-instance.md), and a login must not outlive its POV because an unrelated
        # feature was never enabled.
        # Imported here, like config_service and pov_env_service elsewhere in this file:
        # the sweep is one call on one path, and a module-level import would put the whole
        # accessor stack in front of every reader of this one.
        from . import pov_accessor_service
        try:
            reaped = pov_accessor_service.sweep(db)
        except Exception as exc:  # noqa: BLE001 — a sweep never fails the pass
            logger.warning("POV reconcile: the accessor sweep failed", exc_info=True)
            job_service.append_job_log(db, job_id, f"accessor sweep FAILED: {exc}")
            db.rollback()
            reaped = 0
        if reaped:
            job_service.append_job_log(
                db, job_id, f"revoked {reaped} expired or orphaned accessor login(s)")

        summaries, failures = [], []
        for platform in lab_platforms.VALID_PLATFORMS:
            try:
                summaries.append(await reconcile(db, platform, job_id=job_id))
            except Exception as exc:  # noqa: BLE001
                # One platform failing must not stop the others, and must not fail the
                # pass: a reconcile that reports nothing because a token expired is worse
                # than one that reports what it could reach and says which it could not.
                logger.warning("POV reconcile: %s failed", platform, exc_info=True)
                failures.append(f"{platform}: {exc}")
                db.rollback()
                job_service.append_job_log(db, job_id, f"{platform} FAILED: {exc}")

        result = {"platforms": summaries, "accessors_reaped": reaped}
        if failures:
            result["failures"] = failures

        # The suspend schedule, AFTER the platform loop rather than before it. The
        # decision is "has a boundary been crossed", which needs no platform read — but
        # the skip on `runstate == wanted` is only right if the runstate above is fresh,
        # so it runs once the loop has refreshed every row it could reach.
        try:
            scheduled = sweep_schedules(db, job_id=job_id)
        except Exception as exc:  # noqa: BLE001 — a sweep never fails the pass
            logger.warning("POV reconcile: the schedule sweep failed", exc_info=True)
            job_service.append_job_log(db, job_id, f"schedule sweep FAILED: {exc}")
            db.rollback()
            scheduled = 0
        if scheduled:
            job_service.append_job_log(
                db, job_id, f"schedule: enqueued {scheduled} power action(s)")
        result["scheduled_power_actions"] = scheduled

        # The spend cap, AFTER the platform loop and after the schedule. It acts on totals
        # the loop has just accrued, and running it last means a POV that hits its cap in
        # the same pass a schedule would have resumed it ends up stopped — the cap is the
        # stronger statement of the two.
        try:
            capped = sweep_spend(db, job_id=job_id)
        except Exception as exc:  # noqa: BLE001 — a sweep never fails the pass
            logger.warning("POV reconcile: the spend sweep failed", exc_info=True)
            job_service.append_job_log(db, job_id, f"spend sweep FAILED: {exc}")
            db.rollback()
            capped = 0
        if capped:
            job_service.append_job_log(
                db, job_id, f"spend: acted on {capped} POV(s) at or near their cap")
        result["spend_actions"] = capped

        job_service.set_completed(db, job_id, result)
    finally:
        db.close()


def describe(env: PovEnvironment) -> dict:
    """The reconcile's view of one row, for the POV page.

    ``seen_at`` is served so the page can say how fresh the runstate is rather than
    implying it is live. A dashboard that shows a stale value as though it were current is
    what this module exists to stop, and replacing "never asked" with "asked 40 minutes
    ago, silently" would be a smaller version of the same lie.
    """
    from . import pov_cloud_cost, pov_schedule, pov_spend
    action, percent = _spend_config()
    spend = pov_spend.describe(env, warn_at_percent=percent, action=action)
    priced = pov_cloud_cost.priced(env.platform)
    try:
        schedule = pov_schedule.describe(env)
    except pov_schedule.ScheduleError:
        # A stored schedule that no longer parses — a timezone the image dropped, say.
        # Rendered as "none" rather than failing the row: the POV is running either way,
        # and the sweep logs the same refusal against the row that owns it.
        schedule = pov_schedule.describe(object())
    return {
        "platform_seen_at": (env.platform_seen_at.isoformat()
                             if env.platform_seen_at else ""),
        "rate_limited": bool(env.rate_limited),
        "platform_missing": bool(env.platform_missing),
        "suspend_on_idle_seconds": env.suspend_on_idle_seconds or 0,
        # The dashboard-driven schedule, for a platform with no idle timer of its own. The
        # page shows exactly one of the two — they answer the same question and a row
        # offering both would be a row where neither is clearly in charge.
        "schedule": schedule,
        # The spend cap. `priced` is what the page gates its control on: a cloud with no
        # price source would offer a cap that never accrues, which is worse than no cap.
        "spend": spend,
        "spend_priced": priced,
        "spend_reason": "" if priced else pov_cloud_cost.no_price_reason(env.platform),
    }
