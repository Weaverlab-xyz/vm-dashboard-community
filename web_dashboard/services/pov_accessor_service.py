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

from ..database import (PovAccessor, PovEnvironment, PovEnvironmentVM, User,
                        get_password_hash)
from . import config_service, job_service, pov_share, pov_use_cases

logger = logging.getLogger(__name__)

# Who a customer-initiated start is attributed to. Its own actor, like `pov-schedule`
# and `pov-spend-cap`: the /jobs row should say a prospect pressed the button rather
# than name the SE who set the POV up.
WAKE_ACTOR = "pov-accessor"


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
    off (docs/profiles/pov/README.md), and whether an accessor should still work has nothing to do
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

# ── what the accessor is allowed to see of the POV ───────────────────────────

def wired_view(db: Session, env: PovEnvironment) -> list:
    """What was set up on each VM, for the person about to try it.

    A projection built FOR this audience, never ``api/pov._serialize`` with fields taken
    out. Those per-VM rows carry `pra_jump_id`, `ps_managed_system_id`,
    `ps_managed_account_id`, `entitle_integration_id` and their terraform state — ids of
    objects inside a CUSTOMER'S OWN PRA appliance and Password Safe tenant, meaningful only
    to teardown, and a `wiring_error` written for an operator. Subtracting them one by one
    is how one comes back on the next edit to the serializer; naming what goes IN cannot
    fail that way.

    So each VM reports what a prospect actually needs: what it is, where it is on their own
    lab network, and WHICH KINDS of access exist for it — never the identifiers of those
    artifacts.
    """
    rows = (db.query(PovEnvironmentVM)
              .filter(PovEnvironmentVM.environment_id == env.id)
              .order_by(PovEnvironmentVM.name).all())
    return [{
        "name": vm.name or vm.platform_vm_id,
        "os_family": vm.os_family or "",
        # Their own lab's private network, which the share link already puts them on. Not a
        # credential and not an id in anybody's appliance.
        "private_ip": vm.private_ip or "",
        "brokered_session": bool(vm.pra_jump_id),
        "vaulted_credential": bool(vm.ps_managed_account_id or vm.ps_managed_system_id),
        "requestable_access": bool(vm.entitle_integration_id),
    } for vm in rows]


def share_view(db: Session, env: PovEnvironment) -> dict:
    """The lab link, for the person it was published for.

    ``pov_share.describe`` also returns ``share_id`` — the publish set's own id, which
    exists so this dashboard can revoke exactly that one later. It means nothing to a
    customer and it is an id in the account's lab platform, so it is dropped here rather
    than passed through.

    The password is not here either, and for the same reason it is absent from
    ``pov_share.describe``: a secret that ships with a page is a secret in every browser
    cache and every screenshot of it. Revealing it is its own endpoint.
    """
    state = pov_share.describe(db, env)
    return {
        "url": state["share_url"],
        "expires_at": state["share_expires_at"],
        "expired": state["share_expired"],
        "has_password": state["has_share_password"],
    }


def reveal_share_password(db: Session, env: PovEnvironment, user) -> str:
    """The lab link's password, for an accessor. Audited, like the SE's own reveal.

    The customer is who the link was published for, so this is not a widening of who may
    read it — but it IS a second door onto a live credential, and "who has this link's
    password" has to stay answerable after the fact. Same reasoning, and the same audit
    action shape, as ``api/pov.reveal_share_password``.
    """
    password = pov_share.reveal_password(env)
    if not password:
        raise AccessorError(
            "there is no stored password for this link. Ask your contact to re-share the "
            "environment, which produces a new link and a new password.")
    job_service.log_audit(
        db, getattr(user, "username", "") or "accessor",
        "pov_share_password_revealed", target_vm=env.name,
        details={"environment_id": env.id, "by_kind": pov_use_cases.KIND_ACCESSOR})
    return password


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
        "share": share_view(db, env),
        "wired": wired_view(db, env),
        # Whether the prospect may start a suspended environment, and why not when
        # they may not. Served with the page rather than probed — a control that
        # fails when pressed is worse than one that explains itself.
        "wake": wake_view(db, env),
    }


def env_for_writes(db: Session, user) -> PovEnvironment:
    """The POV an accessor may write to, from the session and nowhere else.

    Every accessor write goes through here rather than taking an id, which is what makes
    "could they tick a card on somebody else's POV?" unanswerable rather than merely
    guarded. A refused write is an accessor whose POV is gone, and it says so.
    """
    env = environment_of(db, user)
    if env is None:
        raise AccessorError(
            "this login is not attached to a POV environment any more — it has probably "
            "been reaped. Ask your contact for a new one.")
    return env


def wake_view(db: Session, env: PovEnvironment) -> dict:
    """Whether this POV's accessor may start it, and why not when they may not.

    A suspend schedule without a customer-reachable resume is a broken demo: the POV goes
    to sleep at 19:00 and the prospect trying it at nine the next morning finds a dead
    environment and an SE to email. This is the other half of that feature.

    Computed and served with the page rather than probed, so the button renders in the
    state it will actually behave in. Every refusal below is a sentence the prospect can
    act on — "ask your contact" is the answer to most of them, and saying so is better
    than a control that fails when pressed.
    """
    from . import lab_platforms, pov_env_service, pov_spend

    out = {"can_wake": False, "reason": "", "running": False, "starting": False}
    runstate = (env.runstate or "").lower()
    out["running"] = runstate == "running"

    if out["running"]:
        out["reason"] = "This environment is already running."
        return out
    if pov_env_service.power_job_in_flight(db, env.id):
        # Not an error, and not a refusal to show — the prospect pressed it and something
        # is happening. A second job would be a race with the first over one environment.
        out["starting"] = True
        out["reason"] = "This environment is starting. Give it a few minutes."
        return out

    actionable, why = pov_env_service.may_act_on(env)
    if not actionable:
        out["reason"] = why
        return out
    if not lab_platforms.supports(env.platform, "runstate"):
        out["reason"] = "This environment cannot be started from here."
        return out

    # **A POV over its spend cap is deliberately NOT wakeable.** The cap latches once it
    # has acted, precisely so an operator can decide what to do without the sweep
    # re-suspending underneath them — which means a prospect who woke it would leave it
    # running past its cap indefinitely, and the one control the account owner has would
    # be the one anybody could undo.
    if pov_spend.describe(env).get("over"):
        out["reason"] = ("This environment has reached the spend limit set for it. Ask "
                         "your contact to raise it before starting it again.")
        return out

    out["can_wake"] = True
    return out


def wake(db: Session, env: PovEnvironment) -> dict:
    """Start a POV. Returns the new wake view plus the job id.

    Start only — never suspend and never destroy. An accessor is a prospect with a login
    into one environment, and this one verb is the whole surface they have.

    Takes the ENVIRONMENT, not the user, and the route resolves it with the same
    `_accessor_env` helper every other accessor write uses. Resolving it in here would
    work identically and would hide the property at the call site — and
    `test_no_accessor_write_route_takes_an_environment_id` is right to insist it stays
    visible there, because that is where a later edit would break it.
    """
    from . import job_service

    view = wake_view(db, env)
    if not view["can_wake"]:
        raise AccessorError(view["reason"] or "This environment cannot be started.")

    job = job_service.create_job(
        db, job_type="pov_env_power", created_by=WAKE_ACTOR,
        workgroup=env.workgroup,
        metadata={"environment_id": env.id, "runstate": "running"})
    logger.info("POV %s: woken from the accessor page (job %s)", env.name, job.id)
    out = wake_view(db, env)
    out["job_id"] = job.id
    return out
