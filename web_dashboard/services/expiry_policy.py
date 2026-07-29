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
REAPABLE_KINDS = ("vm", "database", "k8s")

# Clouds whose VM teardown is a claimable job (the keys of _DESTROY_FOR, as inventory
# `cloud` values).
REAPABLE_VM_CLOUDS = ("aws", "azure", "gcp", "oci")

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
    hours = default_hours()
    if hours <= 0:
        return None
    return expiry_from_now(clamp_hours(hours), now=now)


def destroy_job_type(deploy_job_type: str) -> Optional[str]:
    """The destroy job_type matching a VM deploy type, or None if it has no queued
    teardown. Single source of truth for the reaper and its tests."""
    return _DESTROY_FOR.get(deploy_job_type)


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

    if kind not in REAPABLE_KINDS:
        return False, (f"{kind!r} resources have no auto-delete timer — a virtual-desktop "
                       f"seat is torn down with its pool, not individually.")
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
