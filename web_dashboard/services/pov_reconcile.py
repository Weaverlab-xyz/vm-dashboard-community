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
from datetime import datetime, timedelta

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

        if changed:
            out["updated"] += 1

    db.commit()
    if job_id:
        job_service.append_job_log(
            db, job_id,
            f"{platform}: checked {out['checked']}, updated {out['updated']}, "
            f"newly missing {out['missing']}, recovered {out['recovered']}")
    return out


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
    return {
        "platform_seen_at": (env.platform_seen_at.isoformat()
                             if env.platform_seen_at else ""),
        "rate_limited": bool(env.rate_limited),
        "platform_missing": bool(env.platform_missing),
        "suspend_on_idle_seconds": env.suspend_on_idle_seconds or 0,
    }
