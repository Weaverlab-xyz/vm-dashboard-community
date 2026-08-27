"""Pure policy for the auto-delete timer (resource expiry).

Answers three questions and nothing else: *what expiry should a new resource get*,
*may this resource be reaped right now*, and *what does an operator's extend/pin
request resolve to*. Stdlib + ``config_service`` only — no Session, no cloud SDK, no
models — so it is unit-testable by file path, mirroring ``ephemeral_secrets.py`` and
``managed_accounts.py``.

The sweep that acts on these answers lives in ``services/expiry_reaper.py``; the
operator-facing mutations live in ``api/expiry.py``.

Two properties matter more than anything else here, because this feature deletes
infrastructure:

  * **``expires_at`` NULL means "never", never "inherit the default."** :func:`expired`
    cannot return an item without a real timestamp, so enabling the feature on an
    existing fleet — whose rows all backfilled to NULL — selects zero resources. That
    is a property of the predicate, not of a guard someone could weaken. NULL is also
    how "pinned" is represented, so pin and clear-expiry are the same write.
  * **The floors below cannot be lowered by configuration.** They bound how wrong a
    misconfiguration can be, exactly as ``_CLOUD_RUN_REAP_AGE_MIN_FLOOR`` does for the
    stranded-runner reaper.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ── What is reapable ─────────────────────────────────────────────────────────
#
# A cloud VM has no inventory table: its deploy Job row IS its record of existence,
# which is why `job:<id>` is already its inventory id. Only these four deploy types
# have a teardown the reaper can hand to the queue.
#
# Deliberately absent: proxmox_deploy / nutanix_deploy. Their deletes (`proxmox_delete`,
# `nutanix_delete_vm`) run in-request and are NOT in jobs_worker.HANDLED_TYPES, so there
# is no job for the reaper to enqueue and it must not pretend otherwise. Also absent:
# the bulk PARENT types (ec2_bulk_deploy etc.) — the parent owns no VM, its children do,
# and the children carry the real deploy type. So excluding parents here is correct and
# needs no special case.
REAPABLE_VM_JOB_TYPES = ("ec2_deploy", "azure_deploy", "gce_deploy", "oci_deploy")

# deploy job_type → the destroy job_type its own endpoint creates. Kept next to the
# reapable set so the two can't drift apart.
_DESTROY_FOR = {
    "ec2_deploy":   "ec2_destroy",
    "azure_deploy": "azure_destroy",
    "gce_deploy":   "gce_destroy",
    "oci_deploy":   "oci_destroy",
}

# inventory `kind` values that can carry a timer. "desktop" is absent: a virtual-desktop
# seat's teardown is `vdesktop_pool_teardown(seat_ids)`, so expiring one seat would
# silently shrink a live pool. Gateways aren't in the inventory at all, and the shared
# one is reference-counted.
#
# "pov" is a whole lab environment on a third-party platform, and it is the kind this
# feature was most obviously missing: a POV bills for every VM in it, sits on
# `suspend_on_idle` after the evaluation ends, and nothing else in the codebase ever
# notices it is finished. Its teardown is the same `pov_env_destroy` job the DELETE
# endpoint creates, which is what makes it reapable at all.
REAPABLE_KINDS = ("vm", "database", "k8s", "pov")

# Clouds whose VM teardown is a claimable job (the keys of _DESTROY_FOR, as inventory
# `cloud` values).
REAPABLE_VM_CLOUDS = ("aws", "azure", "gcp", "oci")

# `source` on a row that inventory_service read out of the hypervisor sync cache.
#
# Its own value rather than reusing "registered": a registered database is one the
# dashboard holds a RECORD for and could deregister, whereas this is a read-through
# cache of somebody else's hypervisor, where the dashboard holds no record at all and
# expiring a row would delete a cache entry and leave the VM running.
SYNCED_HYPERVISOR_SOURCE = "hypervisor"

# Per-kind states that mean "healthy and idle" — the only ones a reap may act on. Never
# a row mid-provision, mid-decommission, or failed.
#
# This mapping fails safe by construction: an unrecognised status string is refused, so
# a new state added elsewhere in the codebase can only ever make the reaper do LESS.
_REAPABLE_STATES = {
    # inventory_service._vm_item hardcodes "active" for every live deploy job.
    "vm":       frozenset({"active"}),
    # cloud_database_service marks a finished provision "available".
    "database": frozenset({"available"}),
    # k8s_service lands a finished provision on "registered", and the management-plane
    # path also produces "managed" / "awaiting_agent".
    "k8s":      frozenset({"registered", "managed", "awaiting_agent"}),
    # Exactly `pov_env_service._ACTIONABLE` — the set that module already uses to decide
    # whether a POV may be destroyed at all. Written out here rather than imported because
    # this module must stay loadable by file path with nothing but stdlib and the config
    # layer (see tests/test_expiry_wiring.py::test_the_policy_module_stays_pure), which is
    # also why every other entry above is a literal. The copy is pinned to the original by
    # tests/test_pov_expiry.py, so it cannot drift silently.
    #
    # `failed` belongs here for the reason pov_env_service gives: a POV that failed halfway
    # through provisioning is the one that most needs reaping, because whatever it did
    # create is still billing.
    "pov":      frozenset({"active", "failed"}),
}


# ── Floors an operator cannot lower ──────────────────────────────────────────

# The shortest lifetime that may be stamped. A 5-minute timer on an EKS cluster would
# expire before the cluster finished provisioning.
MIN_TTL_MINUTES_FLOOR = 60

# Never act at the instant of expiry: absorbs clock skew between the app and the worker,
# and gives a mis-stamped resource a window of its own.
REAP_GRACE_MIN_FLOOR = 15

# How long the feature must have been enabled before the first reap. A freshly flipped
# toggle cannot act on a fleet nobody has reviewed — there is a guaranteed hour, with at
# least one sweep report on disk, between "on" and any destruction.
ARM_DELAY_MINUTES = 60

# Ceiling on resource_expiry_max_per_pass. Bounds the damage rate even if every other
# brake is released at once.
MAX_PER_PASS_CEILING = 50

# Fallbacks used when config and settings both fail to produce a number.
_DEFAULT_GRACE_MIN = 30
_DEFAULT_SWEEP_INTERVAL_MIN = 30
_DEFAULT_MAX_PER_PASS = 10
_DEFAULT_MAX_TOTAL_HOURS = 720
_DEFAULT_EXTEND_HOURS = 24
_DEFAULT_WARN_HOURS = 24
# 0, exactly like `resource_expiry_default_hours`, and for the same reason: flipping only
# the master switch must still change nothing. An operator opts a POV estate in by setting
# a number — see pov_default_hours for why it is a separate number.
_DEFAULT_POV_HOURS = 0
_DEFAULT_SWEEP_RETENTION_DAYS = 7


# ── Config access ────────────────────────────────────────────────────────────
#
# Every read goes through here rather than being captured at import, so a Settings
# change takes effect on the next sweep pass without an app restart (config_service's
# 5s process cache bounds the lag).

def _cs():
    from . import config_service
    return config_service


def _flag(key: str, default: bool) -> bool:
    try:
        return _cs().get_bool(key, default)
    except Exception:                                  # pragma: no cover - defensive
        logger.debug("expiry: could not read %s, using %s", key, default)
        return default


def _int(key: str, default: int) -> int:
    """A resource_expiry_* integer, from config → settings → literal default."""
    try:
        raw = _cs().get(key, "")
    except Exception:                                  # pragma: no cover - defensive
        raw = ""
    if raw in (None, ""):
        try:
            from ..config import settings
            raw = getattr(settings, key, default)
        except Exception:                              # pragma: no cover - defensive
            raw = default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    """Master switch. Off means nothing is stamped, swept, or displayed."""
    return _flag("resource_expiry_enabled", False)


def enforce() -> bool:
    """The second, separate gate on DELETION. Enabling the feature is observe-only
    until this is set, so an operator can watch a full cycle before arming it."""
    return _flag("resource_expiry_enforce", False)


def dry_run() -> bool:
    """Report what would be deleted and delete nothing. Defaults ON."""
    return _flag("resource_expiry_dry_run", True)


def allow_never() -> bool:
    """Whether an admin may clear a timer outright. Off means the most anyone can do
    is extend, so every stamped resource keeps a timer."""
    return _flag("resource_expiry_allow_never", False)


def default_hours() -> int:
    """Lifetime stamped on new deployments. 0 = don't stamp (the feature is inert)."""
    return max(0, _int("resource_expiry_default_hours", 0))


def pov_default_hours() -> int:
    """The default lifetime for a new POV environment. 0 = do not stamp one.

    Its own knob rather than `default_hours` because the two describe different things. A
    cloud VM's default is a working-day sort of number; a POV is an *evaluation* that runs
    for weeks with a customer inside it. Sharing one number would force an operator to
    choose which of the two gets a wrong default, and reaping a customer's lab on a
    setting meant for scratch VMs is much the worse half of that trade.

    Falls back to `resource_expiry_default_hours` only when it is ALSO unset — that is,
    never silently: the fallback is 0 either way, so an operator who has set a VM default
    and not a POV one gets no POV timers rather than VM-length ones.
    """
    hours = _int("pov_expiry_default_hours", _DEFAULT_POV_HOURS)
    return hours if hours > 0 else 0


def extend_hours() -> int:
    return max(1, _int("resource_expiry_extend_hours", _DEFAULT_EXTEND_HOURS))


def max_total_hours() -> int:
    """Ceiling on any single resource's total lifetime, counted from its creation.
    0 = no ceiling."""
    return max(0, _int("resource_expiry_max_total_hours", _DEFAULT_MAX_TOTAL_HOURS))


def warn_hours() -> int:
    """How far ahead a resource counts as "expiring soon". Served to the clients so
    /inventory's badge and the dashboard's warning can't use different thresholds."""
    return max(1, _int("resource_expiry_warn_hours", _DEFAULT_WARN_HOURS))


def grace_minutes() -> int:
    return max(REAP_GRACE_MIN_FLOOR, _int("resource_expiry_grace_minutes", _DEFAULT_GRACE_MIN))


def sweep_interval_seconds() -> int:
    """Loop cadence for the background sweeper (see main._expiry_sweeper_loop)."""
    minutes = max(5, _int("resource_expiry_sweep_interval_minutes", _DEFAULT_SWEEP_INTERVAL_MIN))
    return minutes * 60


def sweep_min_gap_seconds() -> int:
    """Shortest gap allowed between two sweep *rows* — the dedupe window
    ``expiry_reaper.enqueue_sweep_if_due`` enforces on top of its active-pass check.

    Half the interval, which is the only value that satisfies both constraints at once:
    long enough that the two ``gunicorn -w 2`` loops ticking within a second of each other
    collapse to one row, and short enough that it can never suppress the *next* scheduled
    tick, whatever the operator sets the interval to.

    Derived rather than configurable on purpose. It is a property of the deployment
    topology (two app workers on one timer), not a preference, so there is nothing here for
    an operator to get right or wrong.
    """
    return max(1, sweep_interval_seconds() // 2)


def sweep_retention_days() -> int:
    """How long a *completed* sweep row is kept on /jobs. 0 = keep forever.

    Needed because the sweep is the only job type that writes a row on a timer whether or
    not it had anything to do: at the 30-minute default that is 48 rows a day, and nothing
    else prunes the ``jobs`` table. Mirrors ``notify_policy.retention_days`` — including
    its rule that only successful rows expire, since a failed pass is evidence.
    """
    return max(0, _int("resource_expiry_sweep_retention_days", _DEFAULT_SWEEP_RETENTION_DAYS))


def max_per_pass() -> int:
    """How many resources one sweep may act on, clamped to MAX_PER_PASS_CEILING."""
    n = _int("resource_expiry_max_per_pass", _DEFAULT_MAX_PER_PASS)
    return max(1, min(MAX_PER_PASS_CEILING, n))


def exempt_workgroups() -> frozenset:
    """Casefolded workgroup names that never get a timer. CSV or JSON list, matching
    how the admission_* list settings are written.

    Note this only covers VMs — a database, cluster or desktop seat carries no
    workgroup — so it is a convenience, not a complete exemption axis.
    """
    try:
        raw = _cs().get("resource_expiry_exempt_workgroups", "") or ""
    except Exception:                                  # pragma: no cover - defensive
        raw = ""
    if not raw:
        try:
            from ..config import settings
            raw = getattr(settings, "resource_expiry_exempt_workgroups", "") or ""
        except Exception:                              # pragma: no cover - defensive
            raw = ""
    raw = raw.strip()
    if not raw:
        return frozenset()
    if raw.startswith("["):
        import json
        try:
            return frozenset(str(w).strip().casefold() for w in json.loads(raw) if str(w).strip())
        except Exception:
            return frozenset()
    return frozenset(w.strip().casefold() for w in raw.split(",") if w.strip())


# ── Time helpers ─────────────────────────────────────────────────────────────
#
# The DB stores naive UTC (`datetime.utcnow`), so everything here is naive UTC too.
# Mixing in an aware datetime would raise on subtraction.

def _now() -> datetime:
    return datetime.utcnow()


def parse_ts(value) -> Optional[float]:
    """Epoch seconds for an ISO string / datetime, or None when it can't be read.

    None is the "don't touch it" answer everywhere downstream — the reaper only ever
    acts on a resource it can date.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            text = str(value).strip()
            if text.endswith("Z"):
                text = text[:-1]
            dt = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None) - dt.utcoffset()
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):        # pragma: no cover - defensive
        return None


def clamp_hours(hours, *, created_at=None, is_admin: bool = False,
                now: Optional[datetime] = None) -> float:
    """Clamp a requested lifetime to the configured floor and ceiling.

    The floor (``MIN_TTL_MINUTES_FLOOR``) is absolute — nobody, admin included, may
    stamp a 1-minute timer, because a provision that hasn't finished yet would be its
    first victim. The ceiling is ``max_total_hours`` measured from ``created_at`` when
    given, so N extensions can't outrun it; admins bypass the ceiling only.

    ``now`` must be threaded from the caller when one was supplied: a request resolved
    against one clock and clamped against another can disagree by whatever the two
    differ by, which is unobservable in production and makes the whole thing untestable.
    """
    try:
        h = float(hours)
    except (TypeError, ValueError):
        h = 0.0
    floor_h = MIN_TTL_MINUTES_FLOOR / 60.0
    h = max(floor_h, h)
    ceiling = max_total_hours()
    if ceiling and not is_admin:
        if created_at is not None:
            elapsed = ((now or _now()) - created_at).total_seconds() / 3600.0
            remaining = ceiling - elapsed
            # Never return less than the floor: a resource already past its ceiling
            # should collapse to the soonest legal expiry, not to a time in the past.
            h = max(floor_h, min(h, remaining))
        else:
            h = min(h, float(ceiling))
    return h


def expiry_from_now(hours, *, now: Optional[datetime] = None) -> datetime:
    return (now or _now()) + timedelta(hours=float(hours))


def resolve_expiry(*, created_at, current, extend_hours_req=None, absolute=None,
                   never: bool = False, is_admin: bool = False,
                   now: Optional[datetime] = None):
    """Resolve one extend/set/pin request to ``(expires_at, clamped)``.

    ``expires_at`` is None for a cleared timer ("never"). ``clamped`` is True when
    policy shortened what was asked for, which the caller reports rather than 400s on:
    the intent was "keep this longer", so honour what policy allows and let the UI
    explain. Raises ValueError for a request policy refuses outright.

    An extend is relative to the CURRENT expiry, not to now — extending a resource with
    12h left by 24h leaves 36h, not 24h. Extending one with no timer starts from now.
    """
    now = now or _now()
    if never:
        if not allow_never():
            raise ValueError("Clearing a resource's timer is disabled by policy "
                             "(resource_expiry_allow_never).")
        if not is_admin:
            raise ValueError("Only an administrator may clear a resource's timer.")
        return None, False

    if absolute is not None:
        target = absolute
        if target.tzinfo is not None:
            target = target.replace(tzinfo=None) - target.utcoffset()
        requested_h = (target - now).total_seconds() / 3600.0
    elif extend_hours_req is not None:
        base = current if (current and current > now) else now
        requested_h = ((base - now).total_seconds() / 3600.0) + float(extend_hours_req)
    else:
        raise ValueError("No expiry change requested.")

    allowed_h = clamp_hours(requested_h, created_at=created_at, is_admin=is_admin, now=now)
    # Compare in whole minutes: a sub-minute difference is float noise from the
    # timestamp arithmetic, not a clamp the operator should be told about.
    clamped = int(allowed_h * 60) < int(requested_h * 60)
    return expiry_from_now(allowed_h, now=now), clamped


# ── Stamping new resources ───────────────────────────────────────────────────

def default_expiry_for(job_type: str, *, workgroup=None,
                       now: Optional[datetime] = None) -> Optional[datetime]:
    """The expiry a newly created Job row should carry, or None for no timer.

    Called from ``job_service.create_job`` — the one funnel every cloud VM deploy row
    passes through, single deploys and bulk children alike — so no provider can be
    forgotten and a fifth cloud is covered the day it is added. The set-membership test
    comes first so the ~40 job types that are not VM deploys cost one dict lookup and
    no config read.

    Returns None unless the feature is on, a default is configured, the job type is a
    reapable VM deploy, and the workgroup isn't exempt. The clock starts here, at
    enqueue, so the lifetime includes provisioning time — minutes for a VM, up to ~20
    for an EKS cluster.
    """
    if job_type not in REAPABLE_VM_JOB_TYPES:
        return None
    if not enabled():
        return None
    if workgroup and workgroup.strip().casefold() in exempt_workgroups():
        return None
    hours = default_hours()
    if hours <= 0:
        return None
    return expiry_from_now(clamp_hours(hours), now=now)


def default_expiry_for_kind(kind: str, *, source: str = "provisioned",
                            now: Optional[datetime] = None) -> Optional[datetime]:
    """The expiry a newly provisioned database / cluster row should carry.

    ``source`` must be "provisioned". A *registered* resource is one the dashboard was
    merely told about — deleting it only drops our own record — so a timer on one would
    eventually make the dashboard silently forget somebody's production database. That
    is worse than no timer, so registered rows are never stamped (and, independently,
    never reaped: see :func:`reap_target`).
    """
    if kind not in REAPABLE_KINDS or kind == "vm":
        return None
    if (source or "provisioned") != "provisioned":
        return None
    if not enabled():
        return None
    # A POV is an evaluation, not a scratch VM: weeks rather than a working day. Read
    # AFTER the master switch above, not before — a default that outran `enabled()` would
    # stamp timers on an instance where the feature is off, and the whole "NULL means
    # never, so turning it on acts on nothing that already exists" property depends on
    # nothing being stamped while it is off.
    hours = pov_default_hours() if kind == "pov" else default_hours()
    if hours <= 0:
        return None
    return expiry_from_now(clamp_hours(hours), now=now)


def destroy_job_type(deploy_job_type: str) -> Optional[str]:
    """The destroy job_type matching a VM deploy type, or None if it has no queued
    teardown. Single source of truth for the reaper and its tests."""
    return _DESTROY_FOR.get(deploy_job_type)


# The metadata each cloud's DELETE endpoint puts on its destroy job, as
# (target_key_in_deploy_metadata, extra_required_keys). The reaper rebuilds exactly this
# shape so the job it enqueues is indistinguishable from one the Destroy button created —
# see build_destroy_metadata.
_DESTROY_META = {
    "ec2_deploy":   ("instance_id",   ("region",)),
    "azure_deploy": ("vm_name",       ("resource_group",)),
    "gce_deploy":   ("instance_name", ("zone", "project_id")),
    "oci_deploy":   ("instance_ocid", ()),
}


def build_destroy_metadata(deploy_job_type: str, deploy_meta: dict, deploy_job_id: str):
    """``(destroy_job_type, metadata)`` for reaping a VM, or ``(None, reason)``.

    Every value is read from the deploy job's own metadata and **never re-derived from
    current config**. That is the whole point: each DELETE endpoint already resolves and
    persists these at deploy time precisely so the runner doesn't re-resolve them later.
    ``api/azure.py`` spells out why — re-deriving would read whatever
    ``azure_resource_group`` is configured *now* rather than the group the VM actually
    went into — and ``api/gcp.py`` is blunter still: "a destroy aimed at the wrong project
    is the worst version of this bug."

    So a deploy job missing a key its destroy needs is REFUSED, with a reason the operator
    sees in the sweep report, rather than reaped against a guessed default. That case is
    real: a VM deployed before multi-region support recorded no ``region``. Refusing means
    it shows up as needing a manual delete — which is right, because the alternative is a
    destroy pointed somewhere nobody chose.
    """
    spec = _DESTROY_META.get(deploy_job_type)
    destroy_type = _DESTROY_FOR.get(deploy_job_type)
    if not spec or not destroy_type:
        return None, (f"{deploy_job_type} has no queued teardown, so it cannot be "
                      f"auto-deleted")
    target_key, extra = spec
    meta = deploy_meta or {}

    target = meta.get(target_key)
    if not target:
        return None, (f"its deploy job recorded no {target_key} — the cloud resource "
                      f"cannot be identified, so delete it manually")
    out = {target_key: target, "deploy_job_id": deploy_job_id}
    for key in extra:
        value = meta.get(key)
        if not value:
            return None, (f"its deploy job recorded no {key}, and re-deriving one from "
                          f"current config could aim the destroy at the wrong "
                          f"{key.replace('_', ' ')} — delete it manually")
        out[key] = value
    return destroy_type, out


# ── The warning ladder ───────────────────────────────────────────────────────
#
# Everything else in this feature warns ONCE, `warn_hours` before expiry, and that is the
# right shape for a VM: one message, one owner, one decision.
#
# A POV is not that. It has been running for weeks, the person who created it has moved
# on to the next evaluation, and a single mail a day before a customer's lab disappears is
# a mail that gets read afterwards. So a POV warns on a LADDER, tightening as the deadline
# approaches, and each rung fires at most once.
#
# Descending, in minutes before expiry. The last rung is deliberately close enough that
# somebody at a keyboard can still act on it.
WARN_LADDER_MINUTES = (7 * 24 * 60,   # a week
                       3 * 24 * 60,   # three days
                       24 * 60,       # a day
                       4 * 60,        # four hours
                       60)            # an hour


def next_warn_stage(remaining_s: float, warned_stage_minutes) -> Optional[int]:
    """The ladder rung to warn about now, or None.

    ``warned_stage_minutes`` is the TIGHTEST rung already warned (the latch on the row);
    None means none yet. Returns the tightest rung the remaining time has crossed that is
    still looser than — or equal to — nothing already sent.

    Two properties this shape buys, both of which a plain "warn when remaining < X" gets
    wrong:

      * **A missed rung does not fire late.** A sweep runs every 30 minutes and a POV
        created inside the window may cross two rungs between passes. Returning the
        TIGHTEST crossed rung means it sends "expires in 4 hours", not a stale "expires in
        3 days" followed by four more mails in quick succession.
      * **An extend re-opens the whole ladder**, because the caller clears the latch — see
        ``expiry_reaper.set_expiry``. The new deadline gets its own full set of warnings
        rather than being permanently silenced by a rung crossed against the old one.

    Past expiry returns None: something already overdue is the reaper's business, and
    "expires soon" about a resource being destroyed right now is noise.
    """
    if remaining_s <= 0:
        return None
    remaining_min = remaining_s / 60.0
    crossed = [m for m in WARN_LADDER_MINUTES if remaining_min <= m]
    if not crossed:
        return None
    stage = min(crossed)                               # the tightest rung crossed
    if warned_stage_minutes is not None and stage >= int(warned_stage_minutes):
        return None                                    # this rung, or a tighter one, is sent
    return stage


# ── Eligibility, as the UI and the server both see it ────────────────────────

def ttl_capable(item: dict) -> tuple:
    """``(eligible, reason_when_not)`` for an inventory item.

    Deliberately shaped like the ``cfg_runnable``/``cfg_reason`` pair
    ``inventory_service.collect`` already computes, and used the same way: the page
    renders the control only when eligible and shows this reason on hover, so an
    operator learns why a row can't have a timer instead of finding out from a 400.
    One predicate, so the page and the server can never disagree.
    """
    kind = item.get("kind")
    cloud = (item.get("cloud") or "").lower()
    source = item.get("source") or "provisioned"

    # FIRST, and by its own rule rather than falling through the `source != "provisioned"`
    # test below. That test would refuse this too, but its reason says "registered", which
    # is not what this is — and a safety property that holds by accident is one refactor
    # away from not holding. This one branch covers both the reaper (reap_target calls
    # ttl_capable) and the operator (expiry_reaper.set_expiry calls it before writing), so
    # a timer cannot be stamped on one of these by hand either.
    if source == SYNCED_HYPERVISOR_SOURCE:
        return False, ("this VM was discovered on a hypervisor, not deployed by the "
                       "dashboard — there is no teardown to run, so it can never be "
                       "auto-deleted. Delete it from its hypervisor instead.")
    if kind not in REAPABLE_KINDS:
        return False, (f"{kind!r} resources have no auto-delete timer — a virtual-desktop "
                       f"seat is torn down with its pool, not individually.")
    # A POV is always dashboard-provisioned (the row only exists because this dashboard
    # created the environment) and its `cloud` is a lab platform rather than one of the
    # four VM clouds, so it must skip both tests below. Checked here rather than by
    # widening them, because "registered" and "which cloud can I destroy in" are questions
    # that simply do not apply to it.
    if kind == "pov":
        return True, ""
    if source != "provisioned":
        return False, ("registered resources are never auto-deleted — the dashboard didn't "
                       "provision this one, and deleting it here would only drop the "
                       "dashboard's own record.")
    if kind == "vm" and cloud not in REAPABLE_VM_CLOUDS:
        return False, (f"{cloud or 'this provider'} VMs have no queued teardown, so they "
                       f"cannot be auto-deleted (supported: "
                       f"{'/'.join(REAPABLE_VM_CLOUDS)}).")
    return True, ""


def is_exempt(item: dict) -> bool:
    """Whether an item's workgroup is on the exemption list."""
    wg = (item.get("workgroup") or "").strip().casefold()
    return bool(wg) and wg in exempt_workgroups()


def state_is_idle(item: dict) -> bool:
    """Whether an item's state means "healthy and idle" — the only states a reap may act
    on. Never a row mid-provision, mid-decommission or failed.

    Fails safe by construction: an unrecognised status string is refused, so a new state
    added anywhere else in the codebase can only ever make the reaper do LESS work, never
    make it act on live work.
    """
    states = _REAPABLE_STATES.get(item.get("kind"), frozenset())
    return (item.get("state") or "") in states


# ── The reap predicate ───────────────────────────────────────────────────────

def armed(armed_at_ts: Optional[float], now_ts: float) -> bool:
    """Whether the arming delay has elapsed since the feature was first seen enabled.

    False when ``armed_at_ts`` is None — an un-armed feature has never completed a pass,
    so nobody has read a report yet.
    """
    if not armed_at_ts:
        return False
    return (now_ts - armed_at_ts) >= ARM_DELAY_MINUTES * 60


def deletion_active(enforce_since_ts: Optional[float], now_ts: float) -> bool:
    """Whether DELETION may happen at all, independent of any single resource.

    Turning off report-only mode is the moment this feature becomes destructive, and the
    dangerous case is specific: an operator watches in report-only for a week, 40
    resources quietly pass their expiry, and unchecking one box makes all 40 eligible at
    once. So enforcement gets its own arming delay, on the same clock as the feature's —
    a guaranteed hour, with the full backlog listed on /inventory and in the sweep report,
    to extend or pin anything still needed.

    A delay rather than a permanent carve-out for the backlog: refusing forever to reap
    anything that expired before enforcement was enabled would leave a set of resources
    the feature can see, reports every pass, and can never act on. That is a worse
    failure — a growing pile of overdue resources nobody deletes — than making the
    operator wait an hour.

    Requires both ``enforce`` and ``not dry_run``; ``enforce_since_ts`` is None until a
    pass has observed both, so the clock starts from an actual observation rather than
    from whenever the setting was written.
    """
    if not enforce() or dry_run():
        return False
    if not enforce_since_ts:
        return False
    return (now_ts - enforce_since_ts) >= ARM_DELAY_MINUTES * 60


def expired(items: list, now_ts: float, *, grace_min: int) -> list:
    """Ids in ``items`` whose expiry is at least ``grace_min`` minutes past.

    ``items`` is ``[{"id": <inventory id>, "expires_ts": <epoch seconds or None>}]``.

    Mirrors ``ephemeral_secrets.expired`` shape-for-shape with the default for unknown
    age INVERTED, and the inversion is the point: that GC reaps an ephemeral it can't
    date, because leaking a stale credential is worse than deleting one. Here the blast
    radius is a live VM, so anything that can't be dated is left alone. A falsy, zero or
    negative ``expires_ts`` is never returned — which is exactly what makes enabling the
    feature on an existing fleet (all NULL) a no-op.

    The grace is floored at ``REAP_GRACE_MIN_FLOOR`` whatever the config says, so a
    misconfigured 0 cannot make the sweeper act at the instant of expiry.
    """
    cutoff = now_ts - max(REAP_GRACE_MIN_FLOOR, int(grace_min or 0)) * 60
    out = []
    for it in items or []:
        exp = it.get("expires_ts")
        if not exp or exp <= 0:
            continue
        if exp <= cutoff:
            out.append(it.get("id"))
    return [i for i in out if i]


def reap_target(item: dict, *, now_ts: float, grace_min: int,
                armed_at_ts: Optional[float]) -> Optional[dict]:
    """Describe a resource that is safe to auto-delete, or None to leave it alone.

    Six guards, all of which must hold:

      * the feature has been ARMED for at least ``ARM_DELAY_MINUTES``, so a freshly
        flipped toggle cannot act on a fleet nobody has reviewed;
      * the item is ``ttl_capable`` — a reapable kind, provisioned rather than
        registered, and for a VM one of the four clouds whose teardown is a claimable
        job (Proxmox/Nutanix deletes run in-request, so there is nothing to enqueue);
      * its workgroup is not exempt;
      * ``state`` is in ``_REAPABLE_STATES`` for its kind — never a row mid-provision,
        mid-decommission or failed. Fails safe by construction: an unrecognised status
        is refused, so a state added elsewhere can only make the reaper do less;
      * ``expires_at`` parses to a real timestamp (see :func:`parse_ts`);
      * it is at least ``grace`` past that expiry (see :func:`expired`).
    """
    if not armed(armed_at_ts, now_ts):
        return None

    capable, _ = ttl_capable(item)
    if not capable:
        return None
    if is_exempt(item):
        return None
    if not state_is_idle(item):
        return None

    kind = item.get("kind")
    expires_ts = parse_ts(item.get("expires_at"))
    if not expires_ts:
        return None
    if not expired([{"id": item.get("id"), "expires_ts": expires_ts}],
                   now_ts, grace_min=grace_min):
        return None

    return {
        "id":         item.get("id"),
        "kind":       kind,
        "cloud":      (item.get("cloud") or "").lower(),
        "name":       item.get("name") or item.get("id"),
        "region":     item.get("region") or "",
        "job_id":     item.get("job_id"),
        "expires_at": item.get("expires_at"),
        "overdue_s":  int(now_ts - expires_ts),
    }
