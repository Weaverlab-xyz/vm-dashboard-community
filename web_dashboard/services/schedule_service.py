"""Recurring job schedules: turning "again, every window" into job rows.

A ``JobSchedule`` is a job row held in escrow — ``job_type`` plus the metadata dict, which
is exactly what ``job_service.create_job`` takes. Materialising an occurrence is therefore
one call, and nothing downstream (the claim query, the tier tables, the dispatch chain)
needs to know a schedule exists. That is the property worth protecting in this module: the
moment materialisation starts special-casing job types, every new job type becomes a thing
somebody has to remember.

**Which jobs may be scheduled is an ALLOWLIST, and the reason is secrets.** A payload is
copied verbatim and re-used weeks later, so it is only safe for job types whose metadata
holds references rather than values. ``ansible_local`` qualifies (see
``ansible_run_meta.RUN_META_KEYS`` — refs only, resolved at run time); a job type that put
a credential, a token or a one-shot handle in its metadata would not. Defaulting to "any
job type" and denying the bad ones would mean a new job type is schedulable — and possibly
unsafe — the day it is added, by omission. So the default is no.

**Idempotence is ``last_materialised_for``**, not a lock. It records the window START
instant of the most recent occurrence turned into a job. A sweep compares the current
occurrence's start against it and does nothing if they match, so two sweeps inside one
window, a sweep after a worker restart, and a sweep that overlaps the previous one all
converge on exactly one job per occurrence.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..database import ChangeWindow, Job, JobSchedule, User
from . import (agent_ansible_meta, ansible_run_meta, change_window,
               change_window_service, job_service)
from .suspend_schedule import ScheduleError

logger = logging.getLogger(__name__)

# The two Ansible key sets are IMPORTED, never restated. `ansible_run_meta.RUN_META_KEYS`
# is the closed allowlist the run path itself reads, so a key added there is a key a
# schedule may carry, automatically and without a second edit that somebody would
# eventually forget. `description` rides along because /jobs renders it as the job's
# label and the runners ignore it.
_ANSIBLE_KEYS = set(ansible_run_meta.RUN_META_KEYS) | {"description"}
_AGENT_ANSIBLE_KEYS = set(agent_ansible_meta.RUN_META_KEYS) | {"description"}

# **The allowlist is per-KEY, not per-type, and that distinction is the whole safety
# argument.** A type-only list said "this job type may repeat" and then copied the
# metadata dict verbatim — but the dict a schedule copies is the POST-RUN one.
# `job_service.set_completed` MERGES each runner's result into `Job.extra_data`, so what
# gets stored is whatever the runner wrote back, not what the endpoint created.
#
# That is not a theoretical gap. `epml_sync`'s create-time metadata is two strings; its
# COMPLETED metadata carries BeyondTrust pre-signed download URLs that expire in about
# thirty minutes. A type-only list would have copied them into `job_schedules.payload`
# and left them there indefinitely — a pre-signed URL and a one-shot handle, both of
# which this comment's predecessor explicitly warned about, smuggled in by the one route
# nobody thought to check.
#
# Filtering to named keys makes every type safe BY CONSTRUCTION rather than by audit: a
# runner result key can never reach the payload, because it was never named here. It also
# means the next contributor cannot widen the blast radius by adding a type — they have
# to say which keys, next to this paragraph.
#
# Membership in this dict is what makes a type schedulable, so the two cannot drift.
#
# What earns a key a place: it must be a REFERENCE the runner reads, still meaningful a
# month later. Not a secret value, not a one-shot handle (an upload id, a pre-signed URL,
# a checked-out lease), not a result.
SCHEDULABLE_PAYLOAD_KEYS = {
    # Config Management. `ansible_run_meta.RUN_META_KEYS` is already a closed allowlist
    # of refs and is reused verbatim rather than restated — the two drifting apart is
    # exactly the failure this whole mechanism exists to prevent. `description` is the
    # human label /jobs renders.
    "ansible_local": frozenset(_ANSIBLE_KEYS),
    "ansible_cloud_run": frozenset({
        "description", "target_kind", "target_id", "cloud", "asset",
        "asset_backend", "extra_vars", "secret_vars",
    }),
    "agent_ansible": frozenset(_AGENT_ANSIBLE_KEYS),
    # Power. References only — an instance id and its placement — and the runners echo
    # back exactly what they were given, so there is no result key to exclude.
    "ec2_power": frozenset({"action", "instance_id", "region", "deploy_job_id",
                            "unmanaged"}),
    "azure_power": frozenset({"action", "vm_name", "resource_group", "deploy_job_id",
                              "unmanaged"}),
    "gce_power": frozenset({"action", "instance_name", "zone", "project_id",
                            "deploy_job_id"}),
    "oci_power": frozenset({"action", "instance_ocid", "deploy_job_id"}),
    # Image export. A source image id plus placement; the runner merges export details
    # and a registered image id on completion, and the filter drops both.
    "aws_export_image": frozenset({"ami_id", "registry_name", "region", "created_by"}),
    "azure_export_image": frozenset({"image_name", "registry_name", "resource_group",
                                     "os_type", "created_by"}),
    "gcp_export_image": frozenset({"image_name", "registry_name", "project_id",
                                   "created_by"}),
    # NB: `oci_export_image` is deliberately absent. It is dispatched by the worker but
    # no endpoint ever creates one — it only arises inline from `packer_oci_build` — so
    # there is no job to repeat from.
    #
    # Image promotion. Every key is a reference to a durable registry row.
    "image_promote_aws": frozenset({"image_id", "image_name", "image_version",
                                    "target_cloud", "target_region"}),
    "image_promote_azure": frozenset({"image_id", "image_name", "image_version",
                                      "target_cloud", "target_region",
                                      "target_resource_group"}),
    "image_promote_gcp": frozenset({"image_id", "image_name", "image_version",
                                    "target_cloud", "target_region"}),
    "image_promote_oci": frozenset({"image_id", "image_name", "image_version",
                                    "target_cloud", "target_region"}),
    # EPM for Linux package sync. The runner reads `backend` and nothing else; the two
    # keys here are the entire safe payload, and the filter is what keeps the completed
    # job's pre-signed package URLs out of it.
    "epml_sync": frozenset({"description", "backend"}),
}

#: Kept for callers that only ask "may this type repeat at all".
SCHEDULABLE_JOB_TYPES = frozenset(SCHEDULABLE_PAYLOAD_KEYS)

# Consecutive failures before a schedule switches itself off. A recurring change that
# fails every week forever is worse than one that stops: it trains people to ignore the
# notification, and by the time it matters nobody is reading it.
MAX_CONSECUTIVE_FAILURES = 3


def _utcnow() -> datetime:
    return datetime.utcnow()


def payload_for(job: Job) -> dict:
    """The job's metadata, filtered to the keys its type is allowed to replay.

    The filter, not the type check, is what makes a stored payload safe: the dict this
    reads is the POST-RUN one, because ``job_service.set_completed`` merges each
    runner's result into it. So the job being copied carries whatever the runner wrote
    back — Ansible output, an export's registered image id, `epml_sync`'s pre-signed
    package URLs — none of which the runner reads on the way IN, and none of which
    should outlive the run.

    Dropping an unknown key rather than refusing it is deliberate. A result key is not
    an error, it is just not input; refusing would make every successful job
    un-repeatable the moment its runner learned to record something new.
    """
    allowed = SCHEDULABLE_PAYLOAD_KEYS.get(job.job_type)
    if allowed is None:
        return {}
    return {k: v for k, v in (job.metadata_dict or {}).items() if k in allowed}


def schedulable_reason(job: Job) -> str:
    """"" if this job may be turned into a recurring schedule, else why not."""
    if job is None:
        return "that job does not exist"
    if job.job_type not in SCHEDULABLE_JOB_TYPES:
        return (f"{job.job_type} jobs cannot be scheduled to repeat. Repeating a job "
                f"re-uses its saved parameters, which is only safe for job types whose "
                f"parameters are references rather than values.")
    if job.agent_id and not job.metadata_dict:
        return "that job has no saved parameters to repeat"
    return ""


def create_from_job(db: Session, *, job: Job, name: str, change_window_id: str,
                    created_by: str, approval_required: Optional[bool] = None
                    ) -> JobSchedule:
    """Copy a job into a recurring schedule against a change window.

    Raises :class:`ScheduleError` with an operator-facing message. The window is resolved
    here rather than at materialise time so a schedule cannot be created against one that
    does not exist or cannot recur.
    """
    refusal = schedulable_reason(job)
    if refusal:
        raise ScheduleError(refusal)

    window = change_window_service.get(db, change_window_id)
    if window is None:
        raise ScheduleError("pick a change window for this to repeat in")
    if window.enabled is False:
        raise ScheduleError(f"the change window {window.name!r} is disabled")
    # Fail now if the window cannot produce an occurrence, rather than creating a
    # schedule that silently never fires.
    change_window.next_occurrence(window, _utcnow())

    clean = (name or "").strip()
    if not clean:
        raise ScheduleError("give this schedule a name so it can be found later")

    row = JobSchedule(
        name=clean,
        change_window_id=window.id,
        job_type=job.job_type,
        workgroup=job.workgroup,
        agent_id=job.agent_id,
        vm_path=job.vm_path,
        approval_required=(job.approval_required if approval_required is None
                           else bool(approval_required)) or None,
        created_by=created_by,
        enabled=True,
        # Armed, not fired. Stamping the CURRENT occurrence means a schedule created at
        # 03:00 inside tonight's window does not immediately materialise a second copy of
        # the job the operator just ran — it starts from the NEXT occurrence. Same arming
        # rule the suspend schedule and the auto-delete timer follow, and for the same
        # reason: switching something on must never make a backlog eligible at once.
        last_materialised_for=change_window.next_occurrence(window, _utcnow())[0],
    )
    row.payload_dict = payload_for(job)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def due(db: Session, now: datetime) -> list:
    """``[(schedule, window, start, end)]`` for every schedule with an occurrence to fire.

    An occurrence is due when its window is OPEN now and this schedule has not already
    produced a job for that occurrence. Comparing against the window's start instant —
    rather than counting ticks or looking at `last_run_at` — is what makes a sweep
    idempotent across restarts and overlapping passes.
    """
    out = []
    rows = (db.query(JobSchedule)
            .filter(JobSchedule.enabled.isnot(False))
            .all())
    for row in rows:
        window = (db.query(ChangeWindow)
                  .filter(ChangeWindow.id == row.change_window_id).first())
        if window is None or window.enabled is False:
            continue
        try:
            start, end = change_window.next_occurrence(window, now)
        except ScheduleError:
            # A misconfigured window is reported on the /schedules page, not raised here
            # — one broken row must not stop every other schedule from firing.
            continue
        # `next_occurrence` returns the occurrence that has not CLOSED yet, which may not
        # have opened. Only fire once it is actually open.
        if start > now:
            continue
        if row.last_materialised_for is not None and row.last_materialised_for >= start:
            continue
        out.append((row, window, start, end))
    return out


def materialise(db: Session, row: JobSchedule, *, start: datetime, end: datetime
                ) -> Optional[Job]:
    """Create this occurrence's job. Returns it, or None if the schedule was skipped.

    ``last_materialised_for`` is stamped WHETHER OR NOT a job was created, so a schedule
    that is skipped for a reason that will not change within the window (its owner is
    gone) does not re-evaluate on every sweep for the next four hours.
    """
    owner_gone = _owner_unusable(db, row)
    row.last_materialised_for = start
    if owner_gone:
        disable(db, row, owner_gone)
        return None

    job = job_service.create_job(
        db,
        job_type=row.job_type,
        created_by=row.created_by or "system",
        vm_path=row.vm_path,
        workgroup=row.workgroup,
        metadata=row.payload_dict,
        agent_id=row.agent_id,
        # The occurrence is booked for the window it belongs to, so it queues behind the
        # same gate a hand-booked change does — including being marked missed if the
        # window closes before a worker gets to it.
        scheduled_for=start,
        window_ends_at=end,
        change_window_id=row.change_window_id,
        job_schedule_id=row.id,
        approval_required=bool(row.approval_required),
    )
    row.last_run_at = _utcnow()
    row.last_job_id = job.id
    return job


def _owner_unusable(db: Session, row: JobSchedule) -> str:
    """Why this schedule's owner can no longer authorise a run, or "".

    A recurring change outliving the authority that created it is the risk this guards.
    The occurrences are created as ``created_by``, inherit that identity in the audit
    log, and — for Config Management — resolve that person's secret references, so a
    schedule left running after someone leaves is a departed employee's access still
    being exercised weekly.
    """
    username = (row.created_by or "").strip()
    if not username or username == "system":
        return ""
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        return (f"the user who created this schedule ({username}) no longer exists, so "
                f"there is no identity to run it as")
    if getattr(user, "is_active", True) is False:
        return (f"the user who created this schedule ({username}) is deactivated, so it "
                f"will not run until an active user takes it over")
    return ""


def note_result(db: Session, job: Job) -> None:
    """Fold a finished occurrence's outcome back into its schedule.

    Called from the sweep rather than from the job's own completion path, so a schedule
    cannot add latency or a failure mode to the job runner. That means the count trails
    reality by up to one sweep, which is the right trade: this drives a self-disable, not
    anything time-critical.
    """
    if not job.job_schedule_id:
        return
    row = db.query(JobSchedule).filter(JobSchedule.id == job.job_schedule_id).first()
    if row is None:
        return
    if job.status == "completed":
        row.consecutive_failures = 0
        return
    if job.status not in ("failed", "cancelled"):
        return
    row.consecutive_failures = int(row.consecutive_failures or 0) + 1
    if row.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        disable(db, row,
                f"disabled automatically after {row.consecutive_failures} consecutive "
                f"failed runs — the last was job {job.id}")


def disable(db: Session, row: JobSchedule, reason: str) -> None:
    """Switch a schedule off and record why, once.

    The reason is the whole point: a schedule that simply stopped firing is something an
    operator discovers from the thing it was supposed to be maintaining.
    """
    if row.enabled is False:
        return
    row.enabled = False
    row.disabled_reason = reason
    logger.warning("job schedule %s (%s) disabled: %s", row.id, row.name, reason)
    _notify_disabled(db, row, reason)


def _notify_disabled(db: Session, row: JobSchedule, reason: str) -> None:
    try:
        from . import notification_service, notify_policy
        notification_service.emit_safe(db, notify_policy.NotificationEvent(
            event_type="job.window_missed",
            title=f"Recurring change disabled: {row.name}",
            body=reason,
            resource_id=f"jobschedule:{row.id}",
            resource_name=row.name,
            workgroup=row.workgroup or "",
            fields={"schedule_id": row.id, "job_type": row.job_type,
                    "created_by": row.created_by or ""},
            url="/schedules",
        ))
    except Exception:  # noqa: BLE001 — a notification must never break the sweep
        logger.warning("could not emit disable notice for schedule %s", row.id,
                       exc_info=True)


def describe(db: Session, row: JobSchedule) -> dict:
    """One schedule, as the /schedules page renders it."""
    window = (db.query(ChangeWindow)
              .filter(ChangeWindow.id == row.change_window_id).first())
    out = {
        "id": row.id,
        "name": row.name,
        "job_type": row.job_type,
        "workgroup": row.workgroup or "",
        "created_by": row.created_by or "",
        "enabled": row.enabled is not False,
        "disabled_reason": row.disabled_reason or "",
        "consecutive_failures": int(row.consecutive_failures or 0),
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else "",
        "last_job_id": row.last_job_id or "",
        "description": (row.payload_dict.get("description") or ""),
        "window_name": window.name if window else "",
        "window_summary": "",
        "next_run": "",
        "problem": "",
    }
    if window is None:
        out["problem"] = ("the change window this repeats in has been deleted — pick "
                          "another, or delete this schedule")
        return out
    if window.enabled is False:
        out["problem"] = (f"the change window {window.name!r} is disabled, so this will "
                          f"not run")
    try:
        out["window_summary"] = change_window.describe(window)["summary"]
        start, _end = change_window.next_occurrence(window, _utcnow())
        out["next_run"] = start.isoformat()
    except ScheduleError as exc:
        out["problem"] = str(exc)
    return out
