"""Durable, cross-process store for provider-issued credential leases.

``workload_credentials_service`` mints credentials; this module decides *whether we are
allowed to mint again* and *what to hand out when the answer is no*. Deliberately the same
shape as ``cost_cache`` — read that module's docstring first, because the two share three
invariants and diverge in two ways that matter.

**A failure never overwrites a success.** The payload column is written only when a
generate succeeded; a failure writes the error and cooldown columns and leaves the working
credential exactly where it was. For a cost figure the consequence of getting this wrong is
a blank tile. Here it is worse: a cleared credential is indistinguishable from a deployment
that was never on the dynamic tier at all, so the dashboard would silently look
mis-configured rather than broken.

**The state is shared and durable.** ``gunicorn -w 2`` plus ``jobs_worker`` is three
processes. A process-local dict would give each one its own lease — and unlike a cached
number, a lease is *billable*. Workload Credentials charges per issuance, so three
processes minting independently is three times the invoice for one credential, and every
image rebuild would discard them and re-mint.

**A failing configuration is left alone.** ``cooldown_until`` stops a bad dynamic-secret
name turning every page load into another billable failed attempt.

Two divergences from ``cost_cache``:

*There is a process-local memo in front of the row.* ``aws_service._aws_kwargs`` is called
for **every** boto3 client, and the home page fans out to ~22 endpoints against
``pool_size=5 + max_overflow=5``; a database read per client would be the pool-exhaustion
failure all over again. The memo is a read-through cache with a short TTL, exactly like
``config_service``'s 5-second one — the row remains the authority, so this is not the
process-local-state hazard that the table exists to avoid.

*An expired lease is not servable.* ``cost_cache`` can serve a stale payload because a
month-old figure is still a figure. A credential past ``expires_at`` is refused by the
cloud, so there is no "stale but usable" state here.

One rule inherited wholesale, and the easiest to get wrong: **sessions never span the
network call.** Claim in one short transaction, close it, generate with no session held,
then open a second session to record. The advisory lock makes the claim atomic and is
released by the claim's own commit — it is never held across the provider call.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import socket
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

CLOUDS = ("aws", "azure")
PURPOSE_PROVISION = "provision"
PURPOSE_READONLY = "readonly"
PURPOSES = (PURPOSE_PROVISION, PURPOSE_READONLY)

# Distinct from cost_cache's namespace so the two features cannot block each other.
_LOCK_ID = 20260820
_CLOUD_LOCK_KEYS = {"aws": 1, "azure": 2}

# How long a claim is honoured before another process may take over. Generous relative to
# a generate (which is one HTTP round trip) so a slow provider does not cause two
# processes to mint concurrently — the outcome that costs money.
_CLAIM_SECONDS = 60

# Never hand out a credential this close to expiry: a call that starts inside the window
# can still be in flight when the credential dies, and the resulting AccessDenied looks
# like a permissions bug rather than an expiry.
_EXPIRY_SAFETY_SECONDS = 60

# Process-local read-through memo. Short enough that another process's refresh is picked
# up promptly; long enough that a page fan-out is one database read, not twenty.
_MEMO_TTL_SECONDS = 20.0

# Backoff after a failed generate. Deliberately coarse: the realistic causes are a wrong
# secret name or a revoked token, and neither is fixed by retrying quickly.
_FAIL_BASE_SECONDS = 60
_COOLDOWN_MAX_SECONDS = 900

# How long a process that lost the claim race waits for the winner before giving up.
_WAIT_TOTAL_SECONDS = 8.0
_WAIT_POLL_SECONDS = 0.25

_memo: dict = {}
_memo_lock = threading.Lock()

# Which purpose the current task needs. Empty means "decide from configuration".
#
# A context variable rather than a parameter because `aws_service._aws_kwargs` receives
# only a region — it is called from ~50 sites and has no idea whether the caller is about
# to describe an instance or terminate one. Threading an operation label through all of
# them is the refactor this design exists to avoid; the job boundary is a seam the code
# already understands.
#
# contextvars are snapshotted per Task at create_task time, so this must be entered
# INSIDE the job's own task — see jobs_worker._run_job, which enters `correlation` there
# for exactly the same reason. asyncio.to_thread copies the context, so the synchronous
# credential lookups a job makes off the event loop inherit it.
_purpose_ctx = contextvars.ContextVar("wlc_purpose", default="")


@contextmanager
def provisioning():
    """Mark the current task as needing write privilege.

    Everything outside such a block gets the everyday lease, which carries no IAM. That
    is the point of the split: `iam:PassRole` and `iam:CreateRole` exist only while a job
    is actually running, so a credential lifted from the lease row at an arbitrary moment
    cannot escalate.
    """
    token = _purpose_ctx.set(PURPOSE_PROVISION)
    try:
        yield
    finally:
        _purpose_ctx.reset(token)


class LeaseUnavailable(Exception):
    """The cloud is on the dynamic tier but no usable credential could be produced.

    Raised rather than returning None so the caller cannot silently fall back to a static
    credential the operator may have deliberately retired. Callers surface it; they do not
    swallow it.
    """


# ── Config ────────────────────────────────────────────────────────────────────

def _cfg(key: str, default: str = "") -> str:
    # The import is inside the try on purpose: these two helpers gate the entire feature,
    # and they are consulted from _aws_kwargs, which runs for every boto3 client. If
    # config_service were ever unimportable (a startup import cycle, say) an escaping
    # ImportError would surface from every AWS call in the app. Degrading to the default
    # means "not on the dynamic tier", which is the correct fail-safe.
    try:
        from . import config_service
        return config_service.get(key) or default
    except Exception:  # noqa: BLE001
        return default


def _cfg_bool(key: str) -> bool:
    try:
        from . import config_service
        return config_service.get_bool(key, default=False)
    except Exception:  # noqa: BLE001
        return False


def dynamic_enabled(cloud: str) -> bool:
    """Whether `cloud` should take its credentials from Workload Credentials.

    Both the master preview flag and the per-cloud flag must be on. Two gates rather than
    one so enabling the feature to browse secrets cannot silently reroute a cloud's
    credentials as a side effect.
    """
    if cloud not in CLOUDS:
        return False
    return _cfg_bool("workload_credentials_enabled") and _cfg_bool(f"wlc_{cloud}_enabled")


def secret_name_for(cloud: str, purpose: str) -> str:
    """The configured dynamic-secret name, or "" when none is set.

    A blank readonly name falls back to the provisioning secret rather than failing: the
    read-only split is an optional refinement, and an operator who has not created the
    second secret should still get a working dashboard.
    """
    if purpose == PURPOSE_READONLY:
        return (_cfg(f"wlc_{cloud}_readonly_secret_name")
                or _cfg(f"wlc_{cloud}_secret_name"))
    return _cfg(f"wlc_{cloud}_secret_name")


def has_distinct_readonly(cloud: str) -> bool:
    """Whether `cloud` has a read-only dynamic secret of its own."""
    return bool(_cfg(f"wlc_{cloud}_readonly_secret_name"))


def purposes_for(cloud: str) -> tuple:
    """The purposes that warrant a lease of their own for `cloud`.

    ``readonly`` appears only when its own dynamic secret is configured. Without one, a
    caller asking for it is served the provisioning lease instead — one lease, one
    issuance.

    This is not a micro-optimisation. Minting a second lease from the *same* dynamic
    secret under a different purpose label bills twice for one credential, and nothing
    reads the copy. Iterating a static list of purposes here is exactly that bug, and it
    is invisible: two rows appear, both refresh forever, and the only symptom is the
    invoice.
    """
    if not dynamic_enabled(cloud) or not _cfg(f"wlc_{cloud}_secret_name"):
        return ()
    if has_distinct_readonly(cloud):
        return (PURPOSE_PROVISION, PURPOSE_READONLY)
    return (PURPOSE_PROVISION,)


def default_purpose(cloud: str) -> str:
    """The purpose to use when a caller did not name one.

    ``provision`` inside a job (set by :func:`provisioning`), the everyday lease
    otherwise. Before a second dynamic secret exists there is only one lease, so
    everything resolves to ``provision`` and behaviour is exactly as it was — an operator
    opts into the split by creating that secret, not by upgrading.
    """
    explicit = _purpose_ctx.get()
    if explicit:
        return explicit
    return PURPOSE_READONLY if has_distinct_readonly(cloud) else PURPOSE_PROVISION


def warm_purposes_for(cloud: str) -> tuple:
    """The purposes the startup warmer and the periodic pass may pre-mint.

    Deliberately NOT :func:`purposes_for`. Pre-minting ``provision`` would leave a
    credential carrying `iam:PassRole` and `iam:CreateRole` in the row at all times,
    which is the exact thing the split removes — warming it would defeat the feature
    while looking like an optimisation.

    So once a second secret exists, only the everyday lease is warmed; ``provision`` is
    minted when a job starts and then allowed to expire. A job pays one HTTP round trip
    at its start, which against a multi-minute deploy is nothing.

    Note what this does NOT do: it does not revoke ``provision`` when the job ends. AWS
    refuses early revocation, so the credential lives to its TTL regardless; dropping the
    row early would only hide it from us, at the cost of a fresh billable issuance per
    job rather than per TTL window. Eager release is available as further hardening if
    the row at rest matters more than the issuance count.
    """
    configured = purposes_for(cloud)
    if PURPOSE_READONLY in configured:
        return (PURPOSE_READONLY,)
    return configured


def _effective_purpose(cloud: str, purpose: str) -> str:
    """Collapse ``readonly`` onto ``provision`` when there is no distinct secret for it.

    Applied before any row lookup, so the collapsed pair share one row rather than
    silently duplicating one.
    """
    if purpose == PURPOSE_READONLY and not has_distinct_readonly(cloud):
        return PURPOSE_PROVISION
    return purpose


def folder_for(cloud: str) -> str:
    return _cfg(f"wlc_{cloud}_folder")


def source_for(cloud: str) -> str:
    """Which credential posture `cloud` is actually on: dynamic | static | unconfigured.

    Surfaced in the UI because "did the flip take effect?" is otherwise unanswerable, and
    because a broken lease and a deployment legitimately on the static tier are
    indistinguishable from the outside.
    """
    if dynamic_enabled(cloud):
        return "dynamic"
    if cloud == "aws":
        static = _cfg("aws_access_key_id") and _cfg("aws_secret_access_key")
    elif cloud == "azure":
        static = _cfg("azure_client_id") and _cfg("azure_client_secret")
    else:
        static = _cfg(f"{cloud}_access_key_id")
    return "static" if static else "unconfigured"


# ── Row helpers ───────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _owner() -> str:
    return f"{socket.gethostname()[:40]}:{os.getpid()}"


def _row(db, cloud: str, purpose: str):
    from ..database import WorkloadCredentialLease
    return db.query(WorkloadCredentialLease).filter(
        WorkloadCredentialLease.cloud == cloud,
        WorkloadCredentialLease.purpose == purpose).first()


def _snap(row) -> Optional[dict]:
    """A detached copy of the decision-relevant columns.

    ``SessionLocal`` has ``expire_on_commit=True``, so touching an ORM attribute after the
    commit raises ``DetachedInstanceError``. Everything below reads this instead.
    """
    if row is None:
        return None
    return {"cloud": row.cloud, "purpose": row.purpose, "payload": row.payload,
            "lease_id": row.lease_id, "issued_at": row.issued_at,
            "expires_at": row.expires_at, "last_error": row.last_error,
            "last_attempt_at": row.last_attempt_at,
            "consecutive_failures": row.consecutive_failures or 0,
            "cooldown_until": row.cooldown_until,
            "claim_until": row.claim_until, "claim_owner": row.claim_owner}


def _ensure_row(db, cloud: str, purpose: str):
    from ..database import WorkloadCredentialLease
    row = _row(db, cloud, purpose)
    if row is None:
        row = WorkloadCredentialLease(cloud=cloud, purpose=purpose,
                                      consecutive_failures=0)
        db.add(row)
        db.flush()
    return row


def _usable(snap, now: datetime) -> bool:
    """Whether this row holds a credential that will still work for a moment longer."""
    if not snap or not snap["payload"] or not snap["expires_at"]:
        return False
    return now < snap["expires_at"] - timedelta(seconds=_EXPIRY_SAFETY_SECONDS)


def _values_of(snap) -> Optional[dict]:
    """Decrypt and parse a row's payload; None when it is absent or unreadable.

    An unreadable payload (a rotated JWT root key, most likely) is treated as absent
    rather than fatal — the next refresh mints a replacement, which is exactly the
    recovery a credential store can offer and a config store cannot.
    """
    if not snap or not snap["payload"]:
        return None
    from . import config_service
    try:
        return json.loads(config_service.decrypt_value(snap["payload"]))
    except Exception:  # noqa: BLE001
        logger.warning("WC lease %s/%s payload is unreadable; treating as absent",
                       snap["cloud"], snap["purpose"])
        return None


def _cooldown_seconds(failures: int) -> int:
    return min(_COOLDOWN_MAX_SECONDS, _FAIL_BASE_SECONDS * max(1, 2 ** (failures - 1)))


def _try_lock(db, cloud: str) -> bool:
    """Try the per-cloud claim lock. Never blocks.

    Transaction-scoped, released by the commit at the end of :func:`_claim` — which happens
    before any network call. SQLite has no advisory locks and serializes writers anyway, so
    single-process dev installs fall back to the claim-expiry check alone.
    """
    from ..database import _is_sqlite
    if _is_sqlite:
        return True
    return bool(db.execute(text("SELECT pg_try_advisory_xact_lock(:c, :k)"),
                           {"c": _LOCK_ID, "k": _CLOUD_LOCK_KEYS.get(cloud, 9)}).scalar())


# ── Claim / record ────────────────────────────────────────────────────────────

def _claim(cloud: str, purpose: str, *, force: bool) -> tuple:
    """Decide whether this process may mint, and stake the claim if so.

    Returns ``(claimed, snap)``. One short transaction that ends before any provider call.
    """
    from ..database import SessionLocal
    now = _utcnow()
    db = SessionLocal()
    try:
        if not _try_lock(db, cloud):
            return False, _snap(_row(db, cloud, purpose))
        row = _ensure_row(db, cloud, purpose)
        snap = _snap(row)

        if _usable(snap, now) and not force:
            return False, snap
        if snap["cooldown_until"] and now < snap["cooldown_until"] and not force:
            return False, snap
        if snap["claim_until"] and now < snap["claim_until"]:
            # Someone else is minting and has not expired yet.
            return False, snap

        row.claim_until = now + timedelta(seconds=_CLAIM_SECONDS)
        row.claim_owner = _owner()
        row.updated_at = now
        db.commit()
        return True, snap
    finally:
        db.close()


def _record_success(cloud: str, purpose: str, result: dict) -> dict:
    """Store a freshly minted credential and clear the failure state.

    Returns the stored snapshot. The previous lease id is returned to the caller so it can
    be released *after* the new one is committed — releasing first would leave a window
    with no working credential.
    """
    from ..database import SessionLocal
    from . import config_service
    now = _utcnow()
    db = SessionLocal()
    try:
        row = _ensure_row(db, cloud, purpose)
        previous = row.lease_id
        row.payload = config_service.encrypt_value(json.dumps(result["values"]))
        row.lease_id = result["lease_id"] or None
        row.issued_at = now
        row.expires_at = result["expires_at"]
        row.last_error = None
        row.consecutive_failures = 0
        row.cooldown_until = None
        row.claim_until = None
        row.claim_owner = None
        row.updated_at = now
        db.commit()
        return {"previous_lease_id": previous, "expires_at": result["expires_at"]}
    finally:
        db.close()


def _record_failure(cloud: str, purpose: str, error: str) -> None:
    """Record a failed generate. Never touches ``payload`` or ``expires_at``."""
    from ..database import SessionLocal
    now = _utcnow()
    db = SessionLocal()
    try:
        row = _ensure_row(db, cloud, purpose)
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
        row.last_error = (error or "")[:2000]
        row.last_attempt_at = now
        row.cooldown_until = now + timedelta(
            seconds=_cooldown_seconds(row.consecutive_failures))
        row.claim_until = None
        row.claim_owner = None
        row.updated_at = now
        db.commit()
    finally:
        db.close()


# ── Memo ──────────────────────────────────────────────────────────────────────

def _memo_get(cloud: str, purpose: str, now: datetime) -> Optional[dict]:
    with _memo_lock:
        hit = _memo.get((cloud, purpose))
    if not hit:
        return None
    values, expires_at, read_at = hit
    if time.monotonic() - read_at > _MEMO_TTL_SECONDS:
        return None
    if not expires_at or now >= expires_at - timedelta(seconds=_EXPIRY_SAFETY_SECONDS):
        return None
    return values


def _memo_put(cloud: str, purpose: str, values: dict, expires_at) -> None:
    with _memo_lock:
        _memo[(cloud, purpose)] = (values, expires_at, time.monotonic())


def invalidate(cloud: str = "", purpose: str = "") -> None:
    """Drop memoised credentials. Call after a config change that could reroute a cloud.

    Process-local by nature — a sibling worker keeps its memo for up to the TTL. That is
    acceptable precisely because the memo is bounded; the row is the authority.
    """
    with _memo_lock:
        for key in [k for k in _memo
                    if (not cloud or k[0] == cloud) and (not purpose or k[1] == purpose)]:
            _memo.pop(key, None)


# ── Public API ────────────────────────────────────────────────────────────────

def credentials(cloud: str, purpose: str = "") -> Optional[dict]:
    """The credential values for `cloud`, or None when it is not on the dynamic tier.

    None means "use the static credential" and is the normal answer for most deployments.
    :class:`LeaseUnavailable` means "this cloud *is* on the dynamic tier and I could not
    produce a credential" — a genuinely different condition, and the caller must not
    conflate them by falling back.

    Synchronous, and on the hot path for every boto3 client, so the common case is a
    process-local memo hit with no database access at all.
    """
    if not dynamic_enabled(cloud):
        return None

    purpose = _effective_purpose(cloud, purpose or default_purpose(cloud))
    now = _utcnow()
    hit = _memo_get(cloud, purpose, now)
    if hit is not None:
        return hit

    name = secret_name_for(cloud, purpose)
    if not name:
        raise LeaseUnavailable(
            f"{cloud} is set to use Workload Credentials but no dynamic secret name is "
            f"configured (wlc_{cloud}_secret_name)")

    values = _serve_or_mint(cloud, purpose, name, force=False)
    return values


def refresh(cloud: str, purpose: str = PURPOSE_PROVISION, *, force: bool = False) -> bool:
    """Proactively mint if the lease is due. Returns True when a new one was issued.

    Driven from the startup warmer and the worker's periodic sweep, which is what keeps the
    synchronous mint inside :func:`credentials` rare.
    """
    if not dynamic_enabled(cloud):
        return False
    purpose = _effective_purpose(cloud, purpose)
    name = secret_name_for(cloud, purpose)
    if not name:
        return False

    from ..database import SessionLocal
    now = _utcnow()
    db = SessionLocal()
    try:
        snap = _snap(_row(db, cloud, purpose))
    finally:
        db.close()

    if not force and snap and _usable(snap, now):
        from . import workload_credentials_service as wlc
        margin = int(_cfg("wlc_refresh_margin_pct", "50") or 50)
        if not wlc.refresh_due(snap["expires_at"], snap["issued_at"], margin, now=now):
            return False

    try:
        _serve_or_mint(cloud, purpose, name, force=True)
        return True
    except LeaseUnavailable as exc:
        logger.warning("WC lease refresh for %s/%s failed: %s", cloud, purpose, exc)
        return False


def _serve_or_mint(cloud: str, purpose: str, name: str, *, force: bool) -> dict:
    """The claim / mint / record cycle. Raises LeaseUnavailable when it cannot produce one."""
    from . import workload_credentials_service as wlc

    claimed, snap = _claim(cloud, purpose, force=force)

    if not claimed:
        now = _utcnow()
        values = _values_of(snap) if _usable(snap, now) else None
        if values is not None:
            _memo_put(cloud, purpose, values, snap["expires_at"])
            return values
        # Another process holds the claim: wait briefly rather than minting a second
        # credential for the same purpose, which would bill twice.
        if snap and snap["claim_until"] and now < snap["claim_until"]:
            values, expires_at = _wait_for_winner(cloud, purpose)
            if values is not None:
                _memo_put(cloud, purpose, values, expires_at)
                return values
        raise LeaseUnavailable(_unavailable_reason(cloud, purpose, snap))

    folder = folder_for(cloud)
    try:
        result = wlc.generate(name, folder=folder)
    except Exception as exc:  # noqa: BLE001 — every failure gets recorded and re-raised
        _record_failure(cloud, purpose, str(exc))
        raise LeaseUnavailable(
            f"could not mint a {cloud} credential from dynamic secret "
            f"{folder + '/' if folder else ''}{name}: {exc}") from exc

    stored = _record_success(cloud, purpose, result)
    _memo_put(cloud, purpose, result["values"], result["expires_at"])

    # Release the credential we just replaced, now that the new one is committed. AWS
    # refuses this and that is expected; Azure accepts it, and skipping it would let
    # passwords accumulate on the target app registration until it hits its cap.
    if stored["previous_lease_id"]:
        try:
            wlc.revoke_lease(stored["previous_lease_id"])
        except Exception as exc:  # noqa: BLE001 — cleanup, not correctness
            logger.warning("WC: could not release the previous %s/%s lease: %s",
                           cloud, purpose, exc)

    return result["values"]


def _wait_for_winner(cloud: str, purpose: str) -> tuple:
    """Poll briefly for the process holding the claim to publish its credential."""
    from ..database import SessionLocal
    deadline = time.monotonic() + _WAIT_TOTAL_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_WAIT_POLL_SECONDS)
        db = SessionLocal()
        try:
            snap = _snap(_row(db, cloud, purpose))
        finally:
            db.close()
        if _usable(snap, _utcnow()):
            values = _values_of(snap)
            if values is not None:
                return values, snap["expires_at"]
        if snap and not snap["claim_until"]:
            break  # the winner finished (successfully or not); stop waiting
    return None, None


def _unavailable_reason(cloud: str, purpose: str, snap) -> str:
    if snap and snap["last_error"]:
        return (f"no usable {cloud} credential for {purpose}; last attempt failed: "
                f"{snap['last_error']}")
    return f"no usable {cloud} credential for {purpose} and none could be minted"


def aws_subprocess_env(purpose: str = ""):
    """``AWS_*`` env vars for a subprocess, or None when AWS is not on the dynamic tier.

    One helper for terraform (both the provider and the S3 state backend) and Packer,
    rather than the mapping written out three times. The session token is the reason: it
    is the field every one of those sites currently lacks, and a copy that forgets it
    fails with ``InvalidClientTokenId`` in a way that looks like a bad key.
    """
    values = credentials("aws", purpose)
    if not values:
        return None
    return {
        "AWS_ACCESS_KEY_ID":     values["access_key_id"],
        "AWS_SECRET_ACCESS_KEY": values["secret_access_key"],
        "AWS_SESSION_TOKEN":     values["session_token"],
    }


def status(cloud: str, purpose: str = PURPOSE_PROVISION) -> dict:
    """Non-secret lease state for the UI and health surfaces. Never returns the payload."""
    from ..database import SessionLocal
    now = _utcnow()
    db = SessionLocal()
    try:
        snap = _snap(_row(db, cloud, purpose))
    finally:
        db.close()
    return {
        "cloud": cloud,
        "purpose": purpose,
        "source": source_for(cloud),
        "has_credential": bool(snap and snap["payload"]),
        "usable": _usable(snap, now),
        "expires_at": snap["expires_at"].isoformat() + "Z" if snap and snap["expires_at"] else None,
        "issued_at": snap["issued_at"].isoformat() + "Z" if snap and snap["issued_at"] else None,
        "last_error": snap["last_error"] if snap else None,
        "consecutive_failures": snap["consecutive_failures"] if snap else 0,
        "cooldown_until": snap["cooldown_until"].isoformat() + "Z" if snap and snap["cooldown_until"] else None,
        "secret_name": secret_name_for(cloud, purpose),
    }
