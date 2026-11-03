"""
Database models and session management
"""
import json
import uuid
from datetime import datetime
from typing import Optional, List
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Boolean, Text, LargeBinary, ForeignKey, UniqueConstraint, Float, text
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool, QueuePool

from ..config import settings
from ..utils.clock import utcnow
from ._engine import (  # noqa: F401  (re-exported: callers import these from web_dashboard.database)
    Base, SessionLocal, _is_sqlite, _pool_kwargs, engine, engine_url,
)
from .passwords import (  # noqa: F401  (re-exported: callers import these from web_dashboard.database)
    BCRYPT_MAX_BYTES, bcrypt_input, check_password_length, get_password_hash,
    password_length_error, verify_password,
)
from .rbac import (  # noqa: F401  (re-exported: callers import these from web_dashboard.database)
    _ROLE_UNRESOLVED, merge_permission_maps, narrow_to_token_scope,
)

# How long init_db's DDL transaction may wait for a table lock before giving up on one
# statement. Deliberately short: the migrations are additive and idempotent, so skipping
# one costs a boot, while WAITING costs the whole app (a pending ACCESS EXCLUSIVE on
# `jobs` blocks every later reader of it -- see the comment at the SET LOCAL in init_db).
# Not a settings field on purpose: it must hold before any config can be read out of the
# database, which is the very thing init_db is creating.
_DDL_LOCK_TIMEOUT_MS = 4000


def pool_capacity() -> int:
    """Max connections this process's pool can hand out — what jobs_worker._limits clamps
    concurrency against. 0 for SQLite's NullPool, which is unbounded (a fresh connection
    per Session), so the caller treats 0 as "don't clamp"."""
    if _is_sqlite:
        return 0
    return settings.db_pool_size + settings.db_max_overflow



# ========== DATABASE MODELS ==========

from .models.identity import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    User, Fido2Credential, PersonalAccessToken,
    OAuthClient, ExternalWorkloadIdentity, RunnerCredentialGrant,
    SpiffeTrustDomain, SpireAdminCall, SpireAdminRelay,
    LoginAttempt,
)


class EphemeralState(Base):
    """One short-lived, single-use, opaque server-side value: a FIDO2/WebAuthn
    ceremony challenge, or an OAuth/OIDC CSRF ``state`` (with its PKCE verifier).

    In the database for the same reason as :class:`LoginAttempt` above, and it is
    the same bug that motivated that one. This started life as a module-level dict
    in ``services/fido2_service`` guarded by a ``threading.Lock`` — which is the
    correct guard for the wrong hazard. The app runs ``gunicorn -w 2``, so a lock
    makes the dict safe against the *threads* of one worker while leaving the two
    worker **processes** with a private copy each.

    Every ceremony this holds state for is split across two requests — begin/complete
    for FIDO2, login/callback for OAuth — and nothing pins a browser to a worker. So
    the second leg landed on the process that had never stored the state roughly half
    the time, and the user got ``/login?error=invalid_state`` or "Invalid or expired
    FIDO2 challenge" with nothing wrong on either side. More replicas, worse odds.

    **Rows are deleted on read, and the delete is the lock.** These values are
    single-use by definition: a CSRF state that survives its first presentation is
    not a CSRF defence. Concurrent consumers race on ``DELETE ... WHERE key = ?``
    and only the one whose rowcount is 1 gets the value — the same portable
    atomic-claim the job queue uses, no ``SKIP LOCKED``.
    """
    __tablename__ = "ephemeral_state"

    # Opaque and namespaced by the service that owns it (`vmcli:oauth:state:<uuid>`,
    # `vmcli:fido2:challenge:<uuid>`), so one table serves both ceremonies without
    # either being able to consume the other's rows.
    key = Column(String(200), primary_key=True)
    # Text, not String(n): the OIDC entry packs the redirect URI and the PKCE
    # verifier together, and a FIDO2 entry is a serialised state dict.
    value = Column(Text, nullable=False, default="")
    # Wall clock, not time.monotonic() as the dict used — monotonic is per-process
    # and meaningless to a reader that did not start the same process.
    # Indexed because the sweep is a range scan on it.
    expires_at = Column(DateTime, nullable=False, index=True)


from .models.agents import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    RemoteAgent, AgentNonce, AgentEnrollAttempt,
    _json_list,
)


from .models.hypervisor import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    HypervisorConnection, HypervisorVMCache, ConfigMgmtRoute,
)


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


class NotificationEndpoint(Base):
    """One outbound webhook sink: a URL plus the payload shape to POST to it.

    Transport is always HTTP POST; `fmt` picks the body. `slack` and `teams` exist
    because those endpoints reject anything that isn't their own shape — a Slack
    incoming webhook wants {"text": ...} and a Teams Power Automate Workflows URL
    wants the Adaptive Card envelope. `custom` is the signed generic envelope, which
    is how email gets delivered here: point it at a Flow / automation platform and
    let that fan out. There is deliberately no SMTP client in this codebase.

    `url` and `secret` are Fernet-encrypted with the same key as app_config, because
    a Slack or Teams webhook URL *is* a bearer credential — anyone holding it can post
    to the channel. They are never returned by the API; the endpoints router hands out
    a scheme+host hint instead.
    """
    __tablename__ = "notification_endpoints"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)
    url = Column(Text, nullable=False)              # Fernet-encrypted
    fmt = Column(String(16), nullable=False, default="custom")   # custom | slack | teams
    secret = Column(Text)                           # Fernet-encrypted; HMAC key, custom only
    enabled = Column(Boolean, default=True, nullable=False)
    # CSV override of notify_event_types. Empty/NULL = inherit the global list, which
    # is what almost every endpoint wants — the per-endpoint filter exists so one noisy
    # sink can be narrowed without narrowing everyone.
    event_types = Column(Text)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100))
    last_success_at = Column(DateTime)
    # The verbatim transport error from the most recent failure. Kept on the endpoint
    # (not just on the delivery rows) so the Settings panel can show "this sink is
    # broken" without joining.
    last_error = Column(Text)


class NotificationDelivery(Base):
    """One outbound message attempt: one event × one endpoint.

    This table is simultaneously the outbox, the dedupe latch, the retry state, and
    the operator's record of what was sent — each of which alone would justify
    persisting it.

    The UNIQUE on `dedupe_key` is the only correct dedupe here: the app runs under
    `gunicorn -w 2` and the worker at `replicas: 3`, so an in-process set would be
    worthless across five processes. job_service.log_audit already absorbs an
    IntegrityError on a unique index the same way.

    Delivery is at-least-once, not exactly-once: a worker killed after the POST but
    before the `sent` write re-sends on the next reclaim. A duplicate alert beats
    silence, and pretending otherwise is how these systems rot.
    """
    __tablename__ = "notification_deliveries"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)

    # ── the event (denormalised: fan-out is a handful of rows, so a join buys nothing) ──
    event_type = Column(String(64), nullable=False, index=True)   # "resource.expiring"
    severity = Column(String(16), nullable=False, default="info")
    # inventory_service's id scheme ("job:<uuid>" / "clouddb:<id>" / "k8s:<id>"), so
    # expiry_reaper._resolve_row can already map a delivery back to its resource.
    resource_id = Column(String(128), index=True)
    resource_kind = Column(String(24))
    resource_name = Column(String(255))
    cloud = Column(String(24))
    region = Column(String(40))
    workgroup = Column(String(64), index=True)
    url = Column(Text)                              # deep link

    # ── routing ──
    endpoint_id = Column(String(36), index=True)
    channel = Column(String(32))                    # the fmt used; free-form on purpose
    # JSON. Phase 1 always writes {"route": "global_sink"}. Owner routing or a rules
    # engine writes {"route": "owner", ...} / {"route": "rule", "rule_id": ...} into
    # this same column with no schema change — which is why it beats a nullable FK to
    # a table that does not exist yet.
    reason = Column(Text)

    # ── what was sent ──
    subject = Column(Text)
    body = Column(Text)
    payload = Column(Text)                          # exact JSON posted (never holds the secret)

    # ── delivery state ──
    status = Column(String(16), nullable=False, default="pending", index=True)
    # pending | sending | sent | failed | dry_run | suppressed
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, index=True)
    # Exists solely so a row stuck in `sending` (worker SIGKILLed mid-POST) can be
    # reclaimed. Without it that notification is silently lost forever.
    claimed_at = Column(DateTime)
    sent_at = Column(DateTime)
    error = Column(Text)

    dedupe_key = Column(String(200), nullable=False)

    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_notification_dedupe"),)

    @property
    def reason_dict(self) -> dict:
        if not self.reason:
            return {}
        try:
            return json.loads(self.reason)
        except Exception:
            return {}

    @reason_dict.setter
    def reason_dict(self, value: dict):
        self.reason = json.dumps(value)


class AuditLog(Base):
    """Audit log for security-relevant operations"""
    __tablename__ = "audit_log"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    timestamp = Column(DateTime, default=utcnow, nullable=False, index=True)
    username = Column(String(100), index=True)
    action = Column(String(100), nullable=False, index=True)  # 'vm_start', 'vm_stop', 'user_login', etc.
    target_vm = Column(Text)
    details = Column(Text)  # JSON string
    ip_address = Column(String(45))  # IPv4 or IPv6

    # Tamper-evident hash chain (see services/audit_chain.py + /api/audit/verify).
    # Nullable so the ALTER-TABLE migration + one-time backfill can populate
    # pre-existing rows; uniqueness of seq is enforced via ix_audit_log_seq.
    seq = Column(Integer)                # global monotonic sequence
    prev_hash = Column(String(64))       # previous entry's entry_hash (genesis = "0"*64)
    entry_hash = Column(String(64))      # sha256 over this entry's fields + prev_hash

    @property
    def details_dict(self) -> dict:
        """Parse JSON details into dict"""
        if not self.details:
            return {}
        try:
            return json.loads(self.details)
        except:
            return {}

    @details_dict.setter
    def details_dict(self, value: dict):
        """Set details from dict"""
        self.details = json.dumps(value)


class ConfigApplyState(Base):
    """Per-(target, playbook) fingerprint of the last successful Ansible apply —
    powers config-drift visibility (backlog #5). `content_hash` fingerprints the
    applied asset bytes so a later edit is detectable; `applied_at` drives the
    'unverified since' staleness signal. Upserted by config_drift.record_apply."""
    __tablename__ = "config_apply_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    target = Column(String(255), nullable=False)
    playbook_ref = Column(String(255), nullable=False)
    content_hash = Column(String(64), nullable=False)   # sha256 of the applied asset bytes
    inputs_hash = Column(String(64))                    # sha256 of resolved extra_vars (one-way)
    applied_at = Column(DateTime, default=utcnow, nullable=False)
    job_id = Column(String(36))

    __table_args__ = (UniqueConstraint("target", "playbook_ref", name="uq_apply_target_playbook"),)


class OAuthGroupMapping(Base):
    """Maps an Entra ID group Object ID to a dashboard workgroup name."""
    __tablename__ = "oauth_group_mappings"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entra_group_id = Column(String(255), unique=True, nullable=False, index=True)
    display_name = Column(String(200), nullable=False)   # friendly label shown in the UI
    workgroup = Column(String(100), nullable=False)       # must match a key in settings.workgroups
    # Default permissions for auto-created users from this group. NULL = all permissions.
    default_permissions = Column(Text, nullable=True)

    # The focus this group confers, and the tie-break when a user matches several
    # mappings. Lowest priority number wins; NULL persona means this mapping expresses no
    # opinion, which is what lets a broad catch-all group grant a workgroup without also
    # dictating a focus. Ties and absent priorities fall back to the mapping's
    # display_name so the outcome is always deterministic rather than row-order dependent.
    persona = Column(String(32), nullable=True)
    persona_priority = Column(Integer, nullable=True)
    # The access role every member of this group is granted, unioned into their
    # `session_permissions` at login alongside `default_permissions` above. NULL = this
    # mapping confers no role, which is every mapping that predates the feature.
    #
    # Only an id, and deliberately no materialised copy of the map -- unlike `users`. The
    # column it feeds is rebuilt from scratch on EVERY login, so there is nothing to keep
    # in step and nothing to reconcile; the login path reads the role directly. The flip
    # side is that a role edit reaches group members at their next sign-in rather than
    # immediately, which is exactly how `default_permissions` has always behaved.
    # Plain column, no ForeignKey -- see the note on `users.role_id` for both reasons. This
    # one is not in the cycle, but an assignment that is constrained on one table and not
    # the other is a distinction no reader would guess and no test could hold.
    role_id = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class AccessRole(Base):
    """A named, reusable permission map that a user or a group mapping is assigned.

    `AccessRole` and not `Role` because three unrelated `role` columns already exist in this
    module -- `EntitleActivation.role` (an IAM policy name), `SpireLab.k8s_workload_role` and
    `PovCloudTemplateVM.role` ("broker"|"target"). Only this one is about dashboard RBAC.

    **`permissions` here inverts the meaning it has on `User`, and that is the single most
    important thing to know before editing this model.** On a user, NULL/`{}` means
    UNRESTRICTED. On a role it means the role grants NOTHING -- a role is a positive
    statement about access, so "no map" cannot sensibly mean "all access", and an
    unrestricted role would silently hand every assignee every scope added in future. The
    create endpoint therefore REQUIRES the field rather than defaulting it, and the Roles
    tab does not offer the grid's "Full access (unrestricted)" escape hatch at all.
    "Everything" is spelled with the admin flag, which is auditable.

    `slug` is the stable handle the built-in seeds are keyed by; it is derived from `name`,
    so a duplicate name yields a duplicate slug and a 409 without a second constraint.
    `is_builtin` can be a real Boolean with a default because this is a NEW table made by
    `create_all` -- the `BOOLEAN DEFAULT 0` trap only bites an `ALTER TABLE` in the
    legacy list (schema_legacy.py), where SQLite accepts an integer default on a boolean
    and PostgreSQL rejects it, silently, inside its own savepoint.
    """
    __tablename__ = "access_roles"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    slug = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    permissions = Column(Text, nullable=True)
    is_builtin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    created_by_user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"),
                                nullable=True)

    @property
    def permissions_dict(self) -> dict:
        """The map this role grants. Empty means it grants nothing -- see the class
        docstring for why that is the opposite of `User.permissions_dict`."""
        if not self.permissions:
            return {}
        try:
            return json.loads(self.permissions)
        except Exception:
            return {}

    @permissions_dict.setter
    def permissions_dict(self, value: dict):
        self.permissions = json.dumps(value) if value else None


class Workgroup(Base):
    """User-managed workgroup: scopes RBAC + cloud resource visibility.

    `name` is canonical lowercase (regex enforced in service layer) so it can be
    written verbatim into AWS instance tags (`Workgroup=<name>`), Azure resource
    tags (`workgroup=<name>`), and GCP labels (`workgroup=<name>`) — all of which
    have casing/character restrictions tighter than the dashboard UI.

    Lookups in `workgroup_service` are case-insensitive so existing TitleCase
    strings in `users.workgroups` and `oauth_group_mappings.workgroup` keep
    resolving without a data migration. `display_name` preserves the original
    casing for UI rendering.
    """
    __tablename__ = "workgroups"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(64), unique=True, nullable=False, index=True)
    display_name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    local_vm_path = Column(Text, nullable=True)  # UNC path for VMware local VMs; null in community
    is_default = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by_user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # ── Required change window (services/admission_service._enforce_change_window) ──
    # "Nothing in this workgroup may be changed outside Prod Weekend." Unlike the
    # per-run picker, which an operator chooses, this one cannot be bypassed by
    # clicking Run now — the gate every mutating endpoint already calls refuses and
    # offers to book instead.
    #
    # BOTH NULL is what every existing row backfills to, and NULL means "as today":
    # no constraint, no behaviour change. A workgroup becomes constrained only when an
    # administrator both picks a window and ticks the requirement, so enabling the
    # feature on a live estate blocks nothing by construction rather than by a guard.
    #
    # A plain id rather than a ForeignKey, for the same reason `Job.change_window_id`
    # is: an administrator tidying up the maintenance calendar must never cascade into
    # a workgroup's configuration. A window that no longer exists reads as "misconfigured
    # — pick another", which is what the Workgroups tab shows.
    change_window_id = Column(String(36), nullable=True)
    # Bare BOOLEAN, NO DEFAULT, deliberately. PostgreSQL has rejected a defaulted ADD
    # COLUMN in this tree's migration list before and the savepoint swallowed it
    # silently — see the `users.is_admin` note. NULL reads as False everywhere.
    require_change_window = Column(Boolean, nullable=True)
    # The workstream's deploy defaults (services/workgroup_service.PROFILE_KEYS), as a
    # JSON object holding only the keys it sets. NULL = sets none: every VM it deploys
    # takes the global defaults, which is every workstream that predates the column.
    deploy_defaults = Column(Text, nullable=True)
    # Secret REFERENCES bound to the workstream by an administrator
    # (services/workgroup_service.SECRET_CLOUDS): the SSH key secret per cloud and a PRA
    # credential. Never a value. Kept apart from deploy_defaults because members edit
    # those and only an administrator may edit these. NULL = none bound.
    secret_bindings = Column(Text, nullable=True)
    # Config-Management assets an administrator chose to run on every VM deployed into
    # the workstream, in order (services/post_deploy_service): a JSON list of
    # {asset, asset_backend}. Administrator-only like secret_bindings, because each one
    # is code run on every new machine. NULL = none.
    post_deploy_runs = Column(Text, nullable=True)
    # Password Safe attributes every managed system onboarded from the workstream carries
    # (services/ps_workstream_attrs), so a customer's Smart Rule can filter on
    # "Workstream = X": a JSON object {"<AttributeType name>": "<value>"}. Administrator-
    # only like post_deploy_runs, because it writes into a customer's PAM tenant. NULL =
    # none, which is every workstream that predates the column.
    ps_attributes = Column(Text, nullable=True)
    # The subnet / security groups its deploy forms start from, per cloud and region
    # (services/workgroup_service.NETWORK_KEYS): {cloud: {region: {key: value}}}. A form
    # convenience, never applied by the server. NULL = none.
    network_defaults = Column(Text, nullable=True)


class StorageAssetScope(Base):
    """A stored asset an administrator has limited to one workstream
    (services/asset_scope_service).

    Assets live in buckets, not here, so this is a row per scoped asset keyed the way
    the Storage page names one: backend + name. No row = global, which is every asset
    that predates the table — creating it scopes nothing. A plain workgroup string, no
    ForeignKey, for the reason the cloud-database column has none; deleting a
    workstream that still scopes assets is refused instead (workgroup_service.delete).
    """
    __tablename__ = "storage_asset_scopes"

    backend = Column(String(32), primary_key=True)
    name = Column(String(255), primary_key=True)
    workgroup = Column(String(64), nullable=False, index=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    updated_by = Column(String(100), nullable=True)


class VMWorkgroupOverride(Base):
    """Workgroup assignments for VMs the dashboard didn't deploy itself.

    Cloud and Proxmox/Nutanix dashboard-driven deploys already record
    `workgroup` on the corresponding Job row, but VMs that pre-existed on the
    on-prem hypervisor — or live on a hypervisor with no deploy flow at all
    (Hyper-V, vSphere, XCP-ng) — have no Job to hang a workgroup off of.
    An admin assigns those via the bulk-assign action on the provider page,
    which writes a row here.

    `vm_id` is normalized per-provider in each *_service module's
    _override_key() helper: Proxmox uses "<node>/<vmid>", everything else
    uses the VM's native uuid/moref.
    """
    __tablename__ = "vm_workgroup_overrides"

    id = Column(Integer, primary_key=True)
    provider = Column(String(20), nullable=False, index=True)   # proxmox|nutanix|hyperv|vsphere|xcpng
    vm_id = Column(String(128), nullable=False)
    workgroup = Column(String(64), ForeignKey("workgroups.name", ondelete="CASCADE"), nullable=False, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("provider", "vm_id", name="uq_vm_workgroup_override"),)


class Approval(Base):
    """Entitle-brokered approval workflow state for gated endpoints.

    A row is created when a user calls a gated endpoint without an approval
    header; Entitle webhook moves status pending→approved/denied; the dep
    consumes the row on the user's retry call.
    """
    __tablename__ = "approvals"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entitle_request_id = Column(String(255), nullable=False, unique=True, index=True)
    action = Column(String(100), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    payload_hash = Column(String(64), nullable=False)
    # status values: pending | approved | denied | expired | consumed
    status = Column(String(20), nullable=False, default="pending", index=True)
    requested_at = Column(DateTime, default=utcnow, nullable=False)
    approved_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)
    denial_reason = Column(Text, nullable=True)

    # principal_kind: "user" for human-facing approvals (the original flow),
    # "machine" for cloud-identity JIT elevations issued by the dashboard
    # on its own behalf. Webhook handler is shared; this column lets policy
    # routing fork on identity type.
    principal_kind = Column(String(16), nullable=False, default="user", index=True)

    user = relationship("User")


class EntitleActivation(Base):
    """Per-cloud-write machine-identity elevation issued via Entitle.

    Phase 0 of the cloud-identity JIT design ships this table empty. When
    ``cloud_identity_gate_enabled`` is False (default), ``cloud_identity_service``
    short-circuits and no rows are inserted. When the gate is on, every
    write-path cloud SDK call is preceded by an elevation request whose
    lifecycle is tracked here.

    Internal ``status`` values:
      - pending     — request submitted to Entitle, awaiting workflow
      - granted     — Entitle agent has finished granting cloud-side IAM;
                      dashboard may proceed to call the cloud SDK
      - denied      — Entitle workflow rejected the request (security alert)
      - failed      — cloud-side IAM call failed (operator alert)
      - revoked     — TTL elapsed or explicit revoke (terminal)
    """
    __tablename__ = "entitle_activations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cloud = Column(String(16), nullable=False, index=True)        # aws | azure | gcp
    operation = Column(String(64), nullable=False, index=True)    # e.g. "aws:ec2:deploy"
    role = Column(String(255), nullable=True)                     # IAM policy / role / binding granted
    requester_user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    entitle_request_id = Column(String(255), nullable=True, unique=True, index=True)
    entitle_policy_id = Column(String(255), nullable=True)
    auto_approved = Column(Boolean, nullable=False, default=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    denial_reason = Column(Text, nullable=True)
    payload_hash = Column(String(64), nullable=False)
    # Which tenant the elevation belongs to. cloud_identity_service passes this
    # into the constructor unconditionally, so its absence made every elevation
    # raise TypeError the moment cloud_identity_gate_enabled was turned on.
    tenant_id = Column(String(64), nullable=True, index=True)
    requested_at = Column(DateTime, default=utcnow, nullable=False)
    granted_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)


class AppConfig(Base):
    """Encrypted key-value store for cloud credentials and feature flags.

    Values are Fernet-encrypted with a key derived from JWT_SECRET_KEY so that
    secrets at rest are protected even if someone reads the DB directly.
    Written by the setup wizard; consumed by config_service.get().

    The optional `workgroup` column lets prod-style multi-tenant deployments
    have per-workgroup overrides for the same key. NULL means "global";
    config_service.get() falls back to the NULL row when no workgroup-scoped
    row exists. Community installs leave `workgroup` NULL always and behave
    as before.
    """
    __tablename__ = "app_config"

    key = Column(String(128), primary_key=True)
    value = Column(Text, nullable=True)         # Fernet-encrypted
    workgroup = Column(String(64), nullable=True)
    updated_at = Column(DateTime, default=utcnow, nullable=False)


class BrandAsset(Base):
    """An uploaded branding image -- today just the operator's logo.

    Its own table rather than an `app_config` row, for reasons that compound:
    `config_service` caches the WHOLE config table in every worker and reloads it from the
    database every 5 seconds, so a base64 logo there would be a per-worker resident string
    re-selected and Fernet-decrypted twelve times a minute forever, whether or not anybody
    is looking at a page. `get_all_public()` also returns every global row, which would
    quietly put the image in the settings payload. And a data URI inlined into the HTML
    cannot be cached by the browser at all, on every page, including /login.

    `slot` is the primary key, which is what makes "one global logo" a property of the
    schema instead of a rule the application has to remember. It leaves room for a
    `favicon` or `logo_dark` slot later without a second table.

    Deliberately NOT Fernet-encrypted, unlike every other sensitive column here. These
    bytes are served verbatim to anonymous browsers -- the sign-in page and the public docs
    shell both render the mark -- so they are by definition not a secret. Encrypting them
    would add ~37% to the stored size, put a decrypt on the pre-auth render path for every
    cache miss, and inherit the JWT_SECRET_KEY rotation hazard, whose symptom here would be
    a corrupt image rather than an error anybody could act on.

    `sha256` is the content hash and does double duty: it is the HTTP ETag and the last
    segment of the URL, so a new upload busts every browser cache immediately while the
    bytes themselves can be served `immutable`. Never add this table to a view that does
    `SELECT *` -- read it by primary key for the bytes, or project the metadata columns.

    No migration entry is needed: `create_all` makes new tables.
    """
    __tablename__ = "brand_asset"

    slot = Column(String(32), primary_key=True)        # "logo"
    content_type = Column(String(64), nullable=False)  # from the sniffer, never the client
    data = Column(LargeBinary, nullable=False)
    sha256 = Column(String(64), nullable=False)        # hex digest; the ETag
    byte_size = Column(Integer, nullable=False)
    # Nullable because an SVG need not declare pixel dimensions, and there is no image
    # library in requirements.txt to infer them. Only used to reserve the right aspect
    # ratio so the nav bar does not reflow when the image lands.
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    filename = Column(String(255), nullable=True)      # display only, in the settings card
    uploaded_by = Column(String(128), nullable=True)
    updated_at = Column(DateTime, default=utcnow, nullable=False)


class SchemaMarker(Base):
    """"This one-time data migration has run." One row per migration, never deleted.

    Deliberately NOT a key in ``app_config``: those values are Fernet-encrypted with a key
    derived from JWT_SECRET_KEY, so rotating that secret would make the marker unreadable
    and re-run the migration. For a backfill that GRANTS permissions, re-running is not a
    harmless no-op — it would re-add a scope an administrator had deliberately removed.

    Most backfills in this file derive idempotency from the data instead (see
    ``job_service.backfill_audit_chain``, which checks whether any row is chained yet).
    That works when the end state is indistinguishable from "already done". It does not
    work here, because "this user has no `costs` scope" is both the pre-migration state and
    a legitimate post-migration choice.
    """
    __tablename__ = "schema_markers"

    key = Column(String(128), primary_key=True)
    applied_at = Column(DateTime, default=utcnow, nullable=False)
    detail = Column(Text, nullable=True)        # free-text: what it did, for support


class SecretVault(Base):
    """Registry of external secret-store endpoints the dashboard can resolve
    references against.

    Used by the multi-vault reference scheme: a config row with
    ``azure_kv://<vault-id>/<secret-name>`` looks up the vault row by
    (id, backend) and routes the read to that vault's endpoint with that
    vault's credentials. When the table is empty (community / fresh install)
    the legacy ``azure_kv://<secret-name>`` shape continues to resolve via
    the singleton config_service.get('azure_kv_*') keys, so behaviour is
    unchanged until an operator registers a vault.
    """
    __tablename__ = "secret_vaults"

    id = Column(String(64), primary_key=True)              # e.g. "primary", "tenant-alpha-eu"
    backend = Column(String(32), nullable=False)           # azure_kv | aws_sm | gcp_sm | bt_secrets_safe
    endpoint = Column(Text, nullable=False)                # e.g. https://my-vault.vault.azure.net
    credentials_ref = Column(Text, nullable=True)          # optional reference to creds (e.g. distinct SP per vault)
    workgroup = Column(String(64), nullable=True)          # if set, only this workgroup resolves here
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by_user_id = Column(String(36), nullable=True)


class ContainerStateCache(Base):
    """Cache for Portainer container state to improve dashboard performance."""
    __tablename__ = "container_state_cache"

    container_id = Column(String(64), primary_key=True)
    short_id = Column(String(12))
    name = Column(String(200))
    image = Column(String(500))
    state = Column(String(50))
    status = Column(String(200))
    ports = Column(Text)                              # JSON list[str]
    endpoint_id = Column(Integer, index=True)
    endpoint_name = Column(String(200))
    workgroup = Column(String(50), index=True)
    created_ts = Column(Integer)                      # unix epoch from Docker
    last_updated = Column(DateTime, default=utcnow, nullable=False, index=True)


class CloudCostCache(Base):
    """Last-known-good month-to-date cost for one (cloud, view), plus the throttle
    state that decides whether we are allowed to ask that cloud again.

    In the database rather than in ``services/cache_service`` for three reasons, each
    sufficient on its own. The app runs ``gunicorn -w 2``, so a process-local dict gives
    each worker its own throttle budget and its own 429 — the same hazard that put
    :class:`EphemeralState` and :class:`LoginAttempt` here. ``jobs_worker`` is a third
    process that warms nothing, so the budget-alert scan could only ever read an empty
    dict there. And Azure Cost Management rate-limits per SUBSCRIPTION, which is a
    property of the account, not of a process — so "we were just throttled" has to
    outlive a redeploy, or every image rebuild re-earns the 429.

    ``payload`` is written ONLY when a cloud returned ``status="ok"``. A failure writes
    the error and cooldown columns and leaves ``payload``/``fetched_at`` untouched. That
    asymmetry is the whole reason this table exists: a 429 must never be able to replace
    a working number. The row is therefore not a cache entry with a TTL — it is a
    last-known-good value plus an expiry *opinion*.
    """
    __tablename__ = "cloud_cost_cache"

    # PK is (cloud, view) and deliberately EXCLUDES the month: throttle state is a
    # property of the API, not of the calendar, so it has to survive a rollover.
    # `period` guards the payload instead — see cost_cache._is_usable.
    cloud = Column(String(16), primary_key=True)   # aws | azure | gcp | oci
    view = Column(String(16), primary_key=True)    # summary | breakdown

    # ── last-known-good (written only on status="ok") ────────────────────────
    payload = Column(Text, nullable=True)                      # JSON: one cloud's entry
    payload_version = Column(Integer, nullable=False, default=0)
    period = Column(String(7), nullable=True)                  # "2026-08"; MTD is month-scoped
    fetched_at = Column(DateTime, nullable=True, index=True)
    # Set by a Setup save. Read as "not fresh" while the payload is still SERVED, so
    # fixing a credential re-queries without blanking the page in the meantime.
    stale = Column(Boolean, nullable=False, default=False)

    # ── failure / throttle (written only on a failed attempt) ────────────────
    last_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    # Hard gate: while this is in the future NOTHING queries this cloud, including an
    # explicit ?refresh=true. Set from the provider's Retry-After when it gave one.
    cooldown_until = Column(DateTime, nullable=True, index=True)

    # ── single-flight ────────────────────────────────────────────────────────
    # A liveness bound, not a mutex: a process that dies mid-fetch releases its claim by
    # expiry. The advisory lock only makes the claim's read-modify-write atomic; it is
    # never held across the network call.
    lease_until = Column(DateTime, nullable=True)
    lease_owner = Column(String(64), nullable=True)   # "host:pid" — diagnostics only
    # Per-CLOUD pacing, written to every row of the cloud: Cost Management throttles per
    # subscription, so this cloud's summary and breakdown queries must not overlap.
    next_query_allowed_at = Column(DateTime, nullable=True)

    updated_at = Column(DateTime, default=utcnow, nullable=False)


class WorkloadCredentialLease(Base):
    """One live Workload Credentials lease per (cloud, purpose).

    Deliberately the same shape as :class:`CloudCostCache` — read that class and
    ``services/cost_cache``'s module docstring first. Same three reasons for being in the
    database rather than in a process-local dict, plus one this table has and that one
    does not.

    The app runs ``gunicorn -w 2`` and ``jobs_worker`` is a third process, so a
    process-local dict would give each one **its own lease**. For a cost figure that is
    wasteful; here it is billable. Workload Credentials charges per credential
    *issuance*, so three processes each minting their own lease is three times the
    invoice for the same credential, and every image rebuild would throw them all away
    and re-mint. The row is what makes one issuance serve the whole deployment.

    ``payload`` is written ONLY when a generate succeeded. A failure writes the error and
    cooldown columns and leaves ``payload``/``expires_at`` untouched. Same asymmetry, same
    reason: a 429 must never be able to replace a working credential. It matters more here
    than for costs, because a blank credential does not degrade a tile — it makes the
    dashboard indistinguishable from one that was never configured for the dynamic tier.

    ``payload`` is **Fernet-encrypted** (``config_service.encrypt_value``), unlike
    ``CloudCostCache.payload``. It holds a live cloud credential, so it gets the same
    at-rest treatment as ``app_config``, and therefore the same rotation hazard: losing the
    JWT root key makes stored leases unreadable. That is survivable in a way losing
    ``app_config`` is not — the next refresh simply mints a new one.

    ``expires_at`` is **not** a TTL opinion. It is the expiry the provider issued, and the
    credential stops working at it whether or not anything here agrees. That is the
    opposite of ``CloudCostCache``, where a stale payload is still servable.

    The single-flight columns are ``claim_*`` rather than ``lease_*`` as in
    ``CloudCostCache``. The word "lease" already means the provider-issued credential lease
    throughout this feature, and having it also mean "this process's short claim on the
    right to refresh" would be genuinely confusing in the one module that handles both.
    """
    __tablename__ = "workload_credential_lease"

    # aws | azure. GCP is absent because Workload Credentials does not mint GCP
    # credentials — a GCP deployment stays on the static tier by design, not by omission.
    cloud = Column(String(16), primary_key=True)
    # provision | readonly. Splitting by lifecycle rather than by operation is what lets
    # the request path hold a read-only credential while write privilege exists only for
    # the duration of a job. See docs/beyondtrust/workload-credentials.md.
    purpose = Column(String(32), primary_key=True)

    # ── last-known-good (written ONLY on a successful generate) ──────────────
    payload = Column(Text, nullable=True)              # Fernet-encrypted JSON of the credential
    lease_id = Column(String(128), nullable=True)      # the provider's id; needed to revoke
    issued_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True, index=True)

    # ── failure (written ONLY on a failed attempt) ───────────────────────────
    last_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    # While this is in the future nothing tries to mint for this (cloud, purpose). Guards
    # against a misconfigured dynamic secret turning every page load into a billable
    # failed attempt.
    cooldown_until = Column(DateTime, nullable=True)

    # ── single-flight ───────────────────────────────────────────────────────
    # A liveness bound, not a mutex: a process that dies mid-generate releases its claim by
    # expiry. The advisory lock only makes the claim's read-modify-write atomic and is
    # never held across the network call.
    claim_until = Column(DateTime, nullable=True)
    claim_owner = Column(String(64), nullable=True)    # "host:pid" — diagnostics only

    updated_at = Column(DateTime, default=utcnow, nullable=False)


class DashboardStatCache(Base):
    """One dashboard tile's last-known-good counts, plus the state that decides whether we
    are allowed to ask that provider again.

    Same three reasons as :class:`CloudCostCache`, and the same invariants — read that
    class and ``services/cost_cache``'s module docstring first, because this is
    deliberately the same shape. The app runs ``gunicorn -w 2`` and ``jobs_worker`` is a
    third process, so a process-local dict gives each one its own copy and its own idea of
    the throttle; and every image rebuild throws all of them away.

    What it buys: the dashboard home page fans out to ~33 endpoints, ~22 of them at once,
    and every one holds a pooled connection for its whole duration — against
    ``pool_size=5 + max_overflow=5``. That is the ``QueuePool limit ... reached`` failure
    the 26.8.5 mitigation could only reduce, not remove. Reading tiles from this table
    makes a page load ONE indexed query and no cloud calls at all.

    ``payload`` is written ONLY on a successful collection. A failure writes the error and
    cooldown columns and leaves ``payload``/``fetched_at`` exactly where they were. That
    asymmetry is the reason this is a table and not a cache entry with a TTL: it is a
    last-known-good value plus an expiry *opinion*.

    NO ``period`` column, unlike CloudCostCache — a VM count is not month-scoped, so a
    calendar rollover is not a miss. ``payload_version`` is the only shape gate.
    """
    __tablename__ = "dashboard_stat_cache"

    # Matches the tile `key` in templates/dashboard.html's tileSections.
    tile_key = Column(String(48), primary_key=True)
    # "" for a tile with one global source. Reserved for per-connection tiles: the
    # hypervisor routers resolve `conn_or_error(db, kind, "")` to the DEFAULT connection,
    # so an install with two Proxmox clusters has a tile describing one of them. Summing a
    # tile's scopes is how that gets fixed. It is in the PRIMARY KEY from the start
    # deliberately — adding a column later is an ALTER, but widening a primary key is not.
    scope = Column(String(64), primary_key=True, default="")

    # Which provider this tile calls, e.g. "gcp" / "proxmox" / "local". A COLUMN rather
    # than a lookup in the collector's spec table, so pacing is a self-contained
    # `UPDATE ... WHERE provider = :p` and this module needs to import nothing to do it.
    provider = Column(String(24), nullable=False, default="", index=True)

    # ── last-known-good (written ONLY on a successful collection) ────────────
    payload = Column(Text, nullable=True)          # JSON, shape owned by the collector
    payload_version = Column(Integer, nullable=False, default=0)
    fetched_at = Column(DateTime, nullable=True, index=True)
    # Set by a Setup save. Read as "not fresh" while the payload is still SERVED, so
    # fixing a credential re-collects without blanking the tile in the meantime.
    stale = Column(Boolean, nullable=False, default=False)

    # ── failure / backoff (written ONLY on a failed attempt) ─────────────────
    last_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    # Hard gate: while this is in the future nothing collects this tile, including an
    # explicit refresh. Mashing Refresh at a saturated provider must not compound it.
    cooldown_until = Column(DateTime, nullable=True, index=True)

    # ── single-flight ────────────────────────────────────────────────────────
    # A liveness bound, not a mutex: a process that dies mid-collection releases its claim
    # by expiry. The advisory lock only makes the claim's read-modify-write atomic; it is
    # never held across the network call.
    lease_until = Column(DateTime, nullable=True)
    lease_owner = Column(String(64), nullable=True)   # "host:pid" — diagnostics only
    # Per-PROVIDER pacing, written to every tile of that provider: GCP alone owns seven
    # tiles against an 8-thread pool, so pacing per tile would let one pass hand itself
    # CloudProviderBusy.
    next_query_allowed_at = Column(DateTime, nullable=True)

    updated_at = Column(DateTime, default=utcnow, nullable=False)


class PovPlatformCache(Base):
    """One lab platform's last-known-good inventory listing — its environments or its
    templates — plus the throttle state that decides whether we may ask it again.

    Same three reasons as :class:`CloudCostCache` and :class:`DashboardStatCache`; read
    ``services/cost_cache``'s module docstring first, because this is deliberately the
    same shape. What makes it necessary here is who the caller is: the POV page's two
    platform listings went out on an INSTANCE-WIDE credential, so every SE on the install
    shared one account's rate limit, and the number of calls scaled with how many of them
    happened to have the page open. Skytap answers a saturated account with 423 plus a
    Retry-After — a property of the account, not of a process, so "we were just throttled"
    has to outlive a redeploy or every image rebuild re-earns it.

    NOT a second use of :class:`DashboardStatCache`, though the columns would have fitted.
    That table is read with a single ``read_all`` and maintained with table-wide
    ``mark_stale`` / ``clear_cooldowns``, and ``api/dashboard.dashboard_refresh`` derives
    its minimum-refresh floor from the NEWEST row in the whole table. Rows for a POV
    listing would therefore have suppressed the dashboard's Refresh button whenever
    somebody opened /pov — one store serving two unrelated consumers, where a maintenance
    call on either silently reaches the other.

    ``payload`` is written ONLY on a successful listing. A failure writes the error and
    cooldown columns and leaves ``payload``/``fetched_at`` exactly where they were. That
    asymmetry is the reason this is a table and not a cache entry with a TTL: a 423 during
    a rate-limit window must not be able to blank the environments table for a TTL, and a
    generic cache cannot tell that answer from an empty account.
    """
    __tablename__ = "pov_platform_cache"

    # environments | templates. The two collection reads templates/pov/index.html makes.
    kind = Column(String(24), primary_key=True)
    # The lab platform, e.g. "skytap" | "azure". Also what pacing keys on, so a platform
    # whose account is saturated is left alone for BOTH its listings at once.
    platform = Column(String(24), primary_key=True)
    # The configured project id the listing was taken under, "" for the account-wide
    # scope. IN THE PRIMARY KEY and load-bearing: `skytap_project_id` is a Settings field,
    # blank is the widest scope, and a key that omitted it would serve the previous
    # project's environment names — on a shared lab account, other customers' POVs —
    # until the TTL expired. Widening a primary key later is not an ALTER.
    project_id = Column(String(64), primary_key=True, default="")

    # ── last-known-good (written ONLY on a successful listing) ───────────────
    payload = Column(Text, nullable=True)          # JSON list, shape owned by the adapter
    payload_version = Column(Integer, nullable=False, default=0)
    fetched_at = Column(DateTime, nullable=True, index=True)
    # Set by a Settings save. Read as "not fresh" while the payload is still SERVED, so
    # fixing a credential re-lists without emptying the table in the meantime.
    stale = Column(Boolean, nullable=False, default=False)

    # ── failure / backoff (written ONLY on a failed attempt) ─────────────────
    last_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    # Hard gate: while this is in the future nothing lists this platform, including an
    # explicit ?refresh=true. Set from the platform's own Retry-After when it gave one.
    cooldown_until = Column(DateTime, nullable=True, index=True)

    # ── single-flight ────────────────────────────────────────────────────────
    # A liveness bound, not a mutex: a process that dies mid-listing releases its claim by
    # expiry. The advisory lock only makes the claim's read-modify-write atomic; it is
    # never held across the network call.
    lease_until = Column(DateTime, nullable=True)
    lease_owner = Column(String(64), nullable=True)   # "host:pid" — diagnostics only
    next_query_allowed_at = Column(DateTime, nullable=True)

    updated_at = Column(DateTime, default=utcnow, nullable=False)


class RegisteredImage(Base):
    """Operator-registered image artefacts. The dashboard's source-of-truth
    record for "this image exists, here's where the artefact lives, here's
    what cloud-native images derive from it." Cross-cloud promotion records
    are stored in `promotions` as JSON because each target carries a
    different shape (AMI ID + region for AWS, resource ID for Azure, full
    self_link for GCP)."""
    __tablename__ = "registered_images"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(200), nullable=False, index=True)
    version = Column(String(64), nullable=False)
    description = Column(Text, nullable=True)

    # Where the image was first built / registered.
    source_cloud = Column(String(20), nullable=False)        # "aws" | "azure" | "gcp"
    source_image_id = Column(String(500), nullable=True)     # AMI / managed image / custom image ID
    source_region = Column(String(64), nullable=True)        # AWS region / Azure location / GCP region

    # Optional storage URL for the portable artefact (e.g. s3://bucket/key,
    # https://acct.blob.core.windows.net/c/k, gs://bucket/key). Lets the
    # promote flow re-import without re-running Packer.
    artefact_url = Column(String(1000), nullable=True)
    artefact_format = Column(String(20), nullable=True)      # "vhd" | "raw" | "vmdk" | "ova"
    # Guest OS of the artefact ("Linux" | "Windows"). Promote targets need it —
    # Azure managed-image import rejects/boots wrong with a mismatched os_type.
    os_type = Column(String(20), nullable=True)
    # The POV standard recipe that built it ("standard-linux" | "standard-windows",
    # services/pov_image_recipes), or NULL for anything else. Display and one behaviour:
    # a standard-linux target gets the POV's own login at launch (pov_cloud_aws).
    recipe = Column(String(40), nullable=True)

    # Per-target promotion records. Shape:
    #   { "aws":   {"image_id": "ami-…", "region": "us-east-2", "status": "completed"|"manual"|"failed", "notes": "..."},
    #     "azure": {"image_id": "/subscriptions/.../images/…", "status": ...},
    #     "gcp":   {"self_link": "...", "status": ...} }
    promotions = Column(Text, nullable=True)                 # JSON

    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100), nullable=False)

    @property
    def promotions_dict(self) -> dict:
        if not self.promotions:
            return {}
        try:
            import json
            return json.loads(self.promotions)
        except Exception:
            return {}


class VirtualDesktop(Base):
    """A single virtual-desktop seat in a dashboard-managed desktop pool.

    One row per desktop (seat), not per pool. AWS, Azure and GCP each provision one
    private VM per seat through their own seat backend in ``vdesktop_service`` and
    fill ``vm_resource_id``, then broker the seat on the PRA Gateway and fill
    ``pra_jump_id``.

    Two things about this table are load-bearing and not obvious from the columns:

      * ``cloud`` is an unconstrained ``String(20)``. Code that dispatches on it must
        tolerate a value with no seat backend rather than assume one of three.
      * The POOL TAG stamped on the backing VM is not one string. Azure and EC2 take
        ``dashboard:desktop_pool``; a colon is ILLEGAL in a GCP label key, so GCP uses
        ``dashboard_desktop_pool``. Each backend owns its own.
    """
    __tablename__ = "virtual_desktops"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cloud = Column(String(20), nullable=False)              # aws | azure | gcp
    pool_name = Column(String(200), nullable=False, index=True)
    # Backing kind. Only vm_pool exists; avd / workspaces are the cloud-native
    # services this table was shaped to leave room for.
    kind = Column(String(20), nullable=False, default="vm_pool")
    # Cloud-native id of the backing VM once provisioned. Null until then. The shape
    # is per cloud and carries what teardown needs, since teardown gets only this
    # string: an Azure ARM id, "<region>/<instance-id>" on AWS,
    # "<project>/<zone>/<name>" on GCP.
    vm_resource_id = Column(String(500), nullable=True)
    # pending | running | stopped | deprovisioning
    status = Column(String(20), nullable=False, default="pending", index=True)
    assigned_user = Column(String(200), nullable=True)
    # PRA jump-item id once the seat is brokered. NULL until then, and NULL rather
    # than "" on a failed registration: jumpoint_host_service._active_vdesktop_count
    # counts a non-NULL value as a live reference and would pin the shared Gateway.
    pra_jump_id = Column(String(200), nullable=True)
    # Scrubbed Terraform state for the seat's PRA jump item (a Remote RDP jump plus
    # its vault account for a Windows seat, a Shell Jump for a Linux one) so teardown
    # can destroy it deterministically. `vdesktop_service.jump_kind` reads WHICH kind
    # off this state rather than storing it in a column that could disagree. Secret
    # values are redacted before storage; never returned by the API.
    pra_tunnel_state = Column(Text, nullable=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class CloudDatabase(Base):
    """Inventory of dashboard-provisioned managed databases — cloud-database
    infrastructure, Phase 1.

    One row per provisioned or registered database (Postgres / MySQL / SQL Server /
    Oracle / MongoDB), always **private** and reached only through a BeyondTrust PRA
    tunnel. In the community edition the PRA tunnel (Phase 2) is brokered with the
    ``beyondtrust/sra`` Terraform provider (``terraform_pra_service``) — never
    ``btapi``. Oracle and MongoDB ride the provider's generic tcp tunnel until it
    ships their dedicated PRA 26.3 tunnel resources.
    The PRA / Password-Safe fields are populated by later phases:
    ``jump_item_id`` by the tunnel brokering (Phase 2); ``ps_*`` are unused in
    community (Password-Safe onboarding is a prod-only path).
    """
    __tablename__ = "cloud_databases"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    engine = Column(String(20), nullable=False)            # postgres | mysql | sqlserver | oracle | mongodb
    provider = Column(String(40), nullable=True)           # e.g. rds | azure_flexible | cloud_sql
    cloud = Column(String(20), nullable=False)             # aws | azure | gcp | local
    region = Column(String(64), nullable=True)
    # Mirrors K8sCluster.source. A `registered` row is a database that already existed —
    # on-prem (cloud='local') or one the dashboard didn't provision — recorded so it can
    # be a Config Management target. It has no Terraform state and no provisioning job,
    # so delete deregisters it rather than destroying it, and its admin credential comes
    # from a Password Safe managed account instead of the job's tf_variables.
    source = Column(String(16), nullable=False, default="provisioned")  # provisioned | registered

    instance_id = Column(String(255), nullable=True)       # cloud resource id (filled on apply)
    private_host = Column(String(255), nullable=True)      # private endpoint host (no public endpoint)
    port = Column(Integer, nullable=True)
    # The database catalog on this instance. Written on BOTH paths: a registered row
    # records the operator's entry, and a provisioned one mirrors the `db_name` handed
    # to Terraform (cloud_database_service.provision), so neither the Databases page
    # nor the Entitle cloud-function adapter depends on the provisioning job surviving.
    # Pre-existing provisioned rows are filled by backfill_provisioned_db_names.
    #
    # NULL means no user database exists to name: RDS SQL Server rejects db_name at
    # creation, and a registered row may legitimately leave it blank. The SQL Server
    # -> `master` substitution is applied at READ time by
    # cloud_database_service.connection_db_name and never stored — on Azure/GCP a real
    # catalog exists alongside master, and the adapter's FN_DB_NAME must name that
    # catalog or nothing at all.
    db_name = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False, default="provisioning", index=True)

    credentials_ref = Column(Text, nullable=True)          # backend-agnostic ref (resolved via config_service)
    jump_item_id = Column(String(64), nullable=True)       # PRA protocol-tunnel jump (Phase 2)
    # Per-DB PRA broker overrides — config defaults are the fallback.
    jump_group = Column(String(128), nullable=True)        # PRA Asset Group name override (else bt_jump_group_name)
    jumpoint_name = Column(String(128), nullable=True)     # PRA Jumpoint name override (else bt_jumpoint_name)
    pra_credential_ref = Column(String(256), nullable=True)  # secret ref → bt_client_secret override
    entitle_integration_id = Column(String(64), nullable=True)  # Entitle DB integration registered on apply

    # Password Safe DB onboarding (optional; the AWS SSM / Azure Run Command custom
    # plugins). Ids, not credentials — exactly what an operator reads back in the
    # Password Safe UI — so the Databases page can show whether a row is onboarded
    # without loading every clouddb_provision job on each poll.
    #
    # The AUTHORITATIVE teardown state stays on the provisioning job's metadata
    # (ps_db_* / ps_pravault_*, which decommission reads); these two are written in
    # the same commit as that stash and cleared by the same teardown, so the pair
    # cannot drift into disagreeing about whether onboarding happened.
    ps_managed_system_id = Column(String(64), nullable=True)
    ps_managed_account_id = Column(String(64), nullable=True)

    # Which remote agent can reach this database, for a Config-Management run. NULL = the
    # dashboard runs it itself, which is the behaviour before this column and the only one
    # available to a provisioned cloud database.
    #
    # Only meaningful on a `registered` row with cloud='local'. Such a database sits on the
    # corporate LAN, so the run used to require a sibling container on the DASHBOARD host —
    # which a cloud-hosted dashboard (ECS / ACI / Container Apps) does not have and could
    # not route from anyway. Naming an agent moves that one-shot container onto a host that
    # does have a route. Unlike HypervisorConnection this row keeps `private_host`: the
    # address is not a credential-aiming risk here, because the operator registered it and
    # the agent re-checks it against its own policy.yaml before connecting.
    agent_id = Column(String(36), ForeignKey("remote_agents.id", ondelete="SET NULL"),
                      index=True, nullable=True)

    # Where a cloud database lives, for Config-Management routes (migration 0008). A route
    # governs a cloud database when its range contains EVERY one of `route_cidrs`: the DB
    # subnet group's subnets on AWS (a failover may land in any), the delegated subnet on
    # Azure, the private IP as a /32 on GCP. Read from the cloud by clouddb_network, at
    # provisioning or by the route network backfill. NULL means not recorded, and such a
    # database matches no route: it stays on the remote worker.
    route_cidrs = Column(Text, nullable=True)              # JSON list of CIDRs
    route_network = Column(String(200), nullable=True)     # cmr.normalize_network form
    # The workstream it was provisioned in. reassign_database_workgroup moves `workgroup`
    # but never this, for the reason a VM deploy keeps `deploy_workgroup`: a route narrowed
    # to a workstream stands for that team's network, which a reassignment does not move.
    deploy_workgroup = Column(String(100), nullable=True)

    # Workgroup scoping. NULL (the state every row predating this column is in)
    # falls back to creator-scoping in inventory_service.visible_to, so adding the
    # column changed nobody's access -- a row becomes shared only when someone tags it.
    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    # Auto-delete timer — NULL = never (see Job.expires_at). Only ever stamped on a
    # `provisioned` row: "deleting" a registered database merely deregisters it, and a
    # timer that silently forgets somebody's registered production database is worse
    # than no timer at all.
    expires_at = Column(DateTime, nullable=True, index=True)
    expiry_warned_at = Column(DateTime, nullable=True)


class CertLab(Base):
    """Inventory of dashboard-provisioned certificate authorities for the Password Safe
    Certificate plugin's lab.

    One row per **CA pool**, which is the thing that carries standing cost: a GCP CAS
    DevOps-tier pool is ~$20/month whether or not it issues anything, and an AWS Private
    CA is ~$400/month. The mTLS endpoint and the CI runner are ordinary VMs deployed
    through the normal cloud pages, so they already have their own timers and their own
    Destroy — this row exists so the CA has one too.

    ``tf_state`` is the scrubbed Terraform state, which is what makes teardown
    deterministic: the destroy is fed by the recorded state rather than by hand-typed
    ids, exactly as the database and cluster paths are.

    ``ps_tf_state`` holds the separate state for the managed system + managed account, so
    deregistering the Password Safe objects and destroying the CA are independent — a
    lab can be torn down while the certificate identity's audit history stays.
    """
    __tablename__ = "cert_labs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(120), nullable=False)
    cloud = Column(String(20), nullable=False, default="gcp")   # gcp | aws
    backend = Column(String(20), nullable=False, default="gcpcas")  # the plugin's backend name
    project = Column(String(120), nullable=True)                # GCP project / AWS account
    location = Column(String(64), nullable=True)                # CAS region — location-scoped
    pool_id = Column(String(120), nullable=True)                # the `pool=` on the address
    # The `arn=` on an awspca address, and the AWS half of what `pool_id` is for GCP. Its
    # own column rather than reusing pool_id: an ARN is not a pool, nothing on the AWS
    # side has a pool, and a field whose meaning depends on a sibling column is how a
    # reader two years from now gets it wrong. NULL until the apply returns it.
    ca_arn = Column(String(255), nullable=True)
    # How many CAs this root permits beneath it — GCP's max_issuer_path_length, and on
    # AWS the ceiling the SubordinateCACertificate_PathLen{N} templates are chosen from.
    #
    # **This is the one prerequisite that stops a subordinate-CA build dead.** A root
    # created to issue leaves has a path length of ZERO and refuses to sign a subordinate
    # at all, and the refusal comes from the CA rather than from the plugin — so it
    # arrives as a policy error a long way from the missing flag. Recording it means the
    # identity form can refuse `isca` against a root that cannot sign one, at the click.
    #
    # 0 on every pre-existing row, which is what those roots actually are.
    ca_path_length = Column(Integer, nullable=False, default=0)
    status = Column(String(32), nullable=False, default="provisioning", index=True)

    # Terraform state lives in the active storage backend under terraform-state/<job id>,
    # so destroy re-inits from there and works after a container recreate wiped the deploy
    # dir. This id is the key to it, which is why deleting a provisioning Job row would
    # orphan a billing CA pool.
    deploy_job_id = Column(String(36), nullable=True)
    # The chain the mTLS endpoint must trust. Public by definition — a CA certificate is
    # not a secret — so it is stored plainly and handed to the nginx playbook as an
    # extra_var rather than being fetched again at run time.
    ca_chain_pem = Column(Text, nullable=True)
    # The enrollment service account's email. Its KEY is never stored here: it goes
    # straight into the Password Safe functional account, which is the protected field
    # built for it. Nothing secret belongs on a row the inventory page renders.
    enroll_account = Column(String(255), nullable=True)

    # The functional account this CA's identities onboard against, recorded in BOTH
    # modes so Add identity never has to fall back to a global config key that may have
    # moved on since the build. Not secret: it is the account's NAME, which is the
    # enrollment principal plus the BeyondInsight run-as user.
    ps_functional_account = Column(String(255), nullable=True)
    # Set ONLY when the dashboard minted that account. Non-NULL is the ownership flag:
    # teardown deletes what it created and never an operator's own account, exactly how
    # the cloud-DB onboarding splits ps_db_functional_account_id from ..._ref. NULL
    # therefore means "referenced, or never created" — never "not yet looked up".
    ps_functional_account_id = Column(String(36), nullable=True)

    # The SECOND functional account, on the "Subordinate CA" platform.
    #
    # Two pairs of columns rather than one, because a functional account is
    # **platform-bound** and a managed system inherits its platform: an account on
    # "Certificate" cannot carry a managed system on "Subordinate CA" — it onboards green
    # and then fails every credential action. The CA credential inside is the same one;
    # what differs is the platform it was created on.
    #
    # Collapsing them would also undo the reason the plugin split in the first place. The
    # whole argument for two platforms is that their access control is separable, and one
    # shared functional account puts a leaf and an issuer back under one grant.
    #
    # NULL until the first subordinate identity is onboarded — the leaf account is minted
    # by the build, this one lazily, from the enrollment credential still sitting in the
    # CA's own terraform state. See cert_lab_service._ensure_package_functional_account.
    ps_subca_functional_account = Column(String(255), nullable=True)
    ps_subca_functional_account_id = Column(String(36), nullable=True)

    ps_system_id = Column(String(36), nullable=True)
    ps_account_id = Column(String(36), nullable=True)
    ps_address = Column(Text, nullable=True)                    # the composed profile
    # Which package ps_system_id was registered against — "certificate" or "subca". The
    # address alone cannot say: a subordinate profile normally carries no isca= at all,
    # that being the package's own default.
    ps_package = Column(String(16), nullable=True)
    ps_tf_state = Column(Text, nullable=True)                   # scrubbed
    error_message = Column(Text, nullable=True)

    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=True)
    # Auto-delete timer — NULL = never, never "inherit the default" (see Job.expires_at).
    # Only ever stamped on a PROVISIONED pool: a CA someone else built is inventory, not
    # something this dashboard may reap.
    expires_at = Column(DateTime, nullable=True, index=True)
    expiry_warned_at = Column(DateTime, nullable=True)

class ManagedDirectory(Base):
    """A Microsoft Active Directory that Windows servers can be domain-joined to.

    AWS (Directory Service: Managed Microsoft AD, AD Connector, Simple AD) and GCP
    (Managed Service for Microsoft Active Directory). Azure is absent on purpose: Windows
    VMs there join Entra ID directly (windows_server_hook.entra_join_azure).

    Cloud identity providers (Entra ID, Okta, PingOne) are rows too, with
    ``cloud="saas"``: not joinable, but browsable and with opt-in group-membership
    writes through ``services/directory_idp``.

    ``source`` follows the CloudDatabase / K8sCluster convention. A ``provisioned`` row
    was built here from ``terraform/directory/<module>`` and can be destroyed here; a
    ``registered`` row is a directory that already existed, found by discovery, and
    deleting it only forgets it.

    No secret is stored on this row. The administrator password of a provisioned
    directory lives in Password Safe / an external secret manager (never the dashboard
    database); only its (backend, ref) pair is kept here. A registered directory carries
    none at all — joining a server needs no domain credential on either cloud.
    """
    __tablename__ = "managed_directories"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)                 # FQDN, e.g. corp.example.com
    netbios = Column(String(15), nullable=True)
    cloud = Column(String(20), nullable=False)                 # aws | gcp | local (on-prem) | saas
    # aws_managed_ad | aws_ad_connector | aws_simple_ad | gcp_managed_ad
    # | onprem_ad | ldap  (the last two: on-prem, reached through a remote agent)
    provider = Column(String(32), nullable=False)
    source = Column(String(16), nullable=False, default="provisioned")  # provisioned | registered
    status = Column(String(32), nullable=False, default="provisioning", index=True)
    edition = Column(String(32), nullable=True)                # AWS Standard | Enterprise

    region = Column(String(64), nullable=True)                 # AWS region
    locations = Column(Text, nullable=True)                    # GCP: JSON array of regions
    project = Column(String(120), nullable=True)               # GCP project
    directory_id = Column(String(64), nullable=True)           # AWS d-xxxxxxxxxx
    resource_name = Column(String(255), nullable=True)         # GCP projects/…/domains/…
    vpc_id = Column(String(64), nullable=True)                 # AWS
    subnet_ids = Column(Text, nullable=True)                   # AWS: JSON array
    networks = Column(Text, nullable=True)                     # GCP: JSON array (authorized)
    reserved_ip_range = Column(String(32), nullable=True)      # GCP
    dns_ips = Column(Text, nullable=True)                      # JSON array
    security_group_id = Column(String(64), nullable=True)      # AWS

    # ── on-prem (cloud="local") ─────────────────────────────────────────────────
    # The remote agent that can reach this directory. Only meaningful on a local row —
    # mirrors CloudDatabase.agent_id. SET NULL, so retiring an agent leaves the row
    # visibly unreachable rather than deleting an inventory record.
    agent_id = Column(String(36), ForeignKey("remote_agents.id", ondelete="SET NULL"),
                      index=True, nullable=True)
    host = Column(String(255), nullable=True)                  # a domain controller / LDAP server
    port = Column(Integer, nullable=True)                      # 389 | 636
    use_ldaps = Column(Boolean, nullable=True)
    base_dn = Column(String(255), nullable=True)               # e.g. DC=corp,DC=example,DC=com
    # "psmanaged:{system_id, account_id, account_name}" — the Password Safe account the
    # playbooks bind as, checked out per run. Never a secret.
    credentials_ref = Column(Text, nullable=True)
    # A cloud-side extension of an on-prem directory (an AWS AD Connector, a GCP DNS
    # link) points at the on-prem row it extends. Not a FK: unregistering the on-prem
    # row is refused while a link exists, and a dangling id reads as "on-prem row gone".
    linked_directory_id = Column(String(36), nullable=True, index=True)
    # A VyOS site link's non-secret settings as JSON (services/vyos_link_service): zone,
    # subnets, both WireGuard PUBLIC keys, the peer's addresses, and the Secret Manager
    # id that holds the peer's private key. Never a secret.
    link_config = Column(Text, nullable=True)

    # ── cloud identity providers (cloud="saas": entra_id | okta | pingone) ──────────
    # Reached over the provider's REST API from the dashboard itself, no agent. The
    # credential is still never on the row: ``credentials_ref`` holds a vault ref
    # (bt_safe:// aws_sm:// azure_kv:// gcp_sm://) or the psmanaged: pointer, or nothing
    # for auth_mode="dashboard_azure" (the dashboard's own Azure identity).
    endpoint = Column(String(255), nullable=True)              # Okta org URL | PingOne TLD
    tenant_id = Column(String(64), nullable=True)              # Entra tenant | PingOne env
    client_id = Column(String(255), nullable=True)
    auth_mode = Column(String(32), nullable=True)              # client_secret | private_key_jwt | ssws | dashboard_azure
    # Group-membership writes are opt-in per directory: registering one grants nothing.
    writes_enabled = Column(Boolean, nullable=True, default=False)
    options = Column(Text, nullable=True)                      # JSON, closed key set per provider
    # Which LDAP server a provider="ldap" row is (openldap | pingdirectory | okta_ldap |
    # 389ds | freeipa). NULL = unknown/generic. Decides defaults and, for okta_ldap, that
    # only read-only playbooks run (directory_service.run_refusal).
    vendor = Column(String(32), nullable=True)
    # The Entitle integration an operator PINNED as governing this directory. Never
    # matched automatically: a name match is a guess, and a wrong link reads as truth.
    entitle_integration_id = Column(String(64), nullable=True)
    entitle_integration_name = Column(String(255), nullable=True)   # as Entitle named it when pinned

    admin_username = Column(String(64), nullable=True)
    admin_password_backend = Column(String(32), nullable=True)
    admin_password_ref = Column(String(255), nullable=True)
    admin_password_custody = Column(String(32), nullable=True)  # secret_manager | passwordsafe_managed

    ps_system_id = Column(String(36), nullable=True)
    ps_account_id = Column(String(36), nullable=True)
    ps_tf_state = Column(Text, nullable=True)                  # scrubbed
    ps_error = Column(Text, nullable=True)

    # Terraform state lives in the storage backend under terraform-state/<job id>.
    deploy_job_id = Column(String(36), nullable=True)
    error_message = Column(Text, nullable=True)

    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=True)
    # NULL by default: a directory is long-lived infrastructure that joined servers
    # depend on. An operator may set one through /api/expiry/set.
    expires_at = Column(DateTime, nullable=True, index=True)
    expiry_warned_at = Column(DateTime, nullable=True)


class DirectoryHealth(Base):
    """The last health check of one :class:`ManagedDirectory`: is it up and answering.

    Infrastructure health only — the control plane's own state, domain controller status,
    whether the identity provider signs in, whether an on-prem DC answers LDAP. Nothing
    here describes accounts, groups or privilege: identity posture is BeyondTrust Identity
    Security Insights' data, and ``services/directory_health_service`` says why it is not
    duplicated here.

    One row per directory, overwritten by each check, so the page is one query and no
    provider calls. ``checks`` is a JSON list of ``{key, label, status, detail}``.

    ``pending_job_id`` is the agent_discover job probing an on-prem directory. The probe
    finishes on the agent's schedule, not the checker's, so its result is folded in when
    that job completes (``api/agent.complete``) rather than awaited.
    """
    __tablename__ = "directory_health"

    # Not a FK: a directory is soft-deleted (status="deleted"), and the health page skips
    # those rows by joining on the directory, so a stale row here is harmless.
    directory_id = Column(String(36), primary_key=True)
    status = Column(String(16), nullable=False, default="unknown")  # ok | warn | fail | unknown
    checks = Column(Text, nullable=True)
    checked_at = Column(DateTime, nullable=True, index=True)
    duration_ms = Column(Integer, nullable=True)
    pending_job_id = Column(String(36), nullable=True, index=True)
    pending_since = Column(DateTime, nullable=True)
    # When the last probe was queued. Separate from checked_at, which every pass moves,
    # because on-prem probes run on their own, slower cadence.
    probe_queued_at = Column(DateTime, nullable=True)
    # The domain controller deep check (builtin:ad-dc-health, an agent_ansible run): the job
    # in flight, since when, and when the last one was queued. Its own cadence again --
    # it checks out a Password Safe credential per run, so it runs hours apart.
    deep_job_id = Column(String(36), nullable=True, index=True)
    deep_since = Column(DateTime, nullable=True)
    deep_queued_at = Column(DateTime, nullable=True)


class HealthCheck(Base):
    """The last health of one object, in one health area (``services/health``).

    The general form of :class:`DirectoryHealth`: one row per ``(area, object_id)``,
    overwritten by the area's collector, so the Health page is one query and no provider
    calls. ``object_id`` is the collector's own stable id for the object (a tenant id, a
    gateway id, an inventory item id). ``checks`` is a JSON list of
    ``{key, label, status, detail}``, exactly as on DirectoryHealth.

    Infrastructure health only, by the same rule: nothing here describes accounts, groups
    or privilege. That is BeyondTrust Identity Security Insights' data.
    """
    __tablename__ = "health_checks"

    area = Column(String(32), primary_key=True)
    object_id = Column(String(160), primary_key=True)
    name = Column(String(255), nullable=False, default="")
    kind = Column(String(64), nullable=True)          # what the object is, for display
    href = Column(String(255), nullable=True)         # where to act on it
    status = Column(String(16), nullable=False, default="unknown", index=True)
    checks = Column(Text, nullable=True)
    checked_at = Column(DateTime, nullable=True)


class HealthArea(Base):
    """When each health area's collector last ran, and how that went.

    Separate from the rows because a collector can fail as a whole -- Password Safe down,
    say -- and that must read as "could not check", not as every account failing. A failed
    run records ``last_error`` here and leaves the area's rows as they were.
    """
    __tablename__ = "health_areas"

    area = Column(String(32), primary_key=True)
    last_run_at = Column(DateTime, nullable=True)
    last_ok_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)


class EntitleVirtualApp(Base):
    """An Entitle **Virtual Application** the dashboard created over a directory's groups.

    Each role on it fronts a role Entitle already synced from the directory's pinned
    integration (a group's membership role), under a friendly name and a tier's workflow.
    Nothing is created in the directory itself. ``tf_state_json`` is the scrubbed state
    ``entitle_registration_service.deregister`` destroys it from; the app holds no
    credential, so the scrub has nothing to remove and is applied anyway.

    ``directory_id`` is not a FK, for the same reason ``linked_directory_id`` is not:
    unregistering the directory is refused while an app is live, and a dangling id reads
    as "directory gone" rather than cascading a delete Entitle would never hear about.
    """
    __tablename__ = "entitle_virtual_apps"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    directory_id = Column(String(36), nullable=False, index=True)
    name = Column(String(50), nullable=False)
    # applying | active | failed | destroying | deleted
    status = Column(String(16), nullable=False, default="applying", index=True)
    integration_id = Column(String(64), nullable=True)
    # JSON list: {key, name, resource, tier, virtualized_role_id, virtualized_role_name,
    # group_name, persona, cloud, role_id}. role_id is filled once Entitle created it.
    roles_json = Column(Text, nullable=False, default="[]")
    tf_state_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class EntitleBundleSet(Base):
    """One apply of persona × cloud bundles (``services/entitle_persona_bundles``).

    A set rather than a row per bundle because they are applied, and destroyed, as one
    Terraform state. ``bundles_json`` records what each bundle resolved to at apply time
    (its role ids, and which components were missing) so the page can show it later
    without asking Entitle again.
    """
    __tablename__ = "entitle_bundle_sets"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    status = Column(String(16), nullable=False, default="applying", index=True)
    bundles_json = Column(Text, nullable=False, default="[]")
    tf_state_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class SpireLab(Base):
    """Inventory of dashboard-provisioned SPIRE trust domains for the Password Safe
    SPIFFE SVID plugin's lab.

    One row per **trust domain**, because that is what the plugin's Managed System
    models — not per VM. Unlike the Certificate Lab there is no Terraform module and no
    standing cost of its own: a SPIRE server is a Go binary and a sqlite file on an
    ordinary Linux VM the dashboard already deploys and reaps.

    **The argument for a timer here is not cost, it is that a forgotten trust domain
    keeps minting.** 8081 is an API that issues identities, so a lab nobody remembers
    is an identity provider nobody is watching. Deregistering the managed system is
    the kill switch, since the plugin implements no Enable/Disable Managed Account.

    **Nothing secret lives on this row.** The admin credential is a PKCS#12 that
    ``spire-admin-identity.yml`` writes straight into Secrets Safe under ``no_log``;
    this row carries only the two TITLES that name it. The same rule covers the
    connection identity the operator picks for the Ansible runs: a source *ref*, a
    Password Safe *id and account name*, and a *username* — resolved at run time, never
    stored. ``trust_bundle_pem`` is public
    by construction — it is what every consumer of the trust domain has to trust, in
    the same sense as a CA chain — and it is stored because the operator has to paste
    it into the managed system and it fits nowhere else.
    """
    __tablename__ = "spire_labs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(120), nullable=False)
    # The bare DNS-style name ('weaverlab.test'), never a spiffe:// URI. Baked into the
    # server config, every SPIFFE ID and the Managed System, so it is immutable once the
    # server is installed — which is why the build form asks for it before anything else.
    trust_domain = Column(String(255), nullable=False)
    cloud = Column(String(20), nullable=False, default="azure")
    region = Column(String(64), nullable=True)

    # The provider's own id, carrying everything teardown needs and nothing else: an ARM
    # id on Azure, "<region>/<instance-id>" on AWS, "<project>/<zone>/<name>" on GCP.
    # `terminate` gets only this string — an instance id alone does not say which
    # regional endpoint owns it. See the per-cloud host backends in spire_lab_service.
    vm_resource_id = Column(String(500), nullable=True)
    vm_name = Column(String(120), nullable=True)
    # BOTH, because they answer different questions and confusing them is the whole of
    # the reachability trap: `private_ip` is what the Resource Broker dials on 8081, and
    # `public_ip` is what the Ansible runner SSHes to when the runner is not in-subnet.
    # Either may be blank.
    private_ip = Column(String(64), nullable=True)
    public_ip = Column(String(64), nullable=True)
    bind_port = Column(Integer, nullable=False, default=8081)
    status = Column(String(32), nullable=False, default="provisioning", index=True)

    # The cloud ACL actually applied, as a comma-joined CIDR list, plus the rule's name.
    # Recorded rather than recomputed so teardown closes exactly what provision opened,
    # and so an operator can see why the broker cannot reach the server: corp egress
    # rotates between two Cloudflare addresses, so a rule pinned to one fails
    # intermittently and looks like a credential problem.
    source_cidrs = Column(Text, nullable=True)
    firewall_name = Column(String(120), nullable=True)

    # Progress through the four playbooks, as the names of the ones that finished. The
    # provision is a sequence of separate `ansible_local` job rows, so this is what tells
    # a resumed or half-failed lab apart from one that never started.
    stages_done = Column(Text, nullable=True)
    # The `ansible_local` job ids, in order, so the page can link each stage's Live
    # Output. A failed stage's log is the only place its Ansible error exists.
    stage_job_ids = Column(Text, nullable=True)
    # How SPIRE runs on the lab host: "vm" (tarball + systemd, the original), "docker"
    # (the official images under Docker Compose) or "k8s" (k3s + the official Helm
    # charts). NULL reads as "vm", which is every lab built before this existed. See
    # spire_lab_service.stages_for and the CLI-prefix note there.
    deployment_mode = Column(String(16), nullable=True)
    # The SPIRE release this lab runs, as built or last upgraded: a SPIRE version for vm /
    # docker, "chart <version> / SPIRE <version>" for k8s (the play sets the chart's
    # images; a lab recorded as a bare "chart <version>" predates that, and is offered
    # the upgrade). NULL is a lab built before this was recorded -- unknown, so the page
    # offers the upgrade.
    spire_version = Column(String(32), nullable=True)

    # ── The Kubernetes half ──────────────────────────────────────────────────
    # A SECOND HOST on the same row rather than a second row: the trust domain is still
    # the thing being modelled, and the k3s node is an attribute of this lab. It holds one
    # SPIRE agent attested into this domain and is meaningless without it.
    #
    # ATTACHED, NEVER CREATED — the same rule as the SPIRE host and for the same reason:
    # privileged playbooks against a host of the caller's choosing is not something this
    # should accept, so both hosts come from the dashboard's own deploy rows.
    k8s_vm_resource_id = Column(String(500), nullable=True)
    k8s_vm_name = Column(String(120), nullable=True)
    # The same two-address split, and the same trap: `k8s_private_ip` is what the SPIRE
    # host's ACL is opened TO and what the agent dials the server from; `k8s_public_ip` is
    # what the Ansible runner SSHes to. Either may be blank.
    k8s_private_ip = Column(String(64), nullable=True)
    k8s_public_ip = Column(String(64), nullable=True)

    # linking | linked | failed, or NULL for "never attempted". Deliberately SEPARATE from
    # `status`: the governance half is what most labs are built for and it stands on its
    # own, so a failed link must not make a working trust domain read as broken.
    k8s_status = Column(String(32), nullable=True)
    k8s_error_message = Column(Text, nullable=True)
    k8s_stages_done = Column(Text, nullable=True)
    k8s_stage_job_ids = Column(Text, nullable=True)

    # The strings that have to agree in three places at once — the registration entry, the
    # API server's AuthenticationConfiguration, and the workload's SPIFFE_JWT_AUDIENCE.
    # Stored rather than recomputed because the panel shows them and because a re-link must
    # not quietly change one: an audience that stops matching rejects every token, and
    # Kubernetes says nothing useful about why.
    k8s_audience = Column(String(120), nullable=True)
    k8s_workload_spiffe_id = Column(String(255), nullable=True)
    k8s_workload_uid = Column(Integer, nullable=True)
    k8s_workload_role = Column(String(120), nullable=True)
    # Spelled exactly as the discovery document advertises it. Kubernetes matches the
    # issuer STRING, so a trailing slash or a swapped host is a total failure with no
    # diagnostic — which is why it is recorded rather than rebuilt from parts each time.
    k8s_issuer_url = Column(String(255), nullable=True)

    # SPIFFE federation with the dashboard's own SPIRE server (docs/preview/workload-lab/spiffe.md,
    # "Federating with the dashboard"): federating | federated | failed, or NULL for never.
    # Separate from `status` for the reason k8s_status is: a failed federation must not
    # make a working trust domain read as broken.
    federation_status = Column(String(32), nullable=True)
    federation_error = Column(Text, nullable=True)
    federated_at = Column(DateTime, nullable=True)
    # What the proof showed: the linked k3s workload's own SVID response carrying the
    # dashboard's bundle (spire-federation-proof.yml), or why not. NULL = no proof run.
    federation_proof = Column(Text, nullable=True)
    # The attribute probe (docs/runbooks/spire-lab-standup.md §5): JSON holding what
    # "Prepare probe" did against Password Safe (each call's status included) and, once an
    # operator has read Verify Functional Account, the answer they recorded. L3 waits on it.
    attr_probe = Column(Text, nullable=True)

    # WHO the runner logs in as ON THE K3S NODE. A SEPARATE SET from the four
    # `ansible_*` / `login_user` fields above, which belong to the SPIRE host, because the
    # two VMs are deployed independently and **do not share an SSH key**.
    #
    # ALL NULL IS THE NORMAL STATE AND IT ALREADY WORKS: the runner auto-derives a keypair
    # from the deploy job of the VM it is CONNECTING TO (`_find_cloud_deploy_meta` matches
    # on the target address), and the Kubernetes stages target this host, so each machine
    # gets its own key with nothing configured.
    #
    # What these exist for is the explicit case. Inheriting the SPIRE host's chosen account
    # or key secret here would be the wrong fallback, not a convenient one: it would connect
    # to this VM with another VM's credential, and the failure is
    # `Permission denied (publickey)` five stages into a run that looked configured. So a
    # NULL here means auto-derive from THIS host's deploy job, never "whatever the SPIRE
    # host uses".
    k8s_ansible_secret_ssh_key_source = Column(Text, nullable=True)
    k8s_ansible_managed_account = Column(Text, nullable=True)
    k8s_ansible_managed_become_self = Column(Boolean, nullable=True)
    k8s_login_user = Column(String(104), nullable=True)

    # What the seed put in and what discovery should return. 11 in, 8 out — and the
    # count IS the assertion, not "discovery succeeded". The plugin shipped with
    # discovery defaulting its path filter to the MINTABLE prefix, which silently
    # narrowed the inventory to 2 while still reporting success.
    entries_seeded = Column(Integer, nullable=True)
    discovery_expected = Column(Integer, nullable=True)

    # Secrets Safe coordinates of the admin credential — TITLES only, never values.
    admin_secret_folder = Column(String(255), nullable=True)
    admin_spiffe_id = Column(String(255), nullable=True)
    ps_safe = Column(String(120), nullable=True)
    # Public by construction, and ~900 B for one authority / ~1.8 KB across a CA
    # rotation. It is 7x Password Safe's 255-character address column, which is why it
    # is held here and pasted rather than composed into an address like every other
    # custom plugin's configuration.
    trust_bundle_pem = Column(Text, nullable=True)
    # The REAL notAfter of the minted admin SVID, which is shorter than what was asked
    # for: SPIRE clamps a mint to the server's `ca_ttl`, so a 720h request against the
    # 168h default yields ~7 days. Once it lapses every plugin action fails
    # PERMISSION_DENIED and reads exactly like an admin_ids problem, so the date is
    # stored and surfaced rather than left in a job log.
    admin_svid_expires_at = Column(DateTime, nullable=True)

    # Password Safe onboarding. `ps_system_id` is written by `start_ps_register` and is
    # what "this trust domain is governed" means; a row carrying one an operator created by
    # hand is still recognised, which is why these predate the button.
    #
    # `ps_account_id` stays NULL BY DESIGN and is not dead weight. The SPIFFE SVID plugin
    # DISCOVERS its accounts — each is a SPIRE registration entry — so there is no single
    # account this lab owns, and creating one would move the discovery count that is the
    # lab's whole assertion (eleven seeded, eight discovered). The column remains because a
    # hand-onboarded lab may name one, and because its emptiness is the fact worth being
    # able to read.
    ps_system_id = Column(String(36), nullable=True)
    ps_account_id = Column(String(36), nullable=True)
    # Scrubbed Terraform state for the managed system, which is what makes the removal
    # deterministic: `ps_resource_service.deregister` destroys from the recorded state
    # rather than from a hand-typed id. Holding the id alone would leave nothing able to
    # remove what was created. Same split as CertLab.ps_tf_state and WorkloadK8sToken's.
    ps_tf_state = Column(Text, nullable=True)

    # WHO the Ansible runner logs in as, when the operator chose explicitly. The first
    # two are EITHER/OR — two answers to one question — and `provision` refuses both at
    # once, because a managed account's name also becomes `ansible_user`, so a run
    # carrying both would connect as one identity holding the other's key.
    #
    # All NULL is the normal state and means what every lab built before this did: the
    # runner auto-derives the keypair from this host's own deploy job. On the ROW rather
    # than in the parent job's metadata because every stage's `vars_for` builder reads
    # only the row and config (see spire_lab_service, "never the request"), which is what
    # lets a resumed provision rebuild an identical run.
    #
    # The `ansible_` prefix is load-bearing: `ps_system_id` / `ps_account_id` above are
    # the SPIFFE functional account being ONBOARDED into Password Safe, which is a
    # different account for a different purpose.
    #
    # Text, not String(n): a source is a config-secret registry key OR a raw
    # `bt_safe://<safe>/<folder>/<title>` vault ref, which has no bounded length, and the
    # ref JSON grows with the account name. Issue #830 — SQLite enforces no VARCHAR
    # length and PostgreSQL does, so a too-long value passes every test here and then
    # dies mid-INSERT with StringDataRightTruncation (see spire_lab_service.batch_id_for,
    # which is this repo's write-up of that exact failure).
    ansible_secret_ssh_key_source = Column(Text, nullable=True)
    ansible_managed_account = Column(Text, nullable=True)   # ManagedAccountRef as JSON
    # "Also use that account for sudo." All four playbooks are `become: true`, so a
    # non-root account without passwordless sudo fails on the first task. Sends the SAME
    # ref as `managed_become`; Password Safe reuses the open request rather than opening
    # a second one (btapi_service passes ConflictOption=reuse). NULL = false.
    ansible_managed_become_self = Column(Boolean, nullable=True)
    # A USERNAME, never a credential. NULL = derive it from ansible_<cloud>_user as
    # before, which is right for every host using its cloud's default login. Needed
    # because an SSH-key secret carries no user, unlike a managed account whose own name
    # becomes the login.
    login_user = Column(String(104), nullable=True)

    error_message = Column(Text, nullable=True)
    deploy_job_id = Column(String(36), nullable=True)
    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=True)
    # Auto-delete timer — NULL = never, never "inherit the default" (see Job.expires_at).
    expires_at = Column(DateTime, nullable=True, index=True)
    expiry_warned_at = Column(DateTime, nullable=True)


class AgentCell(Base):
    """One non-human principal the agent demo cell installed, and what governs it.

    **The row records an authorization, not a credential.** Same rule as ``SpireLab``,
    which holds the *titles* of its admin credential and never the PKCS#12: the raw PAT
    is shown once by ``api/tokens.create_token`` and never again, and this row carries
    only its id, its name and when it expires. The dashboard is not the vault, and a
    table that held a working token would make every backup one.

    **Two identifiers, deliberately, because they are two different things.** The SPIFFE
    ID is who the worker *is* -- attested, re-fetched every loop, stored nowhere on the
    host. The PAT is what it may *do* here -- scoped to ``pat_user_id``'s RBAC, expiring,
    revocable. They are not bridged: ``/mcp`` takes a Bearer PAT and has no mTLS path, so
    nothing mints one from the other. ``docs/profiles/demo/agent-demo-cell.md`` says why
    that gap is stated rather than closed.

    **Attached, never created** -- the host is a VM this dashboard already deployed, and
    ``host_deploy_job_id`` is the proof of it. ``spire_lab_service.resolve_host`` is what
    re-derives it, because a privileged playbook against a host of the caller's choosing
    is not something this should accept. The consequence is the good one: the host keeps
    its auto-delete timer, its Password Safe onboarding and its Destroy button, and this
    row adds no teardown of its own beyond revoking what it issued.
    """
    __tablename__ = "agent_cells"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(120), nullable=False)
    status = Column(String(32), nullable=False, default="provisioning", index=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100), nullable=True)
    workgroup = Column(String(100), nullable=True)

    # ── The host it attached to ──────────────────────────────────────────────
    # The deploy job is the proof the host is one this dashboard built. Resolved through
    # spire_lab_service.resolve_host rather than taken from the request.
    host_deploy_job_id = Column(String(36), nullable=True)
    host_name = Column(String(120), nullable=True)
    cloud = Column(String(20), nullable=True)
    # What the Ansible runner SSHes to. Blank is legal -- an in-subnet runner uses the
    # private address -- which is why the two are not collapsed into one column.
    private_ip = Column(String(64), nullable=True)
    public_ip = Column(String(64), nullable=True)

    # ── Identity: who the worker is ──────────────────────────────────────────
    # The SPIRE lab whose trust domain attests it, and the ID it will name in its logs.
    spire_lab_id = Column(String(36), nullable=True)
    trust_domain = Column(String(255), nullable=True)
    spiffe_id = Column(String(500), nullable=True)

    # ── Authorization: what it may do here ───────────────────────────────────
    # The PAT's ID and NAME only. Never the token. `pat_user_id` is what bounds every MCP
    # tool the worker can call, so it is the actual scope of the agent's access and the
    # first thing to look at when asking what this agent could see.
    pat_id = Column(String(36), nullable=True)
    pat_name = Column(String(100), nullable=True)
    pat_user_id = Column(String(36), nullable=True)
    # Always set by the cell, never None. A non-expiring token for a non-human principal
    # is the thing this demo argues against, so agentcell_service refuses to mint one.
    pat_expires_at = Column(DateTime, nullable=True)
    pat_revoked_at = Column(DateTime, nullable=True)
    # The OAuth client the worker authenticates with, when the cell minted one instead of
    # a PAT. Its id only -- the secret was returned once and is not here. Revoking the cell
    # deactivates that client, which ends every access token it issued on the next call.
    oauth_client_id = Column(String(36), nullable=True)

    # ── What this agent is answerable for in the Workload Lab ───────────────
    # Which lab identity an operator has made this agent answerable for, so that "what
    # does this agent have access to" is one lookup rather than a conversation.
    # `linked_mechanism` is a Workload Lab tab name; `linked_credential_id` is that tab's
    # own row.
    #
    # ALL THREE SHIPPED MECHANISMS ARE CAPABILITIES, and what they differ in is WHICH
    # AUTHORITY the worker reaches -- which is the distinction that replaced "does this
    # confer anything at all":
    #   * `kubernetes` and `certificates` reach PASSWORD SAFE holding nothing (a workload
    #     identity brokered by Workload Credentials), so the worker can genuinely REQUEST
    #     that credential -- subject to an access policy that can hold the request for a
    #     human;
    #   * `cloud` reaches WORKLOAD CREDENTIALS DIRECTLY and mints. No vault in the chain
    #     and no approval, because there is nothing standing to release -- and every
    #     mint is billed. See `agentcell_service.SPENDABLE_MECHANISMS`.
    #
    # This comment has been wrong twice, in two different ways, and both are worth
    # keeping. It said no worker could spend ANY of them, which stopped being true when
    # the worker got `--token-source ps`. It then said `cloud` was accountability only
    # because "that tab returns its credential to nobody" -- a sentence that was always
    # about the DASHBOARD, is still true of the dashboard, and was being read as a
    # statement about the mechanism. The worker calling WC itself is exactly what keeps
    # the dashboard out of that audit trail.
    # See docs/design/next-demo-cells.md sections 5b, 5d, 5e and 5f.
    linked_mechanism = Column(String(32), nullable=True)
    linked_credential_id = Column(String(36), nullable=True)
    linked_at = Column(DateTime, nullable=True)

    # ── The current cluster-access episode, and NEVER its credential ─────────
    # One bounded request at a time: the agent asks Password Safe for the linked token,
    # waits for whatever the access policy requires, probes the cluster, and gives the
    # slot back. What is recorded is the REQUEST -- its id, its state, when it started
    # and when it was released -- because that is what an operator needs to answer "is
    # something out right now" and what makes an abandoned slot findable.
    #
    # THERE IS NO TOKEN COLUMN HERE AND THERE MUST NOT BE. The same rule the four lab
    # models state: the row names the credential, Password Safe holds it. A token that
    # landed here would outlive the request that fetched it, which is the entire property
    # the recorded-request flow exists to provide.
    # "requested" or "released" -- the only two the DASHBOARD writes. The waiting, the
    # approval and the probes happen on the host, and the worker cannot report them back:
    # its PAT belongs to a non-admin user, and these endpoints need `config_mgmt:write`.
    # Granting it that so it could file progress would give a non-human principal more
    # authority than this cell argues for. The journal is the truth for what happened;
    # this answers "is something out right now". See agentcell_service.ROW_STATES.
    episode_state = Column(String(20), nullable=True)
    episode_started_at = Column(DateTime, nullable=True)
    episode_released_at = Column(DateTime, nullable=True)
    # THERE IS NO `episode_request_id` OR `episode_result` HERE, and their absence is a
    # decision rather than an omission. A first draft had both. Neither can ever be
    # filled: the WORKER opens the Password Safe request and runs the probes, so the
    # dashboard never learns the id or the outcome, and the worker has no way to tell it
    # (see above). A column that is structurally always NULL is worse than no column --
    # it reads as "nothing happened yet" on a row where plenty did, and it invites a
    # later change to grant the worker the authority needed to fill it.
    #
    # `journalctl -u mcp-agent` holds both, and the SPIFFE ID in the request's reason is
    # what correlates the two.

    # Progress through the two playbooks, as the names of the ones that finished, and
    # their job ids -- the same arrangement SpireLab uses, and for the same reason: a
    # failed stage's Ansible error exists only in that job's Live Output.
    stages_done = Column(Text, nullable=True)
    stage_job_ids = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)


class WorkloadK8sToken(Base):
    """A Password-Safe-brokered ServiceAccount token for a workload OUTSIDE the cluster.

    The Workload Lab's third answer, and the only one that works on a managed cluster.
    The SPIRE lab proves a workload can reach an API server with a credential stored
    nowhere, but that rests on ``--authentication-config``, a kube-apiserver flag — so it
    is available on self-managed k3s and on **none** of EKS, AKS or GKE. This is the path
    that is: a bound ServiceAccount token, minted by the API server, rotated and audited
    by Password Safe, retrieved by the consumer for each run.

    **One row per IDENTITY, not per cluster**, which is why this is its own table rather
    than more columns on ``K8sCluster``. That model has room for exactly one registration
    (``ps_token_account_id`` plus one state key), and it is already spoken for by the PRA
    tunnel's ``pra-access`` account. A Deployer and a Reader on the same cluster are two
    ServiceAccounts, two managed accounts and two sets of RBAC, so they are two rows.

    ``cluster_id`` references ``k8s_clusters.id`` and is deliberately **not** a foreign
    key, matching how the sibling labs reference their hosts: the two delete paths stay
    independent, so unregistering a cluster cannot cascade away the audit record of a
    token that existed, and this row's own teardown is the only thing that removes the
    ServiceAccount it created.

    **THE PROFILE IS THE POINT, not the token.** ``profile`` picks the RBAC, and the RBAC
    is the whole demonstration:

      * ``deployer`` — ClusterRole ``edit`` through a **RoleBinding** in one namespace. A
        CI build that can deploy where it is supposed to and is refused everywhere else.
      * ``reader`` — ClusterRole ``view`` through a **ClusterRoleBinding**. A fleet scan
        that can read the whole cluster and **cannot read Secrets**, because upstream
        ``view`` omits them by design.

    Neither is ``cluster-admin``. The existing ``ps-token`` path onboards a cluster-admin
    ServiceAccount because it serves a human's brokered session through PRA; a *vaulted*
    cluster-admin token is a vaulted skeleton key and demonstrates nothing about scoping.

    **Nothing secret lives on this row** — the rule both sibling models state. The
    credential is the bearer token, it is held in Password Safe, and what is here is
    ``ps_system_id`` / ``ps_account_id``: ids that NAME it. There is no kubeconfig column
    either, deliberately: a kubeconfig containing the token would be a copy of the
    credential with no expiry and no audit trail, which is the artefact this feature
    exists to remove. The consumer assembles one at run time from what it retrieved.

    **The timer is not about cost.** It is the same argument ``docs/preview/workload-lab/spiffe.md`` makes for
    the SPIRE lab: a forgotten workload token keeps authenticating, and this row is the
    only page it appears on.
    """
    __tablename__ = "workload_k8s_tokens"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(120), nullable=False)

    # The registered cluster this identity lives in. Denormalised alongside it because
    # the row has to stay readable after the cluster is unregistered: an inventory entry
    # naming a cluster id nobody can resolve is a record of nothing, and the whole reason
    # the timer exists is to be read long after the fact.
    cluster_id = Column(String(36), nullable=False, index=True)
    cluster_name = Column(String(200), nullable=True)
    cloud = Column(String(20), nullable=True)

    # deployer | reader. See the class docstring — this selects the RBAC, and changing it
    # on an existing row would leave the old binding in the cluster, so it is set once.
    profile = Column(String(20), nullable=False, default="deployer")
    namespace = Column(String(253), nullable=False, default="default")
    service_account = Column(String(253), nullable=False)

    # bound | longlived, and the reason to record it is that it changes what the ROTATOR
    # is granted: bound gets `serviceaccounts/token` create and no access to Secrets at
    # all. `ttl_seconds` is meaningful in bound mode only, where the API server's own
    # floor is 600 (ps_k8s_token_service._MIN_BOUND_TTL clamps it).
    mode = Column(String(20), nullable=False, default="bound")
    ttl_seconds = Column(Integer, nullable=True)

    # Password Safe coordinates — IDS ONLY, never the credential. `ps_account_id` being
    # set is what "onboarded" means, and it is what the rotate path acts on.
    ps_system_id = Column(String(64), nullable=True)
    ps_account_id = Column(String(64), nullable=True, index=True)
    # The managed system address the plugin parses, e.g. `eks;us-east-2;prod;bound;ttl=600`.
    # Stored because the operator has to be able to see it: when it disagrees with the
    # cluster the rotation fails inside Password Safe, where this dashboard sees only a
    # generic failure.
    ps_address = Column(String(255), nullable=True)
    # `<namespace>/<serviceaccount>` — the managed ACCOUNT name, which is a name and not
    # a credential. Recorded so the consumer playbooks can be told what to look up.
    ps_account_name = Column(String(255), nullable=True)
    # The scrubbed Terraform state for the managed system + account, which is what makes
    # the teardown deterministic: `ps_resource_service.deregister` is fed the recorded
    # state rather than hand-typed ids, exactly as the CA and cluster paths are. Holding
    # the ids alone would leave nothing able to remove them — the ids are for display and
    # for the rotate call, this is what destroys. Scrubbed, so it carries no credential
    # (see CertLab.ps_tf_state, the same split for the same reason).
    ps_tf_state = Column(Text, nullable=True)
    # Whether a rotation has ever completed. A bearer token is far longer than the 128
    # characters Password Safe's create API accepts, so the account is always created
    # holding a placeholder — until this is true, the vault holds nothing that
    # authenticates, and that is indistinguishable from success on the page.
    rotated = Column(Boolean, nullable=True)

    status = Column(String(32), nullable=False, default="onboarding", index=True)
    error_message = Column(Text, nullable=True)
    # Progress through the onboarding steps, as the names of the ones that finished, and
    # the job ids behind them. Same purpose as the sibling labs': a half-failed onboard
    # has to be distinguishable from one that never started, and a failed step's error
    # exists only in its own job log.
    stages_done = Column(Text, nullable=True)
    stage_job_ids = Column(Text, nullable=True)

    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=True)
    # Auto-delete timer — NULL = never, never "inherit the default" (see Job.expires_at).
    expires_at = Column(DateTime, nullable=True, index=True)
    expiry_warned_at = Column(DateTime, nullable=True)


class WorkloadCloudCredential(Base):
    """A short-lived AWS or Azure credential for a workload, minted by Workload Credentials.

    The Workload Lab's fourth answer, and the one that replaces the most common non-human
    credential in existence: an `AWS_ACCESS_KEY_ID` sitting in a CI secret store with no
    expiry, no revocation, and no record of who read it. Workload Credentials mints an
    assumed-role triple (AWS) or a service-principal secret (Azure) on demand, against a
    **dynamic secret** an operator defined in WC, and hands back a LEASE.

    **THE SCOPE IS NOT DEFINED HERE, and that is the honest difference from the sibling
    tabs.** The Kubernetes tab picks a RoleBinding and the Certificate tab picks a profile,
    so the dashboard decides what the identity may do. Here the dynamic secret's own
    definition in Workload Credentials decides — which role is assumed, which subscription,
    which permissions. This row names the secret it draws from; it cannot widen or narrow
    it, and it must not pretend to.

    **NOT the dashboard's own lease.** ``workload_credential_lease`` is a singleton per
    ``(cloud, purpose)`` holding the credential THIS APPLICATION uses for its own cloud
    calls, configured by ``wlc_{cloud}_secret_name``. Minting into it from here would
    overwrite a working credential the dashboard is mid-deployment with — and issuance is
    billed, so it would also charge for the privilege. Rows here call
    ``workload_credentials_service`` directly and share none of that state.

    **Nothing secret lives on this row.** ``lease_id`` is a correlation handle, not a
    credential: it identifies an issuance in Workload Credentials' own audit log and is
    what a revoke acts on. The access key, secret, session token, client secret and tenant
    id are returned to the caller that asked and are never written here.

    **REVOCATION IS ASYMMETRIC AND THE ROW RECORDS WHICH.** Azure leases can be released
    early. AWS cannot — STS refuses with ``lease_not_revocable``, because a credential it
    has already signed cannot be withdrawn before its expiry. So on AWS the TTL is the only
    control, which makes a short one matter more there rather than less. ``revocable`` is
    stored rather than inferred at render time so the page cannot promise a kill switch the
    provider does not have.
    """
    __tablename__ = "workload_cloud_credentials"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(120), nullable=False)

    # aws | azure. Decides the credential SHAPE (an assumed-role triple versus a
    # service-principal secret) and whether a lease can be revoked at all.
    cloud = Column(String(20), nullable=False, index=True)

    # The Workload Credentials dynamic secret this identity draws from — a FOLDER and a
    # NAME, which are addresses and not secrets. The scope lives behind them, in WC.
    dynamic_name = Column(String(255), nullable=False)
    dynamic_folder = Column(String(255), nullable=True)
    # What this identity is for, in the operator's words. Free text on purpose: the
    # dashboard cannot enforce a purpose here (see the class docstring), so claiming a
    # closed vocabulary would imply a guarantee it cannot make.
    purpose = Column(String(120), nullable=True)
    # The TTL asked for. Recorded because what comes BACK may differ — the provider clamps,
    # and AWS caps a role-chained credential at one hour — so the gap between requested and
    # actual is worth being able to see.
    ttl_seconds = Column(Integer, nullable=True)

    # ── the current lease ────────────────────────────────────────────────────
    # A correlation handle to a LIVE credential, not the credential. Cleared when the lease
    # is revoked or retired so the row never names an issuance that no longer exists.
    lease_id = Column(String(128), nullable=True, index=True)
    lease_issued_at = Column(DateTime, nullable=True)
    # The provider's OWN expiry, as returned. Never computed from the requested TTL: the two
    # disagree whenever the provider clamps, and a page showing the ask rather than the
    # answer would tell an operator the credential lives longer than it does.
    lease_expires_at = Column(DateTime, nullable=True)
    # Whether THIS cloud's leases can be released early. See the class docstring.
    revocable = Column(Boolean, nullable=True)
    # How many times this identity has minted. Issuance is METERED, so this is a cost
    # figure as much as an audit one — and a number climbing on an identity nobody is using
    # is the specific shape of a misconfigured consumer retrying.
    issue_count = Column(Integer, nullable=False, default=0)

    status = Column(String(32), nullable=False, default="registered", index=True)
    error_message = Column(Text, nullable=True)
    # The issue/revoke job ids, in order, so each attempt's output stays readable.
    job_ids = Column(Text, nullable=True)

    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=True)
    # Auto-delete timer — NULL = never, never "inherit the default" (see Job.expires_at).
    expires_at = Column(DateTime, nullable=True, index=True)
    expiry_warned_at = Column(DateTime, nullable=True)


class CloudFunction(Base):
    """Inventory of dashboard-deployed cloud functions — Cloud Functions, Phase 1
    (docs/design/cloud-functions.md).

    One row per deployed function: an AWS Lambda, an Azure Linux Function App, or
    a GCP Cloud Run function, all running the same dashboard-authored handler from
    ``web_dashboard/functions/``. Unlike every other compute the dashboard drives,
    this one has a **stable inbound HTTPS endpoint**, which is what an Entitle REST
    integration needs in order to POST Give/Revoke Access (Phase 2).

    Two auth references, because the auth is layered and the halves are
    independent: ``invoke_secret_ref`` is the shared bearer secret the handler
    itself verifies (always set), and ``invoke_key_ref`` is the cloud's own front
    door where it produces a retrievable key (Azure host key only — AWS uses SigV4
    and GCP uses an OIDC token, neither of which is a stored credential).
    """
    __tablename__ = "cloud_functions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(200), nullable=False, index=True)
    workload = Column(String(64), nullable=False)          # fnworkloads/<workload>.py
    cloud = Column(String(20), nullable=False)             # aws | azure | gcp
    region = Column(String(64), nullable=True)
    provider = Column(String(40), nullable=True)           # lambda | function_app | cloudrun_function
    runtime = Column(String(20), nullable=True)            # cloud-specific spelling of python 3.12
    status = Column(String(32), nullable=False, default="deploying", index=True)

    resource_id = Column(String(255), nullable=True)       # ARN / Azure resource id / cloudfunctions2 id
    invoke_url = Column(String(500), nullable=True)        # the endpoint Entitle posts to
    package_sha256 = Column(String(64), nullable=True)     # deployed artifact hash — drives update detection
    package_uri = Column(String(500), nullable=True)       # s3:// | https://…blob… | gs://

    auth_mode = Column(String(32), nullable=True)          # AWS_IAM | NONE | function_key | run_invoker | none
    invoke_secret_ref = Column(Text, nullable=True)        # config://cloudfn/{id}/bearer  (ALWAYS set)
    invoke_key_ref = Column(Text, nullable=True)           # config://cloudfn/{id}/invoke-key (Azure only)

    network_mode = Column(String(16), nullable=False, default="public")  # public | vpc | vnet
    network_ref = Column(Text, nullable=True)              # JSON: subnet ids / connector / security groups
    env_ref = Column(Text, nullable=True)                  # JSON of the NON-secret env applied

    # Where the handler source came from, as JSON: the source-tree hash (always
    # present), plus git commit/ref/origin when the image was built with them.
    # Recorded at deploy so "what is running in that function, and who reviewed it?"
    # stays answerable after the image that produced it is gone.
    provenance = Column(Text, nullable=True)

    # Stored on the row (as K8sCluster does) rather than re-derived by scanning Job
    # metadata: it is a direct lookup and it survives job pruning.
    deploy_job_id = Column(String(36), nullable=True)
    entitle_integration_id = Column(String(64), nullable=True)  # Phase 2

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=True)


class K8sCluster(Base):
    """Inventory of dashboard-managed Kubernetes clusters — Kubernetes
    management (docs/cloud-services/kubernetes.md).

    One row per managed cluster. **Phase 1** records a cluster the dashboard
    can reach (provisioned out-of-band or registered from an existing
    kubeconfig) — lifecycle + kubeconfig-as-reference only, no kubectl
    wrapping. The kubeconfig is **cluster-admin**, so it's written to a
    secrets backend and only ``kubeconfig_ref`` is stored — resolved by
    ``config_service.get()``. Later phases fill ``mgmt_kind`` /
    ``mgmt_endpoint`` (management-plane launch, Phase 2), ``pra_jump_id`` (the
    native ``tunnel_type=k8s`` PRA jump, Phase 3) and ``secrets_delivery_kind``
    (in-cluster Password Safe ESO / Secrets-Agent, Phase 4).
    """
    __tablename__ = "k8s_clusters"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cloud = Column(String(20), nullable=False)             # aws | azure | gcp | local
    name = Column(String(200), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="registered", index=True)
    # §1.1a provisioning: source distinguishes a registered cluster (kubeconfig
    # only — deregister on delete) from a dashboard-provisioned one (terraform
    # destroy on delete); deploy_job_id locates its terraform/deployments/<id> state.
    source = Column(String(16), nullable=False, default="registered")  # registered | provisioned
    region = Column(String(40), nullable=True)             # cloud region (provisioned clusters)

    api_server = Column(String(255), nullable=True)        # cluster API URL (parsed from kubeconfig)
    kubeconfig_ref = Column(Text, nullable=True)           # backend-agnostic ref (resolved via config_service)
    deploy_job_id = Column(String(36), nullable=True)      # provisioning Job id → deploy/state dir (§1.1a destroy)
    # NAT/outbound public IP of a dashboard-PROVISIONED cluster (from the module's
    # nat_public_ip output). Auto-added to the Rancher node firewall as a /32 so the
    # cluster's cattle-cluster-agent can dial out to import. NULL for registered clusters.
    egress_ip = Column(String(45), nullable=True)

    mgmt_kind = Column(String(20), nullable=True)          # portainer | rancher | argocd | headlamp (Phase 2)
    mgmt_endpoint = Column(String(255), nullable=True)     # management-plane URL (Phase 2)
    pra_jump_id = Column(String(64), nullable=True)        # sra_protocol_tunnel_jump id (tunnel_type=k8s, Phase 3b)
    pra_tunnel_state = Column(Text, nullable=True)         # scrubbed Terraform state for the tunnel (drives teardown)
    # Per-cluster broker overrides — config defaults are the fallback (Phase 3b).
    jump_group = Column(String(128), nullable=True)        # PRA Asset Group name override (else bt_jump_group_name)
    jumpoint_name = Column(String(128), nullable=True)     # PRA Jumpoint name override (else bt_jumpoint_name) — the "separate jumpoint"
    pra_credential_ref = Column(String(256), nullable=True)  # secret ref → bt_client_secret override (else config)
    secrets_delivery_kind = Column(String(20), nullable=True)  # eso | secrets_agent (Phase 4)
    # Password-Safe-managed ServiceAccount token rotation. ps_token_account_id being
    # SET is the discriminator that stops the dashboard minting a second token: the
    # rotation plugin sweeps only Secrets carrying ITS labels, so a dashboard-minted
    # Secret alongside a managed one is a cluster-admin credential nothing ever rotates.
    ps_token_account_id = Column(String(64), nullable=True)      # PS ManagedAccount id of <ns>/<sa>
    ps_pra_vault_account_id = Column(String(64), nullable=True)  # PS ManagedAccount id of the "PRA Vault Token" mirror
    pra_vault_account_id = Column(String(64), nullable=True)     # sra_vault_token_account id the rotation is mirrored into

    # Workgroup scoping, same contract as CloudDatabase.workgroup: NULL means
    # creator-scoped, a name means everyone in that workgroup. The kubeconfig behind
    # this row is cluster-admin, so tagging one is a real grant, not a label.
    workgroup = Column(String(100), nullable=True, index=True)
    # Which agent runs this cluster's Kubernetes work: NULL is the remote worker. Only an
    # OWNED agent may be named — one the dashboard deployed (an agent_hosts row),
    # SPIRE-attested, with Kubernetes work allowed on its host — because the work hands it
    # a cluster-admin kubeconfig. Checked when set and again at every use
    # (k8s_service.agent_qualifies); one that stops qualifying sends the work back to the
    # worker. Migration 0009.
    executor_agent_id = Column(String(36), nullable=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    # Auto-delete timer — NULL = never (see Job.expires_at). Only ever stamped on a
    # `provisioned` row, for the same reason as CloudDatabase.expires_at: deleting a
    # registered cluster only drops the dashboard's record of it.
    expires_at = Column(DateTime, nullable=True, index=True)
    expiry_warned_at = Column(DateTime, nullable=True)


class Gateway(Base):
    """Inventory of dashboard-deployed BeyondTrust Gateway hosts.

    Until now a gateway was found, not tracked: the dashboard auto-ensured exactly
    one per cloud by a fixed name tag and reference-counted it against the resources
    using it. That answers "is there a gateway?" but not "what gateways do we have",
    which is the question an operator running several for session load actually has.

    Two kinds of row, distinguished by ``managed``:

      * ``managed=True`` — the auto-ensured shared gateway. Still reference-counted
        and still torn down when idle; the row is a record of it, not the control.
        Adopted on first ensure, so a gateway that already exists gets registered
        rather than duplicated.
      * ``managed=False`` — deployed on request from the Gateways tab. Never
        reference-counted, never auto-torn-down, no cap: three in us-central1 and
        two in us-east-2 is a normal configuration. Removed only when asked.

    ``name`` is the cloud resource name (EC2 ``Name`` tag / GCE instance / Azure VM)
    and is what keeps the two kinds apart in the cloud itself — the managed teardown
    acts on the managed name alone, so it can never reach a user-deployed host.
    """
    __tablename__ = "gateways"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cloud = Column(String(20), nullable=False, index=True)   # aws | azure | gcp
    region = Column(String(40), nullable=True, index=True)
    zone = Column(String(40), nullable=True)                 # GCP/Azure placement
    name = Column(String(200), nullable=False, index=True)   # cloud resource name
    status = Column(String(32), nullable=False, default="provisioning", index=True)
    # provisioning | running | error | deleting | deleted
    # …plus two the reconcile pass writes from what it observed rather than from what the
    # dashboard did (see gateway_service.reconcile):
    #   degraded — the host is up but no gateway task is running on it (AWS only, the one
    #              cloud where host and gateway are separate facts behind separate APIs)
    #   missing  — a REQUESTED gateway whose host is gone without anything having asked.
    #              A managed one goes to `deleted` instead: its host coming and going with
    #              demand is the normal life of that lifecycle, not an anomaly to report.
    managed = Column(Boolean, nullable=False, default=False, index=True)

    host_id = Column(String(128), nullable=True)             # EC2 instance id / VM name
    egress_ip = Column(String(45), nullable=True)            # what a node firewall allows
    deploy_job_id = Column(String(36), nullable=True)
    error = Column(Text, nullable=True)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class AgentRelease(Base):
    """A release of the remote agent image the dashboard may offer as an upgrade target.

    A version and the image DIGEST it is, nothing more. The dashboard never verifies the
    signature: the updater beside each agent does, against the identity pinned in that
    host's policy.yaml, so a wrong or forged row here can at most offer an upgrade every
    host refuses (docs/platform/spiffe/remote-agents/lifecycle.md).
    """
    __tablename__ = "agent_releases"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    version = Column(String(32), nullable=False, unique=True, index=True)
    # Where the image is: the project's repository, or a mirror's (an air-gapped site
    # mirrors the image and pins the mirror in its policy.yaml `upgrades.repository`).
    repository = Column(String(200), nullable=False, default="chrweav/dashboard-agent")
    digest = Column(String(71), nullable=False)        # "sha256:" + 64 hex
    notes = Column(Text)
    source = Column(String(16), nullable=False, default="manual")   # manual | registry
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100))


class AgentRollout(Base):
    """One release rolled out across the eligible agents, canary ring first.

    ``status``: ``canary`` (canary upgrades queued) -> ``default`` (canary all done, the
    rest queued) -> ``done``; or ``stopped`` the moment any upgrade it queued fails, with
    ``detail`` naming it. ``job_ids`` and ``skipped`` (agent -> reason) are JSON, so the
    page can say which agent got what without a join table.
    """
    __tablename__ = "agent_rollouts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    release_id = Column(String(36), ForeignKey("agent_releases.id"), nullable=False)
    change_window_id = Column(String(36), nullable=True)   # provenance only, like Job's
    status = Column(String(16), nullable=False, default="canary")
    canary_job_ids = Column(Text)                       # JSON list
    default_job_ids = Column(Text)                      # JSON list
    skipped = Column(Text)                              # JSON {agent name: reason}
    detail = Column(Text)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100))
    finished_at = Column(DateTime, nullable=True)


class AgentHost(Base):
    """A remote agent the dashboard deployed into a cloud, and that host's lifecycle.

    The agent itself is an ordinary ``remote_agents`` row: it enrols or attests, leases and
    is routed to exactly like one an operator built by hand. This row is the part only a
    DEPLOYED agent has — where its host runs, the policy the dashboard wrote for it, and
    when it should exist at all:

      * ``always`` — up until someone stops or deletes it;
      * ``until``  — up until ``until_at``, then stopped for good;
      * ``window`` — "persistent just in time": started ``lead_minutes`` before each
                     occurrence of a change window and stopped ``grace_minutes`` after it
                     closes.

    **Stopping destroys the host and keeps the agent.** Nothing runs and nothing is at rest
    between windows; the agent row, its Config-Management routes and the runs booked into
    the next window all survive, and each start is a fresh host with a fresh identity
    re-bound to the same agent row. ``status`` is ``parked`` in between.

    Config Management by construction: the dashboard writes this agent's ``policy.yaml``
    at launch from ``ansible_cidrs`` and ``max_concurrent``, with ``job_types:
    [agent_ansible]`` — plus ``agent_k8s`` and a ``k8s:`` block when ``k8s_enabled``
    (services/agent_host_service).
    """
    __tablename__ = "agent_hosts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # One host record per agent. CASCADE is for the ORM; agent_host_service deletes the
    # row explicitly before the agent, for the same SQLite reason delete_agent gives.
    agent_id = Column(String(36), ForeignKey("remote_agents.id", ondelete="CASCADE"),
                      nullable=False, unique=True, index=True)
    name = Column(String(63), nullable=False)               # the cloud resource name
    cloud = Column(String(20), nullable=False, index=True)  # aws | azure | gcp
    region = Column(String(40), nullable=False)
    zone = Column(String(40), nullable=True)                # where it actually landed (GCP)
    # The VPC / VNet / GCP network its host runs in (config_mgmt_route_service.network_of
    # form): the agent can reach only that network, so its routes are narrowed to it.
    network = Column(String(200), nullable=True)
    subnet = Column(String(400), nullable=True)             # blank = the cloud's VM subnet
    instance_size = Column(String(64), nullable=True)       # blank = the per-cloud default

    # What the dashboard writes into the agent's policy.yaml at launch.
    ansible_cidrs = Column(Text, nullable=False, default="[]")   # JSON list of ranges
    max_concurrent = Column(Integer, nullable=False, default=1)
    # Kubernetes work (agent_k8s, and k8s Config-Management plays) for the clusters that
    # name this agent. NULL is off. Takes effect at the next start: the policy is written
    # at launch. Migration 0009.
    k8s_enabled = Column(Boolean, nullable=True)

    mode = Column(String(16), nullable=False, default="always")  # always | until | window
    until_at = Column(DateTime, nullable=True)                   # UTC, mode "until"
    change_window_id = Column(String(36), nullable=True)         # mode "window"
    lead_minutes = Column(Integer, nullable=False, default=20)
    grace_minutes = Column(Integer, nullable=False, default=15)

    # provisioning | starting | running | stopping | parked | error | deleted
    status = Column(String(32), nullable=False, default="parked", index=True)
    host_id = Column(String(200), nullable=True)            # EC2 id / VM name / GCE name
    last_job_id = Column(String(36), nullable=True)         # the latest start/stop job
    error = Column(Text, nullable=True)
    # The schedule as the last sweep computed it, for the Agents page. Display only:
    # the sweep recomputes from the window every pass.
    next_start_at = Column(DateTime, nullable=True)
    next_stop_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


# ========== DATABASE UTILITIES ==========

def get_db() -> Session:
    """
    Dependency for FastAPI endpoints to get database session.
    Usage: def my_endpoint(db: Session = Depends(get_db))
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class BeyondTrustTenant(Base):
    """One BeyondTrust tenant a POV can be wired into.

    The reason the POV profile exists at all. A demo instance resolves its tenant from
    the global singletons — ``bt_api_host``, ``pscli_api_url``, ``entitle_api_url`` — and
    that is correct there, because there is exactly one. A POV instance runs several POVs
    at once and **each has its own PRA appliance and its own Password Safe Cloud tenant**,
    so "which tenant?" stops having one answer. See docs/profiles/pov/README.md.

    Modelled on ``HypervisorConnection``, which solved the same shape one layer down: N
    named rows replacing a singleton, credentials as Fernet ciphertext *or* an external
    reference, an ``is_default``, and a resolver that still falls back to the old config
    keys so nothing breaks on an install that has not seeded yet.

    **One row per product, not per customer.** ``PovEnvironment`` carries three separate
    FKs because the three are genuinely independent: a POV needs its own PRA appliance and
    its own Password Safe tenant, but Entitle is multi-tenant behind a REGIONAL API URL
    and is usually shared across every POV. A single row holding all three would force a
    duplicate Entitle credential per customer, and duplicated credentials rotate apart.

    **Why this is not the workgroup.** ``PovEnvironment.workgroup`` already exists and
    ``config_service`` already scopes values by it, so "just put the tenant on the
    workgroup" is the tempting shortcut. It is wrong: workgroup answers *who may see this*
    — RBAC, the inventory filters, the expiry exempt list all key off it — while a tenant
    answers *whose PRA appliance this is*. An SE running two customers' POVs inside one
    workgroup would get whichever tenant the workgroup named, silently, for both.

    **On customer data.** ``PovEnvironment`` deliberately has no customer field and adding
    one should be treated as a schema regression. This table is the one place that rule
    bends, and only exactly as far as it must: a PRA appliance is reached at a hostname
    like ``acme.beyondtrustcloud.com``, so the connection target inherently identifies
    who it belongs to and there is no way to hold one without that. A *connection target*
    you cannot avoid; a free-text "customer" box you can. ``name`` is therefore a
    constrained slug, like a POV's, and not somewhere to type a company.
    """
    __tablename__ = "beyondtrust_tenants"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # pra | password_safe | entitle — bt_tenant_service.VALID_KINDS
    kind = Column(String(16), nullable=False, index=True)
    name = Column(String(64), nullable=False)               # operator slug, unique per kind

    # PRA appliance host, Password Safe URL, or the Entitle API base.
    base_url = Column(String(255), nullable=False, default="")
    # OAuth client id for PRA and Password Safe. Blank for Entitle, which is a bearer
    # token with no paired id — a column that is empty for one kind beats a second table.
    client_id = Column(String(255))

    # Exactly one of these carries the credential. Same rule and the same two mechanisms
    # as HypervisorConnection, so there is one secret-at-rest story in this database and
    # one rotation hazard rather than two.
    secret_enc = Column(Text)              # Fernet (config_service.encrypt_value)
    secret_ref = Column(String(256))       # aws_sm:// | azure_kv:// | gcp_sm:// | bt_safe://

    # Per-kind NON-SECRET extras, JSON. Allowlisted per kind by the service and pinned by
    # a test: this is a free-form blob on a row that holds credentials, which is exactly
    # where a password ends up by accident.
    options = Column(Text)

    is_default = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    # Set by the Verify action. Derived state would be better, but a live credential check
    # is an outbound round trip nobody wants on a page load, so this is the cached answer
    # and `last_checked_at` is what stops it being read as current.
    last_checked_at = Column(DateTime)
    last_ok_at = Column(DateTime)
    last_error = Column(Text)

    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100))
    updated_at = Column(DateTime, default=utcnow)

    @property
    def options_dict(self) -> dict:
        if not self.options:
            return {}
        try:
            value = json.loads(self.options)
            return value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001
            return {}


from .models.pov import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    PovEnvironment, PovEnvironmentVM, PovUseCaseProgress,
    PovSignoff, PovAccessor, PovVendorUser,
    PovTemplateBuild, PovBlueprint, PovCloudTemplate,
    PovCloudTemplateVM,
)


def create_admin_user(username: str, password: str, workgroups: List[str] = None) -> User:
    """
    Create an admin user with access to all workgroups.

    Args:
        username: Admin username
        password: Plain text password (will be hashed)
        workgroups: List of workgroups to grant access to (default: all)

    Returns:
        Created User object
    """
    check_password_length(password)
    if workgroups is None:
        workgroups = list(settings.workgroups.keys())

    db = SessionLocal()
    try:
        # Check if user already exists
        existing_user = db.query(User).filter(User.username == username).first()
        if existing_user:
            print(f"User '{username}' already exists!")
            return existing_user

        # Create new user
        user = User(
            username=username,
            hashed_password=get_password_hash(password),
            full_name="Administrator",
            is_active=True,
            is_admin=True,
        )
        user.workgroups_list = workgroups

        db.add(user)
        db.commit()
        db.refresh(user)

        print(f"Admin user '{username}' created successfully with access to: {', '.join(workgroups)}")
        return user
    finally:
        db.close()


# Imported last: both modules read the models above, and ``bootstrap`` reads this package.
from .rbac_backfills import (  # noqa: E402,F401  (re-exported: init_db and tests use these names)
    _BACKFILL_V1_DELIBERATELY_EMPTY, _BACKFILL_V1_MARKER, _BACKFILL_V1_PHANTOM_ADMIN,
    _BACKFILL_V1_SCOPES, _EMPTY_MEANS_NOTHING_MARKER, _PRESENTER_OWN_ONLY_MARKER,
    _PRESENTER_OWN_POV_MARKER, _RETIRE_LEVELS_MARKER, _backfill_new_permission_scopes,
    _commit_marked, _grant_presenter_own_pov, _materialise_empty_permission_maps,
    _narrow_presenter_to_own_povs, _retire_unenforced_levels, _strip_retired,
)
from .bootstrap import (  # noqa: E402,F401  (re-exported: callers import init_db from here)
    _SQLITE_CREATE_ATTEMPTS, _create_all_sqlite, init_db,
)
