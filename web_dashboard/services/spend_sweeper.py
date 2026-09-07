"""The spend-cap sweep: accrue what each capped cloud VM is costing, and act at the cap.

``spend_policy`` answers the arithmetic — how much has accrued, and whether this row has
newly reached its warning threshold or its cap. ``pov_cloud_cost`` answers the rate. This
walks the inventory, asks both, and enqueues the power job. Like the suspend sweep it never
powers anything itself: it enqueues the identical ``*_power`` row the Suspend button
creates, so the audit row, the ``/jobs`` entry and the workgroup come out the same either
way.

**Phase 2 of the audit's Recommendation 4.** Phase 1 gave the estate a business-hours
window; this gives it the other lever the POV profile already had — *"how much may this
cost?"* — which is the question an operator on their own cloud account actually loses sleep
over. A clock is a poor proxy: the same fortnight is twenty dollars or two thousand
depending on what was deployed, and the second only becomes visible on an invoice weeks
later.

**Its own sweep, not folded into ``suspend_sweep``.** Different gate and different rows: the
suspend sweep selects VMs carrying a schedule, this one selects VMs carrying a cap, and an
operator may well want one without the other. The same reasoning that kept the suspend sweep
out of ``expiry_sweep``.

**Why the query filters on ``spend_cap_usd IS NOT NULL``, and why that is load-bearing.**
Accrual writes two columns per VM per pass, and ``jobs`` is the table ``_claim_one`` polls
every two seconds. Scoping to capped rows means an estate that has set no caps does no
writes at all — the feature costs nothing until somebody asks for it, which is the same
property ``expires_at IS NULL`` and the NULL schedule latch already give.

**A cap that cannot be priced never gets stored.** ``api/spend`` refuses it at the door via
``spend_policy.cappable``, because ``accrue`` treats a missing rate as *move the clock on,
bill nothing* — right for a blind interval, silence for a permanent one. This sweep handles
the other direction: a VM that was priceable when its cap was set and is not any more (the
region reconfigured, ``pricing:GetProducts`` revoked) is REPORTED, not quietly accrued at
zero forever.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import Job
from . import job_service, spend_policy, suspend_sweeper, vm_suspend_policy

logger = logging.getLogger(__name__)

SWEEP_JOB_TYPE = "spend_sweep"
FLAG = "vm_spend_cap_enabled"

# Distinct from the suspend sweep's — two advisory locks, so a spend pass and a suspend
# pass can be enqueued in the same instant without one blocking the other.
_ENQUEUE_LOCK_ID = 20260402
_LAST_SWEEP_KEY = "vm_spend_last_sweep"


def _utcnow() -> datetime:
    return datetime.utcnow()


def enabled() -> bool:
    from . import config_service
    return config_service.get_bool(FLAG, False)


def action() -> str:
    """``warn`` or ``suspend``. Defaults to ``warn``, and an unreadable value resolves
    there too — the number is an estimate, and an estimate that suspends somebody's
    infrastructure on its first outing would be the last time anybody trusted it."""
    from . import config_service
    from ..config import settings
    return spend_policy.normalize_action(
        config_service.get("vm_spend_cap_action")
        or getattr(settings, "vm_spend_cap_action", ""))


def warn_percent() -> int:
    from . import config_service
    from ..config import settings
    return spend_policy.warn_percent(
        config_service.get("vm_spend_warn_percent")
        or getattr(settings, "vm_spend_warn_percent", None))


def interval_seconds() -> int:
    """How often to accrue. Ten minutes by default.

    The interval is also the accrual resolution: ``accrue`` applies the rate observed now
    across the whole gap since the last pass, so a longer interval means a coarser estimate
    around a power transition. Floored at 60s for the same reason the suspend sweep is —
    below that it is all overhead.
    """
    from . import config_service
    try:
        minutes = int(config_service.get("vm_spend_sweep_interval_minutes", "") or 10)
    except (TypeError, ValueError):
        minutes = 10
    return max(60, minutes * 60)


def enqueue_sweep_if_due(db: Session) -> "str | None":
    """Create one sweep job if none is in flight and the interval has elapsed."""
    if not enabled():
        return None
    try:
        from ..database import _is_sqlite
        if not _is_sqlite:
            db.execute(text("SELECT pg_advisory_xact_lock(:i)"), {"i": _ENQUEUE_LOCK_ID})
        existing = (db.query(Job.id)
                    .filter(Job.job_type == SWEEP_JOB_TYPE,
                            Job.status.in_(job_service.ACTIVE_STATUSES))
                    .first())
        if existing:
            return None
        floor = _utcnow() - timedelta(seconds=interval_seconds())
        recent = (db.query(Job.id)
                  .filter(Job.job_type == SWEEP_JOB_TYPE, Job.created_at >= floor)
                  .first())
        if recent:
            return None
        job = job_service.create_job(db, job_type=SWEEP_JOB_TYPE, created_by="system")
        return job.id
    except Exception:
        logger.warning("could not enqueue a spend sweep", exc_info=True)
        db.rollback()
        return None


def capped_vms(db: Session) -> list:
    """Live cloud VM deploy rows that carry a cap.

    ``spend_cap_usd IS NOT NULL`` is the filter that keeps this feature off a hot table
    until somebody uses it — see the module docstring.
    """
    return (db.query(Job)
            .filter(Job.job_type.in_(suspend_sweeper._DEPLOY_TYPES),
                    Job.status == "completed",
                    Job.spend_cap_usd.isnot(None))
            .all())


def last_power_action(db: Session) -> dict:
    """``{deploy_job_id: "start"|"stop"}`` — the last power action the dashboard took on
    each VM, from the ``*_power`` jobs it created.

    Compute stops billing when a VM is deallocated, so a cap that kept accruing compute for
    a suspended VM would defeat the Phase 1 schedule it sits beside: an operator would
    suspend to save money and watch the estimate climb anyway.

    **The dashboard's own jobs are the source, not a live cloud read.** A per-VM describe on
    every ten-minute pass is a cost of its own, on APIs that are rate-limited and — on AWS —
    billed. These are the VMs the dashboard deploys and powers, so its own job history
    answers for almost all of them.

    Where it is wrong it is wrong in the safe direction: a VM stopped in the cloud's own
    console is still counted as running, so the estimate reads HIGH. That is the direction
    the whole feature already errs in (list price, no reservations, no credits) and the only
    safe one for a cap — the alternative under-reports and lets a cap sail past.

    One query, not one per VM.
    """
    latest: dict = {}
    for job in (db.query(Job)
                .filter(Job.job_type.in_(tuple(suspend_sweeper._POWER_JOB.values())),
                        Job.status == "completed")
                .order_by(Job.created_at.asc())
                .all()):
        m = job.metadata_dict
        deploy_id = m.get("deploy_job_id")
        if deploy_id and m.get("action") in ("start", "stop"):
            # Ascending, so the last write wins and `latest` ends on the newest action.
            latest[deploy_id] = m["action"]
    return latest


def _rate_for(row, meta: dict, *, running: bool):
    """USD/hour for one VM at list price, or ``None`` when it cannot be priced.

    ``None`` is meaningful and is NOT the same as zero: ``accrue`` moves the clock on
    without billing, so a rate that appears later does not then charge for the blind
    period. The caller reports it rather than swallowing it.
    """
    from . import pov_cloud_cost

    cloud = vm_suspend_policy.cloud_of(row.job_type)
    region = meta.get("region") or meta.get("location") or meta.get("zone") or ""
    # A zone is a region plus a suffix on GCP; the price catalogue keys on the region.
    if cloud == "gcp" and region:
        from . import region_catalog
        region = region_catalog.region_from_zone(region) or region
    if not pov_cloud_cost.priceable(cloud, region):
        return None
    # The shape `hourly_for_vm` reads: `runstate` gates compute, `instance_type`,
    # `os_family` and `disk_gb` price it. Each cloud's deploy row spells the size
    # differently, so this is the one place they are normalised.
    vm = {
        "runstate": "running" if running else "stopped",
        "instance_type": (meta.get("instance_type") or meta.get("vm_size")
                          or meta.get("machine_type") or meta.get("shape") or ""),
        "os_family": "windows" if (meta.get("os_type") or "").lower() == "windows" else "linux",
        "disk_gb": (meta.get("disk_size_gb") or meta.get("boot_volume_gb")
                    or meta.get("disk_gb") or 0),
    }
    try:
        return pov_cloud_cost.hourly_for_vm(cloud, region, vm)
    except Exception:  # noqa: BLE001 — one unpriceable shape never stops the sweep
        logger.info("spend sweep: no rate for %s", row.id, exc_info=True)
        return None


async def run(db: Session, *, job_id: str, meta: dict) -> None:
    """One pass: accrue every capped VM, then act on any that newly reached a threshold.

    Reports what it would do whether or not it may act, so an operator can watch the
    estimate move before trusting it with anything.
    """
    job_service.set_running(db, job_id)
    now = datetime.now(timezone.utc)
    configured_action, percent = action(), warn_percent()
    accrued, acted, unpriced = 0, [], []

    try:
        powered = last_power_action(db)
        for row in capped_vms(db):
            m = row.metadata_dict
            if m.get("destroyed"):
                continue

            # Absent any power job, a deployed VM is running — see last_power_action.
            rate = _rate_for(row, m, running=powered.get(row.id, "start") != "stop")
            if rate is None:
                # Reported, never silent. A cap whose VM cannot be priced does not fire,
                # and an operator who is not told believes they are protected.
                unpriced.append({"job_id": row.id,
                                 "name": _name(m) or row.id,
                                 "reason": "no price source for this cloud and region"})
            total, at, _added = spend_policy.accrue(
                row.spend_estimate_usd, row.spend_accrued_at, rate, now)
            row.spend_estimate_usd = total
            row.spend_accrued_at = at.replace(tzinfo=None)
            accrued += 1

            reached = spend_policy.state(row, warn_at_percent=percent)
            if not reached:
                continue

            spent = float(row.spend_estimate_usd or 0.0)
            cap = float(row.spend_cap_usd or 0.0)
            if reached == "warn":
                # Latched before the log line, not after: a warning whose latch failed to
                # write would repeat every ten minutes for the rest of the month.
                row.spend_warned_at = now.replace(tzinfo=None)
                acted.append({"job_id": row.id, "event": "warn", "spent": round(spent, 2)})
                job_service.append_job_log(
                    db, job_id, f"{_name(m) or row.id}: estimated ${spent:,.2f} of its "
                                f"${cap:,.2f} cap ({percent}% threshold)")
                _notify(db, "vm.spend_warn", row, m, spent, cap)
                continue

            # Reached the cap. Latched whatever the action is — under `warn` the operator
            # has been told once and does not need telling every pass.
            row.spend_capped_at = now.replace(tzinfo=None)
            acted.append({"job_id": row.id, "event": "cap", "spent": round(spent, 2)})
            _notify(db, "vm.spend_capped", row, m, spent, cap)

            if configured_action != spend_policy.ACTION_SUSPEND:
                job_service.append_job_log(
                    db, job_id, f"{_name(m) or row.id}: estimated ${spent:,.2f} is OVER its "
                                f"${cap:,.2f} cap. Nothing was suspended — the action is "
                                f"set to warn.")
                continue

            ok, why = vm_suspend_policy.schedulable(row.job_type, m)
            if not ok:
                # The cap was accepted when this VM could be suspended and cannot be acted
                # on now. Say which, rather than logging a suspend that did not happen.
                job_service.append_job_log(
                    db, job_id, f"{_name(m) or row.id}: over its ${cap:,.2f} cap, but it "
                                f"cannot be suspended — {why}")
                continue

            cloud = vm_suspend_policy.cloud_of(row.job_type)
            child = job_service.create_job(
                db,
                job_type=suspend_sweeper._POWER_JOB[cloud],
                created_by="system",
                workgroup=row.workgroup,
                metadata=suspend_sweeper._power_meta(cloud, row, m, "stop"),
            )
            job_service.append_job_log(
                db, job_id, f"{_name(m) or row.id}: over its ${cap:,.2f} cap — suspending "
                            f"(job {child.id})")

        db.commit()
        job_service.set_completed(db, job_id, {
            "accrued": accrued, "acted": acted, "unpriced": unpriced,
            "action": configured_action,
        })
    except Exception as exc:
        logger.error("spend sweep failed: %s", exc)
        db.rollback()
        job_service.set_failed(db, job_id, str(exc))


def _name(meta: dict) -> str:
    return (meta.get("instance_name") or meta.get("vm_name")
            or meta.get("instance_id") or meta.get("instance_ocid") or "")


def _notify(db, event: str, row, meta: dict, spent: float, cap: float) -> None:
    """Raise a notification. Best-effort by contract — a cap must still latch and act when
    the notifier is misconfigured, or one broken webhook would disable the feature.

    The dedupe bucket is the VM's own job id rather than a day stamp: unlike the
    account-level budget scanner, each of these events is already latched in the database
    and fires at most once per VM per threshold, so a second bucket dimension would only
    be able to suppress a real event.
    """
    try:
        from . import notification_service, notify_policy
        notification_service.emit_safe(db, notify_policy.NotificationEvent(
            event_type=event,
            title=f"{_name(meta) or row.id}: ${spent:,.2f} of a ${cap:,.2f} cap",
            body=("Estimated list-price spend for this VM has reached its cap. "
                  "The estimate excludes Savings Plans, reservations, credits, free tier, "
                  "data transfer and snapshots, and errs high."),
            url="/jobs",
            dedupe_bucket=f"{event}:{row.id}",
            fields={k: v for k, v in (
                ("Estimated", f"${spent:,.2f}"),
                ("Cap", f"${cap:,.2f}"),
                ("VM", _name(meta)),
                ("Cloud", vm_suspend_policy.cloud_of(row.job_type).upper()),
            ) if v},
        ))
    except Exception:  # noqa: BLE001
        logger.info("spend sweep: could not raise %s for %s", event, row.id, exc_info=True)
