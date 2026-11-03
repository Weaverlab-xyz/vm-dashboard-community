"""Job queue models: jobs, their logs, change windows and schedules."""
import json
import uuid
from typing import Optional
from sqlalchemy import Column, String, Integer, DateTime, Boolean, Text, ForeignKey, UniqueConstraint, Float

from ...utils.clock import utcnow
from .._engine import Base


class Job(Base):
    """Job model for tracking long-running operations"""
    __tablename__ = "jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_type = Column(String(50), nullable=False, index=True)  # 'ec2_deploy', 'agent_hypervisor', etc.
    workgroup = Column(String(50), index=True)
    vm_path = Column(Text)
    # Cloud SDK resource id (EC2 instance id, Azure VM name, GCP instance id) for
    # cloud-deploy jobs. Indexed so the reassign endpoints can find the originating
    # Job row when an admin rewrites a resource's Workgroup tag/label.
    cloud_resource_id = Column(String(255), index=True, nullable=True)
    # Groups the jobs of one bulk Config-Management run (api/config_mgmt.run_playbook_bulk
    # fans a single asset out to N targets, one job each). A column rather than a key in
    # extra_data for the same reason as cloud_resource_id above: extra_data is a Text
    # column holding a JSON string, so there is no operator that filters it portably
    # across SQLite and PostgreSQL — only a LIKE scan.
    batch_id = Column(String(32), index=True, nullable=True)
    # The enrolled remote agent that owns this row's execution, or NULL for the local
    # job runner. A real indexed column for the same reason as batch_id above: the
    # lease query is `WHERE status='queued' AND agent_id=:id` on every poll, and
    # extra_data cannot be filtered portably.
    #
    # Setting this forces status='queued' in job_service.create_job, which is what
    # keeps jobs_worker._claim_one (status='pending') from racing the agent for the row.
    agent_id = Column(String(36), ForeignKey("remote_agents.id", ondelete="SET NULL"),
                      index=True, nullable=True)
    # An unattended check-in: this row exists because something polled an external
    # system on a timer, not because anyone asked for a change. Stamped in
    # job_service.create_job from job_service.CHECKIN_VERBS, and read only by
    # list_jobs, which hides the COMPLETED ones by default.
    #
    # A real indexed column for the third time on this table, and for the same reason
    # as batch_id and agent_id above: the thing that tells a hypervisor check-in apart
    # from an operator's power op is `verb` inside extra_data, and extra_data is a Text
    # column holding JSON that no operator filters portably across SQLite and
    # PostgreSQL. It cannot be job_type instead — an inventory_sync and a power_on are
    # both `agent_hypervisor`, deliberately (one agent handler, one grant), so hiding
    # by type would hide every power op an operator ever ran.
    #
    # NULL on every pre-existing row, and NULL means "not a check-in", i.e. SHOWN. The
    # additive direction is the safe one here: adding the column cannot make a row that
    # was visible yesterday disappear today.
    is_checkin = Column(Boolean, nullable=True, index=True)
    # Auto-delete timer. Meaningful ONLY on the cloud VM deploy types
    # (expiry_policy.REAPABLE_VM_JOB_TYPES) — a VM has no inventory table of its own,
    # so its deploy Job row IS its record of existence, which is why `job:<id>` is
    # already its inventory id. NULL and ignored on every other job row.
    #
    # NULL means "no expiry, never auto-deleted" — NOT "inherit the global default".
    # Same meaning PersonalAccessToken.expires_at already carries. That is the
    # load-bearing safety property of the whole feature: every row that predates the
    # column is NULL, so enabling auto-delete on an existing fleet selects nothing,
    # by construction rather than by a guard.
    expires_at = Column(DateTime, nullable=True, index=True)
    # Set by the sweeper before it records an impending-deletion warning, so a warning
    # fires once. Read only by the server-side warning channels — deliberately NOT by
    # the dashboard's "Needs attention" item, which is derived client-side on every
    # poll and has its own per-browser dismissal.
    expiry_warned_at = Column(DateTime, nullable=True)
    # ── Suspend schedule ──────────────────────────────────────────────────────
    # Business-hours power windows for a cloud VM. On the deploy job row for the same
    # reason expires_at is: a VM has no inventory table, so its deploy job IS its record
    # of existence. NULL throughout = no schedule, which is what every existing row
    # backfills to — enabling the feature on an existing fleet selects nothing, by
    # construction rather than by a guard.
    #
    # Read by services/suspend_schedule.py, which is duck-typed on exactly these five
    # attributes and shares them with PovEnvironment. Keeping the names identical is what
    # lets one policy module serve both without knowing which it was handed.
    suspend_at_local = Column(String(5), nullable=True)      # "HH:MM", local to the tz below
    resume_at_local = Column(String(5), nullable=True)       # "HH:MM"; NULL = never wake it
    schedule_timezone = Column(String(64), nullable=True)    # IANA, e.g. Europe/London
    schedule_days = Column(String(7), nullable=True)         # 7 chars, Monday first, '1' = on
    # When the schedule was last evaluated. The rule is BOUNDARY CROSSED, not "should it
    # be asleep now" — so this latch is what stops an operator who starts a VM by hand at
    # 20:00 from having it suspended again four minutes later, forever. NULL means never
    # checked, and due_action refuses to act on that first pass.
    schedule_last_checked_at = Column(DateTime, nullable=True)
    # ── Spend cap (services/spend_policy.py, swept by services/spend_sweeper.py) ──
    # The same five attributes PovEnvironment carries, spelled identically for the same
    # reason the schedule columns are: one pure policy module serves both and never knows
    # which row it was handed.
    #
    # NULL cap = no cap, and the sweep's query filters on `spend_cap_usd IS NOT NULL`. That
    # is not only a semantic — it is what keeps accrual off this table. `jobs` is polled by
    # `_claim_one` every two seconds, and an estate that sets no caps must pay nothing for
    # the feature existing.
    spend_cap_usd = Column(Float, nullable=True)             # USD; NULL = uncapped
    spend_estimate_usd = Column(Float, nullable=True, default=0.0)   # accrued list price
    # NULL means never measured. accrue() returns without billing on that first pass, so
    # enabling the feature cannot invent a bill for every hour since the row was created —
    # the same arming rule schedule_last_checked_at follows above.
    spend_accrued_at = Column(DateTime, nullable=True)
    spend_warned_at = Column(DateTime, nullable=True)        # latch: warned once
    spend_capped_at = Column(DateTime, nullable=True)        # latch: acted on the cap once
    # ── Retry (services/retry_policy.py) ──────────────────────────────────────
    # NO new status. `failed` stays terminal and `ACTIVE_STATUSES` is unchanged, because
    # 108 sites in this tree compare against "failed" and the first one missed would be a
    # job some page silently stops showing. A requeue writes `pending` back with a
    # `retry_after`; an exhausted job is `failed` with attempts > 0, so the dead-letter
    # tail is a QUERY rather than a fourth state everything has to learn.
    attempts = Column(Integer, nullable=False, default=0)
    # NULL = claimable now, which is every row that exists today and every row that never
    # fails. `jobs_worker._claim_one` reads it; nothing else may write it.
    retry_after = Column(DateTime, nullable=True)
    # ── Change window / scheduler (services/change_window.py, schedule_sweeper.py) ──
    # "Run this during the approved window, not now." The whole engine is these columns
    # plus one clause in the two claim queries — see job_service.claimable_now.
    #
    # NO new status here either, for the reason stated above. A scheduled job is
    # `pending` with a future `scheduled_for`; a job that never got to run is
    # `cancelled` with `missed_window_at` set. Both are QUERIES over states everything
    # already understands, which is what stops a scheduled row from vanishing out of
    # every page that filters on status.
    #
    # NULL throughout = "behave exactly as before", which is what every row that
    # predates these columns backfills to. That is the load-bearing safety property:
    # turning the scheduler on cannot hold back a single existing job, by construction
    # rather than by a guard. Same meaning `expires_at` carries above.
    scheduled_for = Column(DateTime, nullable=True, index=True)   # naive UTC; NULL = run now
    window_ends_at = Column(DateTime, nullable=True)              # NULL = no hard boundary
    # Provenance, for display only — nothing authorizes off either. A job keeps running
    # to the times copied onto it above even if the window or schedule is later edited
    # or deleted, which is why these are plain ids and not ForeignKeys: an operator
    # tidying up old windows must never mutate or delete history.
    change_window_id = Column(String(36), nullable=True)
    job_schedule_id = Column(String(36), nullable=True, index=True)
    # STORED, not derived from current policy. If approval were re-read at claim time, an
    # admin turning the requirement on would freeze every already-pending job, and turning
    # it off would release changes nobody approved. What the job was created under governs.
    #
    # NULLABLE, and NULL means False. The migration below adds this as a bare BOOLEAN with
    # no DEFAULT clause, because PostgreSQL has rejected a defaulted ADD COLUMN in this
    # list before and each statement runs in its own savepoint — so the rollback is SILENT
    # and the column simply never appears, while SQLite tolerates it and every test stays
    # green. Every pre-existing row therefore reads NULL, and the claim predicate spells
    # this as `approval_required IS NOT TRUE` rather than `== False` so NULL passes.
    approval_required = Column(Boolean, nullable=True, default=False)
    approved_at = Column(DateTime, nullable=True)
    approved_by = Column(String(100), nullable=True)
    # Set by schedule_sweeper when a window closed before the job was ever claimed. The
    # row is `cancelled` — it never ran and nothing broke, so `failed` would be a lie and
    # would also put it in the dead-letter tail, which is for exhausted retries.
    missed_window_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="pending", index=True)  # pending, running, completed, failed, cancelled
    progress_pct = Column(Integer, default=0)
    progress_message = Column(Text)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    # Heartbeat — bumped on every status/progress write (incl. streamed terraform
    # output); the startup reconcile uses it to tell a live job from one orphaned
    # by an app restart.
    updated_at = Column(DateTime)
    created_by = Column(String(100), index=True)  # Username
    error_message = Column(Text)
    extra_data = Column(Text)  # JSON string for flexible storage

    @property
    def metadata_dict(self) -> dict:
        """Parse JSON extra_data into dict"""
        if not self.extra_data:
            return {}
        try:
            return json.loads(self.extra_data)
        except:
            return {}

    @metadata_dict.setter
    def metadata_dict(self, value: dict):
        """Set extra_data from dict"""
        self.extra_data = json.dumps(value)

    @property
    def duration_seconds(self) -> Optional[int]:
        """Calculate job duration in seconds"""
        if self.started_at and self.completed_at:
            return int((self.completed_at - self.started_at).total_seconds())
        elif self.started_at:
            return int((utcnow() - self.started_at).total_seconds())
        return None


class JobLog(Base):
    """Per-line Live Output for a job, persisted so a separate worker process's
    terraform stream reaches WS clients connected to gunicorn (which poll the DB),
    and so a reconnecting client can replay the full output. Append-only; the
    dedicated job runner is the sole writer per job_id (one monotonic seq)."""
    __tablename__ = "job_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), index=True, nullable=False)
    seq = Column(Integer, nullable=False)
    line = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("job_id", "seq", name="uq_job_log_seq"),)


class ChangeWindow(Base):
    """A named, recurring period during which changes are allowed to START.

    "Saturday 02:00–06:00, New York" defined once by an administrator, then chosen by
    name on any run form. The alternative — every operator typing a date and time — gets
    the timezone wrong, gets the DST weekend wrong, and gives nobody a way to answer
    "what is our change window?" in one place.

    **The times are a RECURRENCE, not a date.** A window is "every Saturday", and
    ``change_window.next_occurrence`` resolves it against a clock to get the concrete
    UTC pair stamped onto a job. Those instants are then COPIED onto the job row
    (``Job.scheduled_for`` / ``Job.window_ends_at``), never looked up through this table
    at claim time — so editing a window does not silently move changes already booked
    into it, and deleting one does not strand them. ``Job.change_window_id`` is
    consequently a plain id and not a ForeignKey: it is provenance for a label, and the
    history must survive an administrator tidying up.

    Field shapes deliberately match ``Job``'s suspend-schedule block and
    ``services/suspend_schedule.py``'s duck-typed attributes — ``"HH:MM"``, an IANA zone
    name, seven Monday-first characters — so one set of validation and day-walking
    primitives serves both rather than two near-identical parsers drifting apart.
    """
    __tablename__ = "change_windows"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(120), nullable=False, unique=True)
    description = Column(Text)
    # Local to `timezone`, NOT UTC. An operator states a window in the timezone the
    # business runs in; storing it as UTC would silently move it by an hour twice a year.
    start_at_local = Column(String(5), nullable=False)      # "HH:MM", 24-hour
    duration_minutes = Column(Integer, nullable=False)
    # IANA, e.g. "America/New_York". Blank/NULL resolves to UTC rather than the server's
    # local zone — a container's local zone is an accident of its base image, and a
    # window that moved after a rebuild is a bug nobody attributes to the right cause.
    timezone = Column(String(64))
    schedule_days = Column(String(7), nullable=False, default="1111111")  # Mon first
    # A disabled window stays selectable on jobs ALREADY booked into it (they carry their
    # own copied instants) but is not offered for new ones. Disabling is how a window is
    # retired without rewriting history.
    enabled = Column(Boolean, nullable=True, default=True)
    created_by = Column(String(100))
    created_at = Column(DateTime, default=utcnow, nullable=False)


class JobSchedule(Base):
    """A job that should be created again on every occurrence of a change window.

    **What is stored is the JOB ROW, not the request that made it.** ``job_type`` plus
    ``payload`` (the job's metadata dict) is exactly what ``job_service.create_job``
    takes, so materialising an occurrence is one call and the worker's dispatch table
    needs to know nothing about schedules. It also means a recurring schedule works for
    every job type in ``jobs_worker.HANDLED_TYPES`` the day it is added, rather than only
    for the forms somebody remembered to wire up.

    That is why a schedule is created FROM AN EXISTING JOB rather than from a form of its
    own. Re-deriving a payload through a second, parallel path is how the two drift, and
    the drift would surface as a recurring change that does something subtly different
    from the one-off the operator tested. Copying a job that has already been validated,
    authorized and (usually) run once removes the second path entirely.

    **A payload is refs, never secrets.** That is a property of the job types this can
    copy, not something enforced here: an ``ansible_local`` payload holds secret
    REFERENCES resolved at run time (see ``services/ansible_run_meta``). A job type that
    put a credential in its metadata would be unsafe to schedule, which is why
    ``schedule_service`` keeps an allowlist rather than accepting any job row.

    Recurrence is by ``change_window_id`` only — no free-form cron. A recurring change
    that does not land in an approved window is the thing change control exists to
    prevent, and "every Tuesday at 3am" with no window is that. The window supplies both
    the start and the deadline.
    """
    __tablename__ = "job_schedules"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(160), nullable=False)
    # NULL is not possible in practice (the API requires one) but the column stays
    # nullable so a window deleted out from under a schedule degrades to "disabled and
    # visibly broken" rather than breaking the row's readability.
    change_window_id = Column(String(36), index=True)
    job_type = Column(String(50), nullable=False)
    payload = Column(Text)                       # JSON; the job's metadata dict
    workgroup = Column(String(50))
    # Copied from the source job. A schedule for an agent-executed job must keep
    # producing agent-executed jobs, or the occurrences would silently be handed to the
    # local worker, which has no route to the target at all.
    agent_id = Column(String(36))
    vm_path = Column(Text)
    approval_required = Column(Boolean, nullable=True, default=False)
    # Whose authority the occurrences run under. A schedule outliving the person who
    # created it is a real risk; schedule_service refuses to materialise for a user who
    # is gone or deactivated, rather than quietly running as nobody.
    created_by = Column(String(100), index=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    # NULL/True = active. Real column, not a payload key: the sweep filters on it every
    # pass, and database.py's own note on `batch_id` explains why anything queried must
    # not live inside a JSON Text column.
    enabled = Column(Boolean, nullable=True, default=True)
    # Bookkeeping the /schedules page renders and the sweep writes.
    #
    # `last_materialised_for` is the load-bearing one: it holds the window START instant
    # of the most recent occurrence turned into a job, and is what makes materialising
    # idempotent. Two sweeps in one window — or a sweep after a restart — compare against
    # it and do nothing, rather than firing the same change twice.
    last_materialised_for = Column(DateTime)
    last_run_at = Column(DateTime)
    last_job_id = Column(String(36))
    # Consecutive failures. A recurring change that fails every week forever is noise
    # that trains people to ignore the alert, so the schedule disables itself and says so.
    consecutive_failures = Column(Integer, nullable=False, default=0)
    disabled_reason = Column(Text)

    @property
    def payload_dict(self) -> dict:
        if not self.payload:
            return {}
        try:
            return json.loads(self.payload)
        except Exception:  # noqa: BLE001 — a corrupt payload must not break the page
            return {}

    @payload_dict.setter
    def payload_dict(self, value: dict):
        self.payload = json.dumps(value)
