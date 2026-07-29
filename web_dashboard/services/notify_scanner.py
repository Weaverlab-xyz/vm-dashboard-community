"""Periodic condition scan: the events that are a *state*, not a moment.

A job failing is an event — something happened, and the code that made it happen can
say so. A cost budget being over, a secret being stale, a host having drifted are
*conditions*: nothing transitions, they are simply true until they aren't. Nobody was
going to call ``emit`` for them, so this walks them on a timer.

All three already had an evaluator and no delivery path. ``cost_monthly_budget`` has
sat in config.py labelled "for alerts" with nothing reading it; ``secret_hygiene``
computes ``stale_count`` for a page you have to open; ``config_drift`` the same.

Two design points worth keeping:

  * **The day bucket, not a latch.** Each condition dedupes on today's date, so a
    breach that stays true re-notifies once a day instead of once ever (useless) or
    once per pass (unbearable). No new column, no state to keep in step.
  * **Each source is independently guarded.** A cloud billing API being down must not
    stop the stale-secret warning from going out.

Runs in the worker, from ``notification_service.drain_loop``. Three worker replicas
all reach the same tick; the ``UNIQUE(dedupe_key)`` constraint is what makes that
harmless, so no lock is taken.
"""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def scan_if_due(db) -> dict:
    """Run the condition scan if its interval has elapsed. Never raises."""
    from . import notification_service, notify_policy

    if not notify_policy.enabled():
        return {"skipped": "disabled"}

    last = notification_service.last_scan_at()
    interval = notify_policy.scan_interval_seconds()
    if last is not None and (_utcnow() - last) < timedelta(seconds=interval):
        return {"skipped": "not due"}

    # Stamp first. A scan that half-finishes should not re-run every 30 seconds
    # hammering a billing API — the conditions will still be true next hour.
    notification_service.mark_scanned()
    return await scan_once(db)


async def scan_once(db) -> dict:
    """Evaluate every condition. Each is guarded on its own; one failing source
    cannot suppress the others."""
    from . import notify_policy

    bucket = notify_policy.day_bucket()
    out = {}
    for name, fn in (("cost", _scan_cost),
                     ("secrets", _scan_secrets),
                     ("drift", _scan_drift)):
        try:
            out[name] = await fn(db, bucket)
        except Exception:                              # noqa: BLE001
            logger.warning("notification scan: the %s check failed", name, exc_info=True)
            out[name] = 0
    logger.info("notification scan: %s", out)
    return out


def _emit(db, event_type: str, *, title: str, body: str, bucket: str,
          fields=None, url: str = "", severity: str = "") -> int:
    from . import notification_service, notify_policy
    event = notify_policy.NotificationEvent(
        event_type=event_type, title=title, body=body, severity=severity,
        url=url, dedupe_bucket=bucket,
        fields={k: v for k, v in (fields or {}).items() if v not in (None, "")},
    )
    return notification_service.emit_safe(db, event)


# ── Cost budget ──────────────────────────────────────────────────────────────

async def _scan_cost(db, bucket: str) -> int:
    """Notify once a day per budget whose month-to-date spend is already over.

    Reads the **cached** cost summary and does nothing when the cache is cold: those
    are billable API calls, and a notification is not worth paying for a fresh one.
    ``main._warm_cost_summary`` repopulates the cache on its own schedule.

    The cached payload deliberately has no ``budget`` on it — ``apply_budget_alerts``
    is date- and config-dependent, so the cost router applies it per request and so do
    we. Only ``status == "over"`` notifies; ``"approaching"`` is a softer signal and
    would need its own event type rather than being folded into this one.
    """
    from . import cache_service, config_service, cost_service

    if not config_service.get_bool("cost_explorer_enabled", False):
        return 0
    cached = await cache_service.get(cache_service.key_global("cost_summary"))
    if not cached:
        logger.debug("notification scan: cost cache is cold, skipping the budget check")
        return 0
    summary = cost_service.apply_budget_alerts(cached)

    sent = 0
    budget = summary.get("budget") or {}
    if budget.get("status") == "over":
        cur = budget.get("currency") or ""
        sent += _emit(
            db, "cost.budget_exceeded",
            title=(f"Cloud spend is over budget: {budget.get('mtd')} of "
                   f"{budget.get('limit')} {cur}").strip(),
            body="Month-to-date spend across all clouds has passed the configured budget.",
            bucket=f"{bucket}:total", url="/costs",
            fields={"Month to date": budget.get("mtd"), "Budget": budget.get("limit"),
                    "Used": _pct(budget.get("pct_of_budget")),
                    "Projected month end": budget.get("projected"),
                    "Currency": cur})

    for entry in (summary.get("clouds") or []):
        b = entry.get("budget") or {}
        if b.get("status") != "over":
            continue
        cloud = entry.get("cloud") or "?"
        sent += _emit(
            db, "cost.budget_exceeded",
            title=f"{cloud} spend is over budget: {b.get('mtd')} of {b.get('limit')}",
            body=f"Month-to-date {cloud} spend has passed its configured budget.",
            # Per-cloud buckets, so one cloud going over doesn't mask another.
            bucket=f"{bucket}:{cloud}", url="/costs",
            fields={"Cloud": cloud, "Month to date": b.get("mtd"),
                    "Budget": b.get("limit"), "Used": _pct(b.get("pct_of_budget")),
                    "Projected month end": b.get("projected"),
                    "Currency": b.get("currency")})
    return sent


def _pct(value) -> str:
    try:
        return f"{float(value):.0f}%"
    except (TypeError, ValueError):
        return ""


# ── Secret staleness ─────────────────────────────────────────────────────────

async def _scan_secrets(db, bucket: str) -> int:
    from . import secret_hygiene

    report = secret_hygiene.collect(db)
    if not report.get("enabled") or not report.get("stale_count"):
        return 0
    keys = report.get("stale_keys") or []
    shown = ", ".join(keys[:8]) + (" …" if len(keys) > 8 else "")
    return _emit(
        db, "secret.stale",
        title=f"{report['stale_count']} stored secret(s) are overdue for rotation",
        body=(f"These have not changed in {report.get('max_age_days')} days or more:\n"
              f"{shown}"),
        bucket=bucket, url="/settings",
        fields={"Stale": report["stale_count"],
                "Threshold": f"{report.get('max_age_days')} days"})


# ── Config drift ─────────────────────────────────────────────────────────────

async def _scan_drift(db, bucket: str) -> int:
    from . import config_drift

    report = await config_drift.collect(db)
    changed = int(report.get("changed_count") or 0)
    unverified = int(report.get("unverified_count") or 0)
    if not changed and not unverified:
        return 0
    names = [i.get("target") for i in (report.get("items") or [])
             if i.get("changed") or i.get("unverified")]
    shown = ", ".join(n for n in names[:8] if n) + (" …" if len(names) > 8 else "")
    return _emit(
        db, "config.drift",
        title=f"Config drift: {changed} target(s) changed, {unverified} unverified",
        body=("A changed target is running an older version of its playbook than what "
              "is in storage. An unverified target has not had a successful apply "
              f"recently enough to be trusted.\n\n{shown}"),
        # Bucketed on the counts as well as the day, so drift getting worse during the
        # day notifies again rather than being swallowed by the morning's message.
        bucket=f"{bucket}:{changed}:{unverified}", url="/config-mgmt",
        fields={"Changed": changed or None, "Unverified": unverified or None,
                "Threshold": f"{report.get('stale_days')} days"})
