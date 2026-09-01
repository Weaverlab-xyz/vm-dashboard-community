"""The POV's customer-facing share link.

Slice 7. Every slice before this one built something an SE touches. This is the only one
that produces a URL somebody outside the account opens, which changes what "careful" means
here — a mistake in the earlier slices is a broken POV, and a mistake here is a lab full of
PAM components published to whoever has the link.

Three rules follow from that, and they are the whole of the design:

**A share link is always password-protected.** Skytap treats the password as optional and
this module does not: a blank field is how an anonymous door gets left open, and nobody
has ever deliberately wanted one. The password is *generated* rather than asked for, so
there is no field to leave blank and no chance of the SE's usual one. It is stored
Fernet-encrypted under the same ``pov/<env_id>/<name>`` shape as the Gateway deploy key,
because a link whose password cannot be re-read a day later is a link that gets recreated
with a weaker one.

**A share link always expires.** Not because Skytap needs it to, but because the failure
mode of a POV share is that it *outlives everyone's attention* — the evaluation ends, the
environment stays up on suspend_on_idle, and the URL keeps working. So there is no "no
expiry" option. The default comes from :data:`DEFAULT_DAYS`, and slice 8's per-POV
``expires_at`` wins over it when set, so the link cannot outlive the environment it points
at.

**Revoking is not the same as destroying.** An SE who realises a link went to the wrong
person needs to kill it in one click without tearing down the POV. So revoke is its own
operation, and it is also called on the destroy path — deleting an environment removes its
publish sets server-side, but the row's ``share_id`` and the stored password are ours to
clear and nothing else would.
"""
from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..database import PovEnvironment
from . import config_service, lab_platforms

logger = logging.getLogger(__name__)


class ShareError(Exception):
    """A refusal carrying the remedy, not just the cause."""


# How long a link lasts when nothing else says. Fourteen days is an evaluation, not a
# standing grant; an SE who needs longer re-shares, which is a deliberate act with a date
# attached rather than a link that quietly never ends.
DEFAULT_DAYS = 14

# The ceiling. A caller may ask for less than DEFAULT_DAYS and often will; asking for more
# than this is refused rather than clamped, because silently shortening someone's link is
# the kind of help that gets discovered by a customer at the wrong moment.
MAX_DAYS = 90

# Where a POV's share password lives. Same `<feature>/<id>/<name>` shape as
# `pov/<id>/gateway_deploy_key`, so it is encrypted at rest, resolvable from an external
# vault by reference, and carried by the config-migration tool — all for free.
_PW_FMT = "pov/{env_id}/share_password"

# Generated password shape. Long enough that the link's security does not rest on the
# expiry, and drawn from an alphabet with no Il1O0 so it survives being read down a phone
# line — which is how these actually get delivered.
_PW_ALPHABET = "".join(c for c in (string.ascii_letters + string.digits)
                       if c not in "Il1O0")
_PW_LENGTH = 20


def password_config_key(env_id: str) -> str:
    return _PW_FMT.format(env_id=env_id)


def generate_password() -> str:
    """A password for something a person outside the account will be handed.

    Public because ``pov_accessor_service`` mints one for the same audience under the same
    constraint, and a second generator would mean a second alphabet and a second length to
    keep in step -- with the difference only showing up on the call where somebody reads a
    password out and it does not work.
    """
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(_PW_LENGTH))


# The old private spelling, kept so nothing in-tree has to change in the same commit that
# widens the audience.
_generate_password = generate_password


def _now() -> datetime:
    return datetime.utcnow()


# ── state ────────────────────────────────────────────────────────────────────

def describe(db: Session, env: PovEnvironment) -> dict:
    """The share's state for one POV row — no network calls.

    Deliberately does NOT include the password. The row is serialised into the POV list
    for every environment on the page; a secret that ships with a list is a secret in
    every browser cache and every screenshot of that page. Revealing it is its own
    endpoint, one row at a time, on purpose.
    """
    return {
        "share_url": env.share_url or "",
        "share_id": env.share_id or "",
        "share_expires_at": (env.share_expires_at.isoformat()
                             if env.share_expires_at else ""),
        "share_expired": is_expired(env),
        "has_share_password": bool(config_service.get_opt(password_config_key(env.id))),
        "shareable": lab_platforms.supports(env.platform, "share_link"),
    }


def is_expired(env: PovEnvironment) -> bool:
    """Whether the link has passed its own expiry.

    Reported rather than acted on. Skytap enforces the expiry server-side, so the link is
    already dead; what this drives is the UI saying "expired" instead of showing a URL that
    looks live. Clearing the row automatically would delete the evidence that a link was
    ever shared, which is the wrong trade during an evaluation.
    """
    if not (env.share_url and env.share_expires_at):
        return False
    return env.share_expires_at <= _now()


def reveal_password(env: PovEnvironment) -> str:
    """The share password, for the one endpoint that shows it.

    ``get_fresh`` rather than ``get``: the config cache is per-process with a few seconds
    of TTL, so an SE who clicks Reveal immediately after Share can land on a worker that
    has not seen the write yet and be told there is no password — for a POV they are
    looking at the link of. A single uncached read is the right price.
    """
    return config_service.get_fresh(password_config_key(env.id))


# ── create ───────────────────────────────────────────────────────────────────

def _expiry_for(env: PovEnvironment, days: int | None) -> datetime:
    """When the link should die.

    Order matters: an explicit request wins, then the POV's own auto-delete time, then the
    default. The middle step is what keeps a share from outliving the environment — a live
    URL to an environment that was reaped is a support call that starts with "the link is
    broken" and ends nowhere near the truth.
    """
    if days is not None:
        if days < 1 or days > MAX_DAYS:
            raise ShareError(
                f"a share link must last between 1 and {MAX_DAYS} days; asked for {days}")
        return _now() + timedelta(days=int(days))
    # Slice 8's column. Present since slice 1's schema, so this needs no version check —
    # NULL simply means the POV has no auto-delete date and the default applies.
    if env.expires_at and env.expires_at > _now():
        return env.expires_at
    return _now() + timedelta(days=DEFAULT_DAYS)


async def create(db: Session, env: PovEnvironment, *, days: int | None = None) -> dict:
    """Publish (or re-publish) the environment and record the link.

    Re-sharing revokes the old link first. Leaving it would mean a POV with two live URLs
    and one stored id, so the older one could never be revoked from here again — the exact
    shape of the orphan the tenant registry and the gateway registry both learned to avoid.
    """
    if not lab_platforms.supports(env.platform, "share_link"):
        label = lab_platforms.capabilities(env.platform)["label"]
        raise ShareError(
            f"{label} has no share links. Give the customer access through PRA instead — "
            f"the jump items the wire-up created already point at these VMs.")
    if not env.platform_environment_id:
        raise ShareError(
            "this POV has no environment on the platform yet; provision it first")

    expires = _expiry_for(env, days)

    if env.share_id:
        # Best-effort: a revoke that fails must not stop the new link, or a POV whose old
        # publish set was deleted in the Skytap UI could never be re-shared from here.
        try:
            await revoke(db, env)
        except Exception:  # noqa: BLE001
            logger.warning("POV %s: could not revoke the previous share link before "
                           "re-sharing", env.id, exc_info=True)

    password = _generate_password()
    adapter = lab_platforms.adapter(env.platform)
    result = await adapter.create_share(
        env.platform_environment_id, password, expires.isoformat(), name=env.name or "")

    # Store the password BEFORE the row, and the row before returning. The link is live on
    # the platform the moment the call above returns, so every ordering question here is
    # "which orphan do I prefer": a stored password with no row is invisible and harmless,
    # a row with no password is a link nobody can open, and a live link with neither is one
    # nobody can revoke. This order leaves only the harmless one possible.
    config_service.set(password_config_key(env.id), password)
    env.share_url = result.get("url") or ""
    env.share_id = result.get("id") or ""
    env.share_expires_at = expires
    db.commit()

    logger.info("POV %s: published share link %s (expires %s)",
                env.id, env.share_id, expires.isoformat())
    return {**describe(db, env), "password": password}


# ── revoke ───────────────────────────────────────────────────────────────────

async def revoke(db: Session, env: PovEnvironment) -> None:
    """Kill the link and clear what this row knows about it.

    The platform call comes first and its failure is NOT swallowed: clearing the row while
    the URL still works would leave a live share nobody can find, let alone revoke. The
    stored password is cleared afterwards, and only on success, for the same reason.
    """
    if not env.share_id:
        return
    if not env.platform_environment_id:
        raise ShareError(
            "this POV has a share id but no environment id, so the link cannot be "
            "revoked from here; remove the sharing portal in the platform's own UI")

    adapter = lab_platforms.adapter(env.platform)
    await adapter.delete_share(env.platform_environment_id, env.share_id)

    env.share_url = None
    env.share_id = None
    env.share_expires_at = None
    db.commit()
    try:
        config_service.delete(password_config_key(env.id))
    except Exception:  # noqa: BLE001 — the link is already dead; a stale password is not
        logger.debug("POV %s: no share password to clear", env.id)
    logger.info("POV %s: share link revoked", env.id)


async def teardown(db: Session, env: PovEnvironment) -> str:
    """Revoke on the destroy path. Returns what was left behind, or "".

    Never raises. Deleting the environment removes its publish sets server-side anyway, so
    a failure here costs a stale row and a stored password rather than a live link — and a
    destroy that stops on it would strand a whole environment over cleanup of something
    that is already gone.
    """
    if not env.share_id:
        clear_password(env)
        return ""
    try:
        await revoke(db, env)
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("POV %s: could not revoke the share link during destroy",
                       env.id, exc_info=True)
        # The row is cleared even though the call failed, because the environment is about
        # to be deleted and the publish set goes with it. Keeping the id would leave a
        # destroyed POV advertising a link.
        stale = env.share_id
        env.share_url = None
        env.share_id = None
        env.share_expires_at = None
        db.commit()
        clear_password(env)
        return (f"the share link ({stale}) could not be revoked before the environment "
                f"was deleted ({type(exc).__name__}); it is removed with the environment, "
                f"but check the account's sharing portals if the destroy also failed")


def clear_password(env: PovEnvironment) -> None:
    try:
        config_service.delete(password_config_key(env.id))
    except Exception:  # noqa: BLE001 — teardown must survive a password already gone
        logger.debug("POV %s: no share password to clear", env.id)
