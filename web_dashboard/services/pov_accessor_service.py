"""POV accessors: a prospect's ephemeral login, bound to one POV and reaped with it.

Every other artifact a POV publishes is a door into the LAB — a share link onto its
desktops, a jump item onto one of its VMs. An accessor is a door into **this dashboard**,
and that difference decides everything in this module.

**The account is a real ``users`` row.** Login, the sign-in throttle, MFA and the audit
trail are the ones already here rather than a second implementation of each, and a
principal that can log in belongs in the table everything else looks in. What makes it an
accessor is one column, ``User.accessor_env_id``, and that column is a **deny marker, not a
permission** — read the comment on it before changing anything here. The short version:
``require_permission`` treats an empty permission map as unrestricted, so "an accessor with
no permissions" is an administrator. The guard is a path allowlist in
``api/auth.get_current_user``, the one dependency every authenticated route resolves
through.

**Revoking deletes the row.** Deactivating would leave a username in ``users`` for every
actor lookup in the codebase to remember to skip, and the one that forgets is an
escalation rather than a cosmetic bug.

**An accessor cannot outlive its POV.** Entitle owns expiry on its side and calls
``delete_actor`` when a grant ends, but an integration that is removed, misconfigured or
simply never calls back would leave a working credential behind forever. So this keeps its
own clock, clamped to the environment's, on the same three-step rule ``pov_share`` uses for
the share link and for the same reason: the failure mode of POV access is that it outlives
everyone's attention.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..database import PovAccessor, PovEnvironment, User, get_password_hash
from . import config_service, job_service, pov_share, pov_use_cases

logger = logging.getLogger(__name__)


class AccessorError(Exception):
    """A refusal carrying the remedy, not just the cause."""


# Every accessor username starts with this, and the prefix is load-bearing rather than
# cosmetic: `delete_actor` refuses any name without it, so an Entitle integration can never
# be talked into removing an operator's account. Same guard, and the same reasoning, as
# `_JIT_PREFIX` in functions/fnworkloads/db_grant.py.
USERNAME_PREFIX = "povguest_"

# users.username is String(100). A name is prefix + env slug + randomness, and the slug is
# the part that varies, so it is what gets trimmed.
_USERNAME_MAX = 100
_SLUG_MAX = 40
_RAND_CHARS = 8
_SLUG_RE = re.compile(r"[^a-z0-9-]+")

SOURCE_MANUAL = "manual"
SOURCE_ENTITLE = "entitle"
VALID_SOURCES = (SOURCE_MANUAL, SOURCE_ENTITLE)

# How long an accessor lasts when nothing else says. Fourteen days is an evaluation, the
# same figure and the same argument as pov_share.DEFAULT_DAYS.
DEFAULT_DAYS = 14
MAX_DAYS = 90


def _now() -> datetime:
    return datetime.utcnow()


def _ttl_days() -> int:
    """The configured default, clamped into range. A bad row must not mint a longer one."""
    try:
        raw = int(config_service.get("pov_accessor_ttl_days") or DEFAULT_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(1, min(raw, MAX_DAYS))


# ── identity ─────────────────────────────────────────────────────────────────

def _slug(value: str) -> str:
    out = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    return out[:_SLUG_MAX] or "pov"


def _username_for(env: PovEnvironment) -> str:
    """A name that says what it is and where it came from, and cannot collide.

    The environment slug is in it so an operator reading `users` or an audit line can tell
    which POV an accessor belongs to without a join — the same reason the jump items carry
    the POV's name.
    """
    rand = "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789")
                   for _ in range(_RAND_CHARS))
    name = f"{USERNAME_PREFIX}{_slug(env.name or env.id)}_{rand}"
    return name[:_USERNAME_MAX]


def is_accessor_username(username: str) -> bool:
    """Whether a name is one this module minted.

    The only thing standing between ``delete_actor`` and an operator's account, so it is
    checked on every destructive path that takes a name from a request rather than from a
    row.
    """
    return str(username or "").startswith(USERNAME_PREFIX)


def is_accessor(user) -> bool:
    """Whether this user is a POV accessor. One spelling, so no caller invents another."""
    return bool(getattr(user, "accessor_env_id", None))


# ── expiry ───────────────────────────────────────────────────────────────────

def _expiry_for(env: PovEnvironment, days: int | None) -> datetime:
    """When this accessor stops working.

    Order matters, and it is ``pov_share._expiry_for``'s: an explicit request wins, then
    the POV's own auto-delete time, then the default. The middle step is the one that
    matters — a login that outlives the environment it can reach is a credential nobody
    associates with anything any more.
    """
    if days is not None:
        if days < 1 or days > MAX_DAYS:
            raise AccessorError(
                f"an accessor must last between 1 and {MAX_DAYS} days; asked for {days}")
        wanted = _now() + timedelta(days=int(days))
    else:
        wanted = _now() + timedelta(days=_ttl_days())
    if env.expires_at and env.expires_at > _now():
        # Clamped rather than refused: asking for 30 days on a POV with 5 left is a normal
        # thing to do, and the answer is 5.
        return min(wanted, env.expires_at)
    return wanted


def is_expired(row: PovAccessor) -> bool:
    return bool(row.expires_at and row.expires_at <= _now())


# ── read ─────────────────────────────────────────────────────────────────────

def describe_one(row: PovAccessor) -> dict:
    """One accessor, as the Access tab reads it. **Never a password.**

    The minted password is returned exactly once, by the call that created it. There is no
    endpoint that re-reads one, and there is nothing stored that could serve it: unlike the
    share link — whose password an SE has to be able to read back to a customer a day
    later — an accessor that has lost its password is replaced, which is one click and
    leaves a trail.
    """
    return {
        "id": row.id,
        "username": row.username,
        "email": row.email or "",
        "full_name": row.full_name or "",
        "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "created_by": row.created_by or "",
        "expires_at": row.expires_at.isoformat() if row.expires_at else "",
        "expired": is_expired(row),
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else "",
        "active": not row.revoked_at and not is_expired(row),
    }


def _live_rows(db: Session, env_id: str) -> list:
    return (db.query(PovAccessor)
              .filter(PovAccessor.environment_id == env_id,
                      PovAccessor.revoked_at.is_(None))
              .order_by(PovAccessor.created_at.desc()).all())


def describe(db: Session, env: PovEnvironment) -> dict:
    """This POV's accessors, live ones only.

    Revoked rows are not listed. They are kept as the record that somebody had access, and
    the audit trail is where that question gets asked — a list that grew forever with dead
    logins would make the live ones harder to see, which is the opposite of the point.
    """
    rows = _live_rows(db, env.id)
    return {
        "accessors": [describe_one(r) for r in rows],
        "accessor_count": len(rows),
    }


def get(db: Session, accessor_id: str) -> PovAccessor | None:
    return db.query(PovAccessor).filter(PovAccessor.id == accessor_id).first()


def by_username(db: Session, username: str) -> PovAccessor | None:
    return (db.query(PovAccessor)
              .filter(PovAccessor.username == username,
                      PovAccessor.revoked_at.is_(None)).first())


def environment_of(db: Session, user) -> PovEnvironment | None:
    """The POV this accessor may reach, **resolved from the session, never from a path.**

    That is the whole reason ``/api/pov/accessor/self`` takes no environment id: there is no
    parameter to tamper with, so there is no ownership check to forget.
    """
    env_id = getattr(user, "accessor_env_id", None)
    if not env_id:
        return None
    return db.query(PovEnvironment).filter(PovEnvironment.id == env_id).first()


# ── mint ─────────────────────────────────────────────────────────────────────

def mint(db: Session, env: PovEnvironment, *, email: str = "", full_name: str = "",
         source: str = SOURCE_MANUAL, by: str = "", days: int | None = None,
         entitle_request_id: str = "", entitle_actor_id: str = "") -> tuple:
    """Create an accessor for this POV. Returns ``(PovAccessor, password)``.

    The password is returned and never stored in readable form — this function is the only
    moment it exists outside a hash.

    Refused for a POV that is being torn down or already gone: minting a login into an
    environment whose destroy job is running produces a credential whose whole purpose
    expires within the minute, and the row it hangs off is about to stop being true.
    """
    if source not in VALID_SOURCES:
        raise AccessorError(
            f"source must be one of {', '.join(VALID_SOURCES)}; got {source!r}")
    if env.status in ("destroying", "destroyed"):
        raise AccessorError(
            f"this POV is {env.status}; an accessor for it would be a login into an "
            f"environment that is going away")

    expires = _expiry_for(env, days)
    username = _username_for(env)
    password = pov_share.generate_password()

    user = User(
        username=username,
        hashed_password=get_password_hash(password),
        full_name=(full_name or "").strip()[:200] or None,
        email=(email or "").strip()[:200] or None,
        is_active=True,
        is_admin=False,
        auth_provider="local",
        # Left NULL on purpose, and it is NOT what confines this account: an empty
        # permission map reads as unrestricted (see the User column comment). The
        # allowlist in api/auth.get_current_user is the control. Writing a permission
        # map here would suggest otherwise to the next reader.
        permissions=None,
        accessor_env_id=env.id,
    )
    db.add(user)
    db.flush()          # for user.id, before the binding references it

    row = PovAccessor(
        environment_id=env.id, user_id=user.id, username=username,
        email=user.email, full_name=user.full_name,
        source=source, entitle_request_id=entitle_request_id or None,
        entitle_actor_id=entitle_actor_id or None,
        expires_at=expires, created_by=(by or "")[:100] or None,
    )
    db.add(row)
    db.commit()

    # Audited for the same reason revealing a share password is: this hands back a live
    # credential, and "who could log in to this POV" has to be answerable afterwards.
    job_service.log_audit(
        db, by or source, "pov_accessor_minted", target_vm=env.name,
        details={"environment_id": env.id, "accessor_id": row.id,
                 "username": username, "source": source,
                 "expires_at": expires.isoformat()})
    logger.info("POV %s: minted accessor %s (%s, expires %s)",
                env.id, username, source, expires.isoformat())
    return row, password


# ── revoke ───────────────────────────────────────────────────────────────────

def revoke(db: Session, row: PovAccessor, *, reason: str = "", by: str = "") -> None:
    """Delete the login and stamp the binding. Idempotent.

    The user row goes first. A binding marked revoked while the account still works is the
    one ordering that leaves a live credential nobody can find from here.
    """
    if row.user_id:
        db.query(User).filter(User.id == row.user_id).delete()
    else:
        # Minted before user_id was recorded, or the row was removed by hand. The username
        # is the other handle, and it carries the prefix guard.
        if is_accessor_username(row.username):
            db.query(User).filter(User.username == row.username).delete()

    if not row.revoked_at:
        row.revoked_at = _now()
        row.revoke_reason = (reason or "")[:255] or None
    db.commit()

    job_service.log_audit(
        db, by or "system", "pov_accessor_revoked", target_vm=row.username,
        details={"environment_id": row.environment_id, "accessor_id": row.id,
                 "username": row.username, "reason": reason or ""})
    logger.info("POV %s: revoked accessor %s (%s)",
                row.environment_id, row.username, reason or "no reason given")


# ── teardown and sweep ───────────────────────────────────────────────────────

def teardown(db: Session, env: PovEnvironment) -> str:
    """Remove every accessor on the destroy path. Returns a job-log line, or "".

    Never raises. Called FIRST in ``run_env_destroy``, ahead of the share link: that step
    holds its position because it is the only artifact somebody outside the account can be
    holding, and this is that too — a credential into this dashboard rather than a door
    into a lab, so the window where it still works is the one worth making shortest.
    """
    rows = _live_rows(db, env.id)
    if not rows:
        return ""
    failed = []
    for row in rows:
        try:
            revoke(db, row, reason="the POV was destroyed", by="system")
        except Exception as exc:  # noqa: BLE001 — one bad row must not strand the rest
            logger.warning("POV %s: could not revoke accessor %s", env.id, row.username,
                           exc_info=True)
            failed.append(f"{row.username} ({exc})")
    if failed:
        return (f"WARNING: {len(failed)} of {len(rows)} accessor logins could not be "
                f"removed: {', '.join(failed)}. Disable them on the Users page.")
    return f"removed {len(rows)} accessor login(s)"


def sweep(db: Session) -> int:
    """Revoke accessors that have expired, or whose POV is already gone. Returns the count.

    The backstop for the two ways teardown does not run: Entitle never calling
    ``delete_actor``, and a POV row that reached ``destroyed`` without this module being
    asked. Both leave a working login, which is the one outcome worth a second sweep.

    Ridden on the POV reconcile pass rather than the expiry sweep, and deliberately outside
    the platform-specific part of it: a fresh POV instance starts with the auto-delete timer
    off (docs/pov-instance.md), and whether an accessor should still work has nothing to do
    with whether the lab platform is reachable.
    """
    now = _now()
    dead_envs = {e.id for e in db.query(PovEnvironment)
                                 .filter(PovEnvironment.status == "destroyed").all()}
    rows = (db.query(PovAccessor)
              .filter(PovAccessor.revoked_at.is_(None)).all())
    reaped = 0
    for row in rows:
        if row.expires_at and row.expires_at <= now:
            reason = "expired"
        elif row.environment_id in dead_envs:
            reason = "the POV was destroyed"
        else:
            continue
        try:
            revoke(db, row, reason=reason, by="system")
            reaped += 1
        except Exception:  # noqa: BLE001 — a sweep never stops on one row
            logger.warning("accessor sweep: could not revoke %s", row.username,
                           exc_info=True)
    if reaped:
        logger.info("accessor sweep: revoked %d login(s)", reaped)
    return reaped


# ── the accessor's own view ──────────────────────────────────────────────────

def self_view(db: Session, user) -> dict:
    """What ``GET /api/pov/accessor/self`` serves.

    A projection written for this audience, **not** ``api/pov._serialize`` with fields
    removed. That row carries tenant ids, terraform state, broker ids and a share id —
    every one of them meaningless to a prospect and several of them things they should
    never see. Building it by subtraction is how one of them comes back on the next change
    to the serializer.

    The use-case catalog is the exception, and it is deliberate: the accessor sees exactly
    what the SE sees, through ``pov_use_cases.describe`` unchanged. That is the point of
    the whole feature.
    """
    env = environment_of(db, user)
    if env is None:
        raise AccessorError(
            "this login is not attached to a POV environment any more — it has probably "
            "been reaped. Ask your contact for a new one.")
    row = by_username(db, getattr(user, "username", "") or "")
    return {
        "environment": {
            "id": env.id,
            "name": env.name,
            "status": env.status,
            "runstate": env.runstate or "",
        },
        "accessor": describe_one(row) if row else {},
        "use_cases": pov_use_cases.describe(db, env),
    }
