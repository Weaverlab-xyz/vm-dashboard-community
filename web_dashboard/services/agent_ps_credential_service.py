"""A hypervisor credential checked out of Password Safe for one agent job, then released.

This is the mode where **neither host holds a reusable target credential**. The agent's
connections.yaml carries no password (see ``dashboard_secret`` in ``runners/agent``); the
dashboard row carries only ``ps_account://<id>``; and the password itself is requested per
job, sealed to the agent (``agent_sealing``), checked back in when the job ends and — by
default — rotated on release, so the value the agent held is dead the moment it is done
with it.

Why this module exists rather than a branch in ``ps_api_service``
----------------------------------------------------------------
Two things there cannot be reused directly, and one must not be touched:

* ``checkout_credential`` **discards the request id** and guards its result with
  ``_looks_like_sa_token`` — a JWT shape check that would reject every hypervisor
  password. Its docstring says nothing else should call it, and that is correct: it is the
  k8s ServiceAccount path.
* Rotation there is *forbidden*, by a static test asserting ``rotateoncheckin`` is named
  nowhere in that module. The reason is specific and good: a credential change on either
  member of a synced pair re-rotates both, so rotate-on-release would re-rotate a live
  cluster token every time the tunnel read it. That reason does not apply to a hypervisor
  service account, but the test rightly does not know that — so rotation lives here.

What *is* reused is the wire protocol: ``ps_api_service._checkout`` already returns
``(request_id, credential)``, already sends the ``SystemID`` the tenant authorises the pair
on, and already passes ``ConflictOption=reuse``. The repo carries three copies of that
protocol already and says so; a fourth would be worse than reaching across for a private
sibling helper, which is what the imports below deliberately do.

Releasing is the hard half
--------------------------
A checkout that is never checked in holds the account's concurrent-request slot until its
duration expires, and — with rotation on — a *premature* check-in is worse than a late
one: it would change the password underneath a job still using it. Two properties carry
the design:

* **Ref-counted, never per-job.** ``ConflictOption=reuse`` means two jobs running against
  the same account receive *the same request id*. Releasing on the first completion would
  rotate the credential the second is still authenticating with, mid-flight. So a release
  happens only when no other live job holds that id.
* **The sweeper is the authority, not the completion hook.** The hook is the fast path;
  it does not run at all when an agent's container is killed, because
  ``job_service.reconcile_stale_jobs`` writes ``failed`` inline and never reaches
  ``agent_service.complete_job``. Hooking that reaper directly is not possible either —
  it is sync and called from an async lifespan, while every Password Safe call is async.
  So the reaper is left alone and :func:`sweep` reconciles instead, in the shape
  ``cloud_identity_sweeper_service`` already established for a JIT grant with a TTL.

Worst case for a killed agent is therefore ``STALE_AFTER_MINUTES`` (10) plus one
``RECONCILE_INTERVAL`` (60s) plus one sweep interval — about twelve minutes, after which
the request is checked in and the password rotated. A request whose duration expires first
is closed by Password Safe itself, which is safe; the sweep still clears our record of it.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..database import HypervisorConnection, Job
from . import config_service, hypervisor_connection_service as hcs, ps_api_service

logger = logging.getLogger(__name__)

# Where the open request is recorded. `Job.extra_data` rather than a new table: the
# sweeper below and `reconcile_stale_jobs` both already iterate jobs in Python, so no
# indexed column is needed — and `extra_data` has no portable cross-database filter
# anyway, only a LIKE scan, which is exactly why iterating is the right shape here.
META_REQUEST_ID = "ps_request_id"
META_ACCOUNT_ID = "ps_account_id"

# A job in one of these states will never use its credential again.
TERMINAL_STATUSES = ("completed", "failed", "cancelled")

# Only this job type ever holds one, which bounds every scan below.
_JOB_TYPE = "agent_hypervisor"

_SOURCE_LABEL = "ps_account"


class PSCredentialError(Exception):
    """A Password Safe checkout that could not be completed, with Password Safe's reason."""


def _duration_min() -> int:
    try:
        raw = config_service.get("agent_ps_checkout_duration_min")
        if raw:
            return max(1, int(raw))
    except Exception:  # noqa: BLE001
        pass
    from ..config import settings
    return max(1, int(getattr(settings, "agent_ps_checkout_duration_min", 45) or 45))


def _rotate_on_release() -> bool:
    from ..config import settings
    return config_service.get_bool(
        "agent_ps_rotate_on_release",
        bool(getattr(settings, "agent_ps_rotate_on_release", True)))


def _set_meta(db: Session, job: Job, **pairs) -> None:
    meta = dict(job.metadata_dict or {})
    for key, value in pairs.items():
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
    job.metadata_dict = meta
    db.commit()


# ── acquire ───────────────────────────────────────────────────────────────────

async def acquire(db: Session, job: Job, row: HypervisorConnection) -> tuple:
    """Check a credential out for ``job`` and return ``(secret, source_label)``.

    Idempotent per job by way of ``ConflictOption=reuse``: a retried fetch — which the
    agent does legitimately, since a Workstation job reads the credential on every HTTP
    call — gets the same request back rather than opening a second one.

    The request id is recorded on the job **before** the credential is returned. If the
    order were reversed, a crash between the two would leave an open request with nothing
    pointing at it, invisible to :func:`sweep` and open until its duration expired.
    """
    account_id = hcs.ps_account_id(row)
    if not account_id.isdigit():
        raise PSCredentialError(
            f"connection {row.name!r} has a malformed Password Safe reference "
            f"({row.secret_ref!r}) — it must look like 'ps_account://12345', where the "
            f"number is the managed account id.")
    if not ps_api_service.configured():
        raise PSCredentialError(
            f"connection {row.name!r} takes its credential from Password Safe, but this "
            f"dashboard has no Password Safe API client configured. Set the BeyondTrust "
            f"API URL, client id and client secret under Settings, or point the "
            f"connection at a stored password instead.")

    client = ps_api_service._client()  # noqa: SLF001 — see the module docstring
    try:
        await ps_api_service._sign_in(client)  # noqa: SLF001
        request_id, credential = await ps_api_service._checkout(  # noqa: SLF001
            client, int(account_id), duration_min=_duration_min(),
            reason=f"vm-dashboard agent job {job.id}")
    except ps_api_service.PSApiError as exc:
        raise PSCredentialError(
            f"connection {row.name!r}: {exc}") from exc
    finally:
        await ps_api_service._sign_out(client)  # noqa: SLF001
        await client.aclose()

    _set_meta(db, job, **{META_REQUEST_ID: int(request_id),
                          META_ACCOUNT_ID: str(account_id)})

    if not credential:
        # Released as an empty string rather than refused. Sending that on to a hypervisor
        # would read as "wrong username or password" and, retried on the sync schedule,
        # risks locking the service account out — so it is refused here, and the request
        # is released rather than left open for its whole duration.
        await release_for_job(db, job, force=True)
        raise PSCredentialError(
            f"connection {row.name!r}: Password Safe released an empty credential for "
            f"managed account {account_id}. Check that the account has a stored password "
            f"and that it is API-enabled.")
    return credential, _SOURCE_LABEL


# ── release ───────────────────────────────────────────────────────────────────

def _others_holding(db: Session, job: Job, request_id: int) -> int:
    """How many *other* live jobs hold this same Password Safe request.

    The whole point of the ref count. ``ConflictOption=reuse`` hands concurrent jobs on one
    account a single request id, so releasing on the first completion — and, with rotation
    on, changing the password — would break every sibling still authenticating with it.
    """
    n = 0
    for other in db.query(Job).filter(
            Job.job_type == _JOB_TYPE,
            Job.id != job.id,
            Job.status.notin_(TERMINAL_STATUSES)).all():
        try:
            if int((other.metadata_dict or {}).get(META_REQUEST_ID) or 0) == int(request_id):
                n += 1
        except (TypeError, ValueError):
            continue
    return n


async def release_for_job(db: Session, job: Job, *, force: bool = False) -> bool:
    """Check this job's credential back in, and rotate it. Returns whether it released.

    Idempotent, because two callers race in practice: the completion hook in one gunicorn
    worker and :func:`sweep` in another. The record is cleared from the job **first**, so
    the loser of that race finds nothing to do; a double check-in against Password Safe
    would be harmless anyway, but a double *rotation* would needlessly burn a password.

    ``force`` skips only the ref count, for the one caller that knows the credential was
    never usable (an empty release, in :func:`acquire`).
    """
    meta = job.metadata_dict or {}
    try:
        request_id = int(meta.get(META_REQUEST_ID) or 0)
    except (TypeError, ValueError):
        request_id = 0
    if not request_id:
        return False

    if not force and _others_holding(db, job, request_id):
        # Another live job shares this request. Drop *our* claim on it and leave the
        # request open — whichever sibling finishes last releases it.
        _set_meta(db, job, **{META_REQUEST_ID: None, META_ACCOUNT_ID: None})
        return False

    account_id = str(meta.get(META_ACCOUNT_ID) or "")
    _set_meta(db, job, **{META_REQUEST_ID: None, META_ACCOUNT_ID: None})

    rotate = _rotate_on_release()
    client = ps_api_service._client()  # noqa: SLF001
    try:
        await ps_api_service._sign_in(client)  # noqa: SLF001
        await ps_api_service._checkin(  # noqa: SLF001
            client, request_id, reason=f"vm-dashboard agent job {job.id} finished")
        if rotate and account_id.isdigit():
            # Check in FIRST, then rotate. A rotation while the request is still open can
            # leave the request holding a password that no longer works, which reads to the
            # next reader as a wrong credential rather than as a rotation.
            #
            # `change_managed_account_password` rather than the rotate-on-check-in flag:
            # that flag is what ps_api_service's static test forbids, and this reaches the
            # same end state with a call that module already makes.
            await ps_api_service.change_managed_account_password(int(account_id))
    except Exception as exc:  # noqa: BLE001
        # Never fail a finished job over tidying up. The record is already cleared, so the
        # sweeper will not retry this one — but Password Safe closes the request on its own
        # duration regardless, which is the backstop that makes that safe.
        logger.warning("Password Safe release failed for a finished agent job: %s", exc)
        return False
    finally:
        await ps_api_service._sign_out(client)  # noqa: SLF001
        await client.aclose()
    return True


# ── sweeper ───────────────────────────────────────────────────────────────────

def sweep(db: Session, limit: int = 200) -> int:
    """Release every credential still recorded against a job that has finished.

    The authority, not a safety net. A killed agent container never completes its job, so
    ``reconcile_stale_jobs`` fails it inline without ever reaching the completion hook —
    which makes this the only path that releases in the case that matters most.

    **Synchronous, with an ``asyncio.run`` bridge**, exactly like
    ``cloud_identity_sweeper_service.sweep_once``: it does blocking session work *and*
    async Password Safe calls, so the caller hands the whole pass to a thread
    (``await asyncio.to_thread(sweep, db)``) and the bridge lives in here. Calling this
    from a running event loop is a programming error and will raise, which is the correct
    and loud outcome.

    Bounded per pass so one wedged Password Safe tenant cannot turn a sweep into an
    unbounded run of failing round trips.
    """
    import asyncio

    released = 0
    stale = [j for j in db.query(Job).filter(
        Job.job_type == _JOB_TYPE,
        Job.status.in_(TERMINAL_STATUSES)).order_by(
            Job.completed_at.desc()).limit(limit).all()
        if (j.metadata_dict or {}).get(META_REQUEST_ID)]
    for job in stale:
        try:
            if asyncio.run(release_for_job(db, job)):
                released += 1
        except Exception:  # noqa: BLE001 — one bad row must not stop the sweep
            logger.debug("agent PS credential sweep skipped a job", exc_info=True)
    if released:
        logger.info("released %d Password Safe credential(s) held by finished agent jobs",
                    released)
    return released
